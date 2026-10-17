"""The option calculator engine (OPTION_CALCULATOR_DESIGN.md §4, §6) against the user's
Excel workbook *Option Pricing – Clean Sample 20261017.xlsm*. The expected figures are
the workbook's own cached cell values (read with openpyxl on 2026-10-17), so a pass
means the engine IS the sheet."""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.services.option_calc import (Leg, Scenario, bs_delta, bs_price, evaluate, ncdf,
                                      price_grid)

# the sheet's sample: long 100 C / short 150 C, 120 DTE, IV 20 %, r 4.8 %, q 0, spot 100,
# decay 85 days (-> 35 DTE on the target date), target 175, worst 50, 13 sets
LEGS = [Leg("c", 100, 120, 0.20, +1), Leg("c", 150, 120, 0.20, -1)]
SCEN = Scenario(spot=100, target=175, worst=50, decay_days=85, sets=13, r=0.048)


@pytest.fixture(scope="module")
def res():
    return evaluate(LEGS, SCEN)


def test_per_leg_prices_match_sheet_row_18(res):
    a, b = res.legs
    assert a.price_entry == pytest.approx(5.362314330231001, abs=1e-9)      # H18
    assert b.price_entry == pytest.approx(0.0012146499511057063, abs=1e-9)  # I18
    assert a.price_target == pytest.approx(2.7010859154191635, abs=1e-9)    # M18
    assert a.delta_entry == pytest.approx(0.5772839315034144, abs=1e-9)     # H19
    assert a.d1 == pytest.approx(0.19494994088753861, abs=1e-9)             # H15
    assert a.d2 == pytest.approx(0.08027350507133943, abs=1e-9)             # H16
    assert a.dte_target == 35 and b.dte_target == 35                        # M6, N6


def test_sheet_put_leg_row_18():
    # the sheet's inert leg 4: a 200 put, 120 DTE -> K18 = 96.868608..., P18 at 35 DTE
    put = Leg("p", 200, 120, 0.20, 1)
    r = evaluate([put], SCEN)
    assert r.legs[0].price_entry == pytest.approx(96.86860857128832, abs=1e-9)
    assert r.legs[0].price_target == pytest.approx(99.0815673294957, abs=1e-9)
    assert r.legs[0].delta_entry == pytest.approx(-0.9999999975335987, abs=1e-9)


def test_decision_indicators_match_sheet(res):
    assert res.entry_value == pytest.approx(5.361099680279896, abs=1e-9)      # C10
    assert res.decayed_value == pytest.approx(2.701085915365282, abs=1e-9)    # C14
    assert res.target_value == pytest.approx(49.753938101096814, abs=1e-9)    # C11
    assert abs(res.worst_value) < 1e-12                                       # C12
    assert res.gain_at_target == pytest.approx(44.392838420816915, abs=1e-9)  # D11
    assert res.loss_at_worst == pytest.approx(-5.361099680279896, abs=1e-9)   # D12
    assert res.reward_over_risk == pytest.approx(8.280547101951894, abs=1e-9) # C25
    assert res.roi == pytest.approx(8.280547101951894, abs=1e-9)              # E11 / C26
    assert res.total_decay == pytest.approx(-2.6600137649146136, abs=1e-9)    # D14
    assert res.net_delta_target == pytest.approx(0.5419251679292648, abs=1e-9)  # Q25
    assert res.net_delta_entry == pytest.approx(0.5768662171661734, abs=1e-9)   # W82


def test_dollar_totals_match_sheet(res):
    assert res.total_gains == pytest.approx(57710.68994706199, abs=1e-6)     # C29
    assert res.total_risk == pytest.approx(-6969.429584363865, abs=1e-6)     # C30
    assert res.total_outlay == pytest.approx(6969.429584363864, abs=1e-6)    # C31


def test_breakeven_agrees_with_sheet_to_a_cent(res):
    # the sheet interpolates linearly on a $2 ladder (C24 = 104.016386); the engine
    # bisects the exact curve, so it is the true root, a hair away from the sheet
    assert len(res.breakevens) == 1
    be = res.breakevens[0]
    assert be == pytest.approx(104.016386266629965, abs=0.02)
    # and it really is a root: the target-date value there equals the entry cost
    from app.services.option_calc import net_value
    assert float(net_value(LEGS, be, SCEN, at_target=True)) == pytest.approx(res.entry_value, abs=1e-6)


def test_curves_and_grid(res):
    g = np.asarray(res.grid)
    assert g[0] < 50 < 100 < 175 < g[-1]            # covers worst, spot, target
    assert all(p in set(res.grid) for p in (50.0, 100.0, 150.0, 175.0))  # anchors on the grid
    assert np.all(np.diff(g) > 0)
    net = np.asarray(res.net_curve_target)
    assert net.shape == g.shape and len(res.leg_curves_target) == 2
    assert np.allclose(np.sum(res.leg_curves_target, axis=0), net)
    # a bull call spread's target-date curve is monotonic and bounded by the width
    assert np.all(np.diff(net) >= -1e-9) and net[-1] < 50.0
    assert res.max_profit == pytest.approx(net[-1] - res.entry_value)
    assert res.max_loss == pytest.approx(-res.entry_value, abs=1e-6)
    assert res.roi_on_max_loss == pytest.approx(res.roi)


def test_flat_iv_override_and_per_leg_iv():
    skew = [Leg("c", 100, 120, 0.25, +1), Leg("c", 150, 120, 0.35, -1)]
    flat = evaluate(skew, Scenario(100, 175, 50, 85, r=0.048, iv_override=0.20))
    assert flat.entry_value == pytest.approx(5.361099680279896, abs=1e-9)   # same as the sheet
    own = evaluate(skew, Scenario(100, 175, 50, 85, r=0.048))
    assert own.entry_value != pytest.approx(flat.entry_value)
    assert own.legs[0].iv == 0.25 and own.legs[1].iv == 0.35


def test_expired_leg_is_intrinsic_on_target_date():
    r = evaluate([Leg("c", 100, 30, 0.3, 1)], Scenario(110, 120, 90, 30, r=0.048))
    assert r.legs[0].dte_target == 0
    assert r.decayed_value == pytest.approx(10.0)
    assert r.target_value == pytest.approx(20.0)
    assert r.worst_value == 0.0
    assert r.legs[0].delta_target == 1.0


def test_credit_structure_signs_and_two_breakevens():
    # short 100 straddle, 60 DTE, looked at 30 days on: a credit, two breakevens, R:R None
    # when the "worst" price is actually inside the profit zone
    legs = [Leg("c", 100, 60, 0.3, -1), Leg("p", 100, 60, 0.3, -1)]
    r = evaluate(legs, Scenario(100, 100, 101, 30, r=0.04))
    assert r.entry_value < 0                      # a credit
    assert r.total_outlay < 0
    assert len(r.breakevens) == 2
    lo, hi = r.breakevens
    assert lo < 100 < hi
    assert r.gain_at_target > 0                   # stays put -> decay is ours
    assert r.loss_at_worst > 0 and r.reward_over_risk is None
    assert r.max_loss < 0 and r.roi_on_max_loss is not None
    # the iron-fly-style bound: profit never exceeds the credit
    assert r.max_profit <= -r.entry_value + 1e-9


def test_bs_price_edges_and_parity():
    S = np.array([0.0, 50.0, 100.0, 150.0])
    c = bs_price(S, 100, 0.5, 0.2, "C", 0.05, 0.01)
    p = bs_price(S, 100, 0.5, 0.2, "P", 0.05, 0.01)
    assert c[0] == 0.0 and p[0] == pytest.approx(100 * math.exp(-0.025))
    # put-call parity with dividends
    assert np.allclose(c[1:] - p[1:], S[1:] * math.exp(-0.005) - 100 * math.exp(-0.025))
    # zero vol -> discounted forward intrinsic; zero time -> intrinsic
    assert bs_price(100.0, 90, 1.0, 0.0, "C", 0.05) == pytest.approx(100 - 90 * math.exp(-0.05))
    assert bs_price(100.0, 90, 0.0, 0.3, "C") == 10.0
    assert bs_price(100.0, 90, 0.0, 0.3, "P") == 0.0
    # delta bounds
    d = bs_delta(S[1:], 100, 0.5, 0.2, "C", 0.05, 0.0)
    assert np.all((d >= 0) & (d <= 1)) and d[0] < d[1] < d[2]
    assert bs_delta(S[1:], 100, 0.5, 0.2, "P", 0.05, 0.0)[0] == pytest.approx(d[0] - 1)


def test_ncdf_is_exact():
    assert ncdf(0.0) == 0.5
    assert ncdf(1.959963984540054) == pytest.approx(0.975, abs=1e-12)
    assert np.allclose(ncdf(np.array([-40.0, 40.0])), [0.0, 1.0])


def test_input_validation():
    with pytest.raises(ValueError):
        Leg("x", 100, 30, 0.2)
    with pytest.raises(ValueError):
        Leg("c", 0, 30, 0.2)
    with pytest.raises(ValueError):
        Scenario(0, 1, 1, 1)
    with pytest.raises(ValueError):
        evaluate([], SCEN)
    with pytest.raises(ValueError):
        evaluate([Leg("c", 100, 30, 0.2)] * 5, SCEN)
    # dict legs are accepted
    r = evaluate([{"right": "call", "strike": 100, "dte": 120, "iv": 0.2, "qty": 1}], SCEN)
    assert r.legs[0].right == "C"


def test_grid_is_spot_relative():
    big = price_grid([Leg("c", 500, 60, 0.3, 1)], Scenario(500, 560, 470, 30))
    assert big[0] < 470 and big[-1] > 560
    assert (big[-1] - big[0]) > 0.2 * 500        # wider than a fixed $80 ladder
    small = price_grid([Leg("c", 10, 60, 0.3, 1)], Scenario(10, 12, 9, 30))
    assert small[0] > 0 and (small[-1] - small[0]) < 10


def test_without_curves_is_light():
    r = evaluate(LEGS, SCEN, with_curves=False)
    assert r.grid == [] and r.net_curve_target == []
    assert r.reward_over_risk == pytest.approx(8.280547101951894, abs=1e-9)
