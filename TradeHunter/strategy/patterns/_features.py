"""Layer 2 — feature engine. A window of bars -> ~10-15 scale-invariant numbers
that both the L3 rule scorer AND the (later, optional) ML classifier consume.

Keeping features separate from both the geometry primitives (L1) and the
pattern rules (L3) is what lets us swap rule-scoring for a learned model later
without touching measurement, and lets every pattern reuse the same descriptors.

All features computed from candles available AT THE WINDOW'S LAST CLOSE only
(no lookahead). All scale-invariant (ATR/% terms, ratios, counts) so they're
comparable across tickers and price scales.

Build phase: D2 — IMPLEMENTED. Pure function over the bar window, no I/O.

Unit conventions (consistent with detect.py's SEED_THRESHOLDS so calibration
compares like-for-like):
  * slopes are in **ATR-multiples per bar** (price slope / ATR) — ticker-relative.
  * lines are fit with the window's LAST bar at x=0, so `convergence` returns
    bars-AHEAD to the apex directly (positive = future).
"""
from __future__ import annotations

from . import _geometry as geo

# The descriptor set the rule scorer / ML model read (DETECTOR_DESIGN.md).
FEATURE_KEYS = (
    "res_slope",        # resistance line slope (ATR-multiples/bar)
    "res_r2",
    "sup_slope",        # support line slope (ATR-multiples/bar)
    "sup_r2",
    "apex_bars",        # bars-ahead to convergence
    "apex_frac",        # apex_bars / window_len
    "n_touch_res",
    "n_touch_sup",
    "contraction",      # end-range / start-range, in ATR
    "window_len",       # bars in the window
    "vol_slope",        # volume trend into the apex (declining is classic)
    "atr_pct",          # ATR as fraction of price (window volatility)
    "breakout_room",    # (resistance - last close) in ATR at window end
)

# swing-extraction config used to derive the trendlines for a candidate window.
# Lenient (the window IS the candidate) — the calibration loop can override.
_SW_LEFT = 2
_SW_RIGHT = 2
_SW_PROM_ATR = 0.15


def _empty() -> dict:
    return {k: 0.0 for k in FEATURE_KEYS}


def _vol_slope(bars: list[dict], last: int) -> float:
    """Volume trend across the window as a fraction of mean volume per bar
    (negative = declining into the apex, the classic confirmation)."""
    vols = [float(b["v"]) for b in bars]
    mv = sum(vols) / len(vols) if vols else 0.0
    if mv <= 0:
        return 0.0
    lf = geo.fit_line([(i - last, v) for i, v in enumerate(vols)])
    return lf.slope / mv


def window_features(bars: list[dict]) -> dict:
    """Return a dict keyed by FEATURE_KEYS for one candidate window. Pure
    function over `bars` (the window), no I/O, no lookahead. D2.

    Degrades gracefully: if there aren't >=2 swing highs / lows to fit a line,
    the relevant slope/r2/touch features are 0 (the scorer then scores it low)
    rather than raising."""
    n = len(bars)
    feats = _empty()
    feats["window_len"] = float(n)
    if n < 4:
        return feats
    atr_val = geo.atr(bars)
    last = n - 1
    last_close = float(bars[-1]["c"])
    sw = geo.swings(bars, left=_SW_LEFT, right=_SW_RIGHT, prominence_atr=_SW_PROM_ATR)
    highs = [s for s in sw if s.kind == "high"]
    lows = [s for s in sw if s.kind == "low"]

    # Fit lines with the window's last bar at x=0 (so convergence == bars-ahead).
    # Build swing copies in the SAME relative coordinate so touches() evaluates
    # the line at the right x (geo.touches uses Swing.idx directly).
    rel_highs = [geo.Swing(s.idx - last, s.t, s.price, s.kind) for s in highs]
    rel_lows = [geo.Swing(s.idx - last, s.t, s.price, s.kind) for s in lows]
    res = geo.fit_line([(s.idx, s.price) for s in rel_highs]) if len(rel_highs) >= 2 else None
    sup = geo.fit_line([(s.idx, s.price) for s in rel_lows]) if len(rel_lows) >= 2 else None

    def nslope(line):
        return (line.slope / atr_val) if (line and atr_val > 0) else 0.0

    apex = geo.convergence(res, sup) if (res and sup) else None

    feats["res_slope"] = nslope(res)
    feats["res_r2"] = res.r2 if res else 0.0
    feats["sup_slope"] = nslope(sup)
    feats["sup_r2"] = sup.r2 if sup else 0.0
    feats["apex_bars"] = float(apex) if apex is not None else 0.0
    feats["apex_frac"] = (apex / n) if (apex is not None and n) else 0.0
    feats["n_touch_res"] = float(geo.touches(res, rel_highs, atr_val)) if res else 0.0
    feats["n_touch_sup"] = float(geo.touches(sup, rel_lows, atr_val)) if sup else 0.0
    feats["contraction"] = geo.contraction(bars, atr_val)
    feats["vol_slope"] = _vol_slope(bars, last)
    feats["atr_pct"] = (atr_val / last_close) if last_close else 0.0
    feats["breakout_room"] = ((res.at(0) - last_close) / atr_val) if (res and atr_val > 0) else 0.0
    return feats
