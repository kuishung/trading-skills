"""Ascending-triangle detector — the Layer-3 rule scorer (SKELETON).

The ONLY file that encodes "ascending triangle". Consumes the shared L1/L2
layers (_geometry, _features) and implements the contract in
strategy/patterns/README.md.

SEED_THRESHOLDS are the v0 "beginning version" straight from the well-known
theory (DETECTOR_DESIGN.md spec table) — flat resistance, rising support,
convergence, repeated touches, contraction. They are CALIBRATION SEEDS:
_calibrate.fit_thresholds() replaces them with values fit to the user's labelled
gallery examples (the "smart from examples" step). Every threshold is
TICKER-RELATIVE (ATR/%), never an absolute dollar amount.

Build phase: D2. Body stubbed; the pipeline shape + seed thresholds are recorded
so the harvester/calibrator/validator have a stable target.
"""
from __future__ import annotations

from .. import _features as feat  # noqa: F401
from .. import _geometry as geo  # noqa: F401

# Bump on any change to the geometry or the (default) thresholds, so journal
# events can attribute detections to a detector version.
__version__ = "0.0.1"

# v0 seed thresholds (ticker-relative). Replaced by _calibrate from labels.
SEED_THRESHOLDS = {
    "res_slope_max_atrpct_per_bar": 0.05,   # resistance ~flat
    "res_r2_min": 0.60,                      # highs actually line up
    "sup_slope_min_atrpct_per_bar": 0.10,   # support rising
    "sup_r2_min": 0.60,                      # lows actually line up
    "min_touches_each": 2,                   # >= this many touches per line
    "touch_tol_atr": 0.25,                   # within 0.25*ATR counts as a touch
    "contraction_max": 0.60,                 # end range <= 60% of start range
    "apex_frac_range": (0.5, 2.0),           # apex within 0.5-2.0 x window length
    "window_bars_range": (10, 60),           # sane window on 3m/5m
    "score_min": 0.60,                       # fire at/above this score
}


def detect(bars: list[dict], thresholds: dict | None = None) -> list[dict]:
    """bars ascending [{t,o,h,l,c,v}] -> matches [{start_t,end_t,score,notes}].

    Systematic pipeline (no lookahead; thresholds default to SEED_THRESHOLDS,
    or pass a calibrated dict from _calibrate.fit_thresholds):
      1. measure   — geo.swings(bars); slide a window over the swing sequence.
      2. featurize — feat.window_features(window)                     (Layer 2)
      3. score     — compare features to `thresholds`; combine into 0-1   (L3)
      4. emit      — windows with score >= score_min; notes = raw numbers
                     (slopes, R², touches, apex, contraction) so each match is
                     explainable and the execution layer can reuse the levels.
    """
    raise NotImplementedError("D2: ascending-triangle detector")
