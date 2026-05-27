"""Key support / resistance level detector -- symmetric over patterns.py.

User-facing concept (set 2026-05-27 during dashboard chart pane work):
the three DITP setups all hinge on the same structural-level question:

  P1 -- price rebounding off a key SUPPORT level
        => need horizontal support BELOW current price
  P2 -- breakout PENDING to a key RESISTANCE level
        => need horizontal resistance ABOVE current price (already
        implemented by DITP/scanner.py via patterns.horizontal_resistance_np)
  P3 -- price already broke a key resistance and is coming back to
        RETEST the broken level (resistance becomes support)
        => need historical resistance peaks NOW BELOW current price

This module unifies all three behind a single `find_key_levels(symbol)`
call so the dashboard chart pane can show "R: $X.XX | S: $Y.YY | P3
retest: $Z.ZZ" without each caller re-deriving the math.

Mathematical structure:
- Resistance above:  reuse patterns.horizontal_resistance_np unchanged
- Support below:     mirror image of the resistance function (swing
                     LOWS instead of highs, "mountain valley" = old
                     swing low followed by rally above by N*ATR,
                     floor gate replaces ceiling gate). Implemented
                     directly here because sign-flipping the resistance
                     function through negation muddles the ceiling-gate
                     percentage semantics.
- Broken resistance: enumerate mountain-anchored swing highs in the
                     lookback that are NOW BELOW current price; price
                     has closed above them = they have flipped to
                     support. Returned as a list of polarity-flip
                     candidates for P3 retests.

All thresholds ticker-relative (ATR multiples) per CLAUDE.md
"Normalized strategy parameters" rule.

Lookback window for daily-chart S/R is 252 trading days (~1 year) for
every level finder in this module. User rule 2026-05-27: "when you
look at Support and Resistance on a daily chart, you will look at 1
year daily chart to look at valley and mountains." Less than a year
under-counts structural levels (you miss the prior earnings cycle and
the prior big swing); more than a year over-counts stale levels that
the market has long since forgotten. 252 is the canonical anchor for
all three finders (resistance above, support below, broken-resistance
polarity-flip).

Selection rules are ASYMMETRIC between above and below current price:

  * Resistance ABOVE current: LOWEST mountain top above current wins
    (= next ceiling to break). Price hasn't tested it yet; higher
    mountains above are FUTURE P2 setups.

  * Support BELOW current: MOST RECENT mountain valley below current
    wins (= where the current rally started). Price has been ABOVE
    older swing lows since dipping through them, so those are no
    longer active support -- the most recent low is the active anchor.

  * Broken resistance (polarity flip): HIGHEST broken mountain below
    current wins (= the most recently broken level in a clean
    uptrend, since each new high breaks the lowest unbroken peak first).

This asymmetry was set 2026-05-27 after the USAR case: $19.36 swing
low (5 days ago) is the active support, not $21.46 (19 days ago).
$21.46 sits HIGHER than $19.36 but price went BELOW $21.46 to make
$19.36, so $21.46 was bypassed and is no longer load-bearing.

Public API:
  horizontal_support_np(highs, lows, closes, current_price, atr, **kw)
      -> dict | None  (same shape as horizontal_resistance_np)
  find_broken_resistance_below(highs, lows, closes, current_price, atr, **kw)
      -> list[dict] (polarity-flip support candidates)
  find_key_levels(symbol) -> dict
      one-stop: {current, atr14, resistance_above, support_below,
                 broken_resistance: [...]}
"""
from __future__ import annotations

import sys
from pathlib import Path

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import numpy as np  # type: ignore  # noqa: E402

from patterns import horizontal_resistance_np, atr_wilder_np  # noqa: E402
import bars_store  # noqa: E402


__version__ = "1.2.0"


def horizontal_support_np(highs, lows, closes, current_price: float,
                          atr: float,
                          *,
                          lookback: int = 252,
                          swing_radius: int = 3,
                          min_touches: int = 1,
                          tick_size: float = 0.01,
                          cluster_tolerance_ticks: int = 3,
                          range_pct: float = 0.02,
                          mountain_min_age_bars: int = 5,
                          mountain_pullback_atr: float = 0.5,
                          ) -> dict | None:
    """MOST-RECENT mountain-valley-anchored horizontal SUPPORT below
    `current_price` (= the most recent in time, not necessarily the
    highest in price below current).

    User correction 2026-05-27 from USAR case: the "first valley"
    (most recent swing low in time) is the active support, not the
    highest swing low below current. The asymmetry between resistance
    and support is deliberate:

      * Resistance above current price: LOWEST in price wins -- it's
        the next ceiling to break, since price hasn't tested it yet.
        Higher mountains above are FUTURE P2 setups, not currently
        relevant. (See patterns.horizontal_resistance_np.)

      * Support below current price: MOST RECENT in time wins -- it's
        where the current up-move started, the active anchor of the
        rally. Older swing lows above the most-recent one were
        bypassed when price went below them, so they're no longer
        active support (even if they sit above the most-recent low).

    USAR example: swing lows at $19.36 (5 days ago) and $21.46 (19
    days ago). $21.46 is HIGHER but $19.36 is MORE RECENT. Price went
    BELOW $21.46 to make $19.36, then rallied back above both. The
    active support is $19.36 (the origin of the current rally), not
    $21.46 (a broken-and-bypassed level).

    Two-stage process:

    1. Enumerate every swing LOW in `lookback` (a bar is a swing low
       if its low is the local min within +/- `swing_radius`).
    2. Filter to "mountain valleys" -- swing lows that are
         (a) old enough: >= `mountain_min_age_bars` from the latest bar
         (b) followed by a real rally: at least one subsequent HIGH
             >= valley + `mountain_pullback_atr` * atr

    The level chosen = the MOST RECENT IN TIME mountain valley below
    `current_price` (= the last bounce point that anchors the current
    rally). If none qualify, falls back to the most-recent non-mountain
    swing low below current (fresh-support fallback).

    The RANGE around that level = consensus of mountain valleys within
    +/- `range_pct` of the chosen level.

    **No floor gate.** The resistance function rejects mid-downtrend
    bounces by demanding the chosen level be the HIGHEST mountain in
    the window. The naive mirror would demand the chosen support be
    the LOWEST mountain valley -- but that's wrong for our use case.
    In an UPTREND (the only case where P1 rebound is meaningful), the
    lowest mountain valley is typically 20-30% below current price,
    from early in the lookback; the user-relevant support is the
    RECENT pullback low. Rejecting it because there's an older,
    deeper valley defeats the purpose. So: no floor gate; the
    closest-in-time mountain valley below current wins.

    Cluster gate: the chosen level must have >= `min_touches` swing
    lows (mountain OR non-mountain) within +/- `cluster_tolerance_ticks
    * tick_size` (default +/-$0.03 = 3 ticks). Absolute-tick tolerance
    per user rule 2026-05-27 ("the placeholder cannot be too wide").

    Returns dict with the same field shape as horizontal_resistance_np:
      level, cluster_touches, mountain_anchors, range_low, range_high,
      range_mountains.

    Returns None when: too few bars, no swings below current, or
    cluster gate fails.
    """
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    lb = min(lookback, len(h))
    window_h = h[-lb:]
    window_l = l[-lb:]
    if len(window_l) < swing_radius * 2 + 1:
        return None

    # 1. Swing lows in window (local min within +/- swing_radius)
    swings: list[tuple[int, float]] = []
    for i in range(swing_radius, len(window_l) - swing_radius):
        if window_l[i] == window_l[i - swing_radius:i + swing_radius + 1].min():
            swings.append((i, float(window_l[i])))
    if len(swings) < min_touches:
        return None

    # 2. Mountain-valley filter: old enough + rally above by N*ATR
    mountains: list[tuple[int, float]] = []
    last_idx = len(window_l) - 1
    for i, lo in swings:
        if (last_idx - i) < mountain_min_age_bars:
            continue
        if i + 1 >= len(window_h):
            continue
        post = window_h[i + 1:]
        if (post.max() if len(post) else -float("inf")) >= lo + mountain_pullback_atr * atr:
            mountains.append((i, lo))
    mountain_idxs = {i for i, _ in mountains}

    # 3. Most-recent mountain valley below current = highest INDEX
    #    below current (most recent in time, not highest in price).
    #    User correction 2026-05-27 from USAR case: the active support
    #    is the most recent low (the origin of the current rally),
    #    NOT the highest swing low below current. Older swing lows
    #    above the most-recent one were bypassed when price dipped
    #    through them -- no longer active support.
    swings_below = [(i, lo) for i, lo in swings if lo < current_price]
    if not swings_below:
        return None
    mountains_below = [(i, lo) for i, lo in swings_below if i in mountain_idxs]
    if mountains_below:
        i_imm, lo_imm = max(mountains_below, key=lambda x: x[0])
    else:
        i_imm, lo_imm = max(swings_below, key=lambda x: x[0])
    level = float(lo_imm)

    # Range = consensus of mountain valleys within +/- range_pct of level.
    if mountains_below:
        range_mtns_only = [lo for _, lo in mountains_below
                           if abs(lo - level) / level <= range_pct]
    else:
        range_mtns_only = [level]
    range_low = min(range_mtns_only)
    range_high = max(range_mtns_only)
    n_range_mountains = len(range_mtns_only) if mountains_below else 0

    # (No floor gate -- see docstring rationale.)

    # Cluster touches around the chosen level. Absolute tick-based
    # tolerance per user rule 2026-05-27: "the placeholder cannot be
    # too wide... plus minus 3 tick".
    cluster_band = cluster_tolerance_ticks * tick_size
    cluster = [(i, lo) for i, lo in swings
               if abs(lo - level) <= cluster_band]
    if len(cluster) < min_touches:
        return None
    n_mountains_in_cluster = sum(1 for i, _ in cluster if i in mountain_idxs)
    return {
        "level": level,
        "cluster_touches": len(cluster),
        "mountain_anchors": n_mountains_in_cluster,
        "range_low": range_low,
        "range_high": range_high,
        "range_mountains": n_range_mountains,
    }


def find_broken_resistance_below(highs, lows, closes, current_price: float,
                                 atr: float,
                                 *,
                                 lookback: int = 252,
                                 swing_radius: int = 3,
                                 mountain_min_age_bars: int = 5,
                                 mountain_pullback_atr: float = 0.5,
                                 tick_size: float = 0.01,
                                 breakout_ticks: int = 3,
                                 ) -> list[dict]:
    """Return the IMMEDIATE NEAREST broken-resistance level below
    current price (= the HIGHEST mountain peak BELOW current price
    that has been clearly broken above by > breakout_ticks * tick_size),
    or [] when no such level exists.

    User framework reintegration 2026-05-27: each mountain peak is an
    independent P2 → P3 lifecycle. When price breaks above a mountain
    top, THAT specific mountain transitions from resistance to support
    (polarity flip). Higher unbroken mountains above are FUTURE P2
    setups, NOT disqualifiers of the current P3 retest opportunity.

    History:
    - v1.0.0: returned all mountain peaks below current, deduped + top-3.
      Incorrectly surfaced stale lower-mountain retests in cases where
      the immediate-nearest above hadn't been broken yet (USAR case
      tagged P3 at $25.95 when $28.69 was the active P2 ceiling).
    - v1.1.0: over-corrected — required the ABSOLUTE HIGHEST mountain
      in the lookback to be broken. Too restrictive: USAR's highest
      mountain was $43.98, an irrelevant historical peak. The fix
      blocked even legitimate P3 candidates further down the chart.
    - v1.2.0 (this): return the IMMEDIATE NEAREST mountain below current
      (= highest mountain below current price). The P3 detector's own
      staleness window (3-45 bars since breakout) + retest-touch gate
      + reaction-magnitude gate handle the rest.

    Tick tolerance for "clearly broken": `current_price > immediate
    nearest level + breakout_ticks * tick_size` (default 3 ticks ×
    $0.01 = $0.03). Deliberate exception to CLAUDE.md's ATR-relative
    rule -- this is a noise-suppression check on the level
    determination, not a setup-tightness threshold.

    Returns at most ONE dict (the immediate-nearest broken mountain):
      level     : the broken-resistance price
      bars_ago  : trading days since the swing-high bar
      mountain  : bool -- always True

    Empty list = no mountain below current is clearly broken (price
    is either too close to or below the highest mountain below).
    """
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    lb = min(lookback, len(h))
    window_h = h[-lb:]
    window_l = l[-lb:]
    if len(window_h) < swing_radius * 2 + 1:
        return []

    last_idx = len(window_h) - 1
    peaks: list[dict] = []
    for i in range(swing_radius, len(window_h) - swing_radius):
        if window_h[i] != window_h[i - swing_radius:i + swing_radius + 1].max():
            continue
        if (last_idx - i) < mountain_min_age_bars:
            continue
        peak = float(window_h[i])
        post = window_l[i + 1:]
        if len(post) == 0:
            continue
        # Mountain qualification: subsequent pullback by N*ATR
        if post.min() > peak - mountain_pullback_atr * atr:
            continue
        peaks.append({
            "level": peak,
            "bars_ago": last_idx - i,
            "mountain": True,
        })

    if not peaks:
        return []

    # Filter to mountains clearly BELOW current price (broken above
    # by > breakout_ticks * tick_size).
    broken_threshold = current_price - breakout_ticks * tick_size
    broken_mountains = [p for p in peaks if p["level"] < broken_threshold]
    if not broken_mountains:
        return []

    # Immediate nearest broken = HIGHEST level below current. This is
    # the polarity-flip candidate -- the mountain most recently
    # transitioned from resistance to support.
    immediate = max(broken_mountains, key=lambda d: d["level"])
    return [immediate]


def _round_level_dict(r: dict | None) -> dict | None:
    if r is None:
        return None
    return {
        "level":            round(r["level"], 2),
        "cluster_touches":  r["cluster_touches"],
        "mountain_anchors": r["mountain_anchors"],
        "range_low":        round(r["range_low"], 2),
        "range_high":       round(r["range_high"], 2),
        "range_mountains":  r["range_mountains"],
    }


def find_key_levels(symbol: str) -> dict:
    """One-stop S/R lookup used by the dashboard chart pane.

    Returns a payload of the form:
      {
        symbol             : "AAPL"
        current            : 180.42       (last daily close)
        atr14              : 3.21         (Wilder ATR)
        resistance_above   : { level, range_low, range_high, ... } | None
        support_below      : { level, range_low, range_high, ... } | None
        broken_resistance  : [ { level, bars_ago, mountain }, ... ]
      }

    Distance fields aren't pre-computed -- the caller has `current`
    and each `level`, so distance-in-ATR is one subtraction away. We
    keep the response shape narrow.
    """
    bars = bars_store.load_bars(symbol, timeframe="daily")
    out_empty = {
        "symbol":            symbol.upper(),
        "current":           None,
        "atr14":             None,
        "resistance_above":  None,
        "support_below":     None,
        "broken_resistance": [],
    }
    # User rule 2026-05-27: daily-chart S/R looks at a 1-year window
    # (~252 trading days) for valleys and mountains. Require enough
    # bars that the lookback can be fully populated (252) plus a
    # small ATR-warmup buffer.
    if len(bars) < 252 + 14:
        return out_empty
    highs  = np.array([b["h"] for b in bars], dtype=float)
    lows   = np.array([b["l"] for b in bars], dtype=float)
    closes = np.array([b["c"] for b in bars], dtype=float)
    atr = atr_wilder_np(highs, lows, closes, period=14)
    if atr <= 0:
        return {**out_empty, "current": round(float(closes[-1]), 2)}
    current = float(closes[-1])

    # Use the relaxed defaults (mountain_min_age_bars=10,
    # mountain_pullback_atr=0.5) -- see patterns.horizontal_resistance_np
    # docstring for rationale (user chart-reading framework 2026-05-27).
    resistance = horizontal_resistance_np(
        highs, lows, closes, current, atr,
        lookback=252, swing_radius=3, min_touches=1,
        tick_size=0.01, cluster_tolerance_ticks=3, range_pct=0.02,
        mountain_min_age_bars=5, mountain_pullback_atr=0.5,
        max_below_window_high_pct=1.0,
    )
    support = horizontal_support_np(
        highs, lows, closes, current, atr,
        lookback=252, swing_radius=3, min_touches=1,
        tick_size=0.01, cluster_tolerance_ticks=3, range_pct=0.02,
        mountain_min_age_bars=5, mountain_pullback_atr=0.5,
    )
    broken = find_broken_resistance_below(
        highs, lows, closes, current, atr,
        lookback=252, swing_radius=3,
        mountain_min_age_bars=5, mountain_pullback_atr=0.5,
        tick_size=0.01, breakout_ticks=3,
    )

    return {
        "symbol":            symbol.upper(),
        "current":           round(current, 2),
        "atr14":             round(atr, 2),
        "resistance_above":  _round_level_dict(resistance),
        "support_below":     _round_level_dict(support),
        "broken_resistance": [{"level": round(d["level"], 2),
                                "bars_ago": d["bars_ago"],
                                "mountain": d["mountain"]} for d in broken],
    }


# ---------- CLI smoke test ----------

def _cli(argv: list[str]) -> int:
    """py resources/sr_levels.py <SYM> [<SYM>...]"""
    if not argv:
        sys.stderr.write("usage: py resources/sr_levels.py <SYM> [<SYM>...]\n")
        return 2
    for sym in argv:
        r = find_key_levels(sym)
        print(f"\n--- {sym.upper()} ---")
        print(f"  current : {r['current']}    atr14: {r['atr14']}")
        if r["resistance_above"]:
            ra = r["resistance_above"]
            d_atr = (ra["level"] - r["current"]) / r["atr14"] if r["atr14"] else 0.0
            print(f"  R above : ${ra['level']:.2f}  (+{d_atr:.2f} ATR)  "
                  f"touches={ra['cluster_touches']}  mountains={ra['mountain_anchors']}")
        else:
            print("  R above : (none)")
        if r["support_below"]:
            sb = r["support_below"]
            d_atr = (r["current"] - sb["level"]) / r["atr14"] if r["atr14"] else 0.0
            print(f"  S below : ${sb['level']:.2f}  (-{d_atr:.2f} ATR)  "
                  f"touches={sb['cluster_touches']}  mountains={sb['mountain_anchors']}")
        else:
            print("  S below : (none)")
        if r["broken_resistance"]:
            print(f"  P3 retest candidates ({len(r['broken_resistance'])}):")
            for d in r["broken_resistance"]:
                d_atr = (r["current"] - d["level"]) / r["atr14"] if r["atr14"] else 0.0
                print(f"    ${d['level']:.2f}  (-{d_atr:.2f} ATR)  {d['bars_ago']} bars ago")
        else:
            print("  P3 retest: (none)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
