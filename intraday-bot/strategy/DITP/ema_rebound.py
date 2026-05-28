"""EMA-rebound detector -- daily support bounce on EMA20 / EMA50 / EMA200.

User request 2026-05-27: "Add a setup that will find rebound on EMA20
or EMA50 or EMA200" -- applied as a filter to the dashboard's Finviz
ticker list (same universe as DITP P2; different signal logic).

Pattern: ticker in confirmed uptrend pulled back to one of the
standard EMAs (20, 50, or 200), touched/pierced it, and today
printed a bullish rebound candle that closed back above the EMA.
The candidate carries WHICH EMA acted as support so the user can
size conviction (EMA200 bounce > EMA50 > EMA20 in our scoring).

Per CLAUDE.md normalization rule: all thresholds are ticker-relative
(ATR multiples + scale-free ratios). Same code applies to NVDA's
$4 ATR and HIMS's $1.50 ATR without hand-tuning.

Detection flow (per symbol):
  1. Load daily bars. Need >=210 for EMA200 stability + lookback
     headroom.
  2. Trend gate: EMA20 > EMA50 > EMA200 AND close > EMA200
     (configurable). Without uptrend the EMAs are resistance, not
     support; the setup is meaningless.
  3. Today's candle gate: bullish (close > open) AND close in upper
     half of bar range (rebound character, not weak retest).
  4. For each EMA in descending strength order (200 -> 50 -> 20):
       * Close must be ABOVE the EMA (rebound confirmed).
       * Close not too far above (within max_distance_atr * ATR
         from the EMA) -- we want the rebound RECENT, not a stock
         that already ran 5 ATR away from EMA support.
       * Look back lookback_bars days for a TOUCH: a bar where the
         low was at or just below the EMA value at that time
         (within touch_tolerance_atr * ATR).
       * First EMA that satisfies all three is the anchor. EMA200
         beats EMA50 beats EMA20 because the deeper pullback that
         held = stronger structural support.
  5. Score (informational, drives sort order): EMA weight + proximity
     + recency.

Public API:
  detect_ema_rebound(symbol: str, cfg: EMARebConfig) -> dict | None
  scan_universe(symbols: Iterable[str], cfg: EMARebConfig) -> list[dict]

Returns dicts (not dataclass) because callers always serialize for
the dashboard. Field list documented in detect_ema_rebound's
docstring below.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# --- intraday-bot bootstrap (same pattern as scanner.py) -----------
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


__version__ = "1.3.0"


@dataclass
class EMARebConfig:
    """All thresholds ticker-relative per CLAUDE.md normalization rule.

    Defaults are conservative -- expect to retune after seeing real
    matches against a tradeable universe. Loosen any of these to
    surface more candidates; tighten to filter to higher conviction.
    """
    lookback_bars:         int   = 5       # how far back (in trading days) to find an EMA touch
    touch_tolerance_atr:   float = 0.3     # touch counts if low <= EMA + tolerance*ATR (handles small overshoots)
    max_distance_atr:      float = 1.0     # max ATR-distance close can be above EMA after the bounce (recent rebound only)
    require_bullish_close: bool  = True    # today's close > today's open
    min_close_position:    float = 0.5     # today's close must sit in top N of the bar's range (0.5 = upper half)
    require_above_ema200:  bool  = True    # gate on the major-trend EMA (relaxed by close_below_tolerance for pin bars)
    require_stack:         bool  = True    # require EMA20 > EMA50 > EMA200 (clean bull stack)
    # Reaction-magnitude gate added 2026-05-27 (user rule):
    # "sometimes price action will trade through the EMAs and form a
    # rebound with pin bar or a bullish hammer... we have to find the
    # confirmation candle i.e. the candle formation of a bullish
    # candle." Touch + bullish-close alone isn't enough -- the bounce
    # from the touch's low (which may have pierced through the EMA)
    # to today's close must be a real reaction. Same math as P1/P3.
    min_bounce_atr:        float = 0.3
    # Close-below-EMA tolerance + pin-bar anatomy (added 2026-05-27).
    # User rule: "bounce off EMA does not mean that it has to close
    # above the EMAs. If the last candle form bullish pin bar below
    # the EMA 2-5 ticks below, it can still be qualified as rebound
    # by way of trade through EMAs." So we allow close up to N ticks
    # BELOW the EMA, BUT only if today's bar is a clear bullish pin
    # bar (small body + long lower wick). Above-EMA case keeps v1.1.0
    # behaviour (no pin-bar anatomy required).
    tick_size:                  float = 0.01
    close_below_tolerance_ticks: int  = 5     # close <= EMA - N*tick_size still qualifies (if pin bar)
    pin_max_body_ratio:         float = 0.40  # pin bar: body / range <= this
    pin_min_lower_tail_ratio:   float = 0.50  # pin bar: lower wick / range >= this
    # Prior-tests conviction signal (added 2026-05-27, v1.3.0).
    # User rule: "the chart will give you more conviction if it
    # previously tested the same EMAs by traded through" -- e.g.,
    # NVDA bullish hammer 27.5.2026 with prior trade-throughs on
    # 4.5.2026 and 5.5.2026 = stronger setup. Count bars in the
    # prior_tests_lookback window that ALSO traded through the same
    # EMA with similar rebound anatomy (low pierced, bullish close
    # back near/above EMA, close in upper half).
    prior_tests_lookback:       int   = 60    # ~3 trading months
    prior_tests_score_per:      int   = 5     # +N points per prior test
    prior_tests_score_cap:      int   = 15    # max boost from prior tests


# Strength ranking: deeper-pullback EMA = stronger support if it held.
_EMA_WEIGHTS = {"EMA200": 30, "EMA50": 20, "EMA20": 10}


def _is_prior_trade_through(o: float, h: float, l: float, c: float,
                            ema_value: float, close_below_tol: float) -> bool:
    """Was bar (o, h, l, c) a "trade-through and rebound" event at this
    EMA value? Used to count prior similar tests for the conviction
    score boost (user rule 2026-05-27).

    Criteria (same anatomy the live detector requires for today's bar):
      - Low pierced the EMA (low < ema_value)
      - Close came back to within tick tolerance (close >= ema - tol)
      - Bullish close (close > open)
      - Close in upper half of bar range

    Returns True only when ALL four conditions hold.
    """
    rng = h - l
    if rng <= 0:
        return False
    if l >= ema_value:
        return False                                    # didn't pierce
    if c < ema_value - close_below_tol:
        return False                                    # closed too far below
    if c <= o:
        return False                                    # not bullish
    if (c - l) / rng < 0.5:
        return False                                    # not in upper half
    return True


def detect_ema_rebound(symbol: str, cfg: EMARebConfig) -> dict | None:
    """Apply EMA-rebound rules to one symbol's daily bars. Returns
    candidate dict or None.

    Candidate fields:
      symbol           : uppercase ticker
      ema_anchor       : "EMA20" | "EMA50" | "EMA200" -- which EMA held
      ema_value        : float, current EMA value at the rebound
      last_close       : today's close
      last_open        : today's open
      last_high        : today's high
      last_low         : today's low
      distance_atr     : (last_close - ema_value) / ATR14 -- proximity
      days_since_touch : trading days since the low <= EMA event (0 = today)
      atr14            : Wilder ATR(14), current bar
      close_position_in_range : (close - low) / (high - low) [0..1]
      ema20, ema50, ema200    : current values of all three EMAs
      score            : composite (ema_weight + proximity + recency)

    Symbol form is preserved as uppercase. bars_store.load_bars is
    expected to be monkey-patched by the dashboard's yf_scan endpoint
    when running against fresh yFinance bars (no parquet read).
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

    # Pre-compute tick tolerance + bar anatomy (used for the
    # close-below-EMA pin-bar case introduced in v1.2.0).
    close_below_tol = cfg.close_below_tolerance_ticks * cfg.tick_size

    # Trend gate.
    if cfg.require_stack and not (ema20[-1] > ema50[-1] > ema200[-1]):
        return None
    # Relax the close > EMA200 gate by the same tick tolerance --
    # pin bar that traded through EMA200 still qualifies.
    if cfg.require_above_ema200 and last_close < ema200[-1] - close_below_tol:
        return None

    # Bullish-candle gate.
    if cfg.require_bullish_close and last_close <= last_open:
        return None
    rng = last_high - last_low
    if rng <= 0:
        return None
    close_position = (last_close - last_low) / rng
    if close_position < cfg.min_close_position:
        return None

    # Pin-bar anatomy. Used as an additional gate when close is BELOW
    # the EMA -- a bullish pin bar that pierced and almost-closed-back
    # qualifies as rebound; a plain bullish bar that closed below
    # does NOT (it's a failed reclaim).
    body = abs(last_close - last_open)
    body_ratio = body / rng
    lower_tail = min(last_open, last_close) - last_low
    lower_tail_ratio = lower_tail / rng
    is_pin_bar = (body_ratio <= cfg.pin_max_body_ratio
                  and lower_tail_ratio >= cfg.pin_min_lower_tail_ratio)

    # Check each EMA in descending strength order. First qualifying = winner.
    best: dict | None = None
    for name, series in (("EMA200", ema200), ("EMA50", ema50), ("EMA20", ema20)):
        ema_now = float(series[-1])
        # Two acceptable states:
        #   1. close >= ema_now           -- standard reclaim (v1.1.0 behaviour)
        #   2. ema_now - tol <= close < ema_now AND is_pin_bar
        #                                 -- "trade through" pin bar (v1.2.0)
        if last_close < ema_now - close_below_tol:
            continue                       # close too far below EMA
        if last_close < ema_now and not is_pin_bar:
            continue                       # below EMA but not a pin bar -- failed reclaim
        dist_atr = (last_close - ema_now) / atr
        # dist_atr is positive when close above EMA, negative when below.
        # Cap the upper bound (max_distance_atr); the lower bound is
        # already enforced by the tick-tolerance check above.
        if dist_atr > cfg.max_distance_atr:
            continue
        # Recent touch: scan back, look for low <= EMA + tolerance*ATR.
        # The tolerance allows for both small overshoots ABOVE the EMA
        # (the lower wick just kissed it) AND deep pierces BELOW the
        # EMA (a pin bar / hammer that traded through it before closing
        # back above). The next gate -- bounce magnitude -- ensures we
        # caught a real reaction, not just a touch.
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
        # Reaction-magnitude gate. User rule 2026-05-27: the
        # confirmation candle should be a real bullish reaction off
        # the EMA, not just "price drifted slightly above after a
        # touch". Same math as P1/P3 -- from the deepest probe of
        # the EMA (which may be well below the level on a pin bar)
        # up to today's close.
        bounce_low = float(np.min(lows[touched_idx:]))
        bounce_magnitude_atr = (last_close - bounce_low) / atr
        if bounce_magnitude_atr < cfg.min_bounce_atr:
            continue
        # Prior trade-through count at THIS EMA (v1.3.0). Scan
        # backward from the touch bar over prior_tests_lookback bars,
        # counting each bar that ALSO traded through this EMA with a
        # bullish rebound. NVDA case 2026-05-27: today's hammer is the
        # current setup; prior tests on 4.5.2026 and 5.5.2026 add
        # conviction (=> +score boost). Each prior bar counted
        # independently, so consecutive same-week tests both count.
        prior_count = 0
        prior_test_indices: list[int] = []
        start_j = max(0, touched_idx - cfg.prior_tests_lookback)
        for j in range(start_j, touched_idx):
            if _is_prior_trade_through(
                float(opens[j]), float(highs[j]), float(lows[j]),
                float(closes[j]), float(series[j]), close_below_tol):
                prior_count += 1
                prior_test_indices.append(j)
        best = {
            "ema_anchor":           name,
            "ema_value":            ema_now,
            "distance_atr":         float(dist_atr),
            "days_since_touch":     (len(bars) - 1) - touched_idx,
            "bounce_magnitude_atr": float(bounce_magnitude_atr),
            # rebound_type: which path fired. "reclaim" = close above EMA;
            # "pin_through" = close below EMA but within tick tolerance,
            # bar is a clear bullish pin bar (v1.2.0 trade-through case).
            "rebound_type":         "pin_through" if last_close < ema_now else "reclaim",
            "is_pin_bar":           bool(is_pin_bar),
            "prior_tests_count":    prior_count,
            "prior_tests_indices":  prior_test_indices,
        }
        break       # descending-strength order means first hit = best

    if best is None:
        return None

    # Composite score: weight (EMA200 > 50 > 20) + proximity bonus +
    # recency bonus + reaction-strength bonus + prior-tests bonus.
    weight = _EMA_WEIGHTS[best["ema_anchor"]]
    proximity = max(0, 10 - int(best["distance_atr"] * 5))
    recency = max(0, 5 - best["days_since_touch"])
    reaction = min(10, int(best["bounce_magnitude_atr"] * 10))
    # User rule 2026-05-27: prior tests of the same EMA via
    # trade-through = stronger conviction. Each prior test adds
    # `prior_tests_score_per` points, capped at `prior_tests_score_cap`.
    prior_tests_bonus = min(
        cfg.prior_tests_score_cap,
        best["prior_tests_count"] * cfg.prior_tests_score_per,
    )
    score = weight + proximity + recency + reaction + prior_tests_bonus

    return {
        "symbol":                  symbol.upper(),
        "ema_anchor":              best["ema_anchor"],
        "ema_value":               best["ema_value"],
        "last_close":              last_close,
        "last_open":               last_open,
        "last_high":               last_high,
        "last_low":                last_low,
        "distance_atr":            best["distance_atr"],
        "bounce_magnitude_atr":    best["bounce_magnitude_atr"],
        "rebound_type":            best["rebound_type"],
        "is_pin_bar":              best["is_pin_bar"],
        "prior_tests_count":       best["prior_tests_count"],
        "prior_tests_bonus":       int(prior_tests_bonus),
        "days_since_touch":        best["days_since_touch"],
        "atr14":                   float(atr),
        "close_position_in_range": float(close_position),
        "ema20":                   float(ema20[-1]),
        "ema50":                   float(ema50[-1]),
        "ema200":                  float(ema200[-1]),
        "score":                   score,
    }


def scan_universe(symbols: Iterable[str], cfg: EMARebConfig) -> list[dict]:
    """Loop the universe, call detect_ema_rebound on each. Returns
    candidates sorted by score desc, then distance_atr asc. Per-symbol
    exceptions go to stderr (mirrors strategy/DITP/scanner.py's
    scan_universe behavior) so one broken symbol doesn't kill the
    whole scan.
    """
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_ema_rebound(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_ema_rebound failed: {exc}\n")
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c["score"], c["distance_atr"]))
    return out
