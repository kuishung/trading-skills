"""Contract history for the Options page (v4.139, OPTIONS_SCREENER_DESIGN.md §14): pick a
contract from one day's chain, then see its whole life day by day from the contract history
(``opt_history``, Massive's options day files as Parquet) - open / high / low / close,
volume, trades, the stock's close that day and the implied volatility solved from the
contract's close (the volatility surface's own method, ``vol_surface.solve_ivs``).

Only days a contract TRADED are in Massive's day files, so a day with no trade has no row.
"""
from __future__ import annotations

import datetime as _dt

from . import opt_history
from .vol_surface import SurfaceError, _f, _sym, _to_date, option_root_of, raw_closes, solve_ivs

MAX_PICK_EXPIRIES = 120


def history_days(root=None) -> list[str]:
    """The sessions in the contract history, NEWEST first ([] when it is not set up)."""
    if opt_history.available():
        return []
    return list(reversed(opt_history.converted_days(root)))


def _root_or_error(sdb, symbol) -> tuple[str, str]:
    sym = _sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    why = opt_history.available()
    if why:
        raise SurfaceError("the contract history is not set up on this server (%s)" % why)
    oroot = option_root_of(sdb, sym)
    if oroot is None:
        raise SurfaceError("%s is an index - its options are not kept in the contract history" % sym)
    return sym, oroot


def day_contracts(sdb, symbol, day, *, root=None) -> dict:
    """The contracts of ``symbol`` that traded on ``day``, for the pickers:
    ``{symbol, root, on, spot (the stock's close that day, or None), expiries: [{expiry,
    dte, calls: [strike], puts: [strike]}]}``."""
    sym, oroot = _root_or_error(sdb, symbol)
    d = _to_date(day)
    if d is None:
        raise SurfaceError("pick a day")
    tbl = opt_history.chain(oroot, d, columns=["expiry", "right", "strike"], root=root)
    if tbl is None:
        raise SurfaceError("%s is not in the contract history yet" % d.isoformat())
    by: dict[_dt.date, dict[str, set]] = {}
    for r in tbl.to_pylist():
        e, k = r["expiry"], _f(r["strike"])
        if e is None or k is None:
            continue
        by.setdefault(e, {"C": set(), "P": set()})[r["right"]].add(k)
    if not by:
        raise SurfaceError("no %s option traded on %s" % (sym, d.isoformat()))
    exps = sorted(by)[:MAX_PICK_EXPIRIES]
    return {"symbol": sym, "root": oroot, "on": d.isoformat(),
            "spot": raw_closes(sdb, sym, [d.isoformat()]).get(d.isoformat()),
            "expiries": [{"expiry": e.isoformat(), "dte": (e - d).days, "calls": sorted(by[e]["C"]),
                          "puts": sorted(by[e]["P"])} for e in exps]}


def contract_series(sdb, symbol, expiry, right, strike, *, root=None) -> dict:
    """One contract's daily bars from the contract history with the stock's close and the
    IV of each day: ``{symbol, root, ticker, expiry, right, strike, rows: [{on, open, high,
    low, close, volume, transactions, spot, iv}]}`` (``iv`` PERCENT or None)."""
    sym, oroot = _root_or_error(sdb, symbol)
    e = _to_date(expiry)
    k = _f(strike)
    rt = str(right or "").strip().upper()[:1]
    if e is None or k is None or k <= 0 or rt not in ("C", "P"):
        raise SurfaceError("pick an expiry, a strike and call / put")
    rows = opt_history.contract_history(oroot, e, rt, k, root=root)
    days = [r["on"].isoformat() for r in rows]
    closes = raw_closes(sdb, sym, days)
    prices, S, K, T, calls, at = [], [], [], [], [], []
    for i, r in enumerate(rows):
        spot = closes.get(r["on"].isoformat())
        c = _f(r.get("close"))
        dte = (e - r["on"]).days
        if spot is None or c is None or c <= 0 or dte < 1:
            continue
        prices.append(c)
        S.append(spot)
        K.append(k)
        T.append(dte / 365.0)
        calls.append(rt == "C")
        at.append(i)
    ivs = dict(zip(at, solve_ivs(prices, S, K, T, calls)))
    out = []
    for i, r in enumerate(rows):
        iv = ivs.get(i)
        on = r["on"].isoformat()
        out.append({"on": on, "dte": (e - r["on"]).days, "open": r.get("open"), "high": r.get("high"),
                    "low": r.get("low"), "close": r.get("close"), "volume": r.get("volume"),
                    "transactions": r.get("transactions"), "spot": closes.get(on),
                    "iv": round(iv * 100.0, 2) if iv is not None else None})
    return {"symbol": sym, "root": oroot, "ticker": opt_history.occ_ticker(oroot, e, rt, k),
            "expiry": e.isoformat(), "right": rt, "strike": k, "rows": out}


__all__ = ["history_days", "day_contracts", "contract_series"]
