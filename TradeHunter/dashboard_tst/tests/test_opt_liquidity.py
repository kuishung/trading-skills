"""The option-liquidity verdict per ticker (v4.186, `services/opt_liquidity.py`): Liquid / Fair /
Thin against fixed, member-editable lines, judged near the money in the first two monthlies."""
from __future__ import annotations

import datetime as _dt

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.screener_models import ScrBase, ScrContract, ScrUnderlying
from app.services import opt_liquidity as liq

TODAY = _dt.date(2026, 10, 21)
NOV, DEC, JAN = _dt.date(2026, 11, 20), _dt.date(2026, 12, 18), _dt.date(2027, 1, 15)
WEEKLY = _dt.date(2026, 10, 30)


@pytest.fixture
def sdb(tmp_path):
    eng = sa.create_engine("sqlite:///" + (tmp_path / "screener.db").as_posix(), future=True)
    ScrBase.metadata.create_all(eng)
    s = sessionmaker(bind=eng, future=True)()
    asof = _dt.datetime(2026, 10, 21, 14, 0)

    def contract(sym, exp, k, rt, oi, vol, prev=0, weekly=False):
        s.add(ScrContract(symbol=sym, expiry=exp.isoformat(), right=rt, strike=k, weekly=weekly, oi=oi, volume=vol,
                          vol_prev=prev, iv=0.3, session="2026-10-21", as_of=asof))

    # DEEP: price 100; the near-the-money strikes 90..110 in NOV and DEC carry 2,000 OI each and 600 volume
    s.add(ScrUnderlying(symbol="DEEP", name="Deep Inc", sec_type="stock", spot=100.0, call_oi=90_000, put_oi=80_000,
                        history_done=True, history_tries=0))
    for exp in (NOV, DEC, JAN):
        for k in (70.0, 90.0, 95.0, 100.0, 105.0, 110.0, 130.0):
            for rt in "CP":
                contract("DEEP", exp, k, rt, 2000 if 90 <= k <= 110 else 50, 600 if 90 <= k <= 110 else 1)
    for k in (95.0, 100.0, 105.0):                   # a weekly: never a leg of the verdict
        contract("DEEP", WEEKLY, k, "C", 9_000_000, 9_000_000, weekly=True)
    # MID: clears the Fair lines, not the Liquid ones (near OI 400, near volume 1,500, chain 30k)
    s.add(ScrUnderlying(symbol="MID", name="Mid Corp", sec_type="stock", spot=50.0, call_oi=20_000, put_oi=10_000,
                        history_done=True, history_tries=0))
    for exp in (NOV, DEC):
        for k in (47.5, 50.0, 52.5):
            contract("MID", exp, k, "C", 400, 250)
    # PREOPEN: nothing traded yet today - the previous session's volume stands in
    s.add(ScrUnderlying(symbol="PREOPEN", name="Pre Open", sec_type="stock", spot=20.0, call_oi=200_000, put_oi=100_000,
                        history_done=True, history_tries=0))
    for exp in (NOV, DEC):
        for k in (19.0, 20.0, 21.0):
            contract("PREOPEN", exp, k, "P", 3000, 0, prev=2000)
    # NOPRICE: the screener lists it but has no spot
    s.add(ScrUnderlying(symbol="NOPRICE", name="No Price", sec_type="stock", spot=None, call_oi=5, put_oi=5,
                        history_done=False, history_tries=0))
    s.commit()
    yield s
    s.close()


def test_the_grade_needs_all_three_figures_and_names_the_one_that_held_it_back():
    assert liq.grade(1000, 5000, 100_000) == ("liquid", None)
    assert liq.grade(999, 5000, 100_000) == ("fair", "near-the-money open interest")
    assert liq.grade(1000, 4999, 100_000) == ("fair", "near-the-money volume")
    assert liq.grade(1000, 5000, 99_999) == ("fair", "chain open interest")
    assert liq.grade(249, 5000, 100_000) == ("thin", "near-the-money open interest")
    assert liq.grade(None, None, None) == ("thin", "near-the-money open interest")
    # the member's own lines
    assert liq.grade(300, 1200, 25_000, {"liquid_oi": 300, "liquid_vol": 1000, "liquid_chain": 20_000}) == ("liquid", None)


def test_the_verdict_is_judged_near_the_money_in_the_first_two_monthlies(sdb):
    v = liq.verdict(sdb, "deep", today=TODAY)
    assert v["symbol"] == "DEEP" and v["level"] == "liquid" and v["label"] == "Liquid" and v["held"] is None
    assert v["expiries"] == [NOV.isoformat(), DEC.isoformat()]          # not JAN, never the weekly
    assert v["n_near"] == 20                                              # 5 strikes x 2 rights x 2 expiries
    assert v["near_oi"] == 2000 and v["near_vol"] == 12_000 and v["vol_src"] == "today"
    assert v["chain_oi"] == 170_000 and v["short"] == "2k OI"
    assert "DEEP option liquidity: Liquid." in v["text"] and "median open interest 2k, volume 12k today" in v["text"]
    assert "Lines: Liquid >= 1k / 5k / 100k, Fair >= 250 / 1k / 20k" in v["text"]


def test_fair_names_the_figure_that_held_it_back_and_the_member_lines_move_it(sdb):
    v = liq.verdict(sdb, "MID", today=TODAY)
    assert v["level"] == "fair" and v["near_oi"] == 400 and v["near_vol"] == 1500 and v["chain_oi"] == 30_000
    assert v["held"] == "near-the-money open interest" and "Held back from Liquid by the near-the-money open interest." in v["text"]
    own = {"liquid_oi": 300, "liquid_vol": 1000, "liquid_chain": 20_000}
    assert liq.verdict(sdb, "MID", own, today=TODAY)["level"] == "liquid"


def test_before_the_open_the_previous_sessions_volume_stands_in(sdb):
    v = liq.verdict(sdb, "PREOPEN", today=TODAY)
    assert v["vol_src"] == "prev" and v["near_vol"] == 12_000 and v["level"] == "liquid"
    assert "the previous session (nothing traded yet today)" in v["text"]


def test_no_price_unknown_ticker_and_the_batch(sdb):
    v = liq.verdict(sdb, "NOPRICE", today=TODAY)
    assert v["level"] == "thin" and v["n_near"] == 0 and "No price for the name in the last pass." in v["text"]
    assert liq.verdict(sdb, "ZZZZ", today=TODAY) is None
    out = liq.verdicts(sdb, ["deep", "ZZZZ", "MID", "DEEP"], today=TODAY)
    assert set(out) == {"DEEP", "MID"} and out["DEEP"]["level"] == "liquid"


def test_the_member_lines_are_read_and_written_in_bounds(db, user):
    assert liq.read_thresholds(user) == liq.DEFAULTS
    t, err = liq.write_thresholds(db, user, liquid_oi="800", fair_vol="500")
    assert err == "" and t["liquid_oi"] == 800 and t["fair_vol"] == 500 and t["fair_oi"] == 250
    assert liq.read_thresholds(user)["liquid_oi"] == 800 and user.prefs["liq_liquid_oi"] == 800
    t2, err = liq.write_thresholds(db, user, fair_oi="900")                 # above the Liquid line of 800
    assert err.startswith("the Fair line for near-the-money open interest cannot be above") and t2 == t
    _t3, err = liq.write_thresholds(db, user, liquid_vol="lots")
    assert err == "liquid vol must be a whole number"
    _t4, err = liq.write_thresholds(db, user, liquid_chain="-5")
    assert "must be between" in err
