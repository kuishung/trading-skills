"""DITP P3a detector -- retest of broken support as resistance (polarity flip down).

User framing 2026-05-27: *"a successful break below (P2a) which
support become a resistance after the break below and price action
come back to test the Support turn resistance is P3a setup."*

P3a is the SHORT-SIDE mirror of P3 (retest of broken resistance as
support). Sequence:
  1. A historical mountain VALLEY existed (the prior support).
  2. Price closed below it some days ago (P2a breakdown).
  3. Price rallied back UP to retest the former support from BELOW.
  4. The retest gets rejected -- bearish reaction.

The substrate is sr_levels.find_broken_support_above v1.3.0, which
returns the immediate-nearest broken-S above current price (= the
lowest mountain valley above current = most recently broken in a
clean downtrend). The detector adds:

  * Staleness window: bars_ago in [breakdown_min_age, breakdown_max_age]
    -- too fresh = still rocketing down, too stale = no longer load-bearing
  * Today's high touched the polarity-flip level (within tolerance)
  * Bearish reaction candle (close < open, close in lower half)
  * Reaction-magnitude gate: (touch_high - close) / atr >= min_reaction_atr

Detection flow (per symbol):
  1. Load daily bars. Need >= cfg.lookback + 14 (1-year window + ATR warmup).
  2. Trend gate: EMA20 < EMA50 < EMA200 AND close < EMA200 (downtrend).
  3. Today's candle gate: bearish + close in lower half.
  4. find_broken_support_above returns the polarity-flip level (or empty).
  5. Staleness window check.
  6. Reclaim-from-below: today's close < level (the rejection held).
  7. Proximity: close within max_distance_atr * ATR below the level.
  8. Retest touch: scan last touch_lookback_bars days for a bar where
     high >= level - tolerance * ATR. The touch IS the retest.
  9. Reaction-magnitude gate.

Per CLAUDE.md normalization rule: thresholds ticker-relative.

Public API:
  detect_p3a_retest(symbol: str, cfg: P3aRetestConfig) -> dict | None
  scan_universe(symbols: Iterable[str], cfg: P3aRetestConfig) -> list[dict]
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

from patterns import ema_np, atr_wilder_np  # noqa: E402
from sr_levels import find_broken_support_above  # noqa: E402
import bars_store  # noqa: E402


__version__ = "1.0.0"


@dataclass
class P3aRetestConfig:
    """All thresholds ticker-relative per CLAUDE.md normalization rule.
    Mirror of P3RetestConfig for the short side."""
    # Broken-S discovery (passed to sr_levels.find_broken_support_above)
    lookback:               int   = 252
    swing_radius:           int   = 3
    mountain_min_age_bars:  int   = 5
    mountain_pullback_atr:  float = 0.5
    # Tick-tolerance breakdown gate
    tick_size:              float = 0.01
    breakdown_ticks:        int   = 3
    # Staleness window
    breakdown_min_age:      int   = 3
    breakdown_max_age:      int   = 45
    # Retest gates
    touch_lookback_bars:    int   = 5
    touch_tolerance_atr:    float = 0.3
    min_reaction_atr:       float = 0.3
    max_distance_atr:       float = 1.0
    require_bearish_close:  bool  = True
    max_close_position:     float = 0.5
    # Trend gates
    require_below_ema200:   bool  = True
    require_stack:          bool  = True


def detect_p3a_retest(symbol: str, cfg: P3aRetestConfig) -> dict | None:
    """Apply P3a rules to one symbol's daily bars. Returns candidate
    dict or None.

    Candidate fields:
      symbol                    : uppercase
      polarity_level            : float, the broken-S level being retested
      bars_since_breakdown      : int, age of the prior-S valley in days
      last_close/open/high/low  : today's OHLC
      distance_atr              : (polarity_level - last_close) / ATR14
      reaction_atr              : (touch_high - last_close) / ATR14
      days_since_touch          : days since the retest-touch
      atr14                     : Wilder ATR(14)
      close_position_in_range   : (close - low) / (high - low)
      ema20/50/200              : current EMA values
      score                     : composite (staleness + proximity + reaction)
    """
    bars = bars_store.load_bars(symbol, timeframe="daily")
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
    if cfg.require_stack and not (ema20[-1] < ema50[-1] < ema200[-1]):
        return None
    if cfg.require_below_ema200 and last_close > ema200[-1]:
        return None

    # Bearish-candle gate.
    if cfg.require_bearish_close and last_close >= last_open:
        return None
    rng = last_high - last_low
    if rng <= 0:
        return None
    close_position = (last_close - last_low) / rng
    if close_position > cfg.max_close_position:
        return None

    # Resolve THE polarity-flip level via the broken-support helper.
    broken = find_broken_support_above(
        highs, lows, closes, last_close, atr,
        lookback=cfg.lookback,
        swing_radius=cfg.swing_radius,
        mountain_min_age_bars=cfg.mountain_min_age_bars,
        mountain_pullback_atr=cfg.mountain_pullback_atr,
        tick_size=cfg.tick_size,
        breakdown_ticks=cfg.breakdown_ticks,
    )
    if not broken:
        return None
    d = broken[0]
    level = float(d["level"])
    bars_ago = int(d["bars_ago"])

    # Staleness window.
    if bars_ago < cfg.breakdown_min_age or bars_ago > cfg.breakdown_max_age:
        return None
    # Reclaim from below: close < level (rejection from above held).
    if last_close >= level:
        return None
    distance_atr = (level - last_close) / atr
    if distance_atr > cfg.max_distance_atr:
        return None

    # Retest-touch: scan back for a high at/above level - tolerance.
    lb = min(cfg.touch_lookback_bars, len(bars))
    tolerance = cfg.touch_tolerance_atr * atr
    touched_idx: int | None = None
    for j in range(len(bars) - 1, len(bars) - lb - 1, -1):
        if j < 0:
            break
        if highs[j] >= level - tolerance:
            touched_idx = j
            break
    if touched_idx is None:
        return None

    # Reaction-magnitude: from the highest high in touch window down to today's close.
    touch_high = float(np.max(highs[touched_idx:]))
    reaction_atr = (touch_high - last_close) / atr
    if reaction_atr < cfg.min_reaction_atr:
        return None
    days_since_touch = (len(bars) - 1) - touched_idx

    # Composite score: staleness + proximity + recency + reaction
    if 7 <= bars_ago <= 28:
        staleness = 20
    elif bars_ago < 7:
        staleness = 10 + (bars_ago - cfg.breakdown_min_age) * 2
    else:
        span = max(1, cfg.breakdown_max_age - 28)
        decay = (bars_ago - 28) / span
        staleness = max(0, int(20 - decay * 20))
    proximity = max(0, 10 - int(distance_atr * 5))
    recency = max(0, 5 - days_since_touch)
    reaction = min(10, int(reaction_atr * 10))
    score = staleness + proximity + recency + reaction

    return {
        "symbol":                   symbol.upper(),
        "polarity_level":           round(level, 2),
        "bars_since_breakdown":     int(bars_ago),
        "last_close":               round(last_close, 2),
        "last_open":                round(last_open, 2),
        "last_high":                round(last_high, 2),
        "last_low":                 round(last_low, 2),
        "distance_atr":             round(float(distance_atr), 2),
        "reaction_atr":             round(float(reaction_atr), 2),
        "days_since_touch":         int(days_since_touch),
        "atr14":                    round(float(atr), 2),
        "close_position_in_range":  round(float(close_position), 3),
        "ema20":                    round(float(ema20[-1]), 2),
        "ema50":                    round(float(ema50[-1]), 2),
        "ema200":                   round(float(ema200[-1]), 2),
        "score":                    int(score),
    }


def scan_universe(symbols: Iterable[str], cfg: P3aRetestConfig) -> list[dict]:
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_p3a_retest(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_p3a_retest failed: {exc}\n")
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c["score"], c["distance_atr"]))
    return out


def _cli(argv: list[str]) -> int:
    if argv:
        symbols = [s.upper() for s in argv]
    else:
        symbols = bars_store.list_symbols("daily")
        if not symbols:
            print("(no daily-parquet symbols on disk)")
            return 1
    cfg = P3aRetestConfig()
    print(f"# P3a retest scan: {len(symbols)} symbols")
    out = scan_universe(symbols, cfg)
    print(f"# {len(out)} candidates")
    for c in out:
        print(f"  {c['symbol']:<6}  flip=${c['polarity_level']:>7.2f}  "
              f"close=${c['last_close']:>7.2f}  -{c['distance_atr']:>4.2f}ATR  "
              f"reaction={c['reaction_atr']:>4.2f}ATR  "
              f"brk={c['bars_since_breakdown']}d  score={c['score']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
