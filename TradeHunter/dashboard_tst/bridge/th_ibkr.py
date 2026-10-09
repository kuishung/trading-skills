"""TradeHunter IBKR fetch library (Options v2, OPTIONS_V2_DESIGN.md section 3).

One module, used by BOTH readers of option data:

* the Hermes collector (``app/services/opt_collector.py``, IB Gateway, clientId 89), and
* each member's connector (``bridge/ibkr_bridge.py`` 2.0, the member's own TWS),

so the two can never disagree on how a chain is read. That is why it lives in
``bridge/`` (the folder the connector zip ships) and imports nothing from the app:
stdlib only, plus ``ib_insync`` imported LAZILY inside the functions that need it -
``ib_insync`` cannot even be imported on Python 3.14 (eventkit calls
``asyncio.get_event_loop()`` at import), and the web app / test suite run there.

Every IB-facing function is ``async``, takes an already-connected ``ib_insync.IB``
and never connects itself. ``plan`` is pure.

The IBKR behaviour below was measured on the 1.x bridge (``ibkr_bridge.py`` 1.6) and
is carried over on purpose:

* streaming ``reqMktData`` subscriptions, never snapshots - IBKR holds a snapshot open
  until it "ends" (up to 11 s with no fresh trade) and refuses generic ticks on it;
* a quote window closes once the data has ARRIVED and stopped changing, not after a
  fixed sleep;
* ib_insync keeps one Ticker per contract object for the life of the connection and
  ``cancelMktData`` does not clear it, so a ticker is blanked before it is re-used
  (``_forget``) - otherwise "has the data arrived?" answers yes off last time's numbers;
* the market data type falls back in preference order when the first yields no
  quotes, keeping what the failed attempt did deliver (TWS does not resend model
  greeks to a re-subscription seconds after the first), and a fallback verdict is
  re-tested after ``MDT_RECHECK`` seconds;
* open interest is read from the tick for the contract's own side (27 call / 28 put).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import math
import time
import weakref
from collections import Counter

VERSION = "2.0"

MDT_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}
MDT_IDS = {v: k for k, v in MDT_NAMES.items()}
MDT_ORDER = (1, 2, 3, 4)

# ---------------------------------------------------------------- pacing
# IBKR's limits are per LOGIN, shared by every client of it (TWS itself, the
# trading bot, the ingest on clientId 84, this process). Each constant keeps THIS
# process well inside its share.
HIST_GAP = 11.0           # s between historical requests in this process: IBKR caps
                          # historical data at 60 requests per 10 minutes; 11 s apart
                          # is <= 55 per 10 min, leaving the rest for the ingest.
MSG_RATE = 40             # API messages per second (token bucket). TWS disconnects a
                          # client that sends more than 50/s; ib_insync's own throttle
                          # (45/s) is the backstop. <= 0 disables (tests).
QUALIFY_BATCH = 50        # contracts per qualifyContractsAsync call (one message each)
DEFAULT_MAX_LINES = 60    # concurrent market-data lines per wave. A login's allowance
                          # (100 by default) is shared by every client, so a wave
                          # never takes them all.
OPT_TICKS = "101"         # option open interest (ticks 27/28) - the only generic tick the
                          # 1.x bridge proved on option contracts. Day volume and the model
                          # greeks / IV come with the default tick set; "100" / "106" are
                          # UNDERLYING ticks (option volume / IV of the stock) and on an
                          # option contract risk error 321, which empties the whole chain.
NEG_TTL = 3 * 86400.0     # s a "contract does not exist" verdict is cached. The strike
                          # list is the union over all expiries, so most planned strikes
                          # do not exist on a given (far) expiry - ~2,000 per large single
                          # name. At 6 h every evening's EOD pass asked IBKR about all of
                          # them again (~50 s of messages per symbol, two log lines each).
                          # New strikes are listed as the price moves, so the verdict
                          # still expires - after 3 days.

# ------------------------------------------------------- quote windows
QUOTE_POLL = 0.25         # how often a window looks at what has arrived
QUOTE_MIN = 1.5           # the "quiet" exits never fire before this (fields arrive in
                          # stages: OI ~1.0 s, greeks ~1.5-2.0 s, prices out of hours
                          # ~3.1-3.6 s - measured 2026-09-19 on 22 JPM puts)
QUOTE_QUIET = 1.0         # priced + greeks on half, and no new field for this long
QUOTE_STALL = 3.0         # no new field AT ALL for this long = no entitlement / done
SPOT_POLL = 0.15
SPOT_WAIT = 6.0           # longest wait for a stock price. Out of hours the close
                          # lands at 3.1-3.6 s (measured); 3.0 failed HD / XOM.
SPOT_SETTLE = 0.8         # after this, the last / close is good enough to plan strikes
MDT_RECHECK = 900.0       # s a market-data-type verdict is trusted before re-probing
                          # (a verdict reached on a Saturday must not outlive Monday's open)
DEFAULT_IV = 0.40         # plan() width when no IV hint is known (fraction)

_NAN = float("nan")
_TICK_FIELDS = ("bid", "ask", "last", "close", "volume", "putOpenInterest",
                "callOpenInterest", "bidSize", "askSize", "lastSize")
_GREEK_KEYS = ("iv", "delta", "gamma", "theta", "vega", "und_price")

# time hooks (tests swap these for a fake clock)
_now = time.monotonic


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


# ------------------------------------------------------------ numbers
def _num(v):
    """A finite float, or None for None / NaN / inf / IBKR's huge "unset" doubles."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or math.isinf(f) or abs(f) >= 1e12:
        return None
    return f


def _pos(v):
    """A price: finite and > 0, else None (IBKR sends -1 / 0 for "no quote")."""
    f = _num(v)
    return f if (f is not None and f > 0) else None


def _count(v):
    """A tick count (size, volume, open interest) or None. ib_insync leaves a tick
    that never arrived as NaN; 0 is a real answer ("nobody holds this strike")."""
    f = _num(v)
    return int(f) if (f is not None and f >= 0) else None


def _rnd(v, nd):
    return None if v is None else round(v, nd)


# -------------------------------------------------------------- dates
def _to_date(x):
    """date from a date/datetime, ``YYYY-MM-DD`` or ``YYYYMMDD``; None otherwise."""
    if x is None:
        return None
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    s = str(x).strip()
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return _dt.date.fromisoformat(s[:10])
        if len(s) >= 8 and s[:8].isdigit():
            return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    return None


def _us_eastern_offset(utc_naive: _dt.datetime) -> int:
    """Hours US Eastern is behind UTC at this naive-UTC instant (4 in DST, else 5).
    DST runs from the 2nd Sunday of March 02:00 EST (07:00 UTC) to the 1st Sunday
    of November 02:00 EDT (06:00 UTC)."""
    y = utc_naive.year
    mar1 = _dt.date(y, 3, 1)
    start = _dt.datetime(y, 3, 1 + (6 - mar1.weekday()) % 7 + 7, 7)
    nov1 = _dt.date(y, 11, 1)
    end = _dt.datetime(y, 11, 1 + (6 - nov1.weekday()) % 7, 6)
    return 4 if start <= utc_naive < end else 5


def et_today(now=None) -> _dt.date:
    """Today's date in New York (expiries and DTE are exchange dates). ``now`` is a
    UTC datetime (naive = UTC). Without a tz database (Windows Python without the
    ``tzdata`` package - the member's connector) the US DST rule is applied by hand."""
    utc = now or _dt.datetime.now(_dt.timezone.utc)
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=_dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001  (ZoneInfoNotFoundError, missing module)
        naive = utc.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return (naive - _dt.timedelta(hours=_us_eastern_offset(naive))).date()


def third_friday(year: int, month: int) -> _dt.date:
    """The standard monthly expiration date of a month."""
    d = _dt.date(year, month, 15)
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


def is_monthly(d: _dt.date, listed=None) -> bool:
    """True for a monthly expiry: the third Friday, or the Thursday before it when
    that Friday is an exchange holiday (Good Friday 2025-04-18, Juneteenth observed
    2027-06-18) - recognised by the Friday itself not being listed."""
    tf = third_friday(d.year, d.month)
    if d == tf:
        return True
    return d == tf - _dt.timedelta(days=1) and listed is not None and tf not in listed


# -------------------------------------------------------- module state
_STICKY: dict = {}        # key -> (market data type, _now() when decided)
_QCACHE: dict = {}        # (ib symbol, YYYYMMDD, strike, right, trading class) -> (contract | None, _now())
_QCACHE_DAY = [None]      # ET date the cache was last pruned of expired contracts
_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()   # event loop -> Lock


def reset_mdt() -> None:
    """Forget every market-data-type verdict (call after reconnecting to another
    TWS / login: the entitlement may differ)."""
    _STICKY.clear()


def clear_cache() -> None:
    """Forget every qualified contract (and every "does not exist" verdict)."""
    _QCACHE.clear()
    _QCACHE_DAY[0] = None


def _sticky_get(key):
    hit = _STICKY.get(key)
    if hit and _now() - hit[1] < MDT_RECHECK:
        return hit[0]
    return None


def _sticky_set(key, mdt: int) -> None:
    _STICKY[key] = (mdt, _now())


def _market_lock() -> asyncio.Lock:
    """One market-data user at a time per event loop, so this process never holds
    more than one wave's lines (plus nothing else) however many callers it has."""
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


class _Bucket:
    """Token bucket: bursts up to MSG_RATE messages, then MSG_RATE per second."""

    def __init__(self) -> None:
        self.level = None
        self.at = 0.0

    def reset(self) -> None:
        self.level = None

    def _reserve(self, n: int):
        """Refill, then take ``n`` tokens (the level may go negative). Returns the
        rate, or None when pacing is off."""
        rate = float(MSG_RATE)
        if rate <= 0 or n <= 0:
            return None
        now = _now()
        if self.level is None:
            self.level, self.at = rate, now
        self.level = min(rate, self.level + (now - self.at) * rate)
        self.at = now
        self.level -= n           # reserved before sleeping: concurrent takers queue up
        return rate

    async def take(self, n: int = 1) -> None:
        rate = self._reserve(n)
        if rate is not None and self.level < 0:
            await _sleep(-self.level / rate)

    def charge(self, n: int = 1) -> None:
        """Count ``n`` messages that were sent WITHOUT waiting - the release of market
        data lines, which must never stop half way. The next ``take`` waits for them,
        so the rate still holds over time."""
        self._reserve(n)


_bucket = _Bucket()
_hist_next = [0.0]


async def _pace(n: int = 1) -> None:
    await _bucket.take(n)


async def _hist_slot() -> None:
    """Wait for this process's next historical-request slot (HIST_GAP apart). The
    slot is reserved synchronously, so concurrent callers queue instead of racing.
    A caller cancelled while it waits gives its slot back when nobody queued behind
    it (a cancelled read must not push later reads further into the future)."""
    now = _now()
    start = max(now, _hist_next[0])
    _hist_next[0] = start + HIST_GAP
    if start > now:
        try:
            await _sleep(start - now)
        except BaseException:
            if _hist_next[0] == start + HIST_GAP:
                _hist_next[0] = start
            raise


# ------------------------------------------------------------ deadlines
def _until(deadline):
    """``deadline`` (seconds from now) as an absolute ``_now()`` time; None = none."""
    if deadline is None:
        return None
    try:
        d = float(deadline)
    except (TypeError, ValueError):
        return None
    if d != d:
        return None
    return _now() + max(0.0, d)


def _left(end):
    """Seconds left before ``end`` (None = no deadline)."""
    return None if end is None else end - _now()


def _expired(end) -> bool:
    return end is not None and _now() >= end


def _capped(wait: float, end) -> float:
    """A quote window's ceiling, cut to the time left before ``end`` (one poll at least)."""
    left = _left(end)
    return wait if left is None else max(QUOTE_POLL, min(wait, left))


def _pref(p) -> tuple:
    """Normalise a market-data-type preference (ints or names) to a tuple of ints."""
    out = []
    for x in (p or ()):
        if isinstance(x, str):
            x = int(x) if x.strip().isdigit() else MDT_IDS.get(x.strip().lower())
        try:
            t = int(x)
        except (TypeError, ValueError):
            continue
        if t in MDT_NAMES and t not in out:
            out.append(t)
    return tuple(out) or MDT_ORDER


def _alt_type(verdict: int, pref: tuple):
    """The other family to try for greeks: live/frozen <-> delayed. Delayed-frozen
    (4) first - it is the one that answers out of hours, and free."""
    for t in ((4, 3) if verdict in (1, 2) else (1, 2)):
        if t in pref and t != verdict:
            return t
    return None


def _observed(values, fallback: int) -> int:
    """The market data type TWS says it delivered (its marketDataType callback),
    most common over the priced tickers, else the type requested. A tie goes to
    the more delayed type - never claim fresher data than was seen."""
    c = Counter(v for v in values if isinstance(v, int) and v in MDT_NAMES)
    if not c:
        return fallback
    top = max(c.values())
    return max(v for v, n in c.items() if n == top)


async def _set_type(ib, t: int) -> None:
    await _pace(1)
    ib.reqMarketDataType(t)


def _ib_symbol(symbol) -> str:
    """IBKR spells share classes with a space: BRK.B / BRK-B -> 'BRK B'."""
    return str(symbol or "").strip().upper().replace(".", " ").replace("-", " ")


# ------------------------------------------------------------ tickers
def _forget(ib, contract) -> None:
    """Blank what an EARLIER subscription left on this contract's ticker (see the
    module docstring) so the next window only counts what this one receives."""
    try:
        t = ib.ticker(contract)
    except Exception:  # noqa: BLE001
        return
    if t is None:
        return
    for name in _TICK_FIELDS:
        try:
            setattr(t, name, _NAN)
        except Exception:  # noqa: BLE001
            pass
    for name in ("modelGreeks", "bidGreeks", "askGreeks", "lastGreeks", "time"):
        try:
            setattr(t, name, None)
        except Exception:  # noqa: BLE001
            pass


def _unmark(t) -> None:
    """Clear the ticker's market data type right after subscribing. ib_insync
    defaults it to 1 (live) and only TWS's marketDataType callback changes it, so a
    left-over or default value must not be read as "this was live"."""
    try:
        t.marketDataType = None
    except Exception:  # noqa: BLE001
        pass


def _greeks_ok(g) -> bool:
    d = _num(getattr(g, "delta", None)) if g is not None else None
    return d is not None and -1.0 <= d <= 1.0


def _state(t):
    """(populated fields, has a price, has greeks) for one ticker."""
    bid, ask, last, close = (_pos(getattr(t, k, None)) for k in ("bid", "ask", "last", "close"))
    greeks = _greeks_ok(getattr(t, "modelGreeks", None))
    priced = (bid is not None and ask is not None) or last is not None or close is not None
    n = sum(1 for v in (bid, ask, last, close) if v is not None) + (1 if greeks else 0)
    n += sum(1 for k in ("putOpenInterest", "callOpenInterest", "volume")
             if _num(getattr(t, k, None)) is not None)
    return n, priced, greeks


def _open_interest(t, right: str):
    """IBKR reports a contract's OI on the tick for ITS side: 27 (call) / 28 (put)."""
    first, second = (("putOpenInterest", "callOpenInterest") if right == "P"
                     else ("callOpenInterest", "putOpenInterest"))
    oi = _count(getattr(t, first, None))
    return oi if oi is not None else _count(getattr(t, second, None))


def _row(t, expiry: str, right: str, strike: float) -> dict:
    """One contract in the shared row shape. Units: iv FRACTION, delta signed,
    theta per day, vega per vol point. None wherever IBKR sent nothing usable:
    a bid/ask <= 0 is "no quote" (it cannot be traded at), a crossed bid/ask is a
    half-updated stale pair, and the -2 "not computed" sentinel ib_insync passes
    through for theta / vega is dropped."""
    bid, ask = _pos(getattr(t, "bid", None)), _pos(getattr(t, "ask", None))
    if bid is not None and ask is not None and bid > ask:
        bid = ask = None
    g = getattr(t, "modelGreeks", None)
    iv = delta = gamma = theta = vega = und = None
    if g is not None:
        iv = _pos(getattr(g, "impliedVol", None))
        delta = _num(getattr(g, "delta", None))
        if delta is not None and not -1.0 <= delta <= 1.0:
            delta = None
        gamma = _num(getattr(g, "gamma", None))
        if gamma is not None and gamma < 0:
            gamma = None
        vega = _num(getattr(g, "vega", None))
        if vega is not None and vega < 0:
            vega = None
        theta = _num(getattr(g, "theta", None))
        if theta == -2.0:
            theta = None
        und = _pos(getattr(g, "undPrice", None))
    return {
        "expiry": expiry, "right": right, "strike": float(strike),
        "bid": bid, "ask": ask,
        "mid": round((bid + ask) / 2.0, 4) if (bid is not None and ask is not None) else None,
        # the last trade, else the previous close (the 1.x bridge's rule)
        "last": _pos(getattr(t, "last", None)) or _pos(getattr(t, "close", None)),
        "bid_size": _count(getattr(t, "bidSize", None)) if bid is not None else None,
        "ask_size": _count(getattr(t, "askSize", None)) if ask is not None else None,
        "volume": _count(getattr(t, "volume", None)),
        "oi": _open_interest(t, right),
        "iv": _rnd(iv, 4), "delta": _rnd(delta, 4), "gamma": _rnd(gamma, 5),
        "theta": _rnd(theta, 4), "vega": _rnd(vega, 4), "und_price": _rnd(und, 4),
    }


def _has_data(r: dict) -> bool:
    """A row worth sharing: some price or a delta. Rows IBKR sent nothing for are
    left out so a dead feed never overwrites good shared data with blanks."""
    return any(r[k] is not None for k in ("bid", "ask", "last", "delta"))


def _market_price(t):
    try:
        mp = t.marketPrice()
    except Exception:  # noqa: BLE001
        mp = None
    if _pos(mp) is not None:
        return _pos(mp)
    bid, ask = _pos(getattr(t, "bid", None)), _pos(getattr(t, "ask", None))
    if bid is not None and ask is not None and bid <= ask:
        return (bid + ask) / 2.0
    return None


# ---------------------------------------------------------- contracts
async def _stock(ib, symbol):
    from ib_insync import Stock

    sym = _ib_symbol(symbol)
    if not sym:
        raise RuntimeError("A symbol is required.")
    await _pace(1)
    q = await ib.qualifyContractsAsync(Stock(sym, "SMART", "USD"))
    q = [c for c in (q or []) if c is not None and getattr(c, "conId", 0)]
    if not q:
        raise RuntimeError(f"IBKR does not recognise the symbol {str(symbol).upper()}.")
    return q[0]


def _pick_chain(rows, ib_sym: str):
    """The SMART row, preferring the standard class (trading class = symbol,
    multiplier 100) over adjusted ones, then the one listing the most expiries."""
    def key(p):
        mult = str(getattr(p, "multiplier", "") or "")
        return (getattr(p, "exchange", "") == "SMART",
                (getattr(p, "tradingClass", "") or "") == ib_sym,
                mult in ("100", ""),
                len(getattr(p, "expirations", None) or ()))
    return max(rows, key=key)


async def chain_defs(ib, symbol) -> dict:
    """What IBKR lists for the symbol: expiries (ascending, ISO) and the strike UNION
    over all of them (most strikes do not exist on any one expiry - ``quote`` drops
    those at qualification)."""
    stock = await _stock(ib, symbol)
    await _pace(1)
    params = await ib.reqSecDefOptParamsAsync(stock.symbol, "", "STK", stock.conId)
    rows = list(params or [])
    if not rows:
        raise RuntimeError(f"IBKR returned no option chain for {str(symbol).upper()}.")
    best = _pick_chain(rows, stock.symbol)
    expiries = sorted({d for d in (_to_date(e) for e in (best.expirations or ())) if d})
    strikes = sorted({k for k in (_pos(s) for s in (best.strikes or ())) if k is not None})
    try:
        mult = int(float(best.multiplier or 100))
    except (TypeError, ValueError):
        mult = 100
    return {"symbol": str(symbol).strip().upper(), "con_id": int(stock.conId),
            "exchange": best.exchange or "SMART", "trading_class": best.tradingClass or "",
            "expiries": [d.isoformat() for d in expiries], "strikes": strikes,
            "multiplier": mult}


def plan(defs, *, spot, iv_hint=None, today=None, max_weekly_dte=63, max_dte=1100,
         sigma_k=2.5, min_side=6, max_side=40, expiries=None, max_expiries=None) -> list:
    """The fetch window, pure: ``[{"expiry", "dte", "strikes", ...}]`` ascending.

    Expiries: every listed expiry with 0 <= DTE <= ``max_weekly_dte``, plus the
    monthlies (``is_monthly``) up to ``max_dte``; an explicit ``expiries`` list
    replaces that choice (kept only if listed and not past). ``max_expiries`` (> 0)
    keeps only the nearest that many of the chosen expiries when no explicit list is
    given - a member's connector reads a chain in chunks that fit its time limit.

    Strikes per expiry: the listed strikes within spot +/- sigma_k x spot x iv x
    sqrt(dte / 365) - one expected move scaled by the stock's own IV, so the window
    is ticker-relative (CLAUDE.md) - with at least ``min_side`` and at most
    ``max_side`` strikes on each side of the spot (nearest first). ``iv_hint`` is a
    FRACTION (a value above 5 is read as a percent); missing -> DEFAULT_IV. A DTE of
    0 is widened as if 1 so the window never collapses to nothing.

    Each entry also carries ``trading_class`` / ``multiplier`` when the defs have
    them, so ``quote`` builds unambiguous contracts.
    """
    s = _pos(spot)
    if s is None:
        raise ValueError("plan() needs a positive spot price.")
    day = _to_date(today) or et_today()
    iv = _pos(iv_hint)
    if iv is None:
        iv = DEFAULT_IV
    elif iv > 5.0:
        iv = iv / 100.0
    k = max(0.0, float(sigma_k))
    lo_n = max(0, int(min_side))
    hi_n = max(lo_n, int(max_side))
    listed = sorted({d for d in (_to_date(e) for e in (defs.get("expiries") or ())) if d})
    listed_set = set(listed)
    strikes = sorted({x for x in (_pos(v) for v in (defs.get("strikes") or ())) if x is not None})

    if expiries:
        want = {d for d in (_to_date(e) for e in expiries) if d}
        chosen = sorted(d for d in want if d >= day and (not listed_set or d in listed_set))
    else:
        wk, mx = int(max_weekly_dte), int(max_dte)
        chosen = [d for d in listed
                  if 0 <= (d - day).days and ((d - day).days <= wk
                                              or ((d - day).days <= mx and is_monthly(d, listed_set)))]
        try:
            cap = int(max_expiries) if max_expiries is not None else 0
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            chosen = chosen[:cap]                  # ascending: the nearest first

    below = [x for x in strikes if x < s]       # ascending: nearest is last
    above = [x for x in strikes if x >= s]      # ascending: nearest is first
    extra = {}
    if defs.get("trading_class"):
        extra["trading_class"] = defs["trading_class"]
    if defs.get("multiplier"):
        extra["multiplier"] = int(defs["multiplier"])

    out = []
    for d in chosen:
        dte = (d - day).days
        half = k * s * iv * math.sqrt(max(dte, 1) / 365.0)
        n_below = min(len(below), max(lo_n, min(hi_n, sum(1 for x in below if x >= s - half))))
        n_above = min(len(above), max(lo_n, min(hi_n, sum(1 for x in above if x <= s + half))))
        ks = (below[-n_below:] if n_below else []) + above[:n_above]
        if ks:
            out.append({"expiry": d.isoformat(), "dte": dte, "strikes": ks, **extra})
    return out


_SPEC_KEYS = {"iv_hint": float, "max_weekly_dte": int, "max_dte": int, "sigma_k": float,
              "min_side": int, "max_side": int, "expiries": None, "max_expiries": int,
              "today": None}


def plan_spec(defs, spec, *, spot=None, today=None) -> list:
    """``plan`` from a fetch-window spec (design section 3.2, possibly straight from
    JSON: strings, a "symbol" key, nulls). ``spot`` / ``today`` override the spec's."""
    spec = dict(spec or {})
    kw = {}
    for key, conv in _SPEC_KEYS.items():
        v = spec.get(key)
        if v is None or v == "" or v == []:
            continue
        kw[key] = conv(v) if conv else v
    if today is not None:
        kw["today"] = today
    return plan(defs, spot=spot if spot is not None else spec.get("spot"), **kw)


async def _contracts(ib, symbol: str, window, *, until=None):
    """Qualified Option contracts for the window as ``[(contract, expiry ISO, right,
    strike)]``, the number planned, the number IBKR does not know, and whether the
    deadline ``until`` (an absolute ``_now()`` time) stopped the qualification early
    (the contracts not reached yet are simply not quoted this time).

    Qualified contracts are cached for the life of the process: the conId never
    changes, a cycle re-reads the same contracts every hour, and re-using the same
    contract OBJECT keeps ib_insync's per-object Ticker table from growing without
    bound. Unknown strikes are remembered for NEG_TTL.
    """
    from ib_insync import Option

    today = et_today()
    if _QCACHE_DAY[0] != today:              # drop contracts that have expired
        cut = today.strftime("%Y%m%d")
        for key in [k for k in _QCACHE if k[1] < cut]:
            del _QCACHE[key]
        _QCACHE_DAY[0] = today

    ib_sym = _ib_symbol(symbol)
    keys, seen = [], set()
    for w in window or ():
        d = _to_date(w.get("expiry"))
        if d is None:
            continue
        ymd = d.strftime("%Y%m%d")
        tc = str(w.get("trading_class") or "")
        mult = str(w.get("multiplier") or 100)
        rights = [("C" if str(r).upper().startswith("C") else "P")
                  for r in (w.get("rights") or ("C", "P"))]
        for k in w.get("strikes") or ():
            kf = _pos(k)
            if kf is None:
                continue
            for r in dict.fromkeys(rights):
                key = (ib_sym, ymd, kf, r, tc)
                if key not in seen:
                    seen.add(key)
                    keys.append((key, d.isoformat(), mult))

    known, todo, unknown = {}, [], 0
    now = _now()
    for key, iso, mult in keys:
        hit = _QCACHE.get(key)
        if hit is not None and (hit[0] is not None or now - hit[1] < NEG_TTL):
            if hit[0] is None:
                unknown += 1
            else:
                known[key] = hit[0]
            continue
        _, ymd, kf, r, tc = key
        todo.append((key, Option(ib_sym, ymd, kf, r, "SMART", multiplier=mult,
                                 currency="USD", tradingClass=tc)))

    cut = False
    for i in range(0, len(todo), QUALIFY_BATCH):
        if _expired(until):
            cut = True
            break
        batch = todo[i:i + QUALIFY_BATCH]
        await _pace(len(batch))
        # qualifyContractsAsync fills conId IN PLACE and silently skips (logs) a
        # contract IBKR does not know; a connection error raises to the caller.
        await ib.qualifyContractsAsync(*[c for _, c in batch])
        stamp = _now()
        for key, c in batch:
            ok = bool(getattr(c, "conId", 0))
            _QCACHE[key] = (c if ok else None, stamp)
            if ok:
                known[key] = c
            else:
                unknown += 1

    out = [(known[key], iso, key[3], key[2]) for key, iso, _ in keys if key in known]
    # nearest expiry first (its near-the-money quotes make the market-data-type
    # probe reliable), a same-day expiry last (after its close it has no quotes)
    out.sort(key=lambda x: (x[1] == today.isoformat(), x[1], x[3], x[2]))
    return out, len(keys), unknown, cut


# -------------------------------------------------------------- spot
async def _spot_attempt(ib, stock) -> dict:
    _forget(ib, stock)
    await _pace(1)
    t = ib.reqMktData(stock, "", False, False)
    try:
        if t is None:
            t = ib.ticker(stock)
        if t is not None:
            _unmark(t)
        t0 = _now()
        price = None
        while True:
            await _sleep(SPOT_POLL)
            el = _now() - t0
            tk = t if t is not None else ib.ticker(stock)
            if tk is not None:
                price = _market_price(tk)
                if price is None and el >= SPOT_SETTLE:
                    price = _pos(getattr(tk, "last", None)) or _pos(getattr(tk, "close", None))
                if price is not None:
                    break
            if el >= SPOT_WAIT:
                break
        tk = t if t is not None else ib.ticker(stock)
        got = {"spot": _rnd(price, 4),
               "bid": _pos(getattr(tk, "bid", None)), "ask": _pos(getattr(tk, "ask", None)),
               "last": _pos(getattr(tk, "last", None)), "close": _pos(getattr(tk, "close", None)),
               "_obs": getattr(tk, "marketDataType", None)}
    finally:
        _release(ib, [stock])         # whatever happened: the line goes, uninterruptibly
    return got


async def _spot_locked(ib, symbol, mdt_pref) -> dict:
    stock = await _stock(ib, symbol)
    pref = _pref(mdt_pref)
    key = ("spot", pref[0])
    first = _sticky_get(key) or pref[0]
    for t in [first] + ([4] if first != 4 else []):   # delayed-frozen is free: the fallback
        await _set_type(ib, t)
        got = await _spot_attempt(ib, stock)
        if got["spot"] is not None:
            if t != first:
                _sticky_set(key, t)
            got["mdt"] = MDT_NAMES[_observed([got.pop("_obs")], t)]
            return got
    raise RuntimeError(f"No price for {str(symbol).upper()} from IBKR - no market data "
                       "permission for it, or the market is closed with no frozen data.")


async def spot(ib, symbol, *, mdt_pref=MDT_ORDER) -> dict:
    """The stock's price: ``{"spot", "bid", "ask", "last", "close", "mdt"}``.

    A streaming subscription read as soon as it has a price (a snapshot would sit
    up to 11 s waiting for a trade), cancelled after at most SPOT_WAIT. The last /
    close is accepted after SPOT_SETTLE. If the preferred market data type yields
    nothing, delayed-frozen (4, free) is tried once and remembered for MDT_RECHECK.
    Raises RuntimeError when IBKR gives no price at all.
    """
    async with _market_lock():
        return await _spot_locked(ib, symbol, mdt_pref)


# ------------------------------------------------------------- quote
def _wave_stats(rows) -> dict:
    st = {"priced": 0, "two_sided": 0, "greeks": 0}
    for r in rows:
        if r is None:
            continue
        two = r["bid"] is not None and r["ask"] is not None
        if two:
            st["two_sided"] += 1
        if two or r["last"] is not None:
            st["priced"] += 1
        if r["delta"] is not None:
            st["greeks"] += 1
    return st


def _yields(st: dict, n: int) -> bool:
    """A market data type "yields prices" when at least half the wave has a
    two-sided quote. A last / close alone does not count: after the close a live
    feed still shows those, while the frozen feed has the closing bid / ask."""
    return st["two_sided"] >= 1 and st["two_sided"] * 2 >= n


async def _await_fill(tickers, wait: float) -> None:
    """Return once the wave's data has arrived: every ticker priced AND with greeks;
    or (after QUOTE_MIN) all priced with greeks on half and nothing new for
    QUOTE_QUIET (deep OTM strikes never get a model); or nothing new at all for
    QUOTE_STALL (no entitlement); or ``wait`` seconds. "New" is a field becoming
    populated, not a tick - in market hours ticks never stop."""
    live = [t for t in tickers if t is not None]
    n = len(live)
    t0 = last_change = _now()
    seen = 0
    while True:
        await _sleep(QUOTE_POLL)
        now = _now()
        total = priced = greeks = 0
        for t in live:
            f, p, g = _state(t)
            total += f
            priced += p
            greeks += g
        if total != seen:
            seen, last_change = total, now
        el = now - t0
        if n == 0 or el >= wait:
            return
        if priced == n and greeks == n:
            return
        quiet = now - last_change
        if el >= QUOTE_MIN and ((priced == n and greeks * 2 >= n and quiet >= QUOTE_QUIET)
                                or quiet >= QUOTE_STALL):
            return


def _cancel(ib, contract) -> None:
    try:
        ib.cancelMktData(contract)
    except Exception:  # noqa: BLE001
        pass


def _release(ib, contracts) -> None:
    """Cancel these market-data subscriptions NOW: synchronous and unpaced, so no
    cancellation (a connector timeout, a ``wait_for``) can land half way and leave
    lines open - ib_insync forgets a leaked reqId once the contract is subscribed
    again, and the line then streams until the connection closes, eating the
    login's allowance that the member's own TWS windows share. The cancel messages
    are counted afterwards (``_Bucket.charge``): the next paced request waits for
    them, so the message rate still holds."""
    n = 0
    for c in contracts:
        _cancel(ib, c)
        n += 1
    _bucket.charge(n)


async def _window(ib, wave, wait: float, *, fresh: bool):
    """Subscribe one wave, wait for it, snapshot it into rows, release its lines.
    Returns (rows aligned with ``wave``, observed market data types). The release
    is in a ``finally`` and never awaits (``_release``)."""
    subscribed, tickers = [], []
    try:
        for c, _, _, _ in wave:
            if fresh:
                _forget(ib, c)
            await _pace(1)
            try:
                t = ib.reqMktData(c, OPT_TICKS, False, False)
                subscribed.append(c)
            except Exception:  # noqa: BLE001
                t = None
            if t is None:
                try:
                    t = ib.ticker(c)
                except Exception:  # noqa: BLE001
                    t = None
            if t is not None:
                _unmark(t)
            tickers.append(t)
        await _await_fill(tickers, wait)
        rows = [(_row(t, iso, r, k) if t is not None else None)
                for t, (_, iso, r, k) in zip(tickers, wave)]
        obs = [getattr(t, "marketDataType", None) for t, row in zip(tickers, rows)
               if row is not None and _has_data(row)]
    finally:
        _release(ib, subscribed)
    return rows, obs


async def _probe(ib, wave, order, wait: float, *, fresh: bool, end=None):
    """Quote the wave under each type in ``order`` until one yields prices. Later
    attempts keep what earlier ones delivered (fresh=False). Each window is cut to
    the deadline ``end``; past it no further type is tried. Returns (rows, obs,
    type, found)."""
    rows, obs, best = [None] * len(wave), [], None
    for i, t in enumerate(order):
        if i and _expired(end):
            break
        await _set_type(ib, t)
        rows, obs = await _window(ib, wave, _capped(wait, end), fresh=fresh and i == 0)
        st = _wave_stats(rows)
        if _yields(st, len(wave)):
            return rows, obs, t, True
        score = (st["two_sided"], st["priced"])
        if best is None or score > best[0]:
            best = (score, t)
    return rows, obs, (best[1] if best else (order[0] if order else 1)), False


def _merge_greeks(base, extra) -> None:
    """Take greeks from ``extra`` where ``base`` has none; base prices stay."""
    for a, b in zip(base, extra):
        if a is None or b is None or a["delta"] is not None or b["delta"] is None:
            continue
        for k in _GREEK_KEYS:
            if k != "und_price" or a[k] is None:
                a[k] = b[k]


async def quote(ib, symbol, window, *, max_lines=DEFAULT_MAX_LINES, wait=4.0,
                mdt_pref=MDT_ORDER, progress=None, spot=None, deadline=None) -> dict:
    """Quote every contract of a ``plan`` window.

    Returns ``{"symbol", "spot", "mdt", "rows", "requested", "filled", "ms",
    "partial"}`` plus ``planned`` / ``unknown`` (contracts IBKR does not list) /
    ``priced`` / ``with_greeks`` / ``waves`` / ``attempted`` (contracts actually
    subscribed) / ``mdt_id``. ``rows`` follow ``_row`` (iv FRACTION), sorted by
    expiry, right, strike; contracts IBKR sent nothing for are left out (``filled``
    < ``requested``).

    Contracts are ``OPT symbol expiry strike right SMART``, qualified in batches of
    QUALIFY_BATCH. They are subscribed (streaming, ``OPT_TICKS``) in waves of at most
    ``max_lines``; each wave waits for its data (``_await_fill``, ceiling ``wait``)
    and is cancelled before the next starts - the line allowance is shared by every
    client of the login.

    ``deadline`` (seconds from now, optional): once reached no new qualification
    batch and no new wave starts, the open wave's window is cut short and released,
    and the rows read so far come back with ``partial`` True - a long read returns
    what it has instead of being cancelled with nothing (the member's connector has
    a hard time limit per request). ``partial`` is always in the result.

    Market data type: the first in ``mdt_pref`` that yields prices on the first
    wave, remembered for MDT_RECHECK. A wave that gets no two-sided quote under that
    verdict re-probes once per call; a wave priced but with no greeks at all gets
    one extra window under the other family (live <-> delayed) whose greeks are
    merged in - its prices are not. ``mdt`` reports what TWS said it delivered.

    ``spot`` (optional) fills ``und_price`` where IBKR's model gave none and is
    returned as is; without it the result's spot is the median model underlying
    price, else a ``spot()`` read. ``progress(done, total)`` is called after each
    wave (errors in it are ignored).
    """
    t_start = _now()
    end = _until(deadline)
    sym = str(symbol or "").strip().upper()
    pref = _pref(mdt_pref)
    lines = max(1, int(max_lines or DEFAULT_MAX_LINES))
    wait = max(float(wait or 0.0), QUOTE_POLL)
    key = ("opt",) + pref

    async with _market_lock():
        contracts, planned, unknown, partial = await _contracts(ib, sym, window, until=end)
        waves = [contracts[i:i + lines] for i in range(0, len(contracts), lines)]
        verdict = _sticky_get(key)
        probes_left = 1
        alt_ok = True
        used, all_rows, all_obs = [], [], []
        done = 0
        for wave in waves:
            if _expired(end):
                partial = True                    # out of time: no new wave
                break
            ww = _capped(wait, end)
            if verdict is None:
                probes_left = 0
                rows, obs, verdict, found = await _probe(ib, wave, pref, wait, fresh=True, end=end)
                if found:
                    _sticky_set(key, verdict)
            else:
                await _set_type(ib, verdict)
                rows, obs = await _window(ib, wave, ww, fresh=True)
                st = _wave_stats(rows)
                if st["two_sided"] == 0 and probes_left and not _expired(end):
                    # nothing tradeable under the remembered type: the entitlement or
                    # the session changed (e.g. the close) - look again, once per call
                    probes_left = 0
                    rest = tuple(t for t in pref if t != verdict)
                    if rest:
                        rows2, obs2, v2, found = await _probe(ib, wave, rest, wait, fresh=False,
                                                              end=end)
                        if found:
                            rows, obs, verdict = rows2, obs2, v2
                            _sticky_set(key, verdict)
                        else:
                            rows = rows2          # the union of every attempt
                            await _set_type(ib, verdict)
            st = _wave_stats(rows)
            if st["priced"] and not st["greeks"] and alt_ok and not _expired(end):
                # a chain with NO deltas cannot be screened; out of hours TWS sometimes
                # sends model greeks under one family and not the other (COST,
                # 2026-09-19: 0 under one type, 11 under the other)
                alt = _alt_type(verdict, pref)
                if alt is not None:
                    await _set_type(ib, alt)
                    extra, _ = await _window(ib, wave, _capped(wait, end), fresh=False)
                    if not _wave_stats(extra)["greeks"]:
                        alt_ok = False            # the other family has none either
                    _merge_greeks(rows, extra)
                    await _set_type(ib, verdict)
            if ww < wait and _expired(end):
                partial = True                    # this wave's window was cut short
            used.append(verdict)
            all_rows.extend(r for r in rows if r is not None and _has_data(r))
            all_obs.extend(obs)
            done += len(wave)
            if progress is not None:
                try:
                    progress(done, len(contracts))
                except Exception:  # noqa: BLE001
                    pass

        spot_v = _pos(spot)
        if spot_v is None:
            unds = sorted(r["und_price"] for r in all_rows if r["und_price"] is not None)
            if unds:
                spot_v = unds[len(unds) // 2]
            elif all_rows and not _expired(end):
                try:
                    spot_v = (await _spot_locked(ib, sym, pref))["spot"]
                except Exception:  # noqa: BLE001
                    spot_v = None
        for r in all_rows:
            if r["und_price"] is None and spot_v is not None:
                r["und_price"] = round(spot_v, 4)

    all_rows.sort(key=lambda r: (r["expiry"], r["right"], r["strike"]))
    fallback = max(used) if used else (verdict or pref[0])
    label = _observed(all_obs, fallback)
    return {"symbol": sym, "spot": _rnd(spot_v, 4), "mdt": MDT_NAMES[label], "mdt_id": label,
            "rows": all_rows, "requested": len(contracts), "filled": len(all_rows),
            "ms": int((_now() - t_start) * 1000), "planned": planned, "unknown": unknown,
            "priced": _wave_stats(all_rows)["priced"],
            "with_greeks": sum(1 for r in all_rows if r["delta"] is not None),
            "waves": len(waves), "attempted": done, "partial": bool(partial)}


async def fetch(ib, symbol, spec=None, *, max_lines=DEFAULT_MAX_LINES, wait=4.0,
                mdt_pref=MDT_ORDER, progress=None, today=None, deadline=None) -> dict:
    """``chain_defs`` + ``spot`` + ``plan_spec`` + ``quote`` in one call - the read
    the collector and the connector's ``/chain2`` both do. The fresh spot plans the
    window (the spec's stored spot only if IBKR gives none) and is the result's
    ``spot``; ``spot_mdt`` / ``n_expiries`` are added. ``deadline`` (seconds from
    now) covers the whole read: ``quote`` gets what is left of it after the chain
    definition and the spot."""
    spec = dict(spec or {})
    end = _until(deadline)

    async def _spot_or_none():
        try:
            return await spot(ib, symbol, mdt_pref=mdt_pref)
        except RuntimeError:
            return None

    defs, s = await asyncio.gather(chain_defs(ib, symbol), _spot_or_none())
    px = (s or {}).get("spot") or _pos(spec.get("spot"))
    if px is None:
        raise RuntimeError(f"No price for {str(symbol).upper()} from IBKR and none stored.")
    window = plan_spec(defs, spec, spot=px, today=today)
    left = _left(end)
    out = await quote(ib, symbol, window, max_lines=max_lines, wait=wait,
                      mdt_pref=mdt_pref, progress=progress,
                      spot=(s or {}).get("spot"),
                      deadline=None if left is None else max(0.0, left))
    out["spot_mdt"] = (s or {}).get("mdt")
    out["n_expiries"] = len(window)
    return out


# -------------------------------------------------------- history
def _bar_day(b) -> str:
    """``YYYY-MM-DD`` of a daily bar: ib_insync hands a ``date`` with formatDate=1,
    a raw ``YYYYMMDD`` string has been seen on older builds - accept both."""
    d = getattr(b, "date", None)
    if hasattr(d, "isoformat"):
        return d.isoformat()[:10]
    day = _to_date(d)
    return day.isoformat() if day else ""


async def _historical(ib, symbol, duration: str, what: str):
    stock = await _stock(ib, symbol)
    await _hist_slot()
    await _pace(1)
    bars = await ib.reqHistoricalDataAsync(
        stock, endDateTime="", durationStr=duration, barSizeSetting="1 day",
        whatToShow=what, useRTH=True, formatDate=1)
    return list(bars or [])


async def daily_bars(ib, symbol, duration="2 Y") -> list:
    """Daily TRADES bars, regular hours: ``[{"on", "open", "high", "low", "close",
    "volume"}]`` oldest first (one per day; a bar without a close is skipped)."""
    by_day = {}
    for b in await _historical(ib, symbol, duration, "TRADES"):
        day, c = _bar_day(b), _pos(getattr(b, "close", None))
        if not day or c is None:
            continue
        v = _num(getattr(b, "volume", None))
        by_day[day] = {"on": day, "open": _pos(getattr(b, "open", None)),
                       "high": _pos(getattr(b, "high", None)),
                       "low": _pos(getattr(b, "low", None)), "close": c,
                       "volume": v if (v is not None and v >= 0) else None}
    return [by_day[d] for d in sorted(by_day)]


async def iv_history(ib, symbol, duration="1 Y") -> list:
    """IBKR's 30-day implied volatility index, daily: ``[{"on", "iv"}]`` with iv in
    PERCENT (OPTION_IMPLIED_VOLATILITY close x 100), oldest first, close > 0 only."""
    by_day = {}
    for b in await _historical(ib, symbol, duration, "OPTION_IMPLIED_VOLATILITY"):
        day, c = _bar_day(b), _pos(getattr(b, "close", None))
        if day and c is not None:
            by_day[day] = {"on": day, "iv": round(c * 100.0, 2)}
    return [by_day[d] for d in sorted(by_day)]


async def account(ib) -> dict:
    """``{"net_liquidation", "currency"}`` from the account summary (connector only);
    None values when the login reports none."""
    await _pace(1)
    for r in await ib.accountSummaryAsync() or ():
        if getattr(r, "tag", "") == "NetLiquidation":
            v = _num(getattr(r, "value", None))
            if v is not None:
                return {"net_liquidation": v, "currency": getattr(r, "currency", None) or None}
    return {"net_liquidation": None, "currency": None}


__all__ = ["VERSION", "MDT_NAMES", "MDT_IDS", "chain_defs", "plan", "plan_spec", "spot",
           "quote", "fetch", "daily_bars", "iv_history", "account", "et_today",
           "third_friday", "is_monthly", "reset_mdt", "clear_cache"]
