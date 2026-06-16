# strategy/patterns/ — user-taught chart patterns (committed artifacts)

**Role:** the committed home for patterns taught through the **Pattern Trainer**
(`dashboard_tst` `/patterns`). Each pattern the user teaches is distilled into a
spec + a detector and saved here so it travels across PCs via git/Dropbox and is
reviewable.

These are NOT a strategy *family* (no scanner/orchestrator wiring). They're
reusable detectors the Pattern Trainer's "Find pattern" scan runs over the
parquet universe.

## Layout (one folder per pattern, by slug)
```
strategy/patterns/<slug>/
  pattern.md     # the learned human spec — shape, rules, ticker-relative
                 #   thresholds (ATR/%/z, bar counts), what fires it, sources
  detect.py      # the generated detector implementing the contract below
```

## Detector contract
Every `detect.py` exposes the same interface so the scanner is one code path:
```python
__version__ = "1.0.0"
def detect(bars: list[dict]) -> list[dict]:
    """bars = [{t,o,h,l,c,v}, ...] ascending (bars_store shape).
    Return matches: [{start_t, end_t, score, notes}].
    Thresholds MUST be ticker-relative (ATR/%/z-scores/bar counts) — never
    absolute dollars/shares (CLAUDE.md normalization rule)."""
```

## Source of truth
The Pattern Trainer DB (`patterns` / `pattern_lessons` tables) is authoritative;
the files here are a rendered projection written when you "Save what you learned"
(Phase 2). Edit through the Trainer, not by hand, so the DB and files stay in sync.

## Contents
- `DETECTOR_DESIGN.md` — **the plan** for the detector engine: rule-based
  geometric (swing extraction → line fit → slope/R²/convergence/touches/
  contraction, all ticker-relative), how the gallery examples calibrate it
  (not train it), the validation harness (calibration suite + walk-forward +
  no-lookahead), execution wiring, and where ML enters later (narrow/optional).
- **`__init__.py`** — package docstring stating the 4 layers + the calibration
  loop; records that this framework SUPERSEDES the ad-hoc "LLM emits detect.py".
- **`_geometry.py`** (L1) — swings + line fit + touches + convergence +
  contraction + ATR. Pattern-agnostic measurement; ticker-relative; no lookahead.
- **`_features.py`** (L2) — window → scale-invariant feature vector (`FEATURE_KEYS`).
- **`_harvester.py`** (D3) — loose high-recall sweep of the parquet universe →
  candidate windows for review; active-learning ranking. Background batch job.
- **`_calibrate.py`** (D4) — fit thresholds to the user's labelled set (the
  "smart from examples" step); portable thresholds dict.
- **`_validate.py`** (D4) — calibration suite + walk-forward + held-out tickers +
  no-lookahead probe.
- **`ascending_triangle/`** — first pattern. `detect.py` (L3 rule scorer +
  `SEED_THRESHOLDS`) + `pattern.md` (the human spec). Detector body is a stub
  (build phase D2); spec + seed thresholds are recorded.
- All skeleton modules import cleanly; stub bodies `raise NotImplementedError`
  tagged with their build phase (D1–D4), so the skeleton doubles as the build map.

## Changelog
- **2026-06-16** — **Skeleton of the systematic pattern-recognition framework
  laid down** (the package above), making `DETECTOR_DESIGN.md`'s 4-layer +
  harvest→label→fit→validate approach the structure of record. This SUPERSEDES
  the earlier ad-hoc idea (an LLM emitting a one-off `detect.py` with no shared
  geometry layer or calibration story). Modules are importable stubs phase-tagged
  D1–D4; `ascending_triangle` carries the v0 seed thresholds + spec. Nothing
  implemented yet — next is D1 (`_geometry`) + D2 (`detect.py`).
- **2026-06-16** — Added `DETECTOR_DESIGN.md` (PLAN). Records the decision that
  `detect.py` is a **deterministic geometric** detector (ticker-relative, no ML,
  no hardcoded $) — "not hardcoded" ≠ "needs AI". Saved gallery examples are a
  calibration/validation set, not training data; ML (residual classifier +
  separate success-predictor) is deferred and optional. Refines
  `PATTERN_TRAINER_DESIGN.md` Phase 2 (the generated detector is geometric).
- **2026-06-15** — Folder created. Home for Pattern Trainer artifacts
  (`pattern.md` + `detect.py` per taught pattern). Pairs with dashboard_tst v2.88
  (Pattern Trainer Phase 1) and `PATTERN_TRAINER_DESIGN.md`.
