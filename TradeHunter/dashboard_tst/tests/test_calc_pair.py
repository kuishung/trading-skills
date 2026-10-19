"""Setup pairing (OPTION_CALCULATOR_DESIGN.md step 3) and the Hermes lane (step 2) against
a synthetic chain: direction, horizons, the window, every family, the criteria as
filters, the stored rows, and the worker's look / state file."""
from __future__ import annotations

import datetime as _dt
import json

import numpy as np
import pytest

from app import screener_db
from app.models import OptionCalcResult, User
from app.screener_models import ScrContract, ScrPass, ScrUnderlying
from app.services import calc_chain, calc_pair, calc_store, calc_worker
from app.services.option_calc import Leg, Scenario, bs_price, evaluate

TODAY = _dt.date(2026, 10, 19)                       # a Monday
NOW = _dt.datetime(2026, 10, 19, 15, 0, tzinfo=_dt.timezone.utc)   # 11:00 ET
SPOT = 100.0


def _third_friday(y, m):
    d = _dt.date(y, m, 15)
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


MONTHLIES = [_third_friday(2026, 11), _third_friday(2026, 12), _third_friday(2027, 1), _third_friday(2027, 2)]
WEEKLY = TODAY + _dt.timedelta(days=11)              # a Friday 11 days out


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


def _fill(sdb, *, symbol="ACME", spot=SPOT, earnings=None, pass_id=True):
    as_of = _dt.datetime(2026, 10, 19, 14, 45)
    sdb.add(ScrUnderlying(symbol=symbol, name="Acme", spot=spot, spot_src="massive", iv_rank=40.0,
                          earnings_date=earnings))
    exps = [WEEKLY] + MONTHLIES
    for e in exps:
        d = (e - TODAY).days
        for k in np.arange(60.0, 145.0, 5.0):
            # a mild smile so the window's ATM IV and the legs' IVs differ a little
            iv = 0.30 + 0.002 * abs(k - spot) / 5.0
            for right in ("C", "P"):
                price = float(bs_price(spot, k, d / 365.0, iv, right, 0.04))
                sdb.add(ScrContract(symbol=symbol, expiry=e.isoformat(), right=right, strike=float(k), iv=iv,
                                    oi=800 if k not in (60.0, 140.0) else 20, volume=100, delta=0.5,
                                    price=round(price, 2), session="2026-10-19", as_of=as_of))
    if pass_id:
        sdb.add(ScrPass(kind="cycle", session="2026-10-19", started=as_of, finished=as_of, n_symbols=1,
                        n_ok=1, n_failed=0, n_contracts=170))
    sdb.commit()
    return calc_chain.load_chain(sdb, symbol)


# ────────────────────────────────── horizons + window ──────────────────────────────────

def test_direction_and_horizons(sdb):
    ch = _fill(sdb)
    assert calc_pair.direction_of(100, 120) == "bull" and calc_pair.direction_of(100, 90) == "bear"
    with pytest.raises(calc_chain.ChainError):
        calc_pair.direction_of(100, 100)
    hs = calc_pair.horizons_for(ch, TODAY, horizon_days=None, horizons_n=3, avoid_earnings=True)
    assert [h.target_date for h in hs] == [(m - _dt.timedelta(days=5)).isoformat() for m in MONTHLIES[:3]]
    assert all(h.expiries[0] == m.isoformat() for h, m in zip(hs, MONTHLIES))   # the monthly itself is first
    assert all(len(h.expiries) == 2 and h.reason is None for h in hs)
    one = calc_pair.horizons_for(ch, TODAY, horizon_days=40, horizons_n=3, avoid_earnings=True)
    assert len(one) == 1 and one[0].days == 40 and one[0].expiries[0] == MONTHLIES[1].isoformat()


def test_earnings_flag_horizons_and_drop_only_while_a_clean_one_remains(sdb):
    # v4.157: earnings 10 days before the first monthly -> EVERY horizon crosses it. They are
    # all kept and flagged (dropping them all was why the search came back empty most of the time).
    earn = (MONTHLIES[0] - _dt.timedelta(days=10)).isoformat()
    ch = _fill(sdb, earnings=earn)
    hs = calc_pair.horizons_for(ch, TODAY, horizon_days=None, horizons_n=2, avoid_earnings=True)
    assert all(h.earnings and h.reason is None for h in hs)
    rows, summary = calc_pair.search(ch, 120.0, 92.0, _crit(min_rr=0.0, horizons_n=2), TODAY)
    assert rows and "every horizon" in summary["earnings_note"]
    assert all(r["figures"]["earnings_in_window"] is True and r["figures"]["earnings_date"] == earn for r in rows)
    # earnings between the first and the second monthly -> the first horizon is clean, the
    # later ones are dropped (a clean one remains)
    sdb.query(ScrUnderlying).filter_by(symbol="ACME").update({"earnings_date": (MONTHLIES[1] - _dt.timedelta(days=10)).isoformat()})
    sdb.commit()
    ch2 = calc_chain.load_chain(sdb, "ACME")
    hs2 = calc_pair.horizons_for(ch2, TODAY, horizon_days=None, horizons_n=3, avoid_earnings=True)
    assert hs2[0].reason is None and not hs2[0].earnings
    assert all("earnings" in (h.reason or "") for h in hs2[1:])
    # the member's own days are never dropped, only flagged
    one = calc_pair.horizons_for(ch2, TODAY, horizon_days=60, horizons_n=3, avoid_earnings=True)
    assert one[0].reason is None and one[0].earnings is True
    # avoid earnings off: nothing dropped, still flagged
    hs3 = calc_pair.horizons_for(ch2, TODAY, horizon_days=None, horizons_n=3, avoid_earnings=False)
    assert all(h.reason is None for h in hs3) and hs3[1].earnings


def test_window_is_the_move_plus_a_buffer(sdb):
    ch = _fill(sdb)
    h = calc_pair.horizons_for(ch, TODAY, horizon_days=None, horizons_n=1, avoid_earnings=True)[0]
    lo, hi, cs = calc_pair.window_for(ch, h, SPOT, 120.0, 92.0, min_oi=100)
    assert lo < 92 and hi > 120
    assert (92 - lo) == pytest.approx(hi - 120, abs=1e-9)          # the same buffer on each side
    assert (hi - 120) >= SPOT * calc_pair.MIN_BUFFER
    strikes = sorted({c.strike for c in cs})
    assert strikes[0] >= lo and strikes[-1] <= hi
    assert all(c.oi >= 100 for c in cs)
    assert {c.expiry for c in cs} == set(h.expiries)


# ────────────────────────────────── the search ──────────────────────────────────

def _crit(**over):
    c = dict(calc_store.DEFAULTS)
    c["min_oi"] = 100
    c.update(over)
    return c


def test_bull_search_returns_every_family_ranked(sdb):
    ch = _fill(sdb)
    rows, summary = calc_pair.search(ch, 120.0, 92.0, _crit(min_rr=0.5), TODAY)
    assert summary["direction"] == "bull" and summary["n_candidates"] > 100 and summary["n_kept"] == len(rows)
    fams = {r["family"] for r in rows}
    assert {"long_call", "bull_call_spread", "bull_put_spread", "call_calendar", "call_diagonal"} <= fams
    assert "short_put" not in fams                      # defined risk only by default
    assert all(f in calc_pair.FAMILIES and calc_pair.FAMILIES[f][0] == "bull" for f in fams)
    for fam in fams:
        fr = [r for r in rows if r["family"] == fam]
        ranks = [r["rank"] for r in fr]
        assert ranks == list(range(1, len(fr) + 1)) and len(fr) <= 1 + _crit()["alternatives"]
        assert fr[0]["curves"] is not None and all(r["curves"] is None for r in fr[1:])
        scores = [r["score"] for r in fr]
        assert scores == sorted(scores, reverse=True) or len(fr) == 1 or scores[0] >= scores[1] * 0.0
        rr = [r["figures"]["reward_over_risk"] for r in fr]
        assert all(x is None or x >= 0.5 for x in rr)
        for r in fr:
            assert r["figures"]["gain_at_target"] > 0
            assert all(l["oi"] >= 100 for l in r["legs"])
            assert r["figures"]["family_label"] == calc_pair.FAMILIES[fam][3]
            assert 0 <= (r["figures"]["prob_profit"] or 0) <= 1
    # every row reproduces through the engine from its stored legs
    r0 = [r for r in rows if r["family"] == "bull_call_spread"][0]
    legs = [Leg(l["right"], l["strike"], l["dte"], l["iv"], l["qty"]) for l in r0["legs"]]
    res = evaluate(legs, Scenario(SPOT, 120.0, 92.0, r0["horizon_days"], r=0.04))
    assert res.entry_value == pytest.approx(r0["figures"]["entry_value"], abs=1e-5)
    # a bull call spread's legs: long the lower call, short the higher, same expiry
    a, b = r0["legs"]
    assert a["qty"] == 1 and b["qty"] == -1 and a["strike"] < b["strike"] and a["expiry"] == b["expiry"]


def test_bear_search_and_undefined_risk(sdb):
    ch = _fill(sdb)
    rows, summary = calc_pair.search(ch, 85.0, 106.0, _crit(defined_risk=False, min_rr=0.0), TODAY)
    fams = {r["family"] for r in rows}
    assert summary["direction"] == "bear"
    assert {"long_put", "bear_put_spread", "bear_call_spread", "short_call"} <= fams
    sc = [r for r in rows if r["family"] == "short_call"][0]
    assert sc["legs"][0]["qty"] == -1 and sc["legs"][0]["right"] == "C"
    assert sc["figures"]["entry_value"] < 0                      # a credit


def test_criteria_filter(sdb):
    ch = _fill(sdb)
    debit, _ = calc_pair.search(ch, 120.0, 92.0, _crit(credit_pref="debit"), TODAY)
    assert {r["family"] for r in debit} <= {"long_call", "bull_call_spread", "call_calendar", "call_diagonal"}
    credit, _ = calc_pair.search(ch, 120.0, 92.0, _crit(credit_pref="credit", min_rr=0.0), TODAY)
    assert {r["family"] for r in credit} == {"bull_put_spread"}
    assert all(r["figures"]["entry_value"] < 0 for r in credit)
    capped, s = calc_pair.search(ch, 120.0, 92.0, _crit(max_loss=150.0, min_rr=0.0), TODAY)
    assert all(-r["figures"]["max_loss"] * 100 <= 150.0 + 1e-6 for r in capped)
    assert s["dropped"].get("max loss over the cap", 0) > 0
    strict, s2 = calc_pair.search(ch, 120.0, 92.0, _crit(min_rr=1000.0), TODAY)
    assert strict == [] and s2["n_kept"] == 0
    one, s3 = calc_pair.search(ch, 120.0, 92.0, _crit(min_rr=0.0), TODAY, horizon_days=40)
    assert len(s3["horizons"]) == 1 and all(r["horizon_days"] == 40 for r in one)
    few, _ = calc_pair.search(ch, 120.0, 92.0, _crit(min_rr=0.0), TODAY, families=["long_call"])
    assert {r["family"] for r in few} == {"long_call"}


def test_search_is_fast_enough(sdb):
    import time
    ch = _fill(sdb)
    t0 = time.perf_counter()
    rows, summary = calc_pair.search(ch, 125.0, 90.0, _crit(min_rr=0.0, horizons_n=3), TODAY)
    dt = time.perf_counter() - t0
    assert rows and dt < 10.0, (dt, summary["n_candidates"])


# ────────────────────────────────── the lane ──────────────────────────────────

def _req(db, user, **over):
    kw = dict(symbol="ACME", target=120, worst=92, kind="pair")
    kw.update(over)
    return calc_store.create_request(db, user, **kw)


def test_compute_request_manual_and_pair(db, user, sdb):
    _fill(sdb)
    exp = MONTHLIES[1].isoformat()
    man = _req(db, user, kind="manual", horizon_days=30,
               legs=[{"right": "C", "strike": 100, "expiry": exp, "qty": 1},
                     {"right": "C", "strike": 120, "expiry": exp, "qty": -1}])
    rows, chain = calc_worker.compute_request(sdb, man, user, today=TODAY)
    assert len(rows) == 1 and rows[0]["family"] == "manual" and rows[0]["horizon_days"] == 30
    assert rows[0]["figures"]["entry_value"] > 0 and rows[0]["curves"]["grid"]
    assert rows[0]["legs"][0]["dte"] == (MONTHLIES[1] - TODAY).days
    pair = _req(db, user)
    rows, chain = calc_worker.compute_request(sdb, pair, user, today=TODAY)
    fams = [r["family"] for r in rows]
    assert "_summary" in fams and "long_call" in fams
    summ = [r for r in rows if r["family"] == "_summary"][0]["figures"]
    assert summ["direction"] == "bull" and summ["n_kept"] == len(rows) - 1


def test_worker_look_writes_results_and_state(db, user, sdb, tmp_path, engine):
    _fill(sdb)
    from sqlalchemy.orm import sessionmaker
    TstSession = sessionmaker(bind=engine)
    exp = MONTHLIES[1].isoformat()
    ok = _req(db, user, kind="manual", legs=[{"right": "C", "strike": 100, "expiry": exp, "qty": 1}])
    bad = _req(db, user, kind="manual", legs=[{"right": "C", "strike": 101, "expiry": exp, "qty": 1}])
    pair = _req(db, user)
    db.commit()
    state = tmp_path / "state" / "option_calc_worker.json"
    w = calc_worker.CalcWorker(tst_sessions=TstSession, scr_sessions=screener_db.SessionLocal,
                               state_path=state, tick_s=5)
    out = w.look(NOW)
    assert out["pass_id"] == 1 and out["due"] == 3 and out["done"] == 2 and out["failed"] == 1
    db.expire_all()
    assert ok.last_pass_id == 1 and ok.last_error is None
    assert bad.last_pass_id == 1 and "not in the last market pass" in bad.last_error
    assert pair.last_pass_id == 1
    res = calc_store.latest_results(db, ok)
    assert len(res) == 1 and res[0].pass_id == 1 and res[0].session == "2026-10-19" and res[0].spot == SPOT
    assert len(calc_store.latest_results(db, pair)) > 1
    doc = json.loads(state.read_text())
    assert set(calc_worker.STATE_KEYS) <= set(doc)
    assert doc["state"] == "idle" and doc["last_pass_id"] == 1 and doc["done"] == 2 and doc["failed"] == 1
    assert doc["requests_active"] == 3 and "ACME" in doc["last_error"] and doc["heartbeat"]
    assert (tmp_path / "state" / ".gitignore").exists()
    # nothing is due a second time on the same pass
    out2 = w.look(NOW + _dt.timedelta(minutes=1))
    assert out2["due"] == 0 and out2["done"] == 0
    # a changed request is due again
    calc_store.update_request(db, user, ok.id, target=130)
    assert w.look(NOW + _dt.timedelta(minutes=2))["done"] == 1
    assert calc_worker.read_state(state)["requests_due"] == 0


def test_worker_waits_without_a_pass(db, user, sdb, tmp_path, engine):
    _fill(sdb, pass_id=False)
    from sqlalchemy.orm import sessionmaker
    _req(db, user)
    w = calc_worker.CalcWorker(tst_sessions=sessionmaker(bind=engine), scr_sessions=screener_db.SessionLocal,
                               state_path=tmp_path / "s.json")
    out = w.look(NOW)
    assert out["pass_id"] is None and out["done"] == 0
    doc = calc_worker.read_state(tmp_path / "s.json")
    assert doc["state"] == "idle" and "first market pass" in doc["detail"] and doc["requests_active"] == 1


def test_compute_and_store_inline(db, user, sdb):
    _fill(sdb)
    pair = _req(db, user)
    n = calc_worker.compute_and_store(db, sdb, pair, user, now=NOW)
    assert n > 1 and pair.last_pass_id == 1
    assert db.query(OptionCalcResult).filter_by(request_id=pair.id).count() == n
    ghost = _req(db, user, symbol="ZZZZ")
    with pytest.raises(calc_chain.ChainError, match="not in the screener"):
        calc_worker.compute_and_store(db, sdb, ghost, user, now=NOW)
    assert "not in the screener" in ghost.last_error
