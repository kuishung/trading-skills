"""The interpolated history (v4.209) - the Interpolation page's "History" tab.

The user (2026-10-24): *"if i want to show this historical table but based on interpolated data?"*
A row per trading day of what the Interpolated surface would have shown for ONE cell - a fixed
horizon and a fixed moneyness (or a fixed strike) - rebuilt from THAT day's listed contracts (the
Parquet contract history: closes solved back to IVs) and that day's stock close. Not a contract: no
open / high / low / volume; each row carries the legs and weights it rested on.

``series(sdb, symbol, h, m=|k=, window_days=...)``: the sessions of the window (the converted days,
newest first), for each one the contracts near the level and the horizon only (strikes within
``K_BAND`` of the level, expiries from ``MIN_LEG_DTE`` to ``dte_cap(h)`` days) solved and
interpolated through ``vol_cm_point.point``. Cached in memory ``TTL`` seconds per
(symbol, h, m / k, side, window, data root) - a second look is instant. ``stats``: the range over the
window and where the newest value sits (rank / percentile), the same arithmetic as the listed tab's
``vol_history``. This is the constant-maturity history the phase-2 note planned, computed on demand
for one cell.
"""
from __future__ import annotations

import datetime as _dt
import threading
import time

from . import contract_view, opt_history, vol_cm, vol_cm_point, vol_history, vol_surface
from .opt_constants import RISK_FREE
from .vol_surface import SurfaceError

K_BAND = 0.12                  # contracts within this fraction of the price around the level are solved
TTL = 600.0                    # seconds a computed series is reused
MAX_DAYS = 400                 # sessions solved for one series at most (a 365-day window holds ~252)
_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, dict]] = {}


def dte_cap(h: int) -> int:
    """Expiries read for a horizon: up to twice the horizon plus two months, so the upper leg is there."""
    return int(h * 2 + 60)


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def day_point(sdb, sym: str, oroot: str, d: _dt.date, spot: float | None, h: int, *, m=None, k=None, side=None,
              r: float = RISK_FREE, root=None) -> dict:
    """One day's row: ``{on, spot, K, iv, lo, hi, w, k_lo, k_hi, wk, price, n, why}``."""
    row = {"on": d.isoformat(), "spot": spot, "K": None, "iv": None, "lo": None, "hi": None, "w": None,
           "k_lo": None, "k_hi": None, "wk": None, "price": None, "n": 0, "why": None, "exact": False}
    if spot is None or spot <= 0:
        row["why"] = "no stock close on file"
        return row
    K = round(spot * m, 2) if m is not None else float(k)
    row["K"] = K
    tbl = opt_history.chain(oroot, d, columns=["expiry", "right", "strike", "close"], root=root)
    if tbl is None:
        row["why"] = "day not converted"
        return row
    if tbl.num_rows == 0:
        row["why"] = "no trade"
        return row
    cap = dte_cap(h)
    prices, S, Ks, Ts, calls, meta = [], [], [], [], [], []
    for rr in tbl.to_pylist():
        e, kk, c = rr["expiry"], _f(rr["strike"]), _f(rr["close"])
        if e is None or kk is None or c is None or c <= 0:
            continue
        dte = (e - d).days
        if dte < vol_cm.MIN_LEG_DTE or dte > cap or abs(kk - K) > K_BAND * spot:
            continue
        prices.append(c)
        S.append(spot)
        Ks.append(kk)
        Ts.append(dte / 365.0)
        calls.append(rr["right"] == "C")
        meta.append((e, rr["right"], kk))
    if not prices:
        row["why"] = "no contract near the level traded"
        return row
    ivs = vol_surface.solve_ivs(prices, S, Ks, Ts, calls)
    rows = [{"expiry": e, "right": right, "strike": kk, "iv": iv} for (e, right, kk), iv in zip(meta, ivs)]
    o = vol_surface.norm_options(side, 0, 0, "all")
    try:
        grid = vol_surface.build(rows, spot, d, common=False, **o)
    except SurfaceError as exc:
        row["why"] = str(exc)
        return row
    if not grid["n_cells"]:
        row["why"] = "no contract near the level has a volatility"
        return row
    try:
        pt = vol_cm_point.point(grid, h, m=m, k=k, r=r)
    except (SurfaceError, ValueError) as exc:
        row["why"] = str(exc)
        return row
    row["n"] = int(pt.get("n_contracts") or 0)
    if pt.get("iv") is None:
        row["why"] = pt.get("why") or "no legs around the point"
        return row
    t, s = pt.get("time") or {}, pt.get("strike") or {}
    row.update({"iv": pt["iv"], "exact": bool(pt.get("exact")),
                "lo": {**t["lo"], "iv": (t["per_strike"][0]["lo_iv"] if t.get("per_strike") else None)} if t else None,
                "hi": {**t["hi"], "iv": (t["per_strike"][0]["hi_iv"] if t.get("per_strike") else None)} if t else None,
                "w": t.get("w") if t else None,
                "k_lo": s.get("k_lo") if s else None, "k_hi": s.get("k_hi") if s else None, "wk": s.get("wk") if s else None,
                "price": ((pt.get("greeks") or {}).get("result") or {}).get("price")})
    return row


def series(sdb, symbol, h, *, m=None, k=None, side=None, window_days=None, root=None, today=None) -> dict:
    """The rows (newest first) and the stats for one cell over the rank window."""
    sym = vol_surface._sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    h = vol_cm_point.norm_h(h)
    if (m is None) == (k is None):
        raise ValueError("give a moneyness (m) or a strike (k), not both")
    m = vol_cm_point.norm_m(m) if m is not None else None
    k = vol_cm_point.norm_k(k) if k is not None else None
    wd = vol_history.norm_window(window_days)
    side = vol_surface.norm_options(side, 0, 0, "all")["side"]
    why = opt_history.available()
    if why:
        raise SurfaceError("the contract history is not set up on this server (%s)" % why)
    oroot = vol_surface.option_root_of(sdb, sym)
    if oroot is None:
        raise SurfaceError("%s is an index - the daily files have no index close to price its options against" % sym)
    key = (sym, h, m, k, side, wd, str(root or ""))
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    days_all = contract_view.history_days(root)             # newest first
    if not days_all:
        raise SurfaceError("no day of the contract history has been converted yet")
    end = _to_date(days_all[0])
    start = end - _dt.timedelta(days=wd)
    days = [d for d in days_all if _to_date(d) and _to_date(d) > start][:MAX_DAYS]
    closes = vol_surface.raw_closes(sdb, sym, days)
    rows = []
    for ds in days:
        d = _to_date(ds)
        rows.append(day_point(sdb, sym, oroot, d, closes.get(ds), h, m=m, k=k, side=side, root=root))
    vals = [r["iv"] for r in rows if r["iv"] is not None]
    stats = None
    if vals:
        lo, hi = min(vals), max(vals)
        newest = next((r["iv"] for r in rows if r["iv"] is not None), None)
        below = sum(1 for v in vals if v < newest) if newest is not None else 0
        stats = {"n": len(vals), "lo": round(lo, 4), "hi": round(hi, 4), "newest": newest,
                 "newest_on": next((r["on"] for r in rows if r["iv"] is not None), None),
                 "rank": round((newest - lo) / (hi - lo), 4) if newest is not None and hi > lo else None,
                 "pct": round(below / len(vals), 4) if newest is not None else None,
                 "first": rows[-1]["on"] if rows else None, "last": rows[0]["on"] if rows else None}
    out = {"symbol": sym, "h": h, "m": m, "pct": round(m * 100.0, 1) if m is not None else None, "k": k,
           "fixed": "moneyness" if m is not None else "strike", "side": side, "window_days": wd,
           "window": [days[-1], days[0]] if days else None, "days": len(days), "rows": rows, "stats": stats,
           "rate": RISK_FREE, "root": oroot,
           "method": "each row: that day's listed contracts within %d %% of the level (closes solved to IV), expiries %d "
                     "to %d days out; linear in total variance between the two expiries around %d days, then linear "
                     "in strike between the two listed strikes around the level; nothing extrapolated"
                     % (int(K_BAND * 100), vol_cm.MIN_LEG_DTE, dte_cap(h), h)}
    with _LOCK:
        _CACHE[key] = (now, out)
    return out


__all__ = ["K_BAND", "TTL", "MAX_DAYS", "dte_cap", "day_point", "series", "clear_cache"]
