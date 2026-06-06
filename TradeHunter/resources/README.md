# resources/ — Layer 1: stateless data sources

Read-only fetchers callable on-demand by any strategy. The Resources
layer knows nothing about strategies, holds no per-strategy logic,
never decides whether to trade. It just answers data questions.

Adding a new resource: drop a module here (e.g. `resources/finviz.py`)
and import it from whichever strategy needs it. No registration.

**Horizontal S/R framework**: the canonical specification lives at
`strategies-reference/SR.md` ("Identifying Support & Resistance on a
1Y / 1D Chart"). `patterns.horizontal_resistance_np` + `sr_levels`
(horizontal_support_np, find_broken_resistance_below,
find_broken_support_above) are the numerical implementation of that
framework. Known deviations from SR.md (`min_touches=1`, no volume
integration, round-number detection limited to DITP P2 only) are
documented in the `sr_levels.py` module docstring — read those before
treating any "missing" feature as a bug.

## Contents

- `ibkr_data.py` — IBKR bars / quotes / trades adapter. Lazy `ib_insync` import (heavy dep, only needed when `cfg["data_provider"]=="ibkr"`).
- `ibkr_smoke.py` — Bare-socket TWS handshake test. Run as CLI to verify IBKR connectivity.
- `ibkr_probe_symbols.py` — One-shot read-only diagnostic (clientId 98) for symbols that ingest `+0 bars`. Runs `reqContractDetails` raw + dot→space, prints contract count / `conId` / `primaryExchange`, then a tiny daily TRADES pull. CLI: `py -3.12 resources/ibkr_probe_symbols.py [SYM ...]`.
- `ibkr_unservable.txt` — Plain-text skip-list of symbols IBKR cannot serve historical bars for on this account (resolve only under the dataless `VALUE` exchange, or not findable as a US stock). `ibkr_history.bulk_update()` drops these before the pre-flight scan so the ingest stops re-attempting them every restart. See the file header for the diagnosis + when to re-enable.
- `ibkr_dryrun.py` — Exercises the data adapter end-to-end without any Alpaca side effects.
- `yfinance_float.py` — Free-float lookup via yfinance `Ticker.info["floatShares"]`. 7-day disk cache at `state/cache/float_<sym>.json`. Drops > 100M by default (configurable cap). Used by the GUNS scanner; reusable by future strategies that need float screening.
- `yfinance_news.py` — News-catalyst classifier via yfinance `Ticker.news`. 36-hour freshness window, regex tables for BAD (M&A, offering, dilution, going-concern, SEC actions, FDA reject) vs GOOD (earnings beat, FDA approval, contract, partnership, upgrade, AI sympathy). 4-hour cache. Currently consumed only by the GUNS scanner; patterns are general enough for reuse.
- `smw_premarket_movers.py` — Scrape of `stockmarketwatch.com/movers/premarket`. Returns list of `{symbol, change_pct, price, volume, direction, source}` dicts. Filters by direction (gainers/losers/both), min abs(change%), price range. Handles the page's Next.js SSR HTML-comment-interleaved numbers (`+<!-- -->40<!-- -->%`). Used by GUNS Setup 1's shortlist phase; generic enough for reuse.
- `tradingview-mcp/` — **Vendored MCP server** (`tradesdontlie/tradingview-mcp`). Node.js MCP that bridges Claude to TradingView Desktop via Chrome DevTools Protocol. Per-PC install: `cd resources/tradingview-mcp && npm install`. `node_modules/` + `package-lock.json` are gitignored. Upstream commit recorded in `_UPSTREAM.md`. Registered with Claude Code via `%USERPROFILE%\.claude\.mcp.json` (per-PC absolute path).
- `MATP/` — **Vendored skill** (Median Analyst Target Price). End-to-end pipeline turning a Finviz screener URL into MATP (median post-earnings analyst price target) + MBP (Max Buy Price = MATP/1.15) per ticker, with optional trend classification, daily EMA-bounce Telegram alerts, Google Sheets push, and a TradingView Pine indicator + importable watchlist. Self-contained: `scripts/` + `SKILL.md` + own `requirements.txt`/`.gitignore`. Per-PC `.env` (credentials) and generated run artifacts (`MATP_table.csv`, `MATP_indicator.pine`, `MATP_analysis.md`, `MATP_watchlist.txt`) are NOT committed. Moved here from the repo root on 2026-05-30 to live under the TradeHunter roof; the canonical analyst-target source the `dashboard_tst` trend & swing platform builds its MATP board on (see `dashboard_tst/DESIGN.md`), reusable by any strategy needing target levels.
- `ticker_profile.py` — Per-ticker behavioral baseline (UNIVERSAL cross-strategy product) cached at `data/ticker_profile/<TICKER>.json` with 24h TTL. One file per ticker, sections per timeframe: `stats_daily` (ATR/trend/prev_close), `stats_1m_rth` (vol stats), `stats_3m_rth` (ATR + range percentiles + body / tail ratio distributions + outside-bar freq — the substrate for ticker-relative candlestick anti-patterns). Data sources: yfinance for daily+1m, local 1m parquet → aggregated for 3m. The substrate for the normalized-parameter rule. Public API: `get_profile(ticker)`, `refresh_profile(ticker)`, `get_or_refresh(ticker)`, `is_stale(ticker)`. CLI: `py resources/ticker_profile.py NVDA --force-refresh`. **This is the INTRADAY profile** (see `swing_profile.py` for the trend/swing one). `refresh_profile(t, source="store")` (added 2026-06-06) builds it **offline from the seeded parquet** (daily + native 3min via `compute_3m_stats_from_3m_bars`, no yfinance/1min) — `_bar_stats_3m` is the shared candle-stat core for both the 1m-aggregated and native-3m paths. This is the path the nightly regen uses.
- `swing_profile.py` — **Per-ticker SWING/TREND profile** (the second profile product) cached at `data/swing_profile/<TICKER>.json`. Daily(2yr)+weekly: daily+weekly trend state (reuses MATP `classify_trend`), EMA20/50/200 structure/slope/stacking, 52w position, swing ATR, volatility-contraction (base quality), accumulation/distribution, EMA pullback, 1/3/6-mo momentum, cross-sectional RS percentile. Weekly regen + earnings-driven. API: `get_swing_profile`, `refresh_swing_profile`, `refresh_all_swing`. CLI: `py -3.12 resources/swing_profile.py NVDA | --all`.
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
- `market_calendar.py` — **NYSE market calendar.** Hardcoded full-closure list for 2024-2028 + `is_trading_day(d) / last_trading_day(d) / next_trading_day(d)` helpers. Source-of-truth for any scanner / scheduler that needs to skip Sat/Sun **and** US market holidays. Update annually as the NYSE publishes the new year's calendar. CLI: `py resources/market_calendar.py --date 2026-05-25` / `--list-year 2026`.
- `finviz_screener.py` — **Finviz screener URL → symbol list, with on-disk caching.** Pulls the result-set tickers out of a Finviz screener URL so the dashboard's `/scanner/yf_scan` can use it as the universe. Walks pages (20 rows each) until a page yields zero new symbols; rate-limited at 1 page/sec. Caches per-URL at `state/cache/finviz_<sha1>.json` with default 1h TTL. Parses via `data-boxover-ticker` HTML attribute (most stable hook across recent Finviz markup changes). User-Agent header set to a current Chrome to avoid soft blocks. Returns dotted-form share classes (BRK.B not BRK-B). Empty list on fetch failure → caller falls back to a default universe rather than scanning nothing. Public API: `fetch_screener_symbols(url, max_pages=20, cache_ttl_s=3600, force_refresh=False)`. CLI: `py resources/finviz_screener.py "<URL>"` / `--force-refresh`.
- `yf_daily_bars.py` — **yFinance daily-bar adapter for interactive scanning.** Distinct purpose from `yfinance_history.py`: this module returns bars **in-memory only** in the canonical `[{t,o,h,l,c,v}]` shape; nothing written to disk. Used by the dashboard's `POST /scanner/yf_scan` so daily-chart setups can run on fresh yFinance data without touching the parquet store (which is reserved for backtesting per user directive 2026-05-27). Auto-handles yFinance's multi-index DataFrame shape with `group_by="ticker"` and the `BRK.B <-> BRK-B` share-class symbol normalization. Public API: `fetch_daily_batch(symbols, lookback_days=400)`, `fetch_daily_single(symbol, lookback_days=400)`. CLI: `py resources/yf_daily_bars.py AAPL MSFT --lookback 30` (prints last 5 bars per symbol).
- `sr_levels.py` — **Key support / resistance level detector** (v1.3.0). Unifies the SIX DITP setups' level questions: P1/P2/P3 (long side: support / resistance / broken-R-as-S) AND P1a/P2a/P3a (short-side mirror: rejection-at-R / pending-breakdown-of-S / broken-S-as-R). Built on `patterns.horizontal_resistance_np` (long resistance) + `horizontal_support_np` (long support) + `find_broken_resistance_below` (P3 polarity flip) + `find_broken_support_above` (P3a polarity flip, the short-side mirror). Selection rules are asymmetric (see module docstring): resistance-above = lowest in price, support-below = most-recent in time, broken-R = highest below current, broken-S = lowest above current. One-stop API: `find_key_levels(symbol)` returns `{current, atr14, resistance_above, support_below, broken_resistance:[...]}` — consumed by `GET /chart/sr_levels` in `dashboard_intraday/server.py`. All thresholds ATR-relative per CLAUDE.md normalization rule. CLI: `py resources/sr_levels.py AAPL ABBV MSFT`.
- `symbol_ctx.py` — **Per-symbol shared computation context.** Added 2026-05-29 efficiency Pass 2 #1 per the dashboard audit. Hoists the prelude that every DITP detector independently recomputed (bars load + 4 numpy array allocations + EMA20/EMA50/EMA200 + ATR(14)) into ONE place. Built once per symbol by the dashboard's `_run_all_setups_sync` before fanning each detector across a ThreadPoolExecutor. Detectors accept an optional `ctx: SymbolContext | None = None` kwarg — when present, they skip the entire prelude; when None (CLI / backtest path), they rebuild context internally. `frozen=True` dataclass so accidental mutation is caught. Public API: `SymbolContext(...)` (dataclass), `build_context(symbol, bars=None, *, min_bars=15) -> SymbolContext | None`.
- `bars_store.py` — **The bars I/O layer.** Read/write OHLCV bars to/from `data/price_history/{1min,5min,15min,daily}/<SYM>.parquet`. One file per symbol per timeframe (`AAPL.parquet IS AAPL's full history`). Parquet via lazy `pyarrow` import (the hot path doesn't pay it). Append = read + dedup + rewrite the file; ~50ms at realistic scales. Public API: `load_bars(symbol, start, end, timeframe)`, `write_bars(symbol, bars, timeframe)`, `available_range(symbol, timeframe)`, `list_symbols(timeframe)`, `bars_dir(timeframe)`. Bar shape matches `patterns.py`: `{t, o, h, l, c, v}`. CLI: `py resources/bars_store.py list 1min` / `range NVDA 1min` / `head NVDA 1min --n 5`.
- `trend_state.py` — **EMA 20/50/200 daily-chart trend classifier** (v1.0.0). Source: `strategies-reference/TREND_EMA.md`. Classifies any symbol into one of four states using EMA stack order + spread dynamics: `uptrend` (EMA20 > EMA50 > EMA200), `downtrend` (EMA20 < EMA50 < EMA200), `consolidation` (not stacked + all EMAs converging), `sideways` (not stacked + not converging). Pure-function core (no I/O): `classify_trend_ema(closes)`, `trend_ema_detail(closes)`. Convenience wrappers with bars_store integration: `classify_symbol_trend(symbol, detail=False)`, `classify_universe_trends(symbols)`. String constants exported: `UPTREND`, `DOWNTREND`, `CONSOLIDATION`, `SIDEWAYS`, `UNKNOWN`. Smoke-tested on 679 symbols (0 unknowns).

## Changelog

### 2026-06-06 — backtest no-lookahead primitives (`bar_session_date_et` + `profile_at`)
- **`bars_store.bar_session_date_et(t)` (new)** — the US market SESSION date
  (America/New_York) for a bar timestamp: pre-market 04:00 → after-hours 20:00
  ET fold into one date. The backtester (and any session-bucketing code) must
  use this instead of a naive UTC date, which mis-files after-hours bars
  (20:00 ET = 00:00 UTC next day) into the wrong session.
- **`ticker_profile.profile_at(ticker, as_of, *, save=False)` (new)** — the
  POINT-IN-TIME intraday profile: same shape as `refresh_profile_from_store`
  but built only from parquet bars on/before the ET session `as_of`, and not
  saved by default. This is the no-lookahead guard for backtest adapters — the
  nightly `data/ticker_profile/<T>.json` files are *today's* snapshot, so
  reading them inside a backtest leaks the future. Verified point-in-time: NVDA
  ATR 5.29 (Feb) → 5.45 (Apr) → 7.81 (Jun) across as-of dates.

### 2026-06-06 — `swing_profile.py`: SWING/TREND per-ticker profile (the second profile product)

**Two-profile convention (locked 2026-06-06):** per-ticker behavioral profiles are
**two separate products by trading style**, not one:
- `ticker_profile.py` → **INTRADAY** (3/5min + daily, recency window, **nightly**
  regen) → `data/ticker_profile/<T>.json` → GUNS/DITP / `dashboard_intraday`.
- `swing_profile.py` → **SWING/TREND** (daily 2yr + weekly resample, long lookback,
  **weekly** regen + earnings-driven) → `data/swing_profile/<T>.json` → MATP / swing
  setups / `dashboard_tst`.

They share `bars_store` + the per-ticker-JSON shape but differ in timeframe,
lookback, cadence, fields, and consumer. `swing_profile.py` reuses the MATP
`classify_trend` rule (Uptrend/Downtrend/Sideways/Unknown) so trend states agree.
Fields (daily-derived): daily + weekly trend state, EMA20/50/200 structure +
slope/stacking, 52w position, swing ATR, volatility-contraction (base quality),
accumulation/distribution, EMA pullback, 1/3/6-mo momentum, and **cross-sectional
RS percentile** (rank of 3-mo return across the universe — no index ETF in the
store). `analyst_target`/`mbp`/`next_earnings` left null (merged from MATP/yfinance).
Full universe ~0.8 min (daily files are tiny). CLI:
`py -3.12 resources/swing_profile.py NVDA` or `--all`.

### 2026-06-04 — `bulk_update` recency-skip: fix UTC/ET date skew (winter data-loss bug)

The `fresh_through` recency-skip compared the latest bar's **UTC** date to the ET
session date. Daily bars are anchored to the UTC calendar date (fine), but
intraday bars carry real session timestamps — in **EST** the prior session's last
bar (e.g. 19:55 ET) is `00:55 UTC` the *next* day, so its UTC date equalled
tonight's session and the symbol was falsely classified `fresh` → the entire
nightly **intraday** top-up would be skipped every winter night (silent data
loss, self-healing only the next day). Fix: daily keeps the UTC calendar date;
**intraday converts to ET** (`_et_tz()`) before taking `.date()`. Regression test
confirms the old logic wrongly skips and the new logic fetches.

### 2026-06-03 — `ibkr_history.bulk_update`: add `fresh_through` recency-skip

New `fresh_through` (a `datetime.date`) param: in the pre-flight, any `(sym,tf)`
whose latest stored bar is already `>= fresh_through` is classified `fresh` and
**skipped entirely (no IBKR request)**. Distinct from `skip_up_to_date` (which is
depth-based) — this is recency-based, for the nightly top-up driven by
`ingest_supervisor.py`: on a crash-retry it skips the symbols already brought
current and only re-paces through the un-fetched tail, so the run stays inside
the 08:00 ET deadline. Pre-flight summary now also reports `fresh=N`.

### 2026-06-03 — fix stuck +0-bar ingest loop: share-class `_stock()` + skip-list

Resolved the "ingest keeps looping / last ~28 tickers take ages" problem.
Probe (`ibkr_probe_symbols.py`) on Hermes split the stuck set into three:

1. **Share-class dot bug** (`BF.B`, `BRK.B`, `MOG.A`) — `ibkr_data._stock()`
   sent the dot form to IBKR (`reqContractDetails` → "No security definition")
   so every history request returned empty. **Fix:** `_stock()` now maps
   `.`→` ` for the IBKR contract only (`BRK.B`→`BRK B`), keeping the dotted
   symbol as the parquet/storage key. `ibkr_history._stock` delegates here, so
   both adapters are fixed at once. Confirmed: dot→space returns bars.
2. **IBKR-unservable** (`CWEN.A`, `ASGN`, `BK`, `CSGS`, `EXPI`, `MCW`, `PSTG`,
   `SNCY`) — resolve only under the dataless `VALUE` placeholder exchange
   (hist → Error 162) or aren't findable as US stocks (conId+SMART → "Unknown
   contract"). Likely a missing US market-data subscription on the account.
   **Fix:** added `resources/ibkr_unservable.txt` + `load_unservable()`;
   `bulk_update()` now drops these before the pre-flight scan and logs what it
   excluded (no silent truncation), so they never re-enter the work list.
3. **Corrupt parquets** (`AMRX`, `AORT` 3min — `OSError: Column cannot have
   more than one dictionary`, partial-sync corruption) — deleted so they
   re-seed clean.

### 2026-06-03 — `ibkr_probe_symbols.py`: diagnose stuck +0-bar seeds

Added a one-shot, read-only diagnostic (clientId 98, paper port) to explain
why a fixed set of ~28 `(symbol,timeframe)` pairs return `+0 bars` on every
`wait_and_ingest` run and so never drain from the work list (making the
ingest *look* like an infinite loop). For each symbol it runs
`reqContractDetails` on the raw ticker AND the dot→space variant, reports
contract count (0 = unknown, >1 = ambiguous) + each candidate's
`conId`/`primaryExchange`, then attempts a tiny 5-day daily TRADES pull to
show the real outcome. Isolates the three hypotheses: dotted share classes
(`BRK.B`→`BRK B`, a `_stock()` bug), ambiguous SMART contracts (need
`primaryExchange`), and genuinely-no-data. CLI-only dev tool (dashboard-rule
exempt). Run on whichever machine has IB Gateway paper reachable:
`py -3.12 resources/ibkr_probe_symbols.py`.

First Hermes run (2026-06-03) confirmed: dotted classes (`BF.B`/`BRK.B`/
`MOG.A`) resolve once converted to IBKR's space form (`BF B` …) and return
bars; but 7 "plain" tickers (ASGN/BK/CSGS/EXPI/MCW/PSTG/SNCY) **and** CWEN.A
returned `0 contracts` / "No security definition" while TSLA worked. To tell
"needs `primaryExchange`" from a burst-throttle of `reqContractDetails`, the
probe gained request **pacing** (`--pace`, default 1.5s) and a
`reqMatchingSymbols` fallback that prints IBKR's own search results + retries
contract lookup with the matched `primaryExchange`.

### 2026-05-30 — `finviz_screener.py`: fix Finviz 301 + connection drop

Finviz moved the screener path (`screener.ashx` -> `screener`, served as a 301) and now **drops requests that lack browser-like Accept headers** — the bare `urllib` request (User-Agent only) was getting `RemoteDisconnected`, so `fetch_screener_symbols`/`fetch_screener_rows` returned empty. Fix: `_fetch_page` now sends `Accept` + `Accept-Language` alongside the User-Agent; urllib follows the 301 cleanly. Verified live: a mega-cap-tech filter returns 25 symbols and 25 rows with price+volume (the `<!-- TS -->` block still parses). Benefits every caller (GUNS scanner, the dashboard's universe builder, and the new `dashboard_tst` Finviz tab).

### 2026-05-30 — `MATP/` vendored skill moved in from the repo root

The MATP skill (Median Analyst Target Price pipeline) moved from `trading-skills/MATP/` to `resources/MATP/` so it lives under the TradeHunter roof (day-one rule: every dependency inside `TradeHunter/`). Motivation: it's the canonical analyst-target source the `dashboard_tst` trend & swing platform builds its MATP/MBP board on (see `dashboard_tst/DESIGN.md`), and it sits naturally alongside the other vendored sub-project (`tradingview-mcp/`). Committed as a git rename of the 13 tracked source files (`SKILL.md`, `requirements.txt`, `.gitignore`, `scripts/*.py`) — history preserved. Deliberately excluded from the commit: the per-PC `.env` (credentials), the generated run artifacts (`MATP_table.csv`/`MATP_indicator.pine`/`MATP_analysis.md`/`MATP_watchlist.txt`), `__pycache__`, and a stray Claude worktree (`.claude/worktrees/…`) that got copied along with the move — those remain untracked. Note: MATP's own `.gitignore` covers `.env`/`__pycache__` but not the generated artifacts, so they were kept out by explicit staging rather than ignore rules.

### 2026-05-29 — new `trend_state.py` v1.0.0: EMA 20/50/200 trend classifier

Added `resources/trend_state.py` implementing the four-state daily-chart trend
classifier from `strategies-reference/TREND_EMA.md` (user-provided 2026-05-29).
Decision tree: EMA20>EMA50>EMA200 = uptrend; EMA20<EMA50<EMA200 = downtrend;
not-stacked + converging spreads = consolidation; otherwise sideways. Pure-function
core (`classify_trend_ema`, `trend_ema_detail`) + convenience wrappers
(`classify_symbol_trend`, `classify_universe_trends`) that load daily parquet via
`bars_store.load_bars`. Smoke-tested across the full 679-symbol daily universe:
zero unknowns, distribution uptrend 45% / downtrend 31% / sideways 15% /
consolidation 9%.

### 2026-05-29 — new `symbol_ctx.py` + `finviz_screener.py` in-process cache (dashboard efficiency Pass 1 #5 + Pass 2 #1)

User request 2026-05-29: dashboard scan was lagging at 3+ second total per Run-All; audit identified two resource-layer hotspots out of the 10 total wins.

**`symbol_ctx.py` v1.0 (new)** — `SymbolContext` dataclass + `build_context()` factory. Hoists per-symbol shared work (bars load + 4 numpy arrays + EMA × 3 + ATR(14)) out of the 9 DITP detector preludes. Built once per symbol by the dashboard's `_run_all_setups_sync`, then threaded into each detector via the new `ctx: SymbolContext | None = None` kwarg. Detectors that get a `ctx` skip the prelude entirely; when called without it (CLI / backtest paths), they rebuild context internally so the public API is backward compatible. End-to-end impact: 30-symbol scan dropped from 0.3-0.4s to **0.1s** (3-4× faster on the CPU portion).

**`finviz_screener.py` `_MEM_CACHE` (Pass 1 #5)** — added an in-process dict layered on top of the existing on-disk cache. Previously every `_universe_for_setup` call (hot path for every scan, every Scanner-2 ticker reload, every health-pill render) did SHA1 + path stat + file read + JSON parse on the disk cache — ~5-10ms per hit. The memory layer collapses repeated calls to dict lookups; disk hits get promoted into memory on first read. TTL semantics identical to the disk layer; `force_refresh=True` bypasses both.

### 2026-05-28 — `sr_levels.py` v1.5.0: revert single-most-recent-peak coupling (NVDA overlapping setups)

User correction 2026-05-28 from NVDA case: the v1.4.0 unified rule that bound `horizontal_resistance_np` and `find_broken_resistance_below` to the SAME single most-recent peak was over-restrictive. NVDA has $236.54 (8d ago, above current) AND $212.19 (143d ago, below current); both are valid levels (R-above + polarity-flip P3 candidate). The user's framework: *"the pattern for each ticker can be overlapped."*

**Fix**: the two finders are independent again.
- `horizontal_resistance_np` returns the most-recent peak ABOVE current (NVDA: $236.54).
- `find_broken_resistance_below` returns the HIGHEST broken peak BELOW current (NVDA: $212.19).
- Same applies symmetrically to `horizontal_support_np` (most-recent valley below current) and `find_broken_support_above` (lowest broken valley above current).

**AAOI not re-broken**: the v1.4.0 coupling was added to fix AAOI's false-positive P3 at $173.41. v1.5.0 surfaces $173.41 again but the **`strategy/DITP/p3_retest.py` v1.4.0** added a `max_upper_tail_ratio=0.15` filter that rejects AAOI's 46%-upper-tail rejection bar. Downstream detector gates handle the discrimination; sr_levels just surfaces the candidate.

### 2026-05-27 — `sr_levels.py` v1.4.0: unified single-most-recent-peak/valley rule (AAOI fix)

User correction 2026-05-27 from AAOI case: *"again why you look at April 21, the nearest mountain formed was 13.5.2026 at $233.67"*. AAOI had `R above = $191.87` (lowest above current, 17d ago) AND a P3 tag `flip = $173.41` (highest below current, 25d ago). But the **single most-recent mountain in lookback was $233.67** (9 days ago, above current) — that's the only active level. My algorithm was treating both functions independently, which let stale older peaks below current leak into P3 detection even when the *true* active level was a more-recent peak above.

**Unified rule**: there's ONE most-recent peak in the lookback. Its side relative to current price determines which function fires:
- Most-recent peak ABOVE current → `horizontal_resistance_np` returns it (P2 candidate territory); `find_broken_resistance_below` returns `[]`
- Most-recent peak BELOW current (clearly broken by ≥3 ticks) → `find_broken_resistance_below` returns it (P3 polarity-flip candidate); `horizontal_resistance_np` returns `None`
- Most-recent peak BELOW current but within 3 ticks → transitional state, both return empty/None

Same coupling for the valley side: `horizontal_support_np` (P1) vs `find_broken_support_above` (P3a) — only ONE fires based on the most-recent valley's side. The user's mental model: each ticker has ONE active peak setup and ONE active valley setup, mutually exclusive per side.

**Earlier rejected approaches** (documented in module docstring):
- v1.0.0: returned all broken peaks below — surfaced stale lower-mountain P3s.
- v1.2.0: "highest below" selection — picked $173.41 instead of $233.67 for AAOI.
- v1.3.0: "most-recent broken below" alone — still let $173.41 leak in because the cross-side coupling was missing.
- **v1.4.0 (this)**: cross-side coupling. AAOI's $173.41 polarity-flip case correctly suppressed because the *active* level is $233.67 above.

**Smoke-tested impact** (3 user-provided cases):
- USAR: R above $28.69 ✓ / S below $19.36 ✓ / P3 empty ✓
- GOOGL: R above $408.61 ✓ / S below $382.77 ✓ / P3 empty ✓
- AAOI: R above **$233.67** ✓ (was $191.87) / S below $160.10 / P3 empty (was tagged $173.41) ✓

**Universe-wide** (259 symbols): P1 16→6, P3 19→14, P3a 11→7 candidates. The tightened rule drops mis-classifications where stale levels were leaking into the polarity-flip detection.

### 2026-05-27 — `sr_levels.py` v1.3.0: `find_broken_support_above` helper for the short-side P3a

User teaching 2026-05-27: *"P1 and P3 inverse will be P1a and P3a -- which is shorting setup... a successful break below (P2a) which support become a resistance after the break below and price action come back to test the Support turn resistance is P3a setup."*

The short-side P3a (retest of broken support as resistance) needs the mirror of `find_broken_resistance_below`. New function `find_broken_support_above` returns the **immediate-nearest broken mountain valley above current price** (= lowest mountain valley above current that price has clearly broken below by > 3 ticks), or empty list.

Symmetric design:
- `find_broken_resistance_below` → for P3 long-side polarity flip (R → S)
- `find_broken_support_above` → for P3a short-side polarity flip (S → R)

Same parameters (mountain validation gates, tick-tolerance breakdown check), inverted comparisons. Used by `strategy/DITP/p3a_retest.py` v1.0.0 (see `strategy/DITP/README.md`).

### 2026-05-27 — `horizontal_support_np`: most-recent-in-time selection (asymmetric to resistance)

User correction 2026-05-27 from USAR case: *"the support for USAR is the first valley which is 19.5.2026 candle 19.36"*. USAR's swing lows include $19.36 (5 days ago) and $21.46 (19 days ago). My algorithm picked $21.46 because it was higher (closer in price to current $26.55). But $19.36 is the ACTUAL active support — price went BELOW $21.46 to make $19.36, then rallied back above both. $21.46 was "bypassed" when price dipped through it; the rally's structural origin is $19.36.

**The asymmetry between resistance and support is deliberate:**

| Side | Selection rule | Why |
|---|---|---|
| Resistance above | LOWEST mountain top above current | Next ceiling to break. Price hasn't tested it yet; higher mountains above are FUTURE P2 setups. |
| Support below | MOST RECENT mountain valley below current | Where the current rally started. Older swing lows above the most-recent one were bypassed when price went below them — no longer load-bearing. |
| Broken-R polarity flip | HIGHEST broken mountain below current | The most recently broken level in a clean uptrend (each new high breaks the lowest unbroken peak first). |

**Change in `sr_levels.horizontal_support_np`**: selection swapped from `max(mountains_below, key=lambda x: x[1])` (highest level) to `max(mountains_below, key=lambda x: x[0])` (most recent index). Docstring + module-level docstring document the asymmetry.

**USAR re-verification**: S below now correctly returns $19.36 (was $21.46 under the previous symmetric rule). The chart-pane S/R strip shows the user's identified level. Note: USAR is NOT a P1 candidate today because price ($26.55) is 3.03 ATR above its support — far beyond the 1.0-ATR proximity gate. USAR is "structurally anchored to $19.36" but not "actively retesting $19.36" — those are different conditions.

### 2026-05-27 — Cluster tolerance: percentage → absolute ticks (±3 ticks default)

User rule 2026-05-27: *"the placeholder cannot be too wide... plus minus 3 tick."* The previous `cluster_band_pct=0.01` (1% of level) scaled badly with price — for a $400 stock that meant a ±$4 cluster band (400 ticks wide), nothing like the user's ±3-tick (±$0.03) intent.

**API change** (breaking but contained — all in-repo callers updated):
- `patterns.horizontal_resistance_np`, `patterns.window_slopes_np`, `sr_levels.horizontal_support_np` — parameter `cluster_band_pct: float = 0.01` REPLACED with `tick_size: float = 0.01` + `cluster_tolerance_ticks: int = 3`. Cluster band is computed as `cluster_tolerance_ticks × tick_size` (absolute, default ±$0.03).
- All callers (`strategy/DITP/scanner.py::P2Config`, `strategy/DITP/p1_rebound.py::P1RebConfig`, `find_key_levels`) updated to pass the new parameters.
- The `cluster_touches` and `mountain_anchors` output fields now count touches within ±3 ticks of the chosen level (tight placeholder). Previously they counted touches within ±1% (loose zone).

**Impact**:
- GOOGL: unchanged — still detects S=$382.77 as P1 support, mountain_anchors=1 (only the precise level itself qualifies in a ±$0.03 band).
- Universe-wide P1/P3 candidate COUNTS are unchanged (16 P1, 19 P3) because the detectors pick the immediate-nearest mountain and `min_touches=1` allows single-point levels. But mountain_anchors counts are TIGHTER — AME at $227.95 previously showed 4 anchors (multi-touches within ±$2.27), now shows 1 (only touches within ±$0.03). This is the user's intended "tight placeholder" semantic.
- Scoring downstream: P1/P3 score weights validation (mountain_anchors × 10 + cluster_touches × 2), so scores for previously-multi-anchor levels naturally drop. The ranking ORDER is reshuffled but the candidate SET is preserved.

**Why absolute ticks, not percentage** (deliberate exception to CLAUDE.md's ATR-relative rule): noise around a precise level is dominated by minimum-tick increments, not by ATR-volatility. A $0.03 noise band is the right semantic for "is this touch at THE placeholder" — and it doesn't bloat into a $4 band for high-priced stocks. The range_pct parameter (default 2%) still scales by price for the ZONE width.

### 2026-05-27 — Mountain validation defaults relaxed: `mountain_min_age_bars` 15→5, `mountain_pullback_atr` 2.0→0.5

User teaching from GOOGL case 2026-05-27: GOOGL closed $388.88 with today's low $382.60, bouncing off the $382.77 valley (May 12 pin bar). The valley level was previously $384 resistance (broken on Apr 30 = P2), retested on May 12 (P3 polarity flip), now being tested again as support (P1). Same level evolves P2 → P3 → P1 over time as price interacts with it.

The strict mountain criteria (`15 days old, 2.0×ATR pullback`) were inherited from the original DITP P2 scanner — designed to filter historical structural levels. They're too tight for "actively-tested" levels where price hasn't yet had a deep pullback between visits. GOOGL's pin-bar valley is only 9 trading days old with a 2.68-ATR rally since — well within the chart-reading definition of "valid support being tested" but blocked by the strict gates.

**Relaxed module-level defaults** in `resources/patterns.horizontal_resistance_np`, `resources/sr_levels.horizontal_support_np`, and `resources/sr_levels.find_broken_resistance_below`:
- `mountain_min_age_bars`: 15 → **5** (just above the swing_radius=3 floor — swings of 5+ days have at least 2 days of post-swing confirmation beyond the radius)
- `mountain_pullback_atr`: 2.0 → **0.5** (any meaningful rejection counts as structural validation; the previous 2.0 was tuned for "deep structural pullback")

**Dual-default tradeoff documented**:
- New permissive defaults apply to: `sr_levels.find_key_levels` (chart pane S/R strip), `strategy/DITP/p1_rebound.py` (P1RebConfig defaults overridden to 5/0.5), `strategy/DITP/p3_retest.py` (P3RetestConfig overridden to 5/0.5).
- Legacy strict tuning preserved in: `strategy/DITP/scanner.py::P2Config` (`mountain_min_age_bars=15, mountain_pullback_atr=2.0`). P2 watchlist generation is unchanged — its strict gates have been tuned against the historical-watchlist quality bar.

**Smoke-tested impact**:
- GOOGL via `/chart/sr_levels`: S below now correctly shows **$382.77** (was $331.35 under old gates). R above $408.61. P1 detector confirms GOOGL as a candidate with today's low $382.60 touching the support, bounce magnitude 0.65 ATR, score 30.
- Parquet universe (241): P1 candidates 10 → **16** (additional names like BFS, AVNS, CARR — multi-mountain-anchor supports). P3 candidates 10 → **19** (AA broke just 7 days ago, AVT 7d, AMD 10d — recent breakout-retests that the previous 15-day age gate filtered out).
- All new candidates retain bounce-magnitude ≥0.3 ATR (the reaction-magnitude gate from v1.1.0 remains the quality filter).

### 2026-05-27 — `sr_levels.py` v1.2.0 + `patterns.horizontal_resistance_np`: framework reintegration — immediate-nearest-in-price selection

User framework reintegration 2026-05-27 (USAR re-examination): *"the immediate mountain top nearest to the current price action is relevant because that will be the nearest resistance it has to break (P2 Setup). The Second and Third mountain top is less relevant in the price action because the current price action has not reach the price level yet to test that resistance, but when its come near that level, it will become another new P2 setup. So on and so forth..."*

This reframes how levels are selected for P1/P2/P3. Each mountain peak is an INDEPENDENT P2 → P3 lifecycle. The relevant level at any moment is the one **closest in price to current** — not the most-recent-in-time, not the absolute-highest, not a multi-touch cluster requirement.

**Three coordinated changes:**

1. `patterns.horizontal_resistance_np` — selection rule changed from "most recent in time among mountains above current" to "**LOWEST mountain above current**" (= immediate nearest above). The TSLA-style ceiling gate (`max_below_window_high_pct`) default raised from `0.02` → `1.0` (effectively disabled). The trend-discrimination this gate provided is now redundant with the EMA-stack gate in P1/P2/P3.

2. `sr_levels.horizontal_support_np` — selection rule changed from "most recent in time among valleys below current" to "**HIGHEST mountain valley below current**" (= immediate nearest below). Symmetric mirror of the resistance change.

3. `sr_levels.find_broken_resistance_below` — reverted v1.1.0's overly-restrictive "absolute highest mountain must be broken" gate. Now returns the **IMMEDIATE NEAREST mountain BELOW current** (highest mountain below) that's clearly broken above (still 3-tick tolerance). The v1.1.0 fix was over-correcting USAR — blocking valid P3 candidates whenever some old historical peak loomed unbroken.

**Cluster gate relaxed**: `min_touches` default lowered from 2 → 1 in both resistance and support finders. A single confirmed mountain top is a valid resistance even without a second touch nearby — matches the user's visual chart-reading. The mountain validation (age + 2×ATR pullback) already provides the structural credential. Multi-touch confirmation still surfaces via `cluster_touches`.

**Smoke-tested impact**:
- USAR via `/chart/sr_levels`: now correctly shows immediate-nearest R above = **$32.07** (the next structural mountain after the v1.0.0 false-positive $25.95 and the v1.1.0 absolute-highest $43.98), S below = $21.46, broken-R retest = $26.36 (77d ago, outside P3 staleness so no false P3). Note: user's visually-identified $28.69 peak doesn't satisfy the strict `mountain_pullback_atr=2.0` gate (USAR only dipped ~1.5×ATR after that peak) so the algorithm falls through to $32.07 — a known knob the user can relax if they want shallower peaks to count as structural mountains.
- Live Finviz scan: P3 0 candidates (USAR clean), P1 2 candidates (TSLA, C).
- Parquet universe (241 tickers): P1 candidates 3 → **10** (multi-touch supports like AME with 4 mountain anchors now surface), P3 candidates 4 → **10** (more polarity-flip retests visible). Quality bar held by the bounce-magnitude gate (≥0.3 ATR).

### 2026-05-27 — `sr_levels.py` v1.1.0: `find_broken_resistance_below` enforces "highest mountain" + 3-tick breakout tolerance

User correction 2026-05-27 from the USAR case: the v1.0.0 implementation tagged USAR as P3 because it found a $25.95 mountain peak below the current $27.73 close. But USAR's actual structural ceiling was higher (~$28.69 by the user's chart read, or $43.98 historically — either way, well above current price). Price hasn't broken THE key resistance yet, so USAR is a **P2 pending-breakout**, not a P3 retest. The detector was picking minor crossed peaks instead of the structural ceiling.

**Fix**: `find_broken_resistance_below` now returns at most ONE level — the **HIGHEST** mountain peak in the lookback — and only when current price is clearly above it. "Clearly" = `current_price > highest_level + breakout_ticks * tick_size` (default 3 ticks × $0.01 = $0.03). If the highest mountain is still at-or-above current price within that noise band, the function returns `[]` (no P3).

**Why an absolute tick threshold (not ATR-relative)?** CLAUDE.md's normalization rule says thresholds should be ATR-relative. The breakout test is a deliberate exception: it's a noise-suppression check (is the breakout *unambiguous*?), not a setup-tightness check (is the entry tight enough?). 3 ticks of noise is roughly constant across the price-range of liquid US equities; ATR-relative noise would be too generous for high-ATR names.

**Removed**: the `dedup_pct` and `max_results` params (no longer relevant since we return at most 1 level). Added `tick_size` (default 0.01) and `breakout_ticks` (default 3).

**Smoke-tested impact**:
- USAR: previously tagged P3 with `flip=$25.95`. Now correctly drops out (highest mountain $43.98 vs close $27.73 → not broken).
- Live Finviz scan (44 tickers): P3 candidates dropped from 1 (USAR-only false positive) to 0.
- Parquet universe scan (241 tickers): P3 candidates dropped from 11 to **4** — AOSL, BNL, ATEN, ALGM — all genuine breakout-then-retest setups where the highest mountain was actually crossed.

### 2026-05-27 — `sr_levels.py`: lookback extended to 1 year (252 trading days) per user rule

User rule 2026-05-27: *"when you look at Support and Resistance on a daily chart, you will look at 1 year daily chart to look at valley and mountains."* Initial implementation used 120-day support/resistance and 180-day broken-resistance lookbacks (heuristic defaults). Bumped all three lookbacks in `find_key_levels` to 252 (matches "1 year of trading days"); raised minimum bar requirement from 50 to 252+14 (ATR-warmup buffer); added the rationale to the module docstring as a callable reference for downstream callers.

Effect on existing levels (smoke-tested):
- AAPL: support_below validation strengthened from 2 touches / 2 mountains → **5 touches / 5 mountains** at $265.07 (the wider window picked up older touches at the same level).
- ABBV: P3 retest list grew from 2 to 3 candidates; the closest-in-time retest ($214.87, 16 days ago) remained the top pick, with deeper-history flips at $212.45 (189d) and $197.50 (219d) now visible.
- AAPL P3: same 3 candidates as before, but they're at 73/97/117 days ago — within the new window, out of the old one.

The same rule was propagated to `strategy/DITP/scanner.py` (P2 resistance_lookback 90 → 252), `strategy/DITP/p1_rebound.py` (support_lookback 120 → 252), and `strategy/DITP/p3_retest.py` (lookback 180 → 252) so all four S/R-anchored detectors agree on the lookback window. yFinance fetch defaults in `dashboard/server.py` bumped from 400 → 500 calendar days so 252-bar histories arrive with comfortable headroom (~355 trading days delivered vs 266 needed).

### 2026-05-27 — `sr_levels.py`: symmetric S/R + broken-resistance detector

User question 2026-05-27: *"would you be able to identify key support and resistance we have a pattern recognition in the resources?"* — followed by the framing of the three DITP setups (P1 = rebound off support, P2 = breakout to resistance, P3 = retest of broken resistance now as support). The existing `patterns.horizontal_resistance_np` already handled P2; what was missing was the symmetric support detector and a broken-resistance scanner for P3.

`horizontal_support_np` is a deliberate mirror of the resistance function — swing LOWS instead of highs, mountain-valley filter instead of mountain-top (old swing low followed by a rally back up by N×ATR). One deliberate asymmetry: **no floor gate.** The resistance function rejects mid-downtrend bounces by demanding the chosen level be the highest mountain. The naive symmetric mirror would demand the lowest mountain valley — but for the only case where P1 rebound is meaningful (uptrend), the lowest valley is typically 20-30% below current price from early in the lookback, and would reject the recent pullback low that the user actually cares about. So the closest-in-time mountain valley below current wins, with the cluster gate (≥2 swing lows within 1%) as the only hard filter.

`find_broken_resistance_below` enumerates mountain-anchored swing highs in a 180-bar lookback where price is now strictly above them. Dedups by 1% bands, returns the top 3 closest-to-current first — that's the P3 retest candidate list.

`find_key_levels(symbol)` is the one-stop API consumed by the dashboard. Returns `{current, atr14, resistance_above, support_below, broken_resistance:[...]}`. Bars come from `bars_store.load_bars()`, which the dashboard endpoint monkey-patches to read fresh yFinance bars instead of parquets (consistent with the user's "parquet is for backtest only" rule for the dashboard side).

Smoke-tested on six laptop symbols: AAPL at $308.82 finds support at $265.07 (-7.35 ATR, structurally distant — AAPL is at ATHs); ABBV at $215.70 finds two P3 retest candidates at $214.87 (-0.16 ATR, 16 bars ago) and $212.35 (-0.63 ATR, 25 bars ago) — exactly the polarity-flip retest pattern the user described.

### 2026-05-27 - `finviz_screener.py`: add `fetch_screener_rows()` returning {symbol, price, volume}

The single-purpose `fetch_screener_symbols()` was enough when the dashboard fed the symbol list into the DITP detector. After the user reframed the workflow to step-1=pull-Finviz, step-2=apply-setup-manually-later (see dashboard/README.md changelog), the Scanner view itself wanted to SHOW the Finviz table, which means each row needs price + volume too.

`fetch_screener_rows()` mirrors the existing function's pagination + caching contract but parses the TS comment block (`<!-- TS\nSYM|PRICE|VOLUME\n... -->`) at the bottom of each screener page. That block is server-side-rendered with no JS dependency, identical structure across all `v=` views, and immune to the `class=` reshuffles Finviz periodically applies to its visible table markup. Strictly more robust than walking `<td>` cells.

Cache file is suffixed `_rows.json` to coexist with the symbols-only cache -- callers that want one or the other don't collide.

### 2026-05-27 - `finviz_screener.py`: Finviz screener URL → universe for the dashboard scan

User directive 2026-05-27: *"perhaps we store the finviz criteria in the scanner setting, if we want to change anything we can just change the URL"*. Built a small scraper module that takes a Finviz screener URL and returns the symbol list. Stored on disk per-URL (sha1-keyed cache) with 1h TTL so repeated scans don't hammer Finviz.

Parsing strategy: each ticker row has a `data-boxover-ticker="SYM"` attribute (drives the hover tooltip on the screener page). It appears twice per row (left + right cells), so we dedup by first-seen-wins. Picked this over the older `screener-link-primary` class regex because Finviz has reshuffled link `class=` names multiple times in recent years but the boxover attribute has been consistent.

Pagination: appends `&r=<offset>` with offset 1, 21, 41, ... Stops when a page returns zero NEW symbols (works as both "Finviz looped back to page 1" and "no more rows" sentinel). 1s sleep between pages as courtesy rate limit. Caps at 20 pages = 400 symbols, plenty of headroom for the user's mid-cap+ filter (~40-300 typical results).

Empty-list semantics: any fetch failure or zero-match result returns `[]`, which the caller (`dashboard/server.py::_universe_for_setup`) treats as "fall back to SP500" -- the dashboard never scans an empty universe by accident.

Smoke-tested against the user's actual screener URL (cap_midover, geo_usa, ta_averagetruerange_o2, ta_beta_o1, ta_volatility_2tox2to, sh_price_o20, sh_avgvol_10000to, sort -volume): 43 symbols returned across 3 pages (matching the "Total: 43" badge Finviz shows), cached in ~0ms on the second call.

### 2026-05-27 - `yf_daily_bars.py`: in-memory yFinance adapter for the dashboard

User architectural directive 2026-05-27: *"the parquet store i intend to use it for backtesting only, this scanning of daily setup through yfinance only"*. The dashboard's Scanner view needed a way to fetch fresh daily bars on demand without going through `bars_store` (which is the parquet I/O layer).

`yf_daily_bars.fetch_daily_batch(symbols, lookback_days=400)` does one batched `yfinance.download(... group_by="ticker", threads=True)` call and returns `{SYMBOL: [{t,o,h,l,c,v}, ...]}` -- the same shape `bars_store.load_bars` produces, so the dashboard's `/scanner/yf_scan` endpoint can monkey-patch `bars_store.load_bars` to return from this in-memory cache while the existing DITP `scan_universe()` runs unchanged.

Why not extend `yfinance_history.py`? That module writes parquets -- exactly what the user wants to keep separated. `yf_daily_bars.py` is the in-memory sibling: same data source, different output destination (RAM vs disk).

Internal contract notes worth remembering:
- yFinance returns 2-level multi-index DataFrames even for single-symbol fetches when `group_by="ticker"` is set. We always pass that flag so the per-symbol slice (`df[ticker]`) works in both single and multi cases.
- `BRK.B` is converted to `BRK-B` for the yFinance call and converted back on the return path so callers see the canonical dotted form (matching `sp500.py`'s output and what the journal stores).
- 400-day default lookback covers ~260 trading days, enough for EMA200 stabilization + the DITP P2 detector's 90-bar resistance window + flush-up scan headroom.
- Smoke-tested via CLI: `py resources/yf_daily_bars.py AAPL MSFT --lookback 30` returns 30 bars per symbol with today ET as the latest timestamp.

### 2026-05-26 — `HISTORY_CLIENT_ID` now config-driven (`cfg["ibkr_history_client_id"]`)

Sequel to the connection-failure debug session that produced c3f6dd6. After the FAILED line started landing in Hermes's watcher log, the actual error was visible: `Could not connect to IB Gateway at 127.0.0.1:4002 (clientId=83): . Checks: ...` — empty exception body (= 8s TimeoutError from ib_insync) plus IBC's log showing `remove Client 83` repeating 15+ times. Diagnosis: BOTH the laptop and Hermes were trying to connect on the hardcoded `HISTORY_CLIENT_ID = 83`, so IBKR's server-side session table treated every new attempt as a duplicate and kicked it.

Fix: `ibkr_history._connect` now reads `cfg["ibkr_history_client_id"]` (per-machine, in the gitignored `config.json`), falling back to the legacy `HISTORY_CLIENT_ID = 83` for back-compat when the key is absent. Per CLAUDE.md's allocation table:
- Laptop: `"ibkr_history_client_id": 83` (the default; explicit is good practice)
- Hermes: `"ibkr_history_client_id": 84`

`config.example.json` documents the key with a `_comment_*` explaining the trap (symptom: `remove Client N` in IBC logs + 12s crash-loops). Both machines must be on distinct ids if they ever run ingest concurrently.

Smoke-tested: stubbed `ibkr_data._connect` to capture cfg; verified the override correctly propagates 83 (default), 84, 85 from cfg through `_connect` to the underlying `_base_connect`.

This doesn't fix the immediate Hermes situation by itself — the user also needs to update Hermes's `config.json` to add `"ibkr_history_client_id": 84` so the next iteration connects on 84 instead of the colliding 83.

### 2026-05-26 — Fix: `ibkr_data._connect` raises ConnectionError instead of `sys.exit`

Follow-up to 92eed6b. After that fix landed, Hermes's supervisor log showed iterations now succeeding through the pre-flight (in ~2s with the new fast metadata path), but still crashing with `code=1` after exactly 12 seconds — and the watcher log still ended at `[pre-flight] N unique symbols need work` with no `FAILED:` line.

Root cause: `ibkr_data._connect` called `sys.exit(diagnostic)` when `ib.connect()` failed. `sys.exit` raises `SystemExit`, which is a `BaseException` — NOT an `Exception` — so `wait_and_ingest`'s `try: bulk_update(...) except Exception:` guard didn't catch it. The watcher exited cleanly with the helpful diagnostic going to stderr (discarded by Task Scheduler on Hermes), leaving no breadcrumb in the watcher log file.

Fix: both `_check_lib` and `_connect` now raise (`ImportError` and `ConnectionError` respectively) instead of calling `sys.exit`. The diagnostic message is preserved in the exception's args. Verified by smoke test: connecting to a closed port now raises `ConnectionError`, not `SystemExit`. Orchestrator dry-run still passes.

Net effect on Hermes: when Gateway is unreachable, the next iteration's watcher log will land a `FAILED: ConnectionError('Could not connect to IB Gateway at 127.0.0.1:4002 (clientId=84): [WinError 1225] ...')` line that points directly at the operational issue, instead of crashing silently.

The companion `scripts/_common.py::_try_ibkr` wrapper (which catches both Exception AND SystemExit for the data-provider fallback path) was unaffected — it kept SystemExit handling for any legacy paths but now hits the Exception branch for `_connect` failures. Comment updated to reflect the new reality.

This change doesn't make Gateway reachable — it only makes the failure visible in the watcher log. Operational follow-up (restart Gateway, check clientId 84 availability, verify port 4002 listening) is independent and still required.

### 2026-05-26 — Fix: pre-flight crash on Hermes + 1000x faster metadata scan

Hermes supervisor was reporting `watcher exited (code=1) after 2.2m` in a tight loop, with the watcher log going silent after `starting bulk_update ...` and no `FAILED:` line. Root cause: `_classify_pair` calls `bars_store.available_range`, which materializes every row of every parquet just to extract first/last timestamp. With 1518 syms × 3 tf = 4554 reads over Resilio-synced storage, the scan took minutes. **Worse**, the pre-flight loop had no try/except around `_classify_pair`, so a single corrupted parquet (mid-Resilio-sync or otherwise) crashed the entire pre-flight before any `_emit` could fire — hence the silent crash with no FAILED line.

Three fixes:

1. **`bars_store.available_range_fast(symbol, *, timeframe)`** — reads the parquet's per-row-group min/max statistics for the `t` column instead of materializing rows. Pyarrow records these stats by default on write, so existing files Just Work. Measured: 1083× speedup for a 730-row daily parquet (1627ms → 1.5ms). Across the full 4554-pair scan: ~12s vs the previous several-minute crawl. All exceptions internal to the function (corrupted parquet, partial Resilio sync, schema drift) return `None`, never raise.
2. **`_classify_pair` uses the fast path** + treats `None` from a non-existent parquet as `seed`, from an existing but unreadable file as `unreadable` (→ refill on the IBKR side).
3. **Pre-flight loop wraps `_classify_pair` in try/except** with belt-and-braces error counting. A bad parquet now logs one line (up to 5 verbose, then a suppressed-count footer) and gets classified as `unreadable` instead of crashing the watcher. Also emits a `[pre-flight] scanned N/M (X%)` progress line every 500 pairs so silence ≠ stuck.

Subtle bug encountered + fixed in development: my first cut of `available_range_fast` used `meta.schema.num_columns` + `meta.schema.column(i).name`, which doesn't exist on `pyarrow._parquet.ParquetSchema` (it has `.names` — a list). The `AttributeError` got swallowed by the function's outer `except Exception: return None`, so every call silently returned `None` and every symbol would have classified as `seed` (full re-fetch). Caught by the smoke test (`slow != fast`). Now uses `meta.schema.names.index("t")`.

Smoke-tested end-to-end on the laptop's 74-symbol parquet set: full pre-flight on 222 pairs in 0.58 seconds, returns clean 6-line summary, `_emit` flushes through `log_callback` correctly, `bulk_update` returns `{}` without touching IBKR when `n_work == 0`.

### 2026-05-26 — `ibkr_history.bulk_update`: `log_callback` + `unique symbols need work` line

Follow-up to today's pre-flight refactor (entry below). Two problems surfaced after the user pulled the change on Hermes:

1. On Hermes the supervisor runs under Windows Task Scheduler, which discards the child process's stdout/stderr. The pre-flight summary lines I'd added (`sys.stdout.write(...)`) therefore vanished — none of them landed in the watcher's `_ingest_*.log` file, and the dashboard tray couldn't see them.
2. The tray's denominator was still the universe size (1518), so a watcher that correctly skipped 1471 already-deep symbols still displayed "1 / 1518 (0.1%)" — looked like nothing was happening.

Fix:

- New `log_callback` parameter on `bulk_update`. When set, **every** output line — pre-flight summary, per-iteration progress, reconnect notices, per-symbol failures — is routed through the callback. `scripts/wait_and_ingest.py` now passes `log_callback=log` so all those lines land in the watcher log file (and via the file-logger's `print()`, also on stdout where available).
- Default `log_callback=None` preserves the existing direct-stdout behaviour for the orchestrator's post-EOD path.
- Pre-flight emits a new tray-friendly line: `[pre-flight] N unique symbols need work`. That count is the right denominator for the tray's "unique symbols touched" numerator (vs. the existing `N work items remaining` which counts pair-iterations and overcounts when a symbol has 2-3 shallow timeframes).
- `dashboard/tray_status.py` reads the new line and uses it as the denominator when present.

Smoke-tested end-to-end on the laptop: captured 6 pre-flight log lines via callback, wrote them to a synthetic watcher log, the tray parser extracted the count correctly with mtime-keyed caching.

### 2026-05-26 — `ibkr_history.bulk_update`: pre-flight scan + `skip_up_to_date` for fast restart

User flagged: *"when the ingest restart it always start from the A, i want it to have a log to confirm the which has been done and which now so i can save a lot of time"*. Before this change, `bulk_update` looped all 1518 symbols × 3 timeframes paying the full 7s pacing per pair on every restart — even for symbols already at full depth. With three timeframes that's 4554 pairs × 7s ≈ 9 hours of pacing alone before any new work happens.

Two changes:

1. **Pre-flight pass (no IBKR calls).** New `_classify_pair(sym, tf, target_days, force_seed)` walks each pair locally and returns one of `seed` / `force` / `refill` / `top_up` / `unreadable` based purely on what's on disk. Before the IBKR connection is even opened, `bulk_update` logs:
   - the totals by action (`seed=N force=N refill=N top_up=N unreadable=N`)
   - per-timeframe completion (`3min=1234/1518 5min=1234/1518 daily=1234/1518`)
   - the estimated minutes saved by skipping top_up pairs
   - the count of remaining work items

   This is what the user wanted: a glance at the top of the log answers "which has been done" without RDP-ing in or scrolling.

2. **`skip_up_to_date` parameter (default `False`).** When `True`, the work loop skips `top_up` pairs entirely — no pacing, no IBKR call. The watcher's purpose is bulk backfill, not incremental top-up (that's the orchestrator's post-EOD job), so `scripts/wait_and_ingest.py` now passes `skip_up_to_date=True`. The orchestrator's post-EOD ingest keeps the default `False` since it explicitly wants today's incremental bars.

3. **Per-iteration progress counter.** Each work-item log line is now prefixed with `[i/N]` so progress is visible at a glance instead of inferred from how far down the alphabet the symbols are.

Sample of the new log shape:

```
[pre-flight] 4554 (sym,tf) pairs scanned: seed=0 force=0 refill=120 top_up=4374 unreadable=60
[pre-flight] skipping 4374 top_up pairs already at full depth (skip_up_to_date=True). Estimated time saved: ~510.3 min of pacing.
[pre-flight] per-timeframe completion: 3min=1456/1518 5min=1462/1518 daily=1456/1518
[pre-flight] 180 work items remaining
  [1/180]      NVDA     3min   refill(depth=14d<180d)   +12345 bars
  [2/180]      TSLA     3min   refill(depth=14d<180d)   +12340 bars
  ...
```

No behaviour change for the orchestrator's existing post-EOD ingest (default `skip_up_to_date=False`). The watcher's behaviour change is the intended user-facing fix.

### 2026-05-26 — `market_calendar.py`: NYSE full-closure helpers (skip US holidays, not just weekends)

User rule (chat 2026-05-26): *"when scanning for the tickers we will be looking at last trading day setup, skip the holiday"*. Found because the DITP P2 scanner's `next_trading_day_iso` (and the brand-new TC scanner's `_next_business_day`) only skipped Sat/Sun, not US market holidays. Running EOD Fri 2026-05-22, the P2 scanner had written `watchlist_ditp_2026-05-25.json` (Memorial Day Mon) — a non-trading day with no daily bars — and TC scanner consuming that file found zero data to work with.

The fix is a single source of truth for "is this a trading day?", reusable by every future scanner / scheduler.

- **`_NYSE_FULL_CLOSURES_RAW`** — explicit list of `(year, month, day)` tuples for full NYSE closures 2024–2028. Includes observed dates (Saturday holidays → preceding Friday; Sunday → following Monday; except New Year's Day which is not observed when on Saturday). Source: https://www.nyse.com/markets/hours-calendars. **Update annually.**
- **`is_trading_day(d)`** — `True` iff `d` is Mon-Fri AND not in the closure set.
- **`last_trading_day(d=today)`** — walk backward (inclusive of `d`) until a trading day; bounded at 14 days to surface bugs in the holiday list.
- **`next_trading_day(d=today)`** — walk forward strictly after `d` until a trading day.
- `KNOWN_YEAR_RANGE = (2024, 2028)` — outside this range, holiday data is missing and the CLI warns; weekday-only fallback still applies.
- CLI: `py resources/market_calendar.py --date 2026-05-25` prints `is_trading_day=False`, `last=2026-05-22`, `next=2026-05-26`. `--list-year 2026` prints the year's full-closure list.

Consumers wired in the same turn:
- `strategy/DITP/scanner.py::next_trading_day_iso` — was Mon-Fri-only, now delegates to `next_trading_day`. Effect: EOD Friday-before-Memorial-Day writes a Tuesday-targeted watchlist (correct), not a Monday-targeted one.
- `strategy/DITP/tc_scanner.py` — uses `is_trading_day` + `last_trading_day` + `next_trading_day`. When the consumed P2 watchlist's `target_date` is a holiday (legacy file pattern), TC walks source_date back to the last actual trading day and emits a `# note:` to stdout.

### 2026-05-26 — `universe_full.txt`: static 1518-symbol universe for pure-IBKR runs

- User rule: *"make sure the code only seed from IBKR, nothing outside IBKR"*. The previous launch flow on Hermes had a bootstrap problem — `bars_store.list_symbols('daily')` returns empty when the folder is wiped, so we needed yfinance to pre-seed daily parquets just so the watcher had a universe to iterate. That mixed sources.
- **Pre-generated static universe file** at `resources/universe_full.txt` — 1518 unique symbols, the deduplicated union of S&P 500 (503) + S&P MidCap 400 (400) + S&P SmallCap 600 (603) + NASDAQ-100 (101) + DJIA (30). Generated on a host that has Wikipedia access (laptop), committed to git so Hermes gets it via `git pull`. Reserved Windows names already filtered.
- Paired with `scripts/wait_and_ingest.py --symbols-file <path>` (added same day) so the watcher reads the universe from this text file instead of from existing parquets or Wikipedia-scraping at runtime.
- **Now zero non-IBKR data sources** end-to-end on Hermes: universe comes from a static checked-in text file (just ticker symbols, not OHLCV); every bar of price data comes from IBKR.
- Regenerate by running the same one-liner on the laptop (Wikipedia caches in `state/cache/`):

  ```python
  py -3.12 -c "
  import sys; sys.path.insert(0, 'resources')
  from sp500 import get_sp500_symbols; from sp_midcap400 import get_sp400_symbols
  from sp_smallcap600 import get_sp600_symbols; from nasdaq100 import get_nasdaq100_symbols
  from djia import get_djia_symbols
  RESERVED = {'CON','PRN','AUX','NUL','COM1','COM2','COM3','COM4','COM5','COM6','COM7','COM8','COM9','LPT1','LPT2','LPT3','LPT4','LPT5','LPT6','LPT7','LPT8','LPT9'}
  u = sorted(set(get_sp500_symbols()) | set(get_sp400_symbols()) | set(get_sp600_symbols()) | set(get_nasdaq100_symbols()) | set(get_djia_symbols()) - RESERVED)
  open('resources/universe_full.txt','w').write('\n'.join(u) + '\n')
  print(len(u), 'symbols')
  "
  ```

  Commit the regenerated file when the S&P committee changes membership.

### 2026-05-26 — `bulk_update`: smart-resume for partial seeds (no more need for `--force-seed`)

- User rule: *"seed it without interuption and if IBKR reset in Hermes for whatever reason we continue with the seeding from where it left off"*.
- Previously the per-(sym, tf) mode decision was binary:
  - `available_range == None` → full ingest
  - `available_range != None` → incremental update only (forward gap fill)
  - `force_seed=True` → override to full ingest regardless
- That missed a critical resume case: **a parquet that exists but only has, say, 30 days of the targeted 180 days** (crash mid-symbol). The old logic took that as "exists, just update forward" — leaving the 150-day historical gap unfilled forever.
- New ternary logic — `bulk_update` now checks earliest bar age vs target:
  - No data → full seed
  - Has data, age of earliest ≥ 90% of target days → incremental update (`update(depth=Nd ok)`)
  - Has data, age of earliest < 90% of target days → partial seed detected → full re-fetch to target depth (`refill(depth=30d<180d)`)
  - `force_seed=True` still forces full re-fetch unconditionally (`force-seed(180d)`)
- bars_store deduplicates on write, so re-fetching over existing partial chunks is safe — no data corruption, just network/time cost.
- **Practical implication**: drop `--force-seed` from the launch. The first pass seeds everything; if the watcher crashes / Hermes reboots / IBKR resets, the supervisor relaunches and the script naturally resumes — already-completed symbols are top-up only, partial symbols get re-filled to the right depth, untouched symbols get fresh seed. **True idempotent resume.**

### 2026-05-26 — `bulk_update`: per-timeframe lookback override (`lookback_days_by_tf`)

- User rule: *"I will need 1d (2 years), 3m and 5m for 180 days"*. Previously `lookback_days_for_new` was a single int applied to every timeframe in the run — fine for "everything 180 days", impossible for "daily 2 years AND intraday 180 days" in one launch.
- Added optional `lookback_days_by_tf: dict[str, int] | None` kwarg. When set, overrides `lookback_days_for_new` per timeframe. Any timeframe not in the dict falls back to `lookback_days_for_new`. Backward-compatible — existing callers passing only `lookback_days_for_new` work unchanged.
- Paired with `scripts/wait_and_ingest.py` accepting `--timeframes "3min:180,5min:180,daily:730"` — each entry can carry its own depth via `TF:DAYS` syntax.

### 2026-05-26 — `ibkr_history.bulk_update`: per-symbol try/except so one bad ticker can't kill a 1519-symbol run

- User rule: *"i prefer to run it without interruption"*. Previously, an unhandled exception inside `ingest_history()` or `update_history()` would propagate all the way up to `bulk_update`'s outer `try/finally` and abort the entire run. We hit this during the 180d re-seed when delisted symbols + transient IBKR errors caused the watcher to silently die mid-loop.
- **Per-symbol shielding**: each `(symbol, timeframe)` iteration now sits inside its own `try/except`. Any exception (delisted contract, weird IBKR error code, parquet write failure, transient network blip the reconnect handler can't catch) gets compactly logged as `SYM 3min FAILED: <ExceptionType>: <first 80 chars>` and the loop moves on to the next symbol. Failed (sym, tf) pairs record `0` bars in the results dict.
- **Reconnect failures still break the loop** — those come from `_ensure_connected` running out of retry budget (20 attempts), which means Gateway is genuinely unreachable. No point continuing.
- **What this does NOT fix**: process-level death (crash, OOM, killed by `Stop-Process`). For that, use `scripts/Watch-Ingest.ps1` (added in the same commit) — a PowerShell supervisor that relaunches the watcher on any exit.

### 2026-05-24 — `ibkr_history.py`: `--force-seed` flag for backward-extending stored history

- Use case (chat 2026-05-24): backtester needs 180 days of 3min depth, but the existing universe already has 14-day parquets — `update --universe` only fills the forward gap (incremental), it does NOT extend backward. To bump depth from 14d → 180d we need a flag that bypasses the "incremental if existing" logic and runs `ingest_history(lookback_days=180)` on EVERY symbol.
- New `--force-seed` flag on the `update` subcommand. When set, `bulk_update()` ignores `bars_store.available_range()` and always calls `ingest_history` with `--seed-days` of lookback. Status line shows `force-seed(180d)` (vs the regular `seed(60d)` or `update`) so the operator can see which path each symbol took.
- Safe by construction: `bars_store.write_bars()` deduplicates on timestamp, so re-fetching the recent window that already exists is wasted bandwidth but produces no data corruption / duplicates.
- Wall-clock cost example: 1518 symbols × 180-day 3min = 13 chunks/symbol × ~15s/chunk = ~80-100 hours (~4 days continuous). Mitigated by the auto-reconnect logic added earlier today — long runs survive TWS hiccups without operator intervention.
- CLI: `py resources/ibkr_history.py update --universe --timeframes 3min --seed-days 180 --force-seed`

### 2026-05-24 — `ibkr_history.py`: auto-reconnect on TWS drops during bulk_update

- User report (chat 2026-05-23): the bulk-update ingest would silently spin on `qualify_failed: Not connected` after TWS auto-logoff or a brief network blip, forcing a manual restart of the ingest each time.
- New `_ensure_connected(current_ib)` helper inside `bulk_update()`. Called once per (symbol, timeframe) request — checks `ib.isConnected()`, and if dropped, sleeps `min(60, 5 × attempt#)` seconds, disconnects the dead socket, then re-runs `_connect(cfg)`. Resets the attempt counter on first successful pass so transient drops don't accumulate toward the cap.
- `MAX_RECONNECT_ATTEMPTS = 20` — if TWS is genuinely down (e.g. user shut down the gateway), the run aborts cleanly with a stderr message rather than spinning indefinitely.
- Failed-reconnect paths break the per-symbol loop and propagate to the outer try/finally so the IB instance is always disconnected cleanly even when reconnects fail. No data corruption — `bars_store.write_bars()` is idempotent on the partial pull that completed before the drop.
- Validated during the active 1518-symbol × 3min ingest that's been running across the prior day's session — multiple TWS auto-restarts were absorbed without operator intervention.

### 2026-05-23 — `ticker_profile.py`: bulk-refresh helper + health summary (dashboard integration)
- Following the storage refactor below, two new module-level helpers were added so the dashboard can drive profile state without needing a per-ticker loop in the server:
  - **`refresh_many(tickers, *, pacing_s=0.5, on_progress=None)`** — refresh a batch with yfinance-rate-limit-safe pacing. Returns `{n_total, n_ok, n_partial, n_failed, failures}`. `on_progress(i, ticker, status)` hook for streaming UI updates.
  - **`profile_health()`** — single read over `data/ticker_profile/*.json`; returns coverage + freshness counts (`n_total / n_fresh / n_stale / n_full / n_partial / n_no_daily / oldest_ts / newest_ts / symbols_3m`). The dashboard `/profile/health` endpoint is a thin wrapper that adds an `overall` status bucket.
- The 10 current DITP candidates have been profiled end-to-end as the smoke test (8 full + 2 partial — WSR and MCW are still waiting on 1m parquet to ingest).

### 2026-05-23 — `ticker_profile.py`: universal product, moved to `data/ticker_profile/` + 3m baselines added
- User rule (chat 2026-05-23): *"if it is a universal product then it is data output, i propose to put it into the data folder in ...data\ticker_profile"*. Profiles are NOT strategy-specific — NVDA's 3m ATR is one number, computed once, consumed by any strategy that asks.
- **Storage moved** from `strategy/<FAMILY>/profiles/<TICKER>.json` → `data/ticker_profile/<TICKER>.json`. Gitignored (same lifecycle as `data/price_history/` — regenerable, Dropbox-synced, daily churn doesn't belong in git).
- **`family` parameter dropped** from `get_profile`, `save_profile`, `is_stale`, `refresh_profile`, `get_or_refresh`, `profile_path`. The legacy `--family` CLI flag is gone. Sole pre-existing consumer was the (still-untouched) GUNS scanner; the public API gets simpler.
- **Profile shape now sectioned by timeframe** — `stats_daily`, `stats_1m_rth`, `stats_3m_rth` (each a sub-dict). Legacy top-level fields (`atr_14d`, `prev_close`, `avg_minute_vol_rth`, ...) are still written ALONGSIDE the sections for back-compat with old reads. Future 5m / 15m sections will follow the same pattern.
- **New `compute_3m_stats_from_1m_bars(bars)`** — aggregates 1m → 3m via `patterns.aggregate_to_n_min`, filters to RTH (13:30-21:00 UTC widest year-round window), then computes Wilder ATR + range p10/p50/p90 + body-ratio mean/stddev + upper/lower tail p90 + outside-bar frequency. The percentile bundle is what makes ticker-relative candlestick anti-pattern detection possible.
- **New `_fetch_yfinance(ticker)` helper** — single yfinance call returns `(daily_bars, minute_bars)` so the daily and 1m sections share one HTTP round-trip.
- **`refresh_profile()` rebuilt** — now builds all sections in one pass; each section is best-effort; missing sections are omitted from the JSON (no `null` poisoning). Returns None only if NO section could be populated.
- **Bar timestamp handling** — accepts both `datetime` objects (live ingest) and ISO strings (`bars_store.load_bars` returns ISO strings). The 1m → 3m aggregation coerces to datetime before passing to `patterns.aggregate_to_n_min`.
- **Migrated POET.json** in-place; legacy `strategy/GUNS/profiles/` directory + its stub README deleted.
- **Smoke-tested:** `py resources/ticker_profile.py NVRI --force-refresh` produces all three sections (atr_14d=$0.57, 3m atr=$0.002 reflecting the thin-liquidity reality of a $19 sideways name, p90 upper-tail-ratio=0.80).

### 2026-05-23 — `patterns.py`: numpy-array primitives (cross-strategy hoist)
- User rule (chat 2026-05-23): *"this pattern recognition should be an independent module in the resources because all the strategy will be using it"*.
- Five new functions appended to `resources/patterns.py` as a "Numpy-array primitives" section — separate from the existing list-of-dict API so the two conventions coexist cleanly:
  - **`atr_wilder_np(highs, lows, closes, period=14)`** — Wilder ATR last value
  - **`ema_np(arr, period)`** — EMA series, numpy in / numpy out (no SMA bootstrap; use list-of-dict `ema()` for TradingView parity)
  - **`thrust_bar_np(opens, highs, lows, closes, atr, ...)`** — flush-up bar detector, returns negative offset
  - **`window_slopes_np(opens, highs, lows, closes, resistance, atr, ...)`** — slope-of-highs / slope-of-lows + rect height + last-candle anatomy for a trailing window
  - **`horizontal_resistance_np(highs, lows, closes, current_price, atr, ...)`** — full mountain-anchored cluster resistance finder (the big one — ~120 LOC). Returns a dict `{level, cluster_touches, mountain_anchors, range_low, range_high, range_mountains}` or None.
- numpy imported lazily via `_np()` helper so list-of-dict-only consumers don't pay the import cost.
- Module docstring rewritten to advertise both API conventions side by side. The list-of-dict primitives (`ema`, `find_pivots`, `bull_flag`, etc.) are untouched — strict additive change.
- All 49 existing patterns tests still pass.
- First consumer: `strategy/DITP/scanner.py` — its inline `atr14`, `ema`, `find_flush_up_bar`, `find_resistance` are now thin adapters over these primitives. DITP P2 scan output is byte-identical before and after (10 candidates, same order, same scores). Future ORB / other breakout strategies can reuse the same primitives directly.

### 2026-05-23 — `yfinance_news.py`: cross-strategy docstring REVERTED same session
- User reversed the M&A overlay in DITP same session (*"nevermind we drop out the m&A"*). Since the only consumer outside GUNS was the (now-deleted) DITP filter, the docstring is back to "GUNS-specific … do not import from non-GUNS code".
- The `classify()` function and regex tables were never touched in either direction. Pure docstring revert.

### 2026-05-22 — `ibkr_movers.py`: general IBKR US market scanner CLI + library
- New `resources/ibkr_movers.py` (~280 LOC). Strategy-agnostic wrapper over `ib_insync.ScannerSubscription` exposing IBKR's full scanner catalog: TOP_PERC_GAIN / TOP_PERC_LOSE / MOST_ACTIVE / HOT_BY_VOLUME / HIGH_OPEN_GAP / etc.
- Friendly presets: `gainers`, `losers`, `active`, `volume`, `gappers`, `downgap`, `open_up`, `open_dn`, `near_hi`, `near_lo`. Plus `custom` for any scan code + filter combo, and `codes` to list the common scan codes.
- All filters exposed: `min/max_price`, `min_volume`, `min_avg_volume`, `min/max_change_pct`, `min_market_cap_million`, `stock_type_filter` (CORP/ETF/ALL). Location defaults to `STK.US.MAJOR` (NYSE + NASDAQ + AMEX); accepts `STK.NYSE`, `STK.NASDAQ`, `STK.OTC`, etc.
- **clientId 84** — stays clear of 71 (live bot), 80 (observer), 82 (GUNS scanner), 83 (history ingest), 98 (probe), 99 (dashboard).
- **ScannerSubscription is a streaming subscription**, NOT a historical-data request — it does NOT count toward IBKR's 60-per-600s historical-data pacing cap. Safe to run alongside the parquet ingest or the live bot.
- v0.1 returns contract metadata only (symbol / exchange / primary_exchange). Snapshot price / change% / volume enrichment via chained `reqMktData` is a clean follow-up.
- Smoke-tested 2026-05-22 against live TWS: `gainers` returned QTEX, BIYA, MTVA, RYOJ, GOVX, ...; `active` returned QTEX, RGTI, BIYA, NVDA, ...; `gappers` returned BIYA, QTEX, GOVX, HCWB, ...

### 2026-05-22 — Data-integrity audit: ingest log + integrity checker + dashboard pill
- **New module** `resources/data_integrity.py` (~340 LOC). Three classes of per-symbol check: **freshness** (last bar within `FRESH_DAYS=2` business days = fresh; 3–6 = stale; ≥ 7 = ancient), **consistency** (bars sorted, no duplicates, no business-day gaps > 5), **validity** (OHLCV sanity per bar: `h ≥ max(o,c)`, `l ≤ min(o,c)`, `h ≥ l`, prices > 0, volume ≥ 0). Aggregate `health_report()` returns `HealthReport(fresh, stale, ancient, missing, consistency_failures, validity_failures, overall: ok|warn|critical, stale_symbols, ancient_symbols, invalid_symbols)`.
- **New CLI**: `py resources/data_integrity.py {freshness,validity,log,summary}`. `summary` is the same shape served by `/data/health`.
- **`bars_store.py` now logs every write** to `data/ingest_log.jsonl` (best-effort, never raises). New `write_bars(..., source=)` param identifies the caller; new public `log_ingest_event(...)` hook for resume-skip and error events that don't reach `write_bars`. Each entry: `{ts, source, symbol, timeframe, bars_added, last_bar, n_total, error?, note?}`.
- **`yfinance_history.py`** threads `source=` through `_ingest`, `ingest_daily`, `ingest_intraday`, `_seed_universe`. Each seed/ingest call now tags its writes with `yfinance.seed-sp500` / `yfinance.seed-midcap400` / `yfinance.ingest` etc. Resume-skipped symbols audit-logged with `note=resume_skipped` so the dashboard can explain stale coverage.
- **`ibkr_history.py`** writes tagged with `source="ibkr_history"`. Connection failures (qualify, reqHistoricalData) now record an `error` event so the dashboard's pill turns red when TWS drops mid-ingest.
- **Verified end-to-end** on 2026-05-22 daily snapshot: 1517 fresh / 2 stale (CSGS, SNCY) / 0 ancient / 0 invalid across the 1,519-symbol universe. Pill shows amber `2 stale`; modal lists CSGS + SNCY.

### 2026-05-22 — New constituent fetchers: SmallCap 600 + NASDAQ-100 + DJIA + MidCap 400
- Four new modules mirroring `sp500.py`: `sp_midcap400.py`, `sp_smallcap600.py`, `nasdaq100.py`, `djia.py`. The S&P pages all use the `id="constituents"` table pattern; NASDAQ-100 and DJIA need a more flexible scanner (try all `wikitable`s × first 4 columns, pick the one yielding the expected ticker count). All four cache at `state/cache/<index>.json` with the same 7-day TTL.
- `yfinance_history.py` gained `seed_sp400()` / `seed_sp600()` / `seed_nasdaq100()` / `seed_djia()` plus matching CLI subcommands. Common engine `_seed_universe(symbols, label, ...)` shared across all five seeds.
- Verified counts on 2026-05-22: SmallCap 600 → 603, NASDAQ-100 → 101, DJIA → 30. After seeding, 1,519 unique daily parquets on disk (most NASDAQ-100/DJIA overlap with S&P 500 already in store).

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
- Moved from `~/Dropbox/Claude/mcp-servers/tradingview-mcp` (outside TradeHunter — wrong) into `resources/tradingview-mcp/` (correct). The user reinforced the day-one rule: every dependency lives inside TradeHunter/, no "external tools" exceptions.
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
