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
- _(none yet — Phase 1 ships the trainer; patterns appear here once Phase 2's
  "Save what you learned" generation lands.)_

## Changelog
- **2026-06-15** — Folder created. Home for Pattern Trainer artifacts
  (`pattern.md` + `detect.py` per taught pattern). Pairs with dashboard_tst v2.88
  (Pattern Trainer Phase 1) and `PATTERN_TRAINER_DESIGN.md`.
