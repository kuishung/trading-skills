"""DITP P1a detector -- bearish rejection at a horizontal RESISTANCE.

User framing 2026-05-27: *"a failed P2 setup will be P1a setup."*
P1a is the SHORT-SIDE inverse of P1 (rebound at support). It fires
when price approached a key resistance and got REJECTED -- price
spiked into the level intraday and closed back below.

v1.1.0 (2026-05-29): user correction *"the shorting strategy P1a,
P2a, P3a has to be based on downtrend chart based on EMA20, 50, 200
for shorting"*. P1a v1.0.0 fired on ANY trend (uptrend or downtrend)
because the candle anatomy was treated as the dominant signal; a
bearish rejection bar at R was considered a "failed P2" regardless
of stack. The user has reclassified: bearish rejection at R in an
UPTREND is a P2 candidate (pending continuation breakout, today
just got rejected -- next attempt is the signal). It's NOT a P1a
short. Only in a DOWNTREND stack (EMA20 < EMA50 < EMA200) is the
rejection at R a tradeable short setup -- the trend is your friend,
and the rejection confirms the downtrend will resume. BE 2026-05-28
case: clean uptrend stack (EMA20 276 > EMA50 237 > EMA200 150) with
a bearish rejection bar at $310 R; this WAS firing P1a (false
positive) and is now correctly rejected by the downtrend gate.

P2a and P3a already had `require_stack=True` (downtrend) since
their v1.0.0 -- this fix brings P1a into alignment.

Detection flow (per symbol):
  1. Load daily bars. Need >= cfg.lookback + 14 (1-year window + ATR warmup).
  2. ATR > 0 sanity check.
  3. Bearish-candle gate: close < open AND close in LOWER half of
     bar range AND upper tail >= min_upper_tail_ratio of range.
  4. Resistance discovery via patterns.horizontal_resistance_np
     (same as P2 / chart-pane R above). Pick the immediate-nearest
     mountain top above current price.
  5. Touch: today's HIGH must have touched the resistance (within
     touch_tolerance_atr * ATR). The high reaching the level IS
     the rejection event.
  6. Reaction-magnitude gate: the FALL from today's high to today's
     close must be >= min_reaction_atr * ATR -- a real rejection
     reaction, not just a small upper tail.
  7. Proximity: today's close not too far BELOW resistance (within
     max_distance_atr * ATR) -- the level still matters as overhead.

Per CLAUDE.md normalization rule: thresholds ticker-relative.

Public API:
  detect_p1a_rejection(symbol: str, cfg: P1aRejectConfig) -> dict | None
  scan_universe(symbols: Iterable[str], cfg: P1aRejectConfig) -> list[dict]
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
import bars_store  # noqa: E402
from symbol_ctx import SymbolContext, build_context  # noqa: E402  (resources/symbol_ctx.py)


__version__ = "1.1.1"


@dataclass
class P1aRejectConfig:
    """All thresholds ticker-relative per CLAUDE.md normalization rule."""
    # Resistance discovery (passed through to horizontal_resistance_np)
    lookback:                  int   = 252
    swing_radius:              int   = 3
    mountain_min_age_bars:     int   = 5
    mountain_pullback_atr:     float = 0.5
    tick_size:                 float = 0.01
    cluster_tolerance_ticks:   int   = 3
    # Rejection gates
    touch_tolerance_atr:       float = 0.3   # high must reach resistance - tol*ATR
    max_distance_atr:          float = 1.0   # close within N*ATR below resistance
    min_upper_tail_ratio:      float = 0.30  # upper_tail / range >= this (rejection)
    min_close_position_max:    float = 0.5   # close in LOWER half (cp <= 0.5)
    min_reaction_atr:          float = 0.3   # (high - close) / atr >= this
    require_bearish_close:     bool  = True  # today's close < today's open
    # Trend gate (added v1.1.0). User rule 2026-05-29: short setups
    # must be on a downtrend chart (EMA20 < EMA50 < EMA200). Brings
    # P1a into alignment with P2a / P3a which already required this.
    require_stack:             bool  = True  # require EMA20 < EMA50 < EMA200


def detect_p1a_rejection(
    symbol: str,
    cfg: P1aRejectConfig,
    ctx: SymbolContext | None = None,
) -> dict | None:
    """Apply P1a rules to one symbol's daily bars. Returns candidate
    dict or None.

    Candidate fields:
      symbol                    : uppercase
      resistance_level          : float, the rejected resistance
      resistance_range_low      : float (bottom of consensus zone)
      resistance_range_high     : float (top of consensus zone)
      last_close, last_open,
        last_high, last_low     : today's OHLC
      distance_atr              : (resistance_level - last_close) / ATR14
      reaction_atr              : (last_high - last_close) / ATR14
      upper_tail_ratio          : (last_high - max(open, close)) / range
      atr14                     : Wilder ATR(14)
      close_position_in_range   : (close - low) / (high - low) in [0..1]
      ema20, ema50, ema200      : current EMA values
      score                     : composite (level validation + rejection
                                  strength + proximity)
    """
    # v1.1.1: ctx-aware (Pass 2 #1). Shared prelude hoisted out.
    if ctx is None:
        ctx = build_context(symbol)
    if ctx is None or len(ctx.bars) < cfg.lookback + 14:
        return None

    bars   = ctx.bars
    closes = ctx.closes
    opens  = ctx.opens
    highs  = ctx.highs
    lows   = ctx.lows
    ema20  = ctx.ema20
    ema50  = ctx.ema50
    ema200 = ctx.ema200
    atr    = ctx.atr14

    # Trend gate (v1.1.0). User rule 2026-05-29: short setups must be
    # on a downtrend chart (EMA20 < EMA50 < EMA200). BE 2026-05-28
    # (clean UPTREND stack with a bearish rejection at $310 R)
    # wrongly fired P1a before this gate landed -- in an uptrend, a
    # rejection at R = P2 candidate setting up its NEXT breakout
    # attempt, not a short.
    if cfg.require_stack and not (ema20[-1] < ema50[-1] < ema200[-1]):
        return None

    last_close = float(closes[-1])
    last_open  = float(opens[-1])
    last_high  = float(highs[-1])
    last_low   = float(lows[-1])

    # Bearish-candle gate.
    if cfg.require_bearish_close and last_close >= last_open:
        return None
    rng = last_high - last_low
    if rng <= 0:
        return None
    close_position = (last_close - last_low) / rng
    if close_position > cfg.min_close_position_max:
        return None
    body_top = max(last_open, last_close)
    upper_tail = last_high - body_top
    upper_tail_ratio = upper_tail / rng
    if upper_tail_ratio < cfg.min_upper_tail_ratio:
        return None

    # Resistance discovery
    r = horizontal_resistance_np(
        highs, lows, closes, last_close, atr,
        lookback=cfg.lookback,
        swing_radius=cfg.swing_radius,
        min_touches=1,
        tick_size=cfg.tick_size,
        cluster_tolerance_ticks=cfg.cluster_tolerance_ticks,
        mountain_min_age_bars=cfg.mountain_min_age_bars,
        mountain_pullback_atr=cfg.mountain_pullback_atr,
    )
    if r is None:
        return None
    resistance_level = float(r["level"])

    # Distance + touch + reaction gates
    distance_atr = (resistance_level - last_close) / atr
    if distance_atr < 0 or distance_atr > cfg.max_distance_atr:
        return None
    # Today's high must have reached the resistance (touched it).
    touch_threshold = resistance_level - cfg.touch_tolerance_atr * atr
    if last_high < touch_threshold:
        return None
    reaction_atr = (last_high - last_close) / atr
    if reaction_atr < cfg.min_reaction_atr:
        return None

    # EMAs already computed above for the trend gate (v1.1.0); reuse here.

    # Composite score: level validation + rejection strength + proximity
    validation = (r["mountain_anchors"] * 10) + (r["cluster_touches"] * 2)
    rejection_strength = min(15, int(reaction_atr * 10))
    tail_quality = min(10, int(upper_tail_ratio * 20))
    proximity = max(0, 10 - int(distance_atr * 5))
    score = validation + rejection_strength + tail_quality + proximity

    return {
        "symbol":                   symbol.upper(),
        "resistance_level":         round(resistance_level, 2),
        "resistance_range_low":     round(float(r["range_low"]), 2),
        "resistance_range_high":    round(float(r["range_high"]), 2),
        "resistance_touches":       int(r["cluster_touches"]),
        "resistance_mountains":     int(r["mountain_anchors"]),
        "last_close":               round(last_close, 2),
        "last_open":                round(last_open, 2),
        "last_high":                round(last_high, 2),
        "last_low":                 round(last_low, 2),
        "distance_atr":             round(float(distance_atr), 2),
        "reaction_atr":             round(float(reaction_atr), 2),
        "upper_tail_ratio":         round(float(upper_tail_ratio), 3),
        "atr14":                    round(float(atr), 2),
        "close_position_in_range":  round(float(close_position), 3),
        "ema20":                    round(float(ema20[-1]), 2),
        "ema50":                    round(float(ema50[-1]), 2),
        "ema200":                   round(float(ema200[-1]), 2),
        "score":                    int(score),
    }


def scan_universe(symbols: Iterable[str], cfg: P1aRejectConfig) -> list[dict]:
    """Loop the universe, call detect_p1a_rejection on each. Returns
    candidates sorted by score desc, then distance_atr asc.
    """
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_p1a_rejection(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_p1a_rejection failed: {exc}\n")
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c["score"], c["distance_atr"]))
    return out


# ---------- CLI smoke ----------

def _cli(argv: list[str]) -> int:
    if argv:
        symbols = [s.upper() for s in argv]
    else:
        symbols = bars_store.list_symbols("daily")
        if not symbols:
            print("(no daily-parquet symbols on disk)")
            return 1
    cfg = P1aRejectConfig()
    print(f"# P1a rejection scan: {len(symbols)} symbols")
    out = scan_universe(symbols, cfg)
    print(f"# {len(out)} candidates")
    for c in out:
        print(f"  {c['symbol']:<6}  R=${c['resistance_level']:>7.2f}  "
              f"close=${c['last_close']:>7.2f}  -{c['distance_atr']:>4.2f}ATR  "
              f"reaction={c['reaction_atr']:>4.2f}ATR  "
              f"tail={c['upper_tail_ratio']:>4.2f}  score={c['score']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
