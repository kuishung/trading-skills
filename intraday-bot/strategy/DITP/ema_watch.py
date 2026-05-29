"""EMA-watch detector -- PRE-CONFIRMATION variant of ema_rebound.

User request 2026-05-29: "AAOI is also EMA20 candidate". AAOI today
(2026-05-28) is in a clean 20>50>200 trend stack and its low pierced
EMA20 ($166.69 vs EMA20 $173.05), but today's close ($169.02) is well
below EMA20 with a bearish-body bar -- so ema_rebound (v1.4.0)
correctly skips it. The user still wants this kind of ticker on their
radar: pullback IN PROGRESS, watching for tomorrow's confirmation
candle.

Difference vs ema_rebound (the strict confirmation detector):

| Gate                         | ema_rebound           | ema_watch (this file) |
|------------------------------|-----------------------|-----------------------|
| Trend stack 20>50>200        | required              | required              |
| close > EMA200 (- tolerance) | required              | required              |
| Today's bar bullish or pin   | required              | NOT required          |
| Bounce magnitude (ATR)       | required (>= 0.3 ATR) | NOT required          |
| Recent EMA touch             | required (5d)         | required (3d default) |
| Close near EMA               | <= max_distance_atr   | <= max_distance_atr   |
| Tags surfaced for the user   | rebound_type, etc.    | watch_state           |

`watch_state` field:
  - "PULLING_BACK" : today's low touched/pierced an EMA, no confirmation yet
  - "TESTING"      : today's close is within touch tolerance of the EMA
                     but the bar isn't a pin/reclaim (the "in-progress" case)

Same ticker-relative threshold rule as the rest of the framework
(CLAUDE.md normalization rule). Same EMA strength ordering
(200 > 50 > 20) so the watch list ranks the same way as confirmed
rebounds.

Public API:
  detect_ema_watch(symbol: str, cfg: EMAWatchConfig) -> dict | None
  scan_universe(symbols, cfg) -> list[dict]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# --- intraday-bot bootstrap (same pattern as scanner.py / ema_rebound.py)
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

from patterns import ema_np, atr_wilder_np  # noqa: E402  (resources/patterns.py)
import bars_store  # noqa: E402  (resources/bars_store.py)


__version__ = "1.0.0"


@dataclass
class EMAWatchConfig:
    """Pre-confirmation thresholds. Looser than EMARebConfig by design --
    this surface is meant to widen the user's attention to in-progress
    setups before they confirm.
    """
    # Lookback: how recent does the EMA touch have to be?
    # 3 days = "the touch happened today or in the last 2 sessions".
    lookback_bars:         int   = 3
    # Touch tolerance: same definition as ema_rebound -- low must be at or
    # within tolerance_atr * ATR ABOVE the EMA, or BELOW (any depth).
    touch_tolerance_atr:   float = 0.3
    # Distance cap: today's close must be within max_distance_atr of the
    # EMA (above OR below). Same magnitude as ema_rebound; bigger window
    # would surface tickers that already ran far away from the EMA.
    max_distance_atr:      float = 1.0
    # Trend gates: same as ema_rebound. A pullback in a downtrend isn't
    # a watch candidate -- the EMAs are resistance there, not support.
    require_above_ema200:  bool  = True
    require_stack:         bool  = True
    # Above-EMA200 tolerance (matches ema_rebound's close_below_tolerance
    # math) so pin bars that pierced EMA200 still qualify.
    tick_size:                  float = 0.01
    close_below_tolerance_ticks: int  = 5
    close_below_tolerance_atr:  float = 0.30


# Same EMA-strength ordering as ema_rebound. Deeper-pullback EMA holding
# = stronger setup if it confirms.
_EMA_WEIGHTS = {"EMA200": 30, "EMA50": 20, "EMA20": 10}


def detect_ema_watch(symbol: str, cfg: EMAWatchConfig) -> dict | None:
    """Apply EMA-watch rules to one symbol's daily bars. Returns
    candidate dict or None.

    Candidate fields (mirrors ema_rebound shape for UI reuse):
      symbol             : uppercase ticker
      ema_anchor         : "EMA20" | "EMA50" | "EMA200" -- which EMA is the
                           watch line
      ema_value          : current EMA value
      last_close, last_open, last_high, last_low
      distance_atr       : (close - ema) / ATR  -- signed
      days_since_touch   : trading days since the touch (0 = today)
      atr14              : Wilder ATR(14)
      ema20, ema50, ema200
      watch_state        : "PULLING_BACK" | "TESTING"
      pierced_today      : bool  (today's low touched/pierced the EMA)
      pierce_depth_atr   : ATR-normalized depth of today's pierce
                           (0 if not pierced today)
      score              : composite for sort order (same direction as rebound)

    Symbol form preserved as uppercase. bars_store.load_bars expected to
    be monkey-patched by the dashboard's yf_scan endpoint when running
    against fresh yFinance bars.
    """
    bars = bars_store.load_bars(symbol, timeframe="daily")
    if len(bars) < 210:
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

    close_below_tol = max(
        cfg.close_below_tolerance_ticks * cfg.tick_size,
        cfg.close_below_tolerance_atr * atr,
    )

    # Trend gates (same as ema_rebound).
    if cfg.require_stack and not (ema20[-1] > ema50[-1] > ema200[-1]):
        return None
    if cfg.require_above_ema200 and last_close < ema200[-1] - close_below_tol:
        return None

    # Check each EMA in descending strength order. First qualifying = winner.
    best: dict | None = None
    for name, series in (("EMA200", ema200), ("EMA50", ema50), ("EMA20", ema20)):
        ema_now = float(series[-1])
        dist_atr = (last_close - ema_now) / atr
        if abs(dist_atr) > cfg.max_distance_atr:
            continue                       # close is too far from this EMA

        # Recent touch within lookback window.
        lb = min(cfg.lookback_bars, len(bars))
        tolerance = cfg.touch_tolerance_atr * atr
        touched_idx: int | None = None
        for j in range(len(bars) - 1, len(bars) - lb - 1, -1):
            if j < 0:
                break
            if lows[j] <= float(series[j]) + tolerance:
                touched_idx = j
                break
        if touched_idx is None:
            continue

        # Today's pierce flag (for the UI label).
        pierced_today = last_low <= ema_now + tolerance
        pierce_depth_atr = 0.0
        if pierced_today and last_low < ema_now:
            pierce_depth_atr = (ema_now - last_low) / atr

        # State label:
        #   TESTING       : close within tick tolerance of EMA (above OR below)
        #   PULLING_BACK  : close further away but inside max_distance_atr
        if abs(last_close - ema_now) <= close_below_tol:
            watch_state = "TESTING"
        else:
            watch_state = "PULLING_BACK"

        best = {
            "ema_anchor":       name,
            "ema_value":        ema_now,
            "distance_atr":     float(dist_atr),
            "days_since_touch": (len(bars) - 1) - touched_idx,
            "pierced_today":    bool(pierced_today),
            "pierce_depth_atr": float(pierce_depth_atr),
            "watch_state":      watch_state,
        }
        break       # descending-strength order means first hit = best

    if best is None:
        return None

    # Composite score (informational, drives sort order). Same direction
    # as ema_rebound but no reaction/prior-tests boost -- this is a
    # pre-confirmation surface, so the "would the bounce be strong"
    # signal isn't yet available.
    weight = _EMA_WEIGHTS[best["ema_anchor"]]
    proximity = max(0, 10 - int(abs(best["distance_atr"]) * 5))
    recency = max(0, 5 - best["days_since_touch"])
    # Pierce-today gets a small bump: a fresh pierce TODAY is more
    # interesting than a touch 2 days ago that hasn't bounced yet.
    pierce_bonus = 5 if best["pierced_today"] else 0
    score = weight + proximity + recency + pierce_bonus

    return {
        "symbol":           symbol.upper(),
        "ema_anchor":       best["ema_anchor"],
        "ema_value":        best["ema_value"],
        "last_close":       last_close,
        "last_open":        last_open,
        "last_high":        last_high,
        "last_low":         last_low,
        "distance_atr":     best["distance_atr"],
        "days_since_touch": best["days_since_touch"],
        "atr14":            float(atr),
        "ema20":            float(ema20[-1]),
        "ema50":            float(ema50[-1]),
        "ema200":           float(ema200[-1]),
        "watch_state":      best["watch_state"],
        "pierced_today":    best["pierced_today"],
        "pierce_depth_atr": best["pierce_depth_atr"],
        "score":            score,
    }


def scan_universe(symbols: Iterable[str], cfg: EMAWatchConfig) -> list[dict]:
    """Loop the universe, call detect_ema_watch on each. Returns
    candidates sorted by score desc, then |distance_atr| asc. Per-symbol
    exceptions go to stderr (mirrors scanner.py / ema_rebound.py).
    """
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_ema_watch(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_ema_watch failed: {exc}\n")
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c["score"], abs(c["distance_atr"])))
    return out
