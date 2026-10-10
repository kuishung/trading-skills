"""Options v2 screeners (OPTIONS_V2_DESIGN.md section 8).

Hand-built chains in the ``opt_store.chain_view`` shape give hand-checked numbers
per family (POP re-derived here with an independent erf lognormal); the fixture
``chain_bs`` (converted to the same shape) drives the funnel invariant, the
per-ticker cut, detail() round trips and the 30-ticker timing check.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import time

import pytest

from app.services import clock, opt_rules, opt_screen, payoff
from tests.fixtures.options import chain_bs

TODAY = "2026-10-09"                                  # a Friday
NOW = dt.datetime(2026, 10, 9, 18, 0, 0)               # naive UTC = 14:00 ET
FRESH = NOW - dt.timedelta(minutes=10)
R_FREE = 0.04


def above(S, B, sigma, days):
    """P(S_T > B), risk-neutral lognormal - written out independently of the module."""
    T = days / 365.0
    d2 = (math.log(S / B) + (R_FREE - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))


def row(strike, bid, ask, delta, *, iv=0.30, theta=-0.03, oi=1000, volume=100, as_of=FRESH,
        source="hermes", uid=None, name=None, mdt="live", **extra):
    r = {"strike": float(strike), "bid": bid, "ask": ask,
         "mid": None if (bid is None or ask is None) else round((bid + ask) / 2.0, 4),
         "last": None, "bid_size": 10, "ask_size": 10, "volume": volume, "oi": oi, "iv": iv, "delta": delta,
         "gamma": 0.02, "theta": theta, "vega": 0.10, "und_price": 100.0, "as_of": as_of, "source": source,
         "source_user_id": uid, "source_name": name, "mdt": mdt}
    r.update(extra)
    return r


def chain(expiries: dict, *, sym="SYN", spot=100.0) -> dict:
    t0 = dt.date.fromisoformat(TODAY)
    exps = []
    for e in sorted(expiries):
        v = expiries[e]
        exps.append({"expiry": e, "dte": (dt.date.fromisoformat(e) - t0).days,
                     "calls": sorted(v.get("calls", []), key=lambda r: r["strike"]),
                     "puts": sorted(v.get("puts", []), key=lambda r: r["strike"])})
    return {"symbol": sym, "spot": spot, "spot_as_of": FRESH, "spot_source": "hermes", "spot_mdt": "live",
            "expiries": exps}


def und(**kw) -> dict:
    base = {"symbol": "SYN", "spot": 100.0, "spot_as_of": FRESH, "spot_source": "hermes", "spot_mdt": "live",
            "atr14": 2.0, "hv20": 28.0, "hv60": 30.0, "avg_vol20": 3_000_000.0, "iv30": 30.0, "iv_rank": 55.0,
            "iv_pct": 60.0, "earnings_date": "2027-06-01", "earnings_src": "yahoo", "history_done": True}
    base.update(kw)
    return base


def rules(strategy, shared=None, **own):
    return opt_rules.for_strategy({"schema": 2, "shared": shared or {}, strategy: own}, strategy)


def run(strategy, ch, u, r=None, **kw):
    if isinstance(ch, dict) and "expiries" in ch:
        ch, u = {"SYN": ch}, {"SYN": u}
    return opt_screen.screen(strategy, ch, u, r if r is not None else rules(strategy), today=TODAY, now=NOW, **kw)


def removed(res) -> dict:
    return {f["rule"]: f["removed"] for f in res["funnel"]}


def check_invariant(res):
    """Every enumerated trade is either removed by exactly one rule or listed (the
    no-quotes information line removes nothing, so it is left out of the sum)."""
    trades = sum(f["removed"] for f in res["funnel"] if f["unit"] == "trades" and not f.get("info"))
    assert trades + res["n_passed"] == res["n_considered"], res["funnel"]


# ------------------------------------------------------------------ hand-built chains
def bull_put_chain(long_92=None, short_94=None):
    l92 = dict(row(92, 0.70, 0.80, -0.17), **(long_92 or {}))
    s94 = dict(row(94, 1.20, 1.30, -0.25, iv=0.31), **(short_94 or {}))
    return chain({
        "2026-10-16": {"puts": [row(94, 0.20, 0.30, -0.25)]},                       # 7 DTE: outside 30-60
        "2026-11-20": {"puts": [row(90, 0.40, 0.50, -0.12), l92, s94, row(96, 1.90, 2.00, -0.33)]},
        "2026-11-27": {"puts": [row(92, 0.60, 0.70, -0.17), row(94, 0.95, 1.05, -0.24)]},   # credit too thin
    })


def test_credit_vertical_bull_put_hand_checked():
    res = run("bull_put", bull_put_chain(), und())
    assert res["n_passed"] == 1 and res["tickers"] == {"SYN": {"passed": 1, "reason": None}}
    c = res["rows"][0]
    assert c["id"] == "SYN|bull_put|2026-11-20|P|94|2026-11-20|P|92"
    assert c["symbol"] == "SYN" and c["strategy"] == "bull_put" and c["dte"] == 42
    assert c["net"] == 0.5 and c["net_natural"] == 0.4                   # 1.25 - 0.75 ; 1.20 - 0.80
    assert c["max_profit"] == 50.0 and c["max_loss"] == 150.0 and c["breakevens"] == [93.5]
    assert c["ror"] == round(0.5 / 1.5, 4)
    pop = above(100, 93.5, 0.31, 42)                                      # short leg's IV
    assert abs(c["pop"] - pop) < 1e-4 and abs(c["score"] - (0.5 / 1.5) * pop) < 1e-5
    m = c["metrics"]
    assert m["credit_pct"] == round(0.5 / 1.5 * 100, 4) and m["width"] == 2.0 and m["width_atr"] == 1.0
    assert m["short_delta"] == 0.25 and m["long_delta"] == 0.17
    assert [(l["side"], l["right"], l["strike"]) for l in c["legs"]] == [("sell", "P", 94.0), ("buy", "P", 92.0)]
    leg = c["legs"][0]
    assert leg["mid"] == 1.25 and leg["price"] == 1.25 and leg["iv"] == 0.31 and leg["delta"] == -0.25
    assert leg["as_of"] == FRESH and leg["source"] == "hermes" and leg["mdt"] == "live" and leg["oi"] == 1000
    assert c["liquidity"] == {"oi_min": 1000, "spread_max": 0.1, "spread_pct_max": round(0.1 / 0.75 * 100, 2),
                              "volume_min": 100}
    assert c["data"]["age_min"] == 10 and c["data"]["as_of_oldest"] == FRESH
    assert c["data"]["wall_age_min"] == 10                                # in session: market age = wall age
    assert c["data"]["sources"] == ["hermes·live"] and c["data"]["mixed"] is False
    assert c["underlying"] == {"spot": 100.0, "iv_rank": 55.0, "iv30": 30.0, "hv20": 28.0, "atr14": 2.0,
                               "earnings_date": "2027-06-01"}
    rm = removed(res)
    assert rm["dte"] == 1                                                 # the 7-DTE expiry
    assert rm["short_delta"] == 4 and rm["width"] == 1 and rm["credit"] == 1
    assert res["n_considered"] == 7
    check_invariant(res)
    units = {f["rule"]: f["unit"] for f in res["funnel"]}
    assert units["iv_rank"] == "tickers" and units["dte"] == "expiries" and units["credit"] == "trades"


def test_credit_vertical_bear_call_orientation():
    ch = chain({"2026-11-20": {"calls": [row(104, 1.90, 2.00, 0.33), row(106, 1.20, 1.30, 0.25, iv=0.29),
                                         row(108, 0.70, 0.80, 0.17), row(110, 0.40, 0.50, 0.12)]}})
    res = run("bear_call", ch, und())
    c = res["rows"][0]
    assert c["id"] == "SYN|bear_call|2026-11-20|C|106|2026-11-20|C|108" and res["n_passed"] == 1
    assert c["net"] == 0.5 and c["breakevens"] == [106.5] and c["max_loss"] == 150.0
    assert abs(c["pop"] - (1 - above(100, 106.5, 0.29, 42))) < 1e-4
    check_invariant(res)


def bull_call_chain():
    return chain({"2026-11-20": {"calls": [row(95, 7.40, 7.60, 0.65, iv=0.33), row(100, 4.40, 4.60, 0.50),
                                           row(105, 2.40, 2.60, 0.32), row(110, 1.10, 1.30, 0.18)]}})


def test_debit_vertical_bull_call_hand_checked():
    res = run("bull_call", bull_call_chain(), und(atr14=5.0))
    assert res["n_passed"] == 1
    c = res["rows"][0]
    assert c["id"] == "SYN|bull_call|2026-11-20|C|95|2026-11-20|C|105"
    assert c["net"] == -5.0 and c["net_natural"] == -5.2                   # 7.60 - 2.40
    assert c["max_profit"] == 500.0 and c["max_loss"] == 500.0 and c["breakevens"] == [100.0]
    assert c["ror"] == 1.0 and c["metrics"]["debit_pct"] == 50.0 and c["metrics"]["width_atr"] == 2.0
    pop = above(100, 100.0, 0.33, 42)                                      # bought leg's IV
    assert abs(c["pop"] - pop) < 1e-4 and abs(c["score"] - pop) < 1e-5
    assert [l["side"] for l in c["legs"]] == ["buy", "sell"]
    rm = removed(res)
    assert rm["long_delta"] == 3 and rm["short_delta"] == 2 and rm["width"] == 0 and res["n_considered"] == 6
    check_invariant(res)
    # the family rule: a 40% cap on the cost removes it
    res2 = run("bull_call", bull_call_chain(), und(atr14=5.0), rules("bull_call", debit_pct_max=40))
    assert res2["n_passed"] == 0 and removed(res2)["debit"] == 1
    # an ATR of 1.5 makes the 10-wide spread 6.7 ATR: over the 6.0 band; an ATR of 12 makes
    # it 0.83 ATR: under the 1.0 floor
    for atr in (1.5, 12.0):
        res3 = run("bull_call", bull_call_chain(), und(atr14=atr))
        assert res3["n_passed"] == 0 and removed(res3)["width"] == 1
        check_invariant(res3)


def test_debit_vertical_bear_put_orientation():
    ch = chain({"2026-11-20": {"puts": [row(90, 1.10, 1.30, -0.18), row(95, 2.40, 2.60, -0.32),
                                        row(100, 4.40, 4.60, -0.50), row(105, 7.40, 7.60, -0.65, iv=0.27)]}})
    res = run("bear_put", ch, und(atr14=5.0))
    c = res["rows"][0]
    assert c["id"] == "SYN|bear_put|2026-11-20|P|105|2026-11-20|P|95"
    assert c["net"] == -5.0 and c["breakevens"] == [100.0] and c["max_profit"] == 500.0
    assert abs(c["pop"] - (1 - above(100, 100.0, 0.27, 42))) < 1e-4
    check_invariant(res)


def test_single_buy_call_and_buy_put():
    ch = chain({"2026-11-20": {
        "calls": [row(94, 8.10, 8.30, 0.68, theta=-0.10), row(95, 7.40, 7.60, 0.65, theta=-0.05, iv=0.32),
                  row(100, 4.40, 4.60, 0.50)],
        "puts": [row(105, 7.40, 7.60, -0.65, theta=-0.05, iv=0.28), row(100, 4.40, 4.60, -0.50)]}})
    res = run("buy_call", ch, und(iv_rank=40.0))
    assert res["n_passed"] == 1
    c = res["rows"][0]
    assert c["id"] == "SYN|buy_call|2026-11-20|C|95" and [l["side"] for l in c["legs"]] == ["buy"]
    assert c["net"] == -7.5 and c["net_natural"] == -7.6 and c["max_loss"] == 750.0
    assert c["max_profit"] is None and c["ror"] is None and c["breakevens"] == [102.5]
    assert c["metrics"]["theta_pct"] == round(0.05 / 7.5 * 100, 4)
    assert abs(c["score"] - (-(0.05 / 7.5 * 100))) < 1e-5                 # delta 0.65 = the band middle
    assert abs(c["pop"] - above(100, 102.5, 0.32, 42)) < 1e-4
    rm = removed(res)
    assert rm["delta"] == 1 and rm["theta"] == 1 and res["n_considered"] == 3
    check_invariant(res)
    rp = run("buy_put", ch, und(iv_rank=40.0))
    p = rp["rows"][0]
    assert p["id"] == "SYN|buy_put|2026-11-20|P|105" and p["breakevens"] == [97.5]
    assert p["max_profit"] == 9750.0 and p["max_loss"] == 750.0 and p["ror"] == 13.0
    assert abs(p["pop"] - (1 - above(100, 97.5, 0.28, 42))) < 1e-4
    # IV rank 60 is over buy_call's 0-50 band: the ticker goes at the stock stage
    r60 = run("buy_call", ch, und(iv_rank=60.0))
    assert r60["n_passed"] == 0 and removed(r60)["iv_rank"] == 1
    assert r60["tickers"]["SYN"]["reason"] == "IV rank 60 is outside 0-50"


def leaps_chain():
    return chain({"2026-11-20": {"calls": [row(75, 25.0, 25.4, 0.95)]},
                  "2027-09-17": {"calls": [row(75, 27.0, 27.4, 0.84, iv=0.29), row(80, 23.0, 23.4, 0.80),
                                           row(100, 12.0, 12.4, 0.55)]}})


def test_leaps_hand_checked():
    # LEAPS allow earnings by default: the report on 2027-06-01 (before the 2027-09-17
    # expiry) does not matter, and the earnings rows are not in the funnel
    res = run("leaps_call", leaps_chain(), und(iv_rank=40.0), rules("leaps_call", extrinsic_pct_max=10))
    assert res["n_passed"] == 1
    c = res["rows"][0]
    assert c["id"] == "SYN|leaps_call|2027-09-17|C|75" and c["dte"] == 343
    assert c["net"] == -27.2 and c["max_loss"] == 2720.0 and c["max_profit"] is None
    assert c["breakevens"] == [102.2]
    assert c["metrics"]["intrinsic"] == 25.0 and c["metrics"]["extrinsic_pct"] == round(2.2 / 27.2 * 100, 4)
    assert abs(c["score"] - (-(2.2 / 27.2 * 100) - abs(0.84 - 0.775) / 100)) < 1e-5
    assert abs(c["pop"] - above(100, 102.2, 0.29, 343)) < 1e-4
    rm = removed(res)
    assert rm["dte"] == 1 and rm["delta"] == 1 and rm["extrinsic"] == 1      # 80: 3.2 / 23.2 = 13.8%
    assert "earnings" not in rm and "earnings_known" not in rm
    check_invariant(res)
    # the default 25% time-value cap keeps the 80 call too (13.8%), the best (75) first
    rd = run("leaps_call", leaps_chain(), und(iv_rank=40.0))
    assert [c["id"] for c in rd["rows"]] == ["SYN|leaps_call|2027-09-17|C|75", "SYN|leaps_call|2027-09-17|C|80"]
    assert removed(rd)["extrinsic"] == 0
    # "no earnings before the last expiry" (and short_leg - one option): the report removes the expiry
    for rule in ("none_inside", "short_leg"):
        r2 = run("leaps_call", leaps_chain(), und(iv_rank=40.0), rules("leaps_call", earnings_rule=rule))
        assert r2["n_passed"] == 0 and removed(r2)["earnings"] == 1
        assert r2["tickers"]["SYN"]["reason"] == "no expiry passes: earnings on or before the expiry"


def diagonal_chain(long_extra=()):
    return chain({"2026-11-20": {"calls": [row(108, 1.45, 1.55, 0.25), row(112, 0.70, 0.80, 0.15)]},
                  "2027-04-16": {"calls": [row(90, 16.90, 17.10, 0.75), row(100, 11.0, 11.2, 0.60), *long_extra]}})


def test_diagonal_hand_checked():
    u = und(iv_rank=40.0)
    res = run("diagonal_call", diagonal_chain(), u)
    assert res["n_passed"] == 1
    c = res["rows"][0]
    assert c["id"] == "SYN|diagonal_call|2027-04-16|C|90|2026-11-20|C|108" and c["dte"] == 42
    assert [(l["side"], l["expiry"]) for l in c["legs"]] == [("buy", "2027-04-16"), ("sell", "2026-11-20")]
    assert c["net"] == -15.5 and c["net_natural"] == -15.65 and c["max_loss"] == 1550.0
    assert c["metrics"]["debit_pct_spot"] == 15.5 and abs(c["score"] - (-15.5 / 0.75)) < 1e-5
    # max profit / breakevens from the near-expiry curve: checked against payoff.pnl on a fine grid
    legs = [payoff.Leg(right="C", strike=90.0, expiry="2027-04-16", qty=1, price=17.0, iv=0.30),
            payoff.Leg(right="C", strike=108.0, expiry="2026-11-20", qty=-1, price=1.5, iv=0.30)]
    fine = max(payoff.pnl(legs, 80 + i * 0.05, 42, TODAY) for i in range(1000))
    assert abs(c["max_profit"] - fine) < 1.0 and c["max_profit"] > 0
    assert c["breakevens"] and all(abs(payoff.pnl(legs, b, 42, TODAY)) < 1.0 for b in c["breakevens"])
    assert 0 < c["pop"] < 1 and c["ror"] == round(c["max_profit"] / 1550.0, 4)
    rm = removed(res)
    assert rm["long_delta"] == 2 and rm["short_delta"] == 1 and res["n_considered"] == 4
    check_invariant(res)
    # cost cap 10% of the stock price removes it; a long call above the sold strike breaks the order
    r2 = run("diagonal_call", diagonal_chain(), u, rules("diagonal_call", debit_pct_spot_max=10))
    assert r2["n_passed"] == 0 and removed(r2)["debit_spot"] == 1
    r3 = run("diagonal_call", diagonal_chain([row(110, 1.0, 1.2, 0.78)]), u)
    assert removed(r3)["strike_order"] == 1
    check_invariant(r3)


def calendar_chain(back_iv=0.30):
    return chain({
        "2026-11-06": {"calls": [row(98, 3.40, 3.60, 0.56, iv=0.32), row(100, 2.40, 2.60, 0.51, iv=0.32),
                                 row(102, 1.60, 1.80, 0.45, iv=0.32)]},
        "2026-12-18": {"calls": [row(98, 5.00, 5.20, 0.55, iv=back_iv), row(100, 4.00, 4.20, 0.52, iv=back_iv),
                                 row(102, 3.10, 3.30, 0.48, iv=back_iv)]}})


def test_calendar_hand_checked():
    u = und(iv_rank=40.0)
    res = run("calendar", calendar_chain(), u)
    assert res["n_passed"] == 1
    c = res["rows"][0]
    assert c["id"] == "SYN|calendar|2026-11-06|C|100|2026-12-18|C|100" and c["dte"] == 28
    assert [l["side"] for l in c["legs"]] == ["sell", "buy"]
    assert c["net"] == -1.6 and c["max_loss"] == 160.0 and abs(c["score"] - (-1.6 / 42)) < 1e-6
    assert c["max_profit"] > 0 and len(c["breakevens"]) == 2
    lo, hi = c["breakevens"]
    assert lo < 100 < hi and 0 < c["pop"] < 1
    rm = removed(res)
    assert rm["atm"] == 2 and res["n_considered"] == 3
    assert "iv_order" not in rm                                             # off by default: not listed
    check_invariant(res)
    on = rules("calendar", front_iv_ge_back=True)
    assert run("calendar", calendar_chain(), u, on)["n_passed"] == 1        # 0.32 >= 0.30
    r2 = run("calendar", calendar_chain(back_iv=0.34), u, on)
    assert r2["n_passed"] == 0 and removed(r2)["iv_order"] == 1
    r3 = run("calendar", calendar_chain(), u, rules("calendar", delta_tol=0.01))
    assert r3["n_passed"] == 1                                              # 0.51 is within 0.01
    r4 = run("calendar", calendar_chain(), u, rules("calendar", delta_tol=0.01, front_dte_lo=29))
    assert r4["n_passed"] == 0 and removed(r4)["dte"] == 1                  # 28 DTE front now outside


def condor_chain():
    return chain({"2026-11-13": {
        "puts": [row(88, 0.25, 0.35, -0.08), row(90, 0.45, 0.55, -0.12), row(92, 0.80, 0.90, -0.17, iv=0.32)],
        "calls": [row(108, 0.75, 0.85, 0.18, iv=0.28), row(110, 0.40, 0.50, 0.12), row(112, 0.20, 0.30, 0.07)]}})


def test_condor_hand_checked():
    res = run("iron_condor", condor_chain(), und())
    assert res["n_passed"] == 1
    c = res["rows"][0]
    assert c["id"] == ("SYN|iron_condor|2026-11-13|P|90|2026-11-13|P|92|2026-11-13|C|108|2026-11-13|C|110")
    assert [l["side"] for l in c["legs"]] == ["buy", "sell", "sell", "buy"]
    assert c["net"] == 0.7 and c["max_profit"] == 70.0 and c["max_loss"] == 130.0
    assert c["breakevens"] == [91.3, 108.7] and c["dte"] == 35
    assert c["metrics"]["credit_pct"] == round(0.7 / 1.3 * 100, 4) and c["metrics"]["wing"] == 2.0
    pop = above(100, 91.3, 0.30, 35) - above(100, 108.7, 0.30, 35)          # sigma = mean of the shorts
    assert abs(c["pop"] - pop) < 1e-4 and abs(c["score"] - (0.7 / 1.3) * pop) < 1e-5
    rm = removed(res)
    assert rm["short_delta"] == 5 and rm["wing"] == 3 and res["n_considered"] == 9
    check_invariant(res)
    r2 = run("iron_condor", condor_chain(), und(), rules("iron_condor", credit_pct_min=60))
    assert r2["n_passed"] == 0 and removed(r2)["credit"] == 1
    r3 = run("iron_condor", condor_chain(), und(iv_rank=45.0))
    assert r3["n_passed"] == 0 and removed(r3)["iv_rank"] == 1               # condor wants IV rank 50+


# ------------------------------------------------------------------ shared filters
def test_stock_filters_per_ticker():
    good = bull_put_chain()
    chains = {s: good for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "HHH")}
    unds = {"AAA": und(), "BBB": und(spot=15.0), "CCC": und(iv_rank=20.0), "DDD": und(iv_rank=None),
            "EEE": und(earnings_date=None), "FFF": und(atr14=None), "GGG": und(),
            "HHH": und(avg_vol20=500_000.0)}
    r = rules("bull_put", {"stock_vol_min": 1_000_000})
    res = opt_screen.screen("bull_put", chains, unds, r, today=TODAY, now=NOW)
    tk = res["tickers"]
    assert tk["AAA"] == {"passed": 1, "reason": None}
    assert tk["BBB"]["reason"] == "stock price 15.00 is under 20"
    assert tk["CCC"]["reason"] == "IV rank 20 is outside 30-100"
    assert tk["DDD"]["reason"] == "IV rank not known yet"
    assert tk["EEE"]["reason"] == "earnings date unknown"
    assert tk["FFF"]["reason"] == "ATR not known yet"
    assert tk["GGG"]["reason"] == "no option data yet"
    assert tk["HHH"]["reason"] == "20-day stock volume 500,000 is under 1,000,000"
    rm = removed(res)
    assert (rm["data"], rm["price_min"], rm["stock_vol_min"], rm["iv_rank"], rm["atr"], rm["earnings_known"]) \
        == (1, 1, 1, 2, 1, 1)
    assert res["n_passed"] == 1 and [c["symbol"] for c in res["rows"]] == ["AAA"]
    # earnings allowed (the strategy's own rule): the unknown date no longer matters
    r2 = rules("bull_put", earnings_rule="allow")
    res2 = opt_screen.screen("bull_put", {"EEE": good}, {"EEE": und(earnings_date=None)}, r2, today=TODAY, now=NOW)
    assert res2["n_passed"] == 1 and "earnings_known" not in removed(res2)
    # an old-style SHARED earnings rule is still read (moved onto the strategy)
    r_old = rules("bull_put", {"earnings_rule": "allow"})
    assert r_old["rules"]["earnings_rule"] == "allow" and "earnings_rule" not in r_old["shared"]
    assert opt_screen.screen("bull_put", {"EEE": good}, {"EEE": und(earnings_date=None)}, r_old,
                             today=TODAY, now=NOW)["n_passed"] == 1
    # a past earnings date is stale: unknown
    res3 = run("bull_put", good, und(earnings_date="2026-10-01"))
    assert res3["tickers"]["SYN"]["reason"] == "earnings date unknown"


def test_expiry_filters_monthly_and_earnings():
    ch = chain({"2026-11-20": {"puts": [row(92, 0.70, 0.80, -0.17), row(94, 1.20, 1.30, -0.25)]},
                "2026-11-27": {"puts": [row(92, 0.80, 0.90, -0.18), row(94, 1.30, 1.40, -0.26)]}})
    both = run("bull_put", ch, und())
    assert both["n_passed"] == 2
    monthly = run("bull_put", ch, und(), rules("bull_put", {"monthly_only": True}))
    assert monthly["n_passed"] == 1 and monthly["rows"][0]["legs"][0]["expiry"] == "2026-11-20"
    assert removed(monthly)["monthly"] == 1
    # earnings 2026-11-25: after the Nov 20 expiry, before Nov 27 -> only Nov 27 goes
    e = run("bull_put", ch, und(earnings_date="2026-11-25"))
    assert e["n_passed"] == 1 and e["rows"][0]["legs"][0]["expiry"] == "2026-11-20"
    assert removed(e)["earnings"] == 1
    # earnings ON the expiry day counts as inside
    e2 = run("bull_put", ch, und(earnings_date="2026-11-20"))
    assert e2["n_passed"] == 0 and removed(e2)["earnings"] == 2
    # short_leg on a one-expiry trade is the same rule (both legs share the expiry)
    s = run("bull_put", ch, und(earnings_date="2026-11-25"), rules("bull_put", earnings_rule="short_leg"))
    assert [c["id"] for c in s["rows"]] == [c["id"] for c in e["rows"]] and removed(s)["earnings"] == 1
    assert {f["rule"]: f["label"] for f in s["funnel"]}["earnings"] == "Earnings on or before the expiry"


def test_earnings_rule_is_the_strategys_own():
    """An unknown earnings date removes a ticker only when THAT strategy's rule is not
    "allow"; one strategy's choice never reaches another."""
    nodate = und(iv_rank=40.0, earnings_date=None)
    assert run("bull_put", bull_put_chain(), und(earnings_date=None))["tickers"]["SYN"]["reason"] \
        == "earnings date unknown"
    rl = run("leaps_call", leaps_chain(), nodate)                     # LEAPS allow by default: no date needed
    assert rl["n_passed"] == 2 and "earnings_known" not in removed(rl) and "earnings" not in removed(rl)
    for strategy, ch in (("calendar", calendar_chain()), ("diagonal_call", diagonal_chain())):
        res = run(strategy, ch, nodate)                               # short_leg needs the date
        assert res["n_passed"] == 0 and res["tickers"]["SYN"]["reason"] == "earnings date unknown"
        assert removed(res)["earnings_known"] == 1
        assert run(strategy, ch, nodate, rules(strategy, earnings_rule="allow"))["n_passed"] == 1
    bear = chain({"2026-11-20": {"calls": [row(106, 1.20, 1.30, 0.25), row(108, 0.70, 0.80, 0.17)]}})
    prefs = opt_rules.clean({"schema": 2, "bull_put": {"earnings_rule": "allow"}})     # read() output
    res = opt_screen.screen("bear_call", {"SYN": bear}, {"SYN": und(earnings_date=None)}, prefs, today=TODAY, now=NOW)
    assert res["n_passed"] == 0 and res["tickers"]["SYN"]["reason"] == "earnings date unknown"
    assert opt_screen.screen("bull_put", {"SYN": bull_put_chain()}, {"SYN": und(earnings_date=None)}, prefs,
                             today=TODAY, now=NOW)["n_passed"] == 1


@pytest.mark.parametrize("strategy, make, between, on_sold", [
    # calendar: sells 2026-11-06, buys 2026-12-18; diagonal: sells 2026-11-20, buys 2027-04-16
    ("calendar", calendar_chain, "2026-11-25", "2026-11-06"),
    ("diagonal_call", diagonal_chain, "2027-01-28", "2026-11-20"),
])
def test_short_leg_checks_only_the_sold_leg(strategy, make, between, on_sold):
    ch = make()
    # a report between the two expiries: fine for the default (short_leg) ...
    res = run(strategy, ch, und(iv_rank=40.0, earnings_date=between))
    assert res["n_passed"] == 1 and removed(res)["earnings"] == 0
    labels = {f["rule"]: f["label"] for f in res["funnel"]}
    assert labels["earnings"] == "Earnings on or before the sold call's expiry"
    cid = res["rows"][0]["id"]
    d = opt_screen.detail(strategy, cid, ch, und(iv_rank=40.0, earnings_date=between), rules(strategy),
                          today=TODAY, now=NOW)
    assert d["fails"] is None
    # ... but "no earnings before the last expiry" removes the far expiry
    strict = rules(strategy, earnings_rule="none_inside")
    r2 = run(strategy, ch, und(iv_rank=40.0, earnings_date=between), strict)
    assert r2["n_passed"] == 0 and removed(r2)["earnings"] == 1
    assert r2["tickers"]["SYN"]["reason"] == "no expiry passes: earnings on or before the expiry"
    d2 = opt_screen.detail(strategy, cid, ch, und(iv_rank=40.0, earnings_date=between), strict, today=TODAY, now=NOW)
    assert d2["fails"]["rule"] == "earnings"
    # a report on the sold leg's expiry day: short_leg removes the sold expiry
    r3 = run(strategy, ch, und(iv_rank=40.0, earnings_date=on_sold))
    assert r3["n_passed"] == 0 and removed(r3)["earnings"] == 1
    assert r3["tickers"]["SYN"]["reason"] == "no expiry passes: earnings on or before the sold call's expiry"
    d3 = opt_screen.detail(strategy, cid, ch, und(iv_rank=40.0, earnings_date=on_sold), rules(strategy),
                           today=TODAY, now=NOW)
    assert d3["fails"] == {"rule": "earnings", "label": "Earnings on or before the sold call's expiry"}
    check_invariant(r3)


def test_short_leg_takes_away_only_the_sold_role():
    """With overlapping windows one expiry can be either leg of a calendar; a report
    on or before it removes only its near (sold) role - none_inside removes it."""
    today = dt.date.fromisoformat(TODAY)
    for rule, want in (("short_leg", (None, {"back"})), ("none_inside", ("earnings", set()))):
        r = rules("calendar", front_dte_hi=70, back_dte_lo=20, earnings_rule=rule)
        t = opt_screen._T("calendar", "SYN", calendar_chain(), und(earnings_date="2026-11-01"), r["shared"],
                          r["rules"], today, NOW)
        windows = opt_screen._windows("calendar", r["rules"])
        assert opt_screen._expiry_fail(t, dt.date(2026, 11, 6), 28, windows) == want
        # a report after the expiry leaves both roles
        t.earnings = dt.date(2026, 11, 9)
        assert opt_screen._expiry_fail(t, dt.date(2026, 11, 6), 28, windows) == (None, {"front", "back"})


def test_is_monthly():
    assert opt_screen.is_monthly(dt.date(2026, 11, 20)) and not opt_screen.is_monthly(dt.date(2026, 11, 27))
    assert opt_screen.is_monthly(dt.date(2027, 1, 15)) and not opt_screen.is_monthly(dt.date(2027, 1, 22))
    # June 2027: the third Friday (18th) is the observed Juneteenth holiday, so Thursday the 17th
    assert opt_screen.is_monthly(dt.date(2027, 6, 17)) and not opt_screen.is_monthly(dt.date(2027, 6, 10))


@pytest.mark.parametrize("long_92, shared, own, rule, n", [
    ({"oi": 50}, {}, {}, "oi", 1),
    ({"oi": None}, {}, {}, "oi", 1),
    ({"volume": 5}, {"opt_vol_min": 10}, {}, "opt_vol", 1),
    ({"bid": 0.60, "ask": 0.90, "mid": 0.75}, {}, {"max_leg_spread": 0.25}, "spread", 1),   # the strategy's own cap
    ({"bid": 0.60, "ask": 0.90, "mid": 0.75}, {"max_leg_spread": 0.25}, {}, "spread", 1),   # an old shared one, lifted
    ({}, {"max_leg_spread_pct": 10}, {}, "spread_pct", 2),     # 0.10 / 0.75 = 13.3%; Nov 27 0.10 / 0.65 too
    ({"bid": None, "mid": None}, {}, {}, "quote", 1),
    ({"bid": None, "ask": None, "mid": None}, {}, {}, "quote", 1),             # no quotes and no model price
    ({"bid": 0.0, "ask": 0.0, "mid": 0.0}, {}, {}, "quote", 1),                # a zero price is no price
    ({"bid": 0.90, "ask": 0.80, "mid": 0.85}, {}, {}, "quote", 1),             # a crossed quote is no price
    ({"oi": 50, "bid": 0.30, "ask": 1.20, "mid": 0.75}, {}, {}, "oi", 1),     # first failure wins: oi before spread
    ({"as_of": NOW - dt.timedelta(hours=30)}, {}, {}, "age", 1),
    ({"as_of": None}, {}, {}, "age", 1),
])
def test_leg_filters(long_92, shared, own, rule, n):
    res = run("bull_put", bull_put_chain(long_92=long_92), und(), rules("bull_put", shared, **own))
    assert res["n_passed"] == 0
    rm = removed(res)
    assert rm[rule] == n
    assert sum(rm.get(k, 0) for k in opt_screen.LEG_RULES) == n
    check_invariant(res)


def test_inactive_rules_are_not_in_the_funnel():
    keys = [f["rule"] for f in run("bull_put", bull_put_chain(), und())["funnel"]]
    assert "opt_vol" not in keys and "stock_vol_min" not in keys and "monthly" not in keys
    assert opt_screen.NO_QUOTES not in keys                       # every leg quoted: no information line
    assert keys[0] == "data" and keys[-1] == "per_ticker"
    assert keys.index("iv_rank") < keys.index("dte") < keys.index("short_delta") < keys.index("quote") \
        < keys.index("credit")
    k2 = [f["rule"] for f in run("bull_put", bull_put_chain(), und(),
                                 rules("bull_put", {"oi_min": 0}, earnings_rule="allow"))["funnel"]]
    assert "earnings" not in k2 and "earnings_known" not in k2 and "oi" not in k2
    # oi_min 0 turns the check off: an option with no reported open interest passes
    assert run("bull_put", bull_put_chain(long_92={"oi": None}), und(),
               rules("bull_put", {"oi_min": 0}))["n_passed"] == 1
    k3 = [f["rule"] for f in run("buy_call", bull_put_chain(), und(), rules("buy_call", iv_rank_max=100))["funnel"]]
    assert "iv_rank" not in k3 and "atr" not in k3


def test_stale_quotes_and_age():
    old = NOW - dt.timedelta(hours=30)
    ch = bull_put_chain(long_92={"as_of": old})
    assert run("bull_put", ch, und())["n_passed"] == 0
    res = run("bull_put", ch, und(), rules("bull_put", {"max_age_h": 48}))
    c = res["rows"][0]
    assert c["data"]["age_min"] == 30 * 60 and c["data"]["as_of_oldest"] == old    # the OLDEST leg
    assert c["legs"][1]["as_of"] == old and c["legs"][0]["as_of"] == FRESH
    # an aware timestamp and an ISO string are read as UTC
    ch2 = bull_put_chain(long_92={"as_of": (NOW - dt.timedelta(minutes=90)).replace(tzinfo=dt.timezone.utc)},
                         short_94={"as_of": (NOW - dt.timedelta(minutes=20)).isoformat()})
    assert run("bull_put", ch2, und())["rows"][0]["data"]["age_min"] == 90


# ------------------------------------------------------------------ market-time age
FRI_CLOSE = dt.datetime(2026, 10, 9, 20, 0)              # Fri 16:00 ET (EDT) = 20:00 UTC
FRI_EVENING = dt.datetime(2026, 10, 10, 0, 30)           # Fri 20:30 ET - Hermes's EOD pass
SAT = dt.datetime(2026, 10, 10, 16, 0)                   # Sat 12:00 ET
SUN = dt.datetime(2026, 10, 11, 20, 0)                   # Sun 16:00 ET (Mon 04:00 MYT)
MON_PREOPEN = dt.datetime(2026, 10, 12, 13, 0)           # Mon 09:00 ET
MON_SESSION = dt.datetime(2026, 10, 12, 14, 0)           # Mon 10:00 ET


def restamp(ch: dict, as_of) -> dict:
    """A copy of a chain view with every row's ``as_of`` set."""
    ch = copy.deepcopy(ch)
    for e in ch["expiries"]:
        for side in ("calls", "puts"):
            for r in e[side]:
                r["as_of"] = as_of
    return ch


def at(strategy, ch, u, r, now):
    """screen() at ``now`` with that moment's ET date as today."""
    return opt_screen.screen(strategy, {"SYN": ch}, {"SYN": u}, r, today=clock.et_date(now), now=now)


def test_market_now_and_market_age():
    assert opt_screen.market_now(NOW) == NOW                                     # Fri 14:00 ET: open
    assert opt_screen.market_now(dt.datetime(2026, 10, 9, 20, 30)) == FRI_CLOSE  # Fri 16:30 ET
    for now in (SAT, SUN, MON_PREOPEN, SUN.replace(tzinfo=dt.timezone.utc)):
        assert opt_screen.market_now(now) == FRI_CLOSE
    assert opt_screen.market_now(MON_SESSION) == MON_SESSION
    # Labor Day 2026-09-07: on Tuesday before the open the clock still stands at Friday's close
    tue = dt.datetime(2026, 9, 8, 12, 0)
    assert opt_screen.market_now(tue) == dt.datetime(2026, 9, 4, 20, 0)
    assert opt_screen.market_age_min(dt.datetime(2026, 9, 5, 0, 30), tue) == 0          # ~84 h of wall clock
    # 0 after the close; an older quote keeps the age it had at the close; wall clock in session
    assert opt_screen.market_age_min(FRI_EVENING, SUN) == 0
    assert opt_screen.market_age_min(FRI_EVENING.isoformat() + "Z", SUN) == 0
    assert opt_screen.market_age_min(dt.datetime(2026, 10, 9, 18, 0), SAT) == 120       # Fri 14:00 ET
    assert opt_screen.market_age_min(FRI_EVENING, MON_SESSION) == int(61.5 * 60)
    assert opt_screen.market_age_min(None, SUN) is None


def test_weekend_quotes_stay_current_in_market_time():
    """Friday evening's closing quotes are the latest prices until Monday's open: the 24 h
    default keeps them all weekend (a wall clock dropped them from Saturday evening ET on),
    at age 0, with the wall-clock age kept for the tooltip."""
    ch = restamp(bull_put_chain(), FRI_EVENING)
    cid = "SYN|bull_put|2026-11-20|P|94|2026-11-20|P|92"
    for now in (SAT, SUN, MON_PREOPEN):
        res = at("bull_put", ch, und(), rules("bull_put"), now)
        assert res["n_passed"] == 1, res["tickers"]
        d = res["rows"][0]["data"]
        assert d["age_min"] == 0 and d["as_of_oldest"] == FRI_EVENING
        assert d["wall_age_min"] == round((now - FRI_EVENING).total_seconds() / 60)
        det = opt_screen.detail("bull_put", cid, ch, und(), rules("bull_put"), today=clock.et_date(now), now=now)
        assert det["fails"] is None and det["candidate"]["data"] == d
    # in the Monday session the age is wall clock again: 61.5 h - too old for 24 h, fine for 72 h
    mon = at("bull_put", ch, und(), rules("bull_put"), MON_SESSION)
    assert mon["n_passed"] == 0 and removed(mon)["age"] == 2
    ok = at("bull_put", ch, und(), rules("bull_put", {"max_age_h": 72}), MON_SESSION)
    assert ok["rows"][0]["data"]["age_min"] == ok["rows"][0]["data"]["wall_age_min"] == int(61.5 * 60)


def test_closed_market_freezes_an_older_quotes_age():
    # read Friday 14:00 ET, looked at on Saturday: 2 h old at the close, still 2 h old
    two_h = restamp(bull_put_chain(), dt.datetime(2026, 10, 9, 18, 0))
    d = at("bull_put", two_h, und(), rules("bull_put"), SAT)["rows"][0]["data"]
    assert d["age_min"] == 120 and d["wall_age_min"] == 22 * 60
    # read Thursday 15:00 ET: a whole session missed - 25 h old at Friday's close
    thu = restamp(bull_put_chain(), dt.datetime(2026, 10, 8, 19, 0))
    res = at("bull_put", thu, und(), rules("bull_put"), SUN)
    assert res["n_passed"] == 0 and removed(res)["age"] == 2
    # the oldest leg sets the trade's age on both clocks
    mixed = bull_put_chain(long_92={"as_of": dt.datetime(2026, 10, 9, 18, 0)}, short_94={"as_of": FRI_EVENING})
    d2 = at("bull_put", mixed, und(), rules("bull_put"), SUN)["rows"][0]["data"]
    assert d2["as_of_oldest"] == dt.datetime(2026, 10, 9, 18, 0) and d2["age_min"] == 120
    assert d2["wall_age_min"] == round((SUN - dt.datetime(2026, 10, 9, 18, 0)).total_seconds() / 60)


# ------------------------------------------------------------------ "why nothing passed"
AGE_REASON = ("no trade passes: the last rule in the way is quote older than 24 h on an option "
              "(or undated; the clock stops while the market is closed)")


def test_reason_names_the_last_rule_in_the_way():
    """The band rules always remove the most; the reason names the furthest stage that
    removed anything - the rule that, loosened alone, lets the furthest trades through."""
    stale = restamp(bull_put_chain(), NOW - dt.timedelta(hours=30))
    res = run("bull_put", stale, und())
    rm = removed(res)
    assert rm["short_delta"] == 4 and rm["age"] == 2                     # the band removed the most ...
    assert res["tickers"]["SYN"]["reason"] == AGE_REASON                  # ... the age stopped the rest
    # one pair fails the credit rule (the last stage), the other the age: credit is named
    mixed = bull_put_chain(long_92={"as_of": NOW - dt.timedelta(hours=30)})
    r2 = run("bull_put", mixed, und())
    assert removed(r2)["age"] == 1 and removed(r2)["credit"] == 1
    assert r2["tickers"]["SYN"]["reason"] == "no trade passes: the last rule in the way is credit under 25% of the max loss"
    # nothing gets past the bands: the last band rule that removed any (labels keep "ATR")
    r3 = run("bull_put", bull_put_chain(), und(), rules("bull_put", short_delta_lo=0.40, short_delta_hi=0.45))
    assert r3["tickers"]["SYN"]["reason"] == ("no trade passes: the last rule in the way is sold option delta "
                                              "outside 0.40-0.45 (or not reported)")
    r4 = run("bull_put", bull_put_chain(), und(atr14=10.0))               # 5-15 wide: every pair too narrow
    assert removed(r4)["short_delta"] > removed(r4)["width"] > 0
    assert r4["tickers"]["SYN"]["reason"] == ("no trade passes: the last rule in the way is distance between "
                                              "strikes outside 0.5-1.5 x ATR")
    # a $ cap on the LEAPS (the old flat default) is named, not the delta band
    r5 = run("leaps_call", leaps_chain(), und(iv_rank=40.0), rules("leaps_call", max_leg_spread=0.25))
    assert removed(r5)["spread"] == 2
    assert r5["tickers"]["SYN"]["reason"] == "no trade passes: the last rule in the way is bid/ask wider than $0.25 on an option"


@pytest.mark.parametrize("strategy", ("bull_put", "iron_condor", "buy_call"))
def test_monday_session_on_friday_quotes(synth_iv, strategy):
    """The reviewer's case on the synthetic chain: Hermes's Friday-evening quotes list
    trades all weekend; in Monday's session (Gateway off, no connector) they are 61 h
    old, and the reason says so - not the delta band."""
    ch, u, r = restamp(synth_iv[0.35], FRI_EVENING), default_und(strategy, 0.35), house(strategy)
    assert at(strategy, ch, u, r, SUN)["n_passed"] >= 1
    mon = at(strategy, ch, u, r, MON_SESSION)
    assert mon["n_passed"] == 0 and mon["tickers"]["SYN"]["reason"] == AGE_REASON
    check_invariant(mon)


# ------------------------------------------------------------------ the bid/ask $ cap, per strategy
def test_dollar_cap_is_the_strategys_own():
    for s in opt_rules.STRATEGIES:
        r = house(s)
        keys = [k for k, _, _ in opt_screen.catalog(s, r["shared"], r["rules"])]
        assert ("spread" in keys) == (s in ("bull_put", "bear_call", "iron_condor")), s
        assert "spread_pct" in keys
    # a bought call $0.60 wide at $7.50 (8% of its price): fine for a bull call spread (cap off) ...
    wide = chain({"2026-11-20": {"calls": [row(95, 7.20, 7.80, 0.65, iv=0.33), row(100, 4.40, 4.60, 0.50),
                                           row(105, 2.40, 2.60, 0.32), row(110, 1.10, 1.30, 0.18)]}})
    res = run("bull_call", wide, und(atr14=5.0))
    assert res["n_passed"] == 1 and res["rows"][0]["liquidity"]["spread_max"] == 0.6
    # ... until that strategy sets a cap of its own
    r2 = run("bull_call", wide, und(atr14=5.0), rules("bull_call", max_leg_spread=0.50))
    assert r2["n_passed"] == 0 and removed(r2)["spread"] == 1
    assert {f["rule"]: f["label"] for f in r2["funnel"]}["spread"] == "Bid/ask wider than $0.50 on an option"
    # a premium seller has the $0.50 on by default; 0 turns it off (the % rule still applies)
    wide_short = bull_put_chain(short_94={"bid": 0.95, "ask": 1.55, "mid": 1.25})
    assert removed(run("bull_put", wide_short, und()))["spread"] == 1
    off = run("bull_put", wide_short, und(), rules("bull_put", max_leg_spread=0))
    assert "spread" not in removed(off) and removed(off)["spread_pct"] == 1
    # an old row's SHARED cap reaches the premium sellers only
    assert removed(run("bull_call", wide, und(atr14=5.0), rules("bull_call", {"max_leg_spread": 0.50}))) \
        .get("spread") is None


def test_mixed_source_legs():
    ch = bull_put_chain(short_94={"as_of": NOW - dt.timedelta(minutes=2), "source": "member",
                                  "source_user_id": 7, "source_name": "Kui", "mdt": "live"},
                        long_92={"as_of": NOW - dt.timedelta(minutes=45), "mdt": "delayed"})
    c = run("bull_put", ch, und())["rows"][0]
    short, long_ = c["legs"]
    assert (short["source"], short["source_user_id"], short["source_name"], short["mdt"]) == ("member", 7, "Kui", "live")
    assert (long_["source"], long_["source_user_id"], long_["mdt"]) == ("hermes", None, "delayed")
    assert c["data"]["sources"] == ["member:Kui·live", "hermes·delayed"] and c["data"]["mixed"] is True
    assert c["data"]["age_min"] == 45
    ch2 = bull_put_chain(short_94={"source": "member", "source_user_id": 7, "source_name": None})
    assert run("bull_put", ch2, und())["rows"][0]["data"]["sources"][0] == "member:#7·live"


def test_no_data_and_empty_inputs():
    res = opt_screen.screen("bull_put", {}, {}, None, today=TODAY, now=NOW)
    assert res["rows"] == [] and res["n_passed"] == 0 and res["n_considered"] == 0 and res["tickers"] == {}
    assert res["funnel"][0]["rule"] == "data"
    res2 = opt_screen.screen("bull_put", {"ZZZ": {"symbol": "ZZZ", "spot": None, "expiries": []}}, {},
                             None, today=TODAY, now=NOW)
    assert res2["tickers"]["ZZZ"] == {"passed": 0, "reason": "no option data yet"}
    with pytest.raises(KeyError):
        opt_screen.screen("straddle", {}, {}, None)


def test_screen_reads_rules_from_read_output_too():
    prefs = opt_rules.clean({"schema": 2, "bull_put": {"credit_pct_min": 40}})
    a = opt_screen.screen("bull_put", {"SYN": bull_put_chain()}, {"SYN": und()}, prefs, today=TODAY, now=NOW)
    assert a["n_passed"] == 0 and removed(a)["credit"] == 2                 # 33% and 21% both under 40


# ------------------------------------------------------------------ synthetic chains (fixture chain_bs)
EXPIRIES = (
    [(dt.date(2026, 10, 16) + dt.timedelta(weeks=i)).isoformat() for i in range(13)]       # weeklies to Jan 8
    + ["2027-01-15", "2027-02-19", "2027-03-19", "2027-04-16", "2027-05-21", "2027-06-17", "2027-07-16",
       "2027-08-20", "2027-09-17", "2027-12-17", "2028-01-21", "2028-06-16"])
STRIKES = [71 + 2 * i for i in range(30)]


def view_from_bs(ch: dict, *, as_of=FRESH, source="hermes", mdt="live", today=TODAY) -> dict:
    """``chain_bs`` output -> the ``opt_store.chain_view`` shape (iv stays a fraction,
    ``open_interest`` becomes ``oi``)."""
    by: dict[str, dict] = {}
    for (exp, right, _k), l in ch["legs"].items():
        by.setdefault(exp, {"calls": [], "puts": []})["calls" if right == "C" else "puts"].append({
            "strike": l["strike"], "bid": l["bid"], "ask": l["ask"], "mid": l["mid"], "last": l["last"],
            "bid_size": int(l["bid_size"]), "ask_size": int(l["ask_size"]), "volume": int(l["volume"]),
            "oi": int(l["open_interest"]), "iv": l["iv"], "delta": l["delta"], "gamma": l["gamma"],
            "theta": l["theta"], "vega": l["vega"], "und_price": ch["spot"], "as_of": as_of, "source": source,
            "source_user_id": None, "source_name": None, "mdt": mdt})
    t0 = dt.date.fromisoformat(today)
    return {"symbol": ch["symbol"], "spot": ch["spot"], "spot_as_of": as_of, "spot_source": source, "spot_mdt": mdt,
            "expiries": [{"expiry": e, "dte": (dt.date.fromisoformat(e) - t0).days,
                          "calls": sorted(by[e]["calls"], key=lambda r: r["strike"]),
                          "puts": sorted(by[e]["puts"], key=lambda r: r["strike"])} for e in sorted(by)]}


@pytest.fixture(scope="module")
def synth():
    ch = chain_bs(100.0, 0.35, EXPIRIES, STRIKES, today=TODAY, skew=0.2, spread=0.10, symbol="SYN")
    return view_from_bs(ch)


SYN_UND = und(atr14=3.0, iv30=35.0, hv20=30.0, iv_rank=55.0, earnings_date="2028-12-01")


def open_rules(strategy, **shared):
    """Every strategy screens: IV-rank band open, earnings allowed."""
    return rules(strategy, shared, iv_rank_min=0, iv_rank_max=100, earnings_rule="allow")


def test_synthetic_chain_shape(synth):
    n = sum(len(e["calls"]) + len(e["puts"]) for e in synth["expiries"])
    assert n == 1500 and len(synth["expiries"]) == 25


@pytest.mark.parametrize("strategy", opt_rules.STRATEGIES)
def test_funnel_invariant_and_catalog_order(synth, strategy):
    r = open_rules(strategy)
    res = run(strategy, synth, SYN_UND, r)
    check_invariant(res)
    sh, own = r["shared"], r["rules"]
    assert [(f["rule"], f["unit"], f["label"]) for f in res["funnel"]] == opt_screen.catalog(strategy, sh, own)
    for c in res["rows"]:
        assert c["strategy"] == strategy and c["id"].startswith(f"SYN|{strategy}|")
        assert c["max_loss"] is None or c["max_loss"] > 0
        assert c["pop"] is None or 0 <= c["pop"] <= 1
        assert c["data"]["age_min"] == 10 and c["data"]["sources"] == ["hermes·live"]


def test_per_ticker_limit_and_order(synth):
    big = run("bull_put", synth, SYN_UND, open_rules("bull_put", per_ticker=50))
    n_all = big["n_passed"]
    assert n_all >= 4 and removed(big)["per_ticker"] == 0
    scores = [c["score"] for c in big["rows"]]
    assert scores == sorted(scores, reverse=True)
    one = run("bull_put", synth, SYN_UND, open_rules("bull_put", per_ticker=1))
    assert one["n_passed"] == 1 and removed(one)["per_ticker"] == n_all - 1
    assert one["rows"][0]["id"] == big["rows"][0]["id"]
    check_invariant(one)
    # two tickers: each keeps its own best N
    two = opt_screen.screen("bull_put", {"AAA": synth, "BBB": synth}, {"AAA": SYN_UND, "BBB": SYN_UND},
                            open_rules("bull_put", per_ticker=2), today=TODAY, now=NOW)
    assert two["n_passed"] == 4 and {t["passed"] for t in two["tickers"].values()} == {2}
    assert sorted(c["symbol"] for c in two["rows"]) == ["AAA", "AAA", "BBB", "BBB"]


def test_screen_does_not_mutate_inputs(synth):
    before = copy.deepcopy(synth), copy.deepcopy(SYN_UND)
    for s in opt_rules.STRATEGIES:
        run(s, synth, SYN_UND, open_rules(s))
    assert (synth, SYN_UND) == before


@pytest.mark.parametrize("strategy", opt_rules.STRATEGIES)
def test_stable_ids_and_detail_round_trip(synth, strategy):
    # the defaults (since the debit-vertical width and LEAPS time-value fixes) list every
    # family here; the IV-rank band is opened and the bid/ask % loosened for more rows
    r = rules(strategy, {"max_leg_spread_pct": 40}, iv_rank_min=0, iv_rank_max=100, earnings_rule="allow")
    a = run(strategy, synth, SYN_UND, r)
    b = run(strategy, synth, SYN_UND, r)
    assert a["n_passed"] == 3
    assert [c["id"] for c in a["rows"]] == [c["id"] for c in b["rows"]]
    assert len({c["id"] for c in a["rows"]}) == len(a["rows"])
    for c in a["rows"]:
        d = opt_screen.detail(strategy, c["id"], synth, SYN_UND, r, today=TODAY, now=NOW)
        assert d is not None and d["fails"] is None
        assert d["candidate"] == c
        po = d["payoff"]
        assert po["error"] is None and po["strategy"] == strategy and po["symbol"] == "SYN"
        assert po["family"] == payoff._family_of(strategy)
        if c["max_loss"] is not None and opt_rules.FAMILY[strategy] in ("credit_vertical", "condor",
                                                                        "debit_vertical", "single", "leaps"):
            assert abs(po["max_loss"] - c["max_loss"]) < 0.02


def test_detail_hand_chain_failures_and_gone():
    ch, u = bull_put_chain(), und()
    cid = "SYN|bull_put|2026-11-20|P|94|2026-11-20|P|92"
    d = opt_screen.detail("bull_put", cid, ch, u, rules("bull_put"), today=TODAY, now=NOW)
    assert d["fails"] is None and d["candidate"]["net"] == 0.5
    assert d["payoff"]["max_loss"] == 150.0 and d["payoff"]["max_profit"] == 50.0
    assert d["payoff"]["breakevens"] == [93.5]
    # rules tightened since the list was drawn: the trade is still shown, with the rule it now fails
    d2 = opt_screen.detail("bull_put", cid, ch, u, rules("bull_put", credit_pct_min=40), today=TODAY, now=NOW)
    assert d2["fails"]["rule"] == "credit" and d2["fails"]["label"].startswith("Credit under 40%")
    stale = bull_put_chain(long_92={"as_of": NOW - dt.timedelta(hours=30)})
    assert opt_screen.detail("bull_put", cid, stale, u, rules("bull_put"), today=TODAY, now=NOW)["fails"]["rule"] == "age"
    assert opt_screen.detail("bull_put", cid, ch, und(iv_rank=10.0), rules("bull_put"),
                             today=TODAY, now=NOW)["fails"]["rule"] == "iv_rank"
    # gone / wrong / malformed
    gone = chain({"2026-11-20": {"puts": [row(94, 1.20, 1.30, -0.25)]}})
    assert opt_screen.detail("bull_put", cid, gone, u, rules("bull_put"), today=TODAY, now=NOW) is None
    assert opt_screen.detail("bear_call", cid, ch, u, rules("bear_call"), today=TODAY, now=NOW) is None
    assert opt_screen.detail("bull_put", "SYN|bull_put|bad", ch, u, None, today=TODAY, now=NOW) is None
    assert opt_screen.detail("bull_put", "SYN|bull_put|2026-11-20|C|94|2026-11-20|C|92", ch, u, None,
                             today=TODAY, now=NOW) is None
    assert opt_screen.parse_id(cid) == ("SYN", "bull_put", [("2026-11-20", "P", 94.0), ("2026-11-20", "P", 92.0)])


@pytest.mark.parametrize("strategy, make, u, shared", [
    ("bear_call", lambda: chain({"2026-11-20": {"calls": [row(106, 1.20, 1.30, 0.25), row(108, 0.70, 0.80, 0.17)]}}),
     und(), {}),
    ("bull_call", bull_call_chain, und(atr14=5.0), {}),
    ("buy_call", lambda: chain({"2026-11-20": {"calls": [row(95, 7.40, 7.60, 0.65, theta=-0.05)]}}),
     und(iv_rank=40.0), {}),
    ("leaps_call", leaps_chain, und(iv_rank=40.0), {}),          # LEAPS allow earnings by default
    ("iron_condor", condor_chain, und(), {}),
])
def test_detail_round_trip_hand_chains(strategy, make, u, shared):
    ch, r = make(), rules(strategy, shared)
    c = run(strategy, ch, u, r)["rows"][0]
    d = opt_screen.detail(strategy, c["id"], ch, u, r, today=TODAY, now=NOW)
    assert d["candidate"] == c and d["fails"] is None and d["payoff"]["error"] is None
    assert abs(d["payoff"]["max_loss"] - c["max_loss"]) < 0.02


def test_detail_two_expiry_families_match_the_list():
    u = und(iv_rank=40.0)
    for strategy, ch in (("diagonal_call", diagonal_chain()), ("calendar", calendar_chain())):
        c = run(strategy, ch, u)["rows"][0]
        d = opt_screen.detail(strategy, c["id"], ch, u, rules(strategy), today=TODAY, now=NOW)
        assert d["candidate"] == c and d["fails"] is None
        assert d["payoff"]["error"] is None and d["payoff"]["family"] == "time"
        assert len(d["payoff"]["legs"]) == 2


# ------------------------------------------------------------------ the DEFAULT rules list trades
IVS = (0.25, 0.35, 0.50)


def atr_for(iv: float, spot: float = 100.0) -> float:
    """An ATR that fits the IV: the daily move spot x IV / sqrt(252), ATR ~1.3x it."""
    return round(spot * iv / math.sqrt(252) * 1.3, 2)


@pytest.fixture(scope="module")
def synth_iv():
    """The synthetic chain (25 expiries: weeklies to January, monthlies to June 2028 -
    LEAPS 9-18 months, diagonal 180-365 + 30-45, calendar 20-30 / 50-70 all covered)
    at three IV levels."""
    return {iv: view_from_bs(chain_bs(100.0, iv, EXPIRIES, STRIKES, today=TODAY, skew=0.2, spread=0.10,
                                      symbol="SYN")) for iv in IVS}


def default_und(strategy: str, iv: float, earnings: str = "2028-12-01", spot: float = 100.0) -> dict:
    """The stock: IV rank in the middle of the strategy's default band, ATR fitting the IV,
    the next report after every listed expiry unless ``earnings`` says otherwise."""
    own = opt_rules.SCHEMA[strategy]
    ivr = (own["iv_rank_min"].default + own["iv_rank_max"].default) / 2.0
    return und(spot=spot, atr14=atr_for(iv, spot), iv30=iv * 100.0, hv20=iv * 90.0, iv_rank=ivr,
               earnings_date=earnings)


def house(strategy: str) -> dict:
    return opt_rules.for_strategy(opt_rules.house(), strategy)


@pytest.mark.parametrize("iv", IVS)
@pytest.mark.parametrize("strategy", ("bull_call", "bear_put"))
def test_debit_verticals_list_on_default_rules(synth_iv, strategy, iv):
    """Bought delta 0.60-0.70 and sold 0.25-0.35 at 30-60 days sit ~3-5.5 ATR apart on any
    stock: the 1.0-6.0 default band lists them, the old 0.5-2.0 band never could."""
    r = house(strategy)
    assert (r["rules"]["width_atr_lo"], r["rules"]["width_atr_hi"]) == (1.0, 6.0)
    u = default_und(strategy, iv)
    res = run(strategy, synth_iv[iv], u, r)
    assert res["n_passed"] >= 1, [f for f in res["funnel"] if f["removed"]]
    for c in res["rows"]:
        m = c["metrics"]
        assert 1.0 <= m["width_atr"] <= 6.0 and m["debit_pct"] <= 60
        assert 0.60 <= m["long_delta"] <= 0.70 and 0.25 <= m["short_delta"] <= 0.35
        assert [l["side"] for l in c["legs"]] == ["buy", "sell"] and 30 <= c["dte"] <= 60
    check_invariant(res)
    old = run(strategy, synth_iv[iv], u, rules(strategy, width_atr_lo=0.5, width_atr_hi=2.0))
    assert old["n_passed"] == 0 and removed(old)["width"] > 0


@pytest.mark.parametrize("iv", IVS)
@pytest.mark.parametrize("strategy", opt_rules.STRATEGIES)
def test_every_strategy_lists_a_trade_on_default_rules(synth_iv, strategy, iv):
    res = run(strategy, synth_iv[iv], default_und(strategy, iv), house(strategy))
    assert res["n_passed"] >= 1, (res["tickers"], [f for f in res["funnel"] if f["removed"]])
    check_invariant(res)


@pytest.mark.parametrize("strategy", opt_rules.STRATEGIES)
def test_default_rules_with_a_report_47_days_out(synth_iv, strategy):
    """The next report on 2026-11-25: the one-expiry trades use the 30-46 day expiries,
    LEAPS hold through it, and the diagonal / calendar sell an expiry before it while the
    bought leg runs past it."""
    report = "2026-11-25"
    res = run(strategy, synth_iv[0.35], default_und(strategy, 0.35, earnings=report), house(strategy))
    assert res["n_passed"] >= 1, (res["tickers"], [f for f in res["funnel"] if f["removed"]])
    check_invariant(res)
    rule = opt_rules.SCHEMA[strategy]["earnings_rule"].default
    for c in res["rows"]:
        sold = [l["expiry"] for l in c["legs"] if l["side"] == "sell"]
        bought = [l["expiry"] for l in c["legs"] if l["side"] == "buy"]
        if rule == "none_inside":
            assert max(sold + bought) < report
        elif rule == "short_leg":
            assert max(sold) < report < min(bought)
        else:
            assert min(bought) > report


def proportional(view: dict, pct: float) -> dict:
    """A copy with every bid / ask at mid -/+ half of ``pct``% of the option's price (at
    least a cent each side) - a real market's shape, unlike chain_bs's flat $ spread."""
    out = copy.deepcopy(view)
    for e in out["expiries"]:
        for side in ("calls", "puts"):
            for r in e[side]:
                mid = r["mid"]
                half = max(0.01, round(mid * pct / 200.0, 2))
                r["bid"], r["ask"] = round(max(0.0, mid - half), 2), round(mid + half, 2)
                r["mid"] = round((r["bid"] + r["ask"]) / 2.0, 4)
    return out


# (spot, bid/ask as % of the option's price) -> the strategies the old flat $0.50 cap on
# every leg emptied there (the reviewer's measurements, reproduced)
BIG_STOCKS = {(300.0, 2.0): ("leaps_call", "diagonal_call"),
              (450.0, 3.0): ("buy_call", "buy_put", "bull_call", "bear_put", "leaps_call", "diagonal_call",
                             "calendar")}


@pytest.fixture(scope="module")
def synth_big():
    """A ~$300 and a ~$450 stock (30 strikes from 0.71 x spot, 2% / 3% of spot apart)
    whose bid / ask is a fixed % of each option's price."""
    out = {}
    for spot, pct in BIG_STOCKS:
        step = spot / 50.0
        ch = chain_bs(spot, 0.35, EXPIRIES, [round(0.71 * spot + step * i, 2) for i in range(30)], today=TODAY,
                      skew=0.2, spread=0.0, symbol="SYN")
        out[(spot, pct)] = proportional(view_from_bs(ch), pct)
    return out


@pytest.mark.parametrize("stock", list(BIG_STOCKS), ids=lambda s: f"{s[0]:.0f}usd-{s[1]:.0f}pct")
@pytest.mark.parametrize("strategy", opt_rules.STRATEGIES)
def test_defaults_list_on_larger_stocks_with_proportional_spreads(synth_big, strategy, stock):
    """The house defaults list every strategy on a larger stock whose markets are 2-3% wide
    - LEAPS, the diagonal and the calendar too: their deep / long-dated legs cost $10-$150,
    where the old flat $0.50 cap on every leg (under 1% of the price) listed nothing."""
    u = default_und(strategy, 0.35, spot=stock[0])
    res = run(strategy, synth_big[stock], u, house(strategy))
    assert res["n_passed"] >= 1, (res["tickers"], [f for f in res["funnel"] if f["removed"]])
    check_invariant(res)
    cap = opt_rules.SCHEMA[strategy]["max_leg_spread"].default
    for c in res["rows"]:
        assert cap == 0 or c["liquidity"]["spread_max"] <= cap + 1e-9
        assert c["liquidity"]["spread_pct_max"] <= 25
    if strategy in BIG_STOCKS[stock]:
        assert cap == 0 and max(c["liquidity"]["spread_max"] for c in res["rows"]) > 0.50
        old = run(strategy, synth_big[stock], u, rules(strategy, max_leg_spread=0.50))      # the old flat default
        assert old["n_passed"] == 0 and removed(old)["spread"] > 0
        assert old["tickers"]["SYN"]["reason"] == ("no trade passes: the last rule in the way is bid/ask wider "
                                                   "than $0.50 on an option")


def test_thirty_tickers_screen_fast(synth):
    chains = {f"T{i:02d}": synth for i in range(30)}
    unds = {f"T{i:02d}": SYN_UND for i in range(30)}
    worst = 0.0
    for s in opt_rules.STRATEGIES:
        t0 = time.perf_counter()
        res = opt_screen.screen(s, chains, unds, open_rules(s), today=TODAY, now=NOW)
        worst = max(worst, time.perf_counter() - t0)
        assert len(res["tickers"]) == 30
    assert worst < 4.0, f"slowest strategy took {worst:.2f} s over 30 tickers x 1,500 contracts"


# ------------------------------------------------------------------ no bid/ask quotes (Massive Starter, §13.5)
NO_QUOTES_LINE = "Bid/ask rules not applied - your data plan has no quotes"


def unquote(view: dict, **kw) -> dict:
    """A copy with no bid / ask on any row - Massive's Options Starter plan. ``mid``
    stays: the model price (Black-Scholes from the contract's IV) the collector stores."""
    out = copy.deepcopy(view)
    for e in out["expiries"]:
        for side in ("calls", "puts"):
            for r in e[side]:
                r.update(bid=None, ask=None, bid_size=None, ask_size=None, **kw)
    return out


def test_model_priced_legs_list_trades_and_the_funnel_says_bid_ask_was_skipped():
    ch = unquote(bull_put_chain(), source="massive", mdt="delayed")
    res = run("bull_put", ch, und())
    assert res["n_passed"] == 1
    c = res["rows"][0]
    assert c["id"] == "SYN|bull_put|2026-11-20|P|94|2026-11-20|P|92"           # the same trade as with quotes
    assert c["net"] == 0.5 and c["net_natural"] is None                         # no bid / ask: no natural price
    assert c["max_profit"] == 50.0 and c["max_loss"] == 150.0 and c["breakevens"] == [93.5]
    assert c["data"]["priced"] == "model" and [l["priced"] for l in c["legs"]] == ["model", "model"]
    assert (c["legs"][0]["bid"], c["legs"][0]["ask"], c["legs"][0]["mid"]) == (None, None, 1.25)
    assert c["liquidity"] == {"oi_min": 1000, "spread_max": None, "spread_pct_max": None, "volume_min": 100}
    assert c["data"]["sources"] == ["massive·delayed"] and c["data"]["age_min"] == 10
    rm = removed(res)
    assert rm["spread"] == 0 and rm["spread_pct"] == 0 and rm["credit"] == 1
    # ONE information line, right after the bid/ask rules: the 2 trades that reached them
    keys = [f["rule"] for f in res["funnel"]]
    assert keys.count(opt_screen.NO_QUOTES) == 1
    assert keys.index(opt_screen.NO_QUOTES) == keys.index("spread_pct") + 1
    line = res["funnel"][keys.index(opt_screen.NO_QUOTES)]
    assert line == {"rule": "no_quotes", "label": NO_QUOTES_LINE, "unit": "trades", "removed": 2, "info": True}
    assert opt_screen.NO_QUOTES_LABEL == NO_QUOTES_LINE
    check_invariant(res)
    # a wide market blocks the trade only where there IS a market to judge
    wide = bull_put_chain(short_94={"bid": 0.95, "ask": 1.55, "mid": 1.25})
    assert run("bull_put", wide, und())["n_passed"] == 0
    assert removed(run("bull_put", wide, und()))["spread"] == 1
    assert run("bull_put", unquote(wide), und())["n_passed"] == 1
    # the detail re-derives the same model-priced trade
    d = opt_screen.detail("bull_put", c["id"], ch, und(), rules("bull_put"), today=TODAY, now=NOW)
    assert d["candidate"] == c and d["fails"] is None and d["payoff"]["max_loss"] == 150.0


def test_with_quotes_the_bid_ask_rules_still_bite():
    # every leg quoted: no information line, the $ and % rules remove as before
    res = run("bull_put", bull_put_chain(short_94={"bid": 0.95, "ask": 1.55, "mid": 1.25}), und())
    assert removed(res)["spread"] == 1 and opt_screen.NO_QUOTES not in removed(res)
    pct = run("bull_put", bull_put_chain(), und(), rules("bull_put", {"max_leg_spread_pct": 10}))
    assert removed(pct)["spread_pct"] == 2 and pct["n_passed"] == 0
    c = run("bull_put", bull_put_chain(), und())["rows"][0]
    assert c["data"]["priced"] == "quotes" and c["net_natural"] == 0.4
    assert [l["priced"] for l in c["legs"]] == ["quotes", "quotes"]
    # a quoted leg in a mixed trade is still checked: its wide market removes the trade, and a
    # trade the bid/ask rules removed is not counted as "not applied"
    mixed_wide = bull_put_chain(long_92={"bid": None, "ask": None},
                                short_94={"bid": 0.95, "ask": 1.55, "mid": 1.25})
    r2 = run("bull_put", mixed_wide, und())
    assert removed(r2)["spread"] == 1 and opt_screen.NO_QUOTES not in removed(r2)
    check_invariant(r2)


def test_mixed_legs_are_model_priced_and_have_no_natural_price():
    ch = bull_put_chain(long_92={"bid": None, "ask": None})            # mid 0.75 stays (the model price)
    res = run("bull_put", ch, und())
    c = res["rows"][0]
    assert c["net"] == 0.5 and c["net_natural"] is None and c["data"]["priced"] == "model"
    assert [l["priced"] for l in c["legs"]] == ["quotes", "model"]
    assert c["liquidity"]["spread_max"] is None and c["liquidity"]["oi_min"] == 1000
    assert removed(res)[opt_screen.NO_QUOTES] == 1                     # the Nov 27 pair is fully quoted
    check_invariant(res)
    # one side only (an ask, no bid) is not a two-sided quote: the stored price is used
    one_side = bull_put_chain(long_92={"bid": None})
    c1 = run("bull_put", one_side, und())["rows"][0]
    assert c1["net_natural"] is None and c1["legs"][1]["priced"] == "model"


def test_a_leg_with_no_price_at_all_is_named():
    ch = unquote(chain({"2026-11-20": {"puts": [row(92, 0.70, 0.80, -0.17), row(94, 1.20, 1.30, -0.25)]}}))
    ch["expiries"][0]["puts"][0]["mid"] = None                         # no IV, so no model price either
    res = run("bull_put", ch, und())
    assert res["n_passed"] == 0 and removed(res)["quote"] == 1
    assert opt_screen.NO_QUOTES not in removed(res)                    # removed before the bid/ask rules
    assert res["tickers"]["SYN"]["reason"] == ("no trade passes: the last rule in the way is no price on an "
                                               "option (no bid/ask and no IV)")
    check_invariant(res)


@pytest.mark.parametrize("strategy", opt_rules.STRATEGIES)
def test_every_strategy_lists_on_a_chain_without_quotes(synth_iv, strategy):
    """The house defaults list every strategy on a model-priced chain (the Starter plan):
    the bid/ask rules check nothing, the information line counts what they skipped."""
    ch = unquote(synth_iv[0.35], source="massive", mdt="delayed")
    u = default_und(strategy, 0.35)
    r = house(strategy)
    res = run(strategy, ch, u, r)
    assert res["n_passed"] >= 1, (res["tickers"], [f for f in res["funnel"] if f["removed"]])
    check_invariant(res)
    rm = removed(res)
    assert rm.get("spread", 0) == 0 and rm["spread_pct"] == 0
    assert rm[opt_screen.NO_QUOTES] >= res["n_passed"]
    assert [f for f in res["funnel"] if f.get("info")] == [
        {"rule": "no_quotes", "label": NO_QUOTES_LINE, "unit": "trades", "removed": rm["no_quotes"], "info": True}]
    for c in res["rows"]:
        assert c["data"]["priced"] == "model" and c["net_natural"] is None
        assert c["data"]["sources"] == ["massive·delayed"]
        d = opt_screen.detail(strategy, c["id"], ch, u, r, today=TODAY, now=NOW)
        assert d["candidate"] == c and d["fails"] is None and d["payoff"]["error"] is None
