## A. Data layer and nightly pipeline

Scope of this part: everything between the option feeds and the engines. The engines
(Part B), the page (Part C/D) and the Positions monitor read **only** what this part
stores; nothing on the page waits on a market call (OPTIONS_MODULE_DESIGN.md §2.5, §4.4).

All paths below are relative to `dashboard_tst/`. Line numbers were read on 2026-10-03.

### A0. Files, units and time conventions

| File | New / modified | Role |
|---|---|---|
| `app/services/option_data.py` | **new** | `ChainSource` interface, `Chain`/`ContractRow` dataclasses, `CboeSource`, `AlpacaSource`, `BridgePayloadSource`, `source()` factory, legacy `to_legs()` adapter |
| `app/services/option_quotes.py` | modified (additive, lines 158-166) | the Cboe parser keeps five fields it currently drops (`rho`, `last_trade_price`, `bid_size`, `ask_size`, `prev_day_close`); nothing else changes, every existing caller keeps working |
| `app/services/option_metrics.py` | **new**, pure | HV20/HV60, ATM IV per expiry, IV30 constant-maturity, IV rank / percentile, term slope, 25-delta skew, expected move, days-to-earnings |
| `app/services/option_store.py` | **new** | ORM reads/writes: `replace_snapshot`, `upsert_iv_daily`, `bootstrap_iv`, `backfill_from_iv_history`, `prune`, `latest_chain`, `basket_universe` |
| `app/services/option_prefs.py` | **new** | `HOUSE` defaults, `clean(raw)` merge, `prefs_hash(merged)` |
| `app/services/option_nightly.py` | **new** | orchestration: `run_nightly(db, ...)`, `refresh_symbol(db, sym)`, the per-symbol pipeline, the `option_jobs` record |
| `deploy/options_nightly.py` | **new** | the Hermes script (mirrors `deploy/spread_scan.py`) |
| `deploy/setup_options_nightly_task.ps1` | **new** | registers `TST-Options-Nightly` (mirrors `deploy/setup_spread_scan_task.ps1`) |
| `alembic/versions/f4a5b6c7d8e9_options_module.py` | **new** | six tables, chained off `e2f3a4b5c6d7` |
| `app/models.py` | modified (append) | `OptionBasket`, `OptionChainSnapshot`, `IVDaily`, `OptionSignal`, `UserOptionPrefs`, `OptionJob` |
| `app/config.py` | modified (append to `Settings`) | `options_source`, `options_fallback`, `options_snapshot_days`, `options_full_days`, `alpaca_feed` |
| `app/.env.example` | modified | the five `TST_OPTIONS_*` keys documented |
| `bridge/ibkr_bridge.py` | modified (1.5 → 1.6) | `/iv?symbol=X&series=1` also returns the daily series (the "Live" bootstrap, A4.6) |
| `app/routes/options_page.py` endpoints `/options/status`, `/options/{sym}/refresh`, `/options/{sym}/iv/bootstrap`, `/options/basket/import` | **new** (the page router is Part C's; these four are the data endpoints this part owns) | |

**Units — one convention, stated once, enforced in `option_data.py`:**

| Quantity | Unit | Why |
|---|---|---|
| per-contract `iv` (snapshot row) | **fraction**, 0.3585 | what Cboe (`options[].iv`, probe 2026-10-03) and Alpaca (`impliedVolatility`) send, and what `black_scholes.black_scholes(sigma=)` (black_scholes.py:43) takes |
| per-day vol statistics (`iv_daily.iv30`, `atm_iv30`, `hv20`, `hv60`, `iv_lo`, `iv_hi`) | **percent**, 32.03 | `IVHistory.iv30` is already percent (models.py:948, "percent, e.g. 33.7"); `deploy/iv_seed_ibkr.py:104` multiplies IB's fraction by 100 to match; A6 copies `iv_history` into `iv_daily` 1:1 |
| the bridge's per-row `iv` (`_row`, ibkr_bridge.py:332) | percent | **divided by 100** by `BridgePayloadSource` (A1.5) so engines see one unit. `bull_put.pl_profile` (bull_put.py:165-193, `short_iv / 100.0`) is the one existing reader that assumes percent — cross-part note in A1.7 |
| greeks | per share, signed as the feed gives them (put delta negative) | `spread_monitor.snapshot` relies on the sign (spread_monitor.py:140-143) |
| `oi`, `volume`, `bid_size`, `ask_size` | integer contracts; **None = the feed did not say** (never 0) | `bull_put._count` (bull_put.py:148-156) and the bridge's `_count` (ibkr_bridge.py:304-310) both make that distinction |
| prices | per share | as everywhere else in `option_spreads` |

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
| `spread_scan.build_candidates` | `chain["spot"]`, `chain["iv30"]`, `legs[(expiry,right,strike)]` → `bid ask delta iv volume open_interest` | spread_scan.py:110-181 |
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
    iv: float | None     # FRACTION
    delta: float | None  # signed
    gamma: float | None
    theta: float | None  # per day, per share, signed (negative for a long option)
    vega: float | None
    rho: float | None
    theo: float | None   # model value when the feed gives one (Cboe); None otherwise
    oi: int | None       # open interest; None = unknown
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
        today (option_quotes.py:151-166), key for key, so spread_scan / spread_monitor /
        portfolio run unchanged on a Chain (A1.7)."""
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
flag to show.

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

#### A1.5 `BridgePayloadSource` — the member's "Live" read

The server never reaches the bridge (options.py:1-18). The browser fetches
`127.0.0.1:9224/chain?symbol=X&exp=YYYYMMDD` (one expiry, ±10 strikes, or the put-side
window; ibkr_bridge.py:419-538) and POSTs the JSON exactly as `_options_tab.html:98-116`
does today. `BridgePayloadSource(payload)` is constructed **from that posted dict** — no
network — and yields a `Chain(kind="live", source="bridge", all rows of that one expiry)`:
`iv/100`, `oi`/`volume` via the bridge's own None convention, `delayed_minutes = 0 if
payload["data_mode"] == "live" else 15`, `header = {"greeks_from_delayed", "oi_ok",
"bridge": payload["bridge"]}`. Validation: every number re-checked (the body is untrusted,
options.py:110-112), rows capped at 400, strikes within 50 % of spot.

Live chains are **not written to `option_chain_snapshot`** (decision 1: the bridge is a
read only). They are graded in-request against the member's prefs and shown with a "live ·
TWS" badge; the only thing a Live press persists is the IV series (A4.6).

#### A1.6 Which source when

| Path | Source | Writes |
|---|---|---|
| nightly job (Hermes, 07:15 MYT) | `settings.options_source` (+ fallback) | snapshot `kind=eod`, `iv_daily`, `option_signal` (house hash) |
| page "Refresh" button | same | snapshot `kind=intraday` (replaces the symbol's previous intraday rows), `iv_daily` for today (`kind=intraday`), signals for the house hash and the caller's hash |
| page "Live (TWS)" button | bridge payload | nothing but `iv_daily` bootstrap rows (`source=ibkr`) |
| Positions sweep (`spread_monitor.sweep`) | unchanged: `option_quotes.fetch_chain` | unchanged (`spread_checks`) — migrates to `Chain.legs()` in A1.7 when convenient, not required for step 1 |

#### A1.7 Migrating the existing consumers onto the normalized row

| Consumer | Change | Risk |
|---|---|---|
| `spread_scan.build_candidates(symbol, chain)` | call with `chain.legs()` wrapped: `{"spot": c.spot, "iv30": c.iv30, "legs": c.legs()}` — the function body (spread_scan.py:110-181) reads exactly those keys | none; the nightly Spread scan keeps calling `option_quotes.fetch_chain` directly until step 2 |
| `spread_monitor.snapshot(chain=...)` | accepts either the legacy dict or a `Chain` (`if isinstance(chain, Chain): chain = chain.as_legacy()`), where `as_legacy()` = `{"symbol","spot","iv30","as_of","fetched_at","legs"}` | none |
| `option_quotes.leg/expiries/strikes` | unchanged — they take the legacy dict; `Chain.as_legacy()` feeds them | none |
| `bull_put.rank_pairs(puts=[...])` | Part B's generic picker takes `list[ContractRow]`; `bull_put` itself is untouched in step 1 and is fed `[asdict(r) | {"oi": r.oi}]` for the bull-put path | **`bull_put.pl_profile` divides `short_iv` by 100** (bull_put.py:184-185) because bridge rows carry percent. Part B/C: when the payoff component replaces `pl_profile`, pass fraction IV straight to `black_scholes`; until then the bull-put adapter multiplies by 100 on the way in. Flagged as a cross-part need |
| `routes/options.analyze` | unchanged for the legacy Watchlist tab; the new page's live grade goes through `BridgePayloadSource` | none |

### A2. Tables

#### A2.1 Models (`app/models.py`, appended; conventions from the file: `_utcnow` default (48-49), `ondelete="CASCADE"` FKs (727-728), ISO-date strings for day keys (947, 1125-1127), `JSON` for blobs (778), `UniqueConstraint` + `Index` in `__table_args__` (983-985))

```python
class OptionBasket(Base):
    """A ticker one member (or the system) studies on the Options page. Separate
    from user_watchlist (models.py:711-737): a different cadence (nightly chain
    snapshot, 1.5 s of Cboe pacing each) and a different cost, so adding a name
    here is a deliberate act. owner_key = f"u{user_id}" or "system" so the
    unique constraint works without a NULL user_id (NULLs are distinct in a
    UNIQUE on both SQLite and Postgres)."""
    __tablename__ = "option_basket"
    __table_args__ = (UniqueConstraint("owner_key", "symbol", name="uq_option_basket_owner_symbol"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_key = Column(String(16), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    source = Column(String(12), nullable=False, default="typed")   # typed | watchlist | ivscan | scanner | sector | system
    note = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, default=True)          # off = kept, not fetched
    added_on = Column(String(10), nullable=False)                   # ET date
    created_at = Column(DateTime, default=_utcnow)
    user = relationship("User")


class OptionChainSnapshot(Base):
    """One contract's quote + greeks as seen at one snapshot. The per-symbol-day
    header (spot, iv30, as_of, source, counts) lives in iv_daily, so this table is
    pure contract rows. Rows are replaced per (symbol, snap_on, kind)."""
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
    the server read itself. Per-day statistics are PERCENT (the iv_history unit)."""
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
    iv_hv = Column(Float, nullable=True)       # iv30 - hv20, vol points
    iv_rank = Column(Float, nullable=True);  iv_pct = Column(Float, nullable=True)  # 0..100
    iv_n = Column(Integer, nullable=True)      # observations behind rank/pct
    iv_lo = Column(Float, nullable=True);  iv_hi = Column(Float, nullable=True)      # window min/max (pct)
    iv_by_expiry = Column(JSON, nullable=True) # {expiry: {dte, atm_iv, n_legs, em_1sd}}
    iv_front = Column(Float, nullable=True);  iv_back = Column(Float, nullable=True)
    term_slope = Column(Float, nullable=True)  # (front - back) / back
    skew25 = Column(Float, nullable=True)      # put25 - call25, vol points
    skew_norm = Column(Float, nullable=True)   # skew25 / atm iv of that expiry
    em30 = Column(Float, nullable=True)        # 1-sigma expected move, $, 30 calendar days
    earnings_date = Column(String(10), nullable=True)
    earnings_days = Column(Integer, nullable=True)
    n_contracts = Column(Integer, nullable=True); n_expiries = Column(Integer, nullable=True)
    partial = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class OptionSignal(Base):
    """The cache the page reads: one ticker's full card for one snapshot under one
    set of rules (prefs_hash). user_id NULL = the house-default row the nightly job
    writes; a member's own row is written lazily the first time they open the
    card (A5). JSON shapes are the contract in A5.2."""
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
    status = Column(String(12), nullable=False, default="ok")   # ok | no_setup | no_chain | no_iv | error
    trend = Column(String(12), nullable=True)                   # up | down | sideways | unclear
    headline = Column(Text, nullable=True)
    setup = Column(JSON, nullable=True)
    iv = Column(JSON, nullable=True)
    strategies = Column(JSON, nullable=True)
    picks = Column(JSON, nullable=True)
    payoff = Column(JSON, nullable=True)
    ticket = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    pushed_at = Column(DateTime, nullable=True)                 # Telegram push, house row only
    computed_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class UserOptionPrefs(Base):
    """A member's rule overrides, SPARSE: only the fields they changed, merged over
    option_prefs.HOUSE on read (like ema_setup.clean_enabled over COND_DEFAULT,
    ema_setup.py:455-462). prefs_hash is the hash of the MERGED result, stored so
    the signal lookup is one indexed read."""
    __tablename__ = "user_option_prefs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    prefs = Column(JSON, nullable=False, default=dict)
    prefs_hash = Column(String(16), nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    user = relationship("User")


class OptionJob(Base):
    """One run of the nightly job / a refresh / a bootstrap: the freshness pill and
    the operator's answer to 'did it run?'. Same role as SpreadScan (models.py:954-968)."""
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
    detail = Column(JSON, nullable=True)      # {sym: {"status": "ok|error|partial", "ms": int, "n": int, "err": str|None}}
    note = Column(Text, nullable=True)
```

#### A2.2 The migration — `alembic/versions/f4a5b6c7d8e9_options_module.py`

```python
"""options module: option_basket, option_chain_snapshot, iv_daily, option_signal,
user_option_prefs, option_jobs

Revision ID: f4a5b6c7d8e9
Revises: e2f3a4b5c6d7
Create Date: 2026-10-xx
"""
revision = "f4a5b6c7d8e9"
down_revision = "e2f3a4b5c6d7"      # iv_scan_items, the current head (verified 2026-10-03)

def _tables():  return set(sa.inspect(op.get_bind()).get_table_names())   # d1e2f3a4b5c6:23-24

def upgrade():
    have = _tables()                 # guarded per table like e2f3a4b5c6d7:20-21 — the Hermes
    if "option_basket" not in have:  # DB predates Alembic and create_all may have run
        op.create_table("option_basket", ...)   # columns exactly as the models, server_default for
        op.create_index(...)                    # booleans/ints (sa.true(), "0") like d1e2f3a4b5c6:68
    ... six tables, indexes as declared in __table_args__ ...

def downgrade():
    for t, idx in reversed(ORDER):   # drop indexes then table, guarded by _tables()
```

No data moves in the migration (fast, reversible); the `iv_history` copy is a job step
(A6.1). `render_as_batch=True` is already set (alembic/env.py:41, 58) should a later revision
need an ALTER on SQLite. `init_db()` runs `upgrade head` at startup (db.py:93-94), so the
Hermes deploy needs no manual step (CLAUDE.md data-handling rule).

#### A2.3 Estimated row counts

| Table | Rows / day | Retention | Steady state (30-ticker basket) | Steady state (100 tickers, the IV Rank cap `MAX_SYMBOLS` ivscan.py:87) |
|---|---|---|---|---|
| `option_chain_snapshot` | ~3,000 per ticker (MSFT 3,710; SPY ~15k; a $30 mid-cap ~800) | 90 EOD days, thinned after 7 (A2.4); 1 intraday set per ticker | 7 × 90k + 83 × ~40k ≈ **4.0 M** | ≈ 13 M |
| `iv_daily` | 1 per ticker | 3 years (never pruned in v1) | 23k | 76k |
| `option_signal` | 1 house + 1 per member who opened the card, per ticker | 90 days | ~10k | ~35k |
| `option_jobs` | 1-5 | 180 rows | 180 | 180 |
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

**Switch trigger to write in the README:** basket > 40 tickers, or `tst.db` > 2 GB, or a
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

`hv20`, `hv60` from the same series; `iv_hv = iv30 − hv20` (vol points; positive = sellers
are paid above realised, §6c).

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

def iv_rank_pct(series: list[float], current: float) -> dict:
    """series = up to 252 past+today iv30 values (pct), current = today's.
    rank = (current - lo) / (hi - lo) * 100  (None if hi == lo)
    pct  = count(v < current) / N * 100     (strictly below, as spread_scan.percentile)
    -> {"iv_rank", "iv_pct", "n", "lo", "hi", "state"}
    state: "ok" (n >= 252), "partial" (60 <= n < 252: rank over n days),
           "forming" (20 <= n < 60: pct only), "none" (n < 20)."""
```

What the page shows before the minimum is met (Part C renders; this part supplies `state`
and `n`): `forming` → "IV rank: forming (34 of 60 days) — press **Live** to load a year
from TWS"; `partial` → the number with a footnote "over 118 days"; `none` → "no IV
history yet". The gauge (Part B) treats `None` rank as *unknown*, never as *low* — the
`ivscan._list_context` rule (ivscan.py:192-194).

#### A3.4 IV per expiry, term structure

`iv_by_expiry` (A3.2) is stored as JSON. Front/back:

```python
def term_structure(by_expiry) -> dict:
    """front = expiry nearest 30 DTE with dte >= 7; back = nearest 75 DTE with dte >= 45
    (both must exist, else None).
    term_slope = (iv_front - iv_back) / iv_back      # ratio, ticker-relative
    > +0.05 backwardation: an event is priced in the front (calendars: sell front)
    < -0.05 contango: normal upward curve
    -> {"iv_front", "iv_back", "term_slope", "front_expiry", "back_expiry"}"""
```

#### A3.5 25-delta put/call skew

At the front expiry: the put whose |delta| is nearest 0.25 and the call whose delta is
nearest 0.25, each within 0.07 of it and with a valid iv.
`skew25 = (iv_put25 − iv_call25) × 100` (vol points, positive = puts richer, the normal
equity shape); `skew_norm = skew25 / atm_iv_front` (unitless, so a 60-vol name and a 20-vol
name compare). Both None if either leg is missing.

#### A3.6 Expected move

Per expiry: `em_1sd = spot × (atm_iv/100) × sqrt(dte/365)` stored inside `iv_by_expiry`;
`iv_daily.em30 = spot × (iv30/100) × sqrt(30/365)`. The page shows "±$em (±pct)" and the
engines use the per-expiry value for "short strike outside the expected move".

#### A3.7 Days to earnings

`prices.fetch_next_earnings(symbol)` (prices.py:178-191, cached 6 h, soft-fail None) →
`earnings_date`, `earnings_days`. `earnings_inside(expiry)` = `today <= earnings_date <=
expiry`, the rule `spread_scan.build_candidates` already applies (spread_scan.py:140).
None = unknown, displayed as "earnings date unknown", never as "no earnings".

#### A3.8 ATR(14)

Not stored in `iv_daily` (it is a chart statistic), but the nightly job computes it once per
ticker from the same bars — `support_bounce.atr_series(highs, lows, closes, 14)[-1]`
(support_bounce.py:150-166, Wilder) — and passes it to the engines, so every chart-relative
threshold (CLAUDE.md "Normalized strategy parameters") uses the one ATR the support
detector uses. It is written into `option_signal.setup.atr` so the card and the payoff chart
read the same number.

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
.\.venv\Scripts\python.exe deploy\options_nightly.py --no-push    # skip Telegram
.\.venv\Scripts\python.exe deploy\options_nightly.py --on 2026-10-02 --source alpaca -v
```

`deploy/setup_options_nightly_task.ps1` registers **`TST-Options-Nightly`**, daily at
**07:15 local (MYT)**: after `TST-Portfolio-Check` (06:00) and after `TST-Spread-Scan`
(06:30, ~30 min for ~550 symbols, setup_spread_scan_task.ps1:42-43) so two jobs never hit
Cboe's CDN at once (the 429 was measured at ~24 requests in 10 s, option_quotes.py:127-129).
07:15 MYT = 19:15 ET (EDT) / 18:15 ET (EST): same ET day, after the close. Execution limit
30 min; log `logs\options_nightly.log` appended through the same `cmd /c ... >> log 2>&1`
redirect (setup_spread_scan_task.ps1:38-39). A 100-ticker basket at 1.5 s + ~1 s of work
each is ~4-5 min.

#### A4.2 Ordering, per run

```
run_nightly(db, symbols=None, on=None, source=None, push=True, backfill=False, progress=None):
  0. job = OptionJob(job="nightly", run_on=on or et_today(), source=src.name); commit
  1. if backfill: option_store.backfill_from_iv_history(db)                      # A6.1, idempotent
  2. universe = option_store.basket_universe(db)   # distinct ACTIVE symbols over all owners, sorted
     (+ every symbol with an OPEN option_spreads row, so a tracked position's chain is always fresh)
  3. for sym in universe (sequential, src.capabilities.pacing_seconds between fetches):
        t0 = now
        a. chain = option_data.fetch_chain(sym, fresh=True, retries=3)      # ChainError -> record, continue
        b. bars = prices.fetch_daily_ohlc(sym, "2y"); earnings = prices.fetch_next_earnings(sym)
        c. metrics = option_metrics.all_for(chain, bars, earnings, iv_series=option_store.iv_series(db, sym, 252))
        d. option_store.replace_snapshot(db, chain)                           # delete (sym, snap_on, 'eod') + add_all
        e. option_store.upsert_iv_daily(db, chain, metrics)                   # query-then-write
        f. sig = option_engine.compute(chain, metrics, bars, prefs=option_prefs.HOUSE_MERGED)   # Part B
           option_store.upsert_signal(db, sig, prefs_hash=HOUSE_HASH, user_id=None)
        g. db.commit()                                                        # per symbol: SQLite readers wait < 1 s
        h. job.detail[sym] = {"status", "ms": now - t0, "n": len(chain.rows), "err"}; progress(sym, err)
  4. option_store.prune(db, today)
  5. if push: option_push.push_new_ideas(db, run_on)        # Telegram, A4.4; soft-fail
  6. job.finished_at, ok/errors/rows; commit; return summary dict (the spread_scan.run_scan shape, 396-397)
```

Soft-fail per ticker at every letter: a `ChainError` (a) records `status=error` and moves
on; a Yahoo failure (b) leaves `hv*`/`earnings_*` None and the chain is still stored; an
engine exception (f) is caught, logged with traceback, written as
`option_signal.status='error'` with the message, and the data rows (d, e) are **kept** — a
card that says "the rules could not run tonight" over fresh data beats a blank. The job
exits 0 whenever step 0 succeeded (a missing chain is "a normal Tuesday",
portfolio_daily_check.py:18-19).

#### A4.3 Logging and the health record (dashboard-visibility rule)

| Surface | What |
|---|---|
| `logs\options_nightly.log` | one line per symbol (`%-6s ok 3,710 rows 1.9s` / `skipped: ...`), the summary line, Telegram result |
| `option_jobs` row | counts + `detail` JSON per symbol; written at start (so a crash mid-run leaves a row with no `finished_at` = "started, never finished") |
| `GET /options/status` (require_user) | `{"run_on", "finished_at", "symbols", "ok", "errors", "rows", "source", "stale": run_on < et_today(), "running": finished_at is None and started < 40 min ago, "next_due": "07:15 MYT"}` — read from the latest `option_jobs` row, never from a market call (the `/portfolio/badge` principle, portfolio.py:430-434) |
| Options page status strip | "Data as of Oct 2, 16:00 ET · delayed · job 07:17 MYT ✓" / amber "last run Oct 1 — tonight's run missed" / rose "run failed: N of M tickers" (Part C renders from `/options/status`) |
| Admin Data Ingest page (`/finviz`) | one extra row "Options nightly" from the same endpoint (optional; same turn as the pill if cheap) |

#### A4.4 Telegram push (decision 10)

`app/services/option_push.py::push_new_ideas(db, run_on)`: for every house-hash
`option_signal` with `status='ok'`, a recommended strategy and a top pick, not already
pushed (`pushed_at IS NULL`) and not pushed in the last 5 days for the same
`(symbol, strategy, expiry, short_strike, long_strike)` — send one message per idea in the
§3 wording ("LRCX — uptrend, bounced off 340 on volume, IV rank 62 → sell the Nov 330/320
put spread for ~2.10, 74% chance of keeping it"), with the `/options?sym=LRCX` link
(`settings.public_url`). Transport: `scripts._common.send_telegram(cfg, html)` and
`telegram_env` (_common.py:562-618; creds `telegram.env`, then `matp.env`, from the
vault / in-folder `.env`), imported through the `resources_bridge` sys.path. Not configured
→ logged once, skipped. The text itself is Part B/D's `headline` + `ticket` fields (A5.2).

#### A4.5 Idempotency (re-run the same day)

| Table | Re-run behaviour |
|---|---|
| `option_chain_snapshot` | `replace_snapshot` deletes `(symbol, snap_on, kind)` then inserts — same rows, same count |
| `iv_daily` | upsert on `(symbol, on)`; an EOD row overwrites an intraday one for the same day (`kind` → `eod`); the IBKR bootstrap **never** overwrites a `cboe`/`alpaca` row (A4.6) |
| `option_signal` | upsert on `(symbol, snap_on, kind, prefs_hash)`; `pushed_at` is preserved on overwrite so a re-run does not re-send Telegram |
| `option_jobs` | a new row per run (history); the pill reads the latest **finished** one |
| `--on` | files under that date, like `spread_scan.py --on` (deploy/spread_scan.py:36) — for refiling after a holiday |

#### A4.6 The IBKR "Live" bootstrap into `iv_daily` (decision 4)

Bridge 1.6: `/iv?symbol=X&series=1` returns, in addition to today's summary
(`_iv`, ibkr_bridge.py:541-563), `"series": [{"on": "YYYY-MM-DD", "iv": 0.3123}, ...]`
(fraction, the `b.close` values it already has, oldest first; ≤ 400 points). Without
`series=1` the response is unchanged, so the IV Rank page and the Watchlist tab keep working
on either bridge version. `server_version` → `TradeHunterIBKRBridge/1.6`.

Browser flow on the Options card's **Live** button (Part C's JS, same shape as
`ivscan.html:187-205`):

```
GET 127.0.0.1:9224/iv?symbol=LRCX&series=1          -> r
if (!r.series) -> "Your bridge is older than 1.6 — restart bridge\start_ibkr_bridge.bat" (the ivscan.html:276-278 message pattern)
POST /options/LRCX/iv/bootstrap  {series: r.series, iv_current: r.iv_current, iv_rank: r.iv_rank, iv_percentile: r.iv_percentile}
   -> {ok, inserted, skipped, n_total, iv_rank, iv_pct, state}
```

Server (`option_store.bootstrap_iv(db, symbol, series, *, source="ibkr")`): bounded input
(≤ 400 points, dates `<= today` and `>= today − 400d`, `0.01 <= iv <= 5.0`, the `_b` bounding
style of `ivscan_iv`, ivscan.py:351-360); for each point `iv30 = iv × 100`, insert an
`iv_daily(kind='history', source='ibkr', iv30_src='ibkr')` row **only if no row exists for
that day** — a day the server read itself is never replaced by a broker series; then
recompute today's `iv_rank/iv_pct/iv_n/state` from the now-full window and update today's
row; write an `option_jobs(job='bootstrap', symbols=1, rows=inserted)` row; mark the house
`option_signal` for the symbol `status='stale_iv'` so the next card read recomputes the
gauge (A5.3). Returns in < 200 ms.

The Hermes-side bulk alternative stays `deploy/iv_seed_ibkr.py` unchanged (writes
`iv_history`); the nightly `--backfill` / step 1 copies it over (A6.1).

#### A4.7 On-demand Refresh

`POST /options/{sym}/refresh` (require_user; member must have the symbol in a basket or an
open position) → `option_nightly.refresh_symbol(db, sym, user)`: the per-symbol pipeline of
A4.2 with `kind='intraday'`, `fresh=True`, `retries=0` (a page request never sits through
a backoff, option_quotes.py:127-130), computing the house signal **and** the caller's
`prefs_hash` signal; writes an `option_jobs(job='refresh')` row; returns the re-rendered
card. Measured budget: Cboe fetch 1-2 s + metrics/engines < 0.5 s. Rate-limited per member
to one refresh per symbol per 60 s (the quote cache would answer the same chain anyway).

### A5. The `option_signal` cache contract

#### A5.1 Keys

`(symbol, snap_on, kind, prefs_hash)` is unique. `prefs_hash` = first 12 hex of
`sha1(canonical_json(merged_prefs))` where `canonical_json` sorts keys, rounds floats to 4
dp, drops keys equal to the house default **only if** `schema_version` matches (so the house
row and a member who changed nothing share one hash and one computation).
`HOUSE_HASH = prefs_hash(option_prefs.clean({}))`; `engine_version` is a constant in
`option_engine` (Part B), bumped on any rule change — a row with an older `engine_version`
is treated as missing.

#### A5.2 What the engines write (one row; JSON column shapes)

```json
{
  "status": "ok",
  "trend": "up",
  "headline": "Uptrend: EMA 20 above 50 above 200 for 34 days ... paid to sell a put spread below that support.",
  "setup": {
    "kind": "support_bounce", "level": 340.0, "touches": 3, "level_kind": "support",
    "trend_line": {"value_today": 338.4, "value_at": {"2026-11-20": 341.9}, "touches": 3, "slope_per_day": 0.12},
    "resistance": null, "range": null,
    "stop": 338.0, "target": 372.0, "atr": 11.5,
    "ema": {"ema20": 346.1, "ema50": 335.8, "ema200": 301.2, "stack_days": 34},
    "evidence": ["EMA20 > EMA50 > EMA200 for 34 sessions", "bounce candle 2026-10-01 on 1.7x volume"]
  },
  "iv": {
    "iv30": 41.2, "iv30_src": "cboe", "atm_iv30": 40.6, "hv20": 28.9, "hv60": 31.4, "iv_hv": 12.3,
    "iv_rank": 62.0, "iv_pct": 71.0, "iv_n": 252, "state": "ok", "lo": 24.1, "hi": 51.8,
    "term_slope": 0.04, "iv_front": 42.0, "iv_back": 40.4, "skew25": 4.1, "skew_norm": 0.10,
    "em30": 21.6, "earnings_date": "2026-10-22", "earnings_days": 19,
    "verdict": "SELL", "verdict_why": "IV rank 62 (>= 50) and IV 12 pts above realised"
  },
  "strategies": [
    {"key": "bull_put", "label": "Bull put spread", "fit": "recommended", "rank": 1,
     "why": "uptrend + support holding + IV rank 62", "needs": "LRCX stays above 340 into Nov 20"},
    {"key": "bull_call", "label": "Bull call spread", "fit": "also_fits", "rank": 2, "why": "...", "needs": "..."},
    {"key": "long_call", "label": "Buy call", "fit": "rejected", "reason": "options too expensive (IV rank 62)"},
    {"key": "iron_condor", "fit": "rejected", "reason": "trending, not sideways"}
  ],
  "picks": {
    "bull_put": [
      {"legs": [{"expiry": "2026-11-20", "right": "P", "strike": 330, "side": "sell", "qty": 1, "price": 3.10, "delta": -0.26, "iv": 0.43, "oi": 2140, "bid_ask": 0.15},
                {"expiry": "2026-11-20", "right": "P", "strike": 320, "side": "buy",  "qty": 1, "price": 1.00, "delta": -0.15, "iv": 0.45, "oi": 1630, "bid_ask": 0.10}],
       "credit": 2.10, "max_profit": 210, "max_loss": 790, "breakeven": 327.90, "pop": 0.74,
       "pop_wording": "chance of keeping it", "return_on_risk": 0.266, "score": 0.197,
       "contracts": 2, "risk_budget": 1000, "tag": "best return",
       "chart_stop": 338.0, "chart_stop_pl": -220, "rule_stop_pl": -158,
       "checks": [{"name": "open interest >= 500", "ok": true}, {"name": "earnings inside expiry", "ok": false, "blocking": false, "detail": "Oct 22 is inside Nov 20"}]}
    ]
  },
  "payoff": {"strategy": "bull_put", "pick": 0, "prices": [...41...], "at_expiry": [...], "today": [...],
             "markers": {"spot": 349.2, "breakeven": [327.9], "stop_chart": 338.0, "stop_rule": 331.2, "support": 340.0, "trend_line": 341.9}},
  "ticket": {"strategy": "bull_put", "text": "SELL 2 LRCX 20NOV26 330P / BUY 2 LRCX 20NOV26 320P @ 2.10 CR LMT; condition: LRCX last <= 338 -> close", "stop_level": 338.0}
}
```

Rules this part enforces on the writer: every number the page shows comes from this row
(no recomputation on read); `picks` carry the **leg prices as quoted at `as_of`** so a
member sees what was true when the idea was made; `strategies` is ordered recommended →
also_fits → rejected (decision 9: the chip row shows the recommended first, up to two rejected
with reasons, the rest behind "other strategies" — the writer stores *all ten* with a `fit`;
the template decides how many to show).

#### A5.3 Read path and invalidation

```python
def card_for(db, symbol, user) -> dict:
    prefs = option_prefs.for_user(db, user)             # merged; .hash
    day = option_store.latest_snap_on(db, symbol)       # newest eod/intraday on file
    row = option_store.signal(db, symbol, day, prefs.hash, engine_version=CURRENT)
    if row is None or row.status == "stale_iv":
        chain = option_store.latest_chain(db, symbol)   # the stored rows, ~3k, < 50 ms
        row = option_engine.compute(chain, iv_daily_row, bars_cached, prefs) ; upsert     # ~100-300 ms
    return row + freshness(day, kind, as_of)
```

| Event | Effect |
|---|---|
| nightly run / Refresh writes a new `(symbol, snap_on, kind)` | older-day rows are simply not the latest; no delete needed |
| member saves rules | `user_option_prefs.prefs_hash` changes → next read misses → lazy compute from the stored chain (no market call) |
| house defaults change (constant edit, later admin edit) | `HOUSE_HASH` changes → nightly writes new house rows; members on house defaults follow automatically |
| engine rule change | `engine_version` bump → every row misses → lazy compute on open; the nightly job writes fresh house rows |
| Live bootstrap | house row `status='stale_iv'` → recompute on next read |
| chain older than 2 sessions | row served with `stale=True`; the page shows the amber badge and the Refresh button, never a blank (§2.5) |

Members never see each other's rows: `user_id` is on the row and the read filters
`user_id IN (NULL, me)` via the hash (a hash collision between two members' identical rule
sets is a shared computation, which is correct — the rules are the only input).

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

Not automatic — the basket is a deliberate choice. `POST /options/basket/import?from=`

| `from` | Reads | Writes `source=` |
|---|---|---|
| `ivscan` | `IVScanItem` rows for the user (models.py:904-927), order `pos` | `ivscan` |
| `universe` | `User.prefs["ivscan_universe"]` via `ivscan._clean_symbols` (ivscan.py:91-107) | `typed` |
| `watchlist` | `user_watchlist.symbol_set(db, user)` | `watchlist` |
| `positions` | open `OptionSpread` symbols | `positions` |

The empty-basket state of the page offers these as buttons with counts. Cap 100 symbols per
member (the `MAX_SYMBOLS` of ivscan.py:87; a basket is the same kind of list). Existing rows
are kept; duplicates skipped; returns `{added, skipped, total}`.

#### A6.3 `option_spreads` — untouched

The Positions engine (`spread_monitor`, `bull_put.monitor`) keeps reading `option_spreads`
and writing `spread_checks` exactly as today. New strategies' positions (Part D) add rows
with a different `strategy` value and, where a trade has more than two legs, a `legs` JSON
column in a later revision chained off `f4a5b6c7d8e9`; nothing in this part changes the
table.

### A7. Failure modes

| Failure | Detected by | Stored | Page shows (Part C renders; this part supplies the flag) |
|---|---|---|---|
| Cboe down / 5xx / timeout for all tickers | every `fetch_chain` raises `ChainError`; `option_jobs.errors == symbols` | job row with `ok=0`; yesterday's snapshot untouched | rose pill "tonight's data could not be read (Cboe); showing Oct 1 16:00 ET"; cards render from the previous snapshot with the stale badge; Refresh button offered; if `TST_OPTIONS_FALLBACK=alpaca` the run already retried there and the pill says "via Alpaca" |
| Cboe 403 for one ticker (delisted / typo) | `ChainError("... no Cboe option chain — check the ticker")` (option_quotes.py:144-146) | `detail[sym].status='error'`, signal `status='no_chain'` | the basket row greyed: "no option chain for XYZ — remove it?" |
| Partial chain (few expiries / no deltas) | `Chain.partial` (A1.3) | stored with `iv_daily.partial=True`; engines run only on what is there | amber "chain looks incomplete tonight (2 expiries)"; LEAPS/calendar chips disabled with the reason |
| Stale snapshot (job missed, Hermes down) | `/options/status.stale` or `latest_snap_on < et_today − 1 session` | — | amber strip "data as of Oct 1 — the nightly run has not happened" + Refresh; the card still renders |
| No IV history (new ticker) | `iv.state in ('none','forming')` | rank/pct None, `iv_n` | "IV rank: forming (12 of 60 days) — press Live to load a year from TWS"; gauge = unknown, sell-premium strategies shown as "cannot judge IV yet", never as rejected |
| IV30 source mismatch after a switch | calibration median > 1.5 pts (A3.2) | logged nightly | footnote on the IV line |
| Yahoo bars missing (HV / ATR / trend) | `fetch_daily_ohlc` returns `[]` | chain + iv row stored; `hv* = None`; signal `status='no_setup'`, error "no price history" | "chart could not be read tonight"; IV figures still shown; no strategy recommended |
| Earnings unknown | `fetch_next_earnings` None | `earnings_date=None` | "earnings date unknown — check before trading"; the earnings gate is a warning, not a veto |
| Engine exception | caught at A4.2 f | signal `status='error'` + message; data rows kept | "the rules could not run for LRCX tonight (error id …)"; Refresh recomputes |
| Hermes job never started (task disabled, box off) | no `option_jobs` row for today | — | same as stale; the admin Data Ingest row reads "never ran today" |
| Job started, crashed mid-run | row with `finished_at IS NULL` older than 40 min | partial `detail` | pill "run did not finish (23 of 30 tickers)"; the finished tickers are fresh, the rest stale per card |
| Alpaca credentials missing (source=alpaca) | `ChainError("alpaca: no credentials")` at the first symbol | job `errors=symbols`, note | rose pill "Alpaca source selected but no credentials on this box" |
| Alpaca 429 | status code; `Retry-After` honoured | retries then error per symbol | as Cboe-down for the affected tickers |
| Bridge older than 1.6 on Live | no `series` in the `/iv` reply | nothing written | the restart-the-bridge message |
| SQLite busy (page read during a commit) | `OperationalError: database is locked` after the driver's 5 s wait | — | commits are per symbol (< 1 s), so this should not occur; if it does the request retries once and then renders the stale badge |

### A8. Test plan (README "Tested:" style)

**Synthetic (pure, no network; `dashboard_tst/tests/test_option_data.py`, run with the
venv's python — pytest is added to a dev-only `requirements-dev.txt`):**

- `option_quotes` parser on a saved MSFT fixture (the 2026-10-02 probe, trimmed to 60
  contracts in `tests/fixtures/cboe_MSFT_small.json`): the five new fields present; a `0.0/0.0`
  quote → `bid=ask=mid=None`; `iv=8.3` → `iv=None` after the adapter; `as_of`
  `2026-10-02T15:59:59` → `2026-10-02 19:59:59` UTC; `snap_on='2026-10-02'`; `legs()` equals
  the pre-change dict key for key (so `spread_scan.build_candidates` yields identical rows
  on the fixture before and after).
- `AlpacaSource` on recorded responses: 2 snapshot pages + 1 contracts page → rows joined by
  OCC with `oi` filled; a contract missing from the contracts page → `oi=None`; `iv30=None`,
  `atm_iv30` computed; `expiration_date_lte` present in the request params (the next-weekend
  default trap).
- `BridgePayloadSource`: a `_row`-shaped put list with `iv=43.1` → `0.431`; `oi_ok=False`
  → every `oi=None`; 401 rows → capped at 400; a strike 60 % from spot dropped.
- `option_metrics.hv`: 21 closes of a constant → `0.0`; a known series against numpy's
  `std(ddof=1)·√252·100` to 1e-9; 20 closes → None; last bar with `session_frac=0.4` excluded.
- `atm_iv_by_expiry`: spot exactly on a strike; spot between strikes (interpolation
  weight checked); one leg missing → still computed; one leg only → None.
- `iv30_constant_maturity`: expiries at 20/41 DTE with σ 0.40/0.30 → the CM formula's
  value by hand; only a 45-DTE expiry → its ATM IV; no expiry ≥ 5 DTE → None.
- `iv_rank_pct`: 252 values + today = max → rank 100, pct 99.6; today = min → 0 / 0; flat
  series → rank None, pct 0; n = 19/20/59/60/251/252 → states `none/forming/forming/partial/partial/ok`.
- `term_structure`, `skew25`: front/back selection on a 7-expiry chain; no call within 0.07
  of delta 0.25 → `skew25=None`.
- `replace_snapshot` + `upsert_iv_daily` + `upsert_signal` on an in-memory SQLite: run twice
  with the same chain → identical counts; EOD after intraday → `kind='eod'` on the day row;
  `bootstrap_iv` never overwrites a `cboe` day, inserts the missing ones, bounded input
  rejected (401 points, a future date, iv 7.0).
- `prune`: 100 synthetic days → 90 kept; day-10 rows with |delta| < 0.03 gone, day-3 rows
  intact; expired contracts gone after 7 days; intraday rows from yesterday gone.
- `prefs_hash`: `{}` and the house defaults give the same hash; `{"credit_spread": {"delta_lo": 0.2}}`
  (equal to the default) also the same; `0.25` → different; key order irrelevant.
- `backfill_from_iv_history`: 300 `iv_history` rows, 50 already in `iv_daily` → 250 inserted,
  second run 0.
- Migration: `alembic upgrade head` on an empty DB creates the six tables; on a DB stamped
  at `e2f3a4b5c6d7` with `option_basket` pre-created by `create_all` → no error (guard);
  `downgrade -1` drops all six.

**Live (dev DB on the laptop, then Hermes):**

- `deploy/options_nightly.py NVDA LRCX MSFT KO -v`: four chains, counts logged (MSFT
  ≈ 3,700 rows, 23 expiries), `iv_daily` rows for today with `iv30_src='cboe'` and
  `atm_iv30` within ~2 vol points of Cboe's figure, HV20/HV60 plausible (KO < NVDA),
  `earnings_days` matches the calendar page, four house signals, one `option_jobs` row,
  `/options/status` returns `stale=False`. Re-run: row counts unchanged.
- `--source alpaca` on the same four with the paper keys: same expiries ± the weeklies Cboe
  lists first, `oi` populated from the contracts endpoint, `iv30_src='atm'`, 6 calls per
  ticker in the log, no 429 at 0.4 s pacing.
- Pacing: 30 tickers sequential at 1.5 s → no 429 (the spread scan's measured shape); the
  same 30 with `--pause 0.2` → 429s appear and the backoff recovers them (expected).
- Live bootstrap: on a PC with TWS + bridge 1.6, press Live on a ticker with 15 days of
  history → `inserted ≈ 237`, the rank appears, the ranked figure agrees with the IV Rank
  page's TWS number within 1 point; a pre-1.6 bridge → the restart message; press again →
  `inserted=0`.
- Refresh during the US session: `kind='intraday'` rows replace the previous ones, the day's
  `iv_daily` row says `kind='intraday'`, the 06:00-MYT run next morning turns it `eod`.
- Hermes: `setup_options_nightly_task.ps1` registers `TST-Options-Nightly` 07:15; after the
  first scheduled run `logs\options_nightly.log` has the summary line and the Options page
  pill is green before 07:30 MYT; stop the task for a day → the pill turns amber with
  "the nightly run has not happened" and the cards still render.
- Failure drills: point `TST_OPTIONS_SOURCE` at a typo → the job exits 1 with one clear line;
  add ticker `ZZZZ` to a basket → the row greys with the "no option chain" reason, the other
  tickers are unaffected; kill the job after 2 tickers → the pill reads "did not finish (2 of
  4)".
