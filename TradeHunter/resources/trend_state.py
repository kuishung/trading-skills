"""Trend-state classifier using EMA 20 / 50 / 200 on daily bars.

Source: strategies-reference/TREND_EMA.md
(Trend Identification Strategy — 1 Year · 1 Day Candles)

Four mutually exclusive states, classified by EMA stack order and
spread dynamics (decision tree follows TREND_EMA.md exactly):

    UPTREND       EMA20 > EMA50 > EMA200  (bullish stack)
    DOWNTREND     EMA20 < EMA50 < EMA200  (bearish stack)
    CONSOLIDATION Not stacked, AND all EMAs converging (spread contracting)
    SIDEWAYS      Not stacked, AND not clearly converging

Design contract (consistent with resources/patterns.py):
    - NO I/O inside the pure functions. Accept pre-computed numpy arrays.
    - Deterministic given identical inputs.
    - Convenience wrapper `classify_symbol_trend()` handles bar loading.

Typical call from strategy / scanner code
------------------------------------------
    from resources.trend_state import classify_symbol_trend, UPTREND, DOWNTREND

    state = classify_symbol_trend("NVDA")    # loads daily parquet, returns str
    if state == UPTREND:
        ...   # only take long setups today

Or from already-loaded numpy closes:
    from resources.trend_state import classify_trend_ema
    state = classify_trend_ema(closes_array)

Changelog
---------
2026-05-29  v1.0.0  Initial implementation.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

# ── String constants so callers can compare without magic strings ──────────
UPTREND       = "uptrend"
DOWNTREND     = "downtrend"
CONSOLIDATION = "consolidation"
SIDEWAYS      = "sideways"
UNKNOWN       = "unknown"   # returned when data is insufficient

ALL_STATES = (UPTREND, DOWNTREND, CONSOLIDATION, SIDEWAYS, UNKNOWN)


# ── Lazy numpy import (mirrors patterns.py style) ─────────────────────────
def _np():
    import numpy as _numpy
    return _numpy


# ──────────────────────────────────────────────────────────────────────────
# Core classifier (pure, no I/O)
# ──────────────────────────────────────────────────────────────────────────

def classify_trend_ema(
    closes,
    highs=None,
    lows=None,
    *,
    convergence_window: int = 20,
    convergence_contraction: float = 0.20,
    range_contraction: float = 0.20,
) -> str:
    """Classify daily-chart trend state from OHLC bar data.

    Parameters
    ----------
    closes : array-like of float
        Daily closing prices, oldest-first. At least 200 values required.
    highs : array-like of float, optional
        Daily high prices.  When provided (alongside lows), enables the
        price-range-shrinking check for consolidation detection.
    lows : array-like of float, optional
        Daily low prices.  Must be provided together with highs.
    convergence_window : int, default 20
        Bars back to compare EMA spread for convergence detection.
        20 bars = ~1 calendar month.
    convergence_contraction : float, default 0.20
        Fractional EMA spread contraction required.  0.20 = spread must
        have shrunk by >= 20% over `convergence_window` bars.
    range_contraction : float, default 0.20
        Fractional price-range contraction required.  0.20 = the recent
        20-bar high-low span must be <= 80% of the prior 20-bar span.
        Only applied when highs + lows are supplied.

    Returns
    -------
    str  —  one of: 'uptrend', 'downtrend', 'consolidation', 'sideways',
            'unknown'.

    Decision tree (per strategies-reference/TREND_EMA.md)
    ------------------------------------------------------
    1. EMA20 > EMA50 > EMA200                              → UPTREND
    2. EMA20 < EMA50 < EMA200                              → DOWNTREND
    3. Not stacked + EMA spreads contracting
                   + price range shrinking (coil/wedge)    → CONSOLIDATION
    4. Otherwise (flat, tangled, stable range)             → SIDEWAYS

    Key distinction — Consolidation vs Sideways:
      Sideways      : price bounces between STABLE support and resistance;
                      range width is constant; EMAs flat and tangled.
      Consolidation : price ACTIVELY compressing into a tighter range
                      (triangle / wedge / flag); both EMA spreads AND
                      the high-low range are contracting simultaneously.
                      Volume dries up. Energy builds for a breakout.
    """
    np = _np()
    c = np.asarray(closes, dtype=float)

    if len(c) < 200:
        return UNKNOWN

    from patterns import ema_np  # type: ignore  (resources/patterns.py)

    ema20  = ema_np(c, 20)
    ema50  = ema_np(c, 50)
    ema200 = ema_np(c, 200)

    e20, e50, e200 = ema20[-1], ema50[-1], ema200[-1]

    # ── Step 1: Bullish stack → UPTREND ────────────────────────────────────
    if e20 > e50 > e200:
        return UPTREND

    # ── Step 2: Bearish stack → DOWNTREND ──────────────────────────────────
    if e20 < e50 < e200:
        return DOWNTREND

    # ── Step 3: Not stacked — distinguish Consolidation from Sideways ──────
    #
    # CONSOLIDATION requires TWO simultaneous signals:
    #   A. EMA spreads contracting  (EMAs squeezing together)
    #   B. Price range shrinking    (high-low span getting tighter)
    #
    # SIDEWAYS is the fallback: EMAs are tangled but the price range is
    # stable — price is just oscillating without directional pressure.

    # ── Signal A: EMA spread contraction ───────────────────────────────────
    lookback = min(convergence_window, len(c) - 1)
    spread_near_now  = abs(ema20[-1]          - ema50[-1])
    spread_near_then = abs(ema20[-1-lookback] - ema50[-1-lookback])
    spread_far_now   = abs(ema50[-1]          - ema200[-1])
    spread_far_then  = abs(ema50[-1-lookback] - ema200[-1-lookback])

    ema_near_contracting = (
        spread_near_then > 1e-9 and
        (spread_near_now / spread_near_then) < (1.0 - convergence_contraction)
    )
    ema_far_contracting = (
        spread_far_then > 1e-9 and
        (spread_far_now / spread_far_then) < (1.0 - convergence_contraction)
    )
    ema_converging = ema_near_contracting and ema_far_contracting

    # ── Signal B: Price range shrinking ────────────────────────────────────
    # Compare the high-low span of the most recent `convergence_window` bars
    # against the span of the equal-length window immediately before it.
    # A shrinking span confirms the coil / triangle / wedge shape.
    if highs is not None and lows is not None:
        h = np.asarray(highs, dtype=float)
        lo = np.asarray(lows, dtype=float)
        w = convergence_window
        if len(h) >= w * 2:
            recent_span = h[-w:].max()   - lo[-w:].min()
            prior_span  = h[-2*w:-w].max() - lo[-2*w:-w].min()
            range_shrinking = (
                prior_span > 1e-9 and
                (recent_span / prior_span) < (1.0 - range_contraction)
            )
        else:
            range_shrinking = False
    else:
        # No H/L supplied: fall back to close-range proxy
        w = convergence_window
        if len(c) >= w * 2:
            recent_span = c[-w:].max()   - c[-w:].min()
            prior_span  = c[-2*w:-w].max() - c[-2*w:-w].min()
            range_shrinking = (
                prior_span > 1e-9 and
                (recent_span / prior_span) < (1.0 - range_contraction)
            )
        else:
            range_shrinking = False

    # Both signals must fire → CONSOLIDATION
    if ema_converging and range_shrinking:
        return CONSOLIDATION

    # ── Step 4: Default ────────────────────────────────────────────────────
    return SIDEWAYS


def trend_ema_detail(closes, highs=None, lows=None) -> dict:
    """Extended diagnostic output alongside the state classification.

    Returns a dict with the state string plus the raw EMA values, spread
    metrics, and price-range contraction ratio — useful for dashboard
    display and debugging.

    Keys
    ----
    state             str    one of ALL_STATES
    ema20             float  EMA 20 last value
    ema50             float  EMA 50 last value
    ema200            float  EMA 200 last value
    spread_20_50      float  abs(ema20 - ema50)
    spread_50_200     float  abs(ema50 - ema200)
    stack_bull        bool   ema20 > ema50 > ema200
    stack_bear        bool   ema20 < ema50 < ema200
    price_vs_ema20    str    'above' | 'below' | 'at'
    range_ratio       float  recent_span / prior_span  (< 1 = shrinking)
                             None when H/L not supplied
    """
    np = _np()
    c = np.asarray(closes, dtype=float)
    state = classify_trend_ema(c, highs, lows)
    if state == UNKNOWN:
        return {"state": UNKNOWN}

    from patterns import ema_np  # type: ignore
    ema20  = ema_np(c, 20)
    ema50  = ema_np(c, 50)
    ema200 = ema_np(c, 200)
    e20, e50, e200 = float(ema20[-1]), float(ema50[-1]), float(ema200[-1])
    price  = float(c[-1])

    if price > e20 * 1.001:
        pv = "above"
    elif price < e20 * 0.999:
        pv = "below"
    else:
        pv = "at"

    # Range ratio (recent 20-bar span / prior 20-bar span)
    range_ratio = None
    if highs is not None and lows is not None:
        h = np.asarray(highs, dtype=float)
        lo = np.asarray(lows, dtype=float)
        if len(h) >= 40:
            recent_span = h[-20:].max() - lo[-20:].min()
            prior_span  = h[-40:-20].max() - lo[-40:-20].min()
            if prior_span > 1e-9:
                range_ratio = round(float(recent_span / prior_span), 3)

    return {
        "state":          state,
        "ema20":          round(e20,  4),
        "ema50":          round(e50,  4),
        "ema200":         round(e200, 4),
        "spread_20_50":   round(abs(e20 - e50),   4),
        "spread_50_200":  round(abs(e50 - e200),  4),
        "stack_bull":     e20 > e50 > e200,
        "stack_bear":     e20 < e50 < e200,
        "price_vs_ema20": pv,
        "range_ratio":    range_ratio,
    }


# ──────────────────────────────────────────────────────────────────────────
# Convenience wrapper — loads from bars_store, classifies, returns state
# ──────────────────────────────────────────────────────────────────────────

def classify_symbol_trend(
    symbol: str,
    data_root: "str | Path | None" = None,
    detail: bool = False,
) -> "str | dict":
    """Load daily bars for `symbol` from bars_store and return its trend state.

    Parameters
    ----------
    symbol : str
        Ticker symbol, e.g. "NVDA".
    data_root : str or Path, optional
        Override the Resilio/config data root.  If None, reads from
        `scripts._common.get_data_root()` (honours config.json on each PC).
    detail : bool, default False
        If True, return the full detail dict from `trend_ema_detail()`
        instead of just the state string.

    Returns
    -------
    str or dict
        State string (e.g. 'uptrend') or detail dict when detail=True.
        Returns UNKNOWN if daily bars are unavailable or too short.
    """
    try:
        # Resolve data root the same way every other module does
        if data_root is None:
            try:
                import sys as _sys
                _scripts = Path(__file__).resolve().parent.parent / "scripts"
                if str(_scripts) not in _sys.path:
                    _sys.path.insert(0, str(_scripts))
                from _common import get_data_root  # type: ignore
                data_root = get_data_root()
            except Exception:
                data_root = Path(__file__).resolve().parent.parent / "data"

        import sys as _sys
        _resources = Path(__file__).resolve().parent
        if str(_resources) not in _sys.path:
            _sys.path.insert(0, str(_resources))

        from bars_store import load_bars  # type: ignore  (resources/bars_store.py)
        bars = load_bars(symbol, timeframe="daily")
        if not bars:
            return UNKNOWN if not detail else {"state": UNKNOWN}

        np = _np()
        closes = np.array([b["c"] for b in bars], dtype=float)
        highs  = np.array([b["h"] for b in bars], dtype=float)
        lows   = np.array([b["l"] for b in bars], dtype=float)
        if detail:
            return trend_ema_detail(closes, highs, lows)
        return classify_trend_ema(closes, highs, lows)

    except Exception as exc:
        import warnings
        warnings.warn(f"[trend_state] classify_symbol_trend({symbol}): {exc}")
        return UNKNOWN if not detail else {"state": UNKNOWN}


# ──────────────────────────────────────────────────────────────────────────
# Batch helper — classify a list of symbols, returns {symbol: state}
# ──────────────────────────────────────────────────────────────────────────

def classify_universe_trends(
    symbols: list[str],
    data_root: "str | Path | None" = None,
    detail: bool = False,
) -> dict:
    """Classify trend state for every symbol in `symbols`.

    Returns
    -------
    dict mapping symbol → state-string (or detail-dict when detail=True).
    Symbols that fail or have insufficient data are mapped to UNKNOWN.
    """
    return {
        sym: classify_symbol_trend(sym, data_root=data_root, detail=detail)
        for sym in symbols
    }
