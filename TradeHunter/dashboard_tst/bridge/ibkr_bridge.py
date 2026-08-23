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


async def _connect(ib):
    if ib.isConnected():
        return
    try:
        await ib.connectAsync(CFG["host"], CFG["port"], clientId=CFG["client_id"],
                              timeout=8, readonly=True)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip()
        if not detail:
            raise RuntimeError(
                f"TWS answered on {CFG['host']}:{CFG['port']} but the handshake never "
                f"completed. clientId {CFG['client_id']} may still be held by a previous "
                "session (restart TWS), or TWS is showing an 'Accept incoming connection' "
                "prompt."
            ) from exc
        raise RuntimeError(
            f"Cannot reach TWS on {CFG['host']}:{CFG['port']}. Start TWS, enable "
            "File > Global Configuration > API > 'Enable ActiveX and Socket Clients', "
            f"and check the socket port. Details: {detail}"
        ) from exc


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


async def _quote(ib, contracts):
    """Subscribe all, wait one window, read what arrived, release the lines.

    reqTickersAsync waits for EVERY contract, so on a delayed feed it always
    burns its full timeout; this keeps whatever answered within one window.
    """
    for c in contracts:
        try:
            ib.reqMktData(c, "", False, False)
        except Exception:  # noqa: BLE001
            pass
    await asyncio.sleep(CFG["quote_wait"])
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
        "oi": None,
        "volume": int(t.volume) if t.volume and t.volume > 0 else None,
    }


async def _chain_def(ib, symbol):
    from ib_insync import Stock

    q = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if not q:
        raise RuntimeError(f"IBKR does not recognise the symbol {symbol}.")
    stock = q[0]
    [stk] = await ib.reqTickersAsync(stock)
    spot = stk.marketPrice()
    if not spot or spot != spot:
        spot = stk.close
    if not spot:
        raise RuntimeError(f"No price for {symbol} — market closed with no frozen data.")
    params = await ib.reqSecDefOptParamsAsync(stock.symbol, "", "STK", stock.conId)
    chains = [p for p in params if p.exchange == "SMART"] or list(params)
    if not chains:
        raise RuntimeError(f"IBKR returned no option chain for {symbol}.")
    return stock, float(spot), chains[0]


async def _chain(symbol, expiry=None, dte_min=None, dte_max=None):
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
    probe = strikes[max(0, near - win * 3): near + win * 3 + 1]
    cand = [Option(symbol, chosen, k, r, "SMART", tradingClass=chain.tradingClass)
            for k in probe for r in ("C", "P")]
    qualified = await ib.qualifyContractsAsync(*cand)
    real = sorted({c.strike for c in qualified if getattr(c, "conId", None)})
    if not real:
        raise RuntimeError(f"No {symbol} {_fmt(chosen)} contracts qualified near spot.")
    j = min(range(len(real)), key=lambda i: abs(real[i] - spot))
    keep = set(real[max(0, j - win): j + win + 1])
    contracts = [c for c in qualified if getattr(c, "conId", None) and c.strike in keep]

    # Sticky entitlement probe: one modelGreeks in forty is noise, not a live feed.
    if _mkt_type is None:
        ib.reqMarketDataType(1)
        tickers = await _quote(ib, contracts)
        got = sum(1 for x in tickers if x.modelGreeks)
        if tickers and got >= max(2, len(tickers) // 2):
            _mkt_type = 1
        else:
            _mkt_type = 4       # delayed-frozen; IBKR provides it free
            ib.reqMarketDataType(_mkt_type)
            tickers = await _quote(ib, contracts)
    else:
        ib.reqMarketDataType(_mkt_type)
        tickers = await _quote(ib, contracts)

    calls, puts = [], []
    for t in tickers:
        r = getattr(t.contract, "right", "")
        (calls if r.startswith("C") else puts).append(_row(t, r))
    calls.sort(key=lambda r: r["strike"])
    puts.sort(key=lambda r: r["strike"])
    atm_iv = min(calls, key=lambda r: abs(r["strike"] - spot)).get("iv") if calls else None

    return {
        "ok": True, "symbol": symbol, "spot": round(spot, 2),
        "expiry": chosen, "expiry_label": _fmt(chosen), "dte": _dte(chosen),
        "expirations": [{"value": e, "label": _fmt(e), "dte": _dte(e)} for e in exps[:24]],
        "calls": calls, "puts": puts, "atm_iv": atm_iv,
        "data_mode": "live" if _mkt_type == 1 else "delayed",
        "greeks_ok": any(c.get("delta") is not None for c in calls + puts),
        "strike_window": win,
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
    server_version = "TradeHunterIBKRBridge/1.0"

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
                key = ("chain", sym, exp or "", dmin, dmax)
                self._send(200, cached(key, lambda: worker().submit(
                    _chain(sym, exp, dmin, dmax), 90)), origin)
            elif u.path == "/iv":
                if not sym:
                    raise RuntimeError("symbol is required")
                self._send(200, cached(("iv", sym), lambda: worker().submit(
                    _iv(sym), 60), ttl=600), origin)
            elif u.path == "/account":
                self._send(200, cached(("acct",), lambda: worker().submit(
                    _account(), 30), ttl=60), origin)
            else:
                self._send(404, {"ok": False, "error": "unknown endpoint"}, origin)
        except Exception as exc:  # noqa: BLE001
            self._send(200, {"ok": False, "error": str(exc) or type(exc).__name__}, origin)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


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

    srv = ThreadingHTTPServer(("127.0.0.1", a.bridge_port), Handler)
    print(f"TradeHunter IBKR bridge on http://127.0.0.1:{a.bridge_port}")
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
