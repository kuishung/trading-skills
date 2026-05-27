"""DITP P1 detector -- rebound off a horizontal SUPPORT level.

User framing 2026-05-27: "P1 is price rebounding key support level, so
you must be able to identify the key support level with a horizontal
support." This is the long-side mirror of DITP/scanner.py's P2 (breakout
to horizontal resistance): instead of price approaching overhead R, P1
catches price bouncing UP off a structural support level. The same
"mountain"-style validation (old swing low + subsequent rally) is what
makes the level meaningful.

Detection flow (per symbol):
  1. Load daily bars. Need >= 220 for EMA200 stability + lookback headroom.
  2. Trend gate: EMA20 > EMA50 > EMA200 AND close > EMA200. Bounces are
     meaningful only inside an established uptrend; in a downtrend a
     "support" is just a rest stop on the way down.
  3. Today's candle gate: bullish (close > open) AND close in upper
     half of bar range (rebound character).
  4. Support discovery via sr_levels.horizontal_support_np -- the
     symmetric mirror of patterns.horizontal_resistance_np.
  5. Reclaim: today's close must be ABOVE the support level.
  6. Proximity: today's close not too far above the support (within
     max_distance_atr*ATR). Far-above = the bounce already played out.
  7. Touch: scan back lookback_bars days for a bar where
     low <= support + touch_tolerance_atr*ATR. Most-recent wins;
     the touch IS the bounce.
  8. Score for sort order: anchor quality + proximity + recency.

Per CLAUDE.md normalization rule: all thresholds ticker-relative.

Public API:
  detect_p1_rebound(symbol: str, cfg: P1RebConfig) -> dict | None
  scan_universe(symbols: Iterable[str], cfg: P1RebConfig) -> list[dict]
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


__version__ = "1.2.0"


@dataclass
class P1RebConfig:
    """All thresholds ticker-relative per CLAUDE.md normalization rule.

    Defaults mirror the EMA-rebound detector's anatomy gates (close >
    open, close in upper half, recent touch within 5 days). The
    support-specific knobs (mountain age, pullback ATR) match
    sr_levels.horizontal_support_np's defaults so the support that
    shows up in the dashboard chart-strip is the same level that
    qualifies a P1 candidate.
    """
    # Support discovery (passed through to horizontal_support_np).
    # User rule 2026-05-27: daily-chart S/R looks at a 1-year window
    # (~252 trading days). See resources/sr_levels.py docstring.
    support_lookback:       int   = 252
    support_swing_radius:   int   = 3
    support_min_touches:    int   = 1   # user reintegration 2026-05-27: a single confirmed mountain valley is a valid support
    # Cluster tolerance switched to absolute ticks 2026-05-27 per user rule
    # "the placeholder cannot be too wide... plus minus 3 tick". The
    # previous 1% scaled badly with price.
    tick_size:              float = 0.01
    cluster_tolerance_ticks: int  = 3
    support_range_pct:      float = 0.02
    # Relaxed 2026-05-27 from 15 / 2.0 to match user's chart-reading
    # framework. GOOGL's $382.77 valley (9 trading days old, 2.68-ATR
    # rally since) is a clear P1 support per the user's read but
    # didn't qualify under strict criteria. See
    # patterns.horizontal_resistance_np docstring for the rationale.
    support_min_age_bars:   int   = 5
    support_pullback_atr:   float = 0.5
    # Bounce gates. User rule 2026-05-27: "we want to see if price
    # action is bouncing at the horizontal support... if price action
    # react by [re]bouncing in the horizontal support, we have a
    # potential P1 setup." So the touch alone isn't enough -- there
    # must be a visible REACTION of meaningful magnitude.
    touch_lookback_bars:    int   = 3     # how recent the touch must be (tightened from 5)
    touch_tolerance_atr:    float = 0.3   # touch counts if low <= support + tol*ATR
    min_bounce_atr:         float = 0.3   # (last_close - lowest_low_since_touch) / atr must clear this
    max_distance_atr:       float = 1.0   # close within N*ATR above support
    require_bullish_close:  bool  = True
    min_close_position:     float = 0.5   # close in top N of bar range (0.5 = upper half)
    # Trend gates
    require_above_ema200:   bool  = True
    require_stack:          bool  = True


def detect_p1_rebound(symbol: str, cfg: P1RebConfig) -> dict | None:
    """Apply P1 rules to one symbol's daily bars. Returns candidate dict
    or None.

    Candidate fields (kept compatible with ema_rebound.py's shape so
    the dashboard's Setup column rendering doesn't need a separate
    code path):
      symbol                    : uppercase
      support_level             : float, the horizontal support price
      support_range_low         : float (bottom of consensus zone)
      support_range_high        : float (top of consensus zone)
      support_touches           : int (swing lows within 1% cluster)
      support_mountains         : int (mountain-valley anchors in cluster)
      last_close, last_open,
        last_high, last_low     : today's OHLC
      distance_atr              : (last_close - support_level) / ATR14
      days_since_touch          : trading days since the bounce-touch
      atr14                     : Wilder ATR(14)
      close_position_in_range   : (close - low) / (high - low) in [0..1]
      ema20, ema50, ema200      : current EMA values
      score                     : composite (validation + proximity + recency)
    """
    bars = bars_store.load_bars(symbol, timeframe="daily")
    # Need >= cfg.support_lookback bars to fully populate the 1-year
    # window + ATR(14) warmup buffer. User rule 2026-05-27: daily S/R
    # is a 1-year read.
    if len(bars) < cfg.support_lookback + 14:
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

    # Trend gates -- meaningful support reclaim requires an uptrend.
    if cfg.require_stack and not (ema20[-1] > ema50[-1] > ema200[-1]):
        return None
    if cfg.require_above_ema200 and last_close < ema200[-1]:
        return None

    # Bullish-candle gate (rebound character).
    if cfg.require_bullish_close and last_close <= last_open:
        return None
    rng = last_high - last_low
    if rng <= 0:
        return None
    close_position = (last_close - last_low) / rng
    if close_position < cfg.min_close_position:
        return None

    # Support discovery
    sup = horizontal_support_np(
        highs, lows, closes, last_close, atr,
        lookback=cfg.support_lookback,
        swing_radius=cfg.support_swing_radius,
        min_touches=cfg.support_min_touches,
        tick_size=cfg.tick_size,
        cluster_tolerance_ticks=cfg.cluster_tolerance_ticks,
        range_pct=cfg.support_range_pct,
        mountain_min_age_bars=cfg.support_min_age_bars,
        mountain_pullback_atr=cfg.support_pullback_atr,
    )
    if sup is None:
        return None
    support_level = float(sup["level"])

    # Reclaim: today's close must be above the support.
    if last_close <= support_level:
        return None
    distance_atr = (last_close - support_level) / atr
    if distance_atr > cfg.max_distance_atr:
        return None

    # Touch: scan back for a low at/below support + tolerance.
    lb = min(cfg.touch_lookback_bars, len(bars))
    tolerance = cfg.touch_tolerance_atr * atr
    touched_idx: int | None = None
    for j in range(len(bars) - 1, len(bars) - lb - 1, -1):
        if j < 0:
            break
        if lows[j] <= support_level + tolerance:
            touched_idx = j
            break
    if touched_idx is None:
        return None
    days_since_touch = (len(bars) - 1) - touched_idx

    # Reaction-magnitude gate. User wants a visible BOUNCE from the
    # touch's low to today's close -- not just "price touched and
    # drifted sideways". Measure from the lowest low in the touch
    # window (handles multi-bar touches where the deepest low is
    # earlier than the most-recent qualifying touch).
    bounce_low = float(np.min(lows[touched_idx:]))
    bounce_magnitude_atr = (last_close - bounce_low) / atr
    if bounce_magnitude_atr < cfg.min_bounce_atr:
        return None

    # Composite score: validation (mountain anchors + touches) +
    # proximity bonus + recency bonus + reaction-strength bonus.
    validation = (sup["mountain_anchors"] * 10) + (sup["cluster_touches"] * 2)
    proximity = max(0, 10 - int(distance_atr * 5))
    recency = max(0, 5 - days_since_touch)
    # Stronger bounce = more decisive reaction; cap to keep score balanced.
    reaction = min(10, int(bounce_magnitude_atr * 10))
    score = validation + proximity + recency + reaction

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
        "bounce_magnitude_atr":     round(float(bounce_magnitude_atr), 2),
        "days_since_touch":         int(days_since_touch),
        "atr14":                    round(float(atr), 2),
        "close_position_in_range":  round(float(close_position), 3),
        "ema20":                    round(float(ema20[-1]), 2),
        "ema50":                    round(float(ema50[-1]), 2),
        "ema200":                   round(float(ema200[-1]), 2),
        "score":                    int(score),
    }


def scan_universe(symbols: Iterable[str], cfg: P1RebConfig) -> list[dict]:
    """Loop the universe, call detect_p1_rebound on each. Returns
    candidates sorted by score desc, then distance_atr asc. Per-symbol
    exceptions go to stderr (mirrors ema_rebound.scan_universe).
    """
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_p1_rebound(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_p1_rebound failed: {exc}\n")
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c["score"], c["distance_atr"]))
    return out


# ---------- CLI smoke ----------

def _cli(argv: list[str]) -> int:
    """py strategy/DITP/p1_rebound.py [<SYM> ...]
    With no args, scans all symbols in the daily parquet store.
    """
    if argv:
        symbols = [s.upper() for s in argv]
    else:
        symbols = bars_store.list_symbols("daily")
        if not symbols:
            print("(no daily-parquet symbols on disk)")
            return 1
    cfg = P1RebConfig()
    print(f"# P1 rebound scan: {len(symbols)} symbols")
    out = scan_universe(symbols, cfg)
    print(f"# {len(out)} candidates")
    for c in out:
        print(f"  {c['symbol']:<6}  S=${c['support_level']:>7.2f}  "
              f"close=${c['last_close']:>7.2f}  +{c['distance_atr']:>4.2f}ATR  "
              f"bounce={c['bounce_magnitude_atr']:>4.2f}ATR  "
              f"touch={c['days_since_touch']}d  mtns={c['support_mountains']}  "
              f"score={c['score']}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
