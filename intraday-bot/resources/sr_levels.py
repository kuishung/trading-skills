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


__version__ = "1.0.0"


def horizontal_support_np(highs, lows, closes, current_price: float,
                          atr: float,
                          *,
                          lookback: int = 120,
                          swing_radius: int = 3,
                          min_touches: int = 2,
                          cluster_band_pct: float = 0.01,
                          range_pct: float = 0.02,
                          mountain_min_age_bars: int = 15,
                          mountain_pullback_atr: float = 2.0,
                          ) -> dict | None:
    """Most-recent mountain-valley-anchored horizontal SUPPORT below
    `current_price`. Mirror of patterns.horizontal_resistance_np, with
    one deliberate asymmetry (no floor gate -- see below).

    Two-stage process:

    1. Enumerate every swing LOW in `lookback` (a bar is a swing low
       if its low is the local min within +/- `swing_radius`).
    2. Filter to "mountain valleys" -- swing lows that are
         (a) old enough: >= `mountain_min_age_bars` from the latest bar
         (b) followed by a real rally: at least one subsequent HIGH
             >= valley + `mountain_pullback_atr` * atr

    The level chosen = the most recent in time among mountain valleys
    below `current_price`. If none qualify, falls back to the most
    recent non-mountain swing low (fresh-support fallback).

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
    lows (mountain OR non-mountain) within +/- `cluster_band_pct`.

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

    # 3. Most-recent mountain valley below current price (fallback to
    #    most-recent non-mountain swing if none qualify).
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

    # Cluster touches around the chosen level
    cluster = [(i, lo) for i, lo in swings
               if abs(lo - level) / level <= cluster_band_pct]
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
                                 lookback: int = 180,
                                 swing_radius: int = 3,
                                 mountain_min_age_bars: int = 15,
                                 mountain_pullback_atr: float = 2.0,
                                 dedup_pct: float = 0.01,
                                 max_results: int = 3,
                                 ) -> list[dict]:
    """Enumerate mountain-anchored swing HIGHS in lookback that are now
    BELOW `current_price`. These are P3 polarity-flip candidates:
    historic resistance that price has CLOSED above and may now retest
    as support.

    Returns a list of dicts (closest-to-current first):
      level     : the broken-resistance price
      bars_ago  : trading days between the swing-high bar and the
                  latest bar (proxy for "how stale is this level")
      mountain  : bool -- always True (non-mountain peaks rejected)

    Dedup: levels within `dedup_pct` of each other collapse to the
    higher (closer to current) one. Max `max_results` returned.

    Empty list when no qualifying peaks; never None.
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
        # P3 = price has already BROKEN above the level. Use strict <
        # so a peak still acting as overhead (handled by the resistance
        # finder) doesn't leak in here.
        if peak >= current_price:
            continue
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
    # Sort closest-to-current-price first (highest level wins, since
    # they're all below current price).
    peaks.sort(key=lambda d: -d["level"])
    # Dedup within `dedup_pct`
    dedup: list[dict] = [peaks[0]]
    for p in peaks[1:]:
        if abs(p["level"] - dedup[-1]["level"]) / dedup[-1]["level"] > dedup_pct:
            dedup.append(p)
        if len(dedup) >= max_results:
            break
    return dedup[:max_results]


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
    if len(bars) < 50:
        return out_empty
    highs  = np.array([b["h"] for b in bars], dtype=float)
    lows   = np.array([b["l"] for b in bars], dtype=float)
    closes = np.array([b["c"] for b in bars], dtype=float)
    atr = atr_wilder_np(highs, lows, closes, period=14)
    if atr <= 0:
        return {**out_empty, "current": round(float(closes[-1]), 2)}
    current = float(closes[-1])

    resistance = horizontal_resistance_np(
        highs, lows, closes, current, atr,
        lookback=120, swing_radius=3, min_touches=2,
        cluster_band_pct=0.01, range_pct=0.02,
        mountain_min_age_bars=15, mountain_pullback_atr=2.0,
        max_below_window_high_pct=0.02,
    )
    support = horizontal_support_np(
        highs, lows, closes, current, atr,
        lookback=120, swing_radius=3, min_touches=2,
        cluster_band_pct=0.01, range_pct=0.02,
        mountain_min_age_bars=15, mountain_pullback_atr=2.0,
    )
    broken = find_broken_resistance_below(
        highs, lows, closes, current, atr,
        lookback=180, swing_radius=3,
        mountain_min_age_bars=15, mountain_pullback_atr=2.0,
        dedup_pct=0.01, max_results=3,
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
