# Options Screener — a Barchart-style screener on Massive data (v4.136)

Status: **BUILT in v4.136 (2026-10-10)** — this file is the contract (§11 = what the build
decided where it was silent). Read it before touching any
file named `scr_*`, `screener/*`, `options_page.py` or `options.html`.

## 0. What the user asked for

> *"this is the screener i see from barchart.com
> https://www.barchart.com/options/options-screener — I want to build exactly this screener in
> which user can set his own preference and setting"* · *"with the screener you will see the
> result"* · *"i need the result too"*

Decisions (2026-10-10, the user's answers):

| Question | Answer |
|---|---|
| Which stocks are searched | **"it should be based on what massive API available"** → the whole US options market Massive covers (every underlying with listed options). |
| Bid / ask (Options Starter has none) | **"ignore the bid ask in the filter"** → no bid / ask / spread filters or columns. Every leg price is the **estimated price** from the contract's own IV (`opt_massive.model_price`), marked *est.*; the last trade (`day.close`) is shown as **Last**. |
| Strategy screeners | **"Include strategy screeners now"** → all 33 Barchart screeners in this build. |

TradeHunter only FINDS the trade; members check the live price and enter in IBKR TWS.

## 1. What Barchart has (read 2026-10-10)

**Two tabs.** *SET FILTERS*: the screener's default filters (each a card: checkboxes,
range boxes with preset chips, or a comparator `greater than / less than / equal to /
between`), **Add a Filter** (group → field), *Clear All Filters*, *See Results*, saved
screeners (load / save / organize). *RESULTS*: result count, a sortable table, *views*
(Main View, Filter View, …), *Flag Earnings*, *refresh*, *download* (CSV), *Save Screener*,
*Save as Watchlist*, a *Profit/Loss Chart* on strategy screeners.

**The 33 screeners** (GO TO dropdown): Options Screener · Long Call · Long Put · Covered
Call · Naked Put · Bull Call Spread · Bear Call Spread · Bear Put Spread · Bull Put Spread ·
Married Put · Protective Collar · Long Straddle · Short Straddle · Long Strangle · Short
Strangle · Long Call Calendar · Long Put Calendar · Long Call Diagonal · Short Call Diagonal ·
Long Put Diagonal · Short Put Diagonal · Long/Short Call Butterfly · Long/Short Put
Butterfly · Long/Short Iron Butterfly · Long/Short Call Condor · Long/Short Put Condor ·
Long/Short Iron Condor.

**Default filters, Options Screener:** Exchange (AMEX / NYSE / NASDAQ / INDEX) · Option Type
(Call / Put) · Expiration Date · Days to Expiration (range; < 60, 60-100, 100-150, 150-200,
> 200; Monthly / Weekly) · Security Type (Stock / Non-Common Stock / ETF / Index) · Strike
Price · Option Volume (Very Low 0-100 … Very High > 1000) · Option Open Interest (same
bands) · Moneyness (Deep OTM < -25 %, OTM -25..-5, ATM -5..+5, ITM +5..+25, Deep ITM > +25).

**Default filters, strategy screeners** (bull put shown): Exchange · Trend signal · DTE ·
Security Type · Volume Leg 1 · Open Interest Leg 1 · Moneyness Leg 1 · Volume Leg 2 · Open
Interest Leg 2 · Bid Leg 1 · Ask Leg 2 · OTM Probability.

**Result columns** (Main View):

| Screener | Columns |
|---|---|
| Options / Long Call / Long Put | Symbol, Price, Exp Date, Strike, Moneyness, Ask, %TP Ask, BE (Ask), %BE (Ask), Volume, Open Int, IV Rank, IV, Delta, Profit Prob, Last Trade |
| Covered Call | Symbol, Price, Exp, Strike, Moneyness, Bid, BE (Bid), %BE, Volume, OI, IV Rank, Delta, Return, Ann Rtn, Ptnl Rtn, Profit Prob |
| Naked Put | … same without Ptnl Rtn |
| Married Put | Symbol, Price, Exp, Strike, Ask, BE, %BE, Max Loss, Downside, Volume, OI, IV Rank, Delta, Profit Prob, Last Trade |
| Protective Collar | Symbol, Price, Exp, Short, Bid1, Long, Ask2, BE, Net Cr(Db), %Cost, Max Profit, Max Loss, Upside, Downside, Delta, Profit Prob |
| Verticals | Symbol, Price, Exp, Short/Long strikes + leg prices, BE, BE%, Max Profit, Max Loss, Max Profit%, Risk/Reward, IV Rank, Profit Prob (debit) / Loss Prob (credit) |
| Straddle / Strangle | Symbol, Price, Exp, strike(s), leg prices, BE+, %BE+, BE-, %BE-, Net Debit/Credit, IV Rank, IV/HV, Net Delta, Prob Profit (long) / Loss Prob + Max Profit Prob (short) |
| Calendars / Diagonals | Symbol, Price, Exp Leg1, Leg1, price, Exp Leg2, (Leg2), price, Net Debit/Credit, Leg1 IV, Leg2 IV, IV Skew, IV Rank, IV/HV, Net Delta, Net Vega |
| Butterflies / Condors | Symbol, Price, Exp, strikes + leg prices, BE+, BE-, Max Profit, Max Loss, Risk/Reward, IV Rank, Profit Prob (long) / Loss Prob (short) |

**Add-a-Filter groups:** Option Info · Option Analysis · Options Overview · Break Even
Analysis · Price & Volume · Technical Analysis · Profile (plus Barchart-only groups we do not
have the data for: Short Interest, Fundamentals, Income Statement, Balance Sheet, Cash Flow,
Analysts, Barchart Opinion — **left out**).

## 2. Architecture

```
Massive (Options Starter + Stocks Basic)
   │ HTTPS, one key (TST_MASSIVE_API_KEY, app\.env on Hermes only)
   ▼
deploy/screener_collector.py  --forever      (Hermes task TST-Options-Screener, venv python)
   └─ app/services/scr_collector.py          the loop: universe · market passes · stock days · IV history
        └─ app/services/scr_store.py         the ONLY reader/writer of the screener tables
             ▼
        screener DB  (TST_SCREENER_DATABASE_URL; default sqlite:///<dashboard_tst>/screener.db)
             ▲  migrations: alembic_screener/ (own env, version table alembic_version_screener)
             │
web app ─ app/services/screener/frame.py     loads the latest data into numpy arrays (background reload)
        ─ app/services/screener/{fields,screens,single,strategies,engine}.py   pure screening
        ─ app/routes/options_page.py + templates/options.html                  the page (2 tabs)
        ─ main DB table option_screens (saved screeners per member, main alembic)
```

* **Separate database.** The market tables hold ~1M contract rows replaced every pass; they
  live in their own SQLite file so the platform DB (`tst.db`) never carries that churn or
  that size. Same rules as the main DB: SQLAlchemy ORM only, portable types, schema through
  Alembic (its own environment, `alembic_screener/`, version table
  `alembic_version_screener` so it can share one Postgres later). The URL is
  `TST_SCREENER_DATABASE_URL`; on Postgres it may point at the same database.
* **Separate collector task.** `TST-Options-Screener` runs beside the basket collector
  (`TST-Options-Collector`, unchanged). Two processes, one key; Options Starter has no
  request cap.
* **numpy** is added to `app/requirements.txt` (screening ~1M contracts per request needs
  vectorised arrays; pandas is NOT used).
* The page is **live/operational** → fed by the live Massive pipeline, never parquet.

## 3. The screener database (`app/screener_models.py`, `ScrBase`)

All datetimes naive UTC. `iv` FRACTION; per-underlying IV figures PERCENT (the `opt_*`
convention).

### `scr_contract` — the latest read of every kept contract
One row per (symbol, expiry, right, strike); replaced per underlying each pass.

| column | type | meaning |
|---|---|---|
| id | Integer PK | |
| symbol | String(20) | underlying, our spelling (BRK-B) |
| expiry | String(10) | YYYY-MM-DD |
| right | String(1) | C / P |
| strike | Float | |
| weekly | Boolean | not the standard monthly (`opt_massive.is_monthly`) |
| price | Float null | **estimated** price: `opt_massive.model_price(spot, K, dte, iv, right)`; else `last` |
| last | Float null | the day bar close (last trade of the session) |
| chg_pct | Float null | day change % of the contract |
| volume | Integer null | today's volume |
| oi | Integer null | open interest (as of the prior close) |
| vol_prev | Integer null | the previous session's final volume (carried at the first pass of a new session) |
| oi_prev | Integer null | the previous session's OI (same carry) |
| iv | Float null | FRACTION |
| delta, gamma, theta, vega | Float null | Massive's greeks (delta signed) |
| last_trade | DateTime null | `day.last_updated` |
| session | String(10) | ET session date the row describes |
| as_of | DateTime | read time - 15 min |

Unique `(symbol, expiry, right, strike)`; index `(symbol)`.
**Kept**: standard contracts (`opt_massive.standard_rows`) with `oi > 0 or volume > 0`.

### `scr_underlying` — one row per underlying
symbol PK-unique · name · sec_type (`stock|etf|index|other`) · exchange
(`NYSE|NASDAQ|AMEX|INDEX|OTHER`) · spot · spot_src (`massive|parity|close`) · spot_as_of ·
prev_close · chg_pct · stock_volume · avg_vol20 · avg_vol50 · sma20 · sma50 · sma200 ·
rsi14 · atr14 · atr_pct · hv20 · hv60 (PERCENT) · hi52 · lo52 · perf5 · perf20 (PERCENT) ·
trend (`up|down|sideways|None`, the MATP `classify_trend` rule) · iv30 · iv30_prev ·
iv_rank · iv_pct · iv_hi · iv_lo · iv_n · exp_move30 (PERCENT of spot) · call_vol · put_vol ·
call_oi · put_oi · n_contracts · earnings_date · earnings_src (`nasdaq`) · bars_as_of ·
history_done (Boolean) · history_tries · history_next (DateTime) · pass_id · updated_at.

### `scr_underlying_daily` — daily history per underlying
symbol · on (YYYY-MM-DD) · open · high · low · close (adjusted) · close_raw (unadjusted) ·
volume · iv30 (PERCENT). Unique `(symbol, on)`.

### `scr_universe` — the optionable underlyings
symbol (unique) · n_contracts · first_seen · last_seen · active (Boolean).

### `scr_pass` — one row per market pass
id · kind (`cycle|eod|manual`) · session · started · finished (null while running) ·
n_symbols · n_ok · n_failed · n_contracts · requests · ms.

### `scr_status` — the collector heartbeat (single row, id = 1)
state (`starting|universe|pass|stocks|history|idle|error|stopped`) · detail · heartbeat ·
pid · version · pass_id · symbols_total · symbols_done · universe_n · universe_on ·
history_done_n · history_total · last_error · api_ok.

## 4. The collector (`scr_collector.py`, `deploy/screener_collector.py`)

One process, a tick loop (like `opt_collector`), clock from `services/clock.py` (ET,
trading days, holidays). Env: `TST_SCREENER_CYCLE_MIN` (30), `TST_SCREENER_WORKERS` (8),
`TST_SCREENER_MAX_RPS` (40), `TST_SCREENER_MAX_DTE` (1100).

1. **Universe** — daily from 07:30 ET (and at start when older than 20 h):
   `/v3/reference/options/contracts?expired=false&expiration_date.lte=<today+60d>&limit=1000`,
   cursor-paged → distinct `underlying_ticker` with counts → `scr_universe` (absent today →
   `active=False`; kept 10 days before it is dropped from passes). A failed refresh keeps
   yesterday's list.
2. **Market pass** — on trading days every `CYCLE_MIN` from 09:45 to 16:00 ET, then one
   **EOD pass** after 16:20 ET (a missed evening is caught up before the next open). Every
   active universe symbol, most contracts first, `WORKERS` threads sharing one
   `massive.Client(concurrency=WORKERS, max_rps=MAX_RPS)`:
   `chain_snapshot(sym, exp_gte=today, exp_lte=today+MAX_DTE)` (whole chain, no strike cut)
   → `standard_rows` → `estimate_spot` (stored close = last `scr_underlying_daily.close`) →
   rows as §3 → `scr_store.replace_contracts` (per symbol, one transaction; carries
   `vol_prev`/`oi_prev` when the stored rows are from an older session) →
   `scr_store.update_underlying_pass` (spot, iv30 = `option_metrics.atm_iv_by_expiry` +
   `iv30_constant_maturity` on the kept rows, call/put volume and OI, n_contracts,
   exp_move30 = iv30 × sqrt(30/365)). The EOD pass also files the day's `iv30` into
   `scr_underlying_daily` and recomputes the IV figures. One symbol failing is counted and
   skipped; auth/plan errors pause the pass (state `error`, retried every 5 min).
3. **Stock days** — after 20:00 ET (the bars are published), and at start for missing
   sessions: Stocks Basic **grouped daily**
   `/v2/aggs/grouped/locale/us/market/stocks/{date}` twice (adjusted=true → OHLCV,
   adjusted=false → close_raw) for every missing session of the last 2 years (first run
   ~2 × 504 calls at 5/min ≈ 3.5 h, in the background between passes) → only universe
   symbols filed. Then the technicals per underlying (§3) from the adjusted bars.
   Weekly: `/v3/reference/tickers?market=stocks&active=true` (+ `market=indices` names for
   index underlyings) → name, sec_type (CS → stock; ETF/ETN/ETV → etf; ADRC/PFD/WARRANT/… →
   other; indices → index), exchange (XNYS → NYSE; XNAS → NASDAQ; XASE/ARCX/BATS → AMEX;
   index → INDEX). Daily: earnings dates for the next 70 days from
   `calendars.earnings_for(day)` (Nasdaq, already used by /calendar).
4. **IV history** — between passes, for underlyings with `history_done=False`, most option
   volume first: `opt_massive.iv30_history(client, sym, bars_raw)` (bars from
   `scr_underlying_daily.close_raw`) → `iv30` into `scr_underlying_daily` → recompute
   iv_rank / iv_pct over the last 252 values (`option_metrics.iv_rank_pct`).
   `history_done` once ≥ 20 points; else retried after 30 min doubling to 24 h. ~150-300
   requests per underlying → the whole market takes ~12-20 h the first time; IV rank shows
   "-" for an underlying until it is done.
5. **Status** — `scr_status` heartbeat every tick + `state/screener_collector.json` (the
   Hermes tray reads it); log `logs/screener_collector.log` (5 MB × 5).

## 5. The screening engine (`app/services/screener/`)

Pure functions over numpy arrays; no HTTP, no DB writes.

* `frame.py` — `current()` returns the latest immutable `Frame`; a background thread
  reloads when `scr_pass` has a newer finished pass (checked at most every 30 s) and on
  first use. A Frame: contract arrays sorted by (symbol, right, expiry, strike) with
  group boundaries; underlying arrays indexed by symbol code; derived per contract at load
  (spot, dte, moneyness, intrinsic, tp, tp_pct, be, be_pct, prob_itm, profit_prob,
  vol_oi, oi_chg, oi_chg_pct, vol_chg_pct, earnings_before_exp, …); `meta` (pass id,
  finished, n_underlyings, n_contracts, data as_of). No data yet → an empty Frame with
  `meta.empty=True`.
* `fields.py` — the field registry (`Field(key, label, group, level, kind, unit, fmt,
  help, presets, choices)`); `level` ∈ `underlying | contract | leg | strategy`; `kind` ∈
  `range | choice | bool | date`. Leg fields are addressed `leg1.volume`, `leg2.oi`, ….
* `screens.py` — the 33 `Screen` definitions: key (slug as Barchart's URL tail, e.g.
  `bull-put-spread`), label, family, one-line description (bias / profit / loss like
  Barchart's blurb), legs, default filters, Main-view columns, default sort.
* `single.py` — Options / Long Call / Long Put / Covered Call / Naked Put / Married Put.
* `strategies.py` — the multi-leg screens (§6), vectorised pairing inside groups.
* `engine.py` — the API the routes call:
  * `screens() -> list[dict]` (the GO TO list, grouped by family)
  * `spec(key) -> dict` (screen meta, its default filters, every field it can filter on
    grouped for Add-a-Filter, its views and their columns)
  * `run(key, payload, *, page=1, per_page=100) -> dict` →
    `{"screen", "total", "page", "pages", "rows": [{col: value}], "columns": [{key, label,
    unit, fmt}], "truncated": bool, "ms", "data": frame.meta, "warnings": [...]}`
  * `csv(key, payload, *, limit=1000) -> str`
* **Payload** (what the page sends and what a saved screener stores):
  `{"filters": [{"f": "<field>", "op": "gte|lte|eq|between|in|is|within", "lo": n, "hi": n,
  "v": [...]}], "sort": {"col": "<col>", "dir": "asc|desc"}, "view": "main|filter|greeks|vol",
  "flag_earnings": bool}`. Unknown fields / ops are ignored with a warning, never a 500.
* Limits: at most `MAX_ROWS = 5000` sorted rows kept per run (`truncated=True` beyond);
  pairing is chunked; a run over `COMBO_CAP` candidate combinations stops with a warning
  ("narrow the days to expiration or the deltas").

### Formulas (S = underlying price, K = strike, p = est. price, T = DTE/365, σ = IV)

* Moneyness % — calls `(S-K)/S×100`, puts `(K-S)/S×100` (positive = in the money).
* Intrinsic `max(0,S-K)` / `max(0,K-S)`; time premium `TP = p - intrinsic`; `%TP = TP/S×100`.
* Break-even — long call `K+p`, long put `K-p`; `%BE = (BE-S)/S×100`.
* `P(S_T > X) = N(d2)`, `d2 = (ln(S/X) + (r - σ²/2)T)/(σ√T)`, r = `opt_constants.RISK_FREE`.
  Single leg: σ = its IV. Strategy: σ = the IV of the leg whose strike is nearest that BE.
* OTM probability: call `P(S_T < K)`, put `P(S_T > K)`.
* Covered call: Return `(p - max(0,S-K))/(S-p)×100`; Ann Rtn `Return×365/DTE`; Ptnl Rtn
  `(p + max(0,K-S))/(S-p)×100`; BE `S-p`; Profit prob `P(S_T > BE)`.
* Naked put: Return `p/(K-p)×100`; Ann Rtn; BE `K-p`; Profit prob `P(S_T > BE)`.
* Married put: BE `S+p`; Max loss `(S+p-K)×100`; Downside `(S+p-K)/(S+p)×100`.
* $ figures (max profit, max loss, net debit/credit) are **per contract** (×100).

## 6. Strategy definitions (legs; "sell"/"buy"; all legs one underlying)

| Screen | Legs | Net | BE | Max profit / loss | Prob column |
|---|---|---|---|---|---|
| Bull put spread | sell P(K2), buy P(K1), K1<K2, same exp | credit c | K2-c | c / w-c | Loss = P(S_T<BE) |
| Bear call spread | sell C(K1), buy C(K2), K1<K2 | credit | K1+c | c / w-c | Loss = P(S_T>BE) |
| Bull call spread | buy C(K1), sell C(K2) | debit d | K1+d | w-d / d | Profit = P(S_T>BE) |
| Bear put spread | buy P(K2), sell P(K1) | debit | K2-d | w-d / d | Profit = P(S_T<BE) |
| Long straddle | buy C(K), buy P(K) | debit | K±d | ∞ / d | Profit = P(>BE+)+P(<BE-) |
| Short straddle | sell C(K), sell P(K) | credit | K±c | c / ∞ | Loss = same; Max-profit prob 0 |
| Long strangle | buy P(K1), buy C(K2), K1<K2 | debit | K1-d, K2+d | ∞ / d | Profit = P(>BE+)+P(<BE-) |
| Short strangle | sell P(K1), sell C(K2) | credit | K1-c, K2+c | c / ∞ | Loss; Max-profit prob = P(K1≤S_T≤K2) |
| Long call / put calendar | sell near C/P(K), buy far C/P(K) | debit | — | — | — (Net delta, Net vega, IV skew = near IV - far IV) |
| Long call diagonal | buy far C(K1), sell near C(K2), K1<K2 | debit | — | — | — |
| Short call diagonal | sell far C(K1), buy near C(K2), K1<K2 | credit | — | — | — |
| Long put diagonal | buy far P(K2), sell near P(K1), K1<K2 | debit | — | — | — |
| Short put diagonal | sell far P(K2), buy near P(K1), K1<K2 | credit | — | — | — |
| Long call / put butterfly | +1 K1, -2 K2, +1 K3, K2-K1 = K3-K2 = w | debit | K1+d, K3-d | w-d / d | Profit = P(BE-<S_T<BE+) |
| Short call / put butterfly | -1, +2, -1 | credit | K1+c, K3-c | c / w-c | Loss = P(BE-<S_T<BE+) |
| Long iron butterfly | buy P(K2)+C(K2), sell P(K1)+C(K3) | debit | K2±d | w-d / d | Profit = P(<BE-)+P(>BE+) |
| Short iron butterfly | sell P(K2)+C(K2), buy P(K1)+C(K3) | credit | K2±c | c / w-c | Loss = P(<BE-)+P(>BE+) |
| Long call / put condor | +K1 -K2 -K3 +K4, K2-K1 = K4-K3 = w | debit | K1+d, K4-d | w-d / d | Profit = P(BE-<S_T<BE+) |
| Short call / put condor | -K1 +K2 +K3 -K4 | credit | K1+c, K4-c | c / w-c | Loss = P(BE-<S_T<BE+) |
| Long iron condor | buy P(K2), sell P(K1), buy C(K3), sell C(K4) | debit | K2-d, K3+d | w-d / d | Profit = P(<BE-)+P(>BE+) |
| Short iron condor | sell P(K2), buy P(K1), sell C(K3), buy C(K4) | credit | K2-c, K3+c | c / w-c | Loss = P(<BE-)+P(>BE+) |
| Protective collar | own stock, buy P(K1), sell C(K2), K1<S<K2 | C-P | S-(C-P) | (K2-S+C-P) / (S-K1-(C-P)) | Profit = P(S_T>BE) |

Risk/Reward = max loss / max profit; Max Profit % = max profit / max loss × 100.
Net delta / vega per share, signed (buy +, sell -). IV/HV = mean leg IV×100 / hv20.

**Pairing bounds** (engine constants, documented on the page): strikes at most
`MAX_APART = 10` listed strikes apart within an expiry; calendars/diagonals pair a near and
a far expiry of the same right whose DTEs fall in the screen's two DTE filters; every leg
must pass its leg filters BEFORE pairing (that is what keeps the whole market tractable).
Credit structures need c > 0 and c < w; debit structures need 0 < d < w (where w applies).

## 7. Saved screeners (main DB, `models.OptionScreen`, main Alembic)

`option_screens`: id · user_id FK users CASCADE · screen_key · name · payload JSON (§5) ·
is_default Boolean · created_at · updated_at. Unique `(user_id, screen_key, name)`. A
member's default for a screen loads when they open that screen; otherwise the screen's
built-in defaults. Members see only their own.

## 8. The page (`/options`)

Barchart's layout in the app's theme (dark/light tokens, invisible-until-hover scrollbars):

* Header: title = the screen name; **GO TO** dropdown (grouped by family); the screen's
  one-line description; the **data line** — "Massive · 15-min delayed · prices estimated from
  IV · last market pass 11:42 ET · 4,512 underlyings · 1.02M contracts · IV history 1,204 /
  4,512" with the collector state as a coloured dot (green running/idle, amber stale, rose
  error) — the dashboard-visibility rule.
* Tabs **SET FILTERS | RESULTS**.
* SET FILTERS: *Load a screener* (saved), *Save screener* / *Save as…* / *Delete* /
  *Make default*; the filter cards in order (remove ✕, move up/down); checkbox groups for
  choices; range boxes + preset chips for ranges; comparator select for single values;
  **Add a Filter** (group → field); *Clear all*; *Reset to defaults*; **See results**.
* RESULTS: "Results: N" · views (Main / Filter / Greeks / Volatility) · *Flag earnings* ·
  *refresh* · *download* (CSV, ≤ 1000 rows) · the table (sticky header, click a header to
  sort, 100 rows a page, pager) · a row click on a strategy opens its legs and an at-expiry
  **profit/loss chart** (inline SVG) · the Symbol cell links to the chart page.
* URL keeps the state: `/options?screen=bull-put-spread` (the filters live in the page /
  the saved screener, not the URL).

Endpoints (`routes/options_page.py`, all behind `require_menu("options")`):

| Method | Path | Returns |
|---|---|---|
| GET | `/options` | the page (`?screen=`) |
| GET | `/options/api/screens` | `engine.screens()` |
| GET | `/options/api/spec?screen=` | `engine.spec()` + the member's saved list for that screen |
| POST | `/options/api/run` | `engine.run()` JSON (`{screen, payload, page}`) |
| POST | `/options/api/csv` | text/csv attachment |
| GET | `/options/api/status` | collector status + frame meta |
| GET/POST | `/options/api/saved` | list / create-or-update a saved screener |
| POST | `/options/api/saved/{id}/default` · `/options/api/saved/{id}/delete` | |

## 9. Hermes

* New task **TST-Options-Screener** — `deploy\setup_screener_task.ps1 -StartNow` (same shape as
  the options collector's script: venv python, at boot + daily revive, restart on failure).
* `app\.env`: nothing new is required (the Massive key is already there); optional
  `TST_SCREENER_*` knobs.
* `pip install -r app\requirements.txt` once (numpy).
* The tray (`dashboard_intraday/tray_status.py`) gets an **Options screener** line from
  `state\screener_collector.json` (tray-sync rule).

## 10. Build ownership (v4.136)

| Part | Files |
|---|---|
| D — data + collector | `app/screener_models.py`, `app/screener_db.py`, `alembic_screener/**`, `app/services/scr_store.py`, `app/services/scr_collector.py`, `app/services/massive.py` (additive endpoints), `deploy/screener_collector.py`, `deploy/setup_screener_task.ps1`, `../dashboard_intraday/tray_status.py` (screener line), tests `test_scr_store.py`, `test_scr_collector.py` |
| E — engine | `app/services/screener/**`, tests `test_screener_engine.py`, `test_screener_strategies.py` |
| U — page | `app/routes/options_page.py`, `app/templates/options.html` (+ `_scr_*.html` if needed), `app/models.py` (`OptionScreen`), main `alembic/versions/*_option_screens.py`, tests `test_options_page.py` |
| Integration | `app/main.py` (screener DB init at startup), `app/requirements.txt` (numpy), `tests/conftest.py`, README / DEPLOY / CLAUDE.md, version 4.136 |

## 11. Decided at build (v4.136)

Engine (`services/screener/`):
1. Key names that would collide: the strategy-level profit probability is `win_prob`
   (labelled "Profit Prob"); the single-option one stays `profit_prob`; `breakeven` (contract)
   vs `be` (strategy); `iv_hv` / `iv30_hv20` / `avg_iv_hv` (contract / underlying / strategy);
   the underlying's IV percentile is `iv_pctl`. IV, probabilities and moneyness are PERCENT in
   the API (incl. `legs[].iv`).
2. Leg order: credit verticals sold leg first, debit verticals bought leg first; the collar
   short call then long put; straddle call then put; strangle put then call; butterflies and
   condors by strike, low to high; calendars and diagonals near leg first.
3. Strangles, collars and iron condors take the call from the 10 listed call strikes above the
   higher of the put strike and the stock price; a condor's two middle strikes are at most 10
   listed strikes apart. Equal wings are compared exactly in thousandths of a dollar.
4. Over the 4M-combination cap the run keeps the sorted rows found so far, `truncated=true`, and
   warns with the last symbol covered. `total` counts every match; `kept` (<= 5,000) is what
   pages.
5. Max-profit probability is `None` where profit is unlimited (long straddle / strangle) and 0
   where max profit is at a single price (short straddle, long butterfly, short iron butterfly).
6. Collar: net delta includes the stock's +1; upside = (call strike - BE)/BE, downside = (BE -
   put strike)/BE; %Cost = (put - call)/stock price. Covered call / married put Delta = the
   option's own.
7. An earnings date on the expiry day counts as "before expiration"; "expires before earnings"
   is its opposite (an ETF with no earnings reads true).
8. "Last trade within N hours" is measured from the data's read time (weekend screening works).
9. A payload with no `filters` key = the screen's defaults; `[]` = no filters. Unknown fields /
   ops / values / sort columns / views become warnings, never a 500.
10. The frame also reloads when the ET date changes (DTEs stay right over a weekend).
11. Defaults add a `max_profit_pct` floor to credit and debit structures (10 % / 50 %) - not
    Barchart's - so the probability sorts do not open on penny credits / deep-ITM debits.
12. Single-option rows also carry `legs`, so every row of every screen opens a P/L chart.

Data (`scr_collector`, `scr_store`, `massive`):
13. `massive.Client` gained `option_underlyings` (contract list, up to 5,000 pages, not paced
    per minute), `grouped_daily` and `reference_tickers` (both Stocks-Basic paced, 5/min),
    `our_symbol` (BRK.B -> BRK-B; `I:SPX` kept).
14. A contract whose latest day bar is from an older session is stored with volume 0 and no day
    change (Massive's `day` is the most recent bar).
15. Splits: a filed day whose close is outside 0.6-1.6x the previous close re-reads that
    symbol's adjusted bars; universe symbols with < 20 bars after the backfill get their own
    `stock_daily` read (at most daily; weekly when Massive has none).
16. A pass that reads nothing usable for an underlying keeps its previous spot / IV30
    (`spot_as_of` shows the age). A pass cut short by a Massive pause stays unfinished and
    resumes.
17. Identity (name / type / exchange) re-runs at start whenever a universe symbol still has no
    security type (~25 stock-paced requests); the routine refresh is weekly. Until it has run,
    an underlying is excluded by any Security Type filter (the run warns with the count).

Open, to confirm on Hermes against live Massive: whether index options list under `SPX` or
`I:SPX`; the exact grouped-daily and ticker-reference fields (assumed from Polygon's docs).
