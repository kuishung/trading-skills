## A. Data layer and nightly pipeline

Scope of this part: everything between the option feeds and the engines. The engines
(Part B), the page (Part C/D) and the Positions monitor read **only** what this part
stores; nothing on the page waits on a market call (OPTIONS_MODULE_DESIGN.md §2.5, §4.4).

All paths below are relative to `dashboard_tst/`. Line numbers were read on 2026-10-03.
Reconciled on 2026-10-04 against CRITIQUE.md and the unified contract ("the contract"
below): ONE migration (`f4a5b6c7d8e9`, nine tables), ONE positions store (`option_trades`,
Part B's shape), ONE prefs module (`option_prefs.py`, Part B's blocks), ONE signal read path
(`option_store.card_for` / `basket_rows_for`), ONE payoff renderer (Part C's), ONE Telegram
path (Part D's `telegram_push`), bridge **1.6** with a **percent** IV series, sizing at
**read** time, Live never persisted.

### A0. Files, units and time conventions

| File | New / modified | Role |
|---|---|---|
| `app/services/option_data.py` | **new** | `ChainSource` interface, `Chain`/`ContractRow` dataclasses, `CboeSource`, `AlpacaSource`, `BridgePayloadSource`, `source()` factory, legacy `to_legs()` adapter |
| `app/services/option_quotes.py` | modified (additive, lines 158-166) | the Cboe parser keeps five fields it currently drops (`rho`, `last_trade_price`, `bid_size`, `ask_size`, `prev_day_close`); nothing else changes, every existing caller keeps working |
| `app/services/option_metrics.py` | **new**, pure | HV20/HV60, ATM IV per expiry, IV30 constant-maturity, IV rank / percentile (+ `state`), term ratio (`iv_front / iv_back`), 25-delta skew, expected move, days-to-earnings |
| `app/services/option_store.py` | **new** | ORM reads/writes: `replace_snapshot`, `upsert_iv_daily`, `upsert_signal`, `bootstrap_iv`, `backfill_from_iv_history`, `prune`, `latest_chain`, `latest_snap_on`, `iv_series`, `basket_universe`, and the ONLY two signal read paths `card_for(db, symbol, user)` / `basket_rows_for(db, user)` (A5.3) |
| `app/services/option_prefs.py` | **new** (schema = Part B4.1; presentation columns = Part D3) | the ONE prefs module: blocks `shared, credit_vertical, debit_vertical, long, leaps, condor, time` (Part B's field names), every field carrying `label/help/plain/step/unit` in the SAME table; `HOUSE`, `clean(raw)` merge, `for_user(db, user)`, `prefs_hash(merged)` over pick-relevant fields only (A5.1), `distinct_hashes(db)` for the nightly job (A4.2) |
| `app/services/option_nightly.py` | **new** | orchestration: `run_nightly(db, ...)`, `refresh_symbol(db, sym, user)`, the per-symbol pipeline, the `option_jobs` record written through `services/job_runs.py` |
| `app/services/job_runs.py` | **new** (Part D6's module, listed because this job writes through it) | `start / finish / latest / missed` over `option_jobs` (A4.3) |
| `app/services/telegram_push.py` + `app/services/telegram.py` | **new** (Part D4's modules) | `telegram_push.run(db, as_of, dry_run)` is the nightly job's step 5 (A4.4); nothing else in this part sends anything |
| `deploy/options_nightly.py` | **new**, owned by this part | the Hermes script (mirrors `deploy/spread_scan.py`) |
| `deploy/setup_options_nightly_task.ps1` | **new**, owned by this part | registers `TST-Options-Nightly` (mirrors `deploy/setup_spread_scan_task.ps1`) |
| `alembic/versions/f4a5b6c7d8e9_options_module.py` | **new** | the ONE migration of the module: nine tables + the open-`option_spreads` copy step, chained off `e2f3a4b5c6d7` (A2.2) |
| `app/models.py` | modified (append) | `OptionBasket`, `OptionChainSnapshot`, `IVDaily`, `OptionSignal`, `UserOptionPrefs`, `OptionJob` (this part's shapes, A2.1) + `OptionTrade`, `OptionTradeCheck` (Part B7.1's shapes) + `OptionIdeaPush` (Part D4's shape) |
| `app/config.py` | modified (append to `Settings`) | `options_source`, `options_fallback`, `options_snapshot_days`, `options_full_days`, `alpaca_feed` |
| `app/.env.example` | modified | the five `TST_OPTIONS_*` keys documented |
| `bridge/ibkr_bridge.py` | modified (1.5 → **1.6**) | `/iv?symbol=X&series=1` also returns the daily series, in **percent** (the "Live" bootstrap, A4.6) |
| `app/routes/options_page.py` | Part D owns the router (one module, gated by `require_menu("options")`); this part owns the **semantics** of the data endpoints it serves | `POST /options/refresh/{symbol}` (A4.7), `POST /options/live/{symbol}` (A1.5 + A4.6 — the IV bootstrap is a step inside it), `POST /options/basket/import` (A6.2), `GET /options/badge` + `GET /options/status/strip` (A4.3) |

**Units — one convention, stated once, enforced in `option_data.py`:**

| Quantity | Unit | Why |
|---|---|---|
| per-contract `iv` (snapshot row) | **fraction**, 0.3585 | what Cboe (`options[].iv`, probe 2026-10-03) and Alpaca (`impliedVolatility`) send, and what `black_scholes.black_scholes(sigma=)` (black_scholes.py:43) takes |
| per-day vol statistics (`iv_daily.iv30`, `atm_iv30`, `hv20`, `hv60`, `iv_front`, `iv_back`, `iv_lo`, `iv_hi`) | **percent**, 32.03 | `IVHistory.iv30` is already percent (models.py:948, "percent, e.g. 33.7"); `deploy/iv_seed_ibkr.py:104` multiplies IB's fraction by 100 to match; A6 copies `iv_history` into `iv_daily` 1:1 |
| `term_ratio` | unitless, `iv_front / iv_back` (A3.4) | the one term-structure figure; thresholds `TERM_EVENT 1.05`, `TERM_CONTANGO 0.95` (Part B's gauge) |
| the bridge's per-row `iv` (`_row`, ibkr_bridge.py:332) | percent | **divided by 100** by `BridgePayloadSource` (A1.5) — the ONLY division in the module; the unit is decided by the SOURCE, never by the magnitude (A1.1). `bull_put.pl_profile` (bull_put.py:165-193, `short_iv / 100.0`) is the one existing reader that assumes percent — cross-part note in A1.7 |
| the bridge's `/iv?series=1` daily series (bridge 1.6, A4.6) | **percent**, `round(b.close*100, 1)` — the unit its own `iv_current / iv_low / iv_high` already use (ibkr_bridge.py:559-563) | `option_store.bootstrap_iv` stores it **as-is**, bounded 0.1..1000; no multiplication on the server |
| greeks | per share, signed as the feed gives them (put delta negative) | `spread_monitor.snapshot` relies on the sign (spread_monitor.py:140-143) |
| `oi`, `volume`, `bid_size`, `ask_size` | integer contracts; **None = the feed did not say** (never 0) | `bull_put._count` (bull_put.py:148-156) and the bridge's `_count` (ibkr_bridge.py:304-310) both make that distinction |
| prices | per share | as everywhere else in `option_spreads` |
| pick `max_loss`, `max_profit` | **positive** $ per contract | the contract's PICK shape (A5.2); the payoff chart reports `max_loss` positive too (Part C) |

**Time keys:** every daily key is an **ET trading date** string `YYYY-MM-DD` via
`spread_monitor.et_today()` (spread_monitor.py:204-212; the Malaysia-vs-New-York reason is
in its docstring). The snapshot's `as_of` is the **feed's** own timestamp converted to naive
UTC (A1.3), never the fetch time: staleness is a property of the data (the `RRGPoint.as_of`
rule, models.py:1078-1079).

### A1. The `ChainSource` interface and its three implementations

#### A1.1 The normalized contract row

The row is a superset of every field an existing consumer reads today. Consumers and the
keys they read, so the migration in A1.7 is mechanical:

| Consumer | Reads (key names in use today) | Where |
|---|---|---|
| `spread_scan.build_candidates` | `chain["spot"]`, `chain["iv30"]`, `legs[(expiry,right,strike)]` → `bid ask delta iv volume open_interest` (the LEGACY dict key; after `opt_legs.norm_leg` the key is always `oi`) | spread_scan.py:110-181 |
| `spread_monitor.snapshot` | `chain["spot"] ["iv30"] ["as_of"]`, `option_quotes.leg()` → `bid ask mid delta theta iv` | spread_monitor.py:105-151 |
| `routes/portfolio._entry_greeks` | `leg()` → `delta iv` | portfolio.py:229-245 |
| `bull_put.rank_pairs` / `select` (bridge rows) | `right strike bid ask last delta iv oi volume` (+ `spread_pct` computed again as `_leg_spread`) | bull_put.py:129-156, 258, 340 |
| `routes/options.analyze` (bridge payload) | `chain.ok spot expiry_label dte puts`, `iv.iv_percentile`, `nlv` | options.py:131-146 |

```python
# app/services/option_data.py
@dataclass(frozen=True)
class ContractRow:
    expiry: str          # YYYY-MM-DD
    right: str           # "C" | "P"
    strike: float
    bid: float | None
    ask: float | None
    mid: float | None    # (bid+ask)/2 when both exist, else None (NOT last — bull_put._mid falls back to last itself)
    last: float | None   # last trade price
    bid_size: int | None
    ask_size: int | None
    iv: float | None     # FRACTION — always, whatever the source (A1.1 unit rule below)
    delta: float | None  # signed
    gamma: float | None
    theta: float | None  # per day, per share, signed (negative for a long option)
    vega: float | None
    rho: float | None
    theo: float | None   # model value when the feed gives one (Cboe); None otherwise
    oi: int | None       # open interest; None = unknown. The key is `oi`, never `open_interest`
    volume: int | None   # today's contracts; None = unknown
    prev_close: float | None
    dte: int             # calendar days from snap_on to expiry

@dataclass
class Chain:
    symbol: str
    source: str                    # "cboe" | "alpaca" | "bridge"
    kind: str                      # "eod" | "intraday" | "live"
    as_of: _dt.datetime            # naive UTC, the FEED's timestamp
    snap_on: str                   # ET date the chain describes (A1.3 rule)
    spot: float
    iv30: float | None             # PERCENT; None when the source has no constant-maturity figure (Alpaca, bridge)
    rows: list[ContractRow]
    delayed_minutes: int           # 15 (cboe/alpaca indicative), 0 (bridge live), 15 (bridge delayed-frozen)
    header: dict                   # raw non-contract fields, kept for the full-chain expander (bid/ask/volume of the stock etc.)
    partial: bool = False          # True when the feed returned fewer expiries/strikes than expected (A7)
    note: str = ""

    def legs(self) -> dict[tuple, dict]:
        """The {(expiry, right, strike): {...}} dict option_quotes.fetch_chain returns
        today (option_quotes.py:151-166), key for key (so the legacy key `open_interest`
        survives HERE and only here), so spread_scan / spread_monitor / portfolio run
        unchanged on a Chain (A1.7)."""
```

Field mapping per source (blank = not available, stored as None):

| Normalized | Cboe (`options[]`, probe 2026-10-03) | Alpaca snapshots / contracts | Bridge `_row` (ibkr_bridge.py:321-339) |
|---|---|---|---|
| expiry/right/strike | `option` via `option_quotes.parse_occ` (79-89) | contract symbol (OCC) via `parse_occ`; `expiration_date`/`type`/`strike_price` from `/v2/options/contracts` as the cross-check | `right`, `strike`; expiry = the chain's `expiry` (one per call) |
| bid / ask | `bid` / `ask` (0.0 = no quote → None, `_num` rule 92-100 plus `bid==0 and ask==0 → None`) | `latestQuote.bp` / `.ap` | `bid` / `ask` |
| last | `last_trade_price` | `latestTrade.p` | `last` |
| bid_size / ask_size | `bid_size` / `ask_size` | `latestQuote.bs` / `.as` | — |
| iv | `iv` (fraction) | `impliedVolatility` (fraction) | `iv` **/ 100** |
| delta gamma theta vega rho | same names | `greeks.*` | `delta gamma theta vega` (no rho) |
| theo | `theo` | — | — |
| oi | `open_interest` | `/v2/options/contracts` `open_interest` (joined by OCC symbol; `open_interest_date` kept in `header`) | `oi` |
| volume | `volume` | `dailyBar.v` | `volume` |
| prev_close | `prev_day_close` | `prevDailyBar.c` | — |
| spot | `data.current_price` | `GET /v2/stocks/{sym}/trades/latest?feed=iex` `.trade.p` (fallback `prices.fetch_quote` → `price`, prices.py:111-146) | `spot` |
| iv30 | `data.iv30` (percent) | None → `atm_iv30` computed (A3.2) | None (the bridge's `/iv` gives a 1-year series instead, A4.6) |
| as_of | `data.last_trade_time` (naive ET) → UTC | max `latestQuote.t` over rows | now |

Sanity filters applied in every adapter (a row failing them is kept but its bad field is
None, so the chain is never silently thinner): `iv` outside `(0.01, 5.0)` → None (MSFT's
deep-OTM strikes print `iv` up to 8.3); |delta| > 1 → None; negative prices → None; strike ≤ 0
→ row dropped.

**IV unit rule — by SOURCE, never by magnitude (contract, IV UNITS).** There is no
"a value above three must be a percent" magnitude heuristic anywhere in the module: the 2026-10-02 Cboe MSFT file
prints a deep-ITM contract's `iv` as `3.1099` — a FRACTION — so a magnitude test would
corrupt a real row. Each adapter knows its feed's unit (`cboe` fraction, `alpaca` fraction,
raw bridge `_row` percent) and only `BridgePayloadSource` divides by 100. Downstream,
`opt_legs.norm_leg(row, unit=)` (Part B) and `payoff.normalise_iv(v, unit=)` (Part C) take
the unit explicitly from the chain's `source` — `"fraction"` for a `ContractRow` of any
source, `"percent"` only for a raw bridge row that bypassed `BridgePayloadSource` — and never
guess. A `ContractRow.iv` is therefore a fraction, full stop, by the time anything reads it.

#### A1.2 The interface

```python
class ChainSource(abc.ABC):
    name: str                       # "cboe" | "alpaca" | "bridge"
    capabilities: Capabilities      # frozen dataclass below

    @abc.abstractmethod
    def fetch_chain(self, symbol: str, *, fresh: bool = False,
                    retries: int = 0) -> Chain:
        """Every listed contract on every expiry for one underlying, normalized.
        Raises option_quotes.ChainError (option_quotes.py:69) on any network or
        shape failure — the SAME exception class the Portfolio and Spread pages
        already catch, so no caller gains a new except branch.
        ``fresh`` bypasses the in-process cache (the nightly job passes True,
        like spread_scan.run_scan does via option_quotes.clear_cache, 336-337)."""

    def fetch_iv30(self, symbol: str) -> float | None:
        """The feed's own 30-day constant-maturity IV in PERCENT, or None when the
        source has none. Default: fetch_chain(symbol).iv30 (cache-hit, so free)."""

@dataclass(frozen=True)
class Capabilities:
    has_iv30: bool          # cboe True, alpaca False, bridge False
    has_oi: bool            # cboe True, alpaca True (via contracts), bridge "sometimes" (oi_ok flag, ibkr_bridge.py:534) → True with per-row None
    has_greeks: bool        # all True
    has_rho: bool           # cboe/alpaca True, bridge False
    delayed_minutes: int    # 15 / 15 / 0-or-15
    all_expiries: bool      # cboe/alpaca True, bridge False (one expiry per call)
    server_side: bool       # cboe/alpaca True, bridge False
    pacing_seconds: float   # 1.5 / 0.4 / n.a.
```

Factory and selection:

```python
def source(name: str | None = None) -> ChainSource:
    """settings.options_source unless overridden. Unknown name -> ChainError at
    construction, so a typo in .env fails the nightly job loudly, not per symbol."""

def fetch_chain(symbol, *, fresh=False, retries=0) -> Chain:
    """Primary source; on ChainError, if settings.options_fallback names the other
    server-side source, try it once and set chain.note = 'fallback: alpaca'.
    Both attempts are logged; the Chain.source says which one answered."""
```

Config (`app/config.py`, appended to `Settings`, same `os.environ` style as lines 43-50):

| Env | Default | Meaning |
|---|---|---|
| `TST_OPTIONS_SOURCE` | `cboe` | the source of record (decision 1) |
| `TST_OPTIONS_FALLBACK` | `` (off) | `alpaca` or `cboe`: tried once per symbol when the primary raises |
| `TST_ALPACA_FEED` | `indicative` | `opra` only with the Algo Trader Plus subscription |
| `TST_OPTIONS_SNAPSHOT_DAYS` | `90` | EOD snapshot retention (§4.3) |
| `TST_OPTIONS_FULL_DAYS` | `7` | days a snapshot is kept at full width before thinning (A2.4) |

#### A1.3 `CboeSource` — adapting `services/option_quotes.py`

Keep `option_quotes.fetch_chain` (option_quotes.py:103-181) as the HTTP + cache + retry
layer; it already has the 15-minute cache keyed on the Cboe symbol (52-54, 120-124), the 429
backoff (127-136), the 403-means-bad-ticker message (139-147) and `clear_cache` (205-209).
Two additive edits to its parser (lines 158-166): keep `rho`, `last_trade_price` → `last`,
`bid_size`, `ask_size`, `prev_day_close` → `prev_close` in each leg dict, and keep the
non-`options` keys of `data` as `out["header"]` (the probe shows `bid ask bid_size ask_size
open high low close prev_day_close volume iv30_change iv30_change_percent tick seqno`,
all useful for the full-chain expander and the stale check). Existing consumers index by key
and ignore extras.

```python
class CboeSource(ChainSource):
    name = "cboe"
    capabilities = Capabilities(True, True, True, True, 15, True, True, 1.5)

    def fetch_chain(self, symbol, *, fresh=False, retries=0) -> Chain:
        if fresh:
            option_quotes.clear_cache()           # per-process; the job wants a real read
        raw = option_quotes.fetch_chain(symbol, retries=retries)   # ChainError propagates
        as_of = _et_naive_to_utc(raw["as_of"])    # "2026-10-02T15:59:59" is New York wall time
        snap_on = (raw["as_of"] or "")[:10] or spread_monitor.et_today()
        rows = [ContractRow(..., dte=_dte(leg["expiry"], snap_on)) for leg in raw["legs"].values()]
        return Chain(symbol=raw["symbol"], source="cboe", kind="eod", as_of=as_of,
                     snap_on=snap_on, spot=raw["spot"], iv30=raw["iv30"], rows=rows,
                     delayed_minutes=15, header=raw["header"],
                     partial=_looks_partial(rows))
```

`_et_naive_to_utc` uses `ZoneInfo("America/New_York")` with the same rule-based fallback
`spread_monitor._et_now` carries (spread_monitor.py:215-241; tzdata is in
`app/requirements.txt:22` so the fallback stays unused).

**`snap_on` rule:** the ET date of `last_trade_time`, not the run date. Run at 06:00-07:30 MYT
(= 18:00-19:30 ET, same ET day) it equals `et_today()`; on a US holiday the feed still carries
the prior session, so the job re-files that session under its own date (identical rows
replaced — idempotent, A4.5) rather than inventing a holiday snapshot.

`_looks_partial(rows)`: True when (a) fewer than 2 expiries, or (b) no expiry with
`dte >= 20`, or (c) fewer than 30 % of rows carry a `delta`. Cboe has never been observed
truncating a file, but the endpoint is undocumented (option_quotes.py:27-30) and A7 needs a
flag to show. A `partial` snapshot is also one of the Telegram guards (A4.4).

#### A1.4 `AlpacaSource`

| Item | Value (docs.alpaca.markets, read 2026-10-03) |
|---|---|
| Chain | `GET https://data.alpaca.markets/v1beta1/options/snapshots/{underlying}` — `feed=indicative` (free, ~15-min) or `opra` (subscription), `limit=1000` (max), `page_token` for the rest, optional `type`, `strike_price_gte/lte`, `expiration_date_gte/lte`. Response `{"snapshots": {OCC: {latestQuote{t,bp,bs,ap,as,bx,ax,c}, latestTrade{t,p,s,x,c}, impliedVolatility, greeks{delta,gamma,theta,vega,rho}, dailyBar, minuteBar, prevDailyBar}}, "next_page_token"}` |
| Open interest | **not in snapshots.** `GET https://paper-api.alpaca.markets/v2/options/contracts?underlying_symbols={sym}&status=active&limit=10000&expiration_date_gte={today}&expiration_date_lte={today+1200d}` → `open_interest`, `open_interest_date`, `close_price`, `expiration_date`, `strike_price`, `type`, paginated by `page_token`. **`expiration_date_lte` defaults to the next weekend**, so it must be passed or every LEAPS is missing |
| Spot | `GET https://data.alpaca.markets/v2/stocks/{sym}/trades/latest?feed=iex` → `trade.p`; fallback `prices.fetch_quote(sym)["price"]` |
| Auth | headers `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` from `scripts._common._env_lookup("alpaca.env")` (then `"alpaca-trader-paper.env"`), keys `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` (_common.py:201-221, 233-235). Importable because `resources_bridge` puts `TRADEHUNTER_ROOT` on `sys.path` (resources_bridge.py:17-21). **Not** `load_alpaca_env` — it `sys.exit`s on a missing file (_common.py:237-244), which would take uvicorn down |
| Rate limit | Basic plan **200 requests/min**. Per ticker: ⌈contracts/1000⌉ chain pages (MSFT 3,710 → 4) + ⌈contracts/10000⌉ contract pages (1) + 1 spot = ~6 calls; `pacing_seconds = 0.4` → 150/min worst case, under the cap with the web app's own calls left over. A 429 → sleep `Retry-After` or 20 s, up to `retries` |
| Timeouts | 30 s per call (`httpx.Client`, keep-alive, like `prices._client`, prices.py:38-47) |

```python
class AlpacaSource(ChainSource):
    name = "alpaca"
    capabilities = Capabilities(has_iv30=False, has_oi=True, has_greeks=True, has_rho=True,
                                delayed_minutes=15, all_expiries=True, server_side=True,
                                pacing_seconds=0.4)

    def fetch_chain(self, symbol, *, fresh=False, retries=0) -> Chain:
        key, secret = _alpaca_keys()                     # ChainError("alpaca: no credentials") if absent
        snaps = {}                                       # OCC -> snapshot, all pages
        for page in _paged(f"{DATA}/v1beta1/options/snapshots/{symbol}",
                           {"feed": settings.alpaca_feed, "limit": 1000}):
            snaps.update(page["snapshots"])
        if not snaps:
            raise ChainError(f"{symbol}: Alpaca returned no contracts — check the ticker")
        oi = {c["symbol"]: c for page in _paged(f"{PAPER}/v2/options/contracts", {...}) for c in page["option_contracts"]}
        spot = _latest_trade(symbol) or _yahoo_quote(symbol)
        if not spot: raise ChainError(f"{symbol}: no spot price from Alpaca or Yahoo")
        as_of = max(q["t"] for q in quotes)             # RFC-3339 -> naive UTC
        snap_on = _utc_to_et_date(as_of)
        rows = [_row(occ, s, oi.get(occ), snap_on) for occ, s in snaps.items()]
        return Chain(symbol, "alpaca", "eod", as_of, snap_on, spot, iv30=None, rows=rows,
                     delayed_minutes=15 if feed == "indicative" else 0,
                     header={"oi_date": ..., "feed": feed}, partial=_looks_partial(rows))
```

Cache: same shape as `option_quotes._cache` (15 min, per symbol, lock), so a page refresh
and the Positions sweep share one read.

#### A1.5 `BridgePayloadSource` — the member's "Live" read (`POST /options/live/{symbol}`)

The server never reaches the bridge (options.py:1-18). The browser fetches
`127.0.0.1:9224/chain?symbol=X&exp=YYYYMMDD` (one expiry, ±10 strikes, or the put-side
window; ibkr_bridge.py:419-538), `127.0.0.1:9224/iv?symbol=X&series=1` (A4.6) and, when the
member wants sizing from the broker figure, `/account`, and POSTs them as ONE JSON body to
**`POST /options/live/{symbol}`** (D's `verb/{symbol}` convention; the only Live endpoint —
the former separate IV-bootstrap endpoint does not exist):

```
POST /options/live/{symbol}
body: {"chain": <the /chain reply as _options_tab.html:98-116 posts it today>,
       "iv": {"series": [...], "iv_current": .., "iv_rank": .., "iv_percentile": ..},   # from /iv?series=1
       "nlv": <number | null>,                                                        # from /account, optional
       "diag": {"bridge": "TradeHunterIBKRBridge/1.6", "data_mode": "live|delayed", ...}}
-> the re-rendered card partial (HTMX swap), with the live badge
```

`BridgePayloadSource(payload["chain"])` is constructed **from that posted dict** — no
network — and yields a `Chain(kind="live", source="bridge", all rows of that one expiry)`:
`iv/100`, `oi`/`volume` via the bridge's own None convention, `delayed_minutes = 0 if
payload["diag"]["data_mode"] == "live" else 15`, `header = {"greeks_from_delayed", "oi_ok",
"bridge": payload["diag"]["bridge"]}`. Validation: every number re-checked (the body is
untrusted, options.py:110-112), rows capped at 400, strikes within 50 % of spot.

What the request does, in order (contract, LIVE):

1. builds the `Chain` above and runs Part B's pure `strike_picker.pick(chain, setup, prefs)`
   and `option_sizing.size(pick, nlv, prefs)` **in-request** against the member's prefs — the
   NLV used for sizing is `payload["nlv"]` for THIS request (B5.3's order: the Live figure →
   the stored `trade_prefs` nlv → None + the note), never a stored copy of it;
2. renders the card with the badge **`live · TWS {HH:MM} ET`**, the picks table **restricted
   to the live expiry** and every leg row carrying the live time; the other expiries (last
   night's snapshot) sit behind **"show delayed expiries"** so two moments are never mixed
   in one table without saying so;
3. persists **only** the IV series through `option_store.bootstrap_iv` (A4.6; an
   `option_jobs(job='bootstrap')` row records it) and, **only on an explicit click** of
   "from TWS · [remember this]" (a second small POST), the NLV via `trade_prefs.write` —
   a broker balance is never auto-written (consent on a shared platform; critic 1 major).

Live chains are **never written to `option_chain_snapshot`** (decision 1: the bridge is a
read only; critic 2 major). Nothing of the live grade is cached in `option_signal` either —
the next card open reads the stored snapshot again with the stale/delayed badge it had.

On touch / narrow viewports the Live button is **hidden** with the hint *"Live quotes need
TWS on your PC"* (the bridge lives on the member's PC, so the failure text D2.9 shows on a
phone would make no sense). A bridge older than 1.6 → no `series` in the `/iv` reply → the
chain part of the request still grades live; the IV part reports *"Your bridge is older than
1.6 — restart `bridge\start_ibkr_bridge.bat`"* (A4.6).

#### A1.6 Which source when

| Path | Source | Writes |
|---|---|---|
| nightly job (Hermes, 07:15 MYT) | `settings.options_source` (+ fallback) | snapshot `kind=eod`, `iv_daily`, `option_signal` for the house hash AND every distinct saved member hash (A4.2) |
| page "Refresh" button (`POST /options/refresh/{symbol}`) | same | snapshot `kind=intraday` (replaces the symbol's previous intraday rows), `iv_daily` for today (`kind=intraday`), signals for the house hash and the caller's hash |
| page "Live (TWS)" button (`POST /options/live/{symbol}`) | bridge payload, graded in-request | nothing but `iv_daily` bootstrap rows (`source=ibkr`) — and the NLV only on the explicit "remember this" click |
| Positions grader (`option_exits.sweep`, Part B7.3, over `option_trades`) | `option_data.fetch_chain` (the 15-min cache is shared with Refresh, so one read serves both) | `option_trade_checks` |
| legacy `spread_monitor.sweep` (the old `/portfolio`, until removal) | unchanged: `option_quotes.fetch_chain` | unchanged (`spread_checks`) — `spread_monitor` is **not changed** by this module (contract, POSITIONS STORE) |

#### A1.7 Migrating the existing consumers onto the normalized row

| Consumer | Change | Risk |
|---|---|---|
| `spread_scan.build_candidates(symbol, chain)` | call with `chain.legs()` wrapped: `{"spot": c.spot, "iv30": c.iv30, "legs": c.legs()}` — the function body (spread_scan.py:110-181) reads exactly those keys | none; the nightly Spread scan keeps calling `option_quotes.fetch_chain` directly until step 2 |
| `spread_monitor.snapshot(chain=...)` | **unchanged** (contract: `spread_monitor` is not touched — it serves only the legacy `/portfolio` until removal). `Chain.as_legacy()` = `{"symbol","spot","iv30","as_of","fetched_at","legs"}` exists so a caller MAY hand it a `Chain` without any edit inside `spread_monitor` | none |
| `option_quotes.leg/expiries/strikes` | unchanged — they take the legacy dict; `Chain.as_legacy()` feeds them | none |
| `bull_put.rank_pairs(puts=[...])` | Part B's generic picker takes `list[ContractRow]` through `opt_legs.norm_leg(row, unit="fraction")`; `bull_put` itself is untouched in step 1 and is fed `[asdict(r) | {"oi": r.oi}]` for the bull-put path | **`bull_put.pl_profile` divides `short_iv` by 100** (bull_put.py:184-185) because bridge rows carry percent. Part C's `payoff.build()` replaces `pl_profile` on the new page and takes fraction IV straight into `black_scholes`; until the legacy tab is removed the bull-put adapter multiplies by 100 on the way in. Flagged as a cross-part need |
| `routes/options.analyze` | unchanged for the legacy Watchlist tab (its `POST /options/track` stays untouched and ungated; the new page's tracking endpoint is `POST /options/track-idea`, Part D); the new page's live grade goes through `BridgePayloadSource` | none |

### A2. Tables

#### A2.1 Models (`app/models.py`, appended; conventions from the file: `_utcnow` default (48-49), `ondelete="CASCADE"` FKs (727-728), ISO-date strings for day keys (947, 1125-1127), `JSON` for blobs (778), `UniqueConstraint` + `Index` in `__table_args__` (983-985))

```python
class OptionBasket(Base):
    """A ticker one member (or the system) studies on the Options page. Separate
    from user_watchlist (models.py:711-737): a different cadence (nightly chain
    snapshot, 1.5 s of Cboe pacing each) and a different cost, so adding a name
    here is a deliberate act. owner_key = f"u{user_id}" or "system" so the
    unique constraint works without a NULL user_id (NULLs are distinct in a
    UNIQUE on both SQLite and Postgres). MAX_BASKET = 60 per member (A6.2)."""
    __tablename__ = "option_basket"
    __table_args__ = (UniqueConstraint("owner_key", "symbol", name="uq_option_basket_owner_symbol"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_key = Column(String(16), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    source = Column(String(12), nullable=False, default="typed")
    #   typed | paste | watchlist | ivscan_list | ivscan_scan | scanner | screener | sector | positions | system
    note = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, default=True)          # off = kept, not fetched
    added_on = Column(String(10), nullable=False)                   # ET date
    pos = Column(Integer, nullable=False, default=0)                # member's display order (Part D's column)
    created_at = Column(DateTime, default=_utcnow)
    user = relationship("User")


class OptionChainSnapshot(Base):
    """One contract's quote + greeks as seen at one snapshot. The per-symbol-day
    header (spot, iv30, as_of, source, counts) lives in iv_daily, so this table is
    pure contract rows. Rows are replaced per (symbol, snap_on, kind). kind is
    eod | intraday ONLY — a live (bridge) chain is never written here (A1.5)."""
    __tablename__ = "option_chain_snapshot"
    __table_args__ = (
        Index("ix_ocs_symbol_day_kind", "symbol", "snap_on", "kind"),
        Index("ix_ocs_lookup", "symbol", "snap_on", "expiry", "right", "strike"),
        Index("ix_ocs_snap_on", "snap_on"),
    )
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    snap_on = Column(String(10), nullable=False)       # ET date the chain describes
    kind = Column(String(10), nullable=False, default="eod")   # eod | intraday
    source = Column(String(12), nullable=False, default="cboe")
    expiry = Column(String(10), nullable=False)
    dte = Column(Integer, nullable=False)
    right = Column(String(1), nullable=False)          # C | P
    strike = Column(Float, nullable=False)
    bid = Column(Float, nullable=True);  ask = Column(Float, nullable=True)
    mid = Column(Float, nullable=True);  last = Column(Float, nullable=True)
    bid_size = Column(Integer, nullable=True); ask_size = Column(Integer, nullable=True)
    iv = Column(Float, nullable=True)                  # FRACTION
    delta = Column(Float, nullable=True); gamma = Column(Float, nullable=True)
    theta = Column(Float, nullable=True); vega = Column(Float, nullable=True)
    rho = Column(Float, nullable=True);   theo = Column(Float, nullable=True)
    oi = Column(Integer, nullable=True);  volume = Column(Integer, nullable=True)
    prev_close = Column(Float, nullable=True)


class IVDaily(Base):
    """One underlying, one ET day: the chain header + every derived vol statistic.
    Grows from iv_history (A6) and from every nightly snapshot; the IBKR 'Live'
    bootstrap fills a year of past days (source='ibkr') without overwriting a day
    the server read itself. Per-day statistics are PERCENT (the iv_history unit).
    Column names match the keys of signal.iv (A5.2) so the engine copies, not maps."""
    __tablename__ = "iv_daily"
    __table_args__ = (UniqueConstraint("symbol", "on", name="uq_iv_daily_day"),
                      Index("ix_iv_daily_symbol_on", "symbol", "on"))
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    on = Column(String(10), nullable=False, index=True)
    kind = Column(String(10), nullable=False, default="eod")    # eod | intraday | history
    source = Column(String(12), nullable=False, default="cboe") # cboe | alpaca | ibkr | iv_history
    as_of = Column(DateTime, nullable=True)                     # feed timestamp, naive UTC
    spot = Column(Float, nullable=True)
    iv30 = Column(Float, nullable=True)        # the series the rank is computed on (pct)
    iv30_src = Column(String(8), nullable=True)  # cboe | atm | ibkr — which figure iv30 holds
    atm_iv30 = Column(Float, nullable=True)    # our own constant-maturity ATM IV (pct), computed on EVERY source
    hv20 = Column(Float, nullable=True);  hv60 = Column(Float, nullable=True)   # pct
    iv_hv_premium = Column(Float, nullable=True)   # iv30 - hv20, vol points
    iv_rank = Column(Float, nullable=True);  iv_pct = Column(Float, nullable=True)  # 0..100
    iv_n = Column(Integer, nullable=True)      # observations behind rank/pct
    iv_state = Column(String(8), nullable=True)  # none | forming | pct_only | rank_ok | ok (A3.3)
    iv_lo = Column(Float, nullable=True);  iv_hi = Column(Float, nullable=True)      # window min/max (pct)
    iv_by_expiry = Column(JSON, nullable=True) # {expiry: {dte, atm_iv, n_legs, em_1sd}}
    iv_front = Column(Float, nullable=True);  iv_back = Column(Float, nullable=True)   # pct
    term_ratio = Column(Float, nullable=True)  # iv_front / iv_back (A3.4); the only term figure
    skew25 = Column(Float, nullable=True)      # put25 - call25, vol points
    skew_norm = Column(Float, nullable=True)   # skew25 / atm iv of that expiry
    expected_move = Column(Float, nullable=True)  # 1-sigma expected move, $, 30 calendar days (A3.6)
    earnings_date = Column(String(10), nullable=True)
    earnings_days = Column(Integer, nullable=True)
    n_contracts = Column(Integer, nullable=True); n_expiries = Column(Integer, nullable=True)
    partial = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class OptionSignal(Base):
    """The cache the page reads: one ticker's full card for one snapshot under one
    set of rules (prefs_hash). The nightly job writes one row per DISTINCT prefs
    hash in use (the house hash + every saved override set, A4.2); a member whose
    rules changed after the run gets their row lazily on the first card open
    (A5.3). Read ONLY through option_store.card_for / basket_rows_for. JSON shapes
    are the contract in A5.2. No payoff / ticket columns: both are derived on
    request from the stored picks (Part C's payoff.build, Part B's order_ticket)."""
    __tablename__ = "option_signal"
    __table_args__ = (UniqueConstraint("symbol", "snap_on", "kind", "prefs_hash",
                                       name="uq_option_signal_key"),
                      Index("ix_option_signal_hash_day", "prefs_hash", "snap_on"),
                      Index("ix_option_signal_symbol_day", "symbol", "snap_on"))
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    snap_on = Column(String(10), nullable=False)
    kind = Column(String(10), nullable=False, default="eod")
    as_of = Column(DateTime, nullable=True)
    prefs_hash = Column(String(16), nullable=False)
    engine_version = Column(String(12), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    #   informational only (NULL = written by the job; else the member whose card open
    #   caused the lazy compute). The lookup key is prefs_hash, never user_id.
    status = Column(String(12), nullable=False, default="ok")   # ok | no_setup | no_chain | no_iv | stale_iv | error
    trend = Column(String(12), nullable=True)                   # up | down | sideways | unclear  (a STRING, never JSON)
    headline = Column(Text, nullable=True)                      # composed at WRITE time (option_words.headline)
    setup = Column(JSON, nullable=True)
    iv = Column(JSON, nullable=True)
    strategies = Column(JSON, nullable=True)
    picks = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    computed_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class UserOptionPrefs(Base):
    """A member's rule overrides, SPARSE: only the fields they changed, merged over
    option_prefs.HOUSE on read (like ema_setup.clean_enabled over COND_DEFAULT,
    ema_setup.py:455-462). prefs_hash is the hash of the MERGED result over the
    pick-relevant fields only (A5.1), stored so the signal lookup is one indexed
    read and so the nightly job can enumerate the distinct hashes in use."""
    __tablename__ = "user_option_prefs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    prefs = Column(JSON, nullable=False, default=dict)
    prefs_hash = Column(String(16), nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    user = relationship("User")


class OptionJob(Base):
    """One run of the nightly job / a refresh / a bootstrap: the freshness pill and
    the operator's answer to 'did it run?'. Same role as SpreadScan (models.py:954-968).
    Written through services/job_runs.py (start / finish / latest / missed, Part D6);
    this is the ONE job table of the module."""
    __tablename__ = "option_jobs"
    id = Column(Integer, primary_key=True)
    job = Column(String(12), nullable=False, default="nightly")   # nightly | refresh | bootstrap | backfill
    run_on = Column(String(10), nullable=False, index=True)
    source = Column(String(12), nullable=True)
    started_at = Column(DateTime, default=_utcnow)
    finished_at = Column(DateTime, nullable=True)
    symbols = Column(Integer, nullable=False, default=0)
    ok = Column(Integer, nullable=False, default=0)
    errors = Column(Integer, nullable=False, default=0)
    rows = Column(Integer, nullable=False, default=0)
    pushed = Column(Integer, nullable=False, default=0)   # Telegram ideas sent by step 5 (A4.4)
    detail = Column(JSON, nullable=True)      # {sym: {"status": "ok|error|partial", "ms": int, "n": int, "err": str|None}}
    note = Column(Text, nullable=True)


# Created by the SAME migration, shapes owned elsewhere (listed so the nine-table
# set is visible in one place):
#   OptionTrade / OptionTradeCheck  — Part B7.1: the ONE positions store for EVERY strategy
#       from step 1 (generic legs JSON {expiry, right, strike, side, qty, price, bid, ask,
#       iv, delta, oi, volume, entry_price, entry_delta, entry_iv}; per-day check legs
#       {mid, delta, iv}), graded by services/option_exits.py mark / grade / sweep.
#   OptionIdeaPush                  — Part D4: the Telegram dedupe/opt-in record, keyed
#       (symbol, strategy, front expiry) per member (A4.4).
```

#### A2.2 The migration — `alembic/versions/f4a5b6c7d8e9_options_module.py`

The ONE migration of the whole module (contract, MIGRATION). The two other revision ids
Parts B and D drafted do not exist; every part cites this file.

```python
"""options module: option_basket, option_chain_snapshot, iv_daily, option_signal,
user_option_prefs, option_jobs, option_trades, option_trade_checks, option_idea_push
+ copy of the OPEN option_spreads rows into option_trades (strategy bull_put)

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Create Date: 2026-10-xx
"""
revision = "f4a5b6c7d8e9"
down_revision = "e2f3a4b5c6d7"      # iv_scan_items, the current head (verified 2026-10-03)

ORDER = [  # creation order; reversed on downgrade
    "option_basket", "option_chain_snapshot", "iv_daily", "option_signal",
    "user_option_prefs", "option_jobs", "option_trades", "option_trade_checks",
    "option_idea_push",
]

def _tables():  return set(sa.inspect(op.get_bind()).get_table_names())   # d1e2f3a4b5c6:23-24

def upgrade():
    have = _tables()                 # guarded PER TABLE like e2f3a4b5c6d7:20-21 — the Hermes
    if "option_basket" not in have:  # DB predates Alembic and create_all may have run
        op.create_table("option_basket", ...)   # columns exactly as the models, server_default for
        op.create_index(...)                    # booleans/ints (sa.true(), "0") like d1e2f3a4b5c6:68
    ... nine tables, indexes as declared in __table_args__ (A's six above, B7.1's two, D4's one) ...
    if "option_trades" not in have:              # the data step runs ONLY in the branch that just
        _copy_open_spreads()                     # created the table -> re-runnable, never duplicates

def _copy_open_spreads():
    """Part B7.1's copy step: every option_spreads row with status 'open' becomes an
    option_trades row with strategy='bull_put', legs = [sell P short_strike, buy P
    long_strike] at the row's expiry with entry_price/entry_delta/entry_iv from the
    stored entry fields, opened_on / qty / credit carried over, note 'copied from
    option_spreads #<id>'. Implemented with SQLAlchemy Core sa.table()/select()/insert()
    constructs over op.get_bind() — no raw SQL strings, no import of app.models (a
    migration must not depend on the current model classes) — so it runs the same on
    SQLite and Postgres (CLAUDE.md data-handling rule)."""

def downgrade():
    for t in reversed(ORDER):        # drop indexes then table, guarded by _tables();
        ...                          # option_spreads is never touched, so the copy needs no undo
```

The copy is the only data step; everything else is DDL (fast, reversible). The `iv_history`
copy stays a job step (A6.1). `render_as_batch=True` is already set (alembic/env.py:41, 58)
should a later revision need an ALTER on SQLite. `init_db()` runs `upgrade head` at startup
(db.py:93-94), so the Hermes deploy needs no manual step (CLAUDE.md data-handling rule).

#### A2.3 Estimated row counts

| Table | Rows / day | Retention | Steady state (30-ticker basket) | Steady state (100 distinct tickers across all members — a member's own basket caps at `MAX_BASKET = 60`, A6.2; the union may exceed it) |
|---|---|---|---|---|
| `option_chain_snapshot` | ~3,000 per ticker (MSFT 3,710; SPY ~15k; a $30 mid-cap ~800) | 90 EOD days, thinned after 7 (A2.4); 1 intraday set per ticker | 7 × 90k + 83 × ~40k ≈ **4.0 M** | ≈ 13 M |
| `iv_daily` | 1 per ticker | 3 years (never pruned in v1) | 23k | 76k |
| `option_signal` | 1 per DISTINCT prefs hash in use (house + each saved override set), per ticker | 90 days | ~10k | ~35k |
| `option_jobs` | 1-5 | 180 rows | 180 | 180 |
| `option_trades`, `option_trade_checks` | 1 check per open trade per day (Part B7.3) | checks 90 days | < 5k | < 20k |
| `option_idea_push` | ≤ 5 per member per run | 45 days (Part D4's prune, run from `prune`, A2.4) | < 1k | < 3k |
| `option_basket`, `user_option_prefs` | — | — | < 1k | < 1k |

Row size: 22 columns, ~120-140 B on SQLite including the two composite indexes → **~550 MB**
for the 30-ticker case, ~1.8 GB at 100. `tst.db` is 311 KB today, so this table *is* the
database from day one.

#### A2.4 Retention and pruning (`option_store.prune(db, today)`, called at the end of every nightly run; each statement is a plain ORM `delete(synchronize_session=False)` like `spread_scan.prune`, spread_scan.py:303-314)

| Rule | Statement | Why |
|---|---|---|
| EOD age | `snap_on < today − settings.options_snapshot_days` → delete | §4.3: 90 days |
| Thinning | rows with `snap_on < today − options_full_days` AND (`delta IS NULL` OR `|delta| < 0.03` OR `|delta| > 0.97`) → delete | no strategy in the catalog picks outside delta 0.15-0.80 (§5.2); deep OTM/ITM rows are ~45 % of a chain and carry no replay information. Expiries are **not** thinned (calendars/diagonals/LEAPS need the far months) |
| Intraday | on each refresh: delete that symbol's existing `kind='intraday'` rows before insert; nightly: delete all `kind='intraday'` rows with `snap_on < today` | "only the latest intraday" (§4.3) |
| Expired | rows with `expiry < today − 7` → delete | an expired contract has no quote to replay; a week's grace lets the Positions monitor settle an expiry |
| `option_signal` | `snap_on < today − 90` → delete | matches the snapshot it was computed from |
| `option_jobs` | keep the newest 180 | enough history for "did it run last Thursday" |
| `option_idea_push` | Part D4's 45-day prune, invoked from the same call | one place prunes |
| `iv_daily` | none | the rank window is 252 sessions; 3 years ≈ 760 rows per ticker is nothing |

#### A2.5 SQLite vs Postgres

| Concern | SQLite (today) | Postgres (`TST_DATABASE_URL` switch) |
|---|---|---|
| Volume | fine to ~5 M rows / ~1 GB; `VACUUM` is not automatic, so a shrinking basket does not shrink the file | fine to the 100-ticker case and beyond |
| Writers | one writer at a time, readers block for the duration of a write transaction. The job commits **per symbol** (~3k rows, measured target < 1 s) so a page load never waits more than that; no `PRAGMA journal_mode=WAL` (SQLite-only syntax, forbidden by the data-handling rule; the one pragma in db.py:34 is the FK guard behind an `is_sqlite` check) | concurrent |
| `DateTime` | stored naive; `_utcnow()` is aware → compare via the `_age_hours` pattern (ivscan.py:158-167) | `timestamp without time zone` drops tzinfo; same pattern works |
| `JSON` | text; no indexing on keys (none needed — every lookup is by the indexed scalar columns) | `json`; same |
| Bulk insert | `db.add_all([...])` per symbol, then `commit` | same; `psycopg` batches |
| Copy on switch | one-time `pg_dump`-style copy of the tables above; the migration chain is identical on both | — |

**Switch trigger to write in the README:** basket union > 40 tickers, or `tst.db` > 2 GB, or a
nightly run's DB phase > 10 min.

### A3. Derived metrics — `app/services/option_metrics.py` (pure; every function takes plain lists/dicts and returns None rather than raising; unit-testable on synthetic data like `spread_scan.build_candidates`)

#### A3.1 HV20 / HV60

Price source: **`prices.fetch_daily_ohlc(symbol, rng="2y")`** (prices.py:50-67, Yahoo,
live, never parquet — the CLAUDE.md scope rule). The last bar carries `session_frac` while
the session is open (prices.py:341-345); such a bar is **excluded** (an intraday Refresh must
not compute HV on a half-day close).

```python
def hv(closes: list[float], n: int) -> float | None:
    """Close-to-close historical volatility, annualised, PERCENT.
    r_i = ln(C_i / C_{i-1}) over the last n returns (needs n+1 closes);
    HV = stdev(r, ddof=1) * sqrt(252) * 100. None if fewer than n+1 valid closes
    or any close <= 0."""
```

`hv20`, `hv60` from the same series; `iv_hv_premium = iv30 − hv20` (vol points; positive =
sellers are paid above realised, §6c).

#### A3.2 IV30: Cboe's figure vs our ATM figure

Cboe's `iv30` (probe: `32.034`, with `iv30_change`, `iv30_change_percent`) is Cboe's own
30-day constant-maturity at-the-money implied volatility for the underlying, in percent,
published with the delayed quote. It is the series `iv_history` has accumulated since
2026-09-10 and what `deploy/iv_seed_ibkr.py` seeds in the same unit. We **store it when the
source gives it** and **always also compute our own** so the series survives a source switch:

```python
def atm_iv_by_expiry(rows, spot, snap_on) -> dict[str, dict]:
    """{expiry: {"dte", "atm_iv" (pct), "n_legs"}}.
    Per expiry: K1 = highest strike <= spot, K2 = lowest strike > spot (both must
    exist and be within 15% of spot). Legs = call+put at K1 and K2 with
    0.01 < iv < 5.0 and bid > 0 (a quote, not a stale print). Need >= 2 legs.
    iv(K) = mean of the call and put iv present at K;
    atm_iv = iv(K1) + (iv(K2) - iv(K1)) * (spot - K1) / (K2 - K1), then * 100."""

def iv30_constant_maturity(by_expiry, snap_on) -> float | None:
    """Interpolate in VARIANCE-TIME between the two expiries bracketing 30 calendar
    days (T in years = dte/365), the standard CM formula:
        sig30^2 = [sig1^2*T1*(T2-T30) + sig2^2*T2*(T30-T1)] / [(T2-T1)*T30]
    Expiries with dte < 5 are ignored (settlement noise). If no expiry is beyond
    30 days use the nearest at or under; if none under, the nearest above."""
```

`iv_daily.iv30` = Cboe's figure (`iv30_src='cboe'`) when present, else `atm_iv30`
(`iv30_src='atm'`), else the IBKR value (`'ibkr'`, bootstrap/seed rows). `atm_iv30` is
filled on every source. Calibration check (logged, surfaced in A7): the median of
`|iv30 − atm_iv30|` over the last 20 days per ticker; > 1.5 vol points → the card's IV line
reads "IV rank is computed on Cboe's IV30; our ATM read differs by N pts" so a source
switch is never silent.

#### A3.3 IV rank and IV percentile

Window: the last **252 `iv_daily` rows for the symbol with `on <= today`, today included**
(the bridge's `_iv` includes the current value in min/max, ibkr_bridge.py:558-562; the
existing `spread_scan.iv_percentile` excludes today — spread_scan.py:222-236 — and is left
as it is for the Spread page).

```python
IV_MIN_OBS = 20          # percentile shown (spread_scan.IV_MIN_OBS, spread_scan.py:69)
IV_RANK_MIN_OBS = 60     # rank shown: a quarter of readings before "52-week" high/low means anything
IV_FULL_OBS = 252        # a full year: the rank is "over the last year", basis = rank

def iv_rank_pct(series: list[float], current: float) -> dict:
    """series = up to 252 past+today iv30 values (pct), current = today's.
    rank = (current - lo) / (hi - lo) * 100  (None if hi == lo)
    pct  = count(v < current) / N * 100     (strictly below, as spread_scan.percentile)
    -> {"iv_rank", "iv_pct", "n", "lo", "hi", "state"}
    state (the contract's five values, by n):
      "none"      n == 0               nothing at all (a ticker added tonight)
      "forming"   0 < n < 20           accumulating; neither figure shown
      "pct_only"  20 <= n < 60         percentile shown, rank withheld
      "rank_ok"   60 <= n < 252        rank + percentile over n days (not a full year)
      "ok"        n >= 252             rank over the last year."""
```

`basis` (what the gauge's verdict rests on; Part B's gauge sets it from `state` + HV):
`rank` when `state == "ok"`; `percentile` when `state in ("rank_ok", "pct_only")` — a
rank/percentile exists but over fewer than 252 days; `provisional` when `state == "forming"`
and HV20 is present (the verdict rests on IV−HV alone, B1.5's "< 20 obs, HV present");
`unknown` when `state == "none"`, or `forming` without HV. `provisional: bool` =
`basis in ("provisional", "unknown")` — the Telegram push skips such rows (A4.4). Every
`basis != "rank"` wording **carries the day count** (contract, SIGNAL): the gauge reads
"Options look expensive against the last 34 days (not a full year yet)", the basket IV cell
shows **`~62`** dotted-underlined and is never coloured amber on a provisional read;
`iv_rank_words` says "over 118 days" / "over the last year".

What the page shows before the minimum is met (Part C/D render; this part supplies `state`,
`basis` and `n`): `forming` / `pct_only` → **"IV rank: not enough history yet (34 of 60
days). It fills in by itself; if you run TWS on this PC, Live loads a year at once."** (not
"press Live" — most members on the Hermes site have no TWS; critic 1 minor); `rank_ok` → the
number with the footnote "over 118 days"; `none` → **"We cannot yet say whether options are
expensive — 12 of 60 days of history. If you have TWS on this PC, press Live to load a
year."** with the basket cell `–`. The gauge treats `None` rank as *unknown*, never as
*low* — the `ivscan._list_context` rule (ivscan.py:192-194).

#### A3.4 IV per expiry, term structure

`iv_by_expiry` (A3.2) is stored as JSON. Front/back:

```python
TERM_EVENT = 1.05        # front / back above this: an event is priced in the front (calendars: sell front)
TERM_CONTANGO = 0.95     # below this: normal upward curve

def term_structure(by_expiry) -> dict:
    """front = expiry nearest 30 DTE with dte >= 7; back = nearest 75 DTE with dte >= 45
    (both must exist, else None).
    term_ratio = iv_front / iv_back               # unitless, ticker-relative; the ONE term figure
    >= TERM_EVENT (1.05)   backwardation: an event is priced in the front
    <= TERM_CONTANGO (0.95) contango: normal upward curve
    -> {"iv_front", "iv_back", "term_ratio", "front_expiry", "back_expiry"}"""
```

Part B's gauge and recommender read `term_ratio` against the same two constants (B's
`gauge["term_ratio"] >= 1.0` test for calendars); no other term definition exists in any
part (critic 2 major).

#### A3.5 25-delta put/call skew

At the front expiry: the put whose |delta| is nearest 0.25 and the call whose delta is
nearest 0.25, each within 0.07 of it and with a valid iv.
`skew25 = (iv_put25 − iv_call25) × 100` (vol points, positive = puts richer, the normal
equity shape); `skew_norm = skew25 / atm_iv_front` (unitless, so a 60-vol name and a 20-vol
name compare). Both None if either leg is missing.

#### A3.6 Expected move

Per expiry: `em_1sd = spot × (atm_iv/100) × sqrt(dte/365)` stored inside `iv_by_expiry`;
`iv_daily.expected_move = spot × (iv30/100) × sqrt(30/365)` (the 30-day 1σ figure the card
quotes). The page shows "±$em (±pct)" and the engines use the per-expiry value for "short
strike outside the expected move".

#### A3.7 Days to earnings

`prices.fetch_next_earnings(symbol)` (prices.py:178-191, cached 6 h, soft-fail None) →
`earnings_date`, `earnings_days`. `earnings_inside(expiry)` = `today <= earnings_date <=
expiry`, the rule `spread_scan.build_candidates` already applies (spread_scan.py:140).
None = unknown, displayed as "earnings date unknown", never as "no earnings". An unknown
date is a warning on the card but a **veto for the Telegram push** (A4.4: "skipped: earnings
date unknown"), and an open trade is re-checked every day against a date that appears or
moves after entry (Part B7's "earnings now inside" WATCH-urgent row; the nightly job
refreshes `earnings_date` per symbol, so the grader always sees tonight's date).

#### A3.8 ATR(14)

Not stored in `iv_daily` (it is a chart statistic), but the nightly job computes it once per
ticker from the same bars — `support_bounce.atr_series(highs, lows, closes, 14)[-1]`
(support_bounce.py:150-166, Wilder) — and passes it to the engines, so every chart-relative
threshold (CLAUDE.md "Normalized strategy parameters") uses the one ATR the support
detector uses. It is written into `option_signal.setup.atr` so the card, the sizing line
and the payoff chart read the same number.

### A4. The nightly job

#### A4.1 Script and task

`deploy/options_nightly.py` is `deploy/spread_scan.py` with the body swapped (same
`sys.path.insert` 28, `init_db()` 49, the Alembic-logging re-assert 50-54, UTF-8 reconfigure
from `portfolio_daily_check.py:35-39`, exit 0 = ran / 1 = could not run):

```
cd C:\trading-skills\TradeHunter\dashboard_tst          # Hermes, PowerShell
.\.venv\Scripts\python.exe deploy\options_nightly.py              # every basket ticker
.\.venv\Scripts\python.exe deploy\options_nightly.py NVDA LRCX    # just these
.\.venv\Scripts\python.exe deploy\options_nightly.py --backfill   # also copy iv_history -> iv_daily (A6.1)
.\.venv\Scripts\python.exe deploy\options_nightly.py --no-push    # skip Telegram entirely
.\.venv\Scripts\python.exe deploy\options_nightly.py --telegram-dry-run   # compose + log, send nothing
.\.venv\Scripts\python.exe deploy\options_nightly.py --on 2026-10-02 --source alpaca -v
```

`deploy/setup_options_nightly_task.ps1` registers **`TST-Options-Nightly`**, daily at
**07:15 local (MYT)**: after `TST-Portfolio-Check` (06:00) and after `TST-Spread-Scan`
(06:30, ~30 min for ~550 symbols, setup_spread_scan_task.ps1:42-43) so two jobs never hit
Cboe's CDN at once (the 429 was measured at ~24 requests in 10 s, option_quotes.py:127-129).
07:15 MYT = 19:15 ET (EDT) / 18:15 ET (EST): same ET day, after the close. Execution limit
**30 min**; log `logs\options_nightly.log` appended through the same `cmd /c ... >> log 2>&1`
redirect (setup_spread_scan_task.ps1:38-39).

**Budget:** `MAX_BASKET = 60` per member (A6.2) is set by this limit — 60 tickers × ~2.5 s
(1.5 s pacing + ~1 s of metrics, engines for every distinct prefs hash, and the per-symbol
commit) ≈ 2.5 min for one member's full basket; the union across members is what the job
walks, and ~400 distinct symbols would fill the 30 minutes — one of the Postgres switch
triggers (A2.5). The per-hash engine pass is cheap (the chart read runs ONCE per symbol,
A4.2 f; only the picker repeats), so ten distinct rule sets add well under 1 s per symbol.
Not in this job's budget but on the same Yahoo quota: `option_exits.sweep` (Part B7.3, the
Positions grader) fetches `ema_setup.setup_for(sym, deep=True)` once per underlying that
holds a LEAPS / diagonal trade (cached 15 min) so the weekly-trend exit has its bars
(critic 2 minor).

#### A4.2 Ordering, per run

```
run_nightly(db, symbols=None, on=None, source=None, push=True, telegram_dry_run=False,
            backfill=False, progress=None):
  0. job = job_runs.start(db, job="nightly", run_on=on or et_today(), source=src.name); commit
  1. if backfill: option_store.backfill_from_iv_history(db)                      # A6.1, idempotent
  2. universe = option_store.basket_universe(db)   # distinct ACTIVE symbols over all owners, sorted
     (+ every symbol with an OPEN option_trades row, so a tracked position's chain is always fresh)
     hashes = option_prefs.distinct_hashes(db)     # [HOUSE_HASH] + every DISTINCT user_option_prefs.prefs_hash
  3. for sym in universe (sequential, src.capabilities.pacing_seconds between fetches):
        t0 = now
        a. chain = option_data.fetch_chain(sym, fresh=True, retries=3)      # ChainError -> record, continue
        b. bars = prices.fetch_daily_ohlc(sym, "2y"); long_bars for the weekly read (ema_setup);
           earnings = prices.fetch_next_earnings(sym)
        c. metrics = option_metrics.all_for(chain, bars, earnings, iv_series=option_store.iv_series(db, sym, 252))
        d. option_store.replace_snapshot(db, chain)                           # delete (sym, snap_on, 'eod') + add_all
        e. option_store.upsert_iv_daily(db, chain, metrics)                   # query-then-write
        f. expiries = sorted({r.expiry for r in chain.rows})
           state = chart_state.read(bars, long_bars, atr, at=expiries)       # Part B: ema_setup.analyze() ONCE per
                                                                              # symbol; inside it trend_line.find(...,
                                                                              # at=expiries) fills tl.value_at for
                                                                              # EVERY expiry in the snapshot (Part C);
                                                                              # no detector is run a second time
           for h, prefs in hashes:                                            # house first, then each saved hash
               sig = option_engine.compute(chain, metrics, state, prefs)      # Part B: gauge, recommender, picker
               option_store.upsert_signal(db, sig, prefs_hash=h, user_id=None)
           # picks are stored WITHOUT sizing: option_sizing.size runs at read time (A5.3)
        g. db.commit()                                                        # per symbol: SQLite readers wait < 1 s
        h. job.detail[sym] = {"status", "ms": now - t0, "n": len(chain.rows), "err"}; progress(sym, err)
  4. option_store.prune(db, today)
  5. if push: job.pushed = telegram_push.run(db, as_of=run_on, dry_run=telegram_dry_run)   # Part D4; A4.4; soft-fail
  6. job_runs.finish(db, job, ok/errors/rows); commit; return summary dict (the spread_scan.run_scan shape, 396-397)
```

Soft-fail per ticker at every letter: a `ChainError` (a) records `status=error` and moves
on; a Yahoo failure (b) leaves `hv*`/`earnings_*` None and the chain is still stored; an
engine exception (f) is caught, logged with traceback, written as
`option_signal.status='error'` with the message (for that hash; the other hashes still
run), and the data rows (d, e) are **kept** — a card that says "the rules could not run
tonight" over fresh data beats a blank. The job exits 0 whenever step 0 succeeded (a missing
chain is "a normal Tuesday", portfolio_daily_check.py:18-19).

Computing every distinct hash in step 3f is what makes the basket honest on first paint
(critic 1 major): a member whose rules were saved before the run finds their row ready; only
a member who changes rules AFTER the run sees the basket's third state, **not checked yet**
(grey dot, "open the card to check your rules"), until the card open computes it (A5.3).

#### A4.3 Logging and the health record (dashboard-visibility rule)

| Surface | What |
|---|---|
| `logs\options_nightly.log` | one line per symbol (`%-6s ok 3,710 rows 1.9s` / `skipped: ...`), the summary line, the Telegram result (`pushed 7, skipped 3 (earnings date unknown ×2, provisional ×1)`) |
| `option_jobs` row | counts + `detail` JSON per symbol + `pushed`; written at start through `job_runs.start` (so a crash mid-run leaves a row with no `finished_at` = "started, never finished") |
| `GET /options/badge` (require_user, JSON) | `{"run_on", "finished_at", "ok", "errors", "stale": run_on < last ET trading day, "running": finished_at is None and started < 40 min ago, "job_missed": job_runs.missed(db) — no finished nightly run for the last ET trading day by 08:00 MYT, "ideas_new": house rows for run_on with a recommended strategy and a pick, "urgent", "watch": counts of the member's latest option_trade_checks verdicts (Part B7.3)}` — read from the latest `option_jobs` row via `job_runs.latest` and from `option_trade_checks`, never from a market call (the `/portfolio/badge` principle, portfolio.py:430-434) |
| `GET /options/status/strip` (require_user, HTML) | renders `_options_status.html` from the same JSON: "Data as of Oct 2, 16:00 ET · delayed · job 07:17 MYT ✓" / amber "last run Oct 1 — tonight's run missed" / rose "run failed: N of M tickers" (Part D renders) |
| Admin Data Ingest page (`/finviz`) | one extra row "Options nightly" from `/options/badge` (optional; same turn as the pill if cheap) |

The JSON lives at `/options/badge`, the HTML at `/options/status/strip`; no other status
route exists (contract, JOBS).

#### A4.4 Telegram push (decision 10) — Part D's `telegram_push`, called from step 5

The push is designed ONCE, in Part D4: `app/services/telegram.py` (the sender — it reuses
`scripts._common.telegram_env` (_common.py:562-618; creds `telegram.env`, then `matp.env`,
from the vault / in-folder `.env`, imported through the `resources_bridge` sys.path) but
takes a **`chat_id` parameter**, because `send_telegram` has no chat id of its own and a
multi-member platform needs one per member) + `app/services/telegram_push.py::run(db,
as_of, dry_run)` + the table `option_idea_push`. This part only **calls** it — step 5 of
A4.2 — and records the count in `option_jobs.pushed`. `option_signal` carries no push
timestamp column, this part has no push module of its own, and `option_exits` has no
exit-line `notify`; the exit-line alerts stay on the existing Discord path.

What this part guarantees the push can rely on, row by row from `option_signal` /
`iv_daily` (the guards live in `telegram_push.run`; the data they test is written here):

| Guard (skip the idea when…) | Supplied by |
|---|---|
| `iv_daily.partial` (the chain looked incomplete, A1.3) | A4.2 e |
| `option_signal.status != 'ok'` | A4.2 f |
| `signal.iv.provisional` (basis provisional / unknown, A3.3) | A3.3 |
| `signal.iv.earnings_date is None` → the job log says `skipped: earnings date unknown` | A3.7 |
| `rule.step > current_step` (an unbuilt strategy is never pushed) | Part B's rule table |
| `as_of` older than the last session (a stale snapshot is never pushed as tonight's idea) | A1.3 `as_of` |

Dedupe: key `(symbol, strategy, front expiry)` per member in `option_idea_push`; the same
key is re-pushed only if the short strike moved **> 1 ATR** (`setup.atr`) since the last
push — spot drifting one listed strike a day is NOT a new idea. Cap **5 ideas per message**
ordered by `score` ("and N more on the page"). Per-member opt-in with the chat-id handshake
(the member sends `/start` to the bot → a 6-digit code → the drawer accepts the id only
with that code; `POST /options/telegram`, Part D), a per-member **quiet** switch and a
**"pause for 7 days"** link in every message. A re-run of the job the same day sends
nothing new (the dedupe rows survive the run, A4.5).

Wording the push takes from the stored row: the first line is always *"Ideas for tonight's
US session (opens 21:30 Malaysia). Prices are last night's close."*; then per idea
`headline` + the chosen strategy's `must_happen` + the pick's words with the chance figure
as *"about {p}% chance of keeping it (estimate)"* and the `/options?sym=LRCX` link
(`settings.public_url`). **Telegram never states a contract count** (sizing is a read-time,
per-member figure, A5.3; the push would otherwise quote a number computed from an NLV the
member may not have stored). Not configured → logged once, skipped.

#### A4.5 Idempotency (re-run the same day)

| Table | Re-run behaviour |
|---|---|
| `option_chain_snapshot` | `replace_snapshot` deletes `(symbol, snap_on, kind)` then inserts — same rows, same count |
| `iv_daily` | upsert on `(symbol, on)`; an EOD row overwrites an intraday one for the same day (`kind` → `eod`); the IBKR bootstrap **never** overwrites a `cboe`/`alpaca` row (A4.6) |
| `option_signal` | upsert on `(symbol, snap_on, kind, prefs_hash)`, one row per hash in use; nothing about the push is kept on this row |
| `option_idea_push` | untouched by the signal upsert; `telegram_push.run` finds the same `(symbol, strategy, front expiry)` keys and sends nothing (unless the short strike moved > 1 ATR, A4.4) |
| `option_jobs` | a new row per run (history); the badge reads the latest **finished** one (`job_runs.latest`) |
| `--on` | files under that date, like `spread_scan.py --on` (deploy/spread_scan.py:36) — for refiling after a holiday |

#### A4.6 The IBKR "Live" bootstrap into `iv_daily` (decision 4) — a step inside `POST /options/live/{symbol}`

Bridge **1.6**: `/iv?symbol=X&series=1` returns, in addition to today's summary
(`_iv`, ibkr_bridge.py:541-563), `"series": [{"on": "YYYY-MM-DD", "iv": 31.2}, ...]` in
**PERCENT** — `round(b.close * 100, 1)`, the unit `iv_current / iv_low / iv_high` in the same
reply already use (ibkr_bridge.py:559-563), from the dated daily IV bars the bridge already
fetches; oldest first; ≤ 400 points. Without `series=1` the response is unchanged, so the IV
Rank page and the Watchlist tab keep working on either bridge version. `server_version` →
`TradeHunterIBKRBridge/1.6` (1.4 is "open interest per leg", 1.5 "no fixed waits" —
ibkr_bridge.py:631 — so 1.6 is the next free number; every member-facing string says
**"older than 1.6"**).

Browser flow on the Options card's **Live** button (Part D's JS, same shape as
`ivscan.html:187-205`): the `/iv?series=1` reply travels in the SAME body as the chain
(A1.5, `"iv": {...}`); there is no separate bootstrap endpoint.

```
GET 127.0.0.1:9224/iv?symbol=LRCX&series=1          -> r
if (!r.series) -> iv part reported as "Your bridge is older than 1.6 — restart bridge\start_ibkr_bridge.bat"
                  (the ivscan.html:276-278 message pattern); the chain part still grades live
POST /options/live/LRCX  {chain: ..., iv: {series: r.series, iv_current: r.iv_current, iv_rank: r.iv_rank,
                          iv_percentile: r.iv_percentile}, nlv: ..., diag: {...}}
   -> the card partial; its IV line now carries {inserted, skipped, n_total, iv_rank, iv_pct, state}
```

Server (`option_store.bootstrap_iv(db, symbol, series, *, source="ibkr")`): bounded input
(≤ 400 points, dates `<= today` and `>= today − 400d`, **`0.1 <= iv <= 1000`** — the series is
already percent, stored **as-is**, no multiplication; the `_b` bounding style of `ivscan_iv`,
ivscan.py:351-360); for each point insert an `iv_daily(kind='history', source='ibkr',
iv30=iv, iv30_src='ibkr')` row **only if no row exists for that day** — a day the server read
itself is never replaced by a broker series; then recompute today's
`iv_rank/iv_pct/iv_n/iv_state` from the now-full window and update today's row; write an
`option_jobs(job='bootstrap', symbols=1, rows=inserted)` row; mark every `option_signal` row
for the symbol on the latest `snap_on` `status='stale_iv'` so the next card read recomputes
the gauge (A5.3). Returns in < 200 ms.

The Hermes-side bulk alternative stays `deploy/iv_seed_ibkr.py` unchanged (writes
`iv_history`); the nightly `--backfill` / step 1 copies it over (A6.1).

#### A4.7 On-demand Refresh

`POST /options/refresh/{symbol}` (require_user; member must have the symbol in a basket or
an open `option_trades` row) → `option_nightly.refresh_symbol(db, sym, user)`: the
per-symbol pipeline of A4.2 with `kind='intraday'`, `fresh=True`, `retries=0` (a page request
never sits through a backoff, option_quotes.py:127-130), computing the house signal **and**
the caller's `prefs_hash` signal; writes an `option_jobs(job='refresh')` row through
`job_runs`; returns the re-rendered card. Measured budget: Cboe fetch 1-2 s + metrics/engines
< 0.5 s. Rate-limited per member to one refresh per symbol per 60 s (the quote cache would
answer the same chain anyway) — with ONE exception: when the order ticket finds `as_of`
older than the last session close while the US session is open it renders a **"Refresh
first"** banner with the Refresh button inline, and that first refresh is never blocked by
the cooldown (contract, ORDER TICKET; the member is about to send an order on 14-hour-old
prices otherwise).

### A5. The `option_signal` cache contract

#### A5.1 Keys

`(symbol, snap_on, kind, prefs_hash)` is unique. `prefs_hash` = first 12 hex of
`sha1(canonical_json(pick_relevant(merged_prefs)))` where `pick_relevant` keeps **only the
fields that change a pick** — delta bands, DTE, widths (`width_atr_lo/hi`, `wing_atr_lo/hi`),
credit / reward floors, liquidity (`min_oi`, `max_leg_spread`), `earnings_rule`,
`monthly_only`, `chart_constraint` — and **never** `nlv`, `risk_pct`, `GAP_MULT`, the exit
lines or the Telegram settings (critic 2 major: an account-value edit must not invalidate
every cached pick; sizing runs at read time, A5.3). `canonical_json` sorts keys, rounds
floats to 4 dp, drops keys equal to the house default **only if** `schema_version` matches
(so the house row and a member who changed nothing share one hash and one computation —
"members on house defaults share one signal row"). `HOUSE_HASH =
prefs_hash(option_prefs.clean({}))`; `option_prefs.distinct_hashes(db)` = `[HOUSE_HASH]` +
every distinct `user_option_prefs.prefs_hash` (the nightly job computes all of them, A4.2).
`engine_version` is a constant in `option_engine` (Part B), bumped on any rule change — a
row with an older `engine_version` is treated as missing.

#### A5.2 What the engines write (one row; JSON column shapes)

The shapes below are the contract (SIGNAL / PICK / LEG). `setup.sup`, `setup.tl`,
`setup.tl_bounce` and `setup.rng` are Part C's dicts **verbatim** as `ema_setup.analyze()`
stores them; `setup.plan` is Part B's plan and is the ONE source of the chart stop
(`setup.stop`/`setup.target` do not exist as separate keys — read `plan.stop`, `plan.target`).
Figures are the LRCX fixture's (ATR 11.5, support zone low 339.1 → chart stop `339.1 −
LEVEL_PAD_ATR (0.25) × 11.5 = 336.2`; the mockup's 338 is replaced everywhere); P/L values are illustrative.

```json
{
  "status": "ok",
  "trend": "up",
  "headline": "Uptrend: EMA 20 above 50 above 200 for 34 days ... paid to sell a put spread below that support.",
  "setup": {
    "kind": "support_bounce", "direction": "long", "level": 340.9, "zone": [339.1, 342.0],
    "touches": 3, "quality": "A", "close": 349.2, "trend_days": 34, "atr": 11.5,
    "ema": {"e20": 346.1, "e50": 335.8, "e200": 301.2},
    "plan": {"entry": 349.2, "stop": 336.2, "target": 375.2, "r": 13.0},
    "levels": {"support": 340.9, "resistance": 372.0},
    "sup": {"...": "support_bounce.find() dict verbatim (Part C1)"},
    "tl": {"direction": "up", "p1": {"time": "2026-07-08", "price": 318.6}, "p2": {"time": "2026-09-12", "price": 334.1},
           "i1": 188, "slope_per_bar": 0.33, "slope_atr": 0.03,
           "touches": [{"time": "2026-07-08", "price": 318.6}, {"time": "2026-08-14", "price": 326.9}, {"time": "2026-09-12", "price": 334.1}],
           "n_touches": 3, "span_bars": 61, "value_today": 338.4,
           "value_at": {"2026-10-17": 339.8, "2026-11-20": 341.9, "2026-12-19": 343.8},
           "broken": false, "last_break": null, "warning": null, "residual_atr": 0.2, "atr": 11.5, "channel": null},
    "tl_bounce": null,
    "rng": {"low": 318.6, "high": 372.0, "zone_low": [317.1, 320.4], "zone_high": [370.2, 373.9],
            "n_low": 2, "n_high": 2, "touches_low": ["..."], "touches_high": ["..."], "width_atr": 4.6,
            "pos_pct": 57.0, "stack_flat": false, "sideways": false, "reasons": ["EMA stack rising", "EMA20 moved 1.4 ATR in 10 bars"]},
    "evidence": ["EMA20 > EMA50 > EMA200 for 34 sessions", "bounce candle 2026-10-01 on 1.7x volume"]
  },
  "iv": {
    "iv30": 41.2, "hv20": 28.9, "hv60": 31.4, "iv_hv_premium": 12.3,
    "iv_rank": 62.0, "iv_pct": 71.0, "iv_n": 252, "state": "ok", "basis": "rank", "provisional": false,
    "iv_front": 42.0, "iv_back": 40.4, "term_ratio": 1.04, "skew25": 4.1, "skew_norm": 0.10,
    "expected_move": 21.6, "earnings_date": "2026-10-22", "earnings_days": 19,
    "verdict": "SELL", "verdict_why": "IV rank 62 (>= 50) and IV 12 pts above realised",
    "gates": {"sell_directional": true, "sell_neutral": true, "buy": false, "term_event": false},
    "iv30_src": "cboe", "atm_iv30": 40.6, "lo": 24.1, "hi": 51.8
  },
  "strategies": [
    {"key": "bull_put", "label": "Bull put spread", "fit": "recommended", "score": 8.4, "step": 1,
     "why": "uptrend + support holding + IV rank 62", "must_happen": "LRCX stays above 330 until Nov 20",
     "reasons": [], "reason_key": null, "shown": true},
    {"key": "bull_call", "label": "Bull call spread", "fit": "also_fits", "score": 6.1, "step": 2,
     "why": "...", "must_happen": "...", "reasons": [], "reason_key": null, "shown": true},
    {"key": "leaps_call", "label": "Buy LEAPS call", "fit": "also_fits", "score": 5.0, "step": 4,
     "why": "the long-term chart would suit a long-dated call; that strategy is not in TradeHunter yet",
     "must_happen": "...", "reasons": [], "reason_key": "not_available_yet", "shown": true},
    {"key": "buy_call", "label": "Buy call", "fit": "rejected", "score": 2.0, "step": 2,
     "why": "...", "must_happen": "...", "reasons": ["options too expensive (IV rank 62)"], "reason_key": "expensive", "shown": true},
    {"key": "iron_condor", "label": "Iron condor", "fit": "rejected", "score": 0.5, "step": 3,
     "why": "...", "must_happen": "...", "reasons": ["trending, not sideways"], "reason_key": "trending_not_sideways", "shown": false},
    {"...": "the other five rows (buy_put, bear_put, bear_call, calendar, diagonal_call) in the same shape — all TEN are stored"}
  ],
  "picks": {
    "bull_put": [
      {"legs": [{"expiry": "2026-11-20", "right": "P", "strike": 330, "side": "sell", "qty": 1, "price": 3.10, "bid": 3.00, "ask": 3.20, "iv": 0.43, "delta": -0.26, "oi": 2140, "volume": 412},
                {"expiry": "2026-11-20", "right": "P", "strike": 320, "side": "buy",  "qty": 1, "price": 1.00, "bid": 0.95, "ask": 1.05, "iv": 0.45, "delta": -0.15, "oi": 1630, "volume": 230}],
       "credit": 2.10, "max_profit": 210, "max_loss": 790, "breakevens": [327.90],
       "pop": 0.74, "pop_kind": "keep", "return_on_risk": 0.266, "score": 0.197, "tag": "best return",
       "chart_stop": 336.2, "chart_stop_pl": -125, "rule_stop_pl": -158,
       "sizing": null,
       "words": {"collect": "you collect $300-$317 (worst likely fill to mid)", "risk": "you risk $790", "chance": "about a 74% chance of keeping the credit ..."},
       "why": "short strike 330 sits under support 340.9 and under the trend line at expiry (341.9)",
       "liquidity": {"tier": "clean", "oi_min": 1630, "spread_max": 0.20},
       "checks": [{"name": "open interest >= 500", "ok": true}, {"name": "earnings inside expiry", "ok": false, "blocking": false, "detail": "Oct 22 is inside Nov 20"}]}
    ]
  }
}
```

Vocabulary the row is written with (fixed; the templates branch on these, never on prose):

| Field | Values |
|---|---|
| `trend` | `up` · `down` · `sideways` (iff `setup.rng.sideways`) · `unclear` |
| `strategies[].key` | `buy_call, buy_put, bull_call, bear_put, leaps_call, bull_put, bear_call, iron_condor, calendar, diagonal_call` (ten rows, always) |
| `strategies[].fit` | `recommended` · `also_fits` · `rejected` — ordered in that sequence; `shown=True` on the recommended, the also-fits and the ≤ 2 near-miss rejects |
| `strategies[].reason_key` | `expensive` · `cheap_options` · `not_rich_enough` · `trending_not_sideways` · `no_range` · `wrong_direction` · `no_setup` · `earnings_inside` · `front_iv_under_back` · `no_long_dated` · `no_weekly_trend` · `not_available_yet` (null when not rejected) |
| `strategies[].step` | the build step of the rule (Part B); a rule with `step > current_step` is **never** `recommended` — it goes to `also_fits` with `reason_key='not_available_yet'` (the chip "· not available yet"); the words "step N" never reach a member |
| `iv.state` / `iv.basis` / `iv.verdict` | A3.3's five states / `rank, percentile, provisional, unknown` / `SELL, NEUTRAL, BUY, UNKNOWN` |
| `picks[*][*].pop_kind` | `keep` (credit families: `1 − |delta_short|`, two-sided for the condor) · `profit` (every other family: Part C's `payoff.pop(...)`, the ONE function) — the pick carries no wording key of any kind; `option_words.pop_words(pick)` renders the two sentences |
| `picks[*][*].liquidity.tier` | `clean` · `limit` · `wide` · `thin` · `unknown` |
| leg `side` / `right` / `iv` / `oi` | `sell`/`buy` with a POSITIVE `qty` (Part C's `payoff.Leg.from_dict` derives the sign) / `C`/`P` / FRACTION / `oi` (never `open_interest`) |

Rules this part enforces on the writer: every number the page shows comes from this row
(no recomputation on read) **with one deliberate exception — `sizing`**, stored `null` and
filled at read time by `option_sizing.size(pick, nlv, prefs)` (A5.3), because the account
value is per member and per moment and is not part of the hash; `picks` carry the **leg
prices as quoted at `as_of`** so a member sees what was true when the idea was made;
`strategies` holds *all ten* rows ordered recommended → also_fits → rejected (decision 9:
the chip row shows the recommended first, up to two rejected with reasons, the rest behind
"other strategies" — the template reads `shown`); `headline` is composed at **write** time
by `option_words.headline(...)` (Part D2.7's templates, moved to services) and a chip click
never changes it; `max_loss` / `max_profit` are **positive** $ per contract; `breakevens` is
a list even for one value.

**Not stored — derived on request from this row.** The payoff chart: `GET
/options/payoff/{symbol}?strategy=&pick=&units=$|R` runs Part C's `payoff.build()` over the
pick's `legs`, `setup.plan.stop`, the rule stop, `levels` and `tl.value_at[expiry]` and
renders `_payoff_chart.html` (server-side SVG; the ONE implementation — there is no payoff
JSON column, canvas or client formula). The order ticket: `GET /options/ticket/{symbol}`
runs Part B's `order_ticket.build(pick, setup, prefs)` + `render(ticket, broker)` at request
time — it needs the read-time sizing, the `as_of`-vs-session check ("Refresh first", A4.7)
and the member's "enter on the dip" toggle, none of which belong in a nightly row.

#### A5.3 Read path and invalidation

`option_store.card_for(db, symbol, user)` and `option_store.basket_rows_for(db, user)` are
the **only** two reads of `option_signal` in the code base (contract, SIGNAL); no route
queries the model directly (a `.one_or_none()` on `symbol` alone raises on the second
member — critic 2 blocker).

```python
def card_for(db, symbol, user) -> dict:
    prefs = option_prefs.for_user(db, user)             # merged; .hash over pick-relevant fields (A5.1)
    day, kind, as_of = option_store.latest_snap_on(db, symbol)   # newest eod/intraday on file
    row = option_store.signal(db, symbol, day, prefs.hash, engine_version=CURRENT)
    if row is None or row.status == "stale_iv":
        chain = option_store.latest_chain(db, symbol)   # the stored rows, ~3k, < 50 ms
        row = option_engine.compute(chain, iv_daily_row, bars_cached, prefs) ; upsert     # ~100-300 ms, NO market call
    nlv = trade_prefs.read(db, user).get("nlv") or None # B5.3 order: the Live figure (only inside POST /options/live) ->
                                                        # the stored trade_prefs nlv -> None + the note; never auto-written
    for picks in row.picks.values():
        for p in picks:
            p["sizing"] = option_sizing.size(p, nlv, prefs)   # read time, microseconds; 0 contracts is a valid answer
    return row + {"stale": _stale(day), "age_sessions": _age(day), "kind": kind, "as_of": as_of}

def basket_rows_for(db, user) -> list[dict]:
    """ONE batched query: for every symbol in the member's basket the latest row on the
    newest snap_on for the member's hash, else the house hash. Per symbol:
    {symbol, trend, iv: {iv_rank, basis, iv_n}, idea: <recommended key or None>,
     pick_state in {"has_picks", "no_strike_passes", "not_checked"}, stale}."""
```

`_stale(day)`: `snap_on` older than the **previous ET trading day** — i.e. at least two
sessions behind the ET session date (`spread_monitor.et_today()` rolled back to the latest
weekday) — never a wall-clock age; between the close and the 07:15 MYT run the badge's
`job_missed` / `running` fields say what is going on, not `stale`.

`option_sizing.size(pick, nlv, prefs)` (Part B5, the ONE sizing function): `contracts =
min(floor(risk_budget / loss_at_chart_stop_usd), floor(risk_budget × GAP_MULT /
max_loss_usd), floor(nlv × 10% / notional))` with `risk_budget = nlv × risk_pct`; **0 is a
valid answer** with the note *"Not even one contract fits your 1% — lower the risk or choose
a narrower spread"*; never `max(1, …)`; `nlv is None` → `contracts = None` + the note to
set the account value. The card's sizing line always shows both figures: *"{n} contracts:
about ${loss_at_stop} if the stop fires, up to ${max_loss_total} ({pct}% of your account)
if the stock gaps past it"*.

The three basket states (critic 1 major — "no strike passes" was a false statement for a
row that had simply not been computed):

| `pick_state` | Condition | Cell |
|---|---|---|
| `has_picks` | a row for the member's hash on the latest `snap_on` with `picks[recommended]` non-empty | the idea chip |
| `no_strike_passes` | a row for the member's hash exists and `picks[recommended]` is empty | faded chip, title "recommended, but no strike passes your rules today" |
| `not_checked` | no row for the member's hash on the latest `snap_on` (rules saved after the nightly run) — the house row stands in for `trend` / `iv` | grey dot, title "open the card to check your rules" |

| Event | Effect |
|---|---|
| nightly run / Refresh writes a new `(symbol, snap_on, kind)` | older-day rows are simply not the latest; no delete needed |
| member saves rules | `user_option_prefs.prefs_hash` changes → next read misses → lazy compute from the stored chain (no market call); the basket shows `not_checked` until then; the next nightly run computes the new hash with the rest |
| member edits the account value / risk % / exit lines / Telegram | hash unchanged (A5.1) → no recompute; `sizing` changes on the next read |
| house defaults change (constant edit, later admin edit) | `HOUSE_HASH` changes → nightly writes new house rows; members on house defaults follow automatically |
| engine rule change | `engine_version` bump → every row misses → lazy compute on open; the nightly job writes fresh rows for every hash |
| Live bootstrap | every row for the symbol on the latest day `status='stale_iv'` → recompute on next read |
| chain older than 2 sessions | row served with `stale=True`; the page shows the amber badge and the Refresh button, never a blank (§2.5) |

A member only ever reads rows computed under their own rules or the house rules (the lookup
is by `prefs_hash`); two members with identical rule sets share one row, which is correct —
the rules are the only input. `user_id` on the row is informational, never a filter.

### A6. Migration path for existing data

#### A6.1 `iv_history` → `iv_daily` (`option_store.backfill_from_iv_history(db)`)

Idempotent, run on `--backfill` and as nightly step 1 (cheap: one query per symbol in the
basket, inserts only missing days). For each `IVHistory` row (models.py:930-951) with no
`iv_daily` row for `(symbol, on)`: `IVDaily(symbol, on, kind='history',
source='iv_history', iv30=row.iv30, iv30_src=('cboe' if row.source=='cboe' else 'ibkr'),
spot=row.spot)`. Units match (both percent). `iv_history` keeps being written by
`spread_scan.record_iv` (spread_scan.py:203-219) — the Spread page is not touched, and the
copy picks up anything it files. `deploy/iv_seed_ibkr.py` stays valid as the bulk seeder.
Rank/percentile for the backfilled days are not computed retroactively (only today's row
carries them; a future backtest can recompute from the series).

#### A6.2 IV Rank list / `ivscan_universe` / My Watchlist → `option_basket`

Not automatic — the basket is a deliberate choice. `POST /options/basket/import` with body
`source=` (the contract's source names; `ivscan_list` and `ivscan_scan` are two different
things in the repo — the typed "My list" vs the TWS scanner's output — and are never merged
under one name):

| `source` | Reads | Writes `source=` |
|---|---|---|
| `paste` | the textarea, through `ivscan._clean_symbols` (ivscan.py:91-107) | `paste` |
| `watchlist` | `user_watchlist.symbol_set(db, user)` | `watchlist` |
| `ivscan_list` | `User.prefs["ivscan_universe"]` — the typed "My list" (ivscan.py:85-105) | `ivscan_list` |
| `ivscan_scan` | `IVScanItem` rows for the user (models.py:904-927), order `pos` — the TWS scanner's output | `ivscan_scan` |
| `scanner` | the live bridge `/scan` reply posted by the browser (symbols only) | `scanner` |
| `screener` | tonight's `spread_candidates` symbols (the nightly Cboe screener) | `screener` |
| `positions` | symbols with an open `option_trades` row (and, until removal, open `option_spreads`) | `positions` |

The empty-basket state of the page offers these as buttons with counts. Cap **`MAX_BASKET =
60`** symbols per member (D's budget argument: 60 tickers × ~2.5 s inside the 30-minute task
limit, A4.1 — the IV Rank page's own `MAX_SYMBOLS` of ivscan.py:87 is a different list with a
different cost and does not apply here). Existing rows are kept; duplicates skipped; an
import that would cross 60 adds up to the cap and reports the rest; returns `{added,
skipped, over_cap, total}`. A `sector` row comes from the "study options on this" button on
the Sector page (§2.1); `system` rows are the SYSTEM basket (§3) and have `owner_key =
"system"`.

#### A6.3 `option_spreads` → read-only legacy; `option_trades` is THE positions store

From step 1, every tracked trade of EVERY strategy — bull put included — lives in
`option_trades` + `option_trade_checks` (Part B7.1's shapes, created by `f4a5b6c7d8e9`,
A2.2) and is graded by `services/option_exits.py` (`mark / grade / sweep`, Part B7.3):
generic legs cover the bear call now (a bear call tracked into `option_spreads` would be
priced with PUTS by `spread_monitor.snapshot`, spread_monitor.py:109-110 — critic 2 blocker)
and every later family. The migration's data step copies the OPEN `option_spreads` rows
into `option_trades` with `strategy='bull_put'` (A2.2), so the day the module lands the
Positions tab shows what the old page showed. The Positions tab renders `option_trades`
rows with an `OptionTradeCheck` drawer and `POST /options/positions/{id}/close`;
`/options/badge` counts `urgent` / `watch` from `option_trade_checks` (A4.3).

`option_spreads` **stays read-only** for the legacy `/portfolio` page until that page is
removed; `spread_monitor` and `bull_put.monitor` are **not changed** (contract, POSITIONS
STORE) and keep writing `spread_checks` for it. Nothing in this part alters `option_spreads`;
no later revision adds columns to it.

### A7. Failure modes

| Failure | Detected by | Stored | Page shows (Part C/D render; this part supplies the flag) |
|---|---|---|---|
| Cboe down / 5xx / timeout for all tickers | every `fetch_chain` raises `ChainError`; `option_jobs.errors == symbols` | job row with `ok=0`; yesterday's snapshot untouched | rose pill "tonight's data could not be read (Cboe); showing Oct 1 16:00 ET"; cards render from the previous snapshot with the stale badge; Refresh button offered; if `TST_OPTIONS_FALLBACK=alpaca` the run already retried there and the pill says "via Alpaca" |
| Cboe 403 for one ticker (delisted / typo) | `ChainError("... no Cboe option chain — check the ticker")` (option_quotes.py:144-146) | `detail[sym].status='error'`, signal `status='no_chain'` | the basket row greyed: "no option chain for XYZ — remove it?" |
| Partial chain (few expiries / no deltas) | `Chain.partial` (A1.3) | stored with `iv_daily.partial=True`; engines run only on what is there | amber "chain looks incomplete tonight (2 expiries)"; LEAPS/calendar chips disabled with the reason; the idea is **not pushed** to Telegram (A4.4) |
| Stale snapshot (job missed, Hermes down) | `/options/badge.stale` / `job_missed`, or `card_for`'s `stale` (snap_on older than the previous ET trading day, A5.3) | — | amber strip "data as of Oct 1 — the nightly run has not happened" + Refresh; the card still renders; the order ticket shows "Refresh first" while the US session is open (A4.7) |
| No IV history (new ticker) | `iv.state in ('none', 'forming', 'pct_only')`, `iv.basis in ('provisional', 'unknown')` | rank/pct None, `iv_n`, `iv_state` | "IV rank: not enough history yet (12 of 60 days). It fills in by itself; if you run TWS on this PC, Live loads a year at once." — gauge = unknown with the day count, basket cell `–` / `~62` dotted (A3.3); sell-premium strategies shown as "cannot judge IV yet", never as rejected; nothing pushed to Telegram |
| IV30 source mismatch after a switch | calibration median > 1.5 pts (A3.2) | logged nightly | footnote on the IV line |
| Yahoo bars missing (HV / ATR / trend) | `fetch_daily_ohlc` returns `[]` | chain + iv row stored; `hv* = None`; signal `status='no_setup'`, error "no price history" | "chart could not be read tonight"; IV figures still shown; no strategy recommended |
| Earnings unknown | `fetch_next_earnings` None | `earnings_date=None` | "earnings date unknown — check before trading"; the earnings gate is a warning on the card, a veto for the push ("skipped: earnings date unknown" in the job log, A4.4) |
| Engine exception | caught at A4.2 f | signal `status='error'` + message for that hash; data rows kept; the other hashes still run | "the rules could not run for LRCX tonight (error id …)"; Refresh recomputes |
| Hermes job never started (task disabled, box off) | no `option_jobs` row for today (`job_runs.missed`) | — | same as stale; the admin Data Ingest row reads "never ran today" |
| Job started, crashed mid-run | row with `finished_at IS NULL` older than 40 min | partial `detail` | pill "run did not finish (23 of 30 tickers)"; the finished tickers are fresh, the rest stale per card |
| Alpaca credentials missing (source=alpaca) | `ChainError("alpaca: no credentials")` at the first symbol | job `errors=symbols`, note | rose pill "Alpaca source selected but no credentials on this box" |
| Alpaca 429 | status code; `Retry-After` honoured | retries then error per symbol | as Cboe-down for the affected tickers |
| Bridge older than 1.6 on Live | no `series` in the `/iv` reply (`payload["iv"]["series"]` absent) | nothing written; the chain part still grades live | "Your bridge is older than 1.6 — restart `bridge\start_ibkr_bridge.bat`" on the IV line only |
| Live pressed on a phone / touch viewport | viewport test in the template | — | the Live button is hidden; hint "Live quotes need TWS on your PC" (A1.5) |
| Member's rules saved after the nightly run | no row for the member's hash on the latest `snap_on` | — | basket cell `not_checked` (grey dot, "open the card to check your rules"); the card open computes the row from the stored chain (A5.3) |
| Account value not set (sizing) | `trade_prefs.nlv` None and no Live figure | — | `sizing.contracts = None` with the note to set the account value; the chance / collect / risk figures still show |
| SQLite busy (page read during a commit) | `OperationalError: database is locked` after the driver's 5 s wait | — | commits are per symbol (< 1 s), so this should not occur; if it does the request retries once and then renders the stale badge |

### A8. Test plan (README "Tested:" style)

**Synthetic (pure, no network; `dashboard_tst/tests/test_option_data.py`, run with the
venv's python — pytest is added to a dev-only `dashboard_tst/requirements-dev.txt`; the
`dashboard_tst/tests/` tree and `tests/fixtures/options/` are created in step 1 and shared
by Parts B/C/D's suites — neither exists yet):**

- `option_quotes` parser on a saved MSFT fixture (the 2026-10-02 probe, trimmed to 60
  contracts in `tests/fixtures/options/cboe_MSFT_small.json`): the five new fields present;
  a `0.0/0.0` quote → `bid=ask=mid=None`; `iv=8.3` → `iv=None` after the adapter; the
  deep-ITM `iv=3.1099` row **stays 3.1099** (a fraction — the unit comes from the source, no
  magnitude heuristic, A1.1); `as_of` `2026-10-02T15:59:59` → `2026-10-02 19:59:59` UTC;
  `snap_on='2026-10-02'`; `legs()` equals the pre-change dict key for key (so
  `spread_scan.build_candidates` yields identical rows on the fixture before and after).
- `AlpacaSource` on recorded responses: 2 snapshot pages + 1 contracts page → rows joined by
  OCC with `oi` filled; a contract missing from the contracts page → `oi=None`; `iv30=None`,
  `atm_iv30` computed; `expiration_date_lte` present in the request params (the next-weekend
  default trap).
- `BridgePayloadSource`: a `_row`-shaped put list with `iv=43.1` → `0.431`; `oi_ok=False`
  → every `oi=None`; 401 rows → capped at 400; a strike 60 % from spot dropped;
  `opt_legs.norm_leg(row, unit="fraction")` on the resulting `ContractRow` leaves `iv`
  untouched.
- `option_metrics.hv`: 21 closes of a constant → `0.0`; a known series against numpy's
  `std(ddof=1)·√252·100` to 1e-9; 20 closes → None; last bar with `session_frac=0.4` excluded.
- `atm_iv_by_expiry`: spot exactly on a strike; spot between strikes (interpolation
  weight checked); one leg missing → still computed; one leg only → None.
- `iv30_constant_maturity`: expiries at 20/41 DTE with σ 0.40/0.30 → the CM formula's
  value by hand; only a 45-DTE expiry → its ATM IV; no expiry ≥ 5 DTE → None.
- `iv_rank_pct`: 252 values + today = max → rank 100, pct 99.6; today = min → 0 / 0; flat
  series → rank None, pct 0; n = 0/19/20/59/60/251/252 → states
  `none/forming/pct_only/pct_only/rank_ok/rank_ok/ok`; `basis` `unknown` for n = 0, `provisional`
  for n = 19 with HV present, `percentile` for 20..251, `rank` at 252; `provisional` True
  only for the first two.
- `term_structure`, `skew25`: front/back selection on a 7-expiry chain; `term_ratio` 42.0 /
  40.4 = 1.0396 (below `TERM_EVENT`); no call within 0.07 of delta 0.25 → `skew25=None`.
- `replace_snapshot` + `upsert_iv_daily` + `upsert_signal` on an in-memory SQLite: run twice
  with the same chain → identical counts; EOD after intraday → `kind='eod'` on the day row;
  `bootstrap_iv` never overwrites a `cboe` day, inserts the missing ones, stores the percent
  series **as-is** (a point `31.2` is stored as `iv30=31.2`), bounded input rejected (401
  points, a future date, `iv=1200`, `iv=0.05`).
- `prune`: 100 synthetic days → 90 kept; day-10 rows with |delta| < 0.03 gone, day-3 rows
  intact; expired contracts gone after 7 days; intraday rows from yesterday gone.
- `prefs_hash`: `{}` and the house defaults give the same hash;
  `{"credit_vertical": {"delta_lo": 0.20}}` (equal to the default) also the same; `0.25` →
  different; `{"shared": {"nlv": 250000, "risk_pct": 2.0}}` and an exit-line or Telegram edit
  → the SAME hash (not pick-relevant, A5.1); key order irrelevant;
  `distinct_hashes(db)` with three members (two on house defaults, one with an override)
  → `[HOUSE_HASH, <one more>]`.
- `card_for` / `basket_rows_for`: the same stored pick read with `nlv` 100,000 vs 50,000 →
  different `sizing.contracts`, same row, no write; a 325/315 fixture whose max loss exceeds
  `risk_budget × GAP_MULT` → `contracts=0` and the note, never 1; a member hash with no row
  on the latest day → `pick_state='not_checked'` while `trend`/`iv` come from the house row;
  an empty `picks[recommended]` → `no_strike_passes`; `stale` False for a snapshot of the
  previous ET trading day, True one session older.
- `backfill_from_iv_history`: 300 `iv_history` rows, 50 already in `iv_daily` → 250 inserted,
  second run 0.
- Migration: `alembic upgrade head` on an empty DB creates the **nine** tables; on a DB
  stamped at `e2f3a4b5c6d7` with `option_basket` pre-created by `create_all` → no error
  (guard); a DB with 3 open + 2 closed `option_spreads` rows → 3 `option_trades` rows with
  `strategy='bull_put'` and two legs each, `option_spreads` byte-identical; `upgrade` run
  again → still 3; `downgrade -1` drops all nine and leaves `option_spreads` alone.
- Basket import: 70 pasted symbols into an empty basket → `added=60, over_cap=10`;
  `source='ivscan_list'` reads `prefs.ivscan_universe`, `source='ivscan_scan'` reads
  `IVScanItem` rows — different inputs, different `source` values on the rows.

**Live (dev DB on the laptop, then Hermes):**

- `deploy/options_nightly.py NVDA LRCX MSFT KO -v`: four chains, counts logged (MSFT
  ≈ 3,700 rows, 23 expiries), `iv_daily` rows for today with `iv30_src='cboe'` and
  `atm_iv30` within ~2 vol points of Cboe's figure, HV20/HV60 plausible (KO < NVDA),
  `earnings_days` matches the calendar page, one signal row per distinct hash per symbol
  (house + each saved override set), one `option_jobs` row with `pushed`,
  `/options/badge` returns `stale=False`. Re-run: row counts unchanged, `pushed=0` the
  second time (dedupe).
- `--source alpaca` on the same four with the paper keys: same expiries ± the weeklies Cboe
  lists first, `oi` populated from the contracts endpoint, `iv30_src='atm'`, 6 calls per
  ticker in the log, no 429 at 0.4 s pacing.
- Pacing: 30 tickers sequential at 1.5 s → no 429 (the spread scan's measured shape); the
  same 30 with `--pause 0.2` → 429s appear and the backoff recovers them (expected).
- Live bootstrap: on a PC with TWS + bridge 1.6, press Live on a ticker with 15 days of
  history → `inserted ≈ 237`, the rank appears, the ranked figure agrees with the IV Rank
  page's TWS number within 1 point (both percent, no unit drift); the card shows the
  `live · TWS 21:42 ET` badge with only the live expiry in the picks table and "show delayed
  expiries" below it; `option_chain_snapshot` row count unchanged; a pre-1.6 bridge → the
  "older than 1.6" message on the IV line, the chain still graded live; press again →
  `inserted=0`; the NLV from `/account` sizes the request and is NOT in `trade_prefs` until
  "remember this" is clicked.
- Refresh during the US session: `kind='intraday'` rows replace the previous ones, the day's
  `iv_daily` row says `kind='intraday'`, the 07:15-MYT run next morning turns it `eod`; a
  second Refresh within 60 s is refused, except the one the ticket's "Refresh first" banner
  triggers.
- Hermes: `setup_options_nightly_task.ps1` registers `TST-Options-Nightly` 07:15; after the
  first scheduled run `logs\options_nightly.log` has the summary line and the Options page
  strip is green ("job 07:17 MYT ✓") before 07:30 MYT; stop the task for a day → the strip
  turns amber with "the nightly run has not happened" (`job_missed` by 08:00 MYT) and the
  cards still render.
- Failure drills: point `TST_OPTIONS_SOURCE` at a typo → the job exits 1 with one clear line;
  add ticker `ZZZZ` to a basket → the row greys with the "no option chain" reason, the other
  tickers are unaffected; kill the job after 2 tickers → the pill reads "did not finish (2 of
  4)"; `--telegram-dry-run` → the composed messages in the log, `option_idea_push` unchanged.
