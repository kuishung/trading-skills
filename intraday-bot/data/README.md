# data/ — the cream

Every persistent artifact the bot produces lives here:

- **Historical bars** ingested for analysis + enrichment
- **Journal** of every decision, plan, fill, exit
- **Review reports** — stats snapshots and threshold proposals
- **Fixtures** — curated bar windows + expected pattern verdicts for tests

This is the substrate the bot's self-improvement loop reads from. Other
folders (`strategy/`, `execution/`, `journal/`, `review/`) are **code**;
`state/` is ephemeral session state (flags, today's watchlist, caches);
`data/` is the long-term memory.

## Layout

```
data/
  price_history/
    1min/<SYM>.parquet               one file per symbol, full history
    5min/<SYM>.parquet
    15min/<SYM>.parquet
    daily/<SYM>.parquet
  ticker_profile/
    <SYM>.json                       behavioral baseline, ALL timeframes
  journal/journal_<YYYY-MM-DD>.jsonl one file per ET trading day
  review/
    stats_<YYYY-MM-DD>.json          snapshot from `review/stats.py --save`
    proposals_<YYYY-MM-DD>.json      snapshot from `review/propose.py --save`
  fixtures/
    <slug>.json                      labeled bar windows for _patterns_test.py
```

## Git posture

| Subfolder | Tracked? | Why |
|---|---|---|
| `price_history/`  | gitignored | Bulk binary, regeneratable from IBKR/TV/yfinance. Dropbox-synced across PCs so we don't re-pull on every machine. |
| `ticker_profile/` | gitignored | Per-ticker behavioral baselines (ATR, vol, 3m percentile distributions). Derived nightly from `price_history/`; TTL 24h; regeneratable. Same lifecycle as `price_history/` — Dropbox-synced, daily churn doesn't belong in git. |
| `journal/`  | **committed** | Cream. Decision history is the substrate for `review/`; must travel across PCs and survive disk loss. Append-only, never rewritten. |
| `review/`   | **committed** | Small curated snapshots — captures the state of the bot's beliefs at a moment in time. Treated like reports. |
| `fixtures/` | **committed** | Test corpus. Lives alongside the code it validates. |

## Bars format: Parquet, one file per symbol per timeframe

Why parquet:
- ~10× smaller than CSV / JSONL
- Columnar — filtering by date/symbol is native and fast
- pandas + DuckDB read it without extra glue
- Safe under Dropbox sync — single-writer, no concurrent-write hazard

Why one file per symbol (not partitioned by month):
- `AAPL.parquet IS AAPL's full history` — simplest mental model
- Easy to share/refresh: one ticker = one file to send
- DuckDB / pandas-pyarrow read it directly: `pq.read_table('data/price_history/1min/NVDA.parquet')`

Tradeoff: appending bars means rewriting the file (Parquet is
immutable). At ~3-5 MB per symbol-year and ~50ms per write that's
fine. Revisit (re-introduce month partitioning) only if we ever ingest
tick-level data or push past ~10 years × 100+ symbols.

Bar dict shape (matches `resources/patterns.py`):

```python
{"t": "2025-10-15T13:30:00Z", "o": 18.20, "h": 18.45, "l": 18.15, "c": 18.42, "v": 412300}
```

Read/write happens through `resources/bars_store.py` so the parquet
detail stays in one place. Patterns and ticker-profile code stay
format-agnostic.

## Fixtures format

Each fixture is a JSON file:

```json
{
  "symbol": "NVDA",
  "session_date": "2025-10-15",
  "timeframe": "1min",
  "label": "bull_flag_pmh_break",
  "expected": {"pattern": "bull_flag", "direction": "up"},
  "notes": "PM bull flag into the 09:30 open. Clean pole + flag + break.",
  "bars": [{"t": "...", "o": ..., "h": ..., "l": ..., "c": ..., "v": ...}, ...]
}
```

Loaded by `_patterns_test.py` as positive/negative cases against the
relevant pattern function. Adding a fixture promotes a single
real-world example to permanent regression coverage.

## Adding bars (manual ingest)

1. Pull bars from any source (IBKR `reqHistoricalData`, yfinance, TV export, CSV).
2. Convert to the bar-dict shape above.
3. `from bars_store import write_bars; write_bars("NVDA", bars_list, timeframe="1min")`.
4. The store merges with any existing `data/price_history/1min/NVDA.parquet` (dedup by timestamp, last write wins) and rewrites the file.

Bulk-historical-data ingestion CLIs live alongside the source modules
(e.g. `resources/ibkr_data.py` will grow a `--bulk-history` subcommand
when needed). The store itself stays a thin read/write API.

## What does NOT live here

- **Runtime / session state**: flags, today's watchlist, today's plan,
  per-day fills/equity snapshots, caches → `state/` (mostly gitignored,
  regenerated every session).
- **Code** (resources, strategies, execution, journal writer, review
  analyzers, dashboard server, scripts) → their respective folders.
- **Reference / source-of-truth documents** (strategy PDFs, GUNS
  materials) → `strategy/<FAMILY>/Materials/` (Dropbox-synced, gitignored).

## Changelog

### 2026-05-23 — `ticker_profile/` added (universal per-ticker behavioral baselines)
- User rule (chat 2026-05-23): *"if it is a universal product then it is data output, i propose to put it into the data folder in ...data\ticker_profile"*. Ticker profiles are not strategy-specific — they describe the ticker itself (ATR, vol stats, 3m percentile distributions), so they belong in `data/` next to the parquet bars they're derived from, not under `strategy/<FAMILY>/`.
- Layout: `data/ticker_profile/<TICKER>.json`. One file per ticker, sections per timeframe.
- Gitignored — same lifecycle as `price_history/`: regenerable from local parquets, refreshes daily, Dropbox-synced. Committing daily churn would be noise.
- POET.json migrated from `strategy/GUNS/profiles/`. NVRI freshly computed end-to-end (yfinance daily + yfinance 1m + local-parquet-derived 3m percentiles). See `resources/ticker_profile.py` for the API + recipe.

### 2026-05-21 — `bars/` → `price_history/` (flat one-file-per-symbol layout)
- Renamed `data/bars/` → `data/price_history/`. More descriptive name; reflects how the user thinks about it ("price history per ticker"), not how an engineer thinks ("OHLCV bars").
- Flattened the directory structure: `data/price_history/<tf>/<SYM>.parquet` (single file per symbol per timeframe) instead of `data/bars/<tf>/<SYM>/<YYYY-MM>.parquet` (month-partitioned). At our scale (≤5 years × ≤100 symbols ≈ 1-2 GB total) the rewrite-on-append cost is ~50ms per write — fine. Simpler mental model wins.
- `bars_store.py` refactored: `write_bars` now reads + dedups + rewrites a single file per symbol. `load_bars` reads one file. `available_range` is a thin wrapper over `load_bars`. Round-trip tested: write twice (different timestamps), append merges correctly, date-range filter works.
- `.gitignore`, CLAUDE.md, `data/README.md`, `resources/README.md` all updated. `pyarrow>=15` confirmed in requirements.

### 2026-05-21 — Folder established
- New top-level folder. 8th in the architecture (the existing 7 were:
  `resources/`, `strategy/`, `execution/`, `journal/`, `review/`,
  `dashboard/`, `scripts/`).
- Migrated `state/journal_*.jsonl` → `data/journal/journal_*.jsonl`.
  Journal writer (`journal/writer.py`) + reader (`review/stats.py`)
  updated to write/read from the new path. Old `state/` journals
  moved via `git mv` so history is preserved.
- Added `resources/bars_store.py` — lazy-parquet read/write API. No
  hot dependency on pyarrow; only the actual write path imports it.
- `review/stats.py` + `review/propose.py` got `--save` flags that
  drop JSON snapshots into `data/review/`.
- Reasoning: the journal, ingested bars, and review reports are the
  bot's *output* — the cream. They deserved one canonical home
  separate from `state/` (session-ephemeral) and the code folders.
