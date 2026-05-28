"""DITP P3 detector -- retest of broken resistance (polarity flip).

User framing 2026-05-27: "P3 is price have already breakout of a key
resistance level (p2 setup) and the price come back to retest the the
resistance turn support level." So P3 needs THREE things in sequence:

  1. A historic mountain peak existed (the prior resistance).
  2. Price already CLOSED above it some days ago (breakout).
  3. Price has come back to retest it from above; the level should now
     act as support (polarity flip).

The substrate for step 1 is sr_levels.find_broken_resistance_below,
which enumerates mountain-anchored swing highs in the lookback that
are NOW below current price (= price has broken above them). The
detector here adds the staleness gate (breakout must be recent enough
to matter), the retest-touch gate (price actually came back down to
the level), and the bullish-reclaim signal candle.

Detection flow (per symbol):
  1. Load daily bars. Need >= cfg.lookback + 14 (~252 + ATR warmup).
  2. Trend gate: EMA20 > EMA50 > EMA200 AND close > EMA200. P3 is a
     long-side polarity flip, not a dead-cat bounce in a downtrend.
  3. Today's candle gate: bullish (close > open) AND close in upper
     half of bar range (reclaim character, not a weak retest).
  4. Resolve THE relevant broken resistance via
     sr_levels.find_broken_resistance_below v1.2.0: the IMMEDIATE
     NEAREST mountain below current price (highest mountain below)
     that price has clearly broken above (> breakout_ticks * tick_size).
     User framework reintegration 2026-05-27: each mountain peak is an
     independent P2 -> P3 lifecycle; the relevant level is the one
     closest to current price, not the absolute highest in the window.
     Higher unbroken mountains above are FUTURE P2 setups, not
     disqualifiers of the current P3 opportunity. (The earlier v1.1.0
     "absolute highest" gate was over-restrictive, blocking valid P3
     setups whenever some old historical peak loomed unbroken.)
  5. Apply the P3 gates to that one level:
       * Staleness gate: bars_ago in [breakout_min_age, breakout_max_age]
       * Reclaim: today's close > level
       * Proximity: today's close within max_distance_atr*ATR
       * Retest touch: scan last touch_lookback_bars days for a bar
         where low <= level + tolerance*ATR. The touch IS the retest.
       * Reaction gate: bounce_magnitude_atr >= cfg.min_bounce_atr
  6. Score for sort order: staleness + proximity + recency + reaction.

Per CLAUDE.md normalization rule: all thresholds ticker-relative.

Public API:
  detect_p3_retest(symbol: str, cfg: P3RetestConfig) -> dict | None
  scan_universe(symbols: Iterable[str], cfg: P3RetestConfig) -> list[dict]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import numpy as np  # type: ignore  # noqa: E402

from patterns import ema_np, atr_wilder_np, horizontal_resistance_np  # noqa: E402
from sr_levels import find_broken_resistance_below  # noqa: E402
import bars_store  # noqa: E402


__version__ = "1.5.0"


@dataclass
class P3RetestConfig:
    """All thresholds ticker-relative per CLAUDE.md normalization rule.

    Staleness window (`breakout_min_age`..`breakout_max_age`) is the
    main P3-specific knob. Too recent = price is still rocketing up
    from the breakout (no retest yet); too stale = the level is no
    longer load-bearing. The 3..45 default brackets the typical
    polarity-flip window the user described.
    """
    # Broken-R discovery (passed to sr_levels.find_broken_resistance_below).
    # User rule 2026-05-27: daily-chart S/R looks at a 1-year window
    # (~252 trading days). See resources/sr_levels.py docstring.
    lookback:               int   = 252
    swing_radius:           int   = 3
    # Relaxed 2026-05-27 from 15 / 2.0 to match user's chart-reading
    # framework (see patterns.horizontal_resistance_np docstring).
    mountain_min_age_bars:  int   = 5
    mountain_pullback_atr:  float = 0.5
    # Tick-tolerance breakout gate (user rule 2026-05-27, USAR case):
    # the highest mountain in the lookback is THE key resistance. P3
    # requires current price > highest_mountain + breakout_ticks*tick_size,
    # otherwise the symbol is still pending breakout (P2 territory).
    tick_size:              float = 0.01
    breakout_ticks:         int   = 3
    # Staleness window for the breakout. The min_age (3 days) keeps
    # too-fresh peaks out (price still rocketing up from breakout, not
    # yet a retest). The max_age extended 2026-05-28 from 45 -> 252 to
    # accommodate older polarity-flip levels per user NVDA case --
    # $212.19 from Oct 2025 (143 days ago) IS the relevant retest
    # level. The P3 detector's other gates (touch within last 5 days,
    # bullish reclaim, bounce magnitude, upper-tail filter) ensure
    # only ACTIVELY-retested levels qualify; the breakout age itself
    # doesn't need a tight upper bound.
    breakout_min_age:       int   = 3
    breakout_max_age:       int   = 252
    # Retest gates. User rule 2026-05-27: "price action shows reactions
    # in the resistance turned support" -- the retest is only a P3 setup
    # when there is a visible BOUNCE / reaction off the polarity-flip
    # level, not just price drifting near it.
    touch_lookback_bars:    int   = 5     # how recent the retest-touch must be (tightened from 7)
    touch_tolerance_atr:    float = 0.3
    min_bounce_atr:         float = 0.3   # (last_close - lowest_low_since_touch) / atr must clear this
    max_distance_atr:       float = 1.0
    require_bullish_close:  bool  = True
    min_close_position:     float = 0.5
    # Upper-tail rejection filter added 2026-05-28 (v1.4.0). Mirrors P2's
    # max_upper_tail_ratio=0.15 gate. Differentiates a clean P3 reclaim
    # (small upper tail, body in upper half) from a doji-like
    # indecisive bar (big upper tail = rejection at higher resistance).
    # AAOI case 2026-05-27: 46% upper tail = rejection at $187.18,
    # NOT a clean retest of $173.41 -- now correctly excluded.
    max_upper_tail_ratio:   float = 0.15
    # Pin-bar anatomy (added 2026-05-28 for v1.4.0 bullish-OR-pin acceptance).
    # NVDA case 2026-05-27: bullish hammer with close $1.52 below open
    # but body at top of range + long lower wick = clean rebound signal.
    pin_max_body_ratio:         float = 0.40
    pin_min_lower_tail_ratio:   float = 0.50
    # Trend gates
    require_above_ema200:   bool  = True
    require_stack:          bool  = True


def detect_p3_retest(symbol: str, cfg: P3RetestConfig) -> dict | None:
    """Apply P3 rules to one symbol's daily bars. Returns candidate
    dict or None.

    Candidate fields:
      symbol                    : uppercase
      polarity_level            : float, the broken-R level being retested
      bars_since_breakout       : int, age of the prior-R peak in days
      last_close, last_open,
        last_high, last_low     : today's OHLC
      distance_atr              : (last_close - polarity_level) / ATR14
      days_since_touch          : days since the retest-touch
      atr14                     : Wilder ATR(14)
      close_position_in_range   : (close - low) / (high - low) in [0..1]
      ema20, ema50, ema200      : current EMA values
      score                     : composite (staleness sweet spot + proximity + recency)
    """
    bars = bars_store.load_bars(symbol, timeframe="daily")
    # Need >= cfg.lookback bars to fully populate the 1-year window +
    # ATR(14) warmup buffer. User rule 2026-05-27: daily S/R = 1-year read.
    if len(bars) < cfg.lookback + 14:
        return None

    closes = np.array([b["c"] for b in bars], dtype=float)
    opens  = np.array([b["o"] for b in bars], dtype=float)
    highs  = np.array([b["h"] for b in bars], dtype=float)
    lows   = np.array([b["l"] for b in bars], dtype=float)

    ema20  = ema_np(closes, 20)
    ema50  = ema_np(closes, 50)
    ema200 = ema_np(closes, 200)
    atr    = atr_wilder_np(highs, lows, closes, period=14)
    if atr <= 0:
        return None

    last_close = float(closes[-1])
    last_open  = float(opens[-1])
    last_high  = float(highs[-1])
    last_low   = float(lows[-1])

    # Trend gates.
    if cfg.require_stack and not (ema20[-1] > ema50[-1] > ema200[-1]):
        return None
    if cfg.require_above_ema200 and last_close < ema200[-1]:
        return None

    # Bar anatomy. Compute pin-bar / hammer shape up front because
    # v1.4.0 relaxes the bullish-close gate to accept hammer bars
    # (NVDA case: close < open by $1.52 but bar is a clean hammer).
    rng = last_high - last_low
    if rng <= 0:
        return None
    close_position = (last_close - last_low) / rng
    if close_position < cfg.min_close_position:
        return None
    body = abs(last_close - last_open)
    body_ratio = body / rng
    lower_tail = min(last_open, last_close) - last_low
    lower_tail_ratio = lower_tail / rng
    upper_tail = last_high - max(last_open, last_close)
    upper_tail_ratio = upper_tail / rng
    is_pin_bar = (body_ratio <= cfg.pin_max_body_ratio
                  and lower_tail_ratio >= cfg.pin_min_lower_tail_ratio)

    # Bullish-candle gate (v1.4.0 relaxed: close > open OR pin bar).
    if cfg.require_bullish_close and last_close <= last_open and not is_pin_bar:
        return None
    # Upper-tail rejection filter (v1.4.0): clean retest has SMALL upper tail.
    if upper_tail_ratio > cfg.max_upper_tail_ratio:
        return None

    # Resolve THE key broken-resistance (v1.1.0 of sr_levels: returns
    # the HIGHEST mountain only, or [] if it's not clearly broken
    # above by breakout_ticks * tick_size).
    broken = find_broken_resistance_below(
        highs, lows, closes, last_close, atr,
        lookback=cfg.lookback,
        swing_radius=cfg.swing_radius,
        mountain_min_age_bars=cfg.mountain_min_age_bars,
        mountain_pullback_atr=cfg.mountain_pullback_atr,
        tick_size=cfg.tick_size,
        breakout_ticks=cfg.breakout_ticks,
    )
    if not broken:
        return None

    tolerance = cfg.touch_tolerance_atr * atr

    # v1.5.0: P2-zone vs P3-zone discriminator. User USAR case
    # 2026-05-28: USAR today has R-above = $28.69 (10d, $0.50 above
    # close $28.19) AND broken-R = $26.36 (79d, $1.83 below). Both
    # are valid by individual gates, but the active level the price
    # action is testing is $28.69 (P2 territory), NOT $26.36 (stale
    # P3 retest). The discriminator: close should be CLOSER to the
    # broken-R level than to R-above -- i.e., below the midpoint
    # between the two. If close is above midpoint, the symbol is in
    # P2 zone and P3 must not fire.
    #
    # Compute R-above ONCE up front (same parameters horizontal_resistance
    # would use via find_key_levels). Used as the midpoint anchor below.
    r_above_info = horizontal_resistance_np(
        highs, lows, closes, last_close, atr,
        lookback=cfg.lookback,
        swing_radius=cfg.swing_radius,
        min_touches=1,
        tick_size=cfg.tick_size,
        cluster_tolerance_ticks=cfg.breakout_ticks,  # symmetric noise band
        mountain_min_age_bars=cfg.mountain_min_age_bars,
        mountain_pullback_atr=cfg.mountain_pullback_atr,
    )
    r_above_level = float(r_above_info["level"]) if r_above_info else None

    # Test each candidate (closest-to-current first) against the P3
    # gates. First one that passes is the anchor.
    best: dict | None = None
    for d in broken:
        level = float(d["level"])
        bars_ago = int(d["bars_ago"])

        # Staleness window.
        if bars_ago < cfg.breakout_min_age or bars_ago > cfg.breakout_max_age:
            continue
        # Reclaim: close above the polarity-flip level.
        if last_close <= level:
            continue
        distance_atr = (last_close - level) / atr
        if distance_atr > cfg.max_distance_atr:
            continue
        # v1.5.0 zone discriminator: if R-above exists and close is
        # ABOVE the midpoint of (broken-R, R-above), the active level
        # is R-above (P2 territory) -- not the polarity flip.
        if r_above_level is not None:
            midpoint = (r_above_level + level) / 2.0
            if last_close > midpoint:
                continue   # P2 zone, not P3
        # Retest-touch: scan back for a low at/below level + tolerance.
        lb = min(cfg.touch_lookback_bars, len(bars))
        touched_idx: int | None = None
        for j in range(len(bars) - 1, len(bars) - lb - 1, -1):
            if j < 0:
                break
            if lows[j] <= level + tolerance:
                touched_idx = j
                break
        if touched_idx is None:
            continue

        # Reaction-magnitude gate. The user-visible "reaction" off the
        # polarity-flip level must be a real bounce, not a drift. Same
        # math as P1: distance from the lowest low since the touch up
        # to today's close, in ATR units.
        bounce_low = float(np.min(lows[touched_idx:]))
        bounce_magnitude_atr = (last_close - bounce_low) / atr
        if bounce_magnitude_atr < cfg.min_bounce_atr:
            continue

        days_since_touch = (len(bars) - 1) - touched_idx
        best = {
            "polarity_level":       level,
            "bars_since_breakout":  bars_ago,
            "distance_atr":         float(distance_atr),
            "days_since_touch":     days_since_touch,
            "bounce_magnitude_atr": float(bounce_magnitude_atr),
        }
        break

    if best is None:
        return None

    # Composite score:
    #   - Staleness sweet spot: peak at 14-21 days, decay outward.
    #   - Proximity: closer to the level = stronger retest.
    #   - Recency: more recent touch = fresher.
    bars_ago = best["bars_since_breakout"]
    if 7 <= bars_ago <= 28:
        staleness = 20
    elif bars_ago < 7:
        staleness = 10 + (bars_ago - cfg.breakout_min_age) * 2
    else:
        # Linearly decay from 20 at day 28 to 0 at breakout_max_age
        span = max(1, cfg.breakout_max_age - 28)
        decay = (bars_ago - 28) / span
        staleness = max(0, int(20 - decay * 20))
    proximity = max(0, 10 - int(best["distance_atr"] * 5))
    recency = max(0, 5 - best["days_since_touch"])
    # Reaction strength -- stronger bounce off the polarity flip = more
    # conviction. Cap at 10 to keep the score balanced against staleness.
    reaction = min(10, int(best["bounce_magnitude_atr"] * 10))
    score = staleness + proximity + recency + reaction

    return {
        "symbol":                   symbol.upper(),
        "polarity_level":           round(best["polarity_level"], 2),
        "bars_since_breakout":      int(best["bars_since_breakout"]),
        "last_close":               round(last_close, 2),
        "last_open":                round(last_open, 2),
        "last_high":                round(last_high, 2),
        "last_low":                 round(last_low, 2),
        "distance_atr":             round(float(best["distance_atr"]), 2),
        "bounce_magnitude_atr":     round(float(best["bounce_magnitude_atr"]), 2),
        "days_since_touch":         int(best["days_since_touch"]),
        "atr14":                    round(float(atr), 2),
        "close_position_in_range":  round(float(close_position), 3),
        "upper_tail_ratio":         round(float(upper_tail_ratio), 3),
        "is_pin_bar":               bool(is_pin_bar),
        "ema20":                    round(float(ema20[-1]), 2),
        "ema50":                    round(float(ema50[-1]), 2),
        "ema200":                   round(float(ema200[-1]), 2),
        "score":                    int(score),
    }


def scan_universe(symbols: Iterable[str], cfg: P3RetestConfig) -> list[dict]:
    """Loop the universe, call detect_p3_retest on each. Returns
    candidates sorted by score desc, then distance_atr asc.
    """
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_p3_retest(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_p3_retest failed: {exc}\n")
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c["score"], c["distance_atr"]))
    return out


# ---------- CLI smoke ----------

def _cli(argv: list[str]) -> int:
    """py strategy/DITP/p3_retest.py [<SYM> ...]
    With no args, scans all symbols in the daily parquet store.
    """
    if argv:
        symbols = [s.upper() for s in argv]
    else:
        symbols = bars_store.list_symbols("daily")
        if not symbols:
            print("(no daily-parquet symbols on disk)")
            return 1
    cfg = P3RetestConfig()
    print(f"# P3 retest scan: {len(symbols)} symbols")
    out = scan_universe(symbols, cfg)
    print(f"# {len(out)} candidates")
    for c in out:
        print(f"  {c['symbol']:<6}  flip=${c['polarity_level']:>7.2f}  "
              f"close=${c['last_close']:>7.2f}  +{c['distance_atr']:>4.2f}ATR  "
              f"bounce={c['bounce_magnitude_atr']:>4.2f}ATR  "
              f"brk={c['bars_since_breakout']}d  touch={c['days_since_touch']}d  "
              f"score={c['score']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
