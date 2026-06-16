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

Build phase: D2 — IMPLEMENTED. Deterministic geometric rule-scorer over the
shared L1/L2 layers; thresholds are seeds for the D4 calibration loop.
"""
from __future__ import annotations

from .. import _features as feat
from .. import _geometry as geo  # noqa: F401  (re-exported for callers)

# Bump on any change to the geometry or the (default) thresholds, so journal
# events can attribute detections to a detector version.
__version__ = "0.1.0"

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


# --------------------------------------------------------------------------- #
# Soft sub-scores — each maps a feature to 0..1 (1 = fully satisfies the rule).
# Smooth so the score degrades gracefully instead of a hard pass/fail cliff;
# the calibration step later moves the limits, not this shape.
# --------------------------------------------------------------------------- #
def _le_soft(x: float, limit: float) -> float:
    """1.0 while x <= limit, ramping to 0 at 2*limit."""
    if limit <= 0:
        return 1.0 if x <= 0 else 0.0
    return max(0.0, 1.0 - max(0.0, (x - limit) / limit))


def _ge_soft(x: float, limit: float) -> float:
    """1.0 once x >= limit, ramping down to 0 at x = 0."""
    if limit <= 0:
        return 1.0
    if x >= limit:
        return 1.0
    return max(0.0, 1.0 - (limit - x) / limit)


def _band(x: float, lo: float, hi: float) -> float:
    """1.0 inside [lo, hi]; ramps to 0 over one band-width outside."""
    if lo <= x <= hi:
        return 1.0
    width = max(1e-9, hi - lo)
    if x < lo:
        return max(0.0, 1.0 - (lo - x) / (lo if lo > 0 else width))
    return max(0.0, 1.0 - (x - hi) / width)


def _score(f: dict, thr: dict) -> float:
    """Geometric mean of the per-rule sub-scores → 0..1. Hard gates (window out
    of range, or no fitted lines) return 0 so impossible shapes can't sneak in."""
    lo, hi = thr["window_bars_range"]
    if not (lo <= f["window_len"] <= hi):
        return 0.0
    if f["res_r2"] <= 0.0 or f["sup_r2"] <= 0.0:   # need both trendlines
        return 0.0
    af_lo, af_hi = thr["apex_frac_range"]
    subs = [
        _le_soft(abs(f["res_slope"]), thr["res_slope_max_atrpct_per_bar"]),  # flat ceiling
        _ge_soft(f["res_r2"], thr["res_r2_min"]),                            # highs line up
        _ge_soft(f["sup_slope"], thr["sup_slope_min_atrpct_per_bar"]),       # support rising
        _ge_soft(f["sup_r2"], thr["sup_r2_min"]),                            # lows line up
        _ge_soft(f["n_touch_res"], thr["min_touches_each"]),
        _ge_soft(f["n_touch_sup"], thr["min_touches_each"]),
        _le_soft(f["contraction"], thr["contraction_max"]),                  # squeeze
        _band(f["apex_frac"], af_lo, af_hi),                                 # converging ahead
    ]
    prod = 1.0
    for s in subs:
        prod *= max(1e-6, s)
    return prod ** (1.0 / len(subs))


def _notes(f: dict) -> dict:
    """Explainable raw numbers attached to a match (and reusable by execution)."""
    return {
        "res_slope": round(f["res_slope"], 4), "res_r2": round(f["res_r2"], 3),
        "sup_slope": round(f["sup_slope"], 4), "sup_r2": round(f["sup_r2"], 3),
        "n_touch_res": int(f["n_touch_res"]), "n_touch_sup": int(f["n_touch_sup"]),
        "apex_bars": round(f["apex_bars"], 1), "contraction": round(f["contraction"], 3),
        "window_len": int(f["window_len"]),
    }


def _window_sizes(thr: dict) -> list[int]:
    lo, hi = thr["window_bars_range"]
    lo, hi = int(lo), int(hi)
    if hi <= lo:
        return [lo]
    # a handful of sizes spanning the allowed band
    steps = 4
    return sorted({lo + round(i * (hi - lo) / steps) for i in range(steps + 1)})


def detect(bars: list[dict], thresholds: dict | None = None) -> list[dict]:
    """bars ascending [{t,o,h,l,c,v}] -> matches [{start_t,end_t,score,notes}].

    Systematic pipeline (no lookahead; thresholds default to SEED_THRESHOLDS,
    or pass a calibrated dict from _calibrate.fit_thresholds):
      1. measure   — slide windows (sizes within window_bars_range) over the bars.
      2. featurize — feat.window_features(window)                     (Layer 2)
      3. score     — compare features to `thresholds`; combine into 0-1   (L3)
      4. emit      — windows with score >= score_min; notes = raw numbers
                     (slopes, R², touches, apex, contraction) so each match is
                     explainable and the execution layer can reuse the levels.
    Overlapping windows are de-duplicated (non-max suppression) so one triangle
    yields one match, not a cluster.
    """
    thr = {**SEED_THRESHOLDS, **(thresholds or {})}
    n = len(bars)
    score_min = thr["score_min"]
    raw: list[dict] = []

    for w in _window_sizes(thr):
        if w > n:
            continue
        step = max(1, w // 5)
        for end in range(w, n + 1, step):
            window = bars[end - w:end]
            f = feat.window_features(window)
            s = _score(f, thr)
            if s >= score_min:
                raw.append({"start_idx": end - w, "end_idx": end - 1, "score": s,
                            "start_t": window[0]["t"], "end_t": window[-1]["t"],
                            "notes": _notes(f)})

    # non-max suppression: keep highest-scoring, drop windows overlapping it >50%
    raw.sort(key=lambda m: m["score"], reverse=True)
    kept: list[dict] = []
    for m in raw:
        a0, a1 = m["start_idx"], m["end_idx"]
        clash = False
        for k in kept:
            b0, b1 = k["start_idx"], k["end_idx"]
            inter = max(0, min(a1, b1) - max(a0, b0) + 1)
            shorter = min(a1 - a0 + 1, b1 - b0 + 1)
            if shorter > 0 and inter / shorter > 0.5:
                clash = True
                break
        if not clash:
            kept.append(m)

    kept.sort(key=lambda m: m["start_idx"])
    return [{"start_t": m["start_t"], "end_t": m["end_t"],
             "score": round(m["score"], 4), "notes": m["notes"]} for m in kept]
