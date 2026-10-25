"""The option calculator's store, chain view and migration (OPTION_CALCULATOR_DESIGN.md
steps 2-3): member defaults in User.prefs, saved requests, per-pass results, retention,
and the chain -> engine legs resolution against a screener DB at the Alembic head."""
from __future__ import annotations

import datetime as _dt

import pytest

from app import screener_db
from app.models import OptionCalcRequest, OptionCalcResult
from app.screener_models import ScrContract, ScrUnderlying
from app.services import calc_chain, calc_store
from app.services.option_calc import Scenario, evaluate

TODAY = _dt.date(2026, 10, 17)
EXP = (TODAY + _dt.timedelta(days=120)).isoformat()
EXP2 = (TODAY + _dt.timedelta(days=150)).isoformat()


@pytest.fixture
def sdb(tmp_path):
    screener_db.configure("sqlite:///" + (tmp_path / "screener.db").as_posix())
    screener_db.init_screener_db()
    s = screener_db.SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        screener_db.configure(None)


@pytest.fixture
def chain_db(sdb):
    """NVDA at 100 with the sheet's strikes on two expiries."""
    as_of = _dt.datetime(2026, 10, 17, 15, 45)
    sdb.add(ScrUnderlying(symbol="NVDA", name="NVIDIA", spot=100.0, spot_src="massive",
                          iv_rank=35.0, earnings_date="2026-11-19"))
    for exp in (EXP, EXP2):
        for k in (90.0, 100.0, 110.0, 150.0, 200.0):
            for right in ("C", "P"):
                sdb.add(ScrContract(symbol="NVDA", expiry=exp, right=right, strike=k, iv=0.20,
                                    oi=500 if k != 200 else 10, volume=50, delta=0.5, price=1.0,
                                    session="2026-10-17", as_of=as_of))
    sdb.add(ScrContract(symbol="NVDA", expiry=EXP, right="C", strike=120.0, iv=None, oi=5,
                        session="2026-10-17", as_of=as_of))      # no IV on this pass
    sdb.commit()
    return sdb


# ────────────────────────────────── defaults ──────────────────────────────────

def test_prefs_defaults_and_write(db, user):
    p = calc_store.read_prefs(user)
    assert p["horizon_days"] == 45 and p["defined_risk"] is True and p["credit_pref"] == "any"
    p2, err = calc_store.write_prefs(db, user, horizon_days=30, min_rr="2", defined_risk="off",
                                     credit_pref="credit")
    assert err == "" and p2["horizon_days"] == 30 and p2["min_rr"] == 2.0
    assert p2["defined_risk"] is False and p2["credit_pref"] == "credit"
    _, err = calc_store.write_prefs(db, user, horizon_days=5000)
    assert "between" in err
    _, err = calc_store.write_prefs(db, user, credit_pref="maybe")
    assert "any, debit or credit" in err
    assert calc_store.read_prefs(user)["horizon_days"] == 30     # the bad write changed nothing


# ────────────────────────────────── requests ──────────────────────────────────

LEGS = [{"right": "c", "strike": 100, "expiry": EXP, "qty": 1},
        {"right": "c", "strike": 150, "expiry": EXP, "qty": -1}]


def test_create_list_update_delete(db, user):
    req = calc_store.create_request(db, user, symbol="nvda", target=175, worst=50, legs=LEGS,
                                    horizon_days=85, sets=13, name="  sheet case  ")
    assert req.id and req.symbol == "NVDA" and req.kind == "manual" and req.name == "sheet case"
    assert req.legs[0] == {"right": "C", "strike": 100.0, "expiry": EXP, "qty": 1}
    assert calc_store.list_requests(db, user)[0].id == req.id
    pair = calc_store.create_request(db, user, symbol="NVDA", target=120, worst=90, kind="pair",
                                     criteria={"min_rr": 2, "bogus": 1})
    assert pair.legs is None and pair.criteria == {"min_rr": 2}
    assert [r.kind for r in calc_store.list_requests(db, user, kind="pair")] == ["pair"]
    # stamped, then edited -> stamp cleared so the lane recomputes
    req.last_pass_id = 7
    db.commit()
    req = calc_store.update_request(db, user, req.id, target=180)
    assert req.target == 180 and req.last_pass_id is None
    assert calc_store.set_active(db, user, req.id, False).active is False
    assert calc_store.list_requests(db, user, active=True) == [pair]
    assert calc_store.delete_request(db, user, req.id) is True
    assert calc_store.get_request(db, user, req.id) is None


def test_request_validation(db, user):
    with pytest.raises(calc_store.CalcInputError):
        calc_store.create_request(db, user, symbol="", target=1, worst=2, legs=LEGS)
    with pytest.raises(calc_store.CalcInputError):
        calc_store.create_request(db, user, symbol="NVDA", target=100, worst=100, legs=LEGS)
    with pytest.raises(calc_store.CalcInputError):
        calc_store.create_request(db, user, symbol="NVDA", target=1, worst=2, legs=[])
    with pytest.raises(calc_store.CalcInputError):
        calc_store.create_request(db, user, symbol="NVDA", target=1, worst=2,
                                  legs=[{"right": "x", "strike": 1, "expiry": EXP, "qty": 1}])
    with pytest.raises(calc_store.CalcInputError):
        calc_store.create_request(db, user, symbol="NVDA", target=1, worst=2, legs=LEGS, horizon_days=0)


def test_scoped_to_the_member(db, user):
    from app.models import User
    other = User(email="other@local.test", status="approved", role="member")
    db.add(other)
    db.commit()
    req = calc_store.create_request(db, user, symbol="NVDA", target=175, worst=50, legs=LEGS)
    assert calc_store.get_request(db, other, req.id) is None
    assert calc_store.list_requests(db, other) == []
    assert calc_store.delete_request(db, other, req.id) is False


# ────────────────────────────────── results ──────────────────────────────────

def _rows(res):
    return [{"family": "manual", "rank": 1, "horizon_days": 85, "target_date": "2027-01-10",
             "legs": LEGS, "figures": calc_store.figures_from(res), "curves": calc_store.curves_from(res)}]


def test_results_write_latest_history_and_due(db, user):
    req = calc_store.create_request(db, user, symbol="NVDA", target=175, worst=50, legs=LEGS, horizon_days=85)
    assert calc_store.due_requests(db, 1) == [req]
    res = evaluate([{"right": "C", "strike": 100, "dte": 120, "iv": 0.2, "qty": 1},
                    {"right": "C", "strike": 150, "dte": 120, "iv": 0.2, "qty": -1}],
                   Scenario(100, 175, 50, 85, r=0.048))
    t1 = _dt.datetime(2026, 10, 17, 14, 0)
    n = calc_store.write_results(db, req, _rows(res), pass_id=1, session="2026-10-17", as_of=t1,
                                 spot=100.0, now=t1)
    assert n == 1 and req.last_pass_id == 1 and req.last_error is None
    assert calc_store.due_requests(db, 1) == [] and calc_store.due_requests(db, 2) == [req]
    rows = calc_store.latest_results(db, req)
    assert len(rows) == 1 and rows[0].figures["entry_value"] == pytest.approx(5.3611, abs=1e-4)
    assert rows[0].curves["grid"][0] < 50 and len(rows[0].curves["legs"]) == 2
    # a second pass: latest moves, history keeps both
    t2 = _dt.datetime(2026, 10, 17, 14, 30)
    calc_store.write_results(db, req, _rows(res), pass_id=2, session="2026-10-17", as_of=t2, spot=101.0, now=t2)
    assert calc_store.latest_results(db, req)[0].pass_id == 2
    assert [r.pass_id for r in calc_store.result_history(db, req)] == [2, 1]
    # rewriting the same pass replaces, never duplicates
    calc_store.write_results(db, req, _rows(res), pass_id=2, session="2026-10-17", as_of=t2, spot=101.0, now=t2)
    assert db.query(OptionCalcResult).filter_by(request_id=req.id).count() == 2
    # an error stamps the pass and keeps the message
    calc_store.mark_error(db, req, "NVDA 2027-02-14 call 100 is not in the last market pass", pass_id=3)
    assert req.last_pass_id == 3 and "not in the last market pass" in req.last_error
    assert calc_store.due_requests(db, 3) == []
    # deleting the request cascades to its results
    calc_store.delete_request(db, user, req.id)
    assert db.query(OptionCalcResult).count() == 0


def test_thin_results_keeps_last_pass_of_old_sessions(db, user):
    req = calc_store.create_request(db, user, symbol="NVDA", target=175, worst=50, legs=LEGS)
    res = evaluate([{"right": "C", "strike": 100, "dte": 120, "iv": 0.2, "qty": 1}], Scenario(100, 175, 50, 85))
    pid = 0
    for day in ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"):
        for h in (10, 11, 16):
            pid += 1
            t = _dt.datetime.fromisoformat(day + "T%02d:00" % h)
            calc_store.write_results(db, req, _rows(res), pass_id=pid, session=day, as_of=t, spot=100.0, now=t)
    assert db.query(OptionCalcResult).count() == 12
    removed = calc_store.thin_results(db, keep_sessions=2)
    assert removed == 4
    left = db.query(OptionCalcResult).order_by(OptionCalcResult.pass_id).all()
    assert [r.pass_id for r in left] == [3, 6, 7, 8, 9, 10, 11, 12]


# ────────────────────────────────── the chain view ──────────────────────────────────

def test_load_chain_and_resolve_legs(db, chain_db):
    ch = calc_chain.load_chain(chain_db, "nvda")
    assert ch.symbol == "NVDA" and ch.spot == 100.0 and ch.session == "2026-10-17"
    assert ch.earnings_date == "2026-11-19" and ch.iv_rank == 35.0
    assert ch.expiries() == [EXP, EXP2]
    assert ch.strikes(EXP, "C") == [90.0, 100.0, 110.0, 120.0, 150.0, 200.0]
    assert ch.find("c", 100, EXP).oi == 500 and ch.find("C", 999, EXP) is None
    legs, resolved = calc_chain.resolve_legs(ch, LEGS, TODAY)
    assert [l.dte for l in legs] == [120, 120] and legs[1].qty == -1 and legs[0].iv == 0.2
    assert resolved[0]["oi"] == 500 and resolved[0]["dte"] == 120
    # the sheet's figures come out of the resolved legs
    res = evaluate(legs, Scenario(ch.spot, 175, 50, 85, r=0.048))
    assert res.entry_value == pytest.approx(5.361099680279896, abs=1e-9)
    # the window: strikes between the prices, with an IV and enough open interest
    win = ch.in_window(expiries=[EXP], lo=95, hi=160, min_oi=100)
    assert sorted({c.strike for c in win}) == [100.0, 110.0, 150.0]


def test_chain_errors(db, chain_db):
    with pytest.raises(calc_chain.ChainError, match="not in the screener"):
        calc_chain.load_chain(chain_db, "ZZZZ")
    ch = calc_chain.load_chain(chain_db, "NVDA")
    with pytest.raises(calc_chain.ChainError, match="not in the last market pass"):
        calc_chain.resolve_legs(ch, [{"right": "C", "strike": 101, "expiry": EXP, "qty": 1}], TODAY)
    with pytest.raises(calc_chain.ChainError, match="no implied volatility"):
        calc_chain.resolve_legs(ch, [{"right": "C", "strike": 120, "expiry": EXP, "qty": 1}], TODAY)
    with pytest.raises(calc_chain.ChainError, match="at most 4"):
        calc_chain.normalise_legs([LEGS[0]] * 5)
    assert calc_chain.dte(EXP, TODAY) == 120 and calc_chain.dte("2020-01-01", TODAY) == 0


def test_migration_tables_present(db):
    from sqlalchemy import inspect
    names = set(inspect(db.get_bind()).get_table_names())
    assert {"option_calc_requests", "option_calc_results"} <= names
    assert db.query(OptionCalcRequest).count() == 0


def test_the_iv_change_is_kept_on_a_manual_setup(db, user):
    """v4.217: a manual setup keeps its IV change on the target date (vol points); a pair setup never has one
    (the pairing search keeps IV unchanged); 0 is stored as none; out of bounds is refused."""
    req = calc_store.create_request(db, user, symbol="NVDA", target=175, worst=50, legs=LEGS, iv_shift="-10")
    assert req.iv_shift == -10.0
    assert calc_store.update_request(db, user, req.id, iv_shift=5).iv_shift == 5.0
    assert calc_store.update_request(db, user, req.id, target=180).iv_shift == 5.0          # other edits keep it
    assert calc_store.update_request(db, user, req.id, iv_shift=0).iv_shift is None
    pair = calc_store.create_request(db, user, symbol="NVDA", target=120, worst=90, kind="pair", iv_shift=-10)
    assert pair.iv_shift is None
    for bad in (-60, 101, "lots"):
        with pytest.raises(calc_store.CalcInputError, match="vol points"):
            calc_store.create_request(db, user, symbol="NVDA", target=175, worst=50, legs=LEGS, iv_shift=bad)
    assert calc_store.norm_iv_shift("") == 0.0 and calc_store.norm_iv_shift(None) == 0.0


def test_the_iv_shift_column_is_migrated(db):
    from sqlalchemy import inspect
    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("option_calc_requests")}
    assert "iv_shift" in cols
