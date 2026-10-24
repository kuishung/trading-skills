"""One interpolated cell, opened up (v4.209) - the Interpolation page's "Working" and "Charts" tabs.

The user (2026-10-24): *"when clicked in, we need to see how the interpolated works and represented by
graphical ... a table based on original contract and then show the interpolate based on a linear line
of moneyness and the time horizon"*.

``point(grid, h, m= | k=)`` takes ``vol_surface.build``'s listed grid and derives ONE cell the way
``vol_cm.interpolate`` does (step 1: linear in total variance between the two listed expiries around
the horizon, per listed strike; step 2: linear in strike between the two listed strikes around
spot x moneyness), and returns everything the page draws:

* ``table`` - the listed contracts around the cell (the two leg expiries +- one, the two leg strikes
  +- one) with each contract's own IV, and where the horizon line and the level line cut between
  them (``hpos`` / ``kpos``, fractions);
* ``time`` / ``strike`` - the two steps' legs, weights and arithmetic (the working tables);
* ``term`` - per leg strike, every listed expiry's IV and total variance plus the cut (the
  term-structure chart);
* ``smile`` - every listed strike's time-interpolated IV at the horizon plus the cut (the smile chart);
* ``greeks`` - the legs' greeks and the cell's (the same weights) and the model price.

``point_for(sdb, symbol, day, h, ...)`` reads the day (the last pass, or a past day from the Parquet
history) and calls ``point``. Nothing is extrapolated: a horizon or a level outside the listed
contracts gives ``iv`` None and a ``why``.
"""
from __future__ import annotations

import datetime as _dt
import math

from . import vol_cm, vol_surface
from .opt_constants import RISK_FREE
from .vol_surface import SurfaceError

MIN_H, MAX_H = 1, 1500
MIN_M, MAX_M = 0.5, 1.5
_GREEKS = ("d", "g", "t", "v")


def norm_h(h) -> int:
    try:
        n = int(float(str(h).strip()))
    except (TypeError, ValueError):
        raise ValueError("the horizon must be a whole number of days from %d to %d" % (MIN_H, MAX_H)) from None
    if not (MIN_H <= n <= MAX_H):
        raise ValueError("the horizon must be a whole number of days from %d to %d" % (MIN_H, MAX_H))
    return n


def norm_m(m) -> float:
    """Moneyness as a fraction: ``0.95`` or ``95`` (percent) both mean 95 %."""
    try:
        f = float(str(m).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise ValueError("the moneyness must be a number (a fraction like 0.95 or a percent like 95)") from None
    if f > 3.0:
        f = f / 100.0
    if not (MIN_M <= f <= MAX_M):
        raise ValueError("the moneyness must be between %d %% and %d %% of the price" % (MIN_M * 100, MAX_M * 100))
    return round(f, 4)


def norm_k(k) -> float:
    try:
        f = float(str(k).strip())
    except (TypeError, ValueError):
        raise ValueError("the strike must be a number") from None
    if not (f > 0):
        raise ValueError("the strike must be positive")
    return f


def _var(iv_pct, years) -> float | None:
    return None if iv_pct is None else round((float(iv_pct) / 100.0) ** 2 * years, 6)


def _r4(v):
    return None if v is None else round(float(v), 4)


def _row_of(grid: dict, k: float) -> int | None:
    for i, s in enumerate(grid.get("strikes") or ()):
        if abs(float(s) - k) <= 1e-9:
            return i
    return None


def _col_of(grid: dict, date: str) -> int | None:
    for i, e in enumerate(grid.get("expiries") or ()):
        if e["date"] == date:
            return i
    return None


def _cell(grid: dict, ri: int | None, ci: int | None):
    if ri is None or ci is None:
        return None
    row = (grid.get("cells") or [])[ri] if ri < len(grid.get("cells") or []) else None
    return None if row is None or ci >= len(row) else row[ci]


def _extra(grid: dict, ri: int | None, ci: int | None) -> dict | None:
    if ri is None or ci is None:
        return None
    rows = grid.get("extra") or []
    row = rows[ri] if ri < len(rows) else None
    x = None if row is None or ci >= len(row) else row[ci]
    return {g: x.get(g) for g in _GREEKS} if isinstance(x, dict) else None


MAP_BAND = 0.20                 # the map's strikes: within this fraction of the price ...
MAP_ROWS = 26                   # ... and at most this many, the nearest to the level


def point(grid: dict, h, *, m=None, k=None, r: float = RISK_FREE) -> dict:
    """The one cell, opened up (see the module docstring). ``m`` (moneyness) or ``k`` (a listed
    strike) - one of them."""
    h = norm_h(h)
    if (m is None) == (k is None):
        raise ValueError("give a moneyness (m) or a strike (k), not both")
    spot = float(grid.get("spot") or 0.0)
    if not (spot > 0):
        raise SurfaceError("no price for the underlying, so the level cannot be placed")
    side = grid.get("side") or vol_surface.DEFAULT_SIDE
    T = h / 365.0
    hs = (h,)
    sg, n_used = vol_cm._by_strike(grid, hs, r)
    exps = list(grid.get("expiries") or ())
    usable = [e for e in exps if int(e["dte"]) >= vol_cm.MIN_LEG_DTE]
    out: dict = {"h": h, "years": round(T, 4), "spot": grid.get("spot"), "on": grid.get("on"), "side": side,
                 "rate": r, "n_expiries_used": n_used, "iv": None, "exact": False, "why": None,
                 "time": None, "strike": None, "table": None, "map": None, "term": [], "smile": None, "greeks": None}
    if m is not None:
        m = norm_m(m)
        K = round(spot * m, 2)
        out.update({"m": m, "pct": round(m * 100.0, 1), "k": K, "fixed": "moneyness"})
        mg = vol_cm._by_moneyness(sg, hs, spot, side, r, levels=(m,))
        if not mg["rows"]:
            out["why"] = "no two listed strikes around %.2f have a value at %d days (nothing is extrapolated)" % (K, h)
            return out
        iv, br, x, near, ek = mg["cells"][0][0], mg["brackets"][0][0], mg["extra"][0][0], mg["nearest"][0][0], mg["exact_k"][0][0]
        out["exact"] = bool(mg["exact"][0][0])
        if iv is None:
            out["why"] = "no two listed strikes around %.2f have a value at %d days (nothing is extrapolated)" % (K, h)
            return out
        k_lo = br["k_lo"]["strike"] if br and br.get("k_lo") else K
        k_hi = br["k_hi"]["strike"] if br and br.get("k_hi") else K
        wk = br["wk"] if br and br.get("wk") is not None else None
    else:
        K = norm_k(k)
        out.update({"m": None, "pct": None, "k": K, "fixed": "strike"})
        ri = _row_of(sg, K)
        if ri is None:
            out["why"] = "%.2f is not a listed strike with a value at %d days" % (K, h)
            return out
        iv, br, x, near = sg["cells"][ri][0], sg["brackets"][ri][0], sg["extra"][ri][0], sg["nearest"][ri][0]
        out["exact"] = bool(sg["exact"][ri][0])
        if iv is None:
            out["why"] = "no two listed expiries at least %d days out bracket %d days at strike %.2f" % (vol_cm.MIN_LEG_DTE, h, K)
            return out
        k_lo = k_hi = K
        wk = None
    right = vol_cm._right(side, K, spot)
    out.update({"iv": round(float(iv), 4), "right": right})
    # ── step 1, time: the two leg expiries (or the exact one), per leg strike ──
    lo = hi = None
    w = None
    if br and br.get("lo") and br.get("hi") and br.get("w") is not None:
        lo, hi, w = br["lo"], br["hi"], br["w"]
    elif near:                                   # the horizon IS a listed expiry
        lo = hi = {"date": near["date"], "dte": near["dte"], "iv": None}
    leg_ks = [k_lo] if k_lo == k_hi else [k_lo, k_hi]
    lo_ci, hi_ci = (_col_of(grid, lo["date"]) if lo else None), (_col_of(grid, hi["date"]) if hi else None)
    rows_k = {kk: _row_of(grid, kk) for kk in leg_ks}
    time = None
    if lo and hi:
        T1, T2 = lo["dte"] / 365.0, hi["dte"] / 365.0
        per = []
        for kk in leg_ks:
            s1, s2 = _cell(grid, rows_k[kk], lo_ci), _cell(grid, rows_k[kk], hi_ci)
            ivk = sg["cells"][_row_of(sg, kk)][0] if _row_of(sg, kk) is not None else None
            per.append({"strike": kk, "lo_iv": s1, "hi_iv": s2, "lo_var": _var(s1, T1), "hi_var": _var(s2, T2),
                        "var": _var(ivk, T), "iv": _r4(ivk)})
        time = {"lo": {"date": lo["date"], "dte": int(lo["dte"]), "years": round(T1, 4)},
                "hi": {"date": hi["date"], "dte": int(hi["dte"]), "years": round(T2, 4)},
                "w": _r4(w) if w is not None else 1.0, "exact": lo["date"] == hi["date"], "per_strike": per}
    out["time"] = time
    # ── step 2, strike ──
    if m is not None and wk is not None and br and br.get("k_lo") and br.get("k_hi"):
        out["strike"] = {"k_lo": {"strike": k_lo, "iv": _r4(br["k_lo"]["iv"])},
                         "k_hi": {"strike": k_hi, "iv": _r4(br["k_hi"]["iv"])}, "wk": _r4(wk), "K": K, "exact": False}
    elif m is not None:
        out["strike"] = {"k_lo": {"strike": K, "iv": _r4(iv)}, "k_hi": {"strike": K, "iv": _r4(iv)}, "wk": None, "K": K,
                         "exact": True}
    # ── the contract table around the cell: leg expiries +- 1, leg strikes +- 1 ──
    if lo and hi:
        ci_list = sorted({c for c in (lo_ci, hi_ci) if c is not None})
        if ci_list:
            lo_i, hi_i = ci_list[0], ci_list[-1]
            cols = [c for c in range(lo_i - 1, hi_i + 2) if 0 <= c < len(exps)]
        else:
            cols = []
        ri_list = sorted({rows_k[kk] for kk in leg_ks if rows_k[kk] is not None})
        if ri_list:
            lo_r, hi_r = ri_list[0], ri_list[-1]
            rws = [rr for rr in range(lo_r - 1, hi_r + 2) if 0 <= rr < len(grid.get("strikes") or ())]
        else:
            rws = []
        leg_cols = {c for c in (lo_ci, hi_ci) if c is not None}
        leg_rows = set(ri_list)
        T1, T2 = lo["dte"] / 365.0, hi["dte"] / 365.0
        hpos = 0.5 if T2 == T1 else (T - T1) / (T2 - T1)
        kpos = None if k_hi == k_lo else (K - k_lo) / (k_hi - k_lo)
        out["table"] = {
            "expiries": [{"date": exps[c]["date"], "dte": int(exps[c]["dte"]), "leg": c in leg_cols} for c in cols],
            "strikes": [{"strike": float(grid["strikes"][rr]), "leg": rr in leg_rows} for rr in rws],
            "cells": [[_cell(grid, rr, c) for c in cols] for rr in rws],
            "hpos": _r4(max(0.0, min(1.0, hpos))), "kpos": _r4(kpos) if kpos is not None else None,
            "lo_col": cols.index(lo_ci) if lo_ci in cols else None, "hi_col": cols.index(hi_ci) if hi_ci in cols else None,
            "lo_row": rws.index(rows_k[k_lo]) if rows_k.get(k_lo) in rws else None,
            "hi_row": rws.index(rows_k[k_hi]) if rows_k.get(k_hi) in rws else None,
        }
    # ── the surface map (v4.212): every listed expiry across, the listed strikes within MAP_BAND of the price down
    #    (at most MAP_ROWS, the nearest to the level), each cell that contract's own IV - the first tab draws it
    spot_f = float(spot or 0.0)
    rws_all = [rr for rr, s in enumerate(grid.get("strikes") or ())
               if not spot_f or abs(float(s) / spot_f - 1.0) <= MAP_BAND]
    rws_all = sorted(sorted(rws_all, key=lambda rr: abs(float(grid["strikes"][rr]) - K))[:MAP_ROWS])
    leg_dates = {e["date"] for e in (lo, hi) if e}
    out["map"] = {
        "expiries": [{"date": e["date"], "dte": int(e["dte"]), "leg": e["date"] in leg_dates} for e in exps],
        "strikes": [{"strike": float(grid["strikes"][rr]),
                     "pct": round(float(grid["strikes"][rr]) / spot_f * 100.0, 1) if spot_f else None,
                     "leg": float(grid["strikes"][rr]) in leg_ks} for rr in rws_all],
        "cells": [[_cell(grid, rr, c) for c in range(len(exps))] for rr in rws_all],
        "band": MAP_BAND, "capped": len(rws_all) < sum(1 for s in (grid.get("strikes") or ())
                                                        if not spot_f or abs(float(s) / spot_f - 1.0) <= MAP_BAND),
    }
    # ── the term structure per leg strike (every listed expiry's IV at that strike) ──
    for kk in leg_ks:
        rr = rows_k[kk]
        if rr is None:
            continue
        pts = []
        for c, e in enumerate(exps):
            v = _cell(grid, rr, c)
            if v is None:
                continue
            Te = int(e["dte"]) / 365.0
            pts.append({"date": e["date"], "dte": int(e["dte"]), "years": round(Te, 4), "iv": _r4(v), "var": _var(v, Te),
                        "leg": bool(lo and hi and e["date"] in (lo["date"], hi["date"])),
                        "usable": int(e["dte"]) >= vol_cm.MIN_LEG_DTE})
        sri = _row_of(sg, kk)
        ivk = sg["cells"][sri][0] if sri is not None else None
        out["term"].append({"strike": kk, "points": pts,
                            "at": {"dte": h, "years": round(T, 4), "iv": _r4(ivk), "var": _var(ivk, T)} if ivk is not None else None})
    # ── the smile at the horizon (every listed strike's time-interpolated IV) ──
    sm = [{"strike": float(s), "iv": _r4(sg["cells"][i][0]), "leg": float(s) in (k_lo, k_hi)}
          for i, s in enumerate(sg["strikes"]) if sg["cells"][i][0] is not None]
    out["smile"] = {"points": sm, "K": K, "at": out["iv"], "spot": grid.get("spot")}
    # ── the greeks: the legs' own and the cell's ──
    legs = []
    for kk in leg_ks:
        for ee, ci in ((lo, lo_ci), (hi, hi_ci)):
            if ee is None or ci is None or rows_k[kk] is None:
                continue
            if legs and any(lg["strike"] == kk and lg["date"] == ee["date"] for lg in legs):
                continue
            xg = _extra(grid, rows_k[kk], ci)
            legs.append({"strike": kk, "date": ee["date"], "dte": int(ee["dte"]), "iv": _cell(grid, rows_k[kk], ci),
                         "right": vol_cm._right(side, kk, spot), **{g: (xg or {}).get(g) for g in _GREEKS}})
    res = {g: (x or {}).get(g) for g in _GREEKS}
    res["price"] = (x or {}).get("price") if x else None
    out["greeks"] = {"legs": legs, "result": res, "has_greeks": any(lg.get("d") is not None for lg in legs)}
    out["n_contracts"] = len(legs)
    out["nearest"] = near
    return out


def point_for(sdb, symbol, day="latest", h=90, *, m=None, k=None, side=None, root=None) -> dict:
    """``point`` for ``symbol`` on ``day`` (``latest`` or ``YYYY-MM-DD``): the day's listed grid
    (every strike, every expiry, no common-strikes cut) plus ``symbol, name, source, as_of``."""
    sym = vol_surface._sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    h = norm_h(h)
    o = vol_surface.norm_options(side, 0, 0, "all")
    if str(day or "latest").strip().lower() == "latest":
        rows_in, meta = vol_surface.latest_rows(sdb, sym)
    else:
        rows_in, meta = vol_surface.history_rows(sdb, sym, day, band=0, max_dte=0, root=root)
    grid = vol_surface.build(rows_in, meta.get("spot"), meta.get("on"), common=False, **o)
    if not grid["n_cells"]:
        raise SurfaceError("no %s option has an implied volatility on that day" % sym)
    out = point(grid, h, m=m, k=k)
    as_of = meta.get("as_of")
    out.update({"symbol": sym, "name": meta.get("name"), "source": meta["source"],
                "as_of": as_of.replace(microsecond=0).isoformat() + "Z" if isinstance(as_of, _dt.datetime) else None,
                "has_greeks": meta["source"] == "latest" and bool((out.get("greeks") or {}).get("has_greeks"))})
    return out


__all__ = ["MIN_H", "MAX_H", "MIN_M", "MAX_M", "norm_h", "norm_m", "norm_k", "point", "point_for"]
