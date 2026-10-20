"""The interpolated (constant-maturity) volatility surface (v4.171): ``app/services/vol_cm.py``.
``interpolate`` is fed synthetic ``vol_surface.build``-shaped grids (IVs in PERCENT); the
arithmetic is checked against a hand computation of linear-in-total-variance interpolation.
``surface_cm`` runs against the screener-DB and contract-history fixtures of
``tests/test_vol_surface.py``. No network."""
from __future__ import annotations

import math

import pytest

from app.services import vol_cm
from app.services.black_scholes import black_scholes
from app.services.opt_constants import RISK_FREE
from app.services.vol_surface import SurfaceError

# the DB / Parquet fixtures of the listed surface's tests (pytest finds fixtures imported here)
from tests.test_vol_surface import ON, hist, sdb  # noqa: F401


def exp(date, dte):
    return {"date": date, "dte": dte, "years": round(dte / 365.0, 4), "monthly": True}


def figs(d, g=0.02, t=-0.04, v=0.1):
    return {"d": d, "g": g, "t": t, "v": v, "last": 1.0, "price": 1.1, "chg": 0.0, "oi": 10, "vol": 1}


def grid(strikes, expiries, cells, *, extra=None, spot=100.0, side="otm"):
    return {"strikes": strikes, "expiries": expiries, "cells": cells, "spot": spot, "on": "2026-10-07",
            "side": side, "extra": extra if extra is not None else [[None] * len(expiries) for _ in strikes]}


EXPS = [exp("2026-10-12", 5), exp("2026-11-20", 44), exp("2026-12-18", 72), exp("2027-01-15", 100),
        exp("2027-06-17", 253)]


def tv(s1, d1, s2, d2, h):
    """Hand computation: linear in total variance, IVs in percent, T = days / 365."""
    T1, T2, T = d1 / 365.0, d2 / 365.0, h / 365.0
    w = (T2 - T) / (T2 - T1)
    var = (s1 / 100) ** 2 * T1 * w + (s2 / 100) ** 2 * T2 * (1 - w)
    return math.sqrt(var / T) * 100


def test_bracketing_is_linear_in_total_variance():
    g = grid([95.0, 105.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0], [55.0, 30.0, 28.5, 27.0, 26.0]])
    out = vol_cm.interpolate(g, (30, 60, 90, 180, 270, 365))
    assert [h["days"] for h in out["horizons"]] == [30, 60, 90, 180, 270, 365]
    assert out["horizons"][1]["years"] == round(60 / 365, 4)
    r95 = out["cells"][0]
    assert r95[1] == pytest.approx(tv(34.0, 44, 32.0, 72, 60), abs=1e-9)
    assert r95[2] == pytest.approx(tv(32.0, 72, 31.0, 100, 90), abs=1e-9)
    assert r95[3] == pytest.approx(tv(31.0, 100, 29.0, 253, 180), abs=1e-9)
    # no extrapolation: 30d is before the first usable expiry (44d; the 5-day one is not a leg),
    # 270d and 365d are beyond the last (253d)
    assert r95[0] is None and r95[4] is None and r95[5] is None
    b = out["brackets"][0][1]
    assert b["lo"] == {"date": "2026-11-20", "dte": 44, "iv": 34.0}
    assert b["hi"] == {"date": "2026-12-18", "dte": 72, "iv": 32.0}
    assert b["w"] == pytest.approx((72 - 60) / (72 - 44), abs=1e-12)
    assert out["exact"][0] == [False] * 6
    assert out["method"].startswith("linear in total variance") and "no extrapolation" in out["method"]
    assert out["n_cells"] == 6 and out["n_expiries_used"] == 4
    assert out["scale"][0] <= out["scale"][1]
    assert out["spot_row"] == 1 and out["spot"] == 100.0 and out["side"] == "otm" and out["on"] == "2026-10-07"


def test_a_constant_volatility_stays_constant():
    g = grid([100.0], EXPS, [[30.0] * 5])
    out = vol_cm.interpolate(g, (60, 90, 180))
    assert out["cells"][0] == [pytest.approx(30.0, abs=1e-9)] * 3


def test_a_horizon_on_a_listed_expiry_is_that_contracts_own_iv_and_marked_exact():
    exps = [exp("2026-11-06", 30), exp("2026-12-06", 60), exp("2027-01-05", 90)]
    g = grid([100.0], exps, [[33.0, 31.0, 30.5]], extra=[[figs(0.5), figs(0.52), figs(0.53)]])
    out = vol_cm.interpolate(g, (30, 45, 60))
    assert out["cells"][0][0] == 33.0 and out["exact"][0][0] is True and out["brackets"][0][0] is None
    assert out["nearest"][0][0] == {"date": "2026-11-06", "dte": 30}
    assert out["extra"][0][0]["d"] == 0.5                       # its own greeks
    assert out["cells"][0][2] == 31.0 and out["exact"][0][2] is True
    assert out["exact"][0][1] is False and out["cells"][0][1] == pytest.approx(tv(33.0, 30, 31.0, 60, 45), abs=1e-9)
    # an exact expiry without an IV at that strike: bracketed by the neighbours instead
    g2 = grid([100.0], exps, [[33.0, None, 30.5]])
    out2 = vol_cm.interpolate(g2, (60,))
    assert out2["exact"][0][0] is False and out2["cells"][0][0] == pytest.approx(tv(33.0, 30, 30.5, 90, 60), abs=1e-9)


def test_an_expiry_under_seven_days_is_never_a_leg():
    exps = [exp("2026-10-13", 6), exp("2026-10-14", 7), exp("2026-12-06", 60)]
    out = vol_cm.interpolate(grid([100.0], exps, [[80.0, 50.0, 30.0]]), (30,))
    assert out["cells"][0][0] == pytest.approx(tv(50.0, 7, 30.0, 60, 30), abs=1e-9)     # 7 days is a leg
    exps6 = [exp("2026-10-13", 6), exp("2026-12-06", 60)]
    with_six = vol_cm.interpolate(grid([100.0], exps6, [[80.0, 30.0]]), (30,))
    assert with_six["strikes"] == [] and with_six["n_cells"] == 0                       # 6 days is not


def test_a_strike_missing_on_one_leg_is_blank_and_empty_rows_are_dropped():
    g = grid([90.0, 100.0, 110.0], EXPS,
             [[None, 34.0, None, 31.0, 29.0],          # 90: missing on the 72d leg
              [60.0, 33.0, 31.0, 30.0, 28.0],
              [None, None, 30.0, None, None]])         # 110: never both legs -> dropped
    out = vol_cm.interpolate(g, (60, 90, 180))
    assert out["strikes"] == [90.0, 100.0]
    r90 = out["cells"][0]
    assert r90[0] is None and r90[1] is None                       # 60d and 90d need the 72d leg
    assert r90[2] == pytest.approx(tv(31.0, 100, 29.0, 253, 180), abs=1e-9)
    assert out["brackets"][0][0] is None and out["nearest"][0][0] is None
    assert len(out["extra"]) == len(out["cells"]) == len(out["exact"]) == len(out["nearest"]) == 2


def test_the_greeks_are_linear_in_time_and_null_when_a_leg_has_none():
    g = grid([95.0, 105.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0], [55.0, 30.0, 28.5, 27.0, 26.0]],
             extra=[[figs(-0.2), figs(-0.3), figs(-0.4, g=None), figs(-0.45), figs(-0.5)],
                    [None, figs(0.4), figs(0.45), None, figs(0.5)]])
    out = vol_cm.interpolate(g, (60, 90, 180))
    w60 = (72 - 60) / (72 - 44)
    x = out["extra"][0][0]
    assert x["d"] == pytest.approx(-0.3 * w60 + -0.4 * (1 - w60), abs=1e-12)
    assert x["t"] == pytest.approx(-0.04, abs=1e-12) and x["g"] is None          # gamma missing on one leg
    assert out["extra"][1][1]["d"] is None                                     # 90d: the 100d leg has no figures
    # the model price at the interpolated IV: a put below the spot, a call above it
    T = 60 / 365.0
    put = black_scholes(100.0, 95.0, T, RISK_FREE, out["cells"][0][0] / 100, "put").price
    call = black_scholes(100.0, 105.0, T, RISK_FREE, out["cells"][1][0] / 100, "call").price
    assert x["price"] == pytest.approx(put, abs=1e-4)
    assert out["extra"][1][0]["price"] == pytest.approx(call, abs=1e-4)
    assert set(x) == {"d", "g", "t", "v", "price"}


def test_without_figures_the_extra_cells_are_null():
    """A past day's grid carries no figures: every extra cell is null (no greeks, no price)."""
    out = vol_cm.interpolate(grid([100.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]]), (60, 90))
    assert out["extra"] == [[None, None]]


def test_the_side_decides_the_models_right():
    g = grid([95.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]], extra=[[figs(0.6)] * 5], side="call")
    out = vol_cm.interpolate(g, (60,))
    call = black_scholes(100.0, 95.0, 60 / 365.0, RISK_FREE, out["cells"][0][0] / 100, "call").price
    assert out["extra"][0][0]["price"] == pytest.approx(call, abs=1e-4)


def test_nearest_is_the_closer_leg():
    out = vol_cm.interpolate(grid([100.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]]), (50, 70, 90, 180))
    assert out["nearest"][0][0] == {"date": "2026-11-20", "dte": 44}      # 50: 6 days from 44, 22 from 72
    assert out["nearest"][0][1] == {"date": "2026-12-18", "dte": 72}      # 70: 2 days from 72
    assert out["nearest"][0][2] == {"date": "2027-01-15", "dte": 100}     # 90: 10 from 100, 18 from 72
    assert out["nearest"][0][3] == {"date": "2027-06-17", "dte": 253}     # 180: 73 from 253, 80 from 100
    tie = vol_cm.interpolate(grid([100.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]]), (58,))
    assert tie["nearest"][0][0] == {"date": "2026-11-20", "dte": 44}      # 58: 14 either way - the shorter leg


def test_the_horizons_are_cleaned():
    assert vol_cm.norm_horizons(None) == vol_cm.HORIZONS == (30, 60, 90, 180, 270, 365)
    assert vol_cm.norm_horizons("90, 30,30") == (30, 90)
    assert vol_cm.norm_horizons([365, "60"]) == (60, 365)
    assert vol_cm.norm_horizons("") == vol_cm.HORIZONS
    for bad in ("abc", "0", "-30", "12.5", "99999", [True]):
        with pytest.raises(ValueError):
            vol_cm.norm_horizons(bad)


# ─────────────────────────────── against the screener DB / contract history ───────────────────────────────

def test_the_latest_interpolated_surface(sdb):
    """The fixture's last pass: expiries 44d and 72d, puts 30 % below the 101 spot, calls 25 % above;
    60d is bracketed (and a flat vol stays flat), 30d / 90d / 365d cannot be."""
    g = vol_cm.surface_cm(sdb, "aapl")
    assert g["symbol"] == "AAPL" and g["name"] == "Apple Inc." and g["source"] == "latest"
    assert g["has_greeks"] is True and g["as_of"] == "2026-10-07T19:30:00Z"
    assert g["horizons_requested"] == [30, 60, 90, 180, 270, 365]
    assert g["strikes"] == [90.0, 100.0, 110.0] and g["n_expiries_used"] == 2 and g["n_expiries_listed"] == 2
    by = dict(zip(g["strikes"], g["cells"]))
    assert by[90.0][1] == pytest.approx(30.0, abs=1e-9) and by[110.0][1] == pytest.approx(25.0, abs=1e-9)
    assert by[90.0][0] is None and by[90.0][2] is None and by[90.0][5] is None
    x = dict(zip(g["strikes"], g["extra"]))
    assert x[90.0][1]["d"] == pytest.approx(-0.6) and x[90.0][1]["price"] > 0      # greeks + the model price
    assert x[110.0][1]["d"] is None and x[110.0][1]["price"] > 0                    # no greeks stored at 110
    assert g["nearest"][0][1] == {"date": "2026-12-18", "dte": 72}                 # 60: 12 from 72, 16 from 44
    assert g["method"] == vol_cm.METHOD
    one = vol_cm.surface_cm(sdb, "AAPL", horizons="60")
    assert [h["days"] for h in one["horizons"]] == [60]
    with pytest.raises(SurfaceError, match="bracket"):
        vol_cm.surface_cm(sdb, "AAPL", horizons=(30, 90, 365))
    with pytest.raises(SurfaceError, match="ZZZZ"):
        vol_cm.surface_cm(sdb, "ZZZZ")
    with pytest.raises(SurfaceError, match="symbol"):
        vol_cm.surface_cm(sdb, "")


def test_a_past_days_interpolated_surface_has_no_greeks(sdb, hist):
    g = vol_cm.surface_cm(sdb, "AAPL", ON.isoformat(), root=hist, side="call")
    assert g["source"] == "history" and g["has_greeks"] is False
    assert g["cells"] and all(row[1] == pytest.approx(30.0, abs=0.05) for row in g["cells"])
    assert all(x is None for row in g["extra"] for x in row)
