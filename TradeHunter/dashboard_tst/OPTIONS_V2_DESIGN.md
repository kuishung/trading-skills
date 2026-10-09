# Options v2 — browse by rules, IBKR-only data, shared freshness

Status: **BUILT in v4.133 (2026-10-09)** (contract written the same day; what the build
changed on purpose is in §12), then **reviewed before release (2026-10-09/10)** — every
fix the six-lens review led to is listed in §12 "Review fixes", and §2–§9 below are kept
in line with it. Supersedes the "auto setup" Options page of v4.127–v4.132
(`OPTIONS_MODULE_DESIGN.md`, kept for history). This file is the contract the build parts
worked to; when code and this file disagree, fix one of them in the same commit.

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

*At build (v4.133):* the Telegram ideas push was left in place as a closed island nothing in the app calls any more — `telegram_push.py`, `strategy_rules.py`, `option_words.py`, `option_prefs.py`, `tests/test_telegram_push.py`, and `option_store`'s v1 `option_signal` / `iv_daily` helpers it reads. It goes in one follow-up; see §12.

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
`state` String(12) `starting|history|cycle|eod|idle|error|stopped` (+ `waiting`, §4.0 / §12) · `phase_detail` Text · `gateway` String(40) `127.0.0.1:4002` · `gateway_ok` Boolean · `mdt` String(14) · `cycle_n` Integer · `cycle_started` `cycle_finished` DateTime · `symbols_total` `symbols_done` Integer · `last_eod_on` String(10) · `last_error` Text · `heartbeat` DateTime · `pid` Integer · `version` String(16).

`option_chain_snapshot` (existing) keeps the **daily EOD record**: at the EOD pass the collector copies the day's `opt_quote` rows for each symbol into it with `kind="eod"`, `source="ibkr"`. Existing retention (`option_store.prune`, 90 days) stays.

### 2.3 The merge rule (how one member's data helps everyone)

`opt_store.upsert_quotes(db, symbol, rows, *, source, user_id, mdt, as_of)`:
- per contract: **write if no row, or incoming `as_of` >= stored `as_of`** (newer wins). On an equal timestamp a `live` row beats a delayed one.
- contracts not in the payload are untouched (windows differ between sources).
- a member's `as_of` is **the server's receive time** (the connector's own clock is recorded only in the log) — a member cannot back- or forward-date data. *Review (R2):* a member's `delayed` / `delayed_frozen` data is filed at the receive time **minus 15 min** (`DELAY_S`; quotes, spot and log row) — IBKR's delayed feed shows the market 15 min ago, so it never replaces a live quote taken after it.
- writes one `opt_refresh_log` row.
Readers always get each contract's own `as_of`/`source`/`mdt` — a shortlisted trade can have legs from different sources; the trade shows its **oldest** leg's age and every source involved.

### 2.4 Validation of member data (`opt_store.validate_contribution`)

Reject the whole payload (400 with the reason) if: symbol not in ANY member's active basket; more than 4000 contracts; spot missing or ≤ 0, or ~~more than 20% from the stored spot when one exists from the last 3 days~~ outside the ticker-relative band around **IBKR's own reference price** (*review R1*: `spot_reference` / `spot_band` — a member's spot never becomes the reference, so the band cannot be walked; §12). Drop single rows (counted, not stored) when: right not C/P; expiry not a date or in the past; strike ≤ 0 or outside [0.2 × spot, 5 × spot]; bid > ask; any price < 0; iv outside (0.01, 5.0) (fraction); |delta| > 1; oi/volume negative — plus the review's plausibility rules (R3, R4: weekend / > 1100-day expiries, strikes off the 0.50 grid, prices above the cap or under intrinsic, a delta whose sign contradicts the right; a posted `mid` is ignored and recomputed). Rate limit: 1 contribution per member per symbol per 20 s, 30 per member per minute (in-process dict, like `_cooldown`). Only approved members (`require_user`); the body is read only after the sign-in check and refused over 5 MB (R26).

### 2.5 Freshness and leases

- `opt_store.freshness(db, symbols)` → `{sym: {as_of, source, user_id, mdt, n, age_min}}` from the newest `opt_refresh_log` per symbol (one query). *Review (R6):* only rows that wrote contracts and are not a `trade` refresh count; one bounded `LIMIT 1` subquery per symbol.
- `opt_store.next_for_member(db, user, n=1)` → the member's basket symbols ordered by staleness (no data first, then oldest `as_of`), skipping symbols **leased** ~~in the last 60 s~~ (in-process dict `symbol -> (expires, user_id)`), and leases the chosen one. Returns `[{symbol, spec}]` where `spec` is the fetch window (§3.2) built from the stored spot/IV. *As built + review (R5, R20):* returns ONE `{symbol, spec, history_done}` or None, also skips symbols in a member **back-off** and symbols refreshed in the last 60 s; the lease lasts **240 s** and `release_lease(symbol, user_id)` drops only the caller's own; the spec is a **chunk** (§6).

## 3. The IBKR fetch library — `bridge/th_ibkr.py` (Part B)

Used by BOTH the Hermes collector and the member connector, so it lives in `bridge/` (the connector ships that folder) and has **no app imports** (stdlib + `ib_insync` only). All functions are `async`, take an already-connected `ib_insync.IB`, never connect themselves.

### 3.1 Functions

```python
VERSION = "2.0"
async def chain_defs(ib, symbol) -> dict
    # {"symbol", "con_id", "exchange", "expiries": ["YYYY-MM-DD"...], "strikes": [float...], "multiplier": 100}
    # reqSecDefOptParams, SMART exchange row preferred; expiries sorted ascending.
def plan(defs, *, spot, iv_hint=None, today=None, max_weekly_dte=63, max_dte=1100,
         sigma_k=2.5, min_side=6, max_side=40, expiries=None, max_expiries=None) -> list[dict]
    # [{"expiry", "dte", "strikes": [...]}] - the fetch window, PURE (no IB):
    # expiries: every expiry <= max_weekly_dte, plus monthlies (3rd Friday) up to max_dte;
    #   `expiries` (explicit list) overrides.
    # max_expiries (> 0, review R12): with no explicit list, only the NEAREST that many of the
    #   chosen expiries - a member's connector reads a chain in chunks that fit its time limit.
    # strikes per expiry: within spot +/- sigma_k * spot * iv * sqrt(dte/365) (iv = iv_hint fraction
    #   or 0.40 if None) - TICKER-RELATIVE (CLAUDE.md), at least min_side listed strikes each side,
    #   at most max_side each side.
async def spot(ib, symbol) -> dict
    # {"spot", "bid", "ask", "last", "close", "mdt"} - streaming quote, cancelled after <= 6 s (the bridge's _spot logic).
async def quote(ib, symbol, window, *, max_lines=60, wait=4.0, mdt_pref=(1, 2, 3, 4), progress=None,
                spot=None, deadline=None) -> dict
    # {"symbol", "spot", "mdt", "rows": [row...], "requested", "filled", "ms", "partial", "attempted", ...}
    # deadline (seconds from now, review R12): once reached no new qualification batch and no new
    #   wave starts, the open wave's window is cut short and released, and the rows read so far come
    #   back with partial=True ("partial" and "attempted" - contracts actually subscribed - are always
    #   in the result). A long read returns what it has instead of being cancelled with nothing.
    # Every wave's lines are released in a `finally` that never awaits (unpaced cancels, charged to
    #   the message rate afterwards - review R13): a cancellation can no longer leak lines.
    # contracts = OPT symbol expiry strike right SMART, qualified in batches of 50;
    # subscribed with reqMktData(c, "100,101,106", False, False) in waves of <= max_lines,
    #   (BUILT: OPT_TICKS = "101" only - see §12)
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
async def fetch(ib, symbol, spec=None, *, max_lines=60, wait=4.0, mdt_pref=..., progress=None,
                today=None, deadline=None) -> dict      # added at build (§12 item 7)
    # chain_defs + spot -> plan_spec(spec) (incl. max_expiries / max_side) -> quote; `deadline`
    #   covers the whole read - quote gets what is left after the chain definition and the spot.
MDT_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}
NEG_TTL = 3 days   # a "contract does not exist" verdict is cached this long (review R17; 6 h at build)
```

Pacing (documented in the module): historical requests ≥ 11 s apart per process (60 per 10 min cap, shared with the ingest's clientId 84 on Hermes); ≤ 40 API messages/s; market-data waves ≤ `max_lines` concurrent.

### 3.2 The fetch window spec (what `next_for_member` and the collector pass around)

```json
{"symbol": "LRCX", "spot": 349.2, "iv_hint": 0.46, "expiries": null,
 "max_weekly_dte": 63, "max_dte": 1100, "sigma_k": 2.5, "min_side": 6, "max_side": 40}
```

A member's chunk (review R20, `next_for_member`): `"max_side": 25` and EITHER `"expiries": [up to 6 stored expiries, the stalest first]` OR — nothing stored yet — `"expiries": null, "max_expiries": 6` (the nearest 6 eligible). New expiries of an already-quoted ticker therefore arrive with Hermes's evening pass, which reads the full window.

### 3.3 Throughput (why cycles, and why member data matters)

~25 expiries × ~30 strikes × 2 rights ≈ 1,500 contracts per ticker. At 60 lines and ~3 s per wave ≈ 20 contracts/s → ~75 s per ticker → a 30-ticker basket ≈ 40 min per full cycle. So the collector refreshes the whole universe every ~40–60 min in RTH, and a member's connector refreshes the tickers that member is looking at within seconds — which is exactly why member data is shared. (Built reality, §4.0: on Hermes the Gateway is off through every weekday session, so the RTH cycle does not run there at all — during the session the members' connectors ARE the live feed, and Hermes supplies the evening EOD record and the history.)

## 4. The Hermes collector (Part C)

`deploy/options_collector.py` (CLI, the venv's Python 3.12) + `app/services/opt_collector.py` (the loop, `COLLECTOR_VERSION` 1.1, tested with a fake fetch module). Scheduled task **`TST-Options-Collector`**: at startup (1 min delay) and daily 07:00 MYT (revives a dead copy only), restart on failure, no time limit, runs `--forever --log-file logs\options_collector.log` (python started directly; the collector rotates its own log at 5 MB, keeping 5 old files — review R18; the build appended stdout with `>>`, unbounded). Registered — and, after a pull that changes `opt_collector.py` / `th_ibkr.py` / `options_collector.py`, restarted — by `deploy\setup_options_collector_task.ps1 -StartNow` (PS 5.1, ASCII; it stops the running copy and an orphaned python child first). Env: `TST_IBKR_PORT` (unset = try 4002, 4001, 7497, 7496), `TST_OPTIONS_COLLECTOR_CLIENT_ID` (89), `TST_OPTIONS_MAX_LINES` (60, kept within 1–100). One-off runs: `--once`, `--history SYM ...`, `--eod-now`, `--ignore-ingest`.

### 4.0 The blackout reality (why Hermes is not the session feed)

The ingest supervisor (`scripts/ingest_supervisor.py`) OWNS the Hermes IB Gateway (paper login, port 4002); the collector never starts, stops or holds it up, it only reads what the supervisor publishes (its schedule, `state/ingest_supervisor_state.json`, `state/supervisor_heartbeat.json`):

- **Mon–Fri 08:00–20:10 ET the Gateway is OFF** — the manual-trading blackout: the user trades on the live login, and IBKR shares market data between the two logins but not at the same time.
- **20:10 ET** it opens for the nightly OHLCV top-up (clientId 84, same login) and, on a weekday, **closes the moment the top-up succeeds** (before its read-only deep check).
- **Sat 00:00 – Mon 08:00 ET** it stays up for seeding.

What the build does with that:

1. **During the US session the live updates come from members' connectors** (§5, §6). The collector's state is `waiting` (reason `blackout`), its detail "Gateway off for the manual-trading blackout until 20:10 ET - members' IBKR connectors carry the session"; the page strip shows "Hermes: waiting · Gateway off by design" (neutral grey) and the tray a grey line. So the RTH cycle (step 4) runs only where a Gateway is up during the session — a PC without the supervisor (the laptop), or a supervisor manual override.
2. **Weekday evenings, 20:10 ET until the top-up ends, are Hermes's chain window**: the **EOD pass** (frozen quotes first → `opt_quote`, the daily record in `option_chain_snapshot`, the earnings date) and the first chain read of a never-quoted new ticker. The first 15 min after 20:10 ET are `waiting` (`starting`); after the top-up the Gateway closes → `waiting` (`closed`, until the next 20:10 ET, or Sat 00:00 after a Friday).
3. **Historical requests are deferred.** First-time history (2 Y bars + 1 Y IV30) and the EOD increments share IBKR's per-login historical pacing (60 per 10 min) with the top-up, so they run only when no top-up can be running: no supervisor state file on the PC, its heartbeat older than a day, a weekend, or its `last_success_session` ≥ the session that is due. On a weekday the Gateway closes as soon as that becomes true, so **on Hermes history runs in the weekend seeding window**; increments a deferral left behind are caught up then, with a duration that covers the gap (5 D / 1 M / 6 M / 2 Y). Meanwhile a member's connector can file a new ticker's year of history (§6 step 4). **Chain quotes are market data, not historical — never deferred.** The tray and the state file say "history waits (N)".
4. An unreachable Gateway OUTSIDE those designed windows is an `error` (retry after 60 s, doubling to 5 min): the strip turns red, the tray amber.

### 4.0.1 The loop (one step per 15 s tick, so the heartbeat stays timely)

1. **Heartbeat** → `opt_collector_status` row 1 + `dashboard_tst/state/options_collector.json` (the tray reads the file; the file also carries `wait_reason`, `wait_until`, `history_waiting`, `universe`, `down_since`, `next_connect`).
2. **Connect** if not connected — connecting IS the probe (read-only, clientId 89, the port that last worked first). Down by design → `waiting` (a try every 60 s, no back-off); otherwise `error`.
3. **History first** (when allowed, 4.0 point 3): one universe symbol with `history_done False` → `daily_bars("2 Y")`, `iv_history("1 Y")` → `opt_underlying_daily` → stats (§4.1) → `history_done True` → an immediate chain read (kind `history`). A failed pull is retried after 30 min, doubling to 6 h. While history is deferred, a never-quoted symbol still gets its chain read at once (kind `history`, retried after 30 min on failure).
4. **RTH cycle** (09:30–16:00 ET, trading days via `clock`, only when a Gateway is up — 4.0 point 1): the next symbol in the §4.2 order: `th_ibkr.fetch` (`chain_defs` + `spot` → `plan` → `quote`) → `upsert_quotes(source="hermes", kind="cycle")` + spot. One cycle = one pass; `cycle_n` increments; a new cycle starts at most every 10 min (`MIN_CYCLE_S`); the close cuts a running cycle short.
5. **EOD pass**, once per trading day (`last_eod_on` < the day): the day is today after 16:15 ET, the previous trading day before today's open (a pass missed in the evening is caught up while a Gateway is up before the open — on Hermes, Friday's over the weekend), the last trading day on a weekend / holiday. Per ticker: a chain read with mdt preference (2, 1, 4, 3) (frozen first after the close; kind `eod`); the history increments (`5 D` / `1 M`, when allowed — else left to step 6); `opt_store.snapshot_eod` (the day's `opt_quote` rows with expiry ≥ the day → `option_chain_snapshot`, `kind="eod"`, `source="ibkr"`, `snap_on` = the day); the earnings date (free source). When the pass ends: `opt_store.prune_v2` (snapshots via `option_store.prune`, `opt_refresh_log` > 30 days, `opt_quote` rows expired more than 7 days).
6. Otherwise: the deferred history increments, one symbol per tick, when allowed; else `idle`.

Universe = distinct active `option_basket` symbols over all owners. **No IBKR client in the web app**: the web app never connects to IBKR; only this process (Hermes) and members' connectors do.

### 4.1 Stats (`opt_store.recompute_underlying(db, symbol)`)

From `opt_underlying_daily` (IBKR rows only): `atr14` (Wilder, from high/low/close), `hv20`/`hv60` (`option_metrics.hv` on closes, PERCENT), `avg_vol20`, `iv30` = last `iv30`, `iv_rank`/`iv_pct`/`iv_n`/`iv_lo`/`iv_hi` = `option_metrics.iv_rank_pct(last 252 iv30, iv30)`. IV rank is therefore **IBKR's own 30-day IV series** — the same figure TWS's IV Rank uses.

### 4.2 Priority

Score per symbol: no quotes yet → first; then by `(number of members holding it desc, oldest freshness as_of first)`. A symbol refreshed by a member (or by its own first-time read) less than 10 minutes ago is skipped in that cycle.

### 4.3 Visibility (dashboard rule + tray-sync rule)

- Options page status strip: "Hermes: running · cycle 12 · 18/30 tickers this pass · live data" / "Hermes: waiting · Gateway off by design" (grey; the tooltip says why and until when) / "Hermes: gateway down" (red, a real outage) / "Hermes collector: no heartbeat for 9 min" (amber, stale after 5 min).
- Hermes tray (`dashboard_intraday/tray_status.py`): one detail line + tooltip fragment from `state/options_collector.json`: state, cycle, last EOD date, gateway ok, "history waits (N)", heartbeat age; green running, grey `waiting` or no file, amber error / stopped / stale.

## 5. The member connector 2.0 (Part D)

The existing `bridge/ibkr_bridge.py`, rewritten around `th_ibkr`. Version **2.0**. Still read-only, still `127.0.0.1:9224`, still the origin allow-list + Private Network Access headers. *As built + review (R15):* the allow-list is any HTTPS host in `tradehunter.net` (the apex and every subdomain — the site is `app.tradehunter.net`), `http://127.0.0.1` / `http://localhost` on the **dev ports 8000–8099 only** (the build trusted every loopback port, so any program on the member's PC could read `/account`), plus extras from the settings page or `--origin`.

**Cross-site requests without an Origin are refused (review R14).** Browsers leave `Origin` off an `<img>`, a `<script src>` or a `fetch(..., {mode: "no-cors"})` GET; the page that sent it cannot read the answer, but the TWS read would still run (holding the quote slot and up to 40 market-data lines per request). So `do_GET` answers **403 before any work** when `Origin` is absent and `Sec-Fetch-Site` is present and not `same-origin` / `none` — on every endpoint except the settings page `/` (the Options page links to it). Our own pages always send `Origin` (CORS fetches); curl / scripts send neither header and are unaffected. Requests whose `Host` is not a loopback name are refused too (DNS rebinding).

### 5.1 Settings, set in the connector (the user's answer)

- Config file: `%APPDATA%\TradeHunter\connector.json` (`~/.config/tradehunter/connector.json` elsewhere): `{"tws_host": "127.0.0.1", "tws_port": 7496, "client_id": 86, "max_lines": 40, "allowed_origins": []}`.
- `GET http://127.0.0.1:9224/` → a small self-contained settings page (no CDN): TWS host, port (with the four presets: TWS live 7496 / TWS paper 7497 / Gateway live 4001 / Gateway paper 4002), client ID, max lines; "Save & reconnect"; a live status line (connected / not, account type, data type). `POST /settings` (form or JSON, **same-origin only** — no CORS on this endpoint) writes the file and reconnects.
- CLI flags still override for that run.

### 5.2 Endpoints (CORS for allowed origins)

| Endpoint | Returns |
|---|---|
| `GET /health` | `{ok, version, tws_connected, tws: "host:port", client_id, mdt, account_type ("live"/"paper"/null), error}` — answers in < 200 ms (no IB call; cached state) |
| `GET /chain2?symbol=&spec=<json>` | `th_ibkr.quote` result for the window `plan(spec)` — *as built + review (R12):* `th_ibkr.fetch` (chain_defs + a fresh spot → `plan` → `quote`) + `{ok, connector_version, partial}`. The spec's `max_expiries` (0–80, 0 = no cap) and `max_side` reach `plan`. **Timeout 150 s** (`CHAIN2_TIMEOUT`, under the page's 160 s fetch limit so the page always gets a JSON answer); the read's **deadline is 15 s under it**, counted from the request (~135 s): past it `quote` starts no new wave and returns what it read, `"partial": true` (always in the answer). One read at a time: a read that waited > 1 s for another and has < 20 s left answers **`{"ok": false, "busy": true, "error"}`** rather than an empty read. 20 s cache per (symbol, spec). |
| `GET /underlying?symbol=` | `th_ibkr.spot` + `daily_bars("1 Y")` tail stats are server-side; the connector returns `{spot, mdt, bars: [...last 260], iv_series: [...last 260]}` (one history pull per symbol per day, cached; *review R16:* an empty history from IBKR is an error, never cached) |
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
- *Review (R19, R23)* — two more states. **orange "out of date"**: a connector answered but is not 2.x (a `/health` without `tws_connected`, or a version that does not parse as 2.x, e.g. `TradeHunterIBKRBridge/1.6`) → "IBKR connector 1.6 is out of date - download 2.0" with a Download 2.0 link; no contribution loop runs (the 1.x connector has no `/chain2`). **red "blocked by the browser"**: the probe fails and the browser's Local Network Access permission reads `denied` (Chrome / Edge 142+) → how to allow it under the site settings, and "do not install the connector again". The loop runs only on green.

## 6. Contribution protocol (Part F, page JS + Part A endpoints)

While the page is open and the pill is **green**, a loop in the page (as built + review R20–R22, R26, R28):
1. `GET /options/data/next` → `{symbol, spec, history_done}` or `{wait: 30}`. The symbol is leased to this member for **240 s**; the `spec` is a **chunk** of the chain so one read fits the connector's time limit: `max_side` 25 and either up to 6 stored expiries, the stalest first, or (nothing stored yet) `expiries: null` + `max_expiries: 6` (§3.2). Symbols in a member back-off (below) are skipped.
2. `fetch(127.0.0.1:9224/chain2?symbol=..&spec=..)` with the spec **passed unchanged**, page timeout **160 s** (~~≤ 120 s~~; the connector's own limit is 150 s, §5.2). A partial answer is contributed like any other.
3. `POST /options/data/contribute` `{symbol, spot, mdt, rows, connector_version, client_as_of}` → server validates (§2.4), upserts (`source="member"`, `user_id`), returns `{stored, dropped, as_of}` and frees this member's own lease. **Any** contribution for a basket ticker re-screens the list after a **5 s debounce** (held back while a trade is open, run when it closes); the basket refreshes **only its rows** (`GET /options/basket?part=rows`), so the Add / Import inputs and the chosen sort (kept in page state + localStorage and sent on every refresh, add and remove) survive.
4. Once per symbol per day (when `history_done` is False, i.e. Hermes has not done it yet): `GET /underlying` from the connector → `POST /options/data/contribute_history` `{symbol, bars, iv_series}` → checked by **`opt_store.validate_history`** (bars required, weekday dates not after today, positive closes, high ≥ low, volume ≥ 0, every close inside the spot band around IBKR's reference — wider for older bars; IV points outside 0.1–1000 dropped) → only the clean points go to `opt_underlying_daily` (source `member`) + `recompute_underlying`. A no-op while history is on file: Hermes's, or a member's filed in the last 7 days; Hermes's own pull replaces member rows inside the span it covers (R7).
5. Wait 10 s, repeat. Pauses when the tab is hidden (`document.hidden`).

**A read that fails is reported, and the ticker backs off (review R20).** An error answer, a `busy` answer, a timeout (AbortError at 160 s) or a read with no rows → `POST /options/data/failed {symbol, error}` → `{ok, symbol, backoff_s}`. The route (only for a ticker in the member's own basket; its own rate bucket) calls `opt_store.report_failure(symbol, user_id, error, db=db)`: the member's lease is released, the ticker is skipped by **every** member's loop for **10 min, doubling per consecutive failure up to 2 h** (a failure while already backed off does not double it again), cleared by the next member write with usable rows; an `opt_refresh_log` row (`kind` member, 0 contracts, the error) records it and `freshness` ignores it. Not reported: a network `TypeError` (no connector) or a pill that left green during the read — the lease then lapses after 240 s. A contribution the server **refuses** (400) for a ticker in the member's basket already counts as a failed read on the server (`backoff_s` in the 400 answer; the page does not report it again); a 429 (rate limit) frees the member's own lease. Bodies are read only after the sign-in check and refused over 5 MB (R26).

Plus on demand: the trade detail's **"Refresh these legs live"** → `chain2` with an explicit `expiries` list and a narrow strike window → contribute (`kind: "trade"`) → re-render the detail. *Review (R6, R28):* it pauses the background loop while it runs (saying so when it waits for the read in progress), and a `trade` refresh never counts as the ticker's freshness.

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
| ~~`max_leg_spread`~~ | ~~0.50~~ | ~~widest bid/ask per leg ($)~~ — moved into every strategy (review R10, below) |
| `max_leg_spread_pct` | 25 | widest bid/ask per leg, % of its mid (still shared: every strategy) |
| `monthly_only` | False | monthly expiries only |
| `max_age_h` | 24 | ignore quotes older than this many hours — **market time** (review R9, §8): the clock stops while the US market is closed |
| `per_ticker` | 3 | best N trades per ticker in the list |

(The contract had `earnings_rule` here; it was moved into every strategy at build — below.)

**The bid/ask $ cap is per strategy (review R10).** `max_leg_spread` sits in every strategy block, 0 = off: **$0.50 on `bull_put`, `bear_call`, `iron_condor`** (the user's v4.117 band for selling premium — the credit is small, so the gap is a large part of it) and **0 (off) on the other seven**, whose deep or long-dated legs cost $20–$150 on a larger stock: a flat $0.50 is under 1% of such a price and blocked every LEAPS on a stock above ~$110. The shared 25%-of-price rule stays on for all ten. A stored `shared.max_leg_spread` (a v1 row, or a v2 row saved before the move) is lifted on read into the three premium-selling strategies only (`SPREAD_FROM_SHARED`; a strategy's own value wins), so carrying it onto LEAPS / diagonal / calendar never brings the block back. The panel's number inputs step on the same grid as their min AND their house default (R11: the arrows no longer move 0.50 to 0.51); a value clamped to its bounds is written back into its box (`options:rules-clamped`) and its warning stays until that field is saved again (R27).

**The earnings rule is per strategy** (`earnings_rule` in every strategy block, a `choice`): `none_inside` (no report on or before the LAST expiry), `short_leg` (no report on or before the SOLD leg's expiry — the near leg of a diagonal / calendar; on a one-expiry strategy it acts exactly like `none_inside`), `allow` (reports ignored). Both "no earnings" choices also skip a stock whose next earnings date is unknown. Defaults, and why:
- `none_inside` — every one-expiry strategy (`buy_call`, `buy_put`, `bull_call`, `bear_put`, `bull_put`, `bear_call`, `iron_condor`): a report can gap the stock through the strikes overnight.
- `allow` — `leaps_call`: a 9–18 month call always spans reports; bought instead of the stock, it is meant to hold through them as the shares would.
- `short_leg` — `diagonal_call`, `calendar`: keep reports out of the sold near leg's life; the bought far leg is meant to hold through a report (a report between the two expiries is a common calendar).
A v1 row, or a v2 row saved before the move, that carries a `shared.earnings_rule` is lifted on read into the strategies whose default is `none_inside` (`_lift_shared_earnings`; the stored `schema` stays 2).

**per strategy** (shown under the shared block; `iv_rank_min/max` and `earnings_rule` on every strategy):
- `buy_call`, `buy_put`: `delta_lo` .60 `delta_hi` .70 (|delta|), `dte_lo` 30 `dte_hi` 60, `theta_pct_max` 1.0 (daily decay ≤ % of premium), `iv_rank_min` 0 `iv_rank_max` 50, `earnings_rule` `none_inside`.
- `bull_call`, `bear_put`: `long_delta_lo` .60 `long_delta_hi` .70, `short_delta_lo` .25 `short_delta_hi` .35, **`width_atr_lo` 1.0 `width_atr_hi` 6.0** (the contract's .5–2.0 could never list a trade: a bought .60–.70 delta and a sold .25–.35 delta at 30–60 days sit about 3 to 5.5 ATR apart on ANY stock — the gap grows with √days and ATR scales with the stock's own volatility — so 1.0–6.0 covers it with room either side), `debit_pct_max` 60 (debit ≤ % of width), `dte_lo` 30 `dte_hi` 60, `iv_rank_min` 0 `iv_rank_max` 70, `earnings_rule` `none_inside`.
- `leaps_call`: `delta_lo` .70 `delta_hi` .85, `months_lo` 9 `months_hi` 18, **`extrinsic_pct_max` 25** (time value ≤ % of the premium; the contract's 10 could never list a trade — a 9–18 month call at delta .70–.85 carries ~18–85% time value at 4% rates and IV 20–60%, the deep end ~18–32%), `iv_rank_min` 0 `iv_rank_max` 50, `earnings_rule` `allow`.
- `diagonal_call`: `long_delta_lo` .70 `long_delta_hi` .80, `long_dte_lo` 180 `long_dte_hi` 365, `short_delta_lo` .20 `short_delta_hi` .30, `short_dte_lo` 30 `short_dte_hi` 45, `debit_pct_spot_max` 25 (net debit ≤ % of stock price), `iv_rank_min` 0 `iv_rank_max` 60, `earnings_rule` `short_leg`.
- `bull_put`, `bear_call`: `short_delta_lo` .20 `short_delta_hi` .30, `width_atr_lo` .5 `width_atr_hi` 1.5, `credit_pct_min` 25 (credit ≥ % of the max loss), `dte_lo` 30 `dte_hi` 60, `iv_rank_min` 30 `iv_rank_max` 100, `earnings_rule` `none_inside`.
- `iron_condor`: `short_delta_lo` .15 `short_delta_hi` .20, `wing_atr_lo` .5 `wing_atr_hi` 1.5, `credit_pct_min` 30, `dte_lo` 30 `dte_hi` 45, `iv_rank_min` 50 `iv_rank_max` 100, `earnings_rule` `none_inside`.
- `calendar`: `delta_tol` .05 (strike within this of delta .50, calls), `front_dte_lo` 20 `front_dte_hi` 30, `back_dte_lo` 50 `back_dte_hi` 70, `front_iv_ge_back` False (require near IV ≥ far IV), `iv_rank_min` 0 `iv_rank_max` 60, `earnings_rule` `short_leg`.

Storage is sparse (only the fields a member changed), so a later change to a default — like the two above — reaches every member who never touched that field.

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

`Candidate`: `{"id"` (stable: `sym|strategy|expiry1|right1|strike1|...`), `"symbol"`, `"strategy"`, `"legs": [{"expiry","right","strike","side" ("buy"|"sell"),"qty","bid","ask","mid","iv","delta","oi","volume","as_of","source","source_user_id","mdt"}]`, `"dte"` (nearest leg), `"net"` (per share, + = credit, − = debit, at mids), `"net_natural"` (at bid/ask), `"max_profit"`, `"max_loss"` (per contract $, None = unlimited/undefined), `"breakevens"`, `"pop"` (probability of profit at expiry: credit/debit verticals & condor from the breakeven via a lognormal with the legs' IV; singles/LEAPS from delta of breakeven ≈ same lognormal), `"ror"` (max_profit / max_loss), `"score"` (sort key, per family below), `"metrics"` (family extras: `credit_pct`, `debit_pct`, `width`, `theta_pct`, `extrinsic_pct`, `short_delta`, ...), `"liquidity"` (`oi_min`, `spread_max`, `spread_pct_max`), `"data"` (`as_of_oldest`, `age_min`, `wall_age_min`, `sources`: e.g. `["hermes·live", "member:Kui·live"]`), `"underlying"` (`spot`, `iv_rank`, `iv30`, `hv20`, `atr14`, `earnings_date`)}.

**Quote age is MARKET time (review R9).** `opt_screen.market_now(now)` is `now` while the US regular session is open, otherwise the last session's 16:00 ET close (`clock.us_session_open` / `clock.last_session_close`): the clock stands still over nights, weekends and NYSE holidays. `market_age_min(as_of, now)` is a quote's age against it — a quote taken at or after the last close is 0 until the next open; an older one keeps the age it had at the close. `data.age_min` is the market-time age of the oldest leg (the column, the sort, and what `max_age_h` filters on); **`data.wall_age_min`** is the plain clock age (the Data column's tooltip shows both). `detail()` uses the same clock. In a session the clock is the wall clock: Friday-evening quotes are 61.5 h old at 10:00 ET on Monday and drop under the 24 h default until fresh reads arrive (by design, §12 open points).

Pipeline per ticker: **stock filters** (price, stock volume, IV rank range, earnings data present when `none_inside`) → **expiry window** (DTE, monthly_only, earnings rule per expiry) → **enumerate** the family's structures → **leg filters** (OI, volume, spread $ (the strategy's own `max_leg_spread`, §7) and %, quote age ≤ `max_age_h` in market time) → **family rules** → **score**, keep `per_ticker` best. Every removal increments the funnel counter of the rule that removed it (first failure only), so "why so few?" is answerable.

**Why a ticker lists nothing (review R8).** A ticker that passed the stock filters but keeps no trade gets the reason **"no trade passes: the last rule in the way is <label>"**: the trade rules are walked backwards through the pipeline (family rules → leg rules → band rules) and the first with a non-zero count is named — every trade it removed got past every earlier rule, so loosening it alone lets them through. A band rule (they prune the most, by far) is named only when nothing got past the bands. (The build named the biggest counter, which was always the delta band.) Expiries: "no expiry passes: <the last expiry rule that removed any>". The page shows the reason verbatim. When a ticker has expiries inside the window but every stored quote there is older than the results route loads, the page says instead "its stored quotes in the N-M day window are all too old - waiting for a fresh read" (only when the reason is about trades or expiries — a stock-filter reason such as "earnings date unknown" stays).

Families:
- **single** (`buy_call`/`buy_put`): each call/put with |delta| in band; `theta_pct` = |theta| / mid × 100 ≤ max. Score: lowest `theta_pct`, then delta closest to band middle.
- **debit_vertical** (`bull_call` calls / `bear_put` puts): long leg in long band; short leg further OTM in short band, same expiry; width in `[width_atr_lo, width_atr_hi] × ATR`; debit ≤ `debit_pct_max`% of width. max_profit = (width − debit) × 100, max_loss = debit × 100. Score: ror × pop.
- **credit_vertical** (`bull_put` puts / `bear_call` calls): short in band; long further OTM, width in ATR band; credit ≥ `credit_pct_min`% of (width − credit). max_profit = credit × 100, max_loss = (width − credit) × 100. Score: credit/(width − credit) × pop.
- **leaps**: calls with DTE in `[months_lo, months_hi]` × 30.44 days, delta in band, extrinsic = mid − max(0, spot − strike); `extrinsic_pct` = extrinsic / mid × 100 ≤ max. Score: lowest extrinsic_pct.
- **diagonal**: long call (long band & DTE) × short call (short band & DTE, strike > long strike, earlier expiry); net debit ≤ `debit_pct_spot_max`% of spot. max_loss = net debit × 100 (approximation stated in the UI); max_profit from `payoff` at the short expiry. Score: lowest debit per unit of long delta.
- **condor**: put credit vertical × call credit vertical, same expiry, both shorts in band, wings in ATR band; total credit ≥ `credit_pct_min`% of (widest wing − credit). Score: credit/(wing − credit) × pop.
- **calendar**: call strike nearest delta .50 within `delta_tol` at the front expiry; same strike at a back expiry in its window; optional `front_iv_ge_back`; debit = back mid − front mid > 0. max_loss = debit × 100; max_profit from `payoff` at the front expiry. Score: lowest debit / (back − front DTE).

Payoff for the detail: `payoff.build(legs, strategy=..., spot, atr, as_of=...)` (existing) — `payoff.py` must stop importing `strategy_rules` / `option_prefs`: it imports `opt_rules.LABELS` / `FAMILY` instead.

Performance: screening runs on request over the member's basket (≤ 60 tickers × ≤ 1,500 contracts). Load each chain once per request (one query per symbol — or one `IN` query); enumerations are bounded by bands. Target < 1.5 s for 30 tickers on Hermes. *Review (R25)* — the build loaded every stored contract of every basket ticker through the ORM (~4 s CPU, ~140 MB per request, after every contribution and rule edit). Now `GET /options/results`: skips loading the chain of a ticker that already fails the first stock filters on its stored facts (price, 20-day volume, IV rank range); loads the rest with `opt_store.chain_view(db, sym, dte_min=, dte_max=, max_age_h=max_age_h + 96)` — only the strategy's expiry window (the union of its `*dte_lo/hi`, LEAPS months × 30.44) and a coarse wall-clock age pre-filter (+96 h so a weekend never empties a chain; the screener applies the exact market-time age); passes every stored expiry the load left out as an empty stub, so the funnel counts and every reason stay what a full load would give. `chain_view` reads the needed columns with one Core select and caches the result in-process, keyed by the symbol's newest `opt_refresh_log` id (every write changes it), 5 min TTL.

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
- `GET /options/data/next` · `POST /options/data/contribute` · `POST /options/data/contribute_history` (JSON) · *review:* `POST /options/data/failed` `{symbol, error}` → `{ok, symbol, backoff_s}` (§6). `GET /options/basket?part=rows` returns only the rows container (the refresh after a contribution).
- The selected strategy is remembered per member (`localStorage th.options.strategy`, default `bull_put`).

Templates (new names — no collision with the files Part G deletes): `options.html` (rewritten), `_opt_basket.html`, `_opt_rules.html`, `_opt_results.html`, `_opt_trade.html`, `_opt_status.html`, `_opt_connector.html`; the payoff uses the existing `_payoff_chart.html`. All scroll areas inherit the invisible-until-hover scrollbars (`base.html`). Every rule field shows its label with the `help` as a tooltip. The data column shows e.g. "12 min · Hermes live" or "2 min · Kui (live)"; > `max_age_h` never appears (filtered); amber when older than 60 min in RTH. *Review:* the age shown is the market-time age; the tooltip adds the wall-clock age (`data.wall_age_min`) and the UTC time (§8). Failed rule saves, resets and basket edits say so (htmx `responseError` / `sendError`, R24); an opened trade sticks to the left edge at the visible width on phones (R30).

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

1. `git pull --ff-only`; `pip install -r app\requirements.txt` (venv — `ib_insync` is in it); restart the web app (the canonical script).
2. `schtasks /Change /TN TST-Options-Nightly /DISABLE` (the Cboe job is gone).
3. `powershell -ExecutionPolicy Bypass -File deploy\setup_options_collector_task.ps1 -StartNow`. **Re-run it after any pull that changes `opt_collector.py`, `th_ibkr.py` or `options_collector.py`** (or `opt_store.py` / `models.py`, which it imports) — the web-app restart does not touch the collector. Re-running it also re-registers the task's action (since the review: python started directly with `--log-file`, no `cmd.exe` / `>>`); the old `logs\options_collector.log` is kept and becomes the first file the rotation rolls over.
4. `app\.env`: `TST_IBKR_PORT=4002` (optional; the probe finds it). Delete the dead `TST_OPTIONS_SOURCE` / `_FALLBACK` / `TST_ALPACA_FEED` / `TST_IV_SEED_IBKR` / `TST_IBKR_PYTHON`.
5. Watch the strip: on a weekday in US hours "Hermes: waiting · Gateway off by design" is correct (§4.0); after 20:10 ET "Hermes: end-of-day pass"; over the weekend "Hermes: reading history".

CLAUDE.md clientId table: **89 — Options collector (`dashboard_tst/deploy/options_collector.py`) — Hermes**; 86 stays the members' connector default; 87/88 (v4.131 seeder path) retired from the Options page (87 remains for the legacy screener's manual seeder).

## 12. Deviations recorded at build (v4.133)

What the build parts changed from §2–§9 on purpose, each for a stated reason. The code is the truth; this list says where it differs from the text above.

**Data layer (Part A — `opt_store.py`, `models.py`, migration `3b26d60468a0` off `f4a5b6c7d8e9`)**
1. `opt_underlying` and `opt_underlying_daily` carry an integer `id` primary key; uniqueness is the named constraints `uq_opt_underlying_symbol` (`symbol`) and `uq_opt_und_daily` (`symbol`, `on`). Every v2 table has a surrogate key — portable, and the ORM upserts stay query-then-update.
2. `freshness` reads the newest `opt_refresh_log` row **that wrote at least one contract** (`n_contracts > 0`): an empty or failed fetch (a closed feed, an error row) never makes a symbol look fresh. Ties on `as_of` go to the later row; it also returns `kind` and `source_name`.
3. A member's `as_of` is still the server's receive time, but `upsert_quotes` keeps a passed `as_of` for `source="member"` when it lies within the last **120 s** (`MEMBER_ASOF_SLACK_S`; the route passes its own receive time) — never older, never in the future; otherwise server now. The equal-`as_of` tie-break is the full order live > frozen > delayed > delayed_frozen. (Review R2: delayed member data is then back-dated 15 min.)
4. Validation extras (§2.4): also rejected — a symbol that is not a valid ticker, `rows` not a list, an unknown market-data type, and a payload whose rows ALL drop ("no usable contracts"). Extra per-row drop reasons `bad_row` (not an object), `bad_number` (a field that is not a finite number), `negative_size` (oi / volume < 0); `drop_reasons()` gives the breakdown. `contribute_history` (not in §2.4) checks: symbol in a basket, ≤ 800 points each, the last close within 20% of a stored spot at most 3 days old, and is a no-op once history is on file. (Review R1, R7: the 20% rules are replaced by the IBKR-anchored band, and `contribute_history` by `validate_history`.)
5. `check_rate(user_id, symbol, bucket=)`: the 20 s per-symbol limit is kept per bucket — `chain`, `history`, `trade` — because the page posts a chain and its history for one symbol back to back, and "Refresh these legs live" must not wait behind the background loop. The 30-a-minute cap is shared.
6. `next_for_member(db, user, *, now=None, min_age_s=60)` returns ONE `{symbol, spec}` or None (the contract: a list, `n=1`) and also skips a symbol refreshed in the last `min_age_s`, so two members' connectors do not re-read one chain back to back. `GET /options/data/next` adds **`history_done`** so the page knows when to post the history (§6 step 4). The lease is released when the contribution arrives (`release_lease`). (Review R5, R20: the lease is 240 s and per member, symbols in a back-off are skipped, and the spec is a chunk.)

**Fetch library (Part B — `bridge/th_ibkr.py` 2.0)**
7. `fetch(ib, symbol, spec)` added — `chain_defs` + `spot` in parallel → `plan_spec` → `quote`: the one read both the collector and the connector's `/chain2` do. `plan_spec(defs, spec)` builds a `plan` from a JSON spec. `quote` takes an optional `spot=` and also returns `planned`, `unknown` (contracts IBKR does not list — that verdict cached 6 h, `NEG_TTL`; 3 days since the review, R17), `priced`, `with_greeks`, `waves`, `mdt_id`; `fetch` adds `spot_mdt`, `n_expiries`; `spot` takes `mdt_pref`.
8. **Dead contracts are omitted**: a row IBKR sent nothing for (no bid, ask, last or delta) is left out of `rows` (`filled < requested`), so a dead feed never overwrites good shared data with blanks.
9. **`OPT_TICKS = "101"`** (open interest, ticks 27/28), not `"100,101,106"`: 100 and 106 are UNDERLYING generic ticks (the stock's option volume / IV); on an option contract they risk error 321, which empties the whole chain. Day volume and the model greeks / IV come with the default tick set; 101 is the one generic tick the 1.x bridge proved on option contracts.

**Collector (Part C — `opt_collector.py` 1.1)**
10. **`MIN_CYCLE_S = 600`**: a new RTH cycle starts at most every 10 min — a small basket would otherwise re-read non-stop and hold market-data lines the login shares with the user's own TWS.
11. **Missed-EOD catch-up**: the EOD day is today after 16:15 ET, the previous trading day before today's open, the last trading day on a weekend / holiday, so a missed evening runs before the next open whenever a Gateway is up (on Hermes, Friday's over the weekend). Known limit: on a weekday the supervisor closes the Gateway as soon as its top-up succeeds; an EOD pass not finished by then is not resumed for that day (the next evening's pass is for the next day). Watch for "EOD <day> done" on the strip tooltip / tray.
12. **The `waiting` state** (not in §2.2's list; the status row's `state` column holds it): the Gateway is down by the supervisor's design (§4.0) — `wait_reason` `blackout | starting | closed` and `wait_until` (state file only), a try every 60 s, no error recorded. Historical requests are gated on the supervisor's state ("history waits for tonight's ingest top-up", `history_waiting` in the state file); first chain reads are never deferred; deferred increments are caught up with a duration that covers the gap. CLI: exit code 3 = `--history` deferred; `--ignore-ingest` lifts the wait for a one-off run.
13. Connecting is the probe (no bare socket probe — the Gateway logs "client disconnected before version was sent"); a real outage backs off 60 s doubling to 5 min (the contract: a flat 60 s).

**Page (Part F — `routes/options_page.py`)**
14. Extra route **`GET /options/payoff?strategy=&id=&units=`** — the payoff pane alone, so `_payoff_chart.html`'s $ | R toggle re-requests just the chart.
15. `POST /options/data/contribute` accepts **`kind: "trade"`** — the trade detail's "Refresh these legs live", logged as `kind="trade"` in `opt_refresh_log`, with its own rate bucket (item 5).
16. The v1 nav badge (`/options/badge`: positions at an exit line) went with the Positions tab — no route, no poller in `base.html`.
17. The strip reads the collector's `waiting` as neutral grey ("Hermes: waiting · Gateway off by design", the reason and until-when in the tooltip), never as "gateway down" — fixed at integration, since `waiting` also writes `gateway_ok = False`.

**Rules (Part E)** — 18. The earnings rule moved into every strategy with a third choice `short_leg`; the debit-vertical width band is 1.0–6.0 ATR; the LEAPS time-value cap is 25% (all in §7, each with its reason).

**Left open at build (Part G)**
19. ~~The Telegram ideas push island (§1) is still in the tree — `telegram_push.py`, `strategy_rules.py`, `option_words.py`, `option_prefs.py`, `tests/test_telegram_push.py` (28 tests) and `option_store`'s v1 helpers. Nothing in the app calls it; remove it in one follow-up.~~ **Done before release:** those four modules, `tests/test_telegram_push.py` and the v1 golden fixtures (`tests/fixtures/options/{expected, isrg, lrcx}.json`, `regen.py`) are deleted; `option_store.py` is down to `basket_universe` + `prune`.
20. Still open: `app/config.py` still defines `options_source` / `options_fallback` / `alpaca_feed` (nothing reads them); `app/requirements.txt`'s comment on `ib_insync` still names the v4.131 seeder — the collector is now why it is there.

### Review fixes (2026-10-09/10)

A six-lens review of the v4.133 build ran before release; four fix groups (data layer, screener + rules, IBKR side, page) applied the fixes below, then an integration pass made them agree. `#n` is the review's finding number. The code is the truth; the sections above were brought in line.

**Data layer (`opt_store.py`)**
- **R1 — the spot band is anchored to IBKR and ticker-relative** (#1, high). The build compared a member's spot with the stored spot — which the previous member post had just written — at a flat 20%: one member could walk it anywhere (14 posts took 100 to 1,142), and a real > 20% gap (an earnings move) locked every member out for the session. Now `spot_reference(db, sym)` takes the newest (by ET date) of three IBKR sources a member cannot move: the stored spot when Hermes wrote it, the spot on Hermes's newest surviving `opt_quote` row (`und_price`, the fallback for a new ticker with no Hermes history yet), and the newest Hermes daily close. `spot_band(ref, n) = max(15%, 4 × σ_daily × √(trading days since the reference + 1))`, `σ_daily = (iv30 or hv20 or 40) / 100 / √252`, trading days on the NYSE calendar (`clock`). No reference → accepted; a member's spot is never the reference.
- **R2 — delayed member data is back-dated 15 min** (#2 / #21). Member writes were stamped with the receive time, so a delayed read could replace a live quote taken a minute earlier. `delayed` / `delayed_frozen` member quotes, spot and log row are now filed at receive time − 15 min (`DELAY_S` 900); Hermes stamps are untouched; `next_for_member`'s 60 s min-age adds the 15 min back so a delayed read is not re-asked at once.
- **R3 — mid recomputed + plausibility drops** (#3). A posted `mid` is ignored (mid = (bid + ask) / 2); a call's bid / ask above spot × 1.05, a put's above its strike × 1.05, or an ask more than max($0.25, 2% of spot) under intrinsic drop the row (`price`); so does a delta whose sign contradicts the right (`delta`).
- **R4 — unlisted / invented contracts cannot pile up** (#4). Expiries on a weekend or more than 1,100 days out and strikes off the $0.50 grid drop (`expiry`, `strike`); `prune_v2` deletes quotes nobody refreshed for **7 days** (`quotes_stale` — a listed contract is re-read at least every EOD pass); `chain_view` is **windowed** (`dte_min`, `dte_max`, `max_age_h`) and **cached** (R25).
- **R5 — leases fit a read and belong to one member** (#17). The 60 s lease expired before a chain read finished, so two members read the same chain, and `release_lease` dropped another member's lease. Now `LEASE_S` 240 s (longer than a worst-case chunked read) and `release_lease(symbol, user_id)` drops only the caller's own.
- **R6 — freshness is honest and cheap** (low ×3). A narrow "Refresh these legs live" (`kind` trade) no longer makes the whole ticker look fresh — for the badge, the member loop or the collector's skip; `freshness` is one bounded query (`LIMIT 1` per symbol over the `(symbol, as_of)` index) instead of a 30-day scan (1.7 s at 360k rows).
- **R7 — member history is validated, and Hermes replaces it** (#5). `contribute_history` checked only the last close (nothing when `bars` was empty), and invented days survived Hermes's later pull. Now `validate_history(db, payload, user_id=)` (§6 step 4) runs first and the route files only the clean bars / IV points; `upsert_daily(source="hermes")` clears member bar columns / iv30 inside the span Hermes covers where Hermes sent nothing and deletes a member row left empty. Also: `history_done` is set only on real data (R16).

**Screener + rules (`opt_screen.py`, `opt_rules.py`)**
- **R8 — the no-trade reason names the last rule in the way** (#6): "no trade passes: the last rule in the way is …" (§8); the build always named the delta band.
- **R9 — quote age is market time** (#7): `market_now` / `market_age_min`, `data.age_min` market-time, `data.wall_age_min` wall-clock (§8). Weekend and holiday quotes stay current; in-session ageing is wall clock by decision (open point below).
- **R10 — the bid/ask $ cap is per strategy** (#8): `max_leg_spread` $0.50 on bull put / bear call / iron condor, off on the other seven; the shared 25%-of-price rule stays; stored shared values lifted on read into the three (§7).
- **R11 — number inputs step on the grid of their min and default** (low; `options_page._input_step`).

**IBKR side (`th_ibkr.py`, `ibkr_bridge.py`, `opt_collector.py`, `deploy/`)**
- **R12 — big chains are read in chunks that fit, and a long read returns what it has** (#16 / #19, high). A large-cap's default window (~2,000 contracts, 45–50 waves) took longer than the connector's 110 s cap; the read was cancelled with every row thrown away and the member's loop asked for the same ticker forever. Now `plan(max_expiries=)`, `quote(deadline=)` / `fetch(deadline=)` with `partial` / `attempted` (§3.1), `/chain2` timeout 150 s with the read's deadline 15 s under it, and the `busy` answer (§5.2). The timeout text no longer tells members to lower the lines.
- **R13 — no leaked market-data lines** (#20): a wave's (and the spot's) lines are released in a `finally` that never awaits — unpaced cancels charged to the message rate afterwards; a cancelled wait for a historical slot gives the slot back (`_hist_slot`).
- **R14 — cross-site requests without an Origin do no work** (#0; §5).
- **R15 — loopback origins: dev ports 8000–8099 only** (low; §5).
- **R16 — first-time history is filed only when IBKR really returned it** (#18, high). ib_insync answers a failed historical request with an EMPTY list; the build marked such a symbol `history_done` with no data, for good. Now `history_done` needs ≥ 20 daily bars AND ≥ 20 IV points (`HISTORY_MIN_POINTS`), or a young listing confirmed by a pull on a LATER ET day returning at least as much again; an empty pull is a failure retried on the history back-off (30 min doubling to 6 h). The connector's `/underlying` treats an empty history as an error and never caches it.
- **R17 — `NEG_TTL` 3 days** (low): at 6 h every evening's EOD pass re-asked IBKR about ~2,000 non-existent union strikes per large name.
- **R18 — the collector log stays small** (low). Throttling: a persisting `waiting` / `error` state is logged when it begins or changes, then at most every 30 min (`STATE_LOG_EVERY_S`); `IbLogNoise` drops ib_insync's per-attempt connect chatter and its per-strike "Unknown contract" / "Error 200" lines (pacing, HMDS and farm lines are kept). Rotation (integration): `deploy/options_collector.py --log-file PATH` logs through a `RotatingFileHandler`, **5 MB × 5 files**, re-attached after `init_db`'s Alembic `fileConfig` (which also adds a stderr console, dropped in log-file mode); a rollover Windows refuses because a `Get-Content -Wait` tail holds the file falls back to copy-and-truncate. `setup_options_collector_task.ps1` runs python directly with `--log-file logs\options_collector.log` — no `cmd.exe`, no `>>`.

**Page (`routes/options_page.py`, `options.html`, `_opt_*.html`)**
- **R19 — a 1.x connector gets its own orange "out of date" pill** (#9, high; §5.4): it showed amber "TWS not connected" while TWS was connected.
- **R20 — a ticker that cannot be read never holds the loop** (#10, high): `POST /options/data/failed` → `report_failure` back-off; refused (400) contributions count as failed reads; a 429 frees the member's own lease; the spec is passed unchanged; the `/chain2` fetch waits 160 s; member reads are chunked (§6).
- **R21 — the list re-screens after a refresh** (#11): any contribution for a basket ticker re-screens after a 5 s debounce; "its stored quotes … are all too old - waiting for a fresh read" (§8).
- **R22 — a refresh no longer wipes the basket's inputs or sort** (#12): rows-only swap (`part=rows`); the sort is kept in page state + localStorage and sent on every refresh, add and remove.
- **R23 — a browser-blocked connector says so** (#13): red "blocked by the browser" when Local Network Access is denied (§5.4).
- **R24 — failed saves, resets and basket edits say so** (#14): `htmx:responseError` and `htmx:sendError` for the rules panel (rose "Not saved / Not reset (HTTP n)"), the basket (a toast) and the fragments.
- **R25 — the results load only what can matter** (#15, high; §8 Performance): stock-filter pre-skip, the strategy's expiry window, `max_age_h + 96` h wall-clock pre-filter, empty stubs for the expiries left out, `chain_view`'s cache.
- **R26 — contribution bodies are read after sign-in and capped** (low): over 5 MB refused (by `Content-Length`, or while streaming one without it), JSON parsed off the event loop.
- **R27 — a clamped value is written back into its box** and its warning stays until that field is saved again (low).
- **R28 — "Refresh these legs live" pauses the background loop** while it runs (low ×2); with chunked background reads the wait behind a running read is short.
- **R29 — the basket and help texts tell the truth about Hermes** (low): during US hours only members' connectors read; Hermes reads new tickers in the evening (after 20:10 ET) and at the weekend.
- **R30 — an opened trade fits a phone** (low): it sticks to the left edge of the scrolled table at the visible width (`fitTradeBox`).

**Integration** — the connector's "busy" answer now carries `"busy": true` (`ConnectorBusy`), as the shared contract between the server, the page and the connector says; the log rotation of R18.

**Open points after the review**
- #2 (low, not assigned to a fix group): `install_bridge.ps1` unblocks (`Unblock-File`) every file in the folder it runs from, and the connector zip has no top-level folder, so "Extract Here" in Downloads puts the files — and the folder the Startup shortcut runs `py -3.12 ibkr_bridge.py` from — straight into Downloads. Fix: unblock only the shipped names and put the zip's entries under `TradeHunter-IBKR-Connector/`.
- In a session the age clock is the wall clock (R9): on a Monday, with the Hermes Gateway in its blackout, Friday-evening quotes are ~61 h old by 10:00 ET and fail the 24 h default, so a member without a connector lists nothing until members' reads arrive (the reason says "quote older than 24 h"). Counting only open-market hours would change what `max_age_h` means; not done.
- A `busy` answer is reported as a failed read, so the ticker backs off for every member's loop for 10 min even when the cause was local (a second tab, a trade refresh on the same connector).
- `σ_daily` for the spot band reads `opt_underlying.iv30` / `hv20`, which a member's history can set before Hermes's (bounded by `validate_history`'s 0.1–1000 range); until Hermes's weekend pull, a member posting a huge IV widens the band on that ticker.
- Rows a pre-review build marked `history_done` with no data are not repaired automatically: on Hermes run `deploy\options_collector.py --history SYM` for them. The young-listing memory lives in the collector process (a restart between the two days delays filing by a day).
- The collector passes no deadline to `quote` (its 1,800 s ceiling still cancels a read whole, now without leaking lines; no measured chain gets near it). `parse_spec` still accepts `max_side` up to 400 and 80 expiries from allow-listed origins (the deadline caps the cost at ~135 s a request).
- A member without the `options` menu grant gets a 303 from the router gate, which an htmx request would follow and swap into its target (an expired session is not affected).
