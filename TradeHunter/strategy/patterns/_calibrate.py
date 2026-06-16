"""D4 — threshold fitter. Turns the user's labelled candidates (gallery
positives + rejected near-misses) into tuned detector thresholds.

This is the "smart" step: thresholds are DERIVED FROM the distribution of what
the user confirmed vs rejected — not hand-guessed. It stays low-dimensional
(~6-8 thresholds), which is exactly why geometry-first works at a few-hundred-
label budget where an ML model would starve. The output replaces a detector's
SEED_THRESHOLDS in place (portable dict), so calibration never rewrites logic.

HOW THE GALLERY IMPROVES THE DETECTOR (full rationale in DETECTOR_DESIGN.md →
"How the gallery improves the detector"). The gallery = two labelled clouds in
feature space (confirmed positives + rejected near-misses). The mechanism:

  1. Per-threshold separation — for each feature move the cutoff to the value
     that best splits positives from negatives (e.g. res_r2 0.60 -> 0.71 once
     positives sit above 0.70 and rejects below 0.78).        -> fit_thresholds
  2. Score re-weighting — rank features by how well each ALONE separates the
     labels (per-feature AUC / info-gain); up-weight separators, down-weight
     overlappers.                                  -> feature_separation (+ fit)
  3. Fire-cutoff calibration — score every labelled window, pick `score_min`
     for a chosen precision/recall point, not a guess.       -> fit_score_cutoff
  4. Hard-negative mining — surface rejected windows that still score HIGH; their
     shared feature is a MISSING RULE (e.g. rising wedges pass because resistance
     flatness has no upper bound). Flag the cluster; a human adds the new check.
                                                            -> mine_false_positives
  5. Regression gate (_validate.run_calibration_suite) keeps every confirmed case
     passing, so tuning is monotonic, not whack-a-mole.
  6. Active learning (_harvester.active_learning_rank) picks the next windows to
     label (nearest the cutoff) so a few hundred labels go far.

BRIGHT LINE: steps 1-3 (numbers / weights / cutoff) are AUTOMATIC here; step 4's
new *rules* are human-added — calibration only surfaces the need. When tuned
thresholds plateau, the same labelled feature-vectors train the optional ML model
(D7); the gallery IS its training set, so nothing labelled is wasted.

Build phase: D4 — IMPLEMENTED. Pure functions (no numpy/sklearn) over labelled
feature dicts. Output dicts are MERGE-compatible with a detector's
SEED_THRESHOLDS (detect.py does {**SEED, **calibrated}), so they only carry the
keys they actually fit.
"""
from __future__ import annotations

# Scalar thresholds we fit, as (threshold_key, feature, direction).
# direction 'min' = positives lie ABOVE the cutoff; 'max' = positives BELOW.
_SCALAR = [
    ("res_slope_max_atrpct_per_bar", "res_slope_abs", "max"),
    ("res_r2_min", "res_r2", "min"),
    ("sup_slope_min_atrpct_per_bar", "sup_slope", "min"),
    ("sup_r2_min", "sup_r2", "min"),
    ("contraction_max", "contraction", "max"),
]
# features the score weights cover (must match detect._score's sub-scores)
_WEIGHT_FEATURES = ("res_slope_abs", "res_r2", "sup_slope", "sup_r2",
                    "contraction", "apex_frac", "n_touch_res", "n_touch_sup")


def _fval(f: dict, key: str) -> float:
    if key == "res_slope_abs":
        return abs(f.get("res_slope", 0.0))
    return float(f.get(key, 0.0))


def _split(labeled: list[dict]) -> tuple[list[dict], list[dict]]:
    pos = [r["features"] for r in labeled if r.get("label") == 1]
    neg = [r["features"] for r in labeled if r.get("label") == 0]
    return pos, neg


def _best_cutoff(pos: list[float], neg: list[float], direction: str) -> float | None:
    """Cutoff (a midpoint between observed values) maximising balanced accuracy
    for the rule positives-{above|below}-cutoff. None if either class empty."""
    if not pos or not neg:
        return None
    vals = sorted(set(pos + neg))
    cands = [(vals[i] + vals[i + 1]) / 2 for i in range(len(vals) - 1)] or [vals[0]]
    npos, nneg = len(pos), len(neg)
    best, best_acc = cands[0], -1.0
    for c in cands:
        if direction == "min":      # positive predicted when x >= c
            tp = sum(1 for x in pos if x >= c); tn = sum(1 for x in neg if x < c)
        else:                       # 'max': positive when x <= c
            tp = sum(1 for x in pos if x <= c); tn = sum(1 for x in neg if x > c)
        acc = 0.5 * (tp / npos) + 0.5 * (tn / nneg)
        if acc > best_acc:
            best_acc, best = acc, c
    return best


def _auc(pos: list[float], neg: list[float]) -> float:
    """Separation = max(AUC, 1-AUC) so direction doesn't matter; 0.5 = useless,
    1.0 = perfectly separating. Rank-based (Mann-Whitney)."""
    if not pos or not neg:
        return 0.5
    wins = ties = 0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                ties += 1
    auc = (wins + 0.5 * ties) / (len(pos) * len(neg))
    return max(auc, 1.0 - auc)


def feature_separation(labeled: list[dict]) -> dict:
    """(Mechanism 2) Per-feature discriminative power in [0.5, 1.0] — how well
    each feature ALONE separates positives from negatives (rank AUC). Drives the
    score weights and tells you which geometry actually matters. D4."""
    pos, neg = _split(labeled)
    return {feat: round(_auc([_fval(f, feat) for f in pos],
                             [_fval(f, feat) for f in neg]), 4)
            for feat in _WEIGHT_FEATURES}


def _weights_from_separation(sep: dict) -> dict:
    """Map separation (0.5..1.0) to a weight emphasising real separators. A
    feature that doesn't separate (≈0.5) gets ~0 weight; a perfect one ~1."""
    return {feat: round(max(0.0, (s - 0.5) * 2.0), 4) for feat, s in sep.items()}


def fit_thresholds(labeled: list[dict], *,
                   holdout_tickers: list[str] | None = None,
                   holdout_time: str | None = None) -> dict:
    """(Mechanism 1+2) `labeled` = [{features, label (1/0), symbol, t}]. Choose
    the per-feature thresholds + score weights that best separate positives from
    negatives. HOLD OUT some tickers and a time period (walk-forward) so the
    result generalizes rather than memorising. Returns a dict MERGE-compatible
    with a detector's SEED_THRESHOLDS (only the keys it fit). D4."""
    hold = set(holdout_tickers or [])
    insample = [r for r in labeled
                if r.get("symbol") not in hold
                and not (holdout_time and str(r.get("t", "")) >= holdout_time)]
    pos, neg = _split(insample)
    out: dict = {}
    for key, feat, direction in _SCALAR:
        cut = _best_cutoff([_fval(f, feat) for f in pos],
                           [_fval(f, feat) for f in neg], direction)
        if cut is not None:
            out[key] = round(cut, 4)
    sep = feature_separation(insample)
    out["weights"] = _weights_from_separation(sep)
    out["_separation"] = sep
    out["_n_pos"], out["_n_neg"] = len(pos), len(neg)
    return out


def _scorer(detector):
    score_fn = getattr(detector, "_score", None)
    thr = getattr(detector, "SEED_THRESHOLDS", None)
    if callable(score_fn) and isinstance(thr, dict):
        return lambda f: score_fn(f, thr)
    raise ValueError("detector must expose _score(features, thresholds) + SEED_THRESHOLDS")


def fit_score_cutoff(labeled: list[dict], detector, *,
                     target: str = "balanced") -> float:
    """(Mechanism 3) Score every labelled window with `detector`, then pick the
    `score_min` that hits the chosen precision/recall trade-off (`target`:
    'balanced' | 'high_precision' | 'high_recall'). Returns the cutoff. D4."""
    score_fn = _scorer(detector)
    scored = [(score_fn(r["features"]), r.get("label")) for r in labeled]
    pos = [s for s, lab in scored if lab == 1]
    neg = [s for s, lab in scored if lab == 0]
    if not pos or not neg:
        return float(getattr(detector, "SEED_THRESHOLDS", {}).get("score_min", 0.6))
    cands = sorted({round(s, 4) for s, _ in scored})
    npos, nneg = len(pos), len(neg)

    def metrics(c):
        tp = sum(1 for s in pos if s >= c); fp = sum(1 for s in neg if s >= c)
        tn = nneg - fp
        prec = tp / (tp + fp) if (tp + fp) else 1.0
        rec = tp / npos
        bal = 0.5 * rec + 0.5 * (tn / nneg)
        return prec, rec, bal

    if target == "high_precision":
        ok = [c for c in cands if metrics(c)[0] >= 0.9]
        return float(min(ok)) if ok else float(max(cands))
    if target == "high_recall":
        ok = [c for c in cands if metrics(c)[1] >= 0.9]
        return float(max(ok)) if ok else float(min(cands))
    return float(max(cands, key=lambda c: metrics(c)[2]))   # balanced


def mine_false_positives(labeled: list[dict], detector) -> list[dict]:
    """(Mechanism 4) Return rejected (label=0) windows that still score HIGH,
    each annotated with the `culprit` feature that most let it through — a
    cluster of the same culprit is a candidate MISSING RULE for a human to add
    (e.g. a resistance-flatness upper bound to kill rising wedges). Calibration
    surfaces; a human decides. D4."""
    score_fn = _scorer(detector)
    cutoff = float(getattr(detector, "SEED_THRESHOLDS", {}).get("score_min", 0.6))
    pos, _ = _split(labeled)
    # per-feature positive range, to judge how "positive-like" a negative looks
    pranges = {}
    for feat in _WEIGHT_FEATURES:
        vals = [_fval(f, feat) for f in pos]
        pranges[feat] = (min(vals), max(vals)) if vals else (0.0, 0.0)

    out = []
    for r in labeled:
        if r.get("label") != 0:
            continue
        f = r["features"]
        s = score_fn(f)
        if s < cutoff:
            continue
        # culprit = the feature whose value sits most centrally in the positive
        # range (i.e. looks most like a real triangle on that axis)
        def centrality(feat):
            lo, hi = pranges[feat]
            if hi <= lo:
                return 0.0
            x = _fval(f, feat)
            return max(0.0, 1.0 - abs(x - (lo + hi) / 2) / ((hi - lo) / 2 + 1e-9))
        culprit = max(_WEIGHT_FEATURES, key=centrality)
        out.append({"symbol": r.get("symbol"), "t": r.get("t"),
                    "score": round(s, 4), "culprit": culprit, "features": f})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out
