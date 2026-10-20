"""The INTERPOLATED (constant-maturity) volatility surface (v4.171): the volatility surface
re-cut on FIXED time horizons - 30 / 60 / 90 / 180 / 270 / 365 days - instead of the listed
expiry dates (the user, 2026-10-20: *"the original vol surface displays contracts with a fixed
expiry date; what we need is a fixed time horizon ... the Interpolated Volatility Surface derives
the fixed horizons and generates a new vol surface with the fixed-maturity data"*).

How a cell is derived
---------------------
The listed surface is built first, by ``vol_surface.build`` on the same rows and options (so the
same contract fills a strike on both tabs - ``side``: out of the money / calls / puts / average).
Then, per strike and per horizon ``H`` (calendar days, T = days / 365):

* a listed expiry exactly ``H`` days out with an IV at that strike -> that contract's own IV,
  marked ``exact``;
* otherwise the two LISTED expiries that bracket ``H`` - the last one before it and the first
  one after it, each at least ``MIN_LEG_DTE`` (7) days out (a contract in its last week is pinned
  by expiry flows and is not a fair leg) - and the IVs of the SAME strike on both. The total
  variance ``sigma^2 * T`` is interpolated linearly in time between them:

      w   = (T2 - T) / (T2 - T1)
      var = sigma1^2 * T1 * w + sigma2^2 * T2 * (1 - w)
      iv  = sqrt(var / T)

* no extrapolation: a horizon before the first usable expiry or beyond the last one is blank,
  and so is a strike missing on either bracketing expiry.

Beside each IV (``extra``): the greeks interpolated linearly in time between the same two legs
(null where either leg has none - a past day has none), and the Black-Scholes MODEL price at the
interpolated IV for the cell's right (puts below the spot, calls at / above it on "out of the
money"; the side's right otherwise), T = H / 365, r = ``RISK_FREE``, no dividend. A click on a
cell opens the contract history of the NEARER bracketing contract (``nearest``).

``interpolate`` is pure (it takes ``build``'s output) so it is tested without a DB;
``surface_cm`` reads the rows like ``vol_surface.surface`` does. IVs are PERCENT, like the
listed surface's.
"""
from __future__ import annotations

import datetime as _dt
import math

from . import vol_surface
from .black_scholes import black_scholes
from .opt_constants import RISK_FREE
from .vol_surface import SCALE_HI_PCT, SCALE_LO_PCT, SurfaceError

HORIZONS = (30, 60, 90, 180, 270, 365)
MIN_LEG_DTE = 7                       # an expiry fewer days out than this is never a bracketing leg
MAX_HORIZON = 3650                    # the endpoint accepts horizons up to 10 years
METHOD = ("linear in total variance between the two listed expiries around each horizon "
          "(legs at least 7 days out); no extrapolation")
_GREEKS = ("d", "g", "t", "v")


def norm_horizons(horizons=None) -> tuple[int, ...]:
    """The horizons, cleaned: positive whole days, sorted, no repeats; HORIZONS when none.
    Raises ``ValueError`` on a value that is not a positive whole number of days."""
    if horizons is None:
        return HORIZONS
    if isinstance(horizons, str):
        parts = [p.strip() for p in horizons.split(",") if p.strip()]
    else:
        parts = list(horizons)
    if not parts:
        return HORIZONS
    out = set()
    for p in parts:
        if isinstance(p, bool):
            raise ValueError("a horizon must be a whole number of days")
        try:
            f = float(p)
        except (TypeError, ValueError):
            raise ValueError("a horizon must be a whole number of days, not %r" % (str(p)[:12],)) from None
        if not math.isfinite(f) or f != int(f) or not (1 <= f <= MAX_HORIZON):
            raise ValueError("a horizon must be a whole number of days between 1 and %d" % MAX_HORIZON)
        out.add(int(f))
    return tuple(sorted(out))


def _right(side: str, k: float, spot: float) -> str:
    """The right a cell stands for - the listed surface's rule (``build``'s ``side``)."""
    if side == "call":
        return "C"
    if side == "put":
        return "P"
    return "P" if k < spot else "C"


def _model_price(spot: float, k: float, years: float, iv_pct: float, right: str, r: float) -> float | None:
    try:
        p = black_scholes(spot, k, years, r, iv_pct / 100.0, "call" if right == "C" else "put").price
    except ValueError:
        return None
    return round(p, 4) if math.isfinite(p) else None


def interpolate(grid: dict, horizons=HORIZONS, *, r: float = RISK_FREE) -> dict:
    """The constant-maturity grid from ``vol_surface.build``'s output ``grid``: ``{horizons:
    [{days, years}], strikes, cells (IV % | None per horizon per strike), exact (bool grid),
    brackets ([[{lo: {date, dte, iv}, hi: {date, dte, iv}, w} | None]] - None for an exact or an
    empty cell), extra ([[{d, g, t, v, price} | None]]), nearest ([[{date, dte} | None]] - the
    bracketing contract closer in days, or the exact one), spot, spot_row, on, side, scale,
    n_cells, n_expiries_used, method}``. A strike with no value at any horizon is dropped."""
    hs = norm_horizons(horizons)
    spot = float(grid.get("spot") or 0.0)
    side = grid.get("side") or vol_surface.DEFAULT_SIDE
    exps = list(grid.get("expiries") or ())
    cells_in = grid.get("cells") or []
    extra_in = grid.get("extra") or []
    usable = [(i, e) for i, e in enumerate(exps) if int(e["dte"]) >= MIN_LEG_DTE]

    def leg(row_i: int, col: int) -> tuple[float | None, dict | None]:
        row = cells_in[row_i] if row_i < len(cells_in) else []
        xrow = extra_in[row_i] if row_i < len(extra_in) else []
        v = row[col] if col < len(row) else None
        x = xrow[col] if col < len(xrow) else None
        return v, x

    strikes, cells, exact, brackets, extra, nearest = [], [], [], [], [], []
    used: set[str] = set()
    for ri, k in enumerate(grid.get("strikes") or ()):
        k = float(k)
        right = _right(side, k, spot)
        c_row, e_row, b_row, x_row, n_row = [], [], [], [], []
        for h in hs:
            T = h / 365.0
            iv = None
            is_exact, br, near, x = False, None, None, None
            hit = next(((i, e) for i, e in usable if int(e["dte"]) == h), None)
            hit_v = leg(ri, hit[0]) if hit else (None, None)
            if hit is not None and hit_v[0] is not None:
                iv, is_exact = float(hit_v[0]), True
                near = {"date": hit[1]["date"], "dte": int(hit[1]["dte"])}
                hx = hit_v[1]
                if hx is not None:
                    x = {g: hx.get(g) for g in _GREEKS}
                used.add(hit[1]["date"])
            else:
                lo = max(((i, e) for i, e in usable if int(e["dte"]) < h), key=lambda t: int(t[1]["dte"]), default=None)
                hi = min(((i, e) for i, e in usable if int(e["dte"]) > h), key=lambda t: int(t[1]["dte"]), default=None)
                if lo is not None and hi is not None:
                    (s1, x1), (s2, x2) = leg(ri, lo[0]), leg(ri, hi[0])
                    if s1 is not None and s2 is not None:
                        d1, d2 = int(lo[1]["dte"]), int(hi[1]["dte"])
                        T1, T2 = d1 / 365.0, d2 / 365.0
                        w = (T2 - T) / (T2 - T1)
                        var = (s1 / 100.0) ** 2 * T1 * w + (s2 / 100.0) ** 2 * T2 * (1.0 - w)
                        if var > 0:
                            iv = math.sqrt(var / T) * 100.0
                            br = {"lo": {"date": lo[1]["date"], "dte": d1, "iv": s1},
                                  "hi": {"date": hi[1]["date"], "dte": d2, "iv": s2}, "w": w}
                            pick = lo if (h - d1) <= (d2 - h) else hi     # a tie goes to the shorter leg
                            near = {"date": pick[1]["date"], "dte": int(pick[1]["dte"])}
                            if x1 is not None or x2 is not None:
                                x = {}
                                for g in _GREEKS:
                                    a, b = (x1 or {}).get(g), (x2 or {}).get(g)
                                    x[g] = (a * w + b * (1.0 - w)) if (a is not None and b is not None) else None
                            used.add(lo[1]["date"])
                            used.add(hi[1]["date"])
            if iv is not None and x is not None:
                x["price"] = _model_price(spot, k, T, iv, right, r) if spot > 0 else None
            c_row.append(iv)
            e_row.append(is_exact)
            b_row.append(br)
            x_row.append(x if iv is not None else None)
            n_row.append(near)
        if any(v is not None for v in c_row):
            strikes.append(k)
            cells.append(c_row)
            exact.append(e_row)
            brackets.append(b_row)
            extra.append(x_row)
            nearest.append(n_row)
    flat = [v for row in cells for v in row if v is not None]
    scale = ([round(vol_surface._pctile(flat, SCALE_LO_PCT), 2), round(vol_surface._pctile(flat, SCALE_HI_PCT), 2)]
             if flat else None)
    return {
        "horizons": [{"days": h, "years": round(h / 365.0, 4)} for h in hs],
        "strikes": strikes, "cells": cells, "exact": exact, "brackets": brackets, "extra": extra,
        "nearest": nearest, "spot": grid.get("spot"), "spot_row": sum(1 for k in strikes if k < spot),
        "on": grid.get("on"), "side": side, "scale": scale, "n_cells": len(flat),
        "n_expiries_used": len(used), "method": METHOD,
    }


def surface_cm(sdb, symbol, day="latest", *, side=None, band=None, expiries=None, common=True, horizons=None,
               root=None) -> dict:
    """The interpolated surface of ``symbol`` on ``day`` (``latest`` or ``YYYY-MM-DD``):
    ``interpolate``'s grid plus ``symbol, name, source, spot_src, as_of, traded, has_greeks,
    horizons_requested, band, common, exp_mode, n_expiries_listed``. Every listed expiry is read (a
    365-day horizon needs the expiries beyond a year to bracket it). Raises ``SurfaceError`` with
    the reason, in plain words, when nothing can be derived."""
    sym = vol_surface._sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    hs = norm_horizons(horizons)
    o = vol_surface.norm_options(side, band, 0, expiries)
    if str(day or "latest").strip().lower() == "latest":
        rows, meta = vol_surface.latest_rows(sdb, sym)
    else:
        rows, meta = vol_surface.history_rows(sdb, sym, day, band=o["band"], max_dte=0, root=root)
    grid = vol_surface.build(rows, meta.get("spot"), meta.get("on"), common=common, **o)
    if not grid["n_cells"]:
        raise SurfaceError("no %s option inside this window has an implied volatility - widen the strike "
                           "range or the expiries" % sym)
    n_usable = sum(1 for e in grid["expiries"] if int(e["dte"]) >= MIN_LEG_DTE)
    out = interpolate(grid, hs)
    if not out["n_cells"]:
        span = "%dd" % hs[0] if len(hs) == 1 else "%dd to %dd" % (hs[0], hs[-1])
        if n_usable < 2:
            raise SurfaceError("fewer than two listed %s expiries are at least %d days out, so no horizon (%s) can be "
                               "derived - widen the strikes or show every expiry" % (sym, MIN_LEG_DTE, span))
        raise SurfaceError("no two listed %s expiries at least %d days out bracket any of the horizons (%s) at the "
                           "same strike - pick other horizons, widen the strikes or show every expiry"
                           % (sym, MIN_LEG_DTE, span))
    as_of = meta.get("as_of")
    return {**out, "symbol": sym, "name": meta.get("name"), "source": meta["source"],
            "spot_src": meta.get("spot_src"),
            "as_of": as_of.replace(microsecond=0).isoformat() + "Z" if isinstance(as_of, _dt.datetime) else None,
            "traded": meta.get("traded"), "has_greeks": meta["source"] == "latest",
            "horizons_requested": list(hs), "band": grid["band"], "common": grid["common"],
            "exp_mode": grid["exp_mode"], "n_expiries_listed": len(grid["expiries"])}


__all__ = ["HORIZONS", "MIN_LEG_DTE", "METHOD", "norm_horizons", "interpolate", "surface_cm"]
