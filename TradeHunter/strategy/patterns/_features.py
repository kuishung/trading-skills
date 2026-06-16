"""Layer 2 — feature engine. A window of bars -> ~10-15 scale-invariant numbers
that both the L3 rule scorer AND the (later, optional) ML classifier consume.

Keeping features separate from both the geometry primitives (L1) and the
pattern rules (L3) is what lets us swap rule-scoring for a learned model later
without touching measurement, and lets every pattern reuse the same descriptors.

All features computed from candles available AT THE WINDOW'S LAST CLOSE only
(no lookahead). All scale-invariant (ATR/% terms, ratios, counts) so they're
comparable across tickers and price scales.

Build phase: D1/D2. Stub.
"""
from __future__ import annotations

from . import _geometry as geo  # noqa: F401  (used once implemented)

# The descriptor set the rule scorer / ML model read (DETECTOR_DESIGN.md).
FEATURE_KEYS = (
    "res_slope",        # resistance line slope (ATR%/bar)
    "res_r2",
    "sup_slope",        # support line slope (ATR%/bar)
    "sup_r2",
    "apex_bars",        # bars-ahead to convergence
    "apex_frac",        # apex_bars / window_len
    "n_touch_res",
    "n_touch_sup",
    "contraction",      # end-range / start-range, in ATR
    "window_len",       # bars in the window
    "vol_slope",        # volume trend into the apex (declining is classic)
    "atr_pct",          # ATR as % of price (window volatility)
    "breakout_room",    # price vs resistance at window end
)


def window_features(bars: list[dict]) -> dict:
    """Return a dict keyed by FEATURE_KEYS for one candidate window. Pure
    function over `bars` (the window), no I/O, no lookahead. D1/D2."""
    raise NotImplementedError("D1/D2: feature engine")
