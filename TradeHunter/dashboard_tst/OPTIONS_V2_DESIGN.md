# Options v2 — browse by rules, IBKR-only data, shared freshness

Status: **BUILDING** (contract written 2026-10-09). Supersedes the "auto setup" Options
page of v4.127–v4.132 (`OPTIONS_MODULE_DESIGN.md`, kept for history). This file is the
contract every build part works to; when code and this file disagree, fix one of them
in the same commit.

## 0. What the user asked (2026-10-09) and the decisions

User, verbatim points:
1. *"Instead of the system auto check for setup, I want user to be able browse."*
2. *"Remove all the idea, position, chart, and option & date functions. We will re-write the whole function."*
3. *"For option data, I want every data to be from IBKR. Not sourced from elsewhere."*
4. *"The data from IBKR will be run at the backend and stored in the TradeHunter DB every day, and updated during the live trading session."*
5. *"When the user is using the system the user will use his own IBKR API to get live data."*
6. *"When the option data gets updated from any user using IBKR live data, other users also benefit ... keep track when the option data is updated with a timestamp and from which source."*
7. *"Different strategies ... the user can set rules ... the option filter ... the user rules is always visible and can be adjusted to shortlist."*

Q&A answers (2026-10-09):
- **Member IBKR = a downloadable connector.** *"I need a link for user to download the IBKR connector and it can set the setting there. When it runs it shows a green pill; if it is not running it shows red."* → the page has a Download link; the connector (the existing bridge, v2.0) has its own settings page; the Options page shows a status pill.
- **Shortlist:** *"it will show based on what the user rules set and will only show those fulfil criteria."* → one list of trades that pass every rule, over the member's whole basket.
- **Data scope:** options AND stock data from IBKR (chains, greeks, IV, IV history, stock price, daily bars for HV / ATR / volume). **Earnings dates stay on the free source** (IBKR has none without a Wall Street Horizon subscription).
- **Strategies:** *"use a dropdown list so that only one strategy is shown at once."* All ten strategies are in the dropdown and all ten screen.

Interpretations stated (not asked):
- The **basket stays**: it is the universe the backend collects and the shortlist screens.
- The **payoff (risk/reward) chart stays**, inside the trade detail — the user asked for it on 2026-10-03; "chart" in point 2 is the price chart / Chart tab.
- Connector pill has a third state, **amber** = connector running but TWS not reachable (the tooltip says which).
- Old tables (`option_signal`, `option_trades`, `option_trade_checks`, `option_idea_push`, `iv_daily`) are **left in the DB, unused** — no destructive migration in this release.
- The legacy hidden pages (IV Rank / Spread / Positions under `routes/options.py`, `routes/spreads.py`, `routes/portfolio.py`) and their Cboe code are **untouched**; the IBKR-only rule applies to the Options page.

## 1. What is removed

From the Options page and its jobs: the Ideas tab, the card, the Positions tab, the Chart / Options & dates tabs, the price chart, the volatility chart, the "What the system read" rows, the strategy recommender and its chips, "What has to happen", the order ticket, Track this, Telegram idea pushes, the nightly Cboe job, the first-time-read machinery of v4.131 (`option_backfill`, the `iv_seed_ibkr` subprocess path), the admin "Run the data job now".

Files deleted (Part G): `app/services/{option_engine, chart_state, strategy_rules, strike_picker, premium_gauge, order_ticket, telegram_push, option_exits, option_nightly, option_data, option_backfill, option_vol, option_words, option_sizing}.py`, `deploy/options_nightly.py`, `deploy/setup_options_nightly_task.ps1`, templates `_options_{card,read,vol,picks,chain,chart,ticket,positions_tab,rules,status,basket}.html`, tests `test_option_{engines,nightly,backfill,vol,data}.py`, `test_options_page.py` (rewritten as `test_options_v2_page.py`), `test_option_prefs.py` (rewritten as `test_opt_rules.py`). `option_store.py` is reduced (Part A) — its basket helpers stay. **Kept:** `option_metrics.py` (hv, iv_rank_pct, term, skew, expected_move), `opt_legs.py`, `payoff.py` (+ `_payoff_chart.html`), `opt_constants.py`, `job_runs.py`, `clock.py`, `option_quotes.py` (legacy pages), `option_prefs.py` is replaced by `opt_rules.py` (Part E) — delete `option_prefs.py` once nothing imports it.

## 2. Data architecture

### 2.1 Sources (the only two)

| Source | Who | When | Market data type |
|---|---|---|---|
| `hermes` | the collector on Hermes, IB Gateway 4002 (paper login), clientId **89** | always on: first-time history, RTH cycles, one EOD pass | whatever the Hermes login is entitled to: `live` (1) / `frozen` (2) / `delayed` (3) / `delayed_frozen` (4) |
| `member` | a member's own connector, relayed by their browser | while the member has the Options page open and the connector is green | that member's entitlement |

Every stored figure carries **as_of (UTC), source, source_user_id (member only) and mdt**. Nothing on the Options page reads Yahoo, Cboe or Alpaca — except the earnings date (free source, `prices.fetch_next_earnings`), stored with `earnings_src = "yahoo"` so it is visibly the one exception.

### 2.2 Tables (one Alembic migration, chained off the current head — check `alembic heads`; `f4a5b6c7d8e9` at the time of writing)

All ORM, portable types. Names prefixed `opt_` (new), no change to old tables.

**`opt_quote`** — the latest known quote per contract (the shared pool).
`id` PK · `symbol` String(20) · `expiry` String(10) `YYYY-MM-DD` · `right` String(1) `C|P` · `strike` Float ·
`bid` `ask` `mid` `last` Float null · `bid_size` `ask_size` `volume` `oi` Integer null ·
`iv` Float null (**FRACTION**) · `delta` `gamma` `theta` `vega` Float null · `und_price` Float null (spot when quoted) ·
`as_of` DateTime (naive UTC, server-assigned for member data) · `source` String(8) `hermes|member` · `source_user_id` Integer null FK users.id ondelete SET NULL · `mdt` String(14) `live|frozen|delayed|delayed_frozen` · `updated_at` DateTime.
UniqueConstraint(`symbol`,`expiry`,`right`,`strike`, name `uq_opt_quote_contract`); Index(`symbol`,`expiry`); Index(`as_of`).

**`opt_underlying`** — one row per symbol, the latest stock facts.
`symbol` String(20) unique · `spot` Float · `spot_as_of` DateTime · `spot_source` String(8) · `spot_user_id` Integer null · `spot_mdt` String(14) ·
`atr14` `hv20` `hv60` `avg_vol20` (shares) Float null · `bars_as_of` DateTime null ·
`iv30` Float null (PERCENT, IBKR's 30-day IV index, last value) · `iv_rank` `iv_pct` Float null · `iv_n` Integer null · `iv_lo` `iv_hi` Float null · `iv_as_of` DateTime null ·
`earnings_date` String(10) null · `earnings_src` String(8) null · `earnings_as_of` DateTime null ·
`first_seen` DateTime · `history_done` Boolean default False (the 1-year IV + 2-year bars pull happened) · `updated_at` DateTime.

**`opt_underlying_daily`** — the daily history behind IV rank, HV, ATR.
`symbol` · `on` String(10) · `close` `high` `low` Float null · `volume` Float null · `iv30` Float null (PERCENT, IBKR `OPTION_IMPLIED_VOLATILITY` daily close × 100) · `source` String(8) · `as_of` DateTime.
UniqueConstraint(`symbol`,`on`, name `uq_opt_und_daily`). Upsert = query-then-update/insert (a later IBKR value for the same day replaces the earlier one).

**`opt_refresh_log`** — one row per fetch that wrote quotes (the audit trail, and what the freshness badges read).
`id` · `symbol` · `as_of` · `source` · `source_user_id` · `mdt` · `kind` String(10) `history|cycle|eod|member|trade` · `n_contracts` Integer · `n_expiries` Integer · `ms` Integer · `error` Text null. Index(`symbol`,`as_of`). Pruned to 30 days.

**`opt_collector_status`** — single row (`id = 1`), the collector's heartbeat.
`state` String(12) `starting|history|cycle|eod|idle|error|stopped` · `phase_detail` Text · `gateway` String(40) `127.0.0.1:4002` · `gateway_ok` Boolean · `mdt` String(14) · `cycle_n` Integer · `cycle_started` `cycle_finished` DateTime · `symbols_total` `symbols_done` Integer · `last_eod_on` String(10) · `last_error` Text · `heartbeat` DateTime · `pid` Integer · `version` String(16).

`option_chain_snapshot` (existing) keeps the **daily EOD record**: at the EOD pass the collector copies the day's `opt_quote` rows for each symbol into it with `kind="eod"`, `source="ibkr"`. Existing retention (`option_store.prune`, 90 days) stays.

### 2.3 The merge rule (how one member's data helps everyone)

`opt_store.upsert_quotes(db, symbol, rows, *, source, user_id, mdt, as_of)`:
- per contract: **write if no row, or incoming `as_of` >= stored `as_of`** (newer wins). On an equal timestamp a `live` row beats a delayed one.
- contracts not in the payload are untouched (windows differ between sources).
- a member's `as_of` is **the server's receive time** (the connector's own clock is recorded only in the log) — a member cannot back- or forward-date data.
- writes one `opt_refresh_log` row.
Readers always get each contract's own `as_of`/`source`/`mdt` — a shortlisted trade can have legs from different sources; the trade shows its **oldest** leg's age and every source involved.

### 2.4 Validation of member data (`opt_store.validate_contribution`)

Reject the whole payload (400 with the reason) if: symbol not in ANY member's active basket; more than 4000 contracts; spot missing or ≤ 0, or more than 20% from the stored spot when one exists from the last 3 days. Drop single rows (counted, not stored) when: right not C/P; expiry not a date or in the past; strike ≤ 0 or outside [0.2 × spot, 5 × spot]; bid > ask; any price < 0; iv outside (0.01, 5.0) (fraction); |delta| > 1; oi/volume negative. Rate limit: 1 contribution per member per symbol per 20 s, 30 per member per minute (in-process dict, like `_cooldown`). Only approved members (`require_user`).

### 2.5 Freshness and leases

- `opt_store.freshness(db, symbols)` → `{sym: {as_of, source, user_id, mdt, n, age_min}}` from the newest `opt_refresh_log` per symbol (one query).
- `opt_store.next_for_member(db, user, n=1)` → the member's basket symbols ordered by staleness (no data first, then oldest `as_of`), skipping symbols **leased** in the last 60 s (in-process dict `symbol -> (expires, user_id)`), and leases the chosen one. Returns `[{symbol, spec}]` where `spec` is the fetch window (§3.2) built from the stored spot/IV.

## 3. The IBKR fetch library — `bridge/th_ibkr.py` (Part B)

Used by BOTH the Hermes collector and the member connector, so it lives in `bridge/` (the connector ships that folder) and has **no app imports** (stdlib + `ib_insync` only). All functions are `async`, take an already-connected `ib_insync.IB`, never connect themselves.

### 3.1 Functions

```python
VERSION = "2.0"
async def chain_defs(ib, symbol) -> dict
    # {"symbol", "con_id", "exchange", "expiries": ["YYYY-MM-DD"...], "strikes": [float...], "multiplier": 100}
    # reqSecDefOptParams, SMART exchange row preferred; expiries sorted ascending.
def plan(defs, *, spot, iv_hint=None, today=None, max_weekly_dte=63, max_dte=1100,
         sigma_k=2.5, min_side=6, max_side=40, expiries=None) -> list[dict]
    # [{"expiry", "dte", "strikes": [...]}] - the fetch window, PURE (no IB):
    # expiries: every expiry <= max_weekly_dte, plus monthlies (3rd Friday) up to max_dte;
    #   `expiries` (explicit list) overrides.
    # strikes per expiry: within spot +/- sigma_k * spot * iv * sqrt(dte/365) (iv = iv_hint fraction
    #   or 0.40 if None) - TICKER-RELATIVE (CLAUDE.md), at least min_side listed strikes each side,
    #   at most max_side each side.
async def spot(ib, symbol) -> dict
    # {"spot", "bid", "ask", "last", "close", "mdt"} - streaming quote, cancelled after <= 6 s (the bridge's _spot logic).
async def quote(ib, symbol, window, *, max_lines=60, wait=4.0, mdt_pref=(1, 2, 3, 4), progress=None) -> dict
    # {"symbol", "spot", "mdt", "rows": [row...], "requested", "filled", "ms"}
    # contracts = OPT symbol expiry strike right SMART, qualified in batches of 50;
    # subscribed with reqMktData(c, "100,101,106", False, False) in waves of <= max_lines,
    # each wave waits until every ticker has bid/ask (or last/close) AND modelGreeks, or `wait` s,
    # then cancels the wave (the line limit is shared by every client of that login).
    # Market data type: the first that yields prices, in mdt_pref order (the bridge's fallback logic).
    # row = {"expiry","right","strike","bid","ask","mid","last","bid_size","ask_size","volume","oi",
    #        "iv" (FRACTION),"delta","gamma","theta","vega","und_price"} - None where IBKR sent nothing.
async def daily_bars(ib, symbol, duration="2 Y") -> list[dict]
    # [{"on","open","high","low","close","volume"}] TRADES, useRTH, 1 day, oldest first.
async def iv_history(ib, symbol, duration="1 Y") -> list[dict]
    # [{"on","iv"}] OPTION_IMPLIED_VOLATILITY daily close x 100 (PERCENT), oldest first, close > 0 only.
async def account(ib) -> dict
    # {"net_liquidation", "currency"} (connector only).
MDT_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}
```

Pacing (documented in the module): historical requests ≥ 11 s apart per process (60 per 10 min cap, shared with the ingest's clientId 84 on Hermes); ≤ 40 API messages/s; market-data waves ≤ `max_lines` concurrent.

### 3.2 The fetch window spec (what `next_for_member` and the collector pass around)

```json
{"symbol": "LRCX", "spot": 349.2, "iv_hint": 0.46, "expiries": null,
 "max_weekly_dte": 63, "max_dte": 1100, "sigma_k": 2.5, "min_side": 6, "max_side": 40}
```

### 3.3 Throughput (why cycles, and why member data matters)

~25 expiries × ~30 strikes × 2 rights ≈ 1,500 contracts per ticker. At 60 lines and ~3 s per wave ≈ 20 contracts/s → ~75 s per ticker → a 30-ticker basket ≈ 40 min per full cycle. So the collector refreshes the whole universe every ~40–60 min in RTH, and a member's connector refreshes the tickers that member is looking at within seconds — which is exactly why member data is shared.

## 4. The Hermes collector (Part C)

`deploy/options_collector.py` (CLI, `py -3.12` / the venv) + `app/services/opt_collector.py` (the loop, testable with a fake fetch module). Scheduled task **`TST-Options-Collector`**: at startup and daily 07:00 MYT, restart on failure, runs `--forever`. Registered by `deploy/setup_options_collector_task.ps1` (PS 5.1, ASCII). Env: `TST_IBKR_PORT` (default probe 4002, 4001, 7497, 7496 — the v4.131 order), `TST_OPTIONS_COLLECTOR_CLIENT_ID` (default 89), `TST_OPTIONS_MAX_LINES` (60).

Loop (every 15 s tick):
1. **Heartbeat** → `opt_collector_status` row + `dashboard_tst/state/options_collector.json` (the tray reads the file).
2. **Connect** if not connected (port probe; on failure: state `error`, `gateway_ok False`, retry in 60 s).
3. **History first**: any universe symbol with `history_done False` → `daily_bars("2 Y")`, `iv_history("1 Y")` → `opt_underlying_daily` upserts; recompute `opt_underlying` stats (§4.1); `history_done True`. Then an immediate chain `quote` for it (kind `history`). New basket tickers thus get a full read within a minute or two of being added.
4. **RTH cycle** (09:30–16:00 ET, trading days via `clock`): walk the universe in priority order (§4.2), one ticker at a time: `spot` → `plan` → `quote` → `upsert_quotes(source="hermes")` → update `opt_underlying.spot*`. One cycle = one pass; `cycle_n` increments.
5. **EOD pass** once per trading day after 16:15 ET (`last_eod_on` < today): per ticker `spot` + full `quote` with mdt preference (2, 1, 4, 3) (frozen first after the close) → upsert, then `daily_bars("5 D")` + `iv_history("1 M")` increments → stats → copy the symbol's `opt_quote` rows (expiry ≥ today) to `option_chain_snapshot` (`kind="eod"`, `source="ibkr"`, `snap_on` = today ET); earnings refresh (free source) per ticker; `prune` (old snapshots, `opt_refresh_log` > 30 days, `opt_quote` rows with expiry < today − 7).
6. Otherwise `idle`.

Universe = distinct active `option_basket` symbols over all owners. **No new IBKR client in the web app**: the web app never connects to IBKR; only this process (Hermes) and members' connectors do.

### 4.1 Stats (`opt_store.recompute_underlying(db, symbol)`)

From `opt_underlying_daily` (IBKR rows only): `atr14` (Wilder, from high/low/close), `hv20`/`hv60` (`option_metrics.hv` on closes, PERCENT), `avg_vol20`, `iv30` = last `iv30`, `iv_rank`/`iv_pct`/`iv_n`/`iv_lo`/`iv_hi` = `option_metrics.iv_rank_pct(last 252 iv30, iv30)`. IV rank is therefore **IBKR's own 30-day IV series** — the same figure TWS's IV Rank uses.

### 4.2 Priority

Score per symbol: no quotes yet → first; then by `(number of members holding it desc, oldest freshness as_of first)`. A symbol refreshed by a member less than 10 minutes ago is skipped in that cycle.

### 4.3 Visibility (dashboard rule + tray-sync rule)

- Options page status strip: "Hermes: running · cycle 12 · 18/30 tickers this pass · live data" / "Hermes: gateway down since 21:40" / "Hermes collector: no heartbeat for 9 min" (stale after 5 min).
- Hermes tray (`dashboard_intraday/tray_status.py`): one detail line + tooltip fragment from `state/options_collector.json`: state, cycle, last EOD date, gateway ok, heartbeat age; amber when error / stale.

## 5. The member connector 2.0 (Part D)

The existing `bridge/ibkr_bridge.py`, rewritten around `th_ibkr`. Version **2.0**. Still read-only, still `127.0.0.1:9224`, still the origin allow-list (`https://app.tradehunter.net`, `https://tradehunter.net`, `http://127.0.0.1:*`/`http://localhost:*` for dev) + Private Network Access headers.

### 5.1 Settings, set in the connector (the user's answer)

- Config file: `%APPDATA%\TradeHunter\connector.json` (`~/.config/tradehunter/connector.json` elsewhere): `{"tws_host": "127.0.0.1", "tws_port": 7496, "client_id": 86, "max_lines": 40, "allowed_origins": []}`.
- `GET http://127.0.0.1:9224/` → a small self-contained settings page (no CDN): TWS host, port (with the four presets: TWS live 7496 / TWS paper 7497 / Gateway live 4001 / Gateway paper 4002), client ID, max lines; "Save & reconnect"; a live status line (connected / not, account type, data type). `POST /settings` (form or JSON, **same-origin only** — no CORS on this endpoint) writes the file and reconnects.
- CLI flags still override for that run.

### 5.2 Endpoints (CORS for allowed origins)

| Endpoint | Returns |
|---|---|
| `GET /health` | `{ok, version, tws_connected, tws: "host:port", client_id, mdt, account_type ("live"/"paper"/null), error}` — answers in < 200 ms (no IB call; cached state) |
| `GET /chain2?symbol=&spec=<json>` | `th_ibkr.quote` result for the window `plan(spec)` |
| `GET /underlying?symbol=` | `th_ibkr.spot` + `daily_bars("1 Y")` tail stats are server-side; the connector returns `{spot, mdt, bars: [...last 260], iv_series: [...last 260]}` (one history pull per symbol per day, cached) |
| `GET /account` | `{net_liquidation, currency}` |
| `GET /chain`, `/iv`, `/scan` | kept unchanged for the legacy hidden pages |

### 5.3 Download & install

- `GET /options/connector/download` (server, `require_user`) → `TradeHunter-IBKR-Connector-2.0.zip` built in memory from `bridge/` (`ibkr_bridge.py`, `th_ibkr.py`, `start_ibkr_bridge.bat`, `install_bridge.ps1`, `requirements.txt`, `README.txt` = a plain install guide) — `app/services/opt_connector_pkg.py`.
- `install_bridge.ps1` gains: check for `py -3.12`; if missing, offer `winget install -e --id Python.Python.3.12` (asks first); `py -3.12 -m pip install --user ib_insync`; then the existing Startup shortcut + `tradehunter://start-bridge` handler; opens `http://127.0.0.1:9224/` at the end so the member sets TWS host/port there.
- `GET /options/connector` (server) → a short help fragment: three steps (download, run installer, set the port in the connector), what the pill colours mean, and the TWS API checkbox ("Enable ActiveX and Socket Clients", read-only API is fine).

### 5.4 The pill (page side)

Browser probes `http://127.0.0.1:9224/health` every 10 s (2 s timeout):
- **green** — connector answered and `tws_connected` → "IBKR connector: connected (live data)" / "(delayed data)".
- **amber** — answered, TWS not connected → tooltip shows `error` and "check TWS is running and the API port in the connector's settings".
- **red** — no answer → "IBKR connector not running — Download / Start it" (Start uses `tradehunter://start-bridge`).

## 6. Contribution protocol (Part F, page JS + Part A endpoints)

While the page is open and the pill is **green**, a loop in the page:
1. `GET /options/data/next` → `{symbol, spec}` or `{wait: 30}`.
2. `fetch(127.0.0.1:9224/chain2?symbol=..&spec=..)` (≤ 120 s).
3. `POST /options/data/contribute` `{symbol, spot, mdt, rows, connector_version, client_as_of}` → server validates (§2.4), upserts (`source="member"`, `user_id`), returns `{stored, dropped, as_of}`; the page refreshes the shortlist if that symbol is in it.
4. Once per symbol per day (when `opt_underlying.history_done` is False, i.e. Hermes has not done it yet): `GET /underlying` from the connector → `POST /options/data/contribute_history` `{symbol, bars, iv_series}` → `opt_underlying_daily` upserts (source `member`) + `recompute_underlying`.
5. Wait 10 s, repeat. Pauses when the tab is hidden (`document.hidden`).

Plus on demand: the trade detail's **"Refresh these legs live"** → `chain2` with an explicit `expiries` list and a narrow strike window → contribute → re-render the detail.

## 7. Rules v2 — `app/services/opt_rules.py` (Part E)

Stored in `user_option_prefs.prefs` (existing JSON row) under `{"schema": 2, "shared": {...}, "<strategy>": {...}}`. A v1 row (`schema` 1 / absent) is migrated on read: family values copied into each strategy of that family; unknown keys dropped. API:

```python
STRATEGIES = ("buy_call","buy_put","bull_call","bear_put","leaps_call","diagonal_call",
              "bull_put","bear_call","iron_condor","calendar")         # dropdown order
LABELS = {...}                 # "Bull put spread", ...
FAMILY = {...}                 # single | debit_vertical | credit_vertical | leaps | diagonal | condor | calendar
class Field(NamedTuple): default; lo; hi; kind ("int"|"num"|"bool"|"choice"); label; help; unit; choices
SCHEMA: dict[str, dict[str, Field]]   # "shared" + each strategy
def defaults(strategy) -> dict        # merged shared + strategy defaults
def read(db, user) -> dict            # merged, cleaned, v2
def for_strategy(prefs, strategy) -> dict   # {"shared": {...}, "rules": {...}}
def write(db, user, strategy, form: dict) -> tuple[dict, list[str]]   # partial update; returns (prefs, errors)
def reset(db, user, strategy) -> dict
```

Every field has a plain-words `label` (shown) and `help` (tooltip). Bounds are enforced (clamped, error listed).

**shared** (always shown at the top of the rules panel):
| key | default | meaning |
|---|---|---|
| `price_min` | 20 | stock price at least ($) |
| `stock_vol_min` | 0 | 20-day average stock volume at least (shares; 0 = off) |
| `oi_min` | 100 | open interest per leg at least |
| `opt_vol_min` | 0 | option volume today per leg at least (0 = off) |
| `max_leg_spread` | 0.50 | widest bid/ask per leg ($) |
| `max_leg_spread_pct` | 25 | widest bid/ask per leg, % of its mid |
| `earnings_rule` | `none_inside` | `none_inside` (no earnings before the last expiry) / `allow` |
| `monthly_only` | False | monthly expiries only |
| `max_age_h` | 24 | ignore quotes older than this many hours |
| `per_ticker` | 3 | best N trades per ticker in the list |

**per strategy** (shown under the shared block; `iv_rank_min/max` on every strategy):
- `buy_call`, `buy_put`: `delta_lo` .60 `delta_hi` .70 (|delta|), `dte_lo` 30 `dte_hi` 60, `theta_pct_max` 1.0 (daily decay ≤ % of premium), `iv_rank_min` 0 `iv_rank_max` 50.
- `bull_call`, `bear_put`: `long_delta_lo` .60 `long_delta_hi` .70, `short_delta_lo` .25 `short_delta_hi` .35, `width_atr_lo` .5 `width_atr_hi` 2.0, `debit_pct_max` 60 (debit ≤ % of width), `dte_lo` 30 `dte_hi` 60, `iv_rank_min` 0 `iv_rank_max` 70.
- `leaps_call`: `delta_lo` .70 `delta_hi` .85, `months_lo` 9 `months_hi` 18, `extrinsic_pct_max` 10 (time value ≤ % of the premium), `iv_rank_min` 0 `iv_rank_max` 50.
- `diagonal_call`: `long_delta_lo` .70 `long_delta_hi` .80, `long_dte_lo` 180 `long_dte_hi` 365, `short_delta_lo` .20 `short_delta_hi` .30, `short_dte_lo` 30 `short_dte_hi` 45, `debit_pct_spot_max` 25 (net debit ≤ % of stock price), `iv_rank_min` 0 `iv_rank_max` 60.
- `bull_put`, `bear_call`: `short_delta_lo` .20 `short_delta_hi` .30, `width_atr_lo` .5 `width_atr_hi` 1.5, `credit_pct_min` 25 (credit ≥ % of the max loss), `dte_lo` 30 `dte_hi` 60, `iv_rank_min` 30 `iv_rank_max` 100.
- `iron_condor`: `short_delta_lo` .15 `short_delta_hi` .20, `wing_atr_lo` .5 `wing_atr_hi` 1.5, `credit_pct_min` 30, `dte_lo` 30 `dte_hi` 45, `iv_rank_min` 50 `iv_rank_max` 100.
- `calendar`: `delta_tol` .05 (strike within this of delta .50, calls), `front_dte_lo` 20 `front_dte_hi` 30, `back_dte_lo` 50 `back_dte_hi` 70, `front_iv_ge_back` False (require near IV ≥ far IV), `iv_rank_min` 0 `iv_rank_max` 60.

Widths in ATR multiples are ticker-relative (CLAUDE.md); ATR is IBKR's (`opt_underlying.atr14`).

## 8. The screeners — `app/services/opt_screen.py` (Part E)

```python
def screen(strategy, chains: dict[str, dict], unds: dict[str, dict], rules: dict, *, today=None, now=None) -> dict
    # chains: {sym: opt_store.chain_view(...)}   unds: {sym: opt_store.underlying(...)}   rules: opt_rules.for_strategy(...)
    # -> {"rows": [Candidate...], "funnel": [{"rule", "label", "removed"}...], "tickers": {sym: {"passed", "reason"}},
    #     "n_considered": int, "n_passed": int}
def detail(strategy, candidate_id, chain, und, rules, *, today=None) -> dict | None
    # re-derives one candidate from the store (legs + payoff.build input), None when gone
```

`Candidate`: `{"id"` (stable: `sym|strategy|expiry1|right1|strike1|...`), `"symbol"`, `"strategy"`, `"legs": [{"expiry","right","strike","side" ("buy"|"sell"),"qty","bid","ask","mid","iv","delta","oi","volume","as_of","source","source_user_id","mdt"}]`, `"dte"` (nearest leg), `"net"` (per share, + = credit, − = debit, at mids), `"net_natural"` (at bid/ask), `"max_profit"`, `"max_loss"` (per contract $, None = unlimited/undefined), `"breakevens"`, `"pop"` (probability of profit at expiry: credit/debit verticals & condor from the breakeven via a lognormal with the legs' IV; singles/LEAPS from delta of breakeven ≈ same lognormal), `"ror"` (max_profit / max_loss), `"score"` (sort key, per family below), `"metrics"` (family extras: `credit_pct`, `debit_pct`, `width`, `theta_pct`, `extrinsic_pct`, `short_delta`, ...), `"liquidity"` (`oi_min`, `spread_max`, `spread_pct_max`), `"data"` (`as_of_oldest`, `age_min`, `sources`: e.g. `["hermes·live", "member:Kui·live"]`), `"underlying"` (`spot`, `iv_rank`, `iv30`, `hv20`, `atr14`, `earnings_date`)}.

Pipeline per ticker: **stock filters** (price, stock volume, IV rank range, earnings data present when `none_inside`) → **expiry window** (DTE, monthly_only, earnings rule per expiry) → **enumerate** the family's structures → **leg filters** (OI, volume, spread $ and %, quote age ≤ `max_age_h`) → **family rules** → **score**, keep `per_ticker` best. Every removal increments the funnel counter of the rule that removed it (first failure only), so "why so few?" is answerable.

Families:
- **single** (`buy_call`/`buy_put`): each call/put with |delta| in band; `theta_pct` = |theta| / mid × 100 ≤ max. Score: lowest `theta_pct`, then delta closest to band middle.
- **debit_vertical** (`bull_call` calls / `bear_put` puts): long leg in long band; short leg further OTM in short band, same expiry; width in `[width_atr_lo, width_atr_hi] × ATR`; debit ≤ `debit_pct_max`% of width. max_profit = (width − debit) × 100, max_loss = debit × 100. Score: ror × pop.
- **credit_vertical** (`bull_put` puts / `bear_call` calls): short in band; long further OTM, width in ATR band; credit ≥ `credit_pct_min`% of (width − credit). max_profit = credit × 100, max_loss = (width − credit) × 100. Score: credit/(width − credit) × pop.
- **leaps**: calls with DTE in `[months_lo, months_hi]` × 30.44 days, delta in band, extrinsic = mid − max(0, spot − strike); `extrinsic_pct` = extrinsic / mid × 100 ≤ max. Score: lowest extrinsic_pct.
- **diagonal**: long call (long band & DTE) × short call (short band & DTE, strike > long strike, earlier expiry); net debit ≤ `debit_pct_spot_max`% of spot. max_loss = net debit × 100 (approximation stated in the UI); max_profit from `payoff` at the short expiry. Score: lowest debit per unit of long delta.
- **condor**: put credit vertical × call credit vertical, same expiry, both shorts in band, wings in ATR band; total credit ≥ `credit_pct_min`% of (widest wing − credit). Score: credit/(wing − credit) × pop.
- **calendar**: call strike nearest delta .50 within `delta_tol` at the front expiry; same strike at a back expiry in its window; optional `front_iv_ge_back`; debit = back mid − front mid > 0. max_loss = debit × 100; max_profit from `payoff` at the front expiry. Score: lowest debit / (back − front DTE).

Payoff for the detail: `payoff.build(legs, strategy=..., spot, atr, as_of=...)` (existing) — `payoff.py` must stop importing `strategy_rules` / `option_prefs`: it imports `opt_rules.LABELS` / `FAMILY` instead.

Performance: screening runs on request over the member's basket (≤ 60 tickers × ≤ 1,500 contracts). Load each chain once per request (one query per symbol — or one `IN` query); enumerations are bounded by bands. Target < 1.5 s for 30 tickers on Hermes.

## 9. The page (Part F)

`GET /options` (menu "Options", unchanged key). Layout (desktop; stacked on phones):

```
┌ status strip: [● IBKR connector: connected (live)] [Hermes: running · cycle 12 · 18/30] [Download connector] [?]
├ basket (resizable column, kept)   │ Strategy [Bull put spread ▼]  42 trades pass · 30 tickers
│  ticker · data age dot · source    │ ┌ Rules (always visible) ───────────────────────────┐
│  · trades passing (this strategy)  │ │ shared fields …          strategy fields …        │
│                                    │ │ [Reset to defaults]   changes apply as you type    │
│                                    │ └───────────────────────────────────────────────────┘
│                                    │ Results (only trades that pass)  sortable columns
│                                    │ Ticker · Expiry (DTE) · Legs · Credit/Debit · Max profit · Max loss · RoR · POP · Δ · IV rank · Liquidity · Data
│                                    │ "Why so few?" funnel (collapsed)
│                                    │ ── click a row → detail below the row: legs table with each leg's as_of + source,
│                                    │    payoff chart (expiry + today), breakevens, "Refresh these legs live"
```

Routes (`app/routes/options_page.py`, rewritten; prefix `/options`):
- `GET /options` page · `GET /options/basket` · `POST /options/basket/add|remove|import` (kept behaviour, minus v4.131 first-read: a new symbol simply has no data until Hermes or a connector reads it; the basket row says "waiting for first read").
- `GET /options/rules?strategy=` (fragment) · `POST /options/rules?strategy=` (HTMX `hx-trigger="change, keyup changed delay:600ms"` per field; saves, re-renders results via `HX-Trigger: options:rules-changed`) · `POST /options/rules/reset?strategy=`.
- `GET /options/results?strategy=&sort=&dir=` (fragment) · `GET /options/trade?strategy=&id=` (detail fragment).
- `GET /options/status` (strip fragment; polled every 60 s) · `GET /options/connector` (help fragment) · `GET /options/connector/download` (zip).
- `GET /options/data/next` · `POST /options/data/contribute` · `POST /options/data/contribute_history` (JSON).
- The selected strategy is remembered per member (`localStorage th.options.strategy`, default `bull_put`).

Templates (new names — no collision with the files Part G deletes): `options.html` (rewritten), `_opt_basket.html`, `_opt_rules.html`, `_opt_results.html`, `_opt_trade.html`, `_opt_status.html`, `_opt_connector.html`; the payoff uses the existing `_payoff_chart.html`. All scroll areas inherit the invisible-until-hover scrollbars (`base.html`). Every rule field shows its label with the `help` as a tooltip. The data column shows e.g. "12 min · Hermes live" or "2 min · Kui (live)"; > `max_age_h` never appears (filtered); amber when older than 60 min in RTH.

## 10. Build parts and file ownership

| Part | Owns (creates / rewrites) | Tests |
|---|---|---|
| **A data** | `app/models.py` (append the 5 models), `alembic/versions/<new>_options_v2.py`, `app/services/opt_store.py` (new), `app/services/option_store.py` (reduce: keep basket helpers `basket_universe`, `prune` adapted; drop signal/card/lazy compute) | `tests/test_opt_store.py` |
| **B fetch** | `bridge/th_ibkr.py` | `tests/test_th_ibkr.py` (fake IB) |
| **C collector** | `app/services/opt_collector.py`, `deploy/options_collector.py`, `deploy/setup_options_collector_task.ps1`, the tray lines in `dashboard_intraday/tray_status.py` | `tests/test_opt_collector.py` (fake fetch) |
| **D connector** | `bridge/ibkr_bridge.py` (2.0), `bridge/install_bridge.ps1`, `bridge/start_ibkr_bridge.bat`, `bridge/requirements.txt`, `bridge/README.md`, `app/services/opt_connector_pkg.py` | `tests/test_connector.py` |
| **E rules+screen** | `app/services/opt_rules.py`, `app/services/opt_screen.py`, `app/services/payoff.py` (imports only) | `tests/test_opt_rules.py`, `tests/test_opt_screen.py` |
| **F page** | `app/routes/options_page.py`, `app/templates/options.html`, `_opt_*.html` | `tests/test_options_v2_page.py` |
| **G removal+docs** | deletions in §1, `README.md` (dashboard_tst changelog v4.133), `DEPLOY.md`, `app/.env.example`, `../CLAUDE.md` (clientId 89, the design pointer), `dashboard_intraday/README.md` (tray line), `app/__init__.py` (4.133) | the whole suite green |

Interfaces between parts are exactly the signatures in §2–§8. A part that needs another part's function before it exists stubs it in its own tests only, never in app code.

## 11. Deploy (Hermes)

1. `git pull --ff-only`; `pip install -r app\requirements.txt` (venv); restart the web app (the canonical script).
2. `schtasks /Change /TN TST-Options-Nightly /DISABLE` (the Cboe job is gone).
3. `powershell -ExecutionPolicy Bypass -File deploy\setup_options_collector_task.ps1 -StartNow`.
4. `app\.env`: `TST_IBKR_PORT=4002` (optional; the probe finds it).
5. Watch the strip: "Hermes: history 3/30" → "running · cycle 1".

CLAUDE.md clientId table: **89 — Options collector (`dashboard_tst/deploy/options_collector.py`) — Hermes**; 86 stays the members' connector default; 87/88 (v4.131 seeder path) retired from the Options page (87 remains for the legacy screener's manual seeder).
