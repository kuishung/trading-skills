# resources/ — Layer 1: stateless data sources

Read-only fetchers callable on-demand by any strategy. The Resources
layer knows nothing about strategies, holds no per-strategy logic,
never decides whether to trade. It just answers data questions.

Adding a new resource: drop a module here (e.g. `resources/finviz.py`)
and import it from whichever strategy needs it. No registration.

## Contents

- `ibkr_data.py` — IBKR bars / quotes / trades adapter. Lazy `ib_insync` import (heavy dep, only needed when `cfg["data_provider"]=="ibkr"`).
- `ibkr_smoke.py` — Bare-socket TWS handshake test. Run as CLI to verify IBKR connectivity.
- `ibkr_dryrun.py` — Exercises the data adapter end-to-end without any Alpaca side effects.
- `yfinance_float.py` — Free-float lookup via yfinance `Ticker.info["floatShares"]`. 7-day disk cache at `state/cache/float_<sym>.json`. Drops > 100M by default (configurable cap). Used by the GUNS scanner; reusable by future strategies that need float screening.
- `yfinance_news.py` — News-catalyst classifier via yfinance `Ticker.news`. 36-hour freshness window, regex tables for BAD (M&A, offering, dilution, going-concern, SEC actions, FDA reject) vs GOOD (earnings beat, FDA approval, contract, partnership, upgrade, AI sympathy). 4-hour cache. Currently consumed only by the GUNS scanner; patterns are general enough for reuse.
- `smw_premarket_movers.py` — Scrape of `stockmarketwatch.com/movers/premarket`. Returns list of `{symbol, change_pct, price, volume, direction, source}` dicts. Filters by direction (gainers/losers/both), min abs(change%), price range. Handles the page's Next.js SSR HTML-comment-interleaved numbers (`+<!-- -->40<!-- -->%`). Used by GUNS Setup 1's shortlist phase; generic enough for reuse.
- `tradingview-mcp/` — **Vendored MCP server** (`tradesdontlie/tradingview-mcp`). Node.js MCP that bridges Claude to TradingView Desktop via Chrome DevTools Protocol. Per-PC install: `cd resources/tradingview-mcp && npm install`. `node_modules/` + `package-lock.json` are gitignored. Upstream commit recorded in `_UPSTREAM.md`. Registered with Claude Code via `%USERPROFILE%\.claude\.mcp.json` (per-PC absolute path).
- `ticker_profile.py` — Per-ticker behavioral baseline (ATR, ADR, vol stddev, trend) cached at `strategy/<FAMILY>/profiles/<TICKER>.json` with 24h TTL. Today's data source is yfinance (free); TV MCP wiring is reserved for the next session. The substrate for ticker-relative thresholds (normalized strategy parameters rule). Public API: `get_profile()`, `refresh_profile()`, `get_or_refresh()`. CLI: `py resources/ticker_profile.py NVDA POET --force-refresh`.
- `patterns.py` — Pure-function pattern + signal-math primitives. No I/O, no vendor SDK. Operates on the standard bar dict shape `{t, o, h, l, c, v}`. Public API:
  - `ema(values, period)`, `sma(values, period)`, `vwap(bars)` — moving averages and VWAP series
  - `aggregate_to_n_min(bars, n=5)` — resample 1-min bars to N-min bars
  - `find_pivots(bars, left, right)` — local extrema (the foundation for most pattern logic)
  - `consolidation(bars, lookback_bars, max_range_pct)` — tight-range detection
  - `trend(bars, ema_period, slope_lookback)` — EMA-slope direction (up/down/sideways)
  - `higher_highs_lows(pivots)` — sequence analysis (uptrend / downtrend / mixed)
  - `bull_flag(bars, pole_min_pct, flag_max_bars, …)` — pole + flag detector with full evidence dict
  - `breakout_signal(bars, level, direction, min_volume_mult)` — has the latest bar broken `level`?
  - `ma_resistance(bars, current_price, periods, ma_kind)` — closest moving-average above current price
  
  Every function returns a STRUCTURED dict (not just a bool) so strategies can use the same output for both eligibility AND journal-evidence payloads. CLI demo: `py resources/patterns.py [bull_flag|consolidation|uptrend|aggregate]`.
- `sp500.py` — **S&P 500 constituent list.** Scrapes Wikipedia's canonical `List_of_S%26P_500_companies` table; 7-day cache at `state/cache/sp500.json`. Public API: `get_sp500_symbols(force_refresh=False)`, `refresh_sp500()`. CLI: `py resources/sp500.py` / `--count` / `--force-refresh`. Returns ~503 symbols (BRK.B / BF.B included in canonical dotted form; the yfinance ingest auto-maps these to Yahoo's dashed form).
- `yfinance_history.py` — **yfinance -> Parquet bulk ingest.** The fast-seed path. Bulk-downloads daily + intraday (1min / 5min / 15min) bars via yfinance, writes through `bars_store.write_bars()`. Batches symbols 50-at-a-time, retries transient failures (3 attempts, exponential backoff), maps share-class symbols (BRK.B <-> BRK-B). Intraday pulls include pre-market + after-hours (`prepost=True`) so GUNS-style strategies have the data they need. Yahoo lookback caps: 1min = 7 days, 5min = 60 days, 15min = 60 days, daily = unlimited. Resume-by-default skips symbols already stored. CLI: `seed-sp500 [--years N] [--include-1min] [--include-5min] [--include-15min] [--include-all-intraday] [--force]` / `ingest <SYMS> --tf daily|1min|5min|15min`.
- `ibkr_history.py` — **IBKR -> Parquet ingest pipeline.** Bulk seed and incremental update of historical OHLCV bars. Writes through `bars_store.write_bars()`. Two flows: SEED (one-shot lookback when first ingesting a symbol) and UPDATE (incremental, only bars after the last stored timestamp). Pacing: 7s between requests by default (under IB's 60-per-600s cap). Universe selection: symbols seen in `data/journal/` last 30d + today's GUNS watchlist + `cfg.history_universe`. Auto-triggered post-EOD by the orchestrator when `cfg.eod_history_ingest=true`. Clientid 83 (non-collision). CLI: `ingest <SYMS> --timeframes 1min,daily --days 60` / `update --universe` / `update --symbols NVDA,MSFT` / `list` / `universe`.
- `bars_store.py` — **The bars I/O layer.** Read/write OHLCV bars to/from `data/price_history/{1min,5min,15min,daily}/<SYM>.parquet`. One file per symbol per timeframe (`AAPL.parquet IS AAPL's full history`). Parquet via lazy `pyarrow` import (the hot path doesn't pay it). Append = read + dedup + rewrite the file; ~50ms at realistic scales. Public API: `load_bars(symbol, start, end, timeframe)`, `write_bars(symbol, bars, timeframe)`, `available_range(symbol, timeframe)`, `list_symbols(timeframe)`, `bars_dir(timeframe)`. Bar shape matches `patterns.py`: `{t, o, h, l, c, v}`. CLI: `py resources/bars_store.py list 1min` / `range NVDA 1min` / `head NVDA 1min --n 5`.

## Changelog

### 2026-05-21 — `yfinance_history.py`: 5-min + 15-min support (with pre-market)
- Extended the bulk ingest to handle Yahoo's 5-min and 15-min intervals. Both cap at 60 days of history per call (vs 7 days for 1-min). Pre-market + after-hours included (`prepost=True`) so GUNS pre-market detection has the bars it needs; callers can filter at query time.
- `seed_sp500()` grew `include_5min` + `include_15min` + `include_all_intraday` flags. CLI mirrors them. `--include-all-intraday` is the shortcut for "give me everything yfinance will sell me cheaply".
- `ingest_intraday()` now takes a `timeframe` argument (1min/5min/15min). Yahoo's per-interval lookback caps live in `TIMEFRAME_TO_YF` so passing too many `days` gets clamped with a warning instead of failing.
- Pre-flight check: NVDA returned 11,253 15-min bars and 11,250 5-min bars over the full 60-day window — confirms the prepost integration is working (timestamps start at 09:00 UTC = 04:00 ET pre-market open).

### 2026-05-21 — `sp500.py` + `yfinance_history.py` added — bulk-seed the S&P 500 in ~93 seconds
- User asked: "for first run can we get the S&P 500 historical price at 1 go?" Yes, but the honest answer required two new modules because IBKR pacing makes a 500-symbol seed take ~9 hours; yfinance bulk-downloads do it in ~5 minutes.
- `sp500.py` — Wikipedia scrape of `List_of_S%26P_500_companies`. 7-day cache. Returned 503 symbols on first call. Robust against page changes: regex-based, validates that at least 450 symbols were extracted before trusting the result.
- `yfinance_history.py` — bulk yfinance ingest writing through `bars_store.write_bars()`. Two flows: `seed_sp500()` (one-shot, 2y daily by default, optional --include-1min for 7d intraday) and `ingest(symbols, tf, years)` (manual subset). Batched 50 syms per call, retry/backoff on transient HTTP errors, BRK.B <-> BRK-B mapping for Yahoo's share-class quirk.
- Real-world results: 247,452 daily bars across 503 symbols in 93 seconds = 12 MB on disk. Parquet column compression is doing the heavy lifting.
- Why a SECOND provider instead of just IBKR: IBKR's 60-per-600s pacing cap makes 500-symbol seeding take ~9 hours. yfinance is free, bulk-friendly, and good enough for daily history. We use yfinance for the SEED (first-run, fast), keep IBKR for ongoing fresh-data updates (live, paid, authoritative).
- Boundary: yfinance caps 1-min history at 7 days per call. Anything deeper needs IBKR.

### 2026-05-21 — `ibkr_history.py` added — IBKR -> Parquet ingest pipeline
- New Layer-1 module. Pulls OHLCV history from IB Gateway / TWS via `ib_insync`, writes through `bars_store.write_bars()`. The user's "if I connect IBKR, you can work on Parquet files of most tickers and we need data appended/updated everyday" ask.
- Two flows: `ingest_history()` (bulk seed; chunks the request to stay under IB's per-bar-size duration cap) and `update_history()` (incremental; checks `bars_store.available_range()` and pulls only bars after the last stored timestamp).
- Pacing: 7s between requests by default. IB's hard cap is 60 historical requests per 600s; we operate at ~85 req/600s headroom. Configurable via `--pacing` CLI flag.
- Universe selection (`build_universe()`): UNION of (a) symbols in `data/journal/journal_*.jsonl` over last N days, (b) today's GUNS watchlist, (c) `cfg["history_universe"]` manual list. Returns what the user really cares about -- tickers the bot considered, plus any they manually flagged.
- Holds ONE long-lived IB connection across the whole bulk (avoids per-connect overhead at the cost of one socket). clientId 83 (intentional non-collision: 71 live bot, 80 observer, 82 GUNS scanner, 98 probe, 99 dashboard).
- Wired into `execution/orchestrator.py` post-EOD: after the 15:58 close-all sweep, runs `bulk_update(build_universe(), cfg["eod_history_timeframes"])`. Soft-fail by design (the bot's success doesn't depend on this; user can rerun manually). New `cfg.eod_history_*` keys control: `eod_history_ingest` (master switch, default true), `eod_history_timeframes` (default `["1min","daily"]`), `eod_history_journal_days` (default 30), `eod_history_seed_days` (default 60).
- CLI smoke-tested without an IB connection: `universe` returned 30 unique tickers from the existing 2-day journal. Live IBKR ingest is deferred to the next session when the user is at IB Gateway.

### 2026-05-21 — `bars_store.py` added — single I/O layer for historical bars
- New Layer-1 module. Read/write parquet-backed OHLCV bars under `data/price_history/`. One canonical place where bar storage lives — `patterns.py`, `ticker_profile.py`, and the future `review/backtest.py` all go through this so the parquet detail stays encapsulated.
- Layout: `data/price_history/<tf>/<SYM>.parquet` — one file per symbol per timeframe. User-proposed flat layout; simpler mental model than month-partitioning. Tradeoff: append-rewrites-whole-file (~50ms at our scale). Revisit if we ever ingest tick-level data.
- Lazy `pyarrow` import: the module imports clean without pyarrow installed; only the actual read/write functions trigger the import and surface a clean install hint if missing. Hot trading path stays dependency-light.
- Bar dict shape matches `patterns.py` exactly (`{t, o, h, l, c, v}`). Strategies can write bars they ingest from any source (IBKR, yfinance, TV export, CSV) without format-mismatch glue. Dedup-on-write (last value wins per timestamp) means re-ingesting an overlapping range is idempotent.
- Round-trip tested with real pyarrow: write 2 bars → append 1 more → read total 3 → date-range filter returns expected subset → `list_symbols` + `available_range` correct.

### 2026-05-21 — Evaluated PyPI `tradingpattern==0.0.5` — not viable, do not re-add
- User asked whether the PyPI package `tradingpattern` (TradingPatternScanner) could be wired in as a resource. Installed and tested on our env (numpy 2.4.5, pandas 3.0.3).
- 3 of 9 functions ran (`find_pivots` — weaker than ours; `calculate_support_resistance`; `detect_trendline`).
- 6 of 9 functions broken on pandas 3.0 — they assign string labels into float64 columns (`df['x']=np.nan` then `df.loc[mask,'x']='Head and Shoulder'`), which pandas 3 refuses to silently upcast. Two more (wedge, channel) fail with `KeyError: -1`. The broken ones are exactly the unique-value-add ones (H&S, triangle, wedge, channel, double top/bottom).
- Library is v0.0.5, last released early 2023, no maintenance signal.
- **Decision: don't depend on it.** When a future strategy needs H&S / triangle / wedge / channel / double top/bottom, build it in-house in `patterns.py` so the bar-dict shape stays consistent and the existing test harness covers it.
- Uninstalled. Skip re-evaluating this library unless it ships a new release with pandas 3 support.

### 2026-05-21 — `tradingview-mcp/` vendored in (corrected day-one-rule violation)
- Moved from `~/Dropbox/Claude/mcp-servers/tradingview-mcp` (outside intraday-bot — wrong) into `resources/tradingview-mcp/` (correct). The user reinforced the day-one rule: every dependency lives inside intraday-bot/, no "external tools" exceptions.
- Nested `.git` stripped at vendoring time. Upstream commit hash recorded in `_UPSTREAM.md` along with the procedure for refreshing to a later version.
- Added `resources/tradingview-mcp/node_modules/` and `package-lock.json` to root `.gitignore` (per-OS install artifacts).
- Re-registered with Claude Code at `%USERPROFILE%\.claude\.mcp.json` pointing at the new in-folder path. Per-PC: re-run `npm install` and update the path in `.mcp.json` after a fresh Dropbox sync.

### 2026-05-21 — `ticker_profile.py` added (Track 5 of the enrichment program)
- New Layer-1 module: per-ticker behavioral baselines (ATR, ATR%, avg minute volume, vol stddev, daily trend, prev close, PM range). The substrate for the user's "normalized strategy parameters" rule -- every threshold in strategy code can now be expressed as ATR multiples / vol z-scores rather than absolute dollar/share values.
- Storage: `strategy/<FAMILY>/profiles/<TICKER>.json` per CLAUDE.md convention.
- Refresh policy: 24h TTL, refresh on miss/stale; safe to call from the hot path.
- Data sources (priority): TradingView MCP (TODO, activates next session after MCP install) → yfinance (wired today, free tier) → IBKR (TODO, will use ibkr_history_bars).
- Smoke-tested: built a real POET profile via yfinance (ATR(14)=$2.53 / 17% of close, daily_trend=Uptrend, avg_minute_vol_rth=200K, n_daily_bars=60, n_rth_bars=1950).
- Public API: `profile_path()`, `get_profile()`, `save_profile()`, `is_stale()`, `refresh_profile()`, `get_or_refresh()`, plus the pure-math helpers `compute_profile_from_daily_bars()` and `compute_volume_stats_from_minute_bars()`. CLI: `py resources/ticker_profile.py POET NVDA --force-refresh`.

### 2026-05-21 — `ibkr_data.py`: new `probe_ibkr_reachable()`
- Non-`sys.exit` connect probe — returns `(ok: bool, reason: str)`. Use it at startup to decide between IBKR and Alpaca data without crashing the bot if IB Gateway isn't logged in. Uses `probe_client_id=98` to avoid collision with the live bot (71), dashboard (99), GUNS scanner (82), observer (80).
- Consumed by `execution/orchestrator.py` startup sequence: if `cfg.data_provider="ibkr"` and probe fails, the bot flips to Alpaca for the session, journals `data_provider_fallback`, and keeps running. Lets the bot auto-start at T-60 BMO even when the user forgot to launch IB Gateway.

### 2026-05-21 — `patterns.py` + `_patterns_test.py`: bull_flag algorithm fix
- Added `_patterns_test.py` — 49-check evaluation harness covering every public function in `patterns.py` with positive, negative, and edge cases.
- The harness surfaced and fixed a real bug in `patterns.bull_flag`: was using `highs[-1]` (latest pivot high) as the pole top, which let flag-internal micro-pivots masquerade as a tiny new pole. Changed to `max(highs, key=price)` for the pole top and `min(lows_before, key=price)` for the pole base — i.e., the BIGGEST swing in the visible bar set. Re-tested clean.
- Final state: 49/49 synthetic checks passing. Known limitations documented in the harness header and in chat: no real-historical-data validation yet, wick sensitivity in `find_pivots`, single-timeframe coverage, arbitrary trend slope thresholds.

### 2026-05-21 — `patterns.py` added — pattern-detection primitives
- New Layer-1 module of pure-function pattern + signal-math helpers, generic across timeframes and strategy families. No I/O. No vendor SDK imports.
- Public API: `ema`, `sma`, `vwap`, `aggregate_to_n_min`, `find_pivots`, `consolidation`, `trend`, `higher_highs_lows`, `bull_flag`, `breakout_signal`, `ma_resistance`.
- Foundation for: GUNS Setup 2 (PM pivot break → `find_pivots` + `breakout_signal`), Setup 3 (PM bull flag → `bull_flag` + `breakout_signal`), Setup 4 (post-open bull flag, same primitives), the missing daily-MA-resistance ask (`ma_resistance`), and the missing 5-min PM chart (`aggregate_to_n_min`). Existing Setup 1 + 5 can be retrofitted to use these primitives later (their hand-rolled equivalents in `strategy/GUNS/_helpers.py` and `strategy/signals.py` still work; no forced migration).
- Returns structured evidence dicts (not bools) so strategies can use the same output for both eligibility checks AND journal payloads.
- CLI demo with synthetic bars covers all functions: `py resources/patterns.py [bull_flag|consolidation|uptrend|aggregate]`.

### 2026-05-21 — `smw_premarket_movers.py` added
- New scrape of `stockmarketwatch.com/movers/premarket` (different page from the existing `thestockmarketwatch.com/markets/today.aspx` already used by `strategy/GUNS/scanner.py`).
- Server-side rendered HTML; parses the `stockTable` rows by `data-stock-symbol` attribute. Handles the page's Next.js SSR artifact where numbers are split by HTML comments (`+<!-- -->40<!-- -->%`) — `_strip_html()` removes comments + tags before parsing.
- Initial consumer: GUNS Setup 1's shortlist phase (`strategy/GUNS/guns_setup1/impl.py`), which calls `fetch_smw_premarket_movers(direction="gainers", min_change_pct=5.0, min_price=1.50, max_price=500)` at 09:00 ET.

### 2026-05-21 — Folder established
- Moved from `scripts/`:
  - `scripts/_ibkr_data.py` → `ibkr_data.py`
  - `scripts/_smoke_ibkr.py` → `ibkr_smoke.py`
  - `scripts/_dryrun_ibkr.py` → `ibkr_dryrun.py`
  - `scripts/guns_float_lookup.py` → `yfinance_float.py` (renamed — it was always generic, the `guns_` prefix was wrong)
  - `scripts/guns_catalyst_classifier.py` → `yfinance_news.py` (same)
- Each module starts with a sys.path bootstrap so cross-layer imports (`from _common import ...`) keep working regardless of how the file is invoked.
