"""One interpolated cell opened up (v4.209, `services/vol_cm_point.py`) and its day-by-day history
(`services/vol_cm_series.py`): the working the Interpolation page draws. Synthetic grids checked by hand;
the history against the surface tests' DB and Parquet fixtures. No network."""
from __future__ import annotations

import math

import pytest

from app.services import vol_cm_point, vol_cm_series
from app.services.vol_surface import SurfaceError
from tests.test_vol_cm import EXPS, exp, figs, grid, tv
from tests.test_vol_surface import ON, hist, sdb  # noqa: F401 - the DB / Parquet fixtures


def test_a_moneyness_cell_is_opened_up_by_hand():
    # strikes 95 / 105 at spot 100 -> level 97.5 % = 97.50 sits 25 % of the way from 95 to 105
    g = grid([95.0, 105.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0], [55.0, 30.0, 28.5, 27.0, 26.0]],
             extra=[[figs(-0.4)] * 5, [figs(0.45)] * 5])
    p = vol_cm_point.point(g, 90, m=0.975)
    assert p["fixed"] == "moneyness" and p["k"] == 97.5 and p["h"] == 90 and p["right"] == "P"   # below the price: the put
    iv95, iv105 = tv(32.0, 72, 31.0, 100, 90), tv(28.5, 72, 27.0, 100, 90)
    wk = (105 - 97.5) / (105 - 95)
    assert p["iv"] == pytest.approx(iv95 * wk + iv105 * (1 - wk), abs=1e-3)
    # step 1: the two legs 72d and 100d, the weight on the nearer, per leg strike
    t = p["time"]
    assert t["lo"]["dte"] == 72 and t["hi"]["dte"] == 100 and t["w"] == pytest.approx((100 / 365 - 90 / 365) / (100 / 365 - 72 / 365), abs=1e-3)
    assert [q["strike"] for q in t["per_strike"]] == [95.0, 105.0]
    assert t["per_strike"][0]["lo_iv"] == 32.0 and t["per_strike"][0]["hi_iv"] == 31.0
    assert t["per_strike"][0]["iv"] == pytest.approx(iv95, abs=1e-3) and t["per_strike"][0]["var"] == pytest.approx((iv95 / 100) ** 2 * 90 / 365, abs=1e-5)
    # step 2: the two strikes and the weight on the lower
    s = p["strike"]
    assert s["k_lo"]["strike"] == 95.0 and s["k_hi"]["strike"] == 105.0 and s["wk"] == pytest.approx(wk, abs=1e-3) and s["exact"] is False
    # the contract table around the cell: the leg expiries +- 1 (44d, 72d, 100d, 253d), both strikes, the cuts
    tb = p["table"]
    assert [e["dte"] for e in tb["expiries"]] == [44, 72, 100, 253] and [e["leg"] for e in tb["expiries"]] == [False, True, True, False]
    assert [x["strike"] for x in tb["strikes"]] == [95.0, 105.0] and all(x["leg"] for x in tb["strikes"])
    assert tb["cells"][0] == [34.0, 32.0, 31.0, 29.0] and tb["lo_col"] == 1 and tb["hi_col"] == 2
    assert tb["hpos"] == pytest.approx((90 - 72) / (100 - 72), abs=1e-3) and tb["kpos"] == pytest.approx(0.25, abs=1e-6)
    # the charts: the term structure per leg strike (every listed expiry; the 5-day one is not usable) and the smile
    assert [tr["strike"] for tr in p["term"]] == [95.0, 105.0]
    pts = p["term"][0]["points"]
    assert [q["dte"] for q in pts] == [5, 44, 72, 100, 253] and [q["leg"] for q in pts] == [False, False, True, True, False]
    assert pts[0]["usable"] is False and pts[2]["var"] == pytest.approx(0.32 ** 2 * 72 / 365, abs=1e-5)
    assert p["term"][0]["at"]["iv"] == pytest.approx(iv95, abs=1e-3)
    sm = p["smile"]
    assert [q["strike"] for q in sm["points"]] == [95.0, 105.0] and sm["K"] == 97.5 and sm["at"] == pytest.approx(p["iv"], abs=1e-3)
    # the greeks: four legs (two expiries x two strikes), the cell's by the same weights; the put at 105 restated
    gk = p["greeks"]
    assert len(gk["legs"]) == 4 and gk["has_greeks"] is True and p["n_contracts"] == 4
    assert gk["result"]["price"] is not None and gk["result"]["g"] == pytest.approx(0.02, abs=1e-9)


def test_a_strike_cell_an_exact_cell_and_nothing_to_interpolate():
    g = grid([95.0, 105.0], EXPS, [[60.0, 34.0, 32.0, 31.0, 29.0], [55.0, 30.0, 28.5, 27.0, 26.0]])
    p = vol_cm_point.point(g, 90, k=105)
    assert p["fixed"] == "strike" and p["strike"] is None and p["iv"] == pytest.approx(tv(28.5, 72, 27.0, 100, 90), abs=1e-3)
    assert [tr["strike"] for tr in p["term"]] == [105.0] and p["table"]["strikes"][0]["strike"] == 95.0   # the row above rides along
    # a horizon on a listed expiry at a listed strike: exact, its own IV, no weights
    e = vol_cm_point.point(g, 72, m=1.05)
    assert e["exact"] is True and e["iv"] == 28.5 and e["time"]["exact"] is True and e["strike"]["exact"] is True
    # beyond the listed expiries: nothing, with the reason
    n = vol_cm_point.point(g, 400, m=0.975)
    assert n["iv"] is None and "nothing is extrapolated" in n["why"] and n["table"] is None
    with pytest.raises(ValueError):
        vol_cm_point.point(g, 90)
    with pytest.raises(ValueError):
        vol_cm_point.point(g, 90, m=0.975, k=95)
    with pytest.raises(SurfaceError):
        vol_cm_point.point(grid([95.0], EXPS, [[1, 1, 1, 1, 1]], spot=0.0), 90, m=1.0)


def test_the_inputs_are_cleaned():
    assert vol_cm_point.norm_m("95") == 0.95 and vol_cm_point.norm_m(0.975) == 0.975 and vol_cm_point.norm_m("102.5%") == 1.025
    assert vol_cm_point.norm_h("90") == 90 and vol_cm_point.norm_k("420") == 420.0
    for bad in ("abc", 0, 2000):
        with pytest.raises(ValueError):
            vol_cm_point.norm_h(bad)
    for bad in ("x", 0.1, 3.5):
        with pytest.raises(ValueError):
            vol_cm_point.norm_m(bad)
    with pytest.raises(ValueError):
        vol_cm_point.norm_k(-1)


def test_the_point_for_the_latest_pass_and_the_series_over_the_history(sdb, hist):
    p = vol_cm_point.point_for(sdb, "aapl", "latest", 60, m=0.95)
    assert p["symbol"] == "AAPL" and p["source"] == "latest" and p["h"] == 60 and p["k"] == pytest.approx(101.0 * 0.95, abs=1e-6)
    assert p["iv"] is not None and p["time"]["lo"]["dte"] < 60 < p["time"]["hi"]["dte"]
    # the history: a row per converted session, newest first, the stats over the window
    vol_cm_series.clear_cache()
    s = vol_cm_series.series(sdb, "AAPL", 60, m=0.95, window_days=365, root=hist)
    assert s["symbol"] == "AAPL" and s["fixed"] == "moneyness" and s["days"] >= 1 and len(s["rows"]) == s["days"]
    assert s["rows"][0]["on"] >= s["rows"][-1]["on"] and s["window"][1] == s["rows"][0]["on"]
    done = [r for r in s["rows"] if r["iv"] is not None]
    assert done, [r["why"] for r in s["rows"]]
    r0 = done[0]
    assert r0["K"] == pytest.approx(r0["spot"] * 0.95, abs=0.01) and r0["lo"] and r0["hi"] and 0 <= r0["w"] <= 1 and r0["n"] >= 2
    st = s["stats"]
    assert st["n"] == len(done) and st["lo"] <= st["newest"] <= st["hi"]
    # cached: the same object comes back
    assert vol_cm_series.series(sdb, "AAPL", 60, m=0.95, window_days=365, root=hist) is s
    with pytest.raises(ValueError):
        vol_cm_series.series(sdb, "AAPL", 60, window_days=365, root=hist)
    with pytest.raises(SurfaceError):
        vol_cm_series.series(sdb, "SPX", 60, m=1.0, window_days=365, root=hist)        # an index: no stock close
