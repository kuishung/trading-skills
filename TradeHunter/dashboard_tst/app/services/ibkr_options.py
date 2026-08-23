"""Option chains + Greeks from TWS / IB Gateway.

The user turns TWS on when they want options; this module must therefore treat
"not connected" as a NORMAL state, never an error — every entry point returns a
structured result the template can render as a friendly "TWS is off" panel.

Why a dedicated thread
----------------------
``ib_insync`` owns an asyncio event loop and is not safe to drive from uvicorn's
loop (it binds handlers at construction and blocks on its own futures). So the
IB client lives in ONE background thread with its own loop, created once and
reused; route handlers submit coroutines to it with
``asyncio.run_coroutine_threadsafe`` and wait with a hard timeout. That keeps a
hung or absent TWS from ever blocking a web worker indefinitely.

Python version
--------------
``ib_insync`` imports ``eventkit``, which calls ``asyncio.get_event_loop()`` at
import time — removed in Python 3.14. The dashboard venv is 3.12, per the hard
rule in CLAUDE.md. The import here is LAZY so a machine without ib_insync (or on
3.14) still serves every other page; only the Options tab degrades.

Market-data notes
-----------------
- Greeks come from IBKR's own option model (``ticker.modelGreeks``) rather than
  a local Black-Scholes — IBKR computes them against the same surface it quotes.
- Each option contract costs a **market-data line**, and accounts are capped
  (commonly 100 concurrent). We therefore quote a WINDOW of strikes around spot,
  not the whole chain, and release the subscriptions afterwards.
- Without an OPRA subscription IBKR serves delayed data; we fall back to
  delayed-frozen (``reqMarketDataType(4)``) so the tab still populates, and the
  result says which mode produced the numbers.
"""
from __future__ import annotations

import asyncio
import atexit
import datetime as _dt
import os
import threading
import time

# ---- connection settings (app/.env, TST_ prefixed like everything else) ------
# clientId 86 is this app's slot. CLAUDE.md's allocation table already spends
# 71 (live bot), 80 (observer), 83/84 (ingest laptop/Hermes), 85 (health probe),
# 98/99 (probes) — IBKR refuses two sessions on the same id, so do not reuse one.
HOST = os.environ.get("TST_IBKR_HOST", "127.0.0.1")
# 7496 = TWS live socket (7497 TWS paper, 4001/4002 IB Gateway live/paper).
# Defaulting to the LIVE port is safe here and deliberate: this module connects
# readonly=True and has no order path at all, and the account's real NLV is what
# the 2%-of-net-liquidation sizing rule has to be computed against. TWS's own
# "Read-Only API" setting is the second guarantee. The trading bot's separate
# resources/ibkr_data.py keeps its paper-only guard — that one can place orders.
PORT = int(os.environ.get("TST_IBKR_PORT", "7496"))
CLIENT_ID = int(os.environ.get("TST_IBKR_CLIENT_ID", "86"))
CONNECT_TIMEOUT = float(os.environ.get("TST_IBKR_TIMEOUT", "6"))
STRIKE_WINDOW = int(os.environ.get("TST_IBKR_STRIKE_WINDOW", "10"))  # each side of spot
REQUEST_TIMEOUT = float(os.environ.get("TST_IBKR_REQ_TIMEOUT", "60"))
QUOTE_WAIT = float(os.environ.get("TST_IBKR_QUOTE_WAIT", "8"))  # one window for all strikes
# 1=live, 2=frozen, 3=delayed, 4=delayed-frozen. Leave unset to auto-detect:
# we try live once, and if IBKR answers without an option model (error 354 —
# "market data is not subscribed"), we drop to delayed-frozen and REMEMBER it
# for the process, so the slow probe happens once rather than on every request.
_MKT_TYPE_ENV = os.environ.get("TST_IBKR_MARKET_DATA_TYPE", "").strip()
_mkt_type: int | None = int(_MKT_TYPE_ENV) if _MKT_TYPE_ENV.isdigit() else None

_CACHE_TTL = float(os.environ.get("TST_IBKR_CACHE_TTL", "45"))
# Long enough to make tab switching and the positions refresh cheap, short
# enough that a quote you act on is not stale. Chains take ~30s to build.
_cache: dict = {}
_cache_lock = threading.Lock()


class OptionsUnavailable(Exception):
    """Raised inside the worker; callers convert it to a rendered notice."""


# ---------------------------------------------------------------- worker thread
class _Worker:
    """Owns the asyncio loop and the single IB client."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ib = None
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="ibkr-options", daemon=True)
        self.thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        from ib_insync import IB           # constructed inside its own loop

        self.ib = IB()
        self._ready.set()
        self.loop.run_forever()

    def submit(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise OptionsUnavailable(
                f"TWS did not answer within {timeout:.0f}s. It may be starting up, "
                "or the API request is queued behind a market-data limit."
            )


_worker: _Worker | None = None
_worker_lock = threading.Lock()


@atexit.register
def _disconnect() -> None:
    """Hand the clientId back to TWS on shutdown.

    TWS keeps a client slot registered when a process dies without disconnecting,
    and the NEXT connection on that id then fails with an empty, unhelpful error.
    A restarted uvicorn would hit exactly that, so the slot is released on exit.
    """
    w = _worker
    if w is None or w.ib is None:
        return
    try:
        if w.ib.isConnected():
            w.ib.disconnect()
    except Exception:  # noqa: BLE001
        pass


def _get_worker() -> _Worker:
    global _worker
    with _worker_lock:
        if _worker is None:
            try:
                _worker = _Worker()
            except Exception as exc:  # noqa: BLE001
                raise OptionsUnavailable(
                    f"Could not start the IBKR client thread: {exc}"
                ) from exc
        if _worker.ib is None:
            raise OptionsUnavailable(
                "ib_insync is not installed in this environment "
                "(pip install ib_insync, Python 3.12)."
            )
        return _worker


async def _ensure_connected(w: _Worker) -> None:
    if w.ib.isConnected():
        return
    try:
        await w.ib.connectAsync(
            HOST, PORT, clientId=CLIENT_ID, timeout=CONNECT_TIMEOUT, readonly=True
        )
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip()
        if not detail:
            # connectAsync timing out with nothing to say almost always means the
            # socket answered but the handshake never completed: the clientId is
            # still registered from a process that died, or TWS is showing its
            # "Accept incoming connection attempt?" prompt.
            hint = (f"TWS answered on {HOST}:{PORT} but the handshake never completed. "
                    f"Either clientId {CLIENT_ID} is still held by a previous session "
                    "(restart TWS to release it, or set TST_IBKR_CLIENT_ID to a free "
                    "number), or TWS is waiting on an 'Accept incoming connection' "
                    "prompt — check the TWS window.")
        else:
            hint = (f"No TWS/Gateway on {HOST}:{PORT} (clientId {CLIENT_ID}). Start TWS "
                    "and enable File > Global Configuration > API > 'Enable ActiveX and "
                    "Socket Clients', and check the socket port matches. "
                    f"Details: {detail}")
        raise OptionsUnavailable(hint) from exc


def _fmt_expiry(yyyymmdd: str) -> str:
    try:
        return _dt.datetime.strptime(yyyymmdd, "%Y%m%d").date().isoformat()
    except Exception:  # noqa: BLE001
        return yyyymmdd


def _dte(yyyymmdd: str) -> int | None:
    try:
        d = _dt.datetime.strptime(yyyymmdd, "%Y%m%d").date()
        return (d - _dt.date.today()).days
    except Exception:  # noqa: BLE001
        return None


def _row(ticker, right: str) -> dict:
    """One strike's quote + Greeks, flattened for the template."""
    c = ticker.contract
    g = ticker.modelGreeks
    bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
    ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
    mid = round((bid + ask) / 2, 4) if (bid is not None and ask is not None) else None
    last = ticker.last if ticker.last and ticker.last > 0 else None
    if last is None and ticker.close and ticker.close > 0:
        last = ticker.close
    return {
        "right": right,
        "strike": float(c.strike),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        # spread in % of mid is the liquidity signal that actually decides
        # whether a strike is tradable at retail size
        "spread_pct": (round((ask - bid) / mid * 100, 1)
                       if (bid is not None and ask is not None and mid) else None),
        "iv": round(g.impliedVol * 100, 1) if (g and g.impliedVol) else None,
        "delta": round(g.delta, 3) if (g and g.delta is not None) else None,
        "gamma": round(g.gamma, 4) if (g and g.gamma is not None) else None,
        "theta": round(g.theta, 3) if (g and g.theta is not None) else None,
        "vega": round(g.vega, 3) if (g and g.vega is not None) else None,
        "oi": None,          # OI needs a separate generic tick (101/100); see README
        "volume": int(ticker.volume) if ticker.volume and ticker.volume > 0 else None,
    }


async def _chain_def(symbol: str):
    """Contract + spot + listed strikes/expirations. NO option market data.

    Split out because listing expirations used to go through a full chain fetch,
    which subscribed ~40 option contracts just to read a date list — the single
    most wasteful thing this module did against a rate-limited API.
    """
    from ib_insync import Stock

    w = _get_worker()
    await _ensure_connected(w)
    ib = w.ib

    q = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if not q:
        raise OptionsUnavailable(f"IBKR does not recognise the symbol {symbol}.")
    stock = q[0]

    [stk] = await ib.reqTickersAsync(stock)
    spot = stk.marketPrice()
    if not spot or spot != spot:
        spot = stk.close
    if not spot:
        raise OptionsUnavailable(
            f"No price for {symbol} — the market may be closed with no frozen data."
        )

    params = await ib.reqSecDefOptParamsAsync(stock.symbol, "", "STK", stock.conId)
    chains = [p for p in params if p.exchange == "SMART"] or list(params)
    if not chains:
        raise OptionsUnavailable(f"IBKR returned no option chain for {symbol}.")
    return stock, float(spot), chains[0]


async def _expirations(symbol: str) -> dict:
    _, spot, chain = await _chain_def(symbol)
    exps = sorted(chain.expirations)
    return {"spot": spot,
            "expirations": [{"value": e, "label": _fmt_expiry(e), "dte": _dte(e)}
                            for e in exps]}


async def _quote(ib, contracts):
    """Subscribe every contract at once, wait ONE window, read whatever arrived.

    ``reqTickersAsync`` waits until *all* requested contracts have data. On a
    delayed feed some strikes never populate, so it always burned its full
    timeout — and batching just serialised those timeouts into minutes. Firing
    all subscriptions and reading after a single wait costs one window total and
    keeps the strikes that did answer instead of discarding a whole batch.
    """
    for c in contracts:
        try:
            ib.reqMktData(c, "", False, False)
        except Exception:  # noqa: BLE001
            pass
    await asyncio.sleep(QUOTE_WAIT)
    out = []
    for c in contracts:
        try:
            out.append(ib.ticker(c))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                ib.cancelMktData(c)     # release the market-data line
            except Exception:  # noqa: BLE001
                pass
    return [x for x in out if x is not None]


async def _fetch(symbol: str, expiry: str | None) -> dict:
    global _mkt_type
    from ib_insync import Option

    w = _get_worker()
    ib = w.ib
    stock, spot, chain = await _chain_def(symbol)

    expirations = sorted(chain.expirations)
    if not expirations:
        raise OptionsUnavailable(f"No listed expirations for {symbol}.")
    chosen = expiry if expiry in expirations else expirations[0]

    # reqSecDefOptParams returns the UNION of strikes across every expiration, so
    # a monthly expiry only lists a subset of them (the finer strikes belong to
    # weeklies). Slicing the union directly meant most contracts failed to
    # qualify and the chain came back with a handful of strikes — which silently
    # changes which one looks closest to the target delta. So: qualify a GENEROUS
    # slice first (qualification is free, no market data), then keep the
    # ±STRIKE_WINDOW strikes that actually exist for this expiry.
    strikes = sorted(chain.strikes)
    nearest = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    probe = strikes[max(0, nearest - STRIKE_WINDOW * 3):nearest + STRIKE_WINDOW * 3 + 1]
    if not probe:
        raise OptionsUnavailable(f"No strikes near {spot:.2f} for {symbol}.")

    probe_contracts = [Option(symbol, chosen, k, r, "SMART", tradingClass=chain.tradingClass)
                       for k in probe for r in ("C", "P")]
    qualified = await ib.qualifyContractsAsync(*probe_contracts)
    real = sorted({c.strike for c in qualified if getattr(c, "conId", None)})
    if not real:
        raise OptionsUnavailable(
            f"None of the {symbol} {_fmt_expiry(chosen)} contracts qualified — "
            "the expiry may have no listed strikes near spot."
        )

    j = min(range(len(real)), key=lambda i: abs(real[i] - spot))
    window = set(real[max(0, j - STRIKE_WINDOW):j + STRIKE_WINDOW + 1])
    contracts = [c for c in qualified
                 if getattr(c, "conId", None) and c.strike in window]
    if not contracts:
        raise OptionsUnavailable(
            f"None of the {symbol} {_fmt_expiry(chosen)} contracts qualified — "
            "the expiry may have no listed strikes near spot."
        )

    # Sticky market-data type: probe live ONCE per process, then stay on whatever
    # actually produced Greeks. Re-probing on every request cost a full slow
    # round-trip on an account without an OPRA subscription.
    if _mkt_type is None:
        ib.reqMarketDataType(1)
        tickers = await _quote(ib, contracts)
        got = sum(1 for x in tickers if x.modelGreeks)
        # require a real majority: one stray model among 40 contracts is noise,
        # and latching "live" on it leaves the rest of the chain blank forever.
        if tickers and got >= max(2, len(tickers) // 2):
            _mkt_type = 1
        else:
            _mkt_type = 4                      # delayed-frozen; IBKR offers it free
            ib.reqMarketDataType(_mkt_type)
            tickers = await _quote(ib, contracts)
    else:
        ib.reqMarketDataType(_mkt_type)
        tickers = await _quote(ib, contracts)

    greeks_seen = sum(1 for x in tickers if x.modelGreeks)
    data_mode = "live" if _mkt_type == 1 else "delayed"

    calls, puts = [], []
    for x in tickers:
        right = getattr(x.contract, "right", "")
        (calls if right.startswith("C") else puts).append(_row(x, right))
    calls.sort(key=lambda r: r["strike"])
    puts.sort(key=lambda r: r["strike"])

    # ATM implied vol — the level the IV-regime rule is read off
    atm_iv = None
    if calls:
        atm = min(calls, key=lambda r: abs(r["strike"] - spot))
        atm_iv = atm.get("iv")

    return {
        "symbol": symbol,
        "spot": round(float(spot), 2),
        "expiry": chosen,
        "expiry_label": _fmt_expiry(chosen),
        "dte": _dte(chosen),
        "expirations": [{"value": e, "label": _fmt_expiry(e), "dte": _dte(e)}
                        for e in expirations[:24]],
        "calls": calls,
        "puts": puts,
        "atm_iv": atm_iv,
        "data_mode": data_mode,
        "greeks_ok": greeks_seen > 0,
        "strike_window": STRIKE_WINDOW,
        "source": f"TWS {HOST}:{PORT}",
    }


# ---------------------------------------------------------------- public API
def get_chain(symbol: str, expiry: str | None = None) -> dict:
    """Chain + Greeks for one symbol/expiry.

    Returns ``{"ok": True, ...}`` or ``{"ok": False, "error": "<why>"}``. Never
    raises: TWS being off is an expected state, and the tab renders the reason.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "No symbol."}

    key = (sym, expiry or "")
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

    try:
        w = _get_worker()
        data = w.submit(_fetch(sym, expiry or None), REQUEST_TIMEOUT)
        out = {"ok": True, **data}
    except OptionsUnavailable as exc:
        return {"ok": False, "error": str(exc), "symbol": sym,
                "source": f"TWS {HOST}:{PORT}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "symbol": sym, "source": f"TWS {HOST}:{PORT},",
                "error": f"Unexpected error talking to TWS: {type(exc).__name__}: {exc}"}

    with _cache_lock:
        # time.time() again, NOT the `now` captured before the fetch: a 30s chain
        # request would otherwise be cached already-expired and never hit.
        _cache[key] = (time.time() + _CACHE_TTL, out)
    return out


def status() -> dict:
    """Cheap connection probe for the status pill (never connects on its own)."""
    global _worker
    connected = bool(_worker and _worker.ib is not None and _worker.ib.isConnected())
    return {"connected": connected, "host": HOST, "port": PORT, "client_id": CLIENT_ID}


# ---------------------------------------------------------------- IV regime
async def _iv_stats(symbol: str) -> dict:
    """IV percentile / rank from IBKR's own daily implied-vol history.

    This is the piece Yahoo cannot give: ``whatToShow='OPTION_IMPLIED_VOLATILITY'``
    returns a year of daily IV for the underlying, which is exactly what the
    buy-vs-sell gate needs. Without it there is no IV percentile at all.
    """
    from ib_insync import Stock

    w = _get_worker()
    await _ensure_connected(w)
    ib = w.ib

    q = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if not q:
        raise OptionsUnavailable(f"IBKR does not recognise {symbol}.")

    bars = await ib.reqHistoricalDataAsync(
        q[0], endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
        whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True, formatDate=1,
    )
    vals = [b.close for b in (bars or []) if b.close and b.close > 0]
    if len(vals) < 30:
        return {"iv_percentile": None, "iv_rank": None, "iv_current": None,
                "n": len(vals),
                "note": "Not enough IV history from IBKR to compute a percentile."}

    cur = vals[-1]
    below = sum(1 for v in vals if v < cur)
    lo, hi = min(vals), max(vals)
    return {
        "iv_current": round(cur * 100, 1),
        "iv_percentile": round(below / len(vals) * 100, 1),
        "iv_rank": round((cur - lo) / (hi - lo) * 100, 1) if hi > lo else None,
        "iv_low": round(lo * 100, 1),
        "iv_high": round(hi * 100, 1),
        "n": len(vals),
        "note": "",
    }


async def _net_liquidation() -> float | None:
    """Account net liquidation, for the 2%-of-NLV sizing rule."""
    w = _get_worker()
    await _ensure_connected(w)
    rows = await w.ib.accountSummaryAsync()
    for r in rows:
        if r.tag == "NetLiquidation":
            try:
                return float(r.value)
            except (TypeError, ValueError):
                return None
    return None


def iv_stats(symbol: str) -> dict:
    """IV percentile/rank for one symbol. Never raises."""
    sym = (symbol or "").strip().upper()
    try:
        return {"ok": True, **_get_worker().submit(_iv_stats(sym), REQUEST_TIMEOUT)}
    except OptionsUnavailable as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def net_liquidation() -> float | None:
    """Account NLV, or None if TWS is off / the account has no summary."""
    try:
        return _get_worker().submit(_net_liquidation(), REQUEST_TIMEOUT)
    except Exception:  # noqa: BLE001
        return None


def chain_for_dte(symbol: str, dte_min: int, dte_max: int) -> dict:
    """Chain for the expiry that best fits a DTE window.

    Lists expirations through the CHEAP definition call (no option market data),
    picks the target, then quotes exactly that one expiry — previously this cost
    two full quoted fetches.
    """
    sym = (symbol or "").strip().upper()
    try:
        info = _get_worker().submit(_expirations(sym), REQUEST_TIMEOUT)
    except OptionsUnavailable as exc:
        return {"ok": False, "error": str(exc), "symbol": sym,
                "source": f"TWS {HOST}:{PORT}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "symbol": sym, "source": f"TWS {HOST}:{PORT}",
                "error": f"Unexpected error talking to TWS: {type(exc).__name__}: {exc}"}

    dated = [e for e in info["expirations"] if e.get("dte") is not None]
    if not dated:
        return get_chain(sym)
    inside = [e for e in dated if dte_min <= e["dte"] <= dte_max]
    pool = inside or dated
    target = min(pool, key=lambda e: abs(e["dte"] - (dte_min + dte_max) / 2))
    return get_chain(sym, target["value"])
