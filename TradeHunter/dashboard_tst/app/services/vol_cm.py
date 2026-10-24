"""The INTERPOLATED (constant-maturity) volatility surface (v4.171): the volatility surface
re-cut on FIXED time horizons - 30 days out to two years - instead of the listed expiry dates (the
user, 2026-10-20: *"the original vol surface displays contracts with a fixed expiry date; what we
need is a fixed time horizon ... the Interpolated Volatility Surface derives the fixed horizons and
generates a new vol surface with the fixed-maturity data"*), and since v4.175 on FIXED MONEYNESS
rows (strike / price) instead of the listed strikes (the user: *"the strike price of 340 today is
not comparable to the same strike price of 340 last week in terms of moneyness ... For the columns,
we interpolated for a fixed time to maturity. For the rows, we need to interpolate for a fixed
level of moneyness"*).

How a cell is derived - step 1, time (per listed strike and horizon)
--------------------------------------------------------------------
The listed surface is built first, by ``vol_surface.build`` on the same rows and options (so the
same contract fills a strike on both tabs - ``side``: out of the money / calls / puts / average),
always on EVERY listed expiry (v4.175: the Expiries all / monthly setting belongs to the other tab -
a monthly-only list has no leg before a 30-day horizon when the first monthly is 32 days out).
Then, per strike and per horizon ``H`` (calendar days, T = days / 365):

* a listed expiry exactly ``H`` days out with an IV at that strike -> that contract's own IV,
  marked exact;
* otherwise the two LISTED expiries that bracket ``H`` - the last one before it and the first
  one after it, each at least ``MIN_LEG_DTE`` (7) days out (a contract in its last week is pinned
  by expiry flows and is not a fair leg) - and the IVs of the SAME strike on both. The total
  variance ``sigma^2 * T`` is interpolated linearly in time between them:

      w   = (T2 - T) / (T2 - T1)
      var = sigma1^2 * T1 * w + sigma2^2 * T2 * (1 - w)
      iv  = sqrt(var / T)

* no extrapolation: a horizon before the first usable expiry or beyond the last one is blank,
  and so is a strike missing on either bracketing expiry.

Step 2, strike (per moneyness level and horizon; v4.175)
--------------------------------------------------------
For each level ``m`` of ``MONEYNESS`` (strike / price) the target strike is ``K = spot x m``. In
each horizon column, after step 1:

* a listed strike equal to ``K`` with a value -> its value, marked ``exact_k``;
* otherwise the two LISTED strikes ``K1 < K < K2`` nearest ``K`` that both have a value in that
  column, and the IV is interpolated LINEARLY IN STRIKE:

      wk = (K2 - K) / (K2 - K1)
      iv = iv1 * wk + iv2 * (1 - wk)

  Linear in strike is the standard first-order choice: between neighbouring listed strikes (a few
  dollars apart) the smile is close to a straight line, so a higher-order fit would add noise, not
  accuracy;
* no extrapolation: ``K`` below the lowest or above the highest listed strike with a value in
  that column is blank.

A cell is ``exact`` only when BOTH steps were exact (a listed expiry on the horizon and a listed
strike on ``K``).

Beside each IV (``extra``): the greeks interpolated linearly in time between the same two legs,
then linearly in strike between the same two strikes (null where a leg has none - a past day has
none; on "out of the money" a leg of the other right - the put below the price next to the call
above it - is first restated for the cell's right by put-call parity), and the Black-Scholes MODEL price at the interpolated IV for the cell's strike and right
(puts below the spot, calls at / above it on "out of the money"; the side's right otherwise),
T = H / 365, r = ``RISK_FREE``, no dividend. A click on a cell opens the contract history of the
NEARER listed expiry at the NEARER listed strike (``nearest``).

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

# v4.175: 30 days to two years - after a year by quarters (15, 18, 21 and 24 months in calendar days)
HORIZONS = (30, 60, 90, 180, 270, 365, 456, 547, 638, 730)
# v4.175: the rows - strike / price
MONEYNESS = (0.80, 0.85, 0.90, 0.95, 0.975, 1.00, 1.025, 1.05, 1.10, 1.15, 1.20)
ROWS = ("moneyness", "strike")
DEFAULT_ROWS = "moneyness"
MIN_LEG_DTE = 7                       # an expiry fewer days out than this is never a bracketing leg
MAX_HORIZON = 3650                    # the endpoint accepts horizons up to 10 years
METHOD = ("linear in total variance between the two listed expiries around each horizon "
          "(legs: every listed expiry at least 7 days out); rows at fixed moneyness (strike / price), "
          "linear in strike between the two listed strikes around it; no extrapolation")
_GREEKS = ("d", "g", "t", "v")
_K_TOL = 1e-6                         # a listed strike this close to K (relative) is K


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


def norm_rows(rows=None) -> str:
    """``moneyness`` (the default) or ``strike``; ``ValueError`` on anything else."""
    v = str(rows or DEFAULT_ROWS).strip().lower()
    if v in ("strikes",):
        v = "strike"
    if v not in ROWS:
        raise ValueError("rows must be 'moneyness' or 'strike'")
    return v


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


def _scale(cells) -> list | None:
    flat = [v for row in cells for v in row if v is not None]
    return ([round(vol_surface._pctile(flat, SCALE_LO_PCT), 2), round(vol_surface._pctile(flat, SCALE_HI_PCT), 2)]
            if flat else None)


def _mix(x1: dict | None, x2: dict | None, w: float) -> dict | None:
    """The greeks of two legs, ``w`` on the first; a greek missing on either leg is None."""
    if x1 is None and x2 is None:
        return None
    out = {}
    for g in _GREEKS:
        a, b = (x1 or {}).get(g), (x2 or {}).get(g)
        out[g] = (a * w + b * (1.0 - w)) if (a is not None and b is not None) else None
    return out


def _as_right(x: dict | None, k: float, leg_right: str, right: str, years: float, r: float) -> dict | None:
    """A leg's greeks restated for the cell's ``right`` by put-call parity (no dividend): on "out of
    the money" the strikes either side of the price carry a put's and a call's figures, and a level
    between them is ONE right. Delta: call = put + 1; theta (per day, as Massive quotes it): call =
    put - r K e^(-rT) / 365; gamma and vega are the same for both rights."""
    if x is None or leg_right == right:
        return x
    sgn = 1.0 if right == "C" else -1.0
    y = dict(x)
    if y.get("d") is not None:
        y["d"] = y["d"] + sgn
    if y.get("t") is not None:
        y["t"] = y["t"] - sgn * r * k * math.exp(-r * years) / 365.0
    return y


def _by_strike(grid: dict, hs: tuple[int, ...], r: float) -> tuple[dict, int]:
    """Step 1 (time) per listed strike: the by-strike grid and the number of expiries used."""
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
                            x = _mix(x1, x2, w)
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
    n = sum(1 for row in cells for v in row if v is not None)
    return ({"strikes": strikes, "cells": cells, "exact": exact, "brackets": brackets, "extra": extra,
             "nearest": nearest, "spot_row": sum(1 for k in strikes if k < spot), "scale": _scale(cells),
             "n_cells": n}, len(used))


def _by_moneyness(sg: dict, hs: tuple[int, ...], spot: float, side: str, r: float, levels=MONEYNESS) -> dict:
    """Step 2 (strike) per moneyness level, from step 1's by-strike grid ``sg``. ``levels`` =
    the moneyness levels to derive (the tab's fixed rows by default; one level for the
    interpolation page, v4.209)."""
    strikes = sg["strikes"]
    rows, cells, exact, exact_k, brackets, extra, nearest = [], [], [], [], [], [], []
    for m in levels:
        K = spot * m
        right = _right(side, K, spot)
        c_row, e_row, ek_row, b_row, x_row, n_row = [], [], [], [], [], []
        for c, h in enumerate(hs):
            have = [(i, strikes[i]) for i in range(len(strikes)) if sg["cells"][i][c] is not None]
            iv, ek, br, x, near = None, False, None, None, None
            hit = next(((i, k) for i, k in have if abs(k - K) <= _K_TOL * max(1.0, K)), None) if spot > 0 else None
            if hit is not None:
                i = hit[0]
                iv, ek = sg["cells"][i][c], True
                tb = sg["brackets"][i][c]
                if tb is not None:          # the strike is listed, the horizon is not
                    br = {"lo": dict(tb["lo"]), "hi": dict(tb["hi"]), "w": tb["w"],
                          "k_lo": None, "k_hi": None, "wk": None}
                sx = sg["extra"][i][c]
                x = {g: sx.get(g) for g in _GREEKS} if sx is not None else None
                sn = sg["nearest"][i][c]
                near = {**sn, "strike": hit[1]} if sn else None
            elif spot > 0:
                lo = max((t for t in have if t[1] < K), key=lambda t: t[1], default=None)
                hi = min((t for t in have if t[1] > K), key=lambda t: t[1], default=None)
                if lo is not None and hi is not None:
                    (i1, k1), (i2, k2) = lo, hi
                    v1, v2 = sg["cells"][i1][c], sg["cells"][i2][c]
                    wk = (k2 - K) / (k2 - k1)
                    iv = v1 * wk + v2 * (1.0 - wk)
                    b1, b2 = sg["brackets"][i1][c], sg["brackets"][i2][c]
                    tb = b1 or b2               # both strikes share the bracketing expiries when bracketed

                    def kpart(k, v, b):
                        return {"strike": k, "iv": v, "lo_iv": b["lo"]["iv"] if b else None,
                                "hi_iv": b["hi"]["iv"] if b else None}

                    br = {"lo": {"date": tb["lo"]["date"], "dte": tb["lo"]["dte"], "iv": None} if tb else None,
                          "hi": {"date": tb["hi"]["date"], "dte": tb["hi"]["dte"], "iv": None} if tb else None,
                          "w": tb["w"] if tb else None,
                          "k_lo": kpart(k1, v1, b1), "k_hi": kpart(k2, v2, b2), "wk": wk}
                    T = h / 365.0
                    x1 = _as_right(sg["extra"][i1][c], k1, _right(side, k1, spot), right, T, r) if side == "otm" \
                        else sg["extra"][i1][c]
                    x2 = _as_right(sg["extra"][i2][c], k2, _right(side, k2, spot), right, T, r) if side == "otm" \
                        else sg["extra"][i2][c]
                    x = _mix(x1, x2, wk)
                    pi, pk = (i1, k1) if (K - k1) <= (k2 - K) else (i2, k2)   # a tie goes to the lower strike
                    sn = sg["nearest"][pi][c]
                    near = {**sn, "strike": pk} if sn else None
            if iv is not None and x is not None:
                x["price"] = _model_price(spot, K, h / 365.0, iv, right, r)
            is_exact = bool(ek and br is None)
            c_row.append(iv)
            e_row.append(is_exact)
            ek_row.append(ek)
            b_row.append(None if is_exact else br)
            x_row.append(x if iv is not None else None)
            n_row.append(near if iv is not None else None)
        if any(v is not None for v in c_row):
            rows.append({"m": m, "pct": round(m * 100.0, 1), "strike": round(K, 2)})
            cells.append(c_row)
            exact.append(e_row)
            exact_k.append(ek_row)
            brackets.append(b_row)
            extra.append(x_row)
            nearest.append(n_row)
    n = sum(1 for row in cells for v in row if v is not None)
    return {"rows": rows, "strikes": [row["strike"] for row in rows], "cells": cells, "exact": exact,
            "exact_k": exact_k, "brackets": brackets, "extra": extra, "nearest": nearest,
            "spot_row": sum(1 for row in rows if row["m"] < 1.0), "scale": _scale(cells), "n_cells": n}


def interpolate(grid: dict, horizons=HORIZONS, *, r: float = RISK_FREE, rows: str = DEFAULT_ROWS) -> dict:
    """The constant-maturity grid from ``vol_surface.build``'s output ``grid``.

    ``rows="moneyness"`` (the default) - the PRIMARY grid is on fixed moneyness rows: ``{rows:
    [{m, pct, strike}] (strike = today's implied strike spot x m, 2 decimals), strikes (the same
    implied strikes), cells (IV % | None per row per horizon), exact (both steps exact), exact_k
    (the strike step exact), brackets ([[{lo: {date, dte, iv}, hi, w, k_lo: {strike, iv, lo_iv,
    hi_iv}, k_hi, wk} | None]] - lo / hi / w None when the horizon is a listed expiry for both
    strikes, k_lo / k_hi / wk None when K is a listed strike - then lo.iv / hi.iv are that strike's
    two legs; None for an exact or an empty cell), extra ([[{d, g, t, v, price} | None]]),
    nearest ([[{date, dte, strike} | None]] - the nearer listed expiry at the nearer listed
    strike), spot_row (rows below 100 %), scale, n_cells}``.

    ``rows="strike"`` - the primary grid is the by-strike one: ``{strikes, cells, exact,
    brackets ([[{lo: {date, dte, iv}, hi, w} | None]]), extra, nearest ([[{date, dte} | None]]),
    spot_row, scale, n_cells}``, ``rows`` None.

    Either way: ``by_strike`` (the by-strike grid, same keys), ``rows_mode``, ``horizons:
    [{days, years}]``, ``moneyness``, ``spot``, ``on``, ``side``, ``n_expiries_used``,
    ``method``. A row with no value at any horizon is dropped."""
    hs = norm_horizons(horizons)
    mode = norm_rows(rows)
    spot = float(grid.get("spot") or 0.0)
    side = grid.get("side") or vol_surface.DEFAULT_SIDE
    sg, n_used = _by_strike(grid, hs, r)
    common = {"horizons": [{"days": h, "years": round(h / 365.0, 4)} for h in hs], "moneyness": list(MONEYNESS),
              "spot": grid.get("spot"), "on": grid.get("on"), "side": side, "n_expiries_used": n_used,
              "method": METHOD, "rows_mode": mode, "by_strike": sg}
    if mode == "strike":
        return {**sg, **common, "rows": None}
    return {**_by_moneyness(sg, hs, spot, side, r), **common}


def surface_cm(sdb, symbol, day="latest", *, side=None, band=None, expiries=None, common=True, horizons=None,
               rows=None, root=None) -> dict:
    """The interpolated surface of ``symbol`` on ``day`` (``latest`` or ``YYYY-MM-DD``):
    ``interpolate``'s grid plus ``symbol, name, source, spot_src, as_of, traded, has_greeks,
    horizons_requested, band, common, exp_mode, n_expiries_listed``. Every listed expiry is read
    and used as a leg - ``expiries`` (all / monthly) is IGNORED here (v4.175: it belongs to the
    listed tab; a monthly-only list leaves the 30-day horizon without a near leg), and there is no
    days cap (a two-year horizon needs the expiries beyond it). ``rows`` picks the primary grid
    (``moneyness`` default, or ``strike``; ``ValueError`` otherwise). Raises ``SurfaceError`` with
    the reason, in plain words, when nothing can be derived."""
    mode = norm_rows(rows)
    sym = vol_surface._sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    hs = norm_horizons(horizons)
    o = vol_surface.norm_options(side, band, 0, "all")
    if str(day or "latest").strip().lower() == "latest":
        rows_in, meta = vol_surface.latest_rows(sdb, sym)
    else:
        rows_in, meta = vol_surface.history_rows(sdb, sym, day, band=o["band"], max_dte=0, root=root)
    grid = vol_surface.build(rows_in, meta.get("spot"), meta.get("on"), common=common, **o)
    if not grid["n_cells"]:
        raise SurfaceError("no %s option inside this window has an implied volatility - widen the strike "
                           "range" % sym)
    n_usable = sum(1 for e in grid["expiries"] if int(e["dte"]) >= MIN_LEG_DTE)
    out = interpolate(grid, hs, rows=mode)
    if not out["by_strike"]["n_cells"]:
        span = "%dd" % hs[0] if len(hs) == 1 else "%dd to %dd" % (hs[0], hs[-1])
        if n_usable < 2:
            raise SurfaceError("fewer than two listed %s expiries are at least %d days out, so no horizon (%s) can be "
                               "derived - widen the strikes" % (sym, MIN_LEG_DTE, span))
        raise SurfaceError("no two listed %s expiries at least %d days out bracket any of the horizons (%s) at the "
                           "same strike - pick other horizons or widen the strikes"
                           % (sym, MIN_LEG_DTE, span))
    as_of = meta.get("as_of")
    return {**out, "symbol": sym, "name": meta.get("name"), "source": meta["source"],
            "spot_src": meta.get("spot_src"),
            "as_of": as_of.replace(microsecond=0).isoformat() + "Z" if isinstance(as_of, _dt.datetime) else None,
            "traded": meta.get("traded"), "has_greeks": meta["source"] == "latest",
            "horizons_requested": list(hs), "band": grid["band"], "common": grid["common"],
            "exp_mode": grid["exp_mode"], "n_expiries_listed": len(grid["expiries"])}


__all__ = ["HORIZONS", "MONEYNESS", "ROWS", "MIN_LEG_DTE", "METHOD", "norm_horizons", "norm_rows", "interpolate",
           "surface_cm"]
