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

Build phase: D4. Stubs.
"""
from __future__ import annotations


def fit_thresholds(labeled: list[dict], *,
                   holdout_tickers: list[str] | None = None,
                   holdout_time: str | None = None) -> dict:
    """(Mechanism 1+2) `labeled` = [{features, label (1 positive / 0 negative),
    symbol, t}]. Choose the per-feature thresholds + score weights that best
    separate positives from negatives. HOLD OUT some tickers and a time period
    (walk-forward) so the result generalizes across symbols and forward in time
    rather than memorising. Returns a dict shaped like a detector's
    SEED_THRESHOLDS. D4."""
    raise NotImplementedError("D4: threshold fitting")


def feature_separation(labeled: list[dict]) -> dict:
    """(Mechanism 2) Per-feature discriminative power — how well each feature
    ALONE separates positives from negatives (e.g. AUC or information gain).
    Drives the score weights and tells you which geometry actually matters. D4."""
    raise NotImplementedError("D4: feature separation")


def fit_score_cutoff(labeled: list[dict], detector, *,
                     target: str = "balanced") -> float:
    """(Mechanism 3) Score every labelled window with `detector`, then pick the
    `score_min` that hits the chosen precision/recall trade-off (`target`:
    'balanced' | 'high_precision' | 'high_recall'). Returns the cutoff. D4."""
    raise NotImplementedError("D4: score-cutoff calibration")


def mine_false_positives(labeled: list[dict], detector) -> list[dict]:
    """(Mechanism 4) Return rejected (label=0) windows that still score HIGH,
    grouped by the feature that let them through — each cluster is a candidate
    MISSING RULE for a human to add (e.g. a resistance-flatness upper bound to
    kill rising wedges). Calibration surfaces; a human decides. D4."""
    raise NotImplementedError("D4: hard-negative mining")
