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


def ik(*a, **kw):
    """The by-strike grid as the primary one (v4.175: the default rows are fixed moneyness)."""
    return vol_cm.interpolate(*a, rows="strike", **kw)


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
    out = ik(g, (30, 60, 90, 180, 270, 365))
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
    out = ik(g, (60, 90, 180))
    assert out["cells"][0] == [pytest.approx(30.0, abs=1e-9)] * 3


def test_a_horizon_on_a_listed_expiry_is_that_contracts_own_iv_and_marked_exact():
    exps = [exp("2026-11-06", 30), exp("2026-12-06", 60), exp("2027-01-05", 90)]
    g = grid([100.0], exps, [[33.0, 31.0, 30.5]], extra=[[figs(0.5), figs(0.52), figs(0.53)]])
    out = ik(g, (30, 45, 60))
    assert out["cells"][0][0] == 33.0 and out["exact"][0][0] is True and out["brackets"][0][0] is None
    assert out["nearest"][0][0] == {"date": "2026-11-06", "dte": 30}
    assert out["extra"][0][0]["d"] == 0.5                       # its own greeks
    assert out["cells"][0][2] == 31.0 and out["exact"][0][2] is True
    assert out["exact"][0][1] is False and out["cells"][0][1] == pytest.approx(tv(33.0, 30, 31.0, 60, 45), abs=1e-9)
    # an exact expiry without an IV at that strike: bracketed by the neighbours instead
    g2 = grid([100.0], exps, [[33.0, None, 30.5]])
    out2 = ik(g2, (60,))
    assert out2["exact"][0][0] is False and out2["cells"][0][0] == pytest.approx(tv(33.0, 30, 30.5, 90, 60), abs=1e-9)


def test_an_expiry_under_seven_days_is_never_a_leg():
    exps = [exp("2026-10-13", 6), exp("2026-10-14", 7), exp("2026-12-06", 60)]
    out = ik(grid([100.0], exps, [[80.0, 50.0, 30.0]]), (30,))
    assert out["cells"][0][0] == pytest.approx(tv(50.0, 7, 30.0, 60, 30), abs=1e-9)     # 7 days is a leg
    exps6 = [exp("2026-10-13", 6), exp("2026-12-06", 60)]
    with_six = ik(grid([100.0], exps6, [[80.0, 30.0]]), (30,))
    assert with_six["strikes"] == [] and with_six["n_cells"] == 0                       # 6 days is not


def test_a_strike_missing_on_one_leg_is_blank_and_empty_rows_are_dropped():
    g = grid([90.0, 100.0, 110.0], EXPS,
             [[None, 34.0, None, 31.0, 29.0],          # 90: missing on the 72d leg
              [60.0, 33.0, 31.0, 30.0, 28.0],
              [None, None, 30.0, None, None]])         # 110: never both legs -> dropped
    out = ik(g, (60, 90, 180))
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
    out = ik(g, (60, 90, 180))
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
    out = ik(grid([100.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]]), (60, 90))
    assert out["extra"] == [[None, None]]


def test_the_side_decides_the_models_right():
    g = grid([95.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]], extra=[[figs(0.6)] * 5], side="call")
    out = ik(g, (60,))
    call = black_scholes(100.0, 95.0, 60 / 365.0, RISK_FREE, out["cells"][0][0] / 100, "call").price
    assert out["extra"][0][0]["price"] == pytest.approx(call, abs=1e-4)


def test_nearest_is_the_closer_leg():
    out = ik(grid([100.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]]), (50, 70, 90, 180))
    assert out["nearest"][0][0] == {"date": "2026-11-20", "dte": 44}      # 50: 6 days from 44, 22 from 72
    assert out["nearest"][0][1] == {"date": "2026-12-18", "dte": 72}      # 70: 2 days from 72
    assert out["nearest"][0][2] == {"date": "2027-01-15", "dte": 100}     # 90: 10 from 100, 18 from 72
    assert out["nearest"][0][3] == {"date": "2027-06-17", "dte": 253}     # 180: 73 from 253, 80 from 100
    tie = ik(grid([100.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0]]), (58,))
    assert tie["nearest"][0][0] == {"date": "2026-11-20", "dte": 44}      # 58: 14 either way - the shorter leg


def test_the_horizons_are_cleaned():
    # v4.175: out to two years - 15, 18, 21 and 24 months in calendar days after the year
    assert vol_cm.norm_horizons(None) == vol_cm.HORIZONS == (30, 60, 90, 180, 270, 365, 456, 547, 638, 730)
    assert vol_cm.norm_horizons("90, 30,30") == (30, 90)
    assert vol_cm.norm_horizons([365, "60"]) == (60, 365)
    assert vol_cm.norm_horizons("730,456,638,547") == (456, 547, 638, 730)
    assert vol_cm.norm_horizons("3650") == (3650,)
    assert vol_cm.norm_horizons("") == vol_cm.HORIZONS
    for bad in ("abc", "0", "-30", "12.5", "99999", [True]):
        with pytest.raises(ValueError):
            vol_cm.norm_horizons(bad)


# ─────────────────────────────── v4.175: rows at fixed moneyness ───────────────────────────────

EXPS2 = [exp("2026-11-20", 44), exp("2026-12-18", 72)]


def test_the_moneyness_step_time_then_strike_by_hand():
    """Two strikes (95, 105) on two expiries (44d, 72d), spot 100, horizon 60d: step 1 interpolates each
    strike in total variance, step 2 interpolates linearly in strike to K = 100 x m."""
    g = grid([95.0, 105.0], EXPS2, [[34.0, 32.0], [30.0, 29.0]],
             extra=[[figs(-0.30), figs(-0.40)], [figs(0.40, g=None), figs(0.45, g=None)]])
    out = vol_cm.interpolate(g, (60,))
    assert out["rows_mode"] == "moneyness" and out["moneyness"] == list(vol_cm.MONEYNESS)
    # 80 / 85 / 90 % and 110 / 115 / 120 % are outside 95 ... 105: no extrapolation, the rows are dropped
    assert [r["pct"] for r in out["rows"]] == [95.0, 97.5, 100.0, 102.5, 105.0]
    assert out["rows"][1] == {"m": 0.975, "pct": 97.5, "strike": 97.5}
    assert out["strikes"] == [95.0, 97.5, 100.0, 102.5, 105.0] and out["spot_row"] == 2
    t95, t105 = tv(34.0, 44, 32.0, 72, 60), tv(30.0, 44, 29.0, 72, 60)     # step 1, per strike
    wk = (105.0 - 97.5) / (105.0 - 95.0)                                   # step 2, at K = 97.5
    assert out["cells"][1][0] == pytest.approx(t95 * wk + t105 * (1 - wk), abs=1e-9)
    assert out["cells"][2][0] == pytest.approx((t95 + t105) / 2, abs=1e-9)
    assert out["cells"][0][0] == pytest.approx(t95, abs=1e-9) and out["cells"][4][0] == pytest.approx(t105, abs=1e-9)
    b = out["brackets"][1][0]
    assert b["lo"] == {"date": "2026-11-20", "dte": 44, "iv": None} and b["hi"]["date"] == "2026-12-18"
    assert b["w"] == pytest.approx((72 - 60) / (72 - 44), abs=1e-12) and b["wk"] == pytest.approx(wk, abs=1e-12)
    assert b["k_lo"] == {"strike": 95.0, "iv": pytest.approx(t95), "lo_iv": 34.0, "hi_iv": 32.0}
    assert b["k_hi"]["strike"] == 105.0 and b["k_hi"]["iv"] == pytest.approx(t105)
    assert out["exact"][1] == [False] and out["exact_k"][1] == [False]
    # greeks: linear in time per strike, then linear in strike; a greek missing on a strike stays None.
    # 97.5 is a put (below the price): the 105 CALL's figures are restated as the 105 put's first (put-call
    # parity, no dividend: delta - 1, theta + r K e^(-rT) / 365 per day; gamma and vega unchanged)
    w = (72 - 60) / (72 - 44)
    T = 60 / 365.0
    d95, d105 = -0.30 * w + -0.40 * (1 - w), 0.40 * w + 0.45 * (1 - w)
    x = out["extra"][1][0]
    assert x["d"] == pytest.approx(d95 * wk + (d105 - 1.0) * (1 - wk), abs=1e-12) and x["g"] is None
    t105p = -0.04 + RISK_FREE * 105.0 * math.exp(-RISK_FREE * T) / 365.0
    assert x["t"] == pytest.approx(-0.04 * wk + t105p * (1 - wk), abs=1e-12)
    assert x["v"] == pytest.approx(0.1, abs=1e-12)
    # 100 % is a call (at the price): the 95 PUT's delta becomes the 95 call's
    assert out["extra"][2][0]["d"] == pytest.approx(((d95 + 1.0) + d105) / 2, abs=1e-12)
    # the model price at strike K and the interpolated IV: a put below the price, a call at it
    assert x["price"] == pytest.approx(black_scholes(100.0, 97.5, T, RISK_FREE, out["cells"][1][0] / 100, "put").price,
                                       abs=1e-4)
    atm = out["extra"][2][0]["price"]
    assert atm == pytest.approx(black_scholes(100.0, 100.0, T, RISK_FREE, out["cells"][2][0] / 100, "call").price,
                                abs=1e-4)
    # the click: the nearer listed expiry (72d: 12 days from 60) at the nearer listed strike
    assert out["nearest"][1][0] == {"date": "2026-12-18", "dte": 72, "strike": 95.0}
    assert out["nearest"][2][0]["strike"] == 95.0                # 100: 5 either way - the lower strike
    assert out["nearest"][3][0]["strike"] == 105.0               # 102.5: 2.5 from 105
    assert out["scale"][0] <= out["scale"][1] and out["n_cells"] == 5


def test_one_side_only_keeps_the_legs_figures_as_they_are():
    g = grid([95.0, 105.0], EXPS2, [[34.0, 32.0], [30.0, 29.0]],
             extra=[[figs(0.60), figs(0.58)], [figs(0.40), figs(0.42)]], side="call")
    out = vol_cm.interpolate(g, (60,))
    w = (72 - 60) / (72 - 44)
    d95, d105 = 0.60 * w + 0.58 * (1 - w), 0.40 * w + 0.42 * (1 - w)
    assert out["extra"][1][0]["d"] == pytest.approx(d95 * 0.75 + d105 * 0.25, abs=1e-12)    # 97.5: both calls
    call = black_scholes(100.0, 97.5, 60 / 365.0, RISK_FREE, out["cells"][1][0] / 100, "call").price
    assert out["extra"][1][0]["price"] == pytest.approx(call, abs=1e-4)


def test_an_exact_strike_and_an_exact_horizon():
    exps = [exp("2026-11-06", 30), exp("2026-12-06", 60), exp("2027-01-05", 90)]
    g = grid([95.0, 100.0, 105.0], exps, [[35.0, 33.0, 32.0], [33.0, 31.0, 30.5], [31.0, 29.5, 29.0]],
             extra=[[figs(-0.3)] * 3, [figs(0.5), figs(0.52), figs(0.53)], [figs(0.3)] * 3])
    out = vol_cm.interpolate(g, (45, 60))
    r100 = [r["m"] for r in out["rows"]].index(1.0)
    # 60d on 100 %: a listed expiry AND a listed strike -> that contract's own IV, exact, no working
    assert out["cells"][r100][1] == 31.0 and out["exact"][r100][1] is True and out["exact_k"][r100][1] is True
    assert out["brackets"][r100][1] is None
    assert out["nearest"][r100][1] == {"date": "2026-12-06", "dte": 60, "strike": 100.0}
    assert out["extra"][r100][1]["d"] == 0.52
    # 45d on 100 %: the strike is listed, the horizon is not - exact_k only, the strike's own two legs
    assert out["exact"][r100][0] is False and out["exact_k"][r100][0] is True
    assert out["cells"][r100][0] == pytest.approx(tv(33.0, 30, 31.0, 60, 45), abs=1e-9)
    b = out["brackets"][r100][0]
    assert b["lo"] == {"date": "2026-11-06", "dte": 30, "iv": 33.0} and b["hi"]["iv"] == 31.0
    assert b["k_lo"] is None and b["k_hi"] is None and b["wk"] is None
    # 60d on 97.5 %: the horizon is listed, the strike is not - interpolated by strike only
    r975 = [r["m"] for r in out["rows"]].index(0.975)
    assert out["exact"][r975][1] is False and out["exact_k"][r975][1] is False
    assert out["cells"][r975][1] == pytest.approx(33.0 * 0.5 + 31.0 * 0.5, abs=1e-9)
    b2 = out["brackets"][r975][1]
    assert b2["lo"] is None and b2["w"] is None and b2["k_lo"]["lo_iv"] is None and b2["wk"] == pytest.approx(0.5)


def test_no_extrapolation_beyond_the_listed_strikes_and_a_missing_strike_is_skipped():
    """K is interpolated only between listed strikes that have a value in THAT column; beyond the lowest /
    highest one the cell is blank."""
    g = grid([95.0, 100.0, 105.0], EXPS2, [[34.0, 32.0], [None, None], [30.0, 29.0]])
    out = vol_cm.interpolate(g, (60, 30))                    # sorted: column 0 = 30d, column 1 = 60d
    pcts = [r["pct"] for r in out["rows"]]
    assert pcts == [95.0, 97.5, 100.0, 102.5, 105.0]         # 100 has no value: 95 and 105 bracket it
    r100 = pcts.index(100.0)
    t95, t105 = tv(34.0, 44, 32.0, 72, 60), tv(30.0, 44, 29.0, 72, 60)
    assert out["cells"][r100][1] == pytest.approx((t95 + t105) / 2, abs=1e-9)
    assert all(row[0] is None for row in out["cells"])        # 30d: before the first leg (44d)
    # a lone strike: nothing to interpolate between - only its own level (if any) has a value
    lone = vol_cm.interpolate(grid([97.0], EXPS2, [[34.0, 32.0]]), (60,))
    assert lone["rows"] == [] and lone["n_cells"] == 0 and lone["by_strike"]["n_cells"] == 1


def test_the_by_strike_grid_rides_along_and_the_rows_are_checked():
    g = grid([95.0, 105.0], EXPS2, [[34.0, 32.0], [30.0, 29.0]])
    out = vol_cm.interpolate(g, (60,))
    bs_ = out["by_strike"]
    assert set(bs_) >= {"strikes", "cells", "exact", "extra", "brackets", "nearest", "spot_row", "scale", "n_cells"}
    assert bs_["strikes"] == [95.0, 105.0] and bs_["cells"][0][0] == pytest.approx(tv(34.0, 44, 32.0, 72, 60))
    assert ik(g, (60,))["cells"] == bs_["cells"]
    assert vol_cm.norm_rows(None) == "moneyness" and vol_cm.norm_rows("Strike") == "strike"
    assert vol_cm.norm_rows("strikes") == "strike"
    with pytest.raises(ValueError):
        vol_cm.norm_rows("bad")


# ─────────────────────────────── against the screener DB / contract history ───────────────────────────────

def test_the_latest_interpolated_surface(sdb):
    """The fixture's last pass: expiries 44d and 72d, puts 30 % below the 101 spot, calls 25 % above;
    60d is bracketed (and a flat vol stays flat), 30d / 90d / 365d cannot be. v4.175: the primary rows
    are fixed moneyness levels (strike / price); the by-strike grid rides along in ``by_strike``."""
    g = vol_cm.surface_cm(sdb, "aapl")
    assert g["symbol"] == "AAPL" and g["name"] == "Apple Inc." and g["source"] == "latest"
    assert g["has_greeks"] is True and g["as_of"] == "2026-10-07T19:30:00Z"
    assert g["horizons_requested"] == list(vol_cm.HORIZONS) and g["rows_mode"] == "moneyness"
    assert g["n_expiries_used"] == 2 and g["n_expiries_listed"] == 2
    bs_ = g["by_strike"]
    assert bs_["strikes"] == [90.0, 100.0, 110.0]
    by = dict(zip(bs_["strikes"], bs_["cells"]))
    assert by[90.0][1] == pytest.approx(30.0, abs=1e-9) and by[110.0][1] == pytest.approx(25.0, abs=1e-9)
    assert by[90.0][0] is None and by[90.0][2] is None and by[90.0][5] is None and by[90.0][9] is None
    x = dict(zip(bs_["strikes"], bs_["extra"]))
    assert x[90.0][1]["d"] == pytest.approx(-0.6) and x[90.0][1]["price"] > 0      # greeks + the model price
    assert x[110.0][1]["d"] is None and x[110.0][1]["price"] > 0                    # no greeks stored at 110
    assert bs_["nearest"][0][1] == {"date": "2026-12-18", "dte": 72}               # 60: 12 from 72, 16 from 44
    # the moneyness rows: 101 x m inside 90 ... 110 -> 90 % to 105 %
    assert [r["pct"] for r in g["rows"]] == [90.0, 95.0, 97.5, 100.0, 102.5, 105.0]
    assert g["rows"][3] == {"m": 1.0, "pct": 100.0, "strike": 101.0} and g["spot_row"] == 3
    atm = g["cells"][3][1]                                       # 101: 0.9 on the 100 put (30), 0.1 on the 110 call (25)
    assert atm == pytest.approx(30.0 * 0.9 + 25.0 * 0.1, abs=1e-9)
    assert g["cells"][0][1] == pytest.approx(30.0, abs=1e-9)    # 90.9: between the 90 and 100 puts
    assert g["nearest"][3][1] == {"date": "2026-12-18", "dte": 72, "strike": 100.0}
    assert g["method"] == vol_cm.METHOD
    one = vol_cm.surface_cm(sdb, "AAPL", horizons="60")
    assert [h["days"] for h in one["horizons"]] == [60]
    strike = vol_cm.surface_cm(sdb, "AAPL", horizons="60", rows="strike")
    assert strike["rows_mode"] == "strike" and strike["rows"] is None and strike["strikes"] == [90.0, 100.0, 110.0]
    with pytest.raises(ValueError, match="rows"):
        vol_cm.surface_cm(sdb, "AAPL", rows="diagonal")
    with pytest.raises(SurfaceError, match="bracket"):
        vol_cm.surface_cm(sdb, "AAPL", horizons=(30, 90, 365))
    with pytest.raises(SurfaceError, match="ZZZZ"):
        vol_cm.surface_cm(sdb, "ZZZZ")
    with pytest.raises(SurfaceError, match="symbol"):
        vol_cm.surface_cm(sdb, "")


def test_the_expiries_setting_is_ignored_every_listed_expiry_is_a_leg(sdb, monkeypatch):
    """v4.175 (the user on the live site: no 30d column): with "monthly" passed through, a first monthly
    32 days out left the 30-day horizon without a near leg. The interpolated tab always reads every listed
    expiry, with no days cap; the Expiries setting belongs to the other tab."""
    from app.services import vol_surface

    seen = {}
    real = vol_surface.build

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(vol_surface, "build", spy)
    g = vol_cm.surface_cm(sdb, "AAPL", expiries="monthly", horizons="60")
    assert seen["expiries"] == "all" and seen["max_dte"] == 0 and g["exp_mode"] == "all"


def test_a_past_days_interpolated_surface_is_blank_without_a_closing_snapshot(sdb, hist):
    """v4.225 (the user: "left blank when no snapshot exists for that day"): never the IV solved from
    the last trade."""
    with pytest.raises(SurfaceError, match="no closing snapshot is saved for 2026-10-07"):
        vol_cm.surface_cm(sdb, "AAPL", ON.isoformat(), root=hist, side="call")
