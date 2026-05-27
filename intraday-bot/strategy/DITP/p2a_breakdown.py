"""DITP P2a detector -- pending breakdown below a horizontal SUPPORT.

User framing 2026-05-27: *"a break below support will be a P2a setup."*
P2a is the SHORT-SIDE mirror of P2 (breakout above resistance). It
fires when price is approaching its immediate-nearest support from
above with BEARISH momentum, about to break down. Signal candle has
bearish anatomy with no lower tail -- a clean breakdown approach.

Detection flow (per symbol):
  1. Load daily bars. Need >= cfg.lookback + 14 (1-year window + ATR warmup).
  2. Trend gate: EMA20 < EMA50 < EMA200 AND close < EMA200 (downtrend
     stack). Breakdowns are continuation signals; in an uptrend the
     same level is a P1 rebound candidate, not P2a.
  3. Today's candle gate: bearish (close < open) AND close in LOWER
     half of bar range (breakdown momentum, not a wick reversal).
  4. Support discovery via sr_levels.horizontal_support_np (most-recent
     mountain valley below current).
  5. Pending breakdown: today's close still ABOVE support (the
     breakdown hasn't happened yet). Today's low can be at/near support.
  6. Proximity: close within max_distance_atr * ATR ABOVE support.
  7. Reject if already broken below: a daily close BELOW support
     within breakdown_grace_days marks the symbol as already in P3a
     territory, not pending P2a.
  8. No lower tail: (close - low) / range <= max_lower_tail_ratio.
     A long lower tail = the bar BOUNCED off support intraday =
     P1a rebound territory, not breakdown.

Per CLAUDE.md normalization rule: thresholds ticker-relative.

Public API:
  detect_p2a_breakdown(symbol: str, cfg: P2aBreakdownConfig) -> dict | None
  scan_universe(symbols: Iterable[str], cfg: P2aBreakdownConfig) -> list[dict]
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
from sr_levels import horizontal_support_np  # noqa: E402
import bars_store  # noqa: E402


__version__ = "1.0.0"


@dataclass
class P2aBreakdownConfig:
    """All thresholds ticker-relative per CLAUDE.md normalization rule.
    Mirror of P2Config / P1RebConfig for the short side."""
    # Support discovery
    lookback:                  int   = 252
    swing_radius:              int   = 3
    mountain_min_age_bars:     int   = 5
    mountain_pullback_atr:     float = 0.5
    tick_size:                 float = 0.01
    cluster_tolerance_ticks:   int   = 3
    # Breakdown gates
    max_distance_atr:          float = 1.5     # close within N*ATR above support
    max_lower_tail_ratio:      float = 0.15    # tail rejection of breakdown
    require_bearish_close:     bool  = True
    max_close_position:        float = 0.5     # close in lower half of bar
    # Already-broken check (rejection): if support has been broken-below
    # in the last N days, the symbol is past P2a (P3a or active downtrend)
    recent_breakdown_lookback: int   = 15
    breakdown_grace_days:      int   = 2
    # Trend gates
    require_below_ema200:      bool  = True
    require_stack:             bool  = True


def detect_p2a_breakdown(symbol: str, cfg: P2aBreakdownConfig) -> dict | None:
    """Apply P2a rules to one symbol's daily bars. Returns candidate
    dict or None.

    Candidate fields:
      symbol                    : uppercase
      support_level             : float, the support about to break
      support_range_low/high    : consensus zone
      last_close/open/high/low  : today's OHLC
      distance_atr              : (last_close - support_level) / ATR14
      lower_tail_ratio          : (min(open, close) - low) / range
      atr14                     : Wilder ATR(14)
      close_position_in_range   : (close - low) / (high - low)
      ema20/50/200              : current EMA values
      score                     : composite
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

    # Trend gates: downtrend required.
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

    # Support discovery
    sup = horizontal_support_np(
        highs, lows, closes, last_close, atr,
        lookback=cfg.lookback,
        swing_radius=cfg.swing_radius,
        min_touches=1,
        tick_size=cfg.tick_size,
        cluster_tolerance_ticks=cfg.cluster_tolerance_ticks,
        mountain_min_age_bars=cfg.mountain_min_age_bars,
        mountain_pullback_atr=cfg.mountain_pullback_atr,
    )
    if sup is None:
        return None
    support_level = float(sup["level"])

    # Pending breakdown: close still ABOVE support.
    if last_close <= support_level:
        return None
    distance_atr = (last_close - support_level) / atr
    if distance_atr > cfg.max_distance_atr:
        return None
    # Today's low can't already be way below support (use the same
    # logic in reverse): the low must not have BROKEN the level
    # already (today's break would graduate this to P3a, not P2a).
    if last_low < support_level:
        return None

    # No lower tail (rejection of the breakdown).
    body_bot = min(last_open, last_close)
    lower_tail = body_bot - last_low
    lower_tail_ratio = lower_tail / rng
    if lower_tail_ratio > cfg.max_lower_tail_ratio:
        return None

    # Recent-breakdown rejection check: if support has been broken
    # below in lookback (close[j] < support) within grace_days, the
    # symbol is past P2a state.
    lb_breach = min(cfg.recent_breakdown_lookback, len(closes))
    last_breach_idx: int | None = None
    for j in range(len(closes) - 1, len(closes) - lb_breach - 1, -1):
        if j < 0:
            break
        if float(closes[j]) < support_level:
            last_breach_idx = j
            break
    if last_breach_idx is not None:
        days_since_breach = (len(closes) - 1) - last_breach_idx
        if days_since_breach <= cfg.breakdown_grace_days:
            return None  # already in P3a/active breakdown territory

    # Composite score: level validation + proximity + breakdown anatomy
    validation = (sup["mountain_anchors"] * 10) + (sup["cluster_touches"] * 2)
    proximity = max(0, 10 - int(distance_atr * 5))
    anatomy = 20 if lower_tail_ratio <= 0.02 else (
              15 if lower_tail_ratio <= 0.05 else (
              10 if lower_tail_ratio <= 0.10 else 5))
    score = validation + proximity + anatomy

    return {
        "symbol":                   symbol.upper(),
        "support_level":            round(support_level, 2),
        "support_range_low":        round(float(sup["range_low"]), 2),
        "support_range_high":       round(float(sup["range_high"]), 2),
        "support_touches":          int(sup["cluster_touches"]),
        "support_mountains":        int(sup["mountain_anchors"]),
        "last_close":               round(last_close, 2),
        "last_open":                round(last_open, 2),
        "last_high":                round(last_high, 2),
        "last_low":                 round(last_low, 2),
        "distance_atr":             round(float(distance_atr), 2),
        "lower_tail_ratio":         round(float(lower_tail_ratio), 3),
        "atr14":                    round(float(atr), 2),
        "close_position_in_range":  round(float(close_position), 3),
        "ema20":                    round(float(ema20[-1]), 2),
        "ema50":                    round(float(ema50[-1]), 2),
        "ema200":                   round(float(ema200[-1]), 2),
        "score":                    int(score),
    }


def scan_universe(symbols: Iterable[str], cfg: P2aBreakdownConfig) -> list[dict]:
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_p2a_breakdown(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_p2a_breakdown failed: {exc}\n")
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
    cfg = P2aBreakdownConfig()
    print(f"# P2a breakdown scan: {len(symbols)} symbols")
    out = scan_universe(symbols, cfg)
    print(f"# {len(out)} candidates")
    for c in out:
        print(f"  {c['symbol']:<6}  S=${c['support_level']:>7.2f}  "
              f"close=${c['last_close']:>7.2f}  +{c['distance_atr']:>4.2f}ATR  "
              f"l_tail={c['lower_tail_ratio']:>4.2f}  mtns={c['support_mountains']}  "
              f"score={c['score']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
