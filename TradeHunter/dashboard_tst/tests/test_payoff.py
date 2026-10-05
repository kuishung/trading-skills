"""Options module, the payoff engine (part_C_chart_engines.md C6.3, OPTIONS_MODULE_DESIGN.md
II.2.9): the identities every number on the risk / reward pane rests on, checked
on the golden LRCX pick (the Nov 20 330/320 at 2.10 - II.2.19), the ISRG buy_call
worked example (C5.2), the C5.3 calendar and a few synthetic structures. No DB,
no network, no fixture file: every chain here is a handful of legs and
Black-Scholes.

Run from dashboard_tst/:  py -m pytest tests/test_payoff.py -q
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # dashboard_tst/ -> `from app...`

from app.services import bull_put, payoff as po          # noqa: E402
from app.services.black_scholes import black_scholes, norm_cdf   # noqa: E402
from app.services.opt_constants import LOSS_STOP_FRACTION, STOP_IV_BUMP   # noqa: E402

AS_OF = "2026-10-03"

# ---- the golden LRCX pick (II.2.19): Nov 20 330/320 put spread at 2.10 ------------
LRCX_SPOT, LRCX_ATR, LRCX_STOP = 349.20, 11.54, 336.2
LRCX_LEGS = [
    {"expiry": "2026-11-20", "right": "P", "strike": 330.0, "side": "sell", "qty": 1, "price": 5.70,
     "bid": 5.65, "ask": 5.75, "iv": 0.46, "delta": -0.250, "oi": 2140, "volume": 412},
    {"expiry": "2026-11-20", "right": "P", "strike": 320.0, "side": "buy", "qty": 1, "price": 3.60,
     "bid": 3.55, "ask": 3.65, "iv": 0.47, "delta": -0.174, "oi": 1630, "volume": 230},
]
LRCX_LEVELS = ({"x": 340.0, "label": "support 340", "kind": "support"},
               {"x": 336.1, "label": "trend line at expiry 336.1", "kind": "trend_line"})

# ---- the ISRG buy_call worked example (C5.2): Dec 18 400 call at 34.30 ------------
ISRG_SPOT, ISRG_ATR, ISRG_STOP, ISRG_TARGET = 405.81, 11.54, 394.27, 428.89
ISRG_LEGS = [{"expiry": "2026-12-18", "right": "C", "strike": 400.0, "side": "buy", "qty": 1, "price": 34.30}]


def lrcx(**kw):
    args = dict(strategy="bull_put", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF, chart_stop=LRCX_STOP,
                levels=LRCX_LEVELS, symbol="LRCX")
    args.update(kw)
    return po.build(LRCX_LEGS, **args)


def isrg(**kw):
    args = dict(strategy="buy_call", spot=ISRG_SPOT, atr=ISRG_ATR, as_of=AS_OF, chart_stop=ISRG_STOP,
                target=ISRG_TARGET, premium_stop_pct=50, symbol="ISRG")
    args.update(kw)
    return po.build(ISRG_LEGS, **args)


def calendar_legs():
    """C5.3: XYZ at 100, sell Oct 30 100C (27 DTE, IV 34%) / buy Dec 4 100C (62 DTE, IV 30%)."""
    p1 = black_scholes(100.0, 100.0, 27 / 365, po.RISK_FREE, 0.34, "call").price
    p2 = black_scholes(100.0, 100.0, 62 / 365, po.RISK_FREE, 0.30, "call").price
    return [{"expiry": "2026-10-30", "right": "C", "strike": 100.0, "side": "sell", "qty": 1, "price": round(p1, 2), "iv": 0.34},
            {"expiry": "2026-12-04", "right": "C", "strike": 100.0, "side": "buy", "qty": 1, "price": round(p2, 2), "iv": 0.30}]


def calibrated(legs, spot):
    return po.calibrate([po.Leg.from_dict(l) for l in legs], spot, AS_OF)


def today(legs_c, S):
    return po.pnl(legs_c, S, 0, AS_OF)


def marker(res, kind):
    return [m for m in res["markers"] if m["kind"] == kind]


def hline(res, kind):
    return [h for h in res["hlines"] if h["kind"] == kind]


# ------------------------------------------------------------- model
def test_put_call_parity():
    S, K, T, r, sigma = 405.81, 400.0, 76 / 365, po.RISK_FREE, 0.405
    c = black_scholes(S, K, T, r, sigma, "call").price
    p = black_scholes(S, K, T, r, sigma, "put").price
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 1e-9
    assert abs((c - p) - 9.1277) < 0.001         # the measured figure of C6.3


def test_implied_vol_round_trip():
    S, K, T = 349.2, 330.0, 48 / 365
    price = black_scholes(S, K, T, po.RISK_FREE, 0.2797, "put").price
    assert abs(po.implied_vol(price, S, K, T, "put") - 0.2797) < 1e-6
    assert po.implied_vol(0.0, S, K, T, "put") is None
    assert po.implied_vol(50.0, 405.81, 300.0, T, "call") is None       # under intrinsic: a stale quote
    assert po.implied_vol(5.0, S, K, 0.0, "put") is None                # no time left


# ------------------------------------------------------------- legs and units
def test_leg_from_dict_signs():
    sell = po.Leg.from_dict(LRCX_LEGS[0])
    buy = po.Leg.from_dict(LRCX_LEGS[1])
    assert sell.qty == -1 and sell.side == "sell" and sell.right == "P" and sell.kind == "put"
    assert buy.qty == 1 and buy.side == "buy"
    assert po.Leg.from_dict(dict(LRCX_LEGS[0], qty=2)).qty == -2
    assert po.Leg.from_dict(LRCX_LEGS[0], price=6.10).price == 6.10        # the Positions tab's entry_price wins
    assert po.Leg.from_dict(LRCX_LEGS[0]).price == 5.70
    assert sell.iv == 0.46 and sell.iv_source == "leg" and sell.delta == -0.25
    with pytest.raises(ValueError):
        po.Leg.from_dict(dict(LRCX_LEGS[0], open_interest=2140))        # the key is oi once past norm_leg
    with pytest.raises(ValueError):
        po.Leg.from_dict(dict(LRCX_LEGS[0], right="X"))
    with pytest.raises(ValueError):
        po.Leg.from_dict(dict(LRCX_LEGS[0], side="short"))
    with pytest.raises(ValueError):
        po.Leg.from_dict(dict(LRCX_LEGS[0], qty=0))
    with pytest.raises(ValueError):
        po.Leg.from_dict(dict(LRCX_LEGS[0], expiry="Nov 20"))
    assert sell.as_dict()["qty"] == 1 and sell.as_dict()["side"] == "sell"


def test_normalise_iv_by_unit():
    assert po.normalise_iv(28.0, unit="percent") == 0.28
    assert po.normalise_iv(0.28, unit="fraction") == 0.28
    assert po.normalise_iv(3.1099, unit="fraction") == 3.1099     # a deep-ITM Cboe row is NOT rescaled
    assert po.normalise_iv(0, unit="fraction") is None
    assert po.normalise_iv(None, unit="percent") is None
    assert po.normalise_iv("abc", unit="percent") is None
    with pytest.raises(TypeError):
        po.normalise_iv(28.0)                                     # no unit -> no magnitude guess
    with pytest.raises(ValueError):
        po.normalise_iv(28.0, unit="auto")


# ------------------------------------------------------------- verticals
def test_vertical_max_loss_equals_spread_math():
    r = lrcx()
    assert r["error"] is None and r["family"] == "credit_vertical" and r["strategy"] == "bull_put"
    assert r["max_loss"] == 790.0 and r["max_profit"] == 210.0
    assert r["breakevens"] == [327.9]
    assert r["unlimited_profit"] is False and r["unlimited_loss"] is False
    sm = bull_put.spread_math(short_strike=330.0, long_strike=320.0, credit=2.10)
    assert abs(sm["max_loss"] - r["max_loss"]) < 0.01
    assert abs(sm["max_profit"] - r["max_profit"]) < 0.01
    assert abs(sm["breakeven"] - r["breakevens"][0]) < 0.01
    assert abs(sm["risk_20pct"] - r["units"]["r_dollars"]) < 0.01
    assert r["label"] == "Nov 20 330/320 put"
    assert r["horizon"] == {"expiry": "2026-11-20", "dte": 48}
    assert all(l["iv_source"] == "solved" for l in r["legs"])


def test_mirrored_bear_call():
    # the spec's bear call 360/370 at 2.10
    legs = [{"expiry": "2026-11-20", "right": "C", "strike": 360.0, "side": "sell", "qty": 1, "price": 5.70, "iv": 0.3},
            {"expiry": "2026-11-20", "right": "C", "strike": 370.0, "side": "buy", "qty": 1, "price": 3.60, "iv": 0.3}]
    r = po.build(legs, strategy="bear_call", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)
    assert r["error"] is None and r["family"] == "credit_vertical"
    assert r["max_loss"] == 790.0 and r["max_profit"] == 210.0 and r["breakevens"] == [362.1]
    assert r["label"] == "Nov 20 360/370 call"
    # the exact mirror of the bull put around spot: pnl_bear(2 spot - S) == pnl_bull(S)
    mirror = [dict(l, right="C", strike=round(2 * LRCX_SPOT - l["strike"], 2)) for l in LRCX_LEGS]
    bull = calibrated(LRCX_LEGS, LRCX_SPOT)
    bear = calibrated(mirror, LRCX_SPOT)
    for S in (300.0, 320.0, 327.9, 330.0, 340.0, LRCX_SPOT, 360.0, 380.0):
        assert abs(po.pnl(bear, 2 * LRCX_SPOT - S, 48, AS_OF) - po.pnl(bull, S, 48, AS_OF)) < 1e-6


def test_fresh_idea_today_at_spot_is_zero():
    cases = [
        ("bull_put", LRCX_SPOT, LRCX_LEGS),
        ("bear_call", LRCX_SPOT, [dict(l, right="C", strike=round(2 * LRCX_SPOT - l["strike"], 2)) for l in LRCX_LEGS]),
        ("buy_call", ISRG_SPOT, ISRG_LEGS),
        ("buy_put", ISRG_SPOT, [{"expiry": "2026-12-18", "right": "P", "strike": 400.0, "side": "buy", "qty": 1, "price": 22.10}]),
        ("bull_call", ISRG_SPOT, [{"expiry": "2026-12-18", "right": "C", "strike": 395.0, "side": "buy", "qty": 1, "price": 37.0},
                                  {"expiry": "2026-12-18", "right": "C", "strike": 430.0, "side": "sell", "qty": 1, "price": 19.5}]),
        ("iron_condor", LRCX_SPOT, LRCX_LEGS + [dict(l, right="C", strike=round(2 * LRCX_SPOT - l["strike"], 2)) for l in LRCX_LEGS]),
        ("calendar", 100.0, calendar_legs()),
        ("leaps_call", ISRG_SPOT, [{"expiry": "2027-12-17", "right": "C", "strike": 400.0, "side": "buy", "qty": 1, "price": 78.0}]),
    ]
    for strategy, spot, legs in cases:
        r = po.build(legs, strategy=strategy, spot=spot, atr=11.54 if spot > 200 else 2.0, as_of=AS_OF)
        assert r["error"] is None, strategy
        legs_c = calibrated(legs, spot)
        assert abs(today(legs_c, spot)) < 0.01, strategy
        i = r["xs"].index(round(spot, 4))
        assert abs(r["today"][i]) < 0.01, strategy


def test_open_position_mark_identity():
    """A position: legs at their entry fill, sigma solved from TODAY's mids (what the
    Positions route does from the latest check) -> the today line at spot reads
    exactly sum(qty x (mid_today - entry) x 100), the P&L option_exits.mark records."""
    entry = {330.0: 6.40, 320.0: 4.10}
    mids = {330.0: 5.70, 320.0: 3.60}
    legs = []
    for l in LRCX_LEGS:
        T = 48 / 365
        sigma = po.implied_vol(mids[l["strike"]], LRCX_SPOT, l["strike"], T, "put")
        legs.append(po.Leg.from_dict(dict(l, iv=sigma), price=entry[l["strike"]]))
    pl_now = sum((1 if l["side"] == "buy" else -1) * (mids[l["strike"]] - entry[l["strike"]]) * 100 for l in LRCX_LEGS)
    r = po.build(legs, strategy="bull_put", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF, chart_stop=LRCX_STOP, pl_now=pl_now)
    assert all(l["iv_source"] == "leg" for l in r["legs"])            # an open position keeps today's sigma
    i = r["xs"].index(round(LRCX_SPOT, 4))
    assert abs(r["today"][i] - pl_now) < 0.01
    now = marker(r, "now")[0]
    assert abs(now["y_today"] - pl_now) < 0.01                        # the dot
    assert r["svg"]["dot"] is not None


def test_long_call_identity_and_unlimited():
    r = isrg()
    assert r["error"] is None and r["family"] == "long"
    for x, y in zip(r["xs"], r["at_expiry"]):
        assert abs(y - (max(x - 400.0, 0.0) * 100 - 34.30 * 100)) < 0.01
    assert r["unlimited_profit"] is True and r["max_profit"] is None and r["max_loss"] == 3430.0
    assert r["breakevens"] == [434.3]
    assert r["svg"]["arrow"] is True
    assert "unlimited" in r["legend"]


def test_condor_is_two_verticals():
    calls = [dict(l, right="C", strike=round(2 * LRCX_SPOT - l["strike"], 2)) for l in LRCX_LEGS]
    r = po.build(LRCX_LEGS + calls, strategy="iron_condor", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)
    assert r["error"] is None and r["family"] == "condor"
    bull = calibrated(LRCX_LEGS, LRCX_SPOT)
    bear = calibrated(calls, LRCX_SPOT)
    for x, y in zip(r["xs"], r["at_expiry"]):
        assert abs(y - (po.pnl(bull, x, 48, AS_OF) + po.pnl(bear, x, 48, AS_OF))) < 0.01
    assert r["max_profit"] == 420.0 and r["max_loss"] == 580.0        # the wider wing (10) - credit 4.20
    assert len(r["breakevens"]) == 2
    assert r["pop"]["label"] == "chance of keeping it"
    assert abs(r["pop"]["value"] - (1 - 0.25 - 0.25)) < 1e-9          # 1 - |d short put| - |d short call|


def test_calendar():
    r = po.build(calendar_legs(), strategy="calendar", spot=100.0, atr=2.0, as_of=AS_OF, premium_stop_pct=50)
    assert r["error"] is None and r["family"] == "time"
    assert r["max_loss"] == 143.0                                     # the debit, analytic, positive
    assert min(r["at_expiry"]) >= -r["max_loss"] - 5                 # the model never goes under it
    assert len(r["breakevens"]) == 2
    lo_be, hi_be = r["breakevens"]
    assert abs(lo_be - 93.90) < 0.05 and abs(hi_be - 107.88) < 0.05
    assert lo_be < 100.0 < hi_be
    assert abs(r["max_profit"] - 247) < 2 and hline(r, "max_profit")[0]["label"].endswith("(about, modelled)")
    assert abs(r["pop"]["value"] - 0.54) < 0.01
    assert r["horizon"]["expiry"] == "2026-10-30" and r["horizon"]["dte"] == 27
    assert "Drawn at the near expiry (Oct 30)" in r["caption"]
    assert r["label"] == "Oct 30 100C / Dec 4 100C"


# ------------------------------------------------------------- POP
def test_pop_credit_delta_vs_model():
    r = lrcx()
    assert r["pop"]["label"] == "chance of keeping it"
    assert r["pop"]["value"] == 0.75 and r["pop"]["basis"] == "1 - short delta 0.25"
    assert abs(r["pop"]["model"] - r["pop"]["value"]) < 0.05
    assert r["pop"]["model_basis"].startswith("lognormal, sigma ")
    assert "chance of keeping it 75%" in r["legend"] and "model estimate 73%" in r["legend"]


def test_pop_long_call_is_one_minus_n_d2_at_breakeven():
    r = isrg()
    assert r["pop"]["label"] == "chance of profit" and r["pop"]["model"] == r["pop"]["value"]
    sigma = r["legs"][0]["iv"]
    be = r["breakevens"][0]
    expect = black_scholes(ISRG_SPOT, be, 76 / 365, po.RISK_FREE, sigma, "call").prob_itm   # N(d2) at K = breakeven
    assert abs(r["pop"]["value"] - expect) < 0.002
    assert abs(r["pop"]["value"] - 0.34) < 0.01


def test_pop_function_direct():
    legs_c = calibrated(LRCX_LEGS, LRCX_SPOT)
    xs = po.grid(legs_c, LRCX_SPOT, LRCX_ATR, [LRCX_SPOT], breakevens=[327.9])
    ys = po.expiry_curve(legs_c, xs, AS_OF)
    p = po.pop("credit_vertical", legs_c, LRCX_SPOT, 0.28, 48 / 365, xs, ys)
    assert 0.7 < p < 0.76
    assert po.pop("credit_vertical", legs_c, LRCX_SPOT, None, 48 / 365, xs, ys) is None
    assert po.pop("credit_vertical", legs_c, LRCX_SPOT, 0.28, 0.0, xs, ys) is None


# ------------------------------------------------------------- iv_bump, breakevens, grid
def test_iv_bump_is_relative():
    legs_c = calibrated(LRCX_LEGS, LRCX_SPOT)
    base = today(legs_c, LRCX_STOP)
    bumped = po.pnl(legs_c, LRCX_STOP, 0, AS_OF, iv_bump=STOP_IV_BUMP)
    assert abs(base + 120.7) < 0.5 and abs(bumped + 131.1) < 0.5
    # a short vertical loses when IV rises wherever the SHORT strike is the nearer
    # one (from spot down to the breakeven); deep under the long strike the long
    # put carries the vega and the sign flips - not the region a stop lives in
    for S in (327.9, 330.0, 336.2, 345.0, LRCX_SPOT):
        assert po.pnl(legs_c, S, 0, AS_OF, iv_bump=0.10) < po.pnl(legs_c, S, 0, AS_OF)
    assert po.pnl(legs_c, LRCX_STOP, 0, AS_OF, iv_bump=0.0) == base
    leg = po.Leg(right="P", strike=330.0, expiry="2026-11-20", qty=-1, price=5.70, iv=0.46)
    v = po.leg_value(leg, LRCX_SPOT, 0, AS_OF, iv_bump=0.10)
    assert abs(v - black_scholes(LRCX_SPOT, 330.0, 48 / 365, po.RISK_FREE, 0.506, "put").price) < 1e-9
    assert abs(v - black_scholes(LRCX_SPOT, 330.0, 48 / 365, po.RISK_FREE, 0.56, "put").price) > 0.01


def test_breakevens_are_a_list_and_exact():
    assert po.breakevens([], []) == []
    assert po.breakevens([1.0, 2.0, 3.0], [5.0, 6.0, 7.0]) == []
    r = lrcx()
    legs_c = calibrated(LRCX_LEGS, LRCX_SPOT)
    for b in r["breakevens"]:
        assert abs(po.pnl(legs_c, b, 48, AS_OF)) < 0.01
    cal = po.build(calendar_legs(), strategy="calendar", spot=100.0, atr=2.0, as_of=AS_OF)
    legs_cal = calibrated(calendar_legs(), 100.0)
    for b in cal["breakevens"]:
        # the dict's figure is rounded to the cent; the dome is ~$17 per $1 there
        assert abs(po.pnl(legs_cal, b, 27, AS_OF, strict=False)) < 0.2
        assert abs(po.pnl(legs_cal, b - 0.01, 27, AS_OF, strict=False) * po.pnl(legs_cal, b + 0.01, 27, AS_OF, strict=False)) < 1.0             or po.pnl(legs_cal, b - 0.01, 27, AS_OF, strict=False) * po.pnl(legs_cal, b + 0.01, 27, AS_OF, strict=False) < 0
    # a ray sitting exactly on zero (a leg dealt at 0) is one breakeven, not a hundred
    flat = po.build([{"expiry": "2026-12-18", "right": "C", "strike": 400.0, "side": "buy", "qty": 1, "price": 0.0, "iv": 0.4}],
                    strategy=None, spot=ISRG_SPOT, atr=ISRG_ATR, as_of=AS_OF)
    assert flat["breakevens"] == [400.0]


def test_grid_holds_every_strike_and_marker():
    r = lrcx()
    xs = r["xs"]
    for x in (330.0, 320.0, LRCX_SPOT, LRCX_STOP, 340.0, 336.1, 327.9):
        assert round(x, 4) in xs
    assert xs == sorted(xs) and len(xs) == len(set(xs))
    assert abs(xs[0] - (320.0 - 2 * LRCX_ATR)) < 1e-6
    assert abs(xs[-1] - (LRCX_SPOT + 2 * LRCX_ATR)) < 1e-6
    # the breakeven is a marker too: the ISRG frame pads 2 ATR past 434.30
    i = isrg()
    assert abs(i["xs"][-1] - (434.3 + 2 * ISRG_ATR)) < 1e-6
    assert round(370.82, 4) in i["xs"] or any(abs(x - 370.82) < 0.01 for x in i["xs"])
    xs2 = po.grid(calibrated(LRCX_LEGS, LRCX_SPOT), LRCX_SPOT, None, [LRCX_SPOT])
    assert abs(xs2[0] - (320.0 - 0.05 * LRCX_SPOT)) < 1e-6              # no ATR -> 5 % of spot (with a warning in build)


# ------------------------------------------------------------- stops, R, units
def test_chart_stop_reads_today():
    r = lrcx()
    s = marker(r, "stop")
    assert len(s) == 1 and s[0]["x"] == 336.2
    assert abs(s[0]["y_today"] + 120.7) < 0.5 and abs(s[0]["y_expiry"] - 210.0) < 0.01
    assert s[0]["label"] == "chart stop 336.2 · about -$121 today"
    legs_c = calibrated(LRCX_LEGS, LRCX_SPOT)
    assert abs(today(legs_c, 336.2) + 120.7) < 0.5


def test_rule_stop_lrcx():
    legs_c = calibrated(LRCX_LEGS, LRCX_SPOT)
    x = po.price_at_pnl(legs_c, -0.2 * 790, 300.0, LRCX_SPOT, 0, AS_OF)
    assert abs(x - 332.72) < 0.05
    assert abs(today(legs_c, 332.72) + 158) < 0.5
    r = lrcx()
    rs = marker(r, "rule_stop")
    assert len(rs) == 1 and abs(rs[0]["x"] - 332.72) < 0.05 and rs[0]["y_today"] == -158.0
    assert rs[0]["label"] == "rule stop about 332.7"
    h = hline(r, "rule_stop")
    assert len(h) == 1 and h[0]["y"] == -158.0 and h[0]["label"] == "rule stop -$158 (20% of max loss)"
    tp = hline(r, "target")
    assert tp[0]["y"] == 105.0 and tp[0]["label"] == "take profit +$105 (50% of credit)"
    assert hline(r, "max_loss")[0]["y"] == -790.0 and hline(r, "max_loss")[0]["label"] == "max loss $790"
    assert hline(r, "max_profit")[0]["y"] == 210.0
    # the member's own credit stop moves the line
    r30 = lrcx(loss_fraction=0.30)
    assert hline(r30, "rule_stop")[0]["y"] == -237.0 and r30["units"]["r_dollars"] == 237.0


def test_rule_stop_every_family():
    """R1: the rule stop is drawn (hline + vertical marker) for EVERY family."""
    # buy_call: the long block's 50 % of the premium, reached at 370.82 today
    i = isrg()
    h = hline(i, "rule_stop")
    assert len(h) == 1 and h[0]["y"] == -1715.0 and h[0]["label"] == "rule stop -$1,715 (50% of the premium)"
    m = marker(i, "rule_stop")
    assert len(m) == 1 and abs(m[0]["x"] - 370.82) < 0.05 and m[0]["y_today"] == -1715.0
    # leaps_call: the leaps block's 40 %
    leaps = po.build([{"expiry": "2027-12-17", "right": "C", "strike": 400.0, "side": "buy", "qty": 1, "price": 78.0}],
                     strategy="leaps_call", spot=ISRG_SPOT, atr=ISRG_ATR, as_of=AS_OF, premium_stop_pct=40)
    assert abs(hline(leaps, "rule_stop")[0]["y"] + 0.4 * 78.0 * 100) < 1e-6 and marker(leaps, "rule_stop")
    # bull_call: the long block's 50 % of what you paid
    bc = po.build([{"expiry": "2026-12-18", "right": "C", "strike": 395.0, "side": "buy", "qty": 1, "price": 37.0},
                   {"expiry": "2026-12-18", "right": "C", "strike": 430.0, "side": "sell", "qty": 1, "price": 19.5}],
                  strategy="bull_call", spot=ISRG_SPOT, atr=ISRG_ATR, as_of=AS_OF, chart_stop=ISRG_STOP, target=ISRG_TARGET,
                  premium_stop_pct=50)
    assert bc["family"] == "debit_vertical" and bc["max_loss"] == 1750.0 and bc["max_profit"] == 1750.0
    assert hline(bc, "rule_stop")[0]["y"] == -875.0 and marker(bc, "rule_stop")
    # calendar: 50 % of the debit, the dome crosses it twice
    cal = po.build(calendar_legs(), strategy="calendar", spot=100.0, atr=2.0, as_of=AS_OF, premium_stop_pct=50)
    assert abs(hline(cal, "rule_stop")[0]["y"] + 72) < 1
    cm = sorted(x["x"] for x in marker(cal, "rule_stop"))
    assert len(cm) == 2 and abs(cm[0] - 88.8) < 0.2 and abs(cm[1] - 118.7) < 0.2
    # iron condor: loss_fraction x max loss
    calls = [dict(l, right="C", strike=round(2 * LRCX_SPOT - l["strike"], 2)) for l in LRCX_LEGS]
    ic = po.build(LRCX_LEGS + calls, strategy="iron_condor", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)
    assert hline(ic, "rule_stop")[0]["y"] == -round(LOSS_STOP_FRACTION * 580.0, 2) and len(marker(ic, "rule_stop")) == 2
    # the house premium_stop_pct is read from option_prefs when the route passes none
    i2 = isrg(premium_stop_pct=None)
    assert hline(i2, "rule_stop")[0]["y"] == -1715.0


def test_r_dollars():
    assert lrcx()["units"] == {"mode": "$", "r_dollars": 158.0, "r_basis": "20% of max loss"}
    i = isrg()
    legs_c = calibrated(ISRG_LEGS, ISRG_SPOT)
    assert i["units"]["r_basis"] == "loss at the chart stop today"
    assert abs(i["units"]["r_dollars"] - 640) < 1.0
    assert abs(i["units"]["r_dollars"] + today(legs_c, ISRG_STOP)) < 0.01
    t = marker(i, "target")[0]
    assert abs(t["y_today"] - 1483) < 1.0 and abs(t["y_expiry"] + 541) < 1.0
    # a target read in R: +2.32 R
    iR = isrg(units="R")
    assert abs(marker(iR, "target")[0]["y_today"] - 2.32) < 0.01
    # no chart stop on a debit -> R is the max loss
    assert isrg(chart_stop=None)["units"]["r_basis"] == "max loss"


def test_units_r_arrays_equal_dollar_arrays_over_r():
    d = lrcx()
    r = lrcx(units="R")
    assert r["units"]["mode"] == "R" and r["units"]["r_dollars"] == 158.0
    assert r["xs"] == d["xs"]
    for a, b in zip(r["at_expiry"], d["at_expiry"]):
        assert abs(a - b / 158.0) < 1e-3
    for a, b in zip(r["today"], d["today"]):
        assert abs(a - b / 158.0) < 1e-3
    assert abs(r["max_loss"] - 790 / 158) < 1e-3 and abs(r["max_profit"] - 210 / 158) < 1e-3
    for hr, hd in zip(r["hlines"], d["hlines"]):
        assert abs(hr["y"] - hd["y"] / 158.0) < 1e-3
    assert hline(r, "rule_stop")[0]["label"] == "rule stop -1.00 R (20% of max loss)"
    assert r["series"]["units"]["mode"] == "R"
    assert any(lbl.endswith(" R") for _, lbl in r["svg"]["y_ticks"])
    assert r["uid"] != d["uid"]


# ------------------------------------------------------------- failure modes
def test_failure_modes():
    # a price under intrinsic, no iv, no fallback: the expiry line only
    r = po.build([{"expiry": "2026-12-18", "right": "C", "strike": 300.0, "side": "buy", "qty": 1, "price": 50.0}],
                 strategy="buy_call", spot=ISRG_SPOT, atr=ISRG_ATR, as_of=AS_OF, chart_stop=ISRG_STOP)
    assert r["error"] is None and r["today"] is None and po.WARN_NO_SIGMA in r["warnings"]
    assert r["pop"]["model"] is None and r["svg"]["today_path"] is None and r["series"]["today"] is None
    assert marker(r, "stop")[0]["label"].endswith("at expiry")
    assert r["legs"][0]["iv_source"] == "none"
    # the same leg with a fallback sigma draws the today line from it
    r2 = po.build([{"expiry": "2026-12-18", "right": "C", "strike": 300.0, "side": "buy", "qty": 1, "price": 50.0}],
                  strategy="buy_call", spot=ISRG_SPOT, atr=ISRG_ATR, as_of=AS_OF, sigma_fallback=0.41)
    assert r2["today"] is not None and r2["legs"][0]["iv_source"] == "fallback"
    # expired
    e = po.build([dict(l, expiry="2026-09-18") for l in LRCX_LEGS], strategy="bull_put", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)
    assert e["error"] == "expired" and e["svg"] is None and e["xs"] == [] and e["breakevens"] == []
    # zero credit on a credit strategy
    z = po.build([dict(LRCX_LEGS[0], price=3.60), LRCX_LEGS[1]], strategy="bull_put", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)
    assert z["error"] == "no edge: the spread pays nothing"
    # no legs, unknown strategy
    assert po.build([], strategy="bull_put", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)["error"].startswith("legs invalid")
    assert po.build(LRCX_LEGS, strategy="covered_call", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)["error"].startswith("unknown strategy")
    # no ATR: padded 5 % of spot with a warning, never an exception
    n = lrcx(atr=None)
    assert n["error"] is None and po.WARN_NO_ATR in n["warnings"]
    # a stop above the entry on a debit: R undefined, the $ render stands
    u = isrg(chart_stop=420.0, units="R")
    assert u["units"]["mode"] == "$" and u["units"]["r_dollars"] == 0.0 and po.WARN_R_ZERO in u["warnings"]
    # arbitrary legs: generic labels, numeric extremes
    a = po.build(LRCX_LEGS, strategy=None, spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)
    assert a["error"] is None and a["family"] is None and a["max_loss"] == 790.0 and a["max_profit"] == 210.0
    assert a["pop"]["label"] == "chance of profit"


# ------------------------------------------------------------- the SVG and the partial
def test_svg_paths():
    r = lrcx()
    s = r["svg"]
    assert s["expiry_path"].startswith("M") and s["today_path"].startswith("M")
    assert len(s["profit_zones"]) == 1 and len(s["loss_zones"]) == 1     # split exactly at 327.90
    assert all(z.endswith(" Z") for z in s["profit_zones"] + s["loss_zones"])
    rows = {m["label"]: m["row"] for m in s["markers"]}
    assert rows["trend line at expiry 336.1"] != rows["chart stop 336.2 · about -$121 today"]
    assert [t[1] for t in s["x_ticks"]] == ["300", "320", "340", "360"]       # nice(ATR 11.54) = 20
    assert s["ymin"] < -790 < 210 < s["ymax"] and 8 <= s["zero_y"] <= 264
    assert len(s["hlines"]) == 4 and s["dot"] is None and s["arrow"] is False
    assert s["lo"] == r["xs"][0] and s["hi"] == r["xs"][-1]
    for m in s["markers"]:
        assert 48 <= m["x_px"] <= 628 and m["row"] in range(po.MARKER_ROWS)
        assert m["anchor"] in ("start", "middle", "end") and m["label_y"] == 18 + 12 * m["row"]
    # the C5.1 cluster (eight markers within 30 points) never shares a row with a neighbour it would overprint
    placed = sorted(s["markers"], key=lambda m: m["x_px"])
    for a, b in zip(placed, placed[1:]):
        if a["row"] == b["row"]:
            assert b["x_px"] - a["x_px"] > (len(a["label"]) + len(b["label"])) / 2 * po.LABEL_CHAR_PX


def test_partial_renders():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app" / "templates")), autoescape=True)
    tpl = env.get_template("_payoff_chart.html")
    r = lrcx()
    html = tpl.render(po=r, pane_url="http://localhost:8000/options/payoff/LRCX?strategy=bull_put&pick=0")
    assert 'viewBox="0 0 640 300"' in html
    assert f'id="po-{r["uid"]}"' in html and f'id="po-data-{r["uid"]}"' in html
    assert 'hx-get="/options/payoff/LRCX?strategy=bull_put&amp;pick=0&amp;units=R"' in html   # path + query, never the host
    assert "var(--po-exp, #1D9E75)" in html and "var(--po-today, #7F77DD)" in html
    assert "stroke-dasharray=\"5 3\"" in html
    from markupsafe import escape
    assert str(escape(r["caption"])) in html and "At expiry" in html and "Today" in html
    assert "chance of keeping it 75%" in html
    assert "rule stop -$158 (20% of max loss)" in html and "chart stop 336.2" in html
    assert re.search(r"\bstep \d", html) is None and "phase" not in html.lower()
    # the error build renders the message inside the same pane height, no SVG
    e = po.build([dict(l, expiry="2026-09-18") for l in LRCX_LEGS], strategy="bull_put", spot=LRCX_SPOT, atr=LRCX_ATR, as_of=AS_OF)
    eh = tpl.render(po=e, pane_url="/options/payoff/LRCX?strategy=bull_put")
    assert "Nothing to draw: expired." in eh and "<svg" not in eh and "min-h-[300px]" in eh
    # an undefined R disables the toggle with the reason
    u = isrg(chart_stop=420.0)
    uh = tpl.render(po=u, pane_url="/options/payoff/ISRG?strategy=buy_call")
    assert 'disabled title="R undefined: the stop does not lose money"' in uh
    # the Positions dot and the no-sigma amber line
    p = po.build([{"expiry": "2026-12-18", "right": "C", "strike": 300.0, "side": "buy", "qty": 1, "price": 50.0}],
                 strategy="buy_call", spot=ISRG_SPOT, atr=ISRG_ATR, as_of=AS_OF)
    ph = tpl.render(po=p, pane_url="/options/payoff/ISRG?strategy=buy_call")
    assert po.WARN_NO_SIGMA in ph and str(escape(p["caption"])) not in ph
