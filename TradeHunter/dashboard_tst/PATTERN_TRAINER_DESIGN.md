# Pattern Trainer — design

Status: **Phase 1 BUILT** (2026-06-15). A `dashboard_tst` page where you teach a
chart pattern by example on a real (parquet-backed) chart, then (later phases)
have the assistant generate a Markdown spec + a Python detector and scan the
universe for it.

## Decisions locked (user, 2026-06-15)
| # | Decision | Choice |
|---|---|---|
| 1 | Chart data source | **Parquet** (daily/3min/1min) via `resources.bars_store`. This is an OFFLINE training tool → parquet is correct (see the **CARVE-OUT** added to CLAUDE.md's parquet scope rule; live views still fetch live). |
| 2 | Chart library | **TradingView Lightweight Charts 4.2.0** (already used by the MATP chart) — renders our OHLCV; the TV hosted widget can't load parquet. |
| 3 | How the AI "sees" the chart | **Numeric bars you mark + your words.** You mark a region; those bars' OHLCV are re-loaded from parquet and injected into the prompt. No vision. |
| 4 | "Find pattern" scan universe | **Full parquet universe (~1500)** across daily/3min/1min (Phase 3). |
| 5 | Where pattern artifacts live | **Committed in the repo** — `strategy/patterns/<slug>/pattern.md` + `detect.py`. DB is source of truth; files are a rendered projection. |
| 6 | Thresholds | **Ticker-relative only** (ATR/%, z-scores, bar counts) per CLAUDE.md's normalization rule — never absolute $. |

## Architecture
```
Browser ──▶ /patterns (dashboard_tst, Hermes)
   ├─ chart  ── GET /patterns/{id}/bars?symbol=&tf= ──▶ resources.bars_store (PARQUET)
   ├─ label  ── draw the triangle ──▶ POST /patterns/{id}/examples.json (positive/negative)
   └─ find   ── GET /patterns/{id}/detect?symbol=&tf= ──▶ strategy/patterns/<slug>/detect.py
                 geometric detector overlays its matches on the chart (D5)
   (Phase 2) "Save what you learned" ──▶ pattern.md + detect.py (committed)
   (Phase 3) "Find pattern" ──▶ run detect.py over the parquet universe ──▶ matches
```
Labels are **drawn, not typed** — the drawn geometry IS the label; the calibrator
derives slope/R²/touches from the shape. (The original free-text "teaching chat",
DeepSeek-direct with marked-bars injection, was **removed in v2.97** once drawing
became canonical — it was redundant and confusing.)

## Data model (SQLAlchemy ORM, Postgres-ready; Alembic migrations `f2a3b4c5d6e7`, `a3b4c5d6e7f8`)
- **Pattern** — id, owner_id, name, slug, description, status
  (`learning|ready|archived`), chart_symbol, chart_timeframe, `pattern_md`,
  `detect_py`, md_path, script_path, created/updated.
- **PatternLesson** — pattern_id, seq, role, content, `marked`
  (JSON: {symbol, timeframe, start, end, n}), created_at. The old teaching-chat
  transcript. **The chat UI/routes were removed in v2.97** (drawing is the label);
  the table is retained (no migration) but no longer written.
- **PatternExample** (added 2026-06-16, migration `a3b4c5d6e7f8`) — pattern_id,
  symbol, timeframe, start_t, end_t, n_bars, label, note, created_at. A saved,
  named, reloadable marked region — the per-pattern **example gallery**. Distinct
  from `PatternLesson.marked` (which is per-chat-turn context); an example is a
  first-class artifact you reload to redraw the box and re-teach.

## Detector contract (Phase 2/3)
Each generated `detect.py` exposes a stable interface so the scanner is one code
path for every pattern:
```python
__version__ = "1.0.0"
def detect(bars: list[dict]) -> list[dict]:
    """bars = [{t,o,h,l,c,v}, ...] ascending. Return matches:
    [{start_t, end_t, score, notes}]. Thresholds ticker-relative (ATR/%/z)."""
```
`pattern.md` is the human spec the detector implements (shape, rules, sources).

## Page model — the calibration loop (BUILT v2.98)
The detail page is organised as the loop, not a form:
**Teach → Calibrate → Test → Correct → Promote** (with Test→Correct→Teach the tight
inner loop). Layout = a **chart workspace** (2/3) next to a **brain sidebar** (1/3).
- **One mode switch** `Teach ⇄ Test`. Teach = draw the triangle → save positive /
  counter (drawing is the only label; legacy click-`Mark region` removed). Test =
  `Find pattern` overlays the detector's hits.
- **Brain sidebar** = the pattern's state, always visible: example counts (✓/✗),
  suite pass-rate + bar, and the single next action — plus the gated buttons.
- **Gating** (`_readiness` in `patterns.py`): `Recalibrate` unlocks at
  ≥`_CALIB_MIN_POS` (3) ✓ and ≥`_CALIB_MIN_NEG` (2) ✗; `Promote` only when the
  validation suite ≥ `_PROMOTE_PASS` (0.80); `Run across universe` only once `ready`.
- **Correction loop**: each Test hit carries `✓ correct` / `✗ wrong` → one tap saves
  a positive / counter example (posts the detected window to `examples.json`) and
  bumps readiness. Active learning made literal — misfires feed the next calibration.
- **Routes**: `POST /calibrate` (fit thresholds from examples + run the suite, store
  `detector_thresholds`/`calib_pass_rate`/`calib_at`/`detector_version` on `Pattern`),
  `POST /promote`·`POST /reopen` (gated status flips), `GET /scan` (capped universe
  sweep, honest about the cap). `/detect` runs with the **calibrated** thresholds.

## Phased build
- **Phase 1 (BUILT; chat since removed):** `/patterns` page + parquet chart
  (symbol/timeframe load) + region marking (click start/end) + Pattern/PatternLesson
  models + migration. Originally shipped with a teaching chat (marked-bars
  injection); **the chat was removed in v2.97** when drawing became the canonical
  label. No artifact generation yet.
- **Phase 1.5 (BUILT 2026-06-16):** saved-examples gallery (PatternExample) — save
  a marked region as a named example, reload it to redraw the box. Plus the
  base.html `p`-shadow root-cause fix (see dashboard_tst README v2.94) that
  restores the chart auto-reopen.
- **Phase 2 (SUPERSEDED — see `strategy/patterns/DETECTOR_DESIGN.md`):** the
  original idea was "the assistant emits a one-off `detect.py`." That's now
  replaced by the **systematic framework**: `detect.py` is a deterministic
  geometric rule-scorer built on the shared `_geometry`/`_features` layers, with
  `SEED_THRESHOLDS` calibrated from the user's labelled examples
  (`_calibrate`). The Trainer's role is to feed that loop (gallery labels), not
  to free-write a detector. Still stored under `strategy/patterns/<slug>/`,
  committed, status → `ready`.
- **Phase 3:** "Find pattern" is the **harvester** (`_harvester`) — a loose
  high-recall sweep of the parquet universe surfacing candidate windows for
  confirm/reject review (the calibration/eval input), NOT a live-signal scan.
  See `DETECTOR_DESIGN.md` (D3–D5).
- **Phase 3a — per-chart "Find pattern" overlay (D5, BUILT v2.96):** before the
  full-universe sweep, the Trainer runs the detector against the *currently
  loaded* chart (`GET /patterns/{id}/detect?symbol&timeframe`) and lists the
  flagged windows under the chart; clicking one zooms to its range. This is the
  visual-evaluation surface — eyeball detector-vs-your-label agreement, turn
  mis-fires into negative examples — and the same code path the universe sweep
  will reuse per symbol.
- **Review-queue candidate sources (3):** the queue is fed by (a) pre-filter
  harvest, (b) active-learning (hardest cases), and (c) a **"Random sample"**
  button / auto-feed (`_harvester.random_sample`) — pick a random ticker+window,
  run `detect()`, qualify/correct → calibrate. Random is what catches the
  detector's **false negatives** (missed patterns the pre-filter can never
  surface); low-yield for positives, so it complements (a)/(b). All land on the
  same drag-to-label surface. See `DETECTOR_DESIGN.md` "Candidate sources".

## Open items / follow-ups
- **Marking precision:** Phase 1 marks by two clicks (start/end bar). A drag-box
  selection could be nicer later.
- **Detector execution safety (Phase 3):** `detect.py` is model-generated and runs
  on the dashboard host (where parquet lives). Single-tenant/owner-scoped + a
  per-run timeout for MVP; sandbox (subprocess/resource limits) is a follow-up.
- **Scan cost:** full-universe × 3 timeframes is ~4500 file reads — Phase 3 should
  queue + cap + show progress (mirror the ingest supervisor pattern), not block.
- **Agent-grounded teaching:** optional later — route the teaching chat through the
  Nous agent if it ever needs tools beyond the injected bars.
