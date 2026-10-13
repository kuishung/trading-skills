r"""Snapshot IBKR's implied volatility for a few option chains - READ-ONLY - the independent source
the Hermes volatility-surface check compares Massive's numbers against (v4.139).

Run on the LAPTOP while TWS (or IB Gateway) is logged in with the API enabled:

    py -3.12 dashboard_tst\deploy\ibkr_iv_snapshot.py AAPL MSFT SPY

Then on Hermes, once Resilio has carried the file across (seconds on the LAN):

    .\.venv\Scripts\python.exe deploy\check_vol_surface.py AAPL MSFT SPY --ibkr

What it reads, per symbol: the stock's price; the SMART option chain (reqSecDefOptParams); the
nearest expiries plus the standard monthlies up to ~4 months; the strikes nearest the price
(within 10 %); for every one of those calls and puts: bid, ask, last, volume and IBKR's own option
model (impliedVol + the underlying price it used, plus the bid / ask IVs). Each contract is
subscribed for a few seconds in batches of 40 and cancelled (well inside TWS's market-data lines).

It writes ONE JSON file: <data_root>\iv_check\ibkr_<YYYY-MM-DD_HHMM>ET.json (data_root = the
TradeHunter config.json data_root, D:\HermesSync\MarketData on the laptop - Resilio syncs it to
C:\HermesSync\MarketData on Hermes). Nothing else is written; nothing in the account is read.

SAFETY: market data only. It connects with the low-level client (no position / order / account
sync), it never calls placeOrder (the IB object's order methods are replaced by a guard that
raises), and it disconnects at the end. clientId 90 (CLAUDE.md table; --client-id to change).
Ports: 7496 TWS live, 7497 TWS paper, 4001 / 4002 Gateway.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SKILL = Path(__file__).resolve().parents[2]          # TradeHunter/
CLIENT_ID = 90
BATCH = 40                                           # market-data lines open at once
WAIT_S = 3.0                                         # seconds per batch for the ticks + the model to arrive ...
WAIT_MORE_S = 4.0                                    # ... and up to this much more for contracts still without it
BAND = 0.10                                          # strikes within 10 % of the price ...
MAX_STRIKES = 12                                     # ... at most this many, nearest the price
NEAR_EXPIRIES = 4                                    # the nearest expiries (weeklies included) ...
MONTHLY_DAYS = 130                                   # ... plus the standard monthlies up to this far


_T0 = time.monotonic()


def say(msg: str) -> None:
    print("[%5.1fs] %s" % (time.monotonic() - _T0, msg), flush=True)


def _no_orders(*_a, **_k):
    raise RuntimeError("ibkr_iv_snapshot is read-only: no order may be sent")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > -1 else None


def _price(v):
    f = _num(v)
    return f if f is not None and f > 0 else None


def data_root() -> Path:
    try:
        sys.path.insert(0, str(SKILL))
        from scripts._common import get_data_root  # noqa: PLC0415

        return Path(get_data_root())
    except Exception:  # noqa: BLE001 - no config.json on this PC
        return Path(r"D:\HermesSync\MarketData")


def third_friday(y: int, m: int) -> _dt.date:
    d = _dt.date(y, m, 15)
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


def pick_expiries(exps: list[str], today: _dt.date) -> list[str]:
    ds = sorted(_dt.date(int(e[:4]), int(e[4:6]), int(e[6:8])) for e in exps)
    ds = [d for d in ds if d > today]
    out = set(ds[:NEAR_EXPIRIES])
    for d in ds:
        tf = third_friday(d.year, d.month)
        if (d - today).days <= MONTHLY_DAYS and d in (tf, tf - _dt.timedelta(days=1)):
            out.add(d)
    return [d.strftime("%Y%m%d") for d in sorted(out)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only IBKR implied-volatility snapshot for the surface check.")
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7496, help="7496 TWS live (default), 7497 TWS paper, 4001/4002 Gateway")
    ap.add_argument("--client-id", type=int, default=CLIENT_ID)
    ap.add_argument("--out", default=None, help="folder for the JSON (default <data_root>\\iv_check)")
    a = ap.parse_args(argv)

    from ib_insync import IB, Option, Stock  # noqa: PLC0415 - py -3.12 (CLAUDE.md: ib_insync breaks on 3.14)

    logging.getLogger("ib_insync").setLevel(logging.CRITICAL)   # "no security definition" for strikes a far expiry lacks

    ib = IB()
    ib.placeOrder = ib.cancelOrder = ib.reqGlobalCancel = _no_orders       # belt and braces
    errors: list[str] = []
    ib.errorEvent += lambda reqId, code, msg, contract: errors.append("%s %s" % (code, msg)) \
        if code not in (2104, 2106, 2107, 2108, 2158, 2119, 10167, 300) else None
    ib.client.connect(a.host, a.port, a.client_id, timeout=10)              # no account / order sync
    if not ib.client.isConnected():
        print("could not connect to %s:%s" % (a.host, a.port))
        return 1
    print("connected to IBKR %s:%s (server version %s) as clientId %s - market data only"
          % (a.host, a.port, ib.client.serverVersion(), a.client_id))
    ib.reqMarketDataType(1)                                                 # live (frozen when closed)
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        now_et = now_utc - _dt.timedelta(hours=4)
    doc = {"taken_utc": now_utc.replace(microsecond=0).isoformat(), "taken_et": now_et.strftime("%Y-%m-%d %H:%M"),
           "source": "IBKR %s:%s (read-only snapshot, ibkr_iv_snapshot.py)" % (a.host, a.port),
           "model": "IBKR's own option model (modelGreeks.impliedVol)", "symbols": {}}
    try:
        for sym in [s.strip().upper() for s in a.symbols if s.strip()]:
            stk = Stock(sym, "SMART", "USD")
            say("%s: the stock" % sym)
            if not ib.qualifyContracts(stk):
                say("%s: not found at IBKR" % sym)
                continue
            t = ib.reqMktData(stk, "", False, False)
            ib.sleep(2.5)
            spot = _price(t.last) or (_price(t.bid) and _price(t.ask) and (t.bid + t.ask) / 2) or _price(t.close)
            spot_src = "last" if _price(t.last) else ("mid" if (_price(t.bid) and _price(t.ask)) else "close")
            ib.cancelMktData(stk)
            if not spot:
                print("%s: no stock price from IBKR (market data permission?) - skipped" % sym)
                continue
            chains = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
            ch = next((c for c in chains if c.exchange == "SMART" and c.tradingClass == sym), None) \
                or next((c for c in chains if c.exchange == "SMART"), None)
            if ch is None:
                print("%s: no SMART option chain" % sym)
                continue
            exps = pick_expiries(list(ch.expirations), now_et.date())
            say("%s: price %.2f, chain %s (%d expiries listed)" % (sym, spot, ch.tradingClass, len(ch.expirations)))
            strikes = sorted((k for k in ch.strikes if abs(k / spot - 1) <= BAND), key=lambda k: (abs(k - spot), k))
            strikes = sorted(strikes[:MAX_STRIKES])
            cons = [Option(sym, e, k, r, "SMART", tradingClass=ch.tradingClass, multiplier=ch.multiplier)
                    for e in exps for k in strikes for r in ("C", "P")]
            # the expiry as ASKED (the chain's own date): after qualifying, ib_insync hands back the
            # weeklies' lastTradeDateOrContractMonth one day late (SPY 20261016 -> "20261017", a Saturday)
            asked = {id(c): c.lastTradeDateOrContractMonth for c in cons}
            good = []
            for i in range(0, len(cons), 50):
                good += [c for c in ib.qualifyContracts(*cons[i:i + 50]) if c.conId]
            say("%s: %d of %d contracts exist - reading their market data" % (sym, len(good), len(cons)))
            rows = []
            for i in range(0, len(good), BATCH):
                batch = good[i:i + BATCH]
                tickers = [ib.reqMktData(c, "", False, False) for c in batch]
                ib.sleep(WAIT_S)
                waited = 0.0
                while waited < WAIT_MORE_S and any(tk.modelGreeks is None for tk in tickers):
                    ib.sleep(0.5)
                    waited += 0.5
                for c, tk in zip(batch, tickers):
                    mg, bg, ag = tk.modelGreeks, tk.bidGreeks, tk.askGreeks
                    e = asked.get(id(c)) or c.lastTradeDateOrContractMonth
                    rows.append({
                        "expiry": "%s-%s-%s" % (e[:4], e[4:6], e[6:8]),
                        "ib_last_trade_date": c.lastTradeDateOrContractMonth,
                        "right": c.right, "strike": float(c.strike),
                        "bid": _price(tk.bid), "ask": _price(tk.ask), "last": _price(tk.last), "close": _price(tk.close),
                        "volume": _num(tk.volume),
                        "iv_model": _num(mg.impliedVol) if mg else None,
                        "und_price": _price(mg.undPrice) if mg else None,
                        "iv_bid": _num(bg.impliedVol) if bg else None,
                        "iv_ask": _num(ag.impliedVol) if ag else None})
                for c in batch:
                    ib.cancelMktData(c)
            n_iv = sum(1 for r in rows if r["iv_model"])
            doc["symbols"][sym] = {"spot": spot, "spot_src": spot_src, "expiries": exps, "strikes": strikes,
                                   "contracts": rows}
            print("%s: price %.2f (%s), %d expiries x %d strikes -> %d contracts, %d with IBKR's model IV"
                  % (sym, spot, spot_src, len(exps), len(strikes), len(rows), n_iv))
            # the ATM line per expiry, so the run is readable on its own
            for e in exps:
                ex = "%s-%s-%s" % (e[:4], e[4:6], e[6:8])
                near = [r for r in rows if r["expiry"] == ex and r["iv_model"]]
                if near:
                    k = min({r["strike"] for r in near}, key=lambda x: (abs(x - spot), x))
                    ivs = [r["iv_model"] for r in near if r["strike"] == k]
                    print("   %s  ATM %-7g IBKR IV %5.1f%%" % (ex, k, 100 * sum(ivs) / len(ivs)))
    finally:
        ib.disconnect()
    if errors:
        print("IBKR messages (%d): %s" % (len(errors), "; ".join(sorted(set(errors))[:6])))
    out = Path(a.out) if a.out else data_root() / "iv_check"
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("ibkr_%s.json" % now_et.strftime("%Y-%m-%d_%H%M") + "")
    path = path.with_name(path.stem + "ET.json")
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print("saved %s (%d symbols) - Resilio carries it to Hermes" % (path, len(doc["symbols"])))
    return 0 if doc["symbols"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
