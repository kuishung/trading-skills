"""The sheet view's two endpoints (v4.157): `/calc/api/surface` (the volatility surface as
a leg picker: call and put IV per strike x expiry from the last pass) and
`/calc/api/preview` (the engine's figures for the legs on screen, nothing saved)."""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models, screener_db
from app.db import get_db
from app.main import app
from app.models import OptionCalcRequest
from app.screener_models import ScrContract, ScrPass, ScrUnderlying
from app.security import current_user
from app.services import clock
from app.services.option_calc import bs_price

TODAY = _dt.date.fromisoformat(str(clock.et_today())[:10])   # the route's clock (New York) - the local date is a day ahead in Malaysia before noon


def _third_friday(y, m):
    d = _dt.date(y, m, 15)
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


def _monthlies(n=4):
    out, y, m = [], TODAY.year, TODAY.month
    while len(out) < n:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        out.append(_third_friday(y, m))
    return out


MONTHLIES = _monthlies()


@pytest.fixture
def sdb(tmp_path):
    screener_db.configure("sqlite:///" + (tmp_path / "screener.db").as_posix())
    screener_db.init_screener_db()
    s = screener_db.SessionLocal()
    as_of = _dt.datetime.utcnow().replace(microsecond=0)
    s.add(ScrUnderlying(symbol="ACME", name="Acme", spot=100.0, spot_src="massive", iv_rank=40.0,
                        earnings_date=(TODAY + _dt.timedelta(days=20)).isoformat()))
    for e in MONTHLIES:
        d = (e - TODAY).days
        for k in np.arange(60.0, 145.0, 5.0):
            iv = 0.30 + 0.002 * abs(k - 100.0) / 5.0
            for right in ("C", "P"):
                s.add(ScrContract(symbol="ACME", expiry=e.isoformat(), right=right, strike=float(k), iv=iv,
                                  oi=500, volume=10, delta=0.5,
                                  price=round(float(bs_price(100.0, float(k), d / 365.0, iv, right, 0.04)), 2),
                                  session=TODAY.isoformat(), as_of=as_of))
    s.add(ScrPass(kind="cycle", session=TODAY.isoformat(), started=as_of, finished=as_of, n_symbols=1, n_ok=1,
                  n_failed=0, n_contracts=136))
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        screener_db.configure(None)


@pytest.fixture
def client(engine, user):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        return s.get(models.User, user.id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def test_surface_is_a_call_and_put_grid(client, sdb):
    j = client.get("/calc/api/surface", params={"symbol": "acme"}).json()
    assert j["ok"] and j["symbol"] == "ACME" and j["spot"] == 100.0
    assert [e["date"] for e in j["expiries"]] == [m.isoformat() for m in MONTHLIES]
    assert j["strikes"] == sorted(j["strikes"]) and 65.0 <= j["strikes"][0] and j["strikes"][-1] <= 135.0
    assert len(j["calls"]) == len(j["strikes"]) == len(j["puts"]) == len(j["oi_c"])
    assert len(j["calls"][0]) == len(j["expiries"]) == len(j["atm"])
    i = j["strikes"].index(100.0)
    assert j["calls"][i][0] == pytest.approx(30.0) and j["puts"][i][0] == pytest.approx(30.0)
    assert j["oi_c"][i][0] == 500 and j["spot_row"] == i
    assert j["scale"][0] <= j["scale"][1] and j["earnings_date"] and j["as_of_et"]


def test_surface_errors_in_words(client, sdb):
    assert client.get("/calc/api/surface", params={"symbol": ""}).status_code == 400
    j = client.get("/calc/api/surface", params={"symbol": "ZZZZ"}).json()
    assert j["ok"] is False and "not in the screener" in j["error"]


def test_preview_gives_the_sheet_figures_without_saving(client, sdb, db):
    exp = MONTHLIES[1].isoformat()
    body = {"symbol": "ACME", "target": 120, "worst": 92, "days": 30, "sets": 13,
            "legs": [{"right": "C", "strike": 100, "expiry": exp, "qty": 1},
                     {"right": "C", "strike": 120, "expiry": exp, "qty": -1}]}
    j = client.post("/calc/api/preview", json=body).json()
    assert j["ok"] and j["spot"] == 100.0 and j["horizon_days"] == 30 and j["sets"] == 13
    assert j["target_date"] == (TODAY + _dt.timedelta(days=30)).isoformat()
    f = j["figures"]
    assert f["entry_value"] > 0 and f["target_value"] > f["entry_value"] and f["loss_at_worst"] < 0
    assert f["reward_over_risk"] > 0 and f["total_outlay"] == pytest.approx(f["entry_value"] * 100 * 13, rel=1e-6)
    assert len(f["legs"]) == 2 and f["legs"][1]["qty"] == -1 and f["legs"][0]["dte_target"] == f["legs"][0]["dte_entry"] - 30
    assert f["earnings_in_window"] is True and f["direction"] == "bull" and 0 <= f["prob_profit"] <= 1
    assert j["legs"][0]["iv"] == pytest.approx(0.30) and j["legs"][0]["oi"] == 500
    assert j["curves"]["grid"] and len(j["curves"]["legs"]) == 2
    assert db.query(OptionCalcRequest).count() == 0          # nothing saved


def test_preview_uses_the_member_default_days_and_validates(client, sdb):
    exp = MONTHLIES[1].isoformat()
    leg = [{"right": "P", "strike": 95, "expiry": exp, "qty": 1}]
    j = client.post("/calc/api/preview", json={"symbol": "ACME", "target": 90, "worst": 108, "legs": leg}).json()
    assert j["ok"] and j["horizon_days"] == 45                 # the default
    bad = client.post("/calc/api/preview", json={"symbol": "ACME", "target": "x", "worst": 1, "legs": leg}).json()
    assert bad["ok"] is False and "numbers" in bad["error"]
    none = client.post("/calc/api/preview", json={"symbol": "ACME", "target": 90, "worst": 108, "legs": []}).json()
    assert none["ok"] is False and "at least one leg" in none["error"]
    ghost = client.post("/calc/api/preview", json={"symbol": "ACME", "target": 90, "worst": 108,
                                                    "legs": [{"right": "C", "strike": 101, "expiry": exp, "qty": 1}]}).json()
    assert ghost["ok"] is False and "not in the last market pass" in ghost["error"]
    unknown = client.post("/calc/api/preview", json={"symbol": "ZZZZ", "target": 90, "worst": 108, "legs": leg}).json()
    assert unknown["ok"] is False and "not in the screener" in unknown["error"]


def test_preview_with_an_iv_change_on_the_target_date(client, sdb):
    """v4.217 (user: "the IV-change scenario on the target date"): ``iv_shift`` in vol points; today's figures stay,
    the target-date ones move, and the IV-unchanged figures and curve ride along; bad values are refused in words."""
    exp = MONTHLIES[1].isoformat()
    leg = [{"right": "C", "strike": 100, "expiry": exp, "qty": 1}]
    body = {"symbol": "ACME", "target": 110, "worst": 92, "days": 30, "legs": leg}
    flat = client.post("/calc/api/preview", json=body).json()
    j = client.post("/calc/api/preview", json={**body, "iv_shift": -10}).json()
    assert j["ok"] and j["iv_shift"] == -10.0 and flat["iv_shift"] == 0.0 and "base" not in flat["figures"]
    f, fb = j["figures"], j["figures"]["base"]
    assert f["entry_value"] == pytest.approx(flat["figures"]["entry_value"])
    assert f["target_value"] < flat["figures"]["target_value"] and fb["target_value"] == pytest.approx(flat["figures"]["target_value"])
    assert j["legs"][0]["iv_target"] == pytest.approx(0.20) and flat["legs"][0]["iv_target"] == pytest.approx(0.30)
    assert len(j["curves"]["net_target_base"]) == len(j["curves"]["grid"]) and "net_target_base" not in flat["curves"]
    bad = client.post("/calc/api/preview", json={**body, "iv_shift": 500}).json()
    assert bad["ok"] is False and "vol points" in bad["error"]

