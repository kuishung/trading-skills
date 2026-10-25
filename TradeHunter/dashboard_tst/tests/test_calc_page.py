"""The option calculator API (OPTION_CALCULATOR_DESIGN.md steps 2-3): ``routes/calc.py`` - since v4.218
the page is gone (``/calc`` forwards to the Volatility Surface, no nav link); the chain for the leg picker, the
create -> compute -> read round trip for a manual and a pair setup, the defaults, bad
input, member scoping, and the status line with and without the lane's state file."""
from __future__ import annotations

import datetime as _dt
import json
import re

import numpy as np
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models, screener_db
from app.db import get_db
from app.main import app
from app.routes import calc as calc_routes
from app.screener_models import ScrContract, ScrPass, ScrUnderlying
from app.security import current_user
from app.services import calc_worker, clock
from app.services.option_calc import bs_price

SPOT = 100.0


def _third_friday(y, m):
    d = _dt.date(y, m, 15)
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


def _monthlies(today: _dt.date, n: int = 4) -> list[_dt.date]:
    out, y, m = [], today.year, today.month
    while len(out) < n:
        d = _third_friday(y, m)
        if d > today:
            out.append(d)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


TODAY = clock.et_date()               # the page computes against "now" (compute now has no clock override)
MONTHLIES = _monthlies(TODAY)


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
def no_screener_db(tmp_path):
    screener_db.configure("sqlite:///" + (tmp_path / "absent" / "screener.db").as_posix())
    yield
    screener_db.configure(None)


@pytest.fixture(autouse=True)
def state_path(tmp_path, monkeypatch):
    """The lane's state file - a temp path, never this PC's real one (absent unless written)."""
    p = tmp_path / "state" / "option_calc_worker.json"
    monkeypatch.setattr(calc_worker, "STATE_PATH", p)
    return p


def _fill(sdb, *, symbol="ACME", spot=SPOT, with_pass=True):
    """A synthetic chain on four monthly expiries, strikes 60..140, a mild smile."""
    as_of = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(minutes=15)
    sdb.add(ScrUnderlying(symbol=symbol, name="Acme", spot=spot, spot_src="massive", iv_rank=40.0))
    for e in MONTHLIES:
        d = (e - TODAY).days
        for k in np.arange(60.0, 145.0, 5.0):
            iv = 0.30 + 0.002 * abs(k - spot) / 5.0
            for right in ("C", "P"):
                price = float(bs_price(spot, k, d / 365.0, iv, right, 0.04))
                sdb.add(ScrContract(symbol=symbol, expiry=e.isoformat(), right=right, strike=float(k), iv=iv,
                                    oi=800, volume=100, delta=0.5, price=round(price, 2),
                                    session=TODAY.isoformat(), as_of=as_of))
    if with_pass:
        sdb.add(ScrPass(kind="cycle", session=TODAY.isoformat(), started=as_of, finished=as_of, n_symbols=1,
                        n_ok=1, n_failed=0, n_contracts=170))
    sdb.commit()


def _client(engine, uid):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        return s.get(models.User, uid)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(engine, user):
    try:
        yield _client(engine, user.id)
    finally:
        app.dependency_overrides.clear()


def _boot(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="calcBoot">(.*?)</script>', html, re.S)
    assert m, "no boot block"
    return json.loads(m.group(1))


def _legs():
    exp = MONTHLIES[1].isoformat()
    return [{"right": "C", "strike": 100, "expiry": exp, "qty": 1},
            {"right": "C", "strike": 120, "expiry": exp, "qty": -1}]


# ────────────────────────────────── the page ──────────────────────────────────

def test_the_page_is_gone_and_its_address_forwards_to_the_surface(client, no_screener_db):
    """v4.218 (user: "the current interface of the calculator is too difficult to understand. remove the
    interface."): no page, no nav link, no template; the address lands on the Volatility Surface."""
    r = client.get("/calc?view=sheet&symbol=nvda", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/options"
    html = client.get("/matp").text
    assert 'href="/calc"' not in html and ">Calculator<" not in html
    import pathlib
    assert not (pathlib.Path(calc_routes.__file__).resolve().parent.parent / "templates" / "calc.html").exists()
    from app import menus
    assert "calc" in menus.ALL_KEYS and "calc" not in [m[0] for m in menus.MENUS]     # the API's gate still passes


def test_a_member_without_the_grant_is_sent_away(client, db, user):
    user.menu_access = ["matp"]
    db.commit()
    r = client.get("/calc/api/requests", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/matp"


# ────────────────────────────────── the chain ──────────────────────────────────

def test_chain_for_the_leg_picker(client, sdb):
    _fill(sdb)
    j = client.get("/calc/api/chain?symbol=acme").json()
    assert j["ok"] is True and j["symbol"] == "ACME" and j["spot"] == SPOT and j["iv_rank"] == 40.0
    assert j["expiries"] == [m.isoformat() for m in MONTHLIES]
    e = MONTHLIES[0].isoformat()
    assert j["strikes"][e]["C"][0] == 60.0 and j["strikes"][e]["P"][-1] == 140.0
    assert j["dte"][e] == (MONTHLIES[0] - TODAY).days and j["as_of"].endswith("+00:00") and "ET" in j["as_of_et"]
    miss = client.get("/calc/api/chain?symbol=ZZZZ").json()
    assert miss["ok"] is False and "not in the screener" in miss["error"]
    assert client.get("/calc/api/chain?symbol=").status_code == 400


def test_chain_without_a_market_pass_says_so(client, no_screener_db):
    j = client.get("/calc/api/chain?symbol=ACME").json()
    assert j["ok"] is False and "market pass" in j["error"]


# ────────────────────────────────── create -> compute -> read ──────────────────────────────────

def test_manual_round_trip(client, sdb):
    _fill(sdb)
    r = client.post("/calc/api/requests", json={"symbol": "acme", "kind": "manual", "target": 125, "worst": 92,
                                                 "horizon_days": 30, "sets": 2, "legs": _legs(), "name": "breakout"})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True
    it = j["item"]
    assert it["symbol"] == "ACME" and it["kind"] == "manual" and it["sets"] == 2 and it["last_computed_at"] is None
    rid = it["id"]
    before = client.get("/calc/api/requests/%d" % rid).json()
    assert before["ok"] and before["results"] == [] and before["computed"] is None
    c = client.post("/calc/api/requests/%d/compute" % rid).json()
    assert c["ok"] is True and c["item"]["last_pass_id"] == 1 and c["item"]["last_error"] is None
    assert len(c["results"]) == 1 and c["summary"] is None
    res = c["results"][0]
    assert res["family"] == "manual" and res["horizon_days"] == 30 and res["pass_id"] == 1 and res["spot"] == SPOT
    f = res["figures"]
    assert f["entry_value"] > 0 and f["gain_at_target"] > 0 and f["family_label"] == "Your legs"
    assert 0 <= f["prob_profit"] <= 1 and len(f["legs"]) == 2
    assert res["curves"]["grid"] and len(res["curves"]["legs"]) == 2 and len(res["curves"]["net_target"]) == len(res["curves"]["grid"])
    assert res["legs"][0]["dte"] == (MONTHLIES[1] - TODAY).days
    assert c["computed"]["pass_id"] == 1 and "ET" in c["computed"]["created_et"]
    got = client.get("/calc/api/requests/%d" % rid).json()
    assert got["results"][0]["figures"] == f and len(got["history"]) == 1
    # listed, paused, changed, deleted
    lst = client.get("/calc/api/requests").json()
    assert [x["id"] for x in lst["items"]] == [rid] and lst["items"][0]["last_computed_et"]
    assert client.post("/calc/api/requests/%d/active" % rid, json={"active": False}).json()["item"]["active"] is False
    up = client.post("/calc/api/requests/%d" % rid, json={"target": 130}).json()
    assert up["ok"] and up["item"]["target"] == 130 and up["item"]["last_pass_id"] is None
    assert client.delete("/calc/api/requests/%d" % rid).json() == {"ok": True}
    assert client.get("/calc/api/requests/%d" % rid).status_code == 404


def test_pair_round_trip_and_open_in_calculator(client, sdb):
    _fill(sdb)
    rid = client.post("/calc/api/requests", json={"symbol": "ACME", "target": 120, "worst": 92}).json()["item"]["id"]
    c = client.post("/calc/api/requests/%d/compute" % rid).json()
    assert c["ok"] is True and c["item"]["kind"] == "pair"
    sm = c["summary"]
    assert sm["direction"] == "bull" and sm["horizons"] and sm["n_kept"] == len(c["results"])
    assert all(r["family"] != "_summary" for r in c["results"])
    fams = {r["family"] for r in c["results"]}
    assert "long_call" in fams and "bull_call_spread" in fams
    best = [r for r in c["results"] if r["family"] == "bull_call_spread" and r["rank"] == 1][0]
    assert best["curves"] and best["figures"]["family_label"] == "Bull call spread"
    assert c["history"] and c["history"][0]["family"] in fams
    # the page's "Open in calculator": a manual setup from the row's legs
    legs = [{k: l[k] for k in ("right", "strike", "expiry", "qty")} for l in best["legs"]]
    m = client.post("/calc/api/requests", json={"symbol": "ACME", "kind": "manual", "target": 120, "worst": 92,
                                                 "horizon_days": best["horizon_days"], "legs": legs}).json()
    mc = client.post("/calc/api/requests/%d/compute" % m["item"]["id"]).json()
    assert mc["ok"] and mc["results"][0]["figures"]["entry_value"] == pytest.approx(best["figures"]["entry_value"], abs=1e-6)


def test_compute_reports_a_leg_the_chain_cannot_serve(client, sdb):
    _fill(sdb)
    bad = [{"right": "C", "strike": 101, "expiry": MONTHLIES[1].isoformat(), "qty": 1}]
    rid = client.post("/calc/api/requests", json={"symbol": "ACME", "kind": "manual", "target": 120, "worst": 92,
                                                   "legs": bad}).json()["item"]["id"]
    r = client.post("/calc/api/requests/%d/compute" % rid)
    j = r.json()
    assert r.status_code == 200 and j["ok"] is False and "not in the last market pass" in j["error"]
    assert "not in the last market pass" in j["item"]["last_error"] and j["results"] == []


# ────────────────────────────────── defaults, bad input, scoping ──────────────────────────────────

def test_prefs_round_trip(client):
    j = client.get("/calc/api/prefs").json()
    assert j["ok"] and j["prefs"]["horizon_days"] == 45 and j["limits"]["horizon_days"] == [1, 730]
    s = client.post("/calc/api/prefs", json={"horizon_days": 30, "min_rr": "1.5", "defined_risk": False,
                                             "credit_pref": "debit"}).json()
    assert s["ok"] and s["prefs"]["horizon_days"] == 30 and s["prefs"]["min_rr"] == 1.5
    assert s["prefs"]["defined_risk"] is False and s["prefs"]["credit_pref"] == "debit"
    assert client.get("/calc/api/prefs").json()["prefs"]["horizon_days"] == 30
    bad = client.post("/calc/api/prefs", json={"horizon_days": 5000})
    assert bad.status_code == 400 and "between" in bad.json()["error"]
    assert client.get("/calc/api/prefs").json()["prefs"]["horizon_days"] == 30


def test_bad_input_is_a_400_with_a_plain_message(client):
    for body, words in (({"symbol": "ACME", "target": 100, "worst": 100}, "must differ"),
                        ({"symbol": "", "target": 100, "worst": 90}, "ticker"),
                        ({"symbol": "ACME", "kind": "manual", "target": 110, "worst": 90, "legs": []}, "leg"),
                        ({"symbol": "ACME", "target": 110}, "worst-case")):
        r = client.post("/calc/api/requests", json=body)
        assert r.status_code == 400 and r.json()["ok"] is False and words in r.json()["error"], (body, r.json())
    assert client.post("/calc/api/requests", content=b"not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
    rid = client.post("/calc/api/requests", json={"symbol": "ACME", "target": 110, "worst": 90}).json()["item"]["id"]
    r = client.post("/calc/api/requests/%d" % rid, json={"horizon_days": 0})
    assert r.status_code == 400 and "Days" in r.json()["error"]


def test_another_member_cannot_see_the_setup(client, engine, db):
    rid = client.post("/calc/api/requests", json={"symbol": "ACME", "target": 110, "worst": 90}).json()["item"]["id"]
    other = models.User(email="other@local.test", role=models.ROLE_MEMBER, status=models.APPROVED)
    db.add(other)
    db.commit()
    c2 = _client(engine, other.id)
    assert c2.get("/calc/api/requests").json()["items"] == []
    assert c2.get("/calc/api/requests/%d" % rid).status_code == 404
    assert c2.post("/calc/api/requests/%d" % rid, json={"target": 120}).status_code == 404
    assert c2.post("/calc/api/requests/%d/active" % rid, json={"active": False}).status_code == 404
    assert c2.post("/calc/api/requests/%d/compute" % rid).status_code == 404
    assert c2.delete("/calc/api/requests/%d" % rid).status_code == 404


# ────────────────────────────────── status ──────────────────────────────────

def test_status_without_a_state_file_or_a_pass(client, no_screener_db):
    j = client.get("/calc/api/status").json()
    assert j["ok"] and j["lane"]["state"] == "absent" and j["lane"]["stale"] is True
    assert j["last_pass"] is None


def test_status_with_a_state_file_and_a_pass(client, sdb, state_path):
    _fill(sdb)
    hb = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)).isoformat(timespec="seconds")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"state": "idle", "detail": "up to date with pass 1 · 2 active setups",
                                      "heartbeat": hb, "last_pass_id": 1, "computed_at": hb,
                                      "last_error": None, "requests_active": 2, "requests_due": 0}), encoding="utf-8")
    j = client.get("/calc/api/status").json()
    L = j["lane"]
    assert L["state"] == "idle" and L["stale"] is False and 25 <= L["heartbeat_age_s"] <= 120
    assert L["last_pass_id"] == 1 and "up to date" in L["detail"] and L["requests_active"] == 2
    assert j["last_pass"]["id"] == 1 and j["last_pass"]["session"] == TODAY.isoformat()
    assert j["last_pass"]["finished"].endswith("+00:00") and "ET" in j["last_pass"]["finished_et"]
    # an old heartbeat reads as stale
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).isoformat(timespec="seconds")
    state_path.write_text(json.dumps({"state": "idle", "heartbeat": old}), encoding="utf-8")
    assert client.get("/calc/api/status").json()["lane"]["stale"] is True
