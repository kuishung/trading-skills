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
import datetime as _dt
import os
import threading
import time

# ---- connection settings (app/.env, TST_ prefixed like everything else) ------
# clientId 86 is this app's slot. CLAUDE.md's allocation table already spends
# 71 (live bot), 80 (observer), 83/84 (ingest laptop/Hermes), 85 (health probe),
# 98/99 (probes) — IBKR refuses two sessions on the same id, so do not reuse one.
HOST = os.environ.get("TST_IBKR_HOST", "127.0.0.1")
PORT = int(os.environ.get("TST_IBKR_PORT", "7497"))          # TWS paper
CLIENT_ID = int(os.environ.get("TST_IBKR_CLIENT_ID", "86"))
CONNECT_TIMEOUT = float(os.environ.get("TST_IBKR_TIMEOUT", "6"))
STRIKE_WINDOW = int(os.environ.get("TST_IBKR_STRIKE_WINDOW", "10"))  # each side of spot
REQUEST_TIMEOUT = float(os.environ.get("TST_IBKR_REQ_TIMEOUT", "25"))

_CACHE_TTL = 20.0      # option quotes move fast; just enough to absorb re-renders
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
        raise OptionsUnavailable(
            f"No TWS/Gateway on {HOST}:{PORT} (clientId {CLIENT_ID}). "
            "Start TWS and enable File > Global Configuration > API > "
            "'Enable ActiveX and Socket Clients', check the socket port matches, "
            f"and make sure clientId {CLIENT_ID} isn't already in use. Details: {exc}"
        ) from exc


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


async def _fetch(symbol: str, expiry: str | None) -> dict:
    from ib_insync import Option, Stock

    w = _get_worker()
    await _ensure_connected(w)
    ib = w.ib

    stock = Stock(symbol, "SMART", "USD")
    qualified = await ib.qualifyContractsAsync(stock)
    if not qualified:
        raise OptionsUnavailable(f"IBKR does not recognise the symbol {symbol}.")
    stock = qualified[0]

    # spot — needed to centre the strike window
    [stk] = await ib.reqTickersAsync(stock)
    spot = stk.marketPrice()
    if not spot or spot != spot:            # NaN guard
        spot = stk.close
    if not spot:
        raise OptionsUnavailable(
            f"No price for {symbol} — the market may be closed with no frozen data available."
        )

    params = await ib.reqSecDefOptParamsAsync(stock.symbol, "", "STK", stock.conId)
    chains = [p for p in params if p.exchange == "SMART"] or list(params)
    if not chains:
        raise OptionsUnavailable(f"IBKR returned no option chain for {symbol}.")
    chain = chains[0]

    expirations = sorted(chain.expirations)
    if not expirations:
        raise OptionsUnavailable(f"No listed expirations for {symbol}.")
    chosen = expiry if expiry in expirations else expirations[0]

    # Quote a window of strikes around spot, NOT the whole chain: every contract
    # consumes a market-data line and accounts are capped (~100 concurrent).
    strikes = sorted(chain.strikes)
    nearest = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    lo = max(0, nearest - STRIKE_WINDOW)
    hi = min(len(strikes), nearest + STRIKE_WINDOW + 1)
    window = strikes[lo:hi]
    if not window:
        raise OptionsUnavailable(f"No strikes near {spot:.2f} for {symbol}.")

    contracts = [Option(symbol, chosen, k, r, "SMART", tradingClass=chain.tradingClass)
                 for k in window for r in ("C", "P")]
    contracts = await ib.qualifyContractsAsync(*contracts)
    contracts = [c for c in contracts if getattr(c, "conId", None)]
    if not contracts:
        raise OptionsUnavailable(
            f"None of the {symbol} {_fmt_expiry(chosen)} contracts qualified — "
            "the expiry may have no listed strikes near spot."
        )

    tickers = await ib.reqTickersAsync(*contracts)
    greeks_seen = sum(1 for t in tickers if t.modelGreeks)
    if not greeks_seen:
        # No live entitlement -> retry once in delayed-frozen mode
        ib.reqMarketDataType(4)
        tickers = await ib.reqTickersAsync(*contracts)
        greeks_seen = sum(1 for t in tickers if t.modelGreeks)
        data_mode = "delayed"
    else:
        data_mode = "live"

    calls, puts = [], []
    for t in tickers:
        right = getattr(t.contract, "right", "")
        (calls if right.startswith("C") else puts).append(_row(t, right))
    calls.sort(key=lambda r: r["strike"])
    puts.sort(key=lambda r: r["strike"])

    # ATM implied vol — the number Khoo's IV-regime rule is read off
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
        _cache[key] = (now + _CACHE_TTL, out)
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

    Prefers an expiry inside [dte_min, dte_max]; if none is listed, falls back to
    the closest one and lets the caller's DTE check report the miss.
    """
    probe = get_chain(symbol)
    if not probe.get("ok"):
        return probe
    exps = probe.get("expirations") or []
    inside = [e for e in exps if e.get("dte") is not None and dte_min <= e["dte"] <= dte_max]
    if inside:
        target = min(inside, key=lambda e: abs(e["dte"] - (dte_min + dte_max) / 2))
    else:
        dated = [e for e in exps if e.get("dte") is not None]
        if not dated:
            return probe
        target = min(dated, key=lambda e: abs(e["dte"] - (dte_min + dte_max) / 2))
    if target["value"] == probe.get("expiry"):
        return probe
    return get_chain(symbol, target["value"])


def short_put_delta(symbol: str, expiry_iso: str, strike: float) -> dict:
    """Live |delta| and DTE for one short put — the input to the management rule.

    Re-resolves from (symbol, expiry, strike) through the normal chain fetch, so
    it shares the 20s cache and the same TWS-off handling as everything else.
    """
    exp = (expiry_iso or "").replace("-", "")
    chain = get_chain(symbol, exp)
    if not chain.get("ok"):
        return {"ok": False, "error": chain.get("error", "no chain"), "delta": None, "dte": None}
    match = next((p for p in (chain.get("puts") or [])
                  if abs(p["strike"] - float(strike)) < 1e-6), None)
    if match is None:
        return {"ok": False, "delta": None, "dte": chain.get("dte"),
                "error": (f"{symbol} {expiry_iso} {strike:g}P is outside the "
                          f"±{STRIKE_WINDOW}-strike window being quoted — "
                          "raise TST_IBKR_STRIKE_WINDOW to keep it in view.")}
    d = match.get("delta")
    return {"ok": True, "delta": abs(d) if d is not None else None,
            "dte": chain.get("dte"), "mid": match.get("mid"), "error": None}
