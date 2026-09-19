"""TradeHunter IBKR bridge — runs on YOUR PC, talks to YOUR TWS.

Why this exists
---------------
TradeHunter is a shared, server-hosted platform, but TWS runs on each member's
own machine under their own login. A server-side IBKR client cannot work: it
would need one account for everybody, expose TWS to the LAN, and size positions
against the wrong net liquidation. So the browser talks to a bridge on
``127.0.0.1`` instead, exactly like the TradingView bridge already does — the
server never touches anyone's broker session.

    browser (tradehunter.net)  ──fetch──>  127.0.0.1:9224  ──ib_insync──>  your TWS
                               ──POST───>  server: rule evaluation only

Browsers allow an HTTPS page to fetch ``http://127.0.0.1`` (loopback counts as a
trustworthy origin), which is what makes this work without exposing anything.

Security
--------
This process can read your account, so it does NOT serve every caller: requests
are answered only for allow-listed origins (``--origin`` to add more). Without
that, any website you happened to visit could read your positions and balances
off localhost. It is also strictly read-only — ``readonly=True`` on connect, no
order path anywhere in this file.

Run
---
    py -3.12 ibkr_bridge.py                 # defaults: TWS 127.0.0.1:7496
    py -3.12 ibkr_bridge.py --port 4002     # IB Gateway paper

``ib_insync`` needs Python <= 3.13 (eventkit calls asyncio.get_event_loop() at
import, removed in 3.14).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BRIDGE_PORT = 9224          # 9223 is the TradingView bridge
# Any HTTPS host in the platform's own domain, plus local dev servers. Matching
# the whole domain rather than one hostname is deliberate: the site is served
# from app.tradehunter.net, an exact-match list containing only the apex refused
# it, and the browser reports that refusal as an opaque "TypeError: Failed to
# fetch" — indistinguishable from the bridge being down.
ALLOWED_DOMAIN = "tradehunter.net"
DEFAULT_ALLOWED = [
    "http://localhost:8000", "http://127.0.0.1:8000",
    "http://localhost:8010", "http://127.0.0.1:8010",
    "http://localhost:8011", "http://127.0.0.1:8011",
]


def origin_allowed(origin: str) -> bool:
    """True for the platform's own HTTPS hosts and explicitly allowed origins."""
    if origin in CFG["allowed"]:
        return True
    try:
        u = urlparse(origin)
    except Exception:  # noqa: BLE001
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname.lower()
    return host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN)

CFG = {"host": "127.0.0.1", "port": 7496, "client_id": 86,
       "strike_window": 10, "quote_wait": 8.0, "allowed": list(DEFAULT_ALLOWED)}

_CACHE_TTL = 45.0
_cache: dict = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------- IB worker
class _Worker:
    """Owns the asyncio loop and the single IB client, off the HTTP threads."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ib = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="ib", daemon=True).start()
        self._ready.wait(timeout=10)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        from ib_insync import IB

        self.ib = IB()
        self._ready.set()
        self.loop.run_forever()

    def submit(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


_worker: _Worker | None = None


def worker() -> _Worker:
    global _worker
    if _worker is None:
        _worker = _Worker()
    return _worker


CLIENT_ID_TRIES = 6


async def _connect(ib):
    """Connect, stepping to a free clientId if the configured one is held.

    TWS keeps a client slot registered when a process dies without disconnecting
    — a force-kill, a crash, a closed laptop lid — and the next connection on
    that id then dies in the handshake with an EMPTY error message. Making the
    user restart TWS to clear that is a poor trade, so we simply walk to the next
    id and remember it. A genuinely unreachable TWS fails on the first try with a
    real message (connection refused), so this never masks that case.
    """
    if ib.isConnected():
        return

    first_detail = None
    base = CFG["client_id"]
    for n in range(CLIENT_ID_TRIES):
        cid = base + n
        try:
            await ib.connectAsync(CFG["host"], CFG["port"], clientId=cid,
                                  timeout=8, readonly=True)
            if n:
                print(f"  clientId {base} was held by another session; using {cid}")
                CFG["client_id"] = cid
            return
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).strip()
            if first_detail is None:
                first_detail = detail
            if detail:
                # A real error (refused, wrong port, TWS down) — no point walking
                # ids, the socket itself is the problem.
                raise RuntimeError(
                    f"Cannot reach TWS on {CFG['host']}:{CFG['port']}. Start TWS, enable "
                    "File > Global Configuration > API > 'Enable ActiveX and Socket "
                    f"Clients', and check the socket port. Details: {detail}"
                ) from exc
            # empty message => handshake stalled; try the next id
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    raise RuntimeError(
        f"TWS answered on {CFG['host']}:{CFG['port']} but no clientId in "
        f"{base}-{base + CLIENT_ID_TRIES - 1} completed the handshake. Either TWS is "
        "showing an 'Accept incoming connection attempt?' prompt (check the TWS "
        "window), or those ids are all held by dead sessions — restarting TWS "
        "releases them."
    )


def _fmt(ymd: str) -> str:
    try:
        return _dt.datetime.strptime(ymd, "%Y%m%d").date().isoformat()
    except Exception:  # noqa: BLE001
        return ymd


def _dte(ymd: str):
    try:
        return (_dt.datetime.strptime(ymd, "%Y%m%d").date() - _dt.date.today()).days
    except Exception:  # noqa: BLE001
        return None


_mkt_type: int | None = None
_mkt_type_at = 0.0          # when that verdict was reached (time.monotonic)
MKT_RECHECK = 900.0         # a "delayed" verdict is re-tested after this many seconds


QUOTE_POLL = 0.25       # how often the quote window looks at what has arrived
QUOTE_MIN = 1.5         # never return sooner than this
QUOTE_QUIET = 1.0       # priced + greeks in, and nothing new for this long = done
QUOTE_STALL = 3.0       # nothing new AT ALL for this long = done, complete or not
_NAN = float("nan")


def _filled(t) -> tuple[int, bool, bool]:
    """(populated fields, has a price, has greeks) for one ticker."""
    def ok(v):
        return v is not None and v == v and v > 0
    priced = (ok(t.bid) and ok(t.ask)) or ok(t.last) or ok(t.close)
    greeks = bool(t.modelGreeks and t.modelGreeks.delta is not None)
    n = sum(1 for v in (t.bid, t.ask, t.last, t.close) if ok(v))
    n += 1 if greeks else 0
    n += sum(1 for v in (t.putOpenInterest, t.callOpenInterest, t.volume)
             if v is not None and v == v)
    return n, priced, greeks


def _forget(ib, contract) -> None:
    """Blank what an EARLIER subscription left on this contract's ticker.

    ib_insync keeps one Ticker per contract for the life of the connection, and
    cancelMktData does not clear it. Reading "has the data arrived yet?" off a
    ticker that still holds last time's numbers answers yes before TWS has said
    anything: a second quote window closed at once with the first one's partial
    greeks, and a spot price could be hours old on a bridge left running all day.
    """
    t = ib.ticker(contract)
    if t is None:
        return
    for name in ("bid", "ask", "last", "close", "volume", "putOpenInterest",
                 "callOpenInterest", "bidSize", "askSize", "lastSize"):
        try:
            setattr(t, name, _NAN)
        except Exception:  # noqa: BLE001
            pass
    t.modelGreeks = t.bidGreeks = t.askGreeks = t.lastGreeks = None
    t.time = None


async def _quote(ib, contracts, fresh=True):
    """Subscribe all, wait until the data has ARRIVED, read it, release the lines.

    ``fresh`` blanks whatever an earlier subscription left on these tickers first
    (see ``_forget``). False only for the second half of the entitlement probe.

    The wait used to be a fixed ``quote_wait`` (8 s) sleep. Measured against a live
    TWS (2026-09-19, 22 JPM puts): every field that was ever going to arrive had
    arrived by ~3.6 s (open interest ~1.0 s, greeks ~1.5-2.0 s, prices ~3.1-3.6 s
    out of hours) and then nothing changed, so 4+ s of each chain was spent asleep.
    Now the window closes once the picture is COMPLETE and has stopped changing:
    every contract priced, greeks in on at least half of them (deep OTM strikes
    never get a model, so "all" would never come true), and no new field for
    QUOTE_QUIET. Both halves are required - greeks arrive before prices out of
    hours and after them in hours, and either alone closes the window on a chain
    that cannot be graded. A feed that sends nothing at all (no entitlement for
    this market data type) ends after QUOTE_STALL instead of the full wait. "New"
    means a FIELD becoming populated, not a tick: in market hours bid/ask ticks
    never stop, but the set of filled fields saturates. ``quote_wait`` remains the
    ceiling, so a slow feed still gets its full 8 s.

    reqTickersAsync is not used: it waits for EVERY contract's snapshot to end, so
    on a delayed feed it always burns its full timeout.

    Generic tick 101 = option OPEN INTEREST (1.4). It is not part of the default
    tick set, so without asking for it every leg's OI came back empty and the
    liquidity check could only look at the bid/ask. Streaming request (not a
    snapshot) on purpose: IBKR refuses generic ticks on snapshots. Day VOLUME
    needs nothing extra - it is in the default set.
    """
    for c in contracts:
        try:
            if fresh:
                _forget(ib, c)
            ib.reqMktData(c, "101" if getattr(c, "secType", "") == "OPT" else "", False, False)
        except Exception:  # noqa: BLE001
            pass
    t0 = last_change = time.monotonic()
    seen = 0
    while True:
        await asyncio.sleep(QUOTE_POLL)
        now = time.monotonic()
        total = n_priced = n_greeks = 0
        for c in contracts:
            try:
                n, priced, greeks = _filled(ib.ticker(c))
            except Exception:  # noqa: BLE001
                n, priced, greeks = 0, False, False
            total += n
            n_priced += priced
            n_greeks += greeks
        if total != seen:
            seen, last_change = total, now
        quiet = now - last_change
        if now - t0 >= CFG["quote_wait"]:
            break
        complete = (bool(contracts) and n_priced == len(contracts)
                    and n_greeks * 2 >= len(contracts))
        if now - t0 >= QUOTE_MIN and ((complete and quiet >= QUOTE_QUIET)
                                      or quiet >= QUOTE_STALL):
            break
    out = []
    for c in contracts:
        try:
            out.append(ib.ticker(c))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                ib.cancelMktData(c)
            except Exception:  # noqa: BLE001
                pass
    return [t for t in out if t is not None]


def _count(v):
    """A tick count (open interest, volume) or None. ib_insync leaves a tick that
    never arrived as NaN; 0 is a real answer ("nobody holds this strike")."""
    try:
        return int(v) if (v is not None and v == v and v >= 0) else None
    except (TypeError, ValueError):
        return None


def _open_interest(t, right):
    """IBKR reports a contract's OI on the tick for ITS side: 27 (call) / 28 (put)."""
    first, second = ((t.putOpenInterest, t.callOpenInterest) if right.startswith("P")
                     else (t.callOpenInterest, t.putOpenInterest))
    oi = _count(first)
    return oi if oi is not None else _count(second)


def _row(t, right):
    c, g = t.contract, t.modelGreeks
    bid = t.bid if t.bid and t.bid > 0 else None
    ask = t.ask if t.ask and t.ask > 0 else None
    mid = round((bid + ask) / 2, 4) if (bid is not None and ask is not None) else None
    last = t.last if t.last and t.last > 0 else (t.close if t.close and t.close > 0 else None)
    return {
        "right": right, "strike": float(c.strike), "bid": bid, "ask": ask,
        "mid": mid, "last": last,
        "spread_pct": (round((ask - bid) / mid * 100, 1)
                       if (bid is not None and ask is not None and mid) else None),
        "iv": round(g.impliedVol * 100, 1) if (g and g.impliedVol) else None,
        "delta": round(g.delta, 3) if (g and g.delta is not None) else None,
        "gamma": round(g.gamma, 4) if (g and g.gamma is not None) else None,
        "theta": round(g.theta, 3) if (g and g.theta is not None) else None,
        "vega": round(g.vega, 3) if (g and g.vega is not None) else None,
        "oi": _open_interest(t, right),
        "volume": _count(t.volume),
    }


SPOT_WAIT = 6.0         # longest wait for any price. Out of hours the close lands at
                        # 3.1-3.6 s (measured); 3.0 was tried first and failed HD / XOM
SPOT_SETTLE = 0.8       # after this, yesterday's close is good enough to pick strikes


async def _spot(ib, stock):
    """The underlying's price, as fast as TWS can give it.

    This was ``reqTickersAsync`` - a SNAPSHOT, which IBKR holds open until the
    snapshot "ends": up to 11 seconds whenever no fresh trade arrives (every
    weekend, every evening, and on thin names in hours). Measured 11.1 s on JPM
    with the close sitting in the ticker after 0.3 s. The spot only anchors which
    strikes to quote, so a streaming subscription read as soon as it has a number
    is enough: a live price the moment one exists, otherwise the close once the
    feed has had SPOT_SETTLE to show it has nothing better. If the current market
    data type yields nothing at all (no live subscription for this stock), the free
    delayed-frozen type is tried once before giving up.
    """
    def ok(v):
        return v is not None and v == v and v > 0

    async def attempt():
        _forget(ib, stock)
        ib.reqMktData(stock, "", False, False)
        t0 = time.monotonic()
        try:
            while True:
                await asyncio.sleep(0.15)
                el = time.monotonic() - t0
                t = ib.ticker(stock)
                if t is not None:
                    mp = t.marketPrice()
                    if ok(mp):
                        return float(mp)
                    if el >= SPOT_SETTLE:
                        for v in (t.last, t.close):
                            if ok(v):
                                return float(v)
                if el >= SPOT_WAIT:
                    return None
        finally:
            try:
                ib.cancelMktData(stock)
            except Exception:  # noqa: BLE001
                pass

    price = await attempt()
    if price is None and _mkt_type != 4:
        ib.reqMarketDataType(4)         # _chain sets the type it wants again before quoting
        price = await attempt()
    return price


async def _chain_def(ib, symbol):
    from ib_insync import Stock

    q = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if not q:
        raise RuntimeError(f"IBKR does not recognise the symbol {symbol}.")
    stock = q[0]
    # The price and the chain definition need nothing from each other - ask together.
    spot, params = await asyncio.gather(
        _spot(ib, stock),
        ib.reqSecDefOptParamsAsync(stock.symbol, "", "STK", stock.conId))
    if not spot:
        raise RuntimeError(f"No price for {symbol} — market closed with no frozen data.")
    chains = [p for p in params if p.exchange == "SMART"] or list(params)
    if not chains:
        raise RuntimeError(f"IBKR returned no option chain for {symbol}.")
    return stock, float(spot), chains[0]


PUT_SIDE_BELOW = 26     # puts-only mode: strikes quoted below spot ...
PUT_SIDE_ABOVE = 2      # ... and above it
PUT_SIDE_FLOOR = 0.72   # never probe further than 28% under the price


async def _chain(symbol, expiry=None, dte_min=None, dte_max=None, put_side=False):
    """One expiry of the chain.

    ``put_side`` is the bull put spread view (1.3): PUTS only, from just above
    the price down. The symmetric window (10 strikes each way) is right for
    reading a chain but wrong for finding a short put at delta 0.20-0.25 - on a
    $700 stock with $5 strikes it stops 7% under the price, well short of where
    that delta sits at 45-60 days. Dropping the calls pays for the longer reach:
    28 puts cost fewer market-data lines than 21 calls + 21 puts.
    """
    global _mkt_type
    from ib_insync import Option

    w = worker()
    await _connect(w.ib)
    ib = w.ib
    _, spot, chain = await _chain_def(ib, symbol)

    exps = sorted(chain.expirations)
    if not exps:
        raise RuntimeError(f"No listed expirations for {symbol}.")
    if expiry and expiry in exps:
        chosen = expiry
    elif dte_min is not None:
        dated = [(e, _dte(e)) for e in exps if _dte(e) is not None]
        inside = [e for e in dated if dte_min <= e[1] <= dte_max]
        pool = inside or dated
        chosen = min(pool, key=lambda e: abs(e[1] - (dte_min + dte_max) / 2))[0] if pool else exps[0]
    else:
        chosen = exps[0]

    # reqSecDefOptParams returns the UNION of strikes across all expirations, so
    # most do not exist on a given monthly. Qualify a generous slice first (free,
    # no market data), then quote only strikes that really exist — otherwise the
    # chain comes back starved and the closest-to-target-delta strike changes.
    strikes = sorted(chain.strikes)
    near = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    win = CFG["strike_window"]
    if put_side:
        lo = spot * PUT_SIDE_FLOOR
        probe = [k for k in strikes[:near + PUT_SIDE_ABOVE * 3 + 1] if k >= lo][-90:]
        rights = ("P",)
    else:
        probe = strikes[max(0, near - win * 3): near + win * 3 + 1]
        rights = ("C", "P")
    cand = [Option(symbol, chosen, k, r, "SMART", tradingClass=chain.tradingClass)
            for k in probe for r in rights]
    qualified = await ib.qualifyContractsAsync(*cand)
    real = sorted({c.strike for c in qualified if getattr(c, "conId", None)})
    if not real:
        raise RuntimeError(f"No {symbol} {_fmt(chosen)} contracts qualified near spot.")
    j = min(range(len(real)), key=lambda i: abs(real[i] - spot))
    if put_side:
        keep = set(real[max(0, j - PUT_SIDE_BELOW): j + PUT_SIDE_ABOVE + 1])
    else:
        keep = set(real[max(0, j - win): j + win + 1])
    contracts = [c for c in qualified if getattr(c, "conId", None) and c.strike in keep]

    # Sticky entitlement probe: one modelGreeks in forty is noise, not a live feed.
    # A "delayed" verdict EXPIRES (1.5): it used to last for the life of the process,
    # so a bridge started on a Saturday - when even a fully entitled account gets few
    # model greeks - kept serving 15-minute-old quotes through Monday's session.
    global _mkt_type_at
    greeks_from_delayed = False
    if _mkt_type == 4 and time.monotonic() - _mkt_type_at > MKT_RECHECK:
        _mkt_type = None
    if _mkt_type is None:
        ib.reqMarketDataType(1)
        tickers = await _quote(ib, contracts)
        got = sum(1 for x in tickers if x.modelGreeks)
        if tickers and got >= max(2, len(tickers) // 2):
            _mkt_type = 1
        else:
            _mkt_type = 4       # delayed-frozen; IBKR provides it free
            ib.reqMarketDataType(_mkt_type)
            # fresh=False: keep what the live attempt did deliver. TWS does not
            # resend model greeks to a re-subscription seconds after the first, so
            # wiping them here returned a chain with prices and NO deltas.
            tickers = await _quote(ib, contracts, fresh=False)
        _mkt_type_at = time.monotonic()
    else:
        ib.reqMarketDataType(_mkt_type)
        tickers = await _quote(ib, contracts)
        # A chain with NO deltas cannot be graded at all (the short leg is chosen by
        # delta). Out of hours TWS sometimes sends no model greeks for a large chain
        # under one market data type and does under the other (COST, 29 puts,
        # 2026-09-19: 0 under one, 11 under the other, and not always the same one).
        # One extra window with the OTHER type, only in that case; prices already
        # read are kept and the sticky verdict does not change.
        if tickers and not any(x.modelGreeks for x in tickers):
            ib.reqMarketDataType(4 if _mkt_type == 1 else 1)
            tickers = await _quote(ib, contracts, fresh=False)
            greeks_from_delayed = _mkt_type == 1 and any(x.modelGreeks for x in tickers)
            ib.reqMarketDataType(_mkt_type)   # the next chain's spot reads the right feed

    calls, puts = [], []
    for t in tickers:
        r = getattr(t.contract, "right", "")
        (calls if r.startswith("C") else puts).append(_row(t, r))
    calls.sort(key=lambda r: r["strike"])
    puts.sort(key=lambda r: r["strike"])
    atm_src = calls or puts
    atm_iv = min(atm_src, key=lambda r: abs(r["strike"] - spot)).get("iv") if atm_src else None

    return {
        "ok": True, "symbol": symbol, "spot": round(spot, 2),
        "expiry": chosen, "expiry_label": _fmt(chosen), "dte": _dte(chosen),
        "expirations": [{"value": e, "label": _fmt(e), "dte": _dte(e)} for e in exps[:24]],
        "calls": calls, "puts": puts, "atm_iv": atm_iv,
        "data_mode": "live" if _mkt_type == 1 else "delayed",
        # true = the live feed sent no model greeks, so deltas came from the delayed one
        "greeks_from_delayed": greeks_from_delayed,
        "greeks_ok": any(c.get("delta") is not None for c in calls + puts),
        # did TWS send open interest at all? False = a feed that carries no OI, which
        # the server reports as "unknown" rather than grading every leg as empty
        "oi_ok": any(c.get("oi") is not None for c in calls + puts),
        "bridge": Handler.server_version.split("/")[-1],
        "strike_window": win, "put_side": bool(put_side),
        "source": f"TWS {CFG['host']}:{CFG['port']}",
    }


async def _iv(symbol):
    from ib_insync import Stock

    w = worker()
    await _connect(w.ib)
    ib = w.ib
    q = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if not q:
        raise RuntimeError(f"IBKR does not recognise {symbol}.")
    bars = await ib.reqHistoricalDataAsync(
        q[0], endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
        whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True, formatDate=1)
    vals = [b.close for b in (bars or []) if b.close and b.close > 0]
    if len(vals) < 30:
        return {"ok": True, "iv_percentile": None, "iv_rank": None,
                "iv_current": None, "n": len(vals),
                "note": "Not enough IV history from IBKR to compute a percentile."}
    cur, lo, hi = vals[-1], min(vals), max(vals)
    return {"ok": True, "iv_current": round(cur * 100, 1),
            "iv_percentile": round(sum(1 for v in vals if v < cur) / len(vals) * 100, 1),
            "iv_rank": round((cur - lo) / (hi - lo) * 100, 1) if hi > lo else None,
            "iv_low": round(lo * 100, 1), "iv_high": round(hi * 100, 1),
            "n": len(vals), "note": ""}


async def _scan(iv_rank: float, price: float, volume: float, rows: int = 50):
    """The member's TWS "High IV Rank" scanner through the API.

    ``SCAN_ivRank52w_DESC`` is the API name of TWS's "52 Week IV Rank" sort, and
    ``ivRank52wAbove`` / ``priceAbove`` / ``volumeAbove`` are the scanner's own
    filter codes (read from reqScannerParameters, 2026-09-18). The rank filter is
    in PERCENT (30 = "greater than 30"), like the TWS field. The scanner returns
    contracts in rank order but not the rank figure itself - the page reads that
    per ticker from /iv.
    """
    from ib_insync import ScannerSubscription, TagValue

    w = worker()
    await _connect(w.ib)
    ib = w.ib
    sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR",
                              scanCode="SCAN_ivRank52w_DESC",
                              numberOfRows=max(1, min(int(rows), 50)))
    tags = [TagValue("ivRank52wAbove", "%g" % iv_rank)]
    if price > 0:
        tags.append(TagValue("priceAbove", "%g" % price))
    if volume > 0:
        tags.append(TagValue("volumeAbove", "%d" % int(volume)))
    data = await ib.reqScannerDataAsync(sub, [], tags)
    seen, symbols = set(), []
    for d in data or []:
        try:
            s = (d.contractDetails.contract.symbol or "").strip().upper().replace(" ", ".")
        except Exception:  # noqa: BLE001
            continue
        if s and s not in seen:
            seen.add(s)
            symbols.append(s)
    return {"ok": True, "symbols": symbols, "n": len(symbols),
            "scan_code": "SCAN_ivRank52w_DESC",
            "criteria": {"iv_rank": iv_rank, "price": price, "volume": volume}}


async def _account():
    w = worker()
    await _connect(w.ib)
    for r in await w.ib.accountSummaryAsync():
        if r.tag == "NetLiquidation":
            try:
                return {"ok": True, "net_liquidation": float(r.value),
                        "account": r.account}
            except (TypeError, ValueError):
                break
    return {"ok": True, "net_liquidation": None, "account": None}


def cached(key, fn, ttl=_CACHE_TTL):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time() + ttl, val)   # stamp at STORE time, not entry
    return val


# --------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    server_version = "TradeHunterIBKRBridge/1.5"   # 1.1 /scan; 1.2 one bridge per port; 1.3 put-side chain; 1.4 open interest per leg; 1.5 no fixed waits (spot + quote window)

    def _origin_ok(self):
        o = self.headers.get("Origin")
        # No Origin => a direct visit (curl, address bar), not a cross-site read.
        return (None, True) if o is None else (o, origin_allowed(o))

    def _send(self, code, payload, origin=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802  CORS preflight
        origin, ok = self._origin_ok()
        if not ok:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
            # Private Network Access (Chrome 104+). A page on a PUBLIC origin
            # (https://tradehunter.net) reaching a PRIVATE address (127.0.0.1) is
            # preflighted even for a simple GET, and the browser drops the request
            # unless this header comes back. Without it the tab reports "no bridge"
            # while the bridge is plainly running — and it only shows up from the
            # real site, because localhost -> localhost is private->private and
            # never triggers PNA at all.
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        origin, ok = self._origin_ok()
        if not ok:
            # An un-allow-listed page must not be able to read the account.
            self._send(403, {"ok": False, "error":
                             f"Origin {origin} is not allowed by this bridge."})
            return

        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        sym = (q.get("symbol") or "").strip().upper()

        try:
            if u.path == "/health":
                ib = worker().ib
                self._send(200, {"ok": True, "connected": bool(ib and ib.isConnected()),
                                 "tws": f"{CFG['host']}:{CFG['port']}",
                                 "client_id": CFG["client_id"],
                                 "version": self.server_version}, origin)
            elif u.path == "/chain":
                if not sym:
                    raise RuntimeError("symbol is required")
                exp = q.get("exp") or None
                dmin = int(q["dte_min"]) if q.get("dte_min") else None
                dmax = int(q["dte_max"]) if q.get("dte_max") else None
                side = q.get("side") == "put"      # 1.3: the bull put spread view
                key = ("chain", sym, exp or "", dmin, dmax, side)
                self._send(200, cached(key, lambda: worker().submit(
                    _chain(sym, exp, dmin, dmax, put_side=side), 120)), origin)
            elif u.path == "/iv":
                if not sym:
                    raise RuntimeError("symbol is required")
                self._send(200, cached(("iv", sym), lambda: worker().submit(
                    _iv(sym), 60), ttl=600), origin)
            elif u.path == "/scan":
                def _f(name, dflt):
                    try:
                        return max(0.0, float(q.get(name, dflt)))
                    except (TypeError, ValueError):
                        return float(dflt)
                ivr, px, vol = _f("iv_rank", 30), _f("price", 100), _f("volume", 200000)
                self._send(200, cached(("scan", ivr, px, vol), lambda: worker().submit(
                    _scan(ivr, px, vol), 60), ttl=120), origin)
            elif u.path == "/account":
                self._send(200, cached(("acct",), lambda: worker().submit(
                    _account(), 30), ttl=60), origin)
            else:
                self._send(404, {"ok": False, "error": "unknown endpoint"}, origin)
        except Exception as exc:  # noqa: BLE001
            self._send(200, {"ok": False, "error": str(exc) or type(exc).__name__}, origin)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


class _ExclusiveServer(ThreadingHTTPServer):
    """One bridge per port, enforced by the socket.

    ``HTTPServer`` sets SO_REUSEADDR, and on WINDOWS that lets a second process bind
    a port that is already being listened on - silently. Both "listen", the OLDEST
    one receives every request, and a freshly started bridge never sees traffic.
    That is how a 1.0 bridge from the morning kept answering after two restarts
    onto 1.1 (2026-09-18). Without the flag the second bind fails loudly instead.
    """
    allow_reuse_address = sys.platform != "win32"


def _retire_other_copies(port: int) -> None:
    """Starting the bridge means "run THIS bridge": stop any copy already on the port.

    Windows only (the launcher, the Startup shortcut and the web app's Start button
    are all Windows). Only processes that are LISTENING on our port AND whose
    command line names this script are touched; the whole launcher tree
    (cmd.exe -> py.exe -> python.exe) goes, so no stale "press any key" window is
    left behind. Best effort: any failure here falls through to the bind, which
    reports a held port plainly.
    """
    if sys.platform != "win32":
        return
    import os
    import subprocess

    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15).stdout
        listeners = set()
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 5 and f[0] == "TCP" and f[3] == "LISTENING" \
                    and f[1].endswith(":%d" % port) and f[4].isdigit():
                listeners.add(int(f[4]))
        listeners.discard(os.getpid())
        if not listeners:
            return
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
              "'*ibkr_bridge*' } | ForEach-Object { '{0},{1}' -f $_.ProcessId, $_.ParentProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
        parent = {}
        for line in out.splitlines():
            a, _, b = line.strip().partition(",")
            if a.isdigit() and b.isdigit():
                parent[int(a)] = int(b)
        mine = set()                     # this process and its own launcher chain
        p = os.getpid()
        while p in parent and p not in mine:
            mine.add(p)
            p = parent[p]
        mine.add(os.getpid())
        for pid in listeners:
            if pid not in parent:        # something else owns the port - not ours to stop
                continue
            top = pid
            while parent.get(top) in parent and parent[top] not in mine:
                top = parent[top]
            if top in mine:
                continue
            print(f"  an earlier bridge is still on port {port} (pid {pid}) - stopping it")
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(top)],
                           capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not check for an earlier bridge: {exc})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tws-host", default=CFG["host"])
    ap.add_argument("--port", type=int, default=CFG["port"], help="TWS socket port")
    ap.add_argument("--client-id", type=int, default=CFG["client_id"])
    ap.add_argument("--bridge-port", type=int, default=BRIDGE_PORT)
    ap.add_argument("--strike-window", type=int, default=CFG["strike_window"])
    ap.add_argument("--origin", action="append", default=[],
                    help="extra allowed browser origin (repeatable)")
    a = ap.parse_args()

    CFG.update(host=a.tws_host, port=a.port, client_id=a.client_id,
               strike_window=a.strike_window)
    CFG["allowed"] = list(DEFAULT_ALLOWED) + a.origin

    try:
        import ib_insync  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"ib_insync is not importable: {exc}\n"
              "  pip install ib_insync   (Python 3.12 — 3.14 removed the event loop\n"
              "  API that eventkit needs at import time)", file=sys.stderr)
        raise SystemExit(1)

    _retire_other_copies(a.bridge_port)
    srv = None
    for attempt in range(10):           # the retired copy's socket can take a moment to go
        try:
            srv = _ExclusiveServer(("127.0.0.1", a.bridge_port), Handler)
            break
        except OSError as exc:
            if attempt == 9:
                print(f"Port {a.bridge_port} is held by another program and could not be "
                      f"freed: {exc}\n  Close the other bridge window (or end its python.exe "
                      "in Task Manager), then start this again.", file=sys.stderr)
                raise SystemExit(1)
            time.sleep(0.5)
    print(f"TradeHunter IBKR bridge {Handler.server_version.split('/')[-1]} "
          f"on http://127.0.0.1:{a.bridge_port}")
    print(f"  -> TWS {CFG['host']}:{CFG['port']} (clientId {CFG['client_id']}, read-only)")
    print(f"  allowed: https://*.{ALLOWED_DOMAIN} + {', '.join(CFG['allowed'])}")
    print("  Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        w = _worker
        if w and w.ib and w.ib.isConnected():
            # hand the clientId back, or TWS keeps the slot and the next run hangs
            try:
                w.ib.disconnect()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
