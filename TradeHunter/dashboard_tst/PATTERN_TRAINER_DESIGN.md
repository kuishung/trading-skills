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
   └─ chat   ── POST /patterns/{id}/chat.json ──▶ pattern_llm (DeepSeek-direct)
                 marked region's OHLCV injected into the prompt
   (Phase 2) "Save what you learned" ──▶ pattern.md + detect.py (committed)
   (Phase 3) "Find pattern" ──▶ run detect.py over the parquet universe ──▶ matches
```
The teaching chat does **not** use the Nous agent/corpus — the data it reasons
over (the bars) is injected directly — so DeepSeek-direct is sufficient and fast.

## Data model (SQLAlchemy ORM, Postgres-ready; Alembic migration `f2a3b4c5d6e7`)
- **Pattern** — id, owner_id, name, slug, description, status
  (`learning|ready|archived`), chart_symbol, chart_timeframe, `pattern_md`,
  `detect_py`, md_path, script_path, created/updated.
- **PatternLesson** — pattern_id, seq, role, content, `marked`
  (JSON: {symbol, timeframe, start, end, n}), created_at. The teaching transcript.

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

## Phased build
- **Phase 1 (BUILT):** `/patterns` page + parquet chart (symbol/timeframe load) +
  region marking (click start/end) + teaching chat with marked-bars injection +
  Pattern/PatternLesson models + migration. No artifact generation yet.
- **Phase 2:** "Save what you learned" → the assistant emits `pattern.md` +
  `detect.py` (validated against the contract), stored in the DB and written to
  `strategy/patterns/<slug>/`, committed. Status → `ready`.
- **Phase 3:** "Find pattern" → run `detect.py` over the full parquet universe ×
  timeframes (bounded/queued), surface matches the user can load on the chart to
  verify. Show match counts + per-match mini-context.

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
