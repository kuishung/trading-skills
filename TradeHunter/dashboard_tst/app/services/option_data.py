"""Option chains from any source, behind ONE interface, in ONE normalized shape.

Three feeds can describe the same chain and each spells it differently - Cboe's
delayed JSON (``option_quotes``, the source of record), Alpaca's snapshots (the
contracted fallback) and the member's own IBKR bridge (``bridge/ibkr_bridge.py``,
the "Live" read, posted by the browser). ``ChainSource.fetch_chain`` returns the
same ``Chain`` of ``ContractRow`` for all three (OPTIONS_MODULE_DESIGN.md II.2.12;
design/options/part_A_data.md A1), so the metrics, the store and the engines never
know which feed answered.

Units - one convention, enforced here and nowhere else:

* ``ContractRow.iv`` is a FRACTION (0.3585), whatever the source. Cboe and Alpaca
  send fractions; the raw bridge row is in PERCENT and ``BridgePayloadSource`` is
  the ONLY place that divides by 100. **The unit is decided by the SOURCE, never
  by magnitude**: a deep-in-the-money Cboe row legitimately prints ``iv`` 3.1099,
  so there is no "above three must be a percent" guess anywhere.
* ``Chain.iv30`` is PERCENT (Cboe's constant-maturity figure, the ``iv_history``
  unit); None for a source without one.
* greeks per share, signed as the feed gives them; ``oi`` / ``volume`` are ints
  with None = "the feed did not say" (never 0).
* ``as_of`` is the FEED's own timestamp as naive UTC - staleness is a property
  of the data, never of the fetch; ``snap_on`` is the ET date the chain describes.

Sanity filters applied in every adapter (a row failing them is kept with the bad
field None, so a chain is never silently thinner): ``iv`` outside
``(IV_SANITY_LO, IV_SANITY_HI)`` -> None, ``|delta| > 1`` -> None, a negative
price -> None, a 0.0/0.0 quote -> no quote; a strike <= 0 drops the row.

Every failure raises ``option_quotes.ChainError`` - the SAME class the Portfolio
and Spread pages already catch, so no caller gains a new except branch. A live
(bridge) chain is never persisted; ``Chain.legs()`` / ``as_legacy()`` let the
legacy consumers (``spread_scan.build_candidates``, ``spread_monitor``) run on a
``Chain`` unchanged.
"""
from __future__ import annotations

import abc
import datetime as _dt
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

from . import option_quotes
from .opt_constants import (
    IV_SANITY_HI,
    IV_SANITY_LO,
    LIVE_MAX_ROWS,
    LIVE_STRIKE_WINDOW,
    PARTIAL_MIN_DELTA_SHARE,
    PARTIAL_MIN_DTE,
    PARTIAL_MIN_EXPIRIES,
)
from .option_quotes import ChainError, parse_occ
from .spread_monitor import et_today

log = logging.getLogger(__name__)

SOURCES = ("cboe", "alpaca", "bridge")
KINDS = ("eod", "intraday", "live")


# ------------------------------------------------------------- the shapes
@dataclass(frozen=True)
class Capabilities:
    has_iv30: bool          # a constant-maturity IV30 of its own (Cboe only)
    has_oi: bool            # open interest (bridge: sometimes, per row None)
    has_greeks: bool
    has_rho: bool
    delayed_minutes: int    # 15 / 15 / 0-or-15
    all_expiries: bool      # bridge: one expiry per call
    server_side: bool       # bridge: never
    pacing_seconds: float   # between two symbols in a batch


@dataclass(frozen=True)
class ContractRow:
    expiry: str             # YYYY-MM-DD
    right: str              # "C" | "P"
    strike: float
    bid: float | None
    ask: float | None
    mid: float | None       # (bid + ask) / 2 when both exist, else None (NOT last)
    last: float | None
    bid_size: int | None
    ask_size: int | None
    iv: float | None        # FRACTION - always, whatever the source
    delta: float | None     # signed
    gamma: float | None
    theta: float | None     # per day, per share, signed
    vega: float | None
    rho: float | None
    theo: float | None      # model value when the feed gives one (Cboe)
    oi: int | None          # open interest; None = unknown. The key is `oi`, never `open_interest`
    volume: int | None      # today's contracts; None = unknown
    prev_close: float | None
    dte: int                # calendar days from snap_on to expiry


@dataclass
class Chain:
    symbol: str
    source: str                     # "cboe" | "alpaca" | "bridge"
    kind: str                       # "eod" | "intraday" | "live"
    as_of: _dt.datetime             # naive UTC, the FEED's timestamp
    snap_on: str                    # ET date the chain describes
    spot: float
    iv30: float | None              # PERCENT; None when the source has no constant-maturity figure
    rows: list[ContractRow]
    delayed_minutes: int            # 15 (cboe / alpaca indicative), 0 (bridge live), 15 (bridge delayed)
    header: dict = field(default_factory=dict)   # raw non-contract fields (the underlying's own quote etc.)
    partial: bool = False           # the feed returned fewer expiries / strikes than expected
    note: str = ""                  # "fallback: alpaca" when the primary failed

    # -- views ---------------------------------------------------------------
    def expiries(self) -> list[str]:
        return sorted({r.expiry for r in self.rows})

    @property
    def n_contracts(self) -> int:
        return len(self.rows)

    @property
    def n_expiries(self) -> int:
        return len(self.expiries())

    def rows_for(self, expiry: str, right: str | None = None) -> list[ContractRow]:
        rt = (right or "").upper()[:1]
        return sorted((r for r in self.rows if r.expiry == expiry and (not rt or r.right == rt)),
                      key=lambda r: r.strike)

    def legs(self) -> dict[tuple, dict]:
        """The ``{(expiry, right, strike): {...}}`` dict ``option_quotes.fetch_chain``
        returns today, key for key - the legacy key ``open_interest`` survives HERE
        and only here - so ``spread_scan`` / ``spread_monitor`` / ``portfolio`` run
        unchanged on a Chain. Values are the normalized row's (a sanity-nulled
        ``iv`` stays None; a 0.0/0.0 quote stays None), which is the point."""
        out: dict[tuple, dict] = {}
        for r in self.rows:
            out[(r.expiry, r.right, round(r.strike, 3))] = {
                "expiry": r.expiry, "right": r.right, "strike": r.strike,
                "bid": r.bid, "ask": r.ask, "mid": r.mid,
                "iv": r.iv, "delta": r.delta, "gamma": r.gamma, "theta": r.theta,
                "vega": r.vega, "theo": r.theo,
                "open_interest": r.oi, "volume": r.volume,
                "rho": r.rho, "last": r.last, "bid_size": r.bid_size, "ask_size": r.ask_size,
                "prev_close": r.prev_close,
            }
        return out

    def as_legacy(self) -> dict:
        """``{"symbol", "spot", "iv30", "as_of", "fetched_at", "legs", "header"}`` -
        the ``option_quotes.fetch_chain`` dict, so ``spread_monitor.snapshot(chain=)``
        and ``option_quotes.leg / expiries / strikes`` take a Chain without an edit.
        ``as_of`` is the feed's own ET string when the header carries one (Cboe),
        else the UTC timestamp in ISO form."""
        as_of = self.header.get("last_trade_time") or self.as_of.isoformat(timespec="seconds")
        return {
            "symbol": self.symbol, "spot": self.spot, "iv30": self.iv30,
            "as_of": as_of,
            "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "legs": self.legs(), "header": dict(self.header),
        }


# ---------------------------------------------------------- time helpers
def _nth_sunday(y: int, month: int, n: int) -> _dt.date:
    d = _dt.date(y, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def _et_offset_hours(d: _dt.date) -> int:
    """US Eastern offset by rule (second Sunday of March .. first Sunday of November
    = EDT, -4) - spread_monitor._et_now's fallback, for a box without tzdata."""
    return -4 if _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1) else -5


def _et_naive_to_utc(s: str | None) -> _dt.datetime:
    """'2026-10-02T15:59:59' (New York wall time, Cboe's last_trade_time) -> naive
    UTC 2026-10-02 19:59:59. Unparsable / missing -> now (UTC)."""
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    if not s:
        return now
    try:
        naive = _dt.datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return now
    try:
        from zoneinfo import ZoneInfo
        aware = naive.replace(tzinfo=ZoneInfo("America/New_York"))
        return aware.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - no tzdata on this box
        return naive - _dt.timedelta(hours=_et_offset_hours(naive.date()))


def _utc_to_et_date(utc_naive: _dt.datetime) -> str:
    """The ET trading date a UTC instant falls on."""
    try:
        from zoneinfo import ZoneInfo
        aware = utc_naive.replace(tzinfo=_dt.timezone.utc)
        return aware.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # noqa: BLE001
        return (utc_naive + _dt.timedelta(hours=_et_offset_hours(utc_naive.date()))).date().isoformat()


_FRAC = re.compile(r"(\.\d{1,6})\d*")


def _parse_rfc3339(s: str | None) -> _dt.datetime | None:
    """'2026-10-02T19:59:59.123456789Z' -> naive UTC (Alpaca's nanosecond stamps)."""
    if not s:
        return None
    t = str(s).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    t = _FRAC.sub(r"\1", t)
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return d


def _dte(expiry: str, snap_on: str) -> int:
    try:
        return (_dt.date.fromisoformat(expiry) - _dt.date.fromisoformat(snap_on[:10])).days
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------- the row builder
def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _price(v):
    f = _num(v)
    return None if (f is None or f < 0) else f


def _int(v):
    f = _num(v)
    return int(f) if (f is not None and 0 <= f < 1e9) else None


def make_row(*, expiry: str, right: str, strike, snap_on: str, bid=None, ask=None, last=None,
             bid_size=None, ask_size=None, iv=None, delta=None, gamma=None, theta=None, vega=None,
             rho=None, theo=None, oi=None, volume=None, prev_close=None) -> ContractRow | None:
    """One normalized row with the sanity filters applied. ``iv`` must already be a
    FRACTION (the caller's adapter knows its feed's unit). None = drop the row
    (bad strike, bad expiry, bad right)."""
    k = _num(strike)
    rt = (right or "").upper()[:1]
    if k is None or k <= 0 or rt not in ("C", "P") or not expiry:
        return None
    b, a = _price(bid), _price(ask)
    if b == 0.0 and a == 0.0:               # Cboe sends 0.0/0.0 for "no quote"
        b = a = None
    mid = (b + a) / 2.0 if (b is not None and a is not None) else None   # unrounded, as the legacy parser (legs() key for key)
    v = _num(iv)
    if v is not None and not (IV_SANITY_LO < v < IV_SANITY_HI):
        v = None
    d = _num(delta)
    if d is not None and abs(d) > 1.0:
        d = None
    return ContractRow(
        expiry=expiry, right=rt, strike=k, bid=b, ask=a, mid=mid, last=_price(last),
        bid_size=_int(bid_size), ask_size=_int(ask_size), iv=v, delta=d,
        gamma=_num(gamma), theta=_num(theta), vega=_num(vega), rho=_num(rho), theo=_num(theo),
        oi=_int(oi), volume=_int(volume), prev_close=_price(prev_close),
        dte=_dte(expiry, snap_on),
    )


def _looks_partial(rows: list[ContractRow]) -> bool:
    """True when (a) fewer than 2 expiries, (b) no expiry with dte >= 20, or (c)
    under 30 % of rows carry a delta. Cboe has never been seen truncating a file,
    but the endpoint is undocumented and the page needs a flag to show (A7)."""
    if not rows:
        return True
    expiries = {r.expiry for r in rows}
    if len(expiries) < PARTIAL_MIN_EXPIRIES:
        return True
    if max(r.dte for r in rows) < PARTIAL_MIN_DTE:
        return True
    with_delta = sum(1 for r in rows if r.delta is not None)
    return with_delta < PARTIAL_MIN_DELTA_SHARE * len(rows)


def chain_from_legacy(raw: dict, *, source: str = "cboe", kind: str = "eod",
                      delayed_minutes: int = 15) -> Chain:
    """``option_quotes.fetch_chain``'s dict (or a saved payload parsed by
    ``option_quotes.parse_chain``) -> a normalized Chain. ``as_of`` = the feed's
    ``last_trade_time`` (New York wall time) in UTC; ``snap_on`` = its ET date, so
    a run after a US holiday re-files the prior session under its own date."""
    as_of_str = raw.get("as_of")
    as_of = _et_naive_to_utc(as_of_str)
    snap_on = (str(as_of_str or "")[:10]) or et_today()
    rows: list[ContractRow] = []
    for leg in (raw.get("legs") or {}).values():
        r = make_row(
            expiry=leg.get("expiry"), right=leg.get("right"), strike=leg.get("strike"), snap_on=snap_on,
            bid=leg.get("bid"), ask=leg.get("ask"), last=leg.get("last"),
            bid_size=leg.get("bid_size"), ask_size=leg.get("ask_size"),
            iv=leg.get("iv"), delta=leg.get("delta"), gamma=leg.get("gamma"), theta=leg.get("theta"),
            vega=leg.get("vega"), rho=leg.get("rho"), theo=leg.get("theo"),
            oi=leg.get("open_interest", leg.get("oi")), volume=leg.get("volume"),
            prev_close=leg.get("prev_close"),
        )
        if r is not None:
            rows.append(r)
    if not rows:
        raise ChainError(f"{raw.get('symbol')}: chain returned no usable contracts")
    spot = _num(raw.get("spot"))
    if not spot or spot <= 0:
        raise ChainError(f"{raw.get('symbol')}: chain carries no spot price")
    header = dict(raw.get("header") or {})
    if as_of_str and "last_trade_time" not in header:
        header["last_trade_time"] = as_of_str
    return Chain(symbol=(raw.get("symbol") or "").upper(), source=source, kind=kind, as_of=as_of,
                 snap_on=snap_on, spot=spot, iv30=_num(raw.get("iv30")), rows=rows,
                 delayed_minutes=delayed_minutes, header=header, partial=_looks_partial(rows))


# ------------------------------------------------------------ the sources
class ChainSource(abc.ABC):
    name: str = ""
    capabilities: Capabilities

    @abc.abstractmethod
    def fetch_chain(self, symbol: str, *, fresh: bool = False, retries: int = 0) -> Chain:
        """Every listed contract on every expiry for one underlying, normalized.
        Raises ``option_quotes.ChainError`` on any network or shape failure.
        ``fresh`` bypasses the in-process cache (the nightly job passes True)."""

    def fetch_iv30(self, symbol: str) -> float | None:
        """The feed's own 30-day constant-maturity IV in PERCENT, or None. Default:
        ``fetch_chain(symbol).iv30`` (a cache hit, so free)."""
        return self.fetch_chain(symbol).iv30


class CboeSource(ChainSource):
    """Cboe's delayed JSON through ``option_quotes.fetch_chain`` (its 15-minute
    cache, 429 backoff and 403-means-bad-ticker message stay where they are)."""

    name = "cboe"
    capabilities = Capabilities(has_iv30=True, has_oi=True, has_greeks=True, has_rho=True,
                                delayed_minutes=15, all_expiries=True, server_side=True,
                                pacing_seconds=1.5)

    def fetch_chain(self, symbol: str, *, fresh: bool = False, retries: int = 0) -> Chain:
        if fresh:
            option_quotes.clear_cache()           # per process; the job wants a real read
        raw = option_quotes.fetch_chain(symbol, retries=retries)   # ChainError propagates
        return chain_from_legacy(raw, source="cboe", kind="eod", delayed_minutes=15)


ALPACA_DATA = "https://data.alpaca.markets"
ALPACA_PAPER = "https://paper-api.alpaca.markets"
ALPACA_CONTRACT_HORIZON_DAYS = 1200     # expiration_date_lte: the contracts endpoint defaults to the next weekend, which would lose every LEAPS
ALPACA_TTL = 900.0
_alpaca_cache: dict[str, tuple[float, Chain]] = {}
_alpaca_lock = threading.Lock()


def _alpaca_keys() -> tuple[str, str] | None:
    """``ALPACA_API_KEY_ID`` / ``ALPACA_API_SECRET_KEY`` from the vault through
    ``scripts._common._env_lookup("alpaca.env")`` (then ``alpaca-trader-paper.env``),
    importable because ``resources_bridge`` puts the TradeHunter root on sys.path.
    NOT ``load_alpaca_env`` - it sys.exits on a missing file, which would take
    uvicorn down. Plain environment variables are the last resort. None = no creds."""
    lookups = []
    try:
        from . import resources_bridge  # noqa: F401 - sys.path side effect
        from scripts._common import _env_lookup  # type: ignore
        lookups = [lambda fn=fn: _env_lookup(fn) for fn in ("alpaca.env", "alpaca-trader-paper.env")]
    except Exception:  # noqa: BLE001 - no scripts/ beside this checkout
        lookups = []
    for look in lookups:
        try:
            env = look() or {}
        except Exception:  # noqa: BLE001
            env = {}
        k, s = env.get("ALPACA_API_KEY_ID"), env.get("ALPACA_API_SECRET_KEY")
        if k and s:
            return str(k).strip(), str(s).strip()
    k, s = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if k and s:
        return k.strip(), s.strip()
    return None


class AlpacaSource(ChainSource):
    """Alpaca options snapshots (``/v1beta1/options/snapshots/{sym}``, feed
    ``indicative`` free / ``opra`` with the subscription) joined by OCC symbol with
    ``/v2/options/contracts`` for open interest, spot from the latest IEX trade.
    ~6 calls per ticker at 0.4 s pacing stays under the 200/min cap. A ``client``
    may be injected (tests use ``httpx.MockTransport``)."""

    name = "alpaca"
    capabilities = Capabilities(has_iv30=False, has_oi=True, has_greeks=True, has_rho=True,
                                delayed_minutes=15, all_expiries=True, server_side=True,
                                pacing_seconds=0.4)

    def __init__(self, *, client: httpx.Client | None = None, feed: str | None = None,
                 keys: tuple[str, str] | None = None, page_pause: float | None = None):
        self._client = client
        self.feed = (feed or _setting("alpaca_feed", "indicative") or "indicative").strip().lower()
        self._keys = keys
        self.page_pause = self.capabilities.pacing_seconds if page_pause is None else page_pause

    # -- plumbing --
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0, follow_redirects=True,
                                        headers={"User-Agent": option_quotes._UA})
        return self._client

    def _headers(self, symbol: str) -> dict:
        keys = self._keys or _alpaca_keys()
        if not keys:
            raise ChainError("alpaca: no credentials (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY in alpaca.env)")
        return {"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1], "Accept": "application/json"}

    def _get(self, symbol: str, url: str, params: dict, headers: dict, retries: int) -> dict:
        r = None
        try:
            for attempt in range(max(0, int(retries)) + 1):
                r = self._http().get(url, params=params, headers=headers)
                if r.status_code == 429 and attempt < retries:
                    wait = _num(r.headers.get("Retry-After")) or 20.0
                    time.sleep(wait)
                    continue
                break
            assert r is not None
            if r.status_code in (401, 403):
                raise ChainError(f"{symbol}: Alpaca rejected the credentials (HTTP {r.status_code})")
            if r.status_code == 404:
                raise ChainError(f"{symbol}: Alpaca has no option chain - check the ticker")
            r.raise_for_status()
            return r.json()
        except ChainError:
            raise
        except Exception as exc:  # noqa: BLE001 - network, JSON
            raise ChainError(f"{symbol}: Alpaca {type(exc).__name__}: {exc}") from exc

    def _paged(self, symbol, url, params, headers, retries):
        token = None
        while True:
            p = dict(params)
            if token:
                p["page_token"] = token
            j = self._get(symbol, url, p, headers, retries)
            yield j
            token = j.get("next_page_token")
            if not token:
                return
            if self.page_pause:
                time.sleep(self.page_pause)

    # -- the read --
    def fetch_chain(self, symbol: str, *, fresh: bool = False, retries: int = 0) -> Chain:
        sym = (symbol or "").strip().upper()
        if not sym:
            raise ChainError("no symbol")
        now = time.time()
        if not fresh:
            with _alpaca_lock:
                hit = _alpaca_cache.get(sym)
                if hit and now - hit[0] < ALPACA_TTL:
                    return hit[1]
        headers = self._headers(sym)
        snaps: dict[str, dict] = {}
        for page in self._paged(sym, f"{ALPACA_DATA}/v1beta1/options/snapshots/{sym}",
                                {"feed": self.feed, "limit": 1000}, headers, retries):
            snaps.update(page.get("snapshots") or {})
        if not snaps:
            raise ChainError(f"{sym}: Alpaca returned no contracts - check the ticker")
        today = _dt.date.today()
        contracts: dict[str, dict] = {}
        for page in self._paged(sym, f"{ALPACA_PAPER}/v2/options/contracts",
                                {"underlying_symbols": sym, "status": "active", "limit": 10000,
                                 "expiration_date_gte": today.isoformat(),
                                 "expiration_date_lte": (today + _dt.timedelta(days=ALPACA_CONTRACT_HORIZON_DAYS)).isoformat()},
                                headers, retries):
            for c in page.get("option_contracts") or []:
                if c.get("symbol"):
                    contracts[c["symbol"]] = c
        spot = None
        try:
            j = self._get(sym, f"{ALPACA_DATA}/v2/stocks/{sym}/trades/latest", {"feed": "iex"}, headers, retries)
            spot = _num((j.get("trade") or {}).get("p"))
        except ChainError:
            spot = None
        if not spot:
            try:
                from . import prices
                q = prices.fetch_quote(sym)
                spot = _num((q or {}).get("price"))
            except Exception:  # noqa: BLE001
                spot = None
        if not spot or spot <= 0:
            raise ChainError(f"{sym}: no spot price from Alpaca or Yahoo")
        stamps = [_parse_rfc3339(((s.get("latestQuote") or {}).get("t"))) for s in snaps.values()]
        stamps = [t for t in stamps if t is not None]
        as_of = max(stamps) if stamps else _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        snap_on = _utc_to_et_date(as_of)
        rows: list[ContractRow] = []
        oi_dates: list[str] = []
        for occ, s in snaps.items():
            p = parse_occ(occ)
            if p is None:
                continue
            q = s.get("latestQuote") or {}
            t = s.get("latestTrade") or {}
            g = s.get("greeks") or {}
            day = s.get("dailyBar") or {}
            prev = s.get("prevDailyBar") or {}
            c = contracts.get(occ) or {}
            if c.get("open_interest_date"):
                oi_dates.append(str(c["open_interest_date"]))
            r = make_row(
                expiry=p["expiry"], right=p["right"], strike=p["strike"], snap_on=snap_on,
                bid=q.get("bp"), ask=q.get("ap"), bid_size=q.get("bs"), ask_size=q.get("as"),
                last=t.get("p"), iv=s.get("impliedVolatility"),
                delta=g.get("delta"), gamma=g.get("gamma"), theta=g.get("theta"), vega=g.get("vega"), rho=g.get("rho"),
                oi=c.get("open_interest") if c else None, volume=day.get("v"), prev_close=prev.get("c"),
            )
            if r is not None:
                rows.append(r)
        if not rows:
            raise ChainError(f"{sym}: Alpaca returned no usable contracts")
        chain = Chain(symbol=sym, source="alpaca", kind="eod", as_of=as_of, snap_on=snap_on, spot=spot,
                      iv30=None, rows=rows, delayed_minutes=15 if self.feed == "indicative" else 0,
                      header={"feed": self.feed, "oi_date": max(oi_dates) if oi_dates else None,
                              "n_snapshots": len(snaps), "n_contracts_listed": len(contracts)},
                      partial=_looks_partial(rows))
        with _alpaca_lock:
            _alpaca_cache[sym] = (now, chain)
        return chain


class BridgePayloadSource(ChainSource):
    """The member's "Live" read: built from the ``/chain`` reply the browser POSTs
    to ``POST /options/live/{symbol}`` - no network, the body is untrusted and every
    number is re-checked. ONE expiry (the bridge's), rows capped at LIVE_MAX_ROWS,
    strikes within LIVE_STRIKE_WINDOW of spot, ``iv / 100`` (the bridge's rows are
    PERCENT - the ONLY division in the module), ``oi`` None for every row when the
    bridge says ``oi_ok`` is False. Never persisted (``kind="live"``)."""

    name = "bridge"
    capabilities = Capabilities(has_iv30=False, has_oi=True, has_greeks=True, has_rho=False,
                                delayed_minutes=0, all_expiries=False, server_side=False,
                                pacing_seconds=0.0)

    def __init__(self, payload: dict, *, diag: dict | None = None, symbol: str | None = None,
                 today: str | None = None):
        self.payload = payload if isinstance(payload, dict) else {}
        self.diag = diag if isinstance(diag, dict) else {}
        self.chain = self._build(symbol, today)

    def _build(self, symbol: str | None, today: str | None) -> Chain:
        p = self.payload
        if p.get("ok") is False:
            raise ChainError(f"bridge: {p.get('error') or 'the chain reply says ok=false'}")
        sym = (symbol or p.get("symbol") or "").strip().upper()
        if not sym:
            raise ChainError("bridge: no symbol in the chain reply")
        if symbol and p.get("symbol") and str(p["symbol"]).strip().upper() != sym:
            raise ChainError(f"bridge: the chain is for {str(p['symbol']).upper()}, not {sym}")
        spot = _num(p.get("spot"))
        if not spot or spot <= 0:
            raise ChainError(f"{sym}: bridge chain carries no spot price")
        exp_raw = str(p.get("expiry") or "")
        if len(exp_raw) == 8 and exp_raw.isdigit():
            expiry = f"{exp_raw[:4]}-{exp_raw[4:6]}-{exp_raw[6:]}"
        else:
            expiry = exp_raw[:10]
        try:
            _dt.date.fromisoformat(expiry)
        except ValueError as exc:
            raise ChainError(f"{sym}: bridge chain has no usable expiry ({exp_raw!r})") from exc
        snap_on = today or et_today()
        oi_ok = p.get("oi_ok")
        oi_known = oi_ok is not False
        lo, hi = spot * (1 - LIVE_STRIKE_WINDOW), spot * (1 + LIVE_STRIKE_WINDOW)
        raw_rows = [(r, "P") for r in (p.get("puts") or []) if isinstance(r, dict)] \
            + [(r, "C") for r in (p.get("calls") or []) if isinstance(r, dict)]
        rows: list[ContractRow] = []
        for r, rt in raw_rows:
            k = _num(r.get("strike"))
            if k is None or not (lo <= k <= hi):
                continue
            iv = _num(r.get("iv"))
            row = make_row(
                expiry=expiry, right=rt, strike=k, snap_on=snap_on,
                bid=r.get("bid"), ask=r.get("ask"), last=r.get("last"),
                iv=(iv / 100.0) if iv is not None else None,           # the bridge's rows are PERCENT
                delta=r.get("delta"), gamma=r.get("gamma"), theta=r.get("theta"), vega=r.get("vega"),
                oi=r.get("oi") if oi_known else None, volume=r.get("volume"),
            )
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda x: (abs(x.strike - spot), x.right, x.strike))
        rows = rows[:LIVE_MAX_ROWS]
        rows.sort(key=lambda x: (x.right, x.strike))
        if not rows:
            raise ChainError(f"{sym}: the live chain carried no usable contracts")
        mode = (self.diag.get("data_mode") or p.get("data_mode") or "").lower()
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        header = {
            "greeks_from_delayed": bool(p.get("greeks_from_delayed")),
            "oi_ok": oi_ok, "bridge": self.diag.get("bridge") or p.get("bridge"),
            "data_mode": mode or None, "expiry_label": p.get("expiry_label"),
            "atm_iv": _num(p.get("atm_iv")), "source_text": p.get("source"),
        }
        return Chain(symbol=sym, source="bridge", kind="live", as_of=now, snap_on=snap_on, spot=spot,
                     iv30=None, rows=rows, delayed_minutes=0 if mode == "live" else 15,
                     header=header, partial=True)          # one expiry is partial by construction

    def fetch_chain(self, symbol: str, *, fresh: bool = False, retries: int = 0) -> Chain:
        if symbol and (symbol or "").strip().upper() != self.chain.symbol:
            raise ChainError(f"bridge: the posted chain is for {self.chain.symbol}, not {symbol.upper()}")
        return self.chain


# ------------------------------------------------------- factory + fallback
_SOURCE_CLASSES = {"cboe": CboeSource, "alpaca": AlpacaSource}


def _setting(name: str, default):
    """``app.config.settings.<name>`` when the Settings class has it, else the
    default - the Options settings land with the module and may lag this file."""
    try:
        from .config import settings
        v = getattr(settings, name, None)
    except Exception:  # noqa: BLE001
        v = None
    return default if v is None else v


def source(name: str | None = None) -> ChainSource:
    """``settings.options_source`` (TST_OPTIONS_SOURCE, default "cboe") unless
    overridden. An unknown name raises ChainError at construction, so a typo in
    .env fails the nightly job loudly, not per symbol."""
    n = str(name or _setting("options_source", "cboe") or "cboe").strip().lower()
    cls = _SOURCE_CLASSES.get(n)
    if cls is None:
        raise ChainError(f"unknown options source {n!r} - TST_OPTIONS_SOURCE must be one of {sorted(_SOURCE_CLASSES)}")
    return cls()


def fetch_chain(symbol: str, *, fresh: bool = False, retries: int = 0) -> Chain:
    """The primary source; on ChainError, if ``settings.options_fallback``
    (TST_OPTIONS_FALLBACK) names the other server-side source, try it once and set
    ``chain.note = "fallback: <name>"``. Both attempts are logged; ``Chain.source``
    says which one answered."""
    primary = source()
    try:
        return primary.fetch_chain(symbol, fresh=fresh, retries=retries)
    except ChainError as exc:
        fb = str(_setting("options_fallback", "") or "").strip().lower()
        if not fb or fb == primary.name or fb not in _SOURCE_CLASSES:
            raise
        log.warning("%s: %s failed (%s); trying %s", symbol, primary.name, exc, fb)
        chain = source(fb).fetch_chain(symbol, fresh=fresh, retries=retries)
        chain.note = f"fallback: {fb}"
        log.info("%s: %s answered after %s failed", symbol, fb, primary.name)
        return chain
