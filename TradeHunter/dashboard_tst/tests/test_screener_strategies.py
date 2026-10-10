"""Strategy formulas and pairing (OPTIONS_SCREENER_DESIGN.md §5-§6), hand-checked on a small
Black-Scholes chain: one underlying at 100, two expiries (30 and 60 days), strikes 80-120 by
5, a strike-dependent IV so "the IV of the leg nearest the break-even" is observable.

Every expected figure is recomputed here from the leg prices / IVs with scalar math
(``math.erf``), independent of the engine's vectorised code.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from app.services.screener import engine, frame as frame_mod, strategies
from app.services.screener.fields import Plan, get
from app.services.screener.frame import Frame
from app.services.screener.screens import SCREENS
from tests.test_screener_engine import AS_OF, TODAY, bs, p_above

S0 = 100.0
E1, E2 = 30, 60
EXP1 = (TODAY + dt.timedelta(days=E1)).isoformat()
EXP2 = (TODAY + dt.timedelta(days=E2)).isoformat()
STRIKES = (80, 85, 90, 95, 100, 105, 110, 115, 120)


def iv_of(K, dte):
    return (0.30 if dte == E1 else 0.25) + 0.002 * (100 - K) / 5      # puts' side richer


def make_chain(strikes=STRIKES, exps=(E1, E2), spot=S0, symbol="XYZ") -> tuple[list, dict]:
    rows, book = [], {}
    for dd in exps:
        exp = (TODAY + dt.timedelta(days=dd)).isoformat()
        T = dd / 365.0
        for put in (False, True):
            for K in strikes:
                sig = iv_of(K, dd)
                price, delta, gamma, theta, vega = bs(spot, K, T, sig, put)
                r = dict(symbol=symbol, expiry=exp, right="P" if put else "C", strike=float(K), weekly=False,
                         price=round(price, 2), last=round(price, 2), chg_pct=0.0, volume=1000, oi=1000,
                         vol_prev=900, oi_prev=900, iv=sig, delta=delta, gamma=gamma, theta=theta, vega=vega,
                         last_trade=AS_OF, session="2026-10-09", as_of=AS_OF)
                rows.append(r)
                book[(dd, "P" if put else "C", float(K))] = r
    return rows, book


def und(symbol="XYZ", spot=S0) -> dict:
    return dict(symbol=symbol, sec_type="stock", exchange="NYSE", spot=spot, hv20=20.0, iv_rank=50.0,
                iv_pct=50.0, trend="up", history_done=True)


ROWS, BOOK = make_chain()


@pytest.fixture(autouse=True)
def pinned():
    frame_mod.set_current(Frame.from_records(ROWS, [und()], meta={"pass_id": 1}, today=TODAY))
    yield
    frame_mod.reset()


def P(dte, right, K):
    return BOOK[(dte, right, float(K))]["price"]


def IV(dte, right, K):
    return BOOK[(dte, right, float(K))]["iv"]


def D(dte, right, K, key="delta"):
    return BOOK[(dte, right, float(K))][key]


def pa(X, sig, dte=E1):
    return p_above(S0, X, sig, dte / 365.0)


def nearest_iv(X, legs, dte=E1):
    """The IV of the leg (right, strike) whose strike is nearest X - first leg on a tie."""
    best = min(legs, key=lambda rk: abs(rk[1] - X))
    return IV(dte, *best)


def one(key, filters, **kw) -> dict:
    out = engine.run(key, {"filters": filters, **kw}, per_page=1000)
    assert out["warnings"] == [], out["warnings"]
    assert out["total"] == 1, (key, out["total"])
    return out["rows"][0]


def rows(key, filters, **kw) -> list[dict]:
    out = engine.run(key, {"filters": filters, **kw}, per_page=1000)
    assert out["warnings"] == [], out["warnings"]
    return out["rows"]


def legs_eq(*pairs, dte_filter=E1):
    f = [{"f": "dte", "op": "eq", "v": dte_filter}] if dte_filter is not None else []
    return f + [{"f": f"leg{n}.strike", "op": "eq", "v": k} for n, k in pairs]


A = dict(abs=0.011)      # money values are rounded to the cent in rows
PCT = dict(abs=5.1e-3)   # percent values (rounded to 2 dp)


# ─────────────────────────────────── verticals ───────────────────────────────────

def test_bull_put_spread_credit():
    r = one("bull-put-spread", legs_eq((1, 95), (2, 90)))
    c = P(E1, "P", 95) - P(E1, "P", 90)
    be = 95 - c
    assert r["net_credit"] == pytest.approx(c * 100, **A)
    assert r["be"] == pytest.approx(be, **A)
    assert r["be_pct"] == pytest.approx((be - S0) / S0 * 100, **PCT)
    assert r["max_profit"] == pytest.approx(c * 100, **A)
    assert r["max_loss"] == pytest.approx((5 - c) * 100, **A)
    assert r["max_profit_pct"] == pytest.approx(c / (5 - c) * 100, **PCT)
    assert r["risk_reward"] == pytest.approx((5 - c) / c, abs=6e-3)
    loss = 1 - pa(be, nearest_iv(be, [("P", 95), ("P", 90)]))
    assert r["loss_prob"] == pytest.approx(loss * 100, **PCT)
    assert r["leg1.price"] == pytest.approx(P(E1, "P", 95)) and r["leg2.price"] == pytest.approx(P(E1, "P", 90))
    assert [(x["action"], x["right"], x["strike"]) for x in r["legs"]] == [("sell", "P", 95.0), ("buy", "P", 90.0)]
    full = engine.run("bull-put-spread", {"filters": legs_eq((1, 95), (2, 90)), "view": "greeks"})["rows"][0]
    assert full["net_delta"] == pytest.approx(-D(E1, "P", 95) + D(E1, "P", 90), abs=2e-4)


def test_bull_put_spread_metrics_via_api_internals():
    """max_profit_prob = P(S_T > short strike) with the short leg's IV (not a column, so read
    through the engine's kept table)."""
    fr = frame_mod.current()
    scr = SCREENS["bull-put-spread"]
    from app.services.screener.fields import parse_filters
    w = []
    filts = parse_filters(legs_eq((1, 95), (2, 90)), None, fr, w)
    t, total, _, _ = strategies.run(fr, scr, Plan.build(filts, get("loss_prob"), False, 100))
    assert total == 1
    assert t.m["max_profit_prob"][0] == pytest.approx(pa(95, IV(E1, "P", 95)) * 100, abs=1e-4)
    assert t.m["win_prob"][0] + t.m["loss_prob"][0] == pytest.approx(100)


def test_bear_call_spread_credit():
    r = one("bear-call-spread", legs_eq((1, 105), (2, 110)))
    c = P(E1, "C", 105) - P(E1, "C", 110)
    be = 105 + c
    assert r["net_credit"] == pytest.approx(c * 100, **A)
    assert r["be"] == pytest.approx(be, **A)
    assert r["max_loss"] == pytest.approx((5 - c) * 100, **A)
    assert r["loss_prob"] == pytest.approx(pa(be, nearest_iv(be, [("C", 105), ("C", 110)])) * 100, **PCT)


def test_bull_call_spread_debit():
    r = one("bull-call-spread", legs_eq((1, 100), (2, 105)))
    d = P(E1, "C", 100) - P(E1, "C", 105)
    be = 100 + d
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["be"] == pytest.approx(be, **A)
    assert r["max_profit"] == pytest.approx((5 - d) * 100, **A)
    assert r["max_loss"] == pytest.approx(d * 100, **A)
    assert r["win_prob"] == pytest.approx(pa(be, nearest_iv(be, [("C", 100), ("C", 105)])) * 100, **PCT)
    assert [(x["action"], x["strike"]) for x in r["legs"]] == [("buy", 100.0), ("sell", 105.0)]


def test_bear_put_spread_debit():
    r = one("bear-put-spread", legs_eq((1, 100), (2, 95)))
    d = P(E1, "P", 100) - P(E1, "P", 95)
    be = 100 - d
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["be"] == pytest.approx(be, **A)
    assert r["win_prob"] == pytest.approx((1 - pa(be, nearest_iv(be, [("P", 100), ("P", 95)]))) * 100, **PCT)


def test_vertical_pairing_bounds(monkeypatch):
    f = [{"f": "dte", "op": "eq", "v": E1}]
    n = len(STRIKES)
    allp = rows("bull-put-spread", f)
    # every (short above long) pair is a valid credit spread here: C(9, 2) of them
    assert len(allp) == n * (n - 1) // 2
    assert all(r["leg1.strike"] > r["leg2.strike"] for r in allp)
    monkeypatch.setattr(strategies, "MAX_APART", 2)
    near = rows("bull-put-spread", f)
    assert len(near) == (n - 1) + (n - 2)
    assert all(0 < r["leg1.strike"] - r["leg2.strike"] <= 10 for r in near)


def test_leg_filters_apply_before_pairing():
    out = rows("bull-put-spread", [{"f": "dte", "op": "eq", "v": E1},
                                   {"f": "leg2.strike", "op": "gte", "lo": 100},
                                   {"f": "leg1.moneyness", "op": "lte", "hi": 10}])
    assert out and all(r["leg2.strike"] >= 100 and r["leg1.strike"] <= 110 for r in out)


def test_strategy_level_filter_and_sort():
    out = rows("bull-call-spread", [{"f": "dte", "op": "eq", "v": E1}, {"f": "win_prob", "op": "gte", "lo": 50}],
               sort={"col": "max_profit_pct", "dir": "desc"})
    assert out and all(r["win_prob"] >= 50 for r in out)
    mpp = [r["max_profit_pct"] for r in out]
    assert mpp == sorted(mpp, reverse=True)


# ─────────────────────────────────── straddles / strangles ───────────────────────────────────

def test_long_and_short_straddle():
    r = one("long-straddle", legs_eq((1, 100)))
    d = P(E1, "C", 100) + P(E1, "P", 100)
    sig = IV(E1, "C", 100)
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["be_hi"] == pytest.approx(100 + d, **A) and r["be_lo"] == pytest.approx(100 - d, **A)
    win = pa(100 + d, sig) + (1 - pa(100 - d, sig))
    assert r["win_prob"] == pytest.approx(win * 100, **PCT)
    assert [x["right"] for x in r["legs"]] == ["C", "P"] and r["legs"][0]["strike"] == r["legs"][1]["strike"]
    s = one("short-straddle", legs_eq((1, 100)))
    assert s["net_credit"] == pytest.approx(d * 100, **A)
    assert s["loss_prob"] == pytest.approx(win * 100, **PCT)
    assert s["max_profit_prob"] == 0
    # every straddle pairs a call and a put at the same strike and expiry
    for x in rows("long-straddle", []):
        assert x["legs"][0]["strike"] == x["legs"][1]["strike"] and x["legs"][0]["expiry"] == x["legs"][1]["expiry"]


def test_short_strangle():
    r = one("short-strangle", legs_eq((1, 95), (2, 105)))
    c = P(E1, "P", 95) + P(E1, "C", 105)
    lo, hi = 95 - c, 105 + c
    assert r["net_credit"] == pytest.approx(c * 100, **A)
    assert r["be_lo"] == pytest.approx(lo, **A) and r["be_hi"] == pytest.approx(hi, **A)
    loss = (1 - pa(lo, IV(E1, "P", 95))) + pa(hi, IV(E1, "C", 105))
    assert r["loss_prob"] == pytest.approx(loss * 100, **PCT)
    mpp = pa(95, IV(E1, "P", 95)) - pa(105, IV(E1, "C", 105))
    assert r["max_profit_prob"] == pytest.approx(mpp * 100, **PCT)
    lg = one("long-strangle", legs_eq((1, 95), (2, 105)))
    assert lg["win_prob"] == pytest.approx(loss * 100, **PCT)
    assert all(x["leg1.strike"] < x["leg2.strike"] for x in rows("long-strangle", []))


# ─────────────────────────────────── calendars / diagonals ───────────────────────────────────

def test_long_call_calendar():
    r = one("long-call-calendar", [{"f": "leg1.dte", "op": "eq", "v": E1}, {"f": "leg2.dte", "op": "eq", "v": E2},
                                   {"f": "leg1.strike", "op": "eq", "v": 100}])
    d = P(E2, "C", 100) - P(E1, "C", 100)
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["iv_skew"] == pytest.approx((IV(E1, "C", 100) - IV(E2, "C", 100)) * 100, **PCT)
    assert r["net_delta"] == pytest.approx(D(E2, "C", 100) - D(E1, "C", 100), abs=2e-4)
    assert r["net_vega"] == pytest.approx(D(E2, "C", 100, "vega") - D(E1, "C", 100, "vega"), abs=2e-4)
    assert r["leg1.expiry"] == EXP1 and r["leg2.expiry"] == EXP2
    assert [x["action"] for x in r["legs"]] == ["sell", "buy"]
    for x in rows("long-put-calendar", []):
        assert x["leg1.strike"] == x["legs"][1]["strike"] and x["leg1.expiry"] < x["leg2.expiry"]


def test_diagonals_strike_sides():
    calls = rows("long-call-diagonal", [])
    assert calls and all(x["legs"][1]["strike"] < x["leg1.strike"] and x["leg1.expiry"] < x["leg2.expiry"]
                         for x in calls)
    puts = rows("short-put-diagonal", [])
    assert puts and all(x["legs"][1]["strike"] > x["leg1.strike"] for x in puts)
    r = one("long-call-diagonal", [{"f": "leg1.strike", "op": "eq", "v": 110}, {"f": "leg2.strike", "op": "eq", "v": 95}])
    d = P(E2, "C", 95) - P(E1, "C", 110)
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["iv_skew"] == pytest.approx((IV(E1, "C", 110) - IV(E2, "C", 95)) * 100, **PCT)
    s = one("short-call-diagonal", [{"f": "leg1.strike", "op": "eq", "v": 110}, {"f": "leg2.strike", "op": "eq", "v": 95}])
    assert s["net_credit"] == pytest.approx(d * 100, **A)


# ─────────────────────────────────── butterflies / condors ───────────────────────────────────

def test_long_call_butterfly():
    r = one("long-call-butterfly", legs_eq((1, 95), (2, 100), (3, 105)))
    d = P(E1, "C", 95) - 2 * P(E1, "C", 100) + P(E1, "C", 105)
    lo, hi = 95 + d, 105 - d
    legs = [("C", 95), ("C", 100), ("C", 105)]
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["be_lo"] == pytest.approx(lo, **A) and r["be_hi"] == pytest.approx(hi, **A)
    assert r["max_profit"] == pytest.approx((5 - d) * 100, **A) and r["max_loss"] == pytest.approx(d * 100, **A)
    win = pa(lo, nearest_iv(lo, legs)) - pa(hi, nearest_iv(hi, legs))
    assert r["win_prob"] == pytest.approx(win * 100, **PCT)
    assert [(x["action"], x["qty"]) for x in r["legs"]] == [("buy", 1), ("sell", 2), ("buy", 1)]
    s = one("short-call-butterfly", legs_eq((1, 95), (2, 100), (3, 105)))
    assert s["net_credit"] == pytest.approx(d * 100, **A) and s["loss_prob"] == pytest.approx(win * 100, **PCT)
    assert s["max_loss"] == pytest.approx((5 - d) * 100, **A)


def test_butterfly_equal_wings_on_irregular_strikes():
    strikes = (90, 95, 97.5, 100, 105, 110)
    rws, _ = make_chain(strikes, exps=(E1,))
    frame_mod.set_current(Frame.from_records(rws, [und()], today=TODAY))
    got = {(x["leg1.strike"], x["leg2.strike"], x["leg3.strike"]) for x in rows("long-put-butterfly", [])}
    expect = {(a, b, c) for a in strikes for b in strikes for c in strikes
              if a < b < c and abs((b - a) - (c - b)) < 1e-9}
    assert got <= expect and got            # equal wings only (and the debit must be 0 < d < width)
    assert (95, 97.5, 100) in got and (90, 100, 110) in got
    cond = rows("long-call-condor", [])
    assert cond and all(abs((x["leg2.strike"] - x["leg1.strike"]) - (x["leg4.strike"] - x["leg3.strike"])) < 1e-9
                        and x["leg2.strike"] < x["leg3.strike"] for x in cond)


def test_long_call_condor():
    r = one("long-call-condor", legs_eq((1, 90), (2, 95), (3, 105), (4, 110)))
    d = P(E1, "C", 90) - P(E1, "C", 95) - P(E1, "C", 105) + P(E1, "C", 110)
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["be_lo"] == pytest.approx(90 + d, **A) and r["be_hi"] == pytest.approx(110 - d, **A)
    assert r["max_profit"] == pytest.approx((5 - d) * 100, **A)


def test_short_iron_condor():
    r = one("short-iron-condor", legs_eq((1, 85), (2, 90), (3, 110), (4, 115)))
    c = P(E1, "P", 90) - P(E1, "P", 85) + P(E1, "C", 110) - P(E1, "C", 115)
    lo, hi = 90 - c, 110 + c
    legs = [("P", 85), ("P", 90), ("C", 110), ("C", 115)]
    assert r["net_credit"] == pytest.approx(c * 100, **A)
    assert r["be_lo"] == pytest.approx(lo, **A) and r["be_hi"] == pytest.approx(hi, **A)
    assert r["max_profit"] == pytest.approx(c * 100, **A)
    assert r["max_loss"] == pytest.approx((5 - c) * 100, **A)
    loss = (1 - pa(lo, nearest_iv(lo, legs))) + pa(hi, nearest_iv(hi, legs))
    assert r["loss_prob"] == pytest.approx(loss * 100, **PCT)
    assert [(x["action"], x["right"]) for x in r["legs"]] == [("buy", "P"), ("sell", "P"), ("sell", "C"), ("buy", "C")]
    for x in rows("short-iron-condor", [{"f": "dte", "op": "eq", "v": E1}]):
        k = [leg["strike"] for leg in x["legs"]]
        assert k[0] < k[1] < k[2] < k[3] and abs((k[1] - k[0]) - (k[3] - k[2])) < 1e-9
    # the mirror trade (buy the inner legs, sell the wings) pays the same amount
    lg = one("long-iron-condor", legs_eq((1, 85), (2, 90), (3, 110), (4, 115)))
    assert lg["net_debit"] == pytest.approx(c * 100, **A)
    assert lg["win_prob"] == pytest.approx(r["loss_prob"], abs=0.011)    # it wins where the short loses


def test_long_iron_condor_debit_structure():
    # buy the inner put / call, sell the outer wings: a debit when the inner legs are nearer the money
    r = one("long-iron-condor", legs_eq((1, 90), (2, 95), (3, 105), (4, 110)))
    d = P(E1, "P", 95) - P(E1, "P", 90) + P(E1, "C", 105) - P(E1, "C", 110)
    assert r["net_debit"] == pytest.approx(d * 100, **A)
    assert r["be_lo"] == pytest.approx(95 - d, **A) and r["be_hi"] == pytest.approx(105 + d, **A)
    legs = [("P", 90), ("P", 95), ("C", 105), ("C", 110)]
    lo, hi = 95 - d, 105 + d
    win = (1 - pa(lo, nearest_iv(lo, legs))) + pa(hi, nearest_iv(hi, legs))
    assert r["win_prob"] == pytest.approx(win * 100, **PCT)
    assert r["max_profit"] == pytest.approx((5 - d) * 100, **A) and r["max_loss"] == pytest.approx(d * 100, **A)


def test_short_iron_butterfly():
    r = one("short-iron-butterfly", legs_eq((1, 95), (2, 100), (3, 100), (4, 105)))
    c = P(E1, "P", 100) + P(E1, "C", 100) - P(E1, "P", 95) - P(E1, "C", 105)
    assert r["net_credit"] == pytest.approx(c * 100, **A)
    assert r["be_lo"] == pytest.approx(100 - c, **A) and r["be_hi"] == pytest.approx(100 + c, **A)
    assert r["max_loss"] == pytest.approx((5 - c) * 100, **A)
    for x in rows("short-iron-butterfly", []):
        k = [leg["strike"] for leg in x["legs"]]
        assert k[1] == k[2] and abs((k[1] - k[0]) - (k[3] - k[2])) < 1e-9


# ─────────────────────────────────── collar / single-leg trades ───────────────────────────────────

def test_protective_collar():
    r = one("protective-collar", legs_eq((1, 105), (2, 95)))
    nc = P(E1, "C", 105) - P(E1, "P", 95)
    be = S0 - nc
    assert r["net_credit"] == pytest.approx(nc * 100, **A)
    assert r["be"] == pytest.approx(be, **A)
    assert r["max_profit"] == pytest.approx((105 - S0 + nc) * 100, **A)
    assert r["max_loss"] == pytest.approx((S0 - 95 - nc) * 100, **A)
    assert r["cost_pct"] == pytest.approx(-nc / S0 * 100, **PCT)
    assert r["upside_pct"] == pytest.approx((105 - be) / be * 100, **PCT)
    assert r["downside_pct"] == pytest.approx((be - 95) / be * 100, **PCT)
    assert r["win_prob"] == pytest.approx(pa(be, nearest_iv(be, [("C", 105), ("P", 95)])) * 100, **PCT)
    assert r["net_delta"] == pytest.approx(1 + D(E1, "P", 95) - D(E1, "C", 105), abs=2e-4)
    assert r["legs"][0]["right"] == "S" and r["legs"][0]["price"] == S0
    for x in rows("protective-collar", []):
        assert x["leg2.strike"] < S0 < x["leg1.strike"]


def test_covered_call_returns():
    filt = [{"f": "dte", "op": "eq", "v": E1}, {"f": "strike", "op": "eq", "v": 105}]
    r = one("covered-calls", filt)
    p = P(E1, "C", 105)
    ret = p / (S0 - p) * 100
    assert r["return_pct"] == pytest.approx(ret, **PCT)
    assert r["ann_return"] == pytest.approx(ret * 365 / E1, abs=0.02)
    assert r["ptnl_return"] == pytest.approx((p + 5) / (S0 - p) * 100, **PCT)
    assert r["be"] == pytest.approx(S0 - p, **A)
    assert r["win_prob"] == pytest.approx(pa(S0 - p, IV(E1, "C", 105)) * 100, **PCT)
    itm = one("covered-calls", [{"f": "dte", "op": "eq", "v": E1}, {"f": "strike", "op": "eq", "v": 95}])
    q = P(E1, "C", 95)
    assert itm["return_pct"] == pytest.approx((q - 5) / (S0 - q) * 100, **PCT)
    assert itm["ptnl_return"] == pytest.approx(q / (S0 - q) * 100, **PCT)
    assert [x["right"] for x in itm["legs"]] == ["S", "C"] and itm["legs"][1]["action"] == "sell"


def test_naked_put_return():
    r = one("naked-puts", [{"f": "dte", "op": "eq", "v": E1}, {"f": "strike", "op": "eq", "v": 95}])
    p = P(E1, "P", 95)
    assert r["return_pct"] == pytest.approx(p / (95 - p) * 100, **PCT)
    assert r["ann_return"] == pytest.approx(p / (95 - p) * 100 * 365 / E1, abs=0.02)
    assert r["be"] == pytest.approx(95 - p, **A)
    assert r["win_prob"] == pytest.approx(pa(95 - p, IV(E1, "P", 95)) * 100, **PCT)


def test_married_put():
    r = one("married-put", [{"f": "dte", "op": "eq", "v": E1}, {"f": "strike", "op": "eq", "v": 95}])
    p = P(E1, "P", 95)
    assert r["be"] == pytest.approx(S0 + p, **A)
    assert r["max_loss"] == pytest.approx((S0 + p - 95) * 100, **A)
    assert r["downside_pct"] == pytest.approx((S0 + p - 95) / (S0 + p) * 100, **PCT)
    assert r["win_prob"] == pytest.approx(pa(S0 + p, IV(E1, "P", 95)) * 100, **PCT)


# ─────────────────────────────────── caps ───────────────────────────────────

def test_combo_cap_direct():
    fr = frame_mod.current()
    scr = SCREENS["short-iron-condor"]
    plan = Plan.build([], get("loss_prob"), False, 5000)
    t, total, w, stopped = strategies.run(fr, scr, plan)
    assert not stopped and total > 0 and not w
    old = strategies.PAIR_BUDGET, strategies.MIN_CHUNK
    try:
        strategies.PAIR_BUDGET, strategies.MIN_CHUNK = 1, 1      # one anchor per chunk
        t2, total2, w2, stopped2 = strategies.run(fr, scr, plan, combo_cap=1)
    finally:
        strategies.PAIR_BUDGET, strategies.MIN_CHUNK = old
    assert stopped2 and w2 and "Stopped after" in w2[0] and total2 < total


def test_every_row_has_consistent_probabilities():
    for key in ("bull-put-spread", "bear-call-spread", "long-straddle", "short-strangle", "long-call-butterfly",
                "short-iron-condor", "long-iron-butterfly", "protective-collar"):
        out = engine.run(key, {"filters": []}, per_page=1000)
        assert out["total"] > 0, key
        fr = frame_mod.current()
        scr = SCREENS[key]
        t, _, _, _ = strategies.run(fr, scr, Plan.build([], get("symbol"), False, 5000))
        wp, lp = t.m["win_prob"], t.m["loss_prob"]
        ok = np.isfinite(wp)
        assert ok.all(), key
        assert np.allclose(wp + lp, 100.0) and (wp >= 0).all() and (wp <= 100).all()
        mpp = t.m["max_profit_prob"]
        fin = np.isfinite(mpp)
        assert (mpp[fin] <= np.maximum(wp[fin], 0) + 1e-6).all(), key     # max profit is a subset of profit
