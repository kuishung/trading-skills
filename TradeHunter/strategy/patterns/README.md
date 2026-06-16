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
- **`_geometry.py`** (L1, **IMPLEMENTED D1**) — swings + line fit + touches +
  convergence + contraction + ATR. Pattern-agnostic measurement; ticker-relative;
  no lookahead. Pure Python (no numpy). Convention: lines fit with the window's
  last bar at x=0, so `convergence` returns bars-ahead-to-apex directly.
- **`_features.py`** (L2, **IMPLEMENTED D2**) — window → scale-invariant feature
  vector (`FEATURE_KEYS`); slopes in ATR-multiples/bar; degrades gracefully when
  a window lacks ≥2 swing highs/lows.
- **`_harvester.py`** (D3) — review-queue candidate sources: loose high-recall
  `harvest` sweep, `active_learning_rank` (hardest cases), and `random_sample`
  (random ticker+window → run detect → qualify; catches the detector's FALSE
  NEGATIVES that the pre-filter can't surface). Background/offline (parquet).
- **`_calibrate.py`** (D4) — fit thresholds to the user's labelled set (the
  "smart from examples" step); portable thresholds dict.
- **`_validate.py`** (D4) — calibration suite + walk-forward + held-out tickers +
  no-lookahead probe.
- **`ascending_triangle/`** — first pattern. `detect.py` (L3 rule scorer +
  `SEED_THRESHOLDS`, **IMPLEMENTED D2**: slides windows → features → soft-scored
  rules → NMS → explainable matches) + `pattern.md` (the human spec).
- Remaining stubs (`_harvester` D3, `_calibrate`/`_validate` D4) still
  `raise NotImplementedError` tagged with their build phase, so the skeleton
  doubles as the build map. **D1–D2 are now implemented + verified** (synthetic
  triangle scores ~0.97; runs on real parquet — NVDA/AAPL/AMD).

## Changelog
- **2026-06-16** — **D1 + D2 implemented + verified.** `_geometry` (ATR, fractal
  swings with no-lookahead + ATR-prominence, least-squares line fit/R², touches,
  convergence, contraction) and `_features.window_features` are now real pure
  functions; `ascending_triangle/detect.py` is a working geometric rule-scorer
  (sliding windows → soft sub-scores → geometric-mean → non-max suppression →
  explainable `notes`). Verified: a synthetic ascending triangle scores ~0.97 and
  the detector runs on real parquet (NVDA/AAPL/AMD) producing a handful of
  explainable candidates each. Fixed a coordinate bug found in verification
  (touches were evaluated at absolute bar-index against window-relative lines →
  always 0; now evaluated in the line's coordinate space). Seed thresholds
  unchanged — precision tuning is D4 (calibration from gallery labels). Next: D3
  harvester (feed the review queue) then D4 calibrate/validate.
- **2026-06-16** — Added **`_harvester.random_sample`** + a "Candidate sources"
  section to `DETECTOR_DESIGN.md`: the review queue is fed by harvest +
  active-learning + **random sampling** (random ticker+window → run detect →
  qualify/calibrate). Random is what catches the detector's FALSE NEGATIVES the
  pre-filter can never surface; low-yield for positives, so it complements the
  other two. (User, 2026-06-16: "randomly allow trainer call the detector to
  detect a pattern and calibrate on it.")
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
