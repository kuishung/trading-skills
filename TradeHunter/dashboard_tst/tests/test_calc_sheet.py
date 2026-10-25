"""The calculator's Sheet view (v4.157): ``/calc`` opens on an Excel-style sheet whose leg
picker is the volatility surface. The page markup and the Sheet / Setups switch, the
``?view=`` / ``?symbol=`` / ``?id=`` boot, the surface grid the picker draws, a preview round
trip with legs picked off that grid, and the plain-words answer for an unknown ticker."""
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


TODAY = clock.et_date()
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
    p = tmp_path / "state" / "option_calc_worker.json"
    monkeypatch.setattr(calc_worker, "STATE_PATH", p)
    return p


def _fill(sdb, *, symbol="ACME", spot=SPOT):
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
    sdb.add(ScrPass(kind="cycle", session=TODAY.isoformat(), started=as_of, finished=as_of, n_symbols=1,
                    n_ok=1, n_failed=0, n_contracts=170))
    sdb.commit()


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


def _boot(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="calcBoot">(.*?)</script>', html, re.S)
    assert m, "no boot block"
    return json.loads(m.group(1))


def _pick(surface: dict, right: str, strike: float, col: int, qty: int) -> dict:
    """A leg the way the page builds it from a clicked cell."""
    r = surface["strikes"].index(strike)
    grid = surface["calls"] if right == "C" else surface["puts"]
    assert grid[r][col] is not None, "the page only lets a cell with an IV be clicked"
    return {"right": right, "strike": strike, "expiry": surface["expiries"][col]["date"], "qty": qty}


# ────────────────────────────────── the page ──────────────────────────────────

# ────────────────────────────────── the picker ──────────────────────────────────

def test_the_surface_is_a_grid_for_the_picker(client, sdb):
    _fill(sdb)
    j = client.get("/calc/api/surface?symbol=acme").json()
    assert j["ok"] is True and j["symbol"] == "ACME" and j["spot"] == SPOT and "ET" in j["as_of_et"]
    assert [e["date"] for e in j["expiries"]] == [m.isoformat() for m in MONTHLIES]
    assert all(e["dte"] == (_dt.date.fromisoformat(e["date"]) - TODAY).days for e in j["expiries"])
    assert j["strikes"] == sorted(j["strikes"]) and 100.0 in j["strikes"]
    for g in ("calls", "puts", "oi_c", "oi_p"):
        assert len(j[g]) == len(j["strikes"]) and all(len(row) == len(j["expiries"]) for row in j[g])
    r = j["strikes"].index(100.0)
    assert j["calls"][r][0] == pytest.approx(30.0) and j["oi_c"][r][0] == 800      # IV in %, OI per cell
    assert j["spot_row"] == r and len(j["atm"]) == len(j["expiries"])
    lo, hi = j["scale"]
    assert lo <= hi


def test_preview_with_two_legs_picked_from_the_surface(client, sdb):
    _fill(sdb)
    s = client.get("/calc/api/surface?symbol=ACME").json()
    legs = [_pick(s, "C", 100.0, 1, 1), _pick(s, "C", 120.0, 1, -1)]      # a bull call spread off the grid
    p = client.post("/calc/api/preview", json={"symbol": "acme", "legs": legs, "target": 125, "worst": 92,
                                               "days": 30, "sets": 2}).json()
    assert p["ok"] is True and p["symbol"] == "ACME" and p["spot"] == SPOT and "ET" in p["as_of_et"]
    assert p["horizon_days"] == 30 and p["sets"] == 2
    assert p["target_date"] == (TODAY + _dt.timedelta(days=30)).isoformat()
    f = p["figures"]
    # the sheet's Option Values and Decision Indicators
    for k in ("entry_value", "target_value", "worst_value", "decayed_value", "reward_over_risk", "roi",
              "total_gains", "total_risk", "total_outlay", "prob_profit", "net_delta_entry", "net_delta_target",
              "total_decay", "breakevens"):
        assert k in f, k
    assert f["entry_value"] > 0 and f["target_value"] > f["entry_value"] > f["worst_value"]
    assert f["reward_over_risk"] > 0 and 0 <= f["prob_profit"] <= 1 and len(f["breakevens"]) == 1
    assert SPOT < f["breakevens"][0] < 120
    assert f["total_outlay"] == pytest.approx(f["entry_value"] * 100 * 2, rel=1e-4)
    assert f["earnings_in_window"] is False and f["direction"] == "bull"
    # the leg columns: one per picked cell, in the order picked
    assert [(lg["strike"], lg["qty"]) for lg in p["legs"]] == [(100.0, 1), (120.0, -1)]
    assert p["legs"][0]["dte"] == s["expiries"][1]["dte"] and p["legs"][0]["iv"] == pytest.approx(0.30)
    fl = f["legs"]
    assert len(fl) == 2 and fl[0]["price_entry"] > fl[1]["price_entry"] > 0
    assert fl[1]["value_entry"] == pytest.approx(-fl[1]["price_entry"], rel=1e-6)
    # the payoff chart
    c = p["curves"]
    assert c["grid"] and len(c["net_target"]) == len(c["grid"]) == len(c["net_entry"]) and len(c["legs"]) == 2


def test_the_preview_matches_what_a_saved_setup_computes(client, sdb):
    """"Save this setup" makes the same manual setup: its first calculation agrees with the sheet."""
    _fill(sdb)
    s = client.get("/calc/api/surface?symbol=ACME").json()
    legs = [_pick(s, "P", 95.0, 2, 1), _pick(s, "P", 85.0, 2, -1)]          # a bear put spread
    body = {"symbol": "ACME", "legs": legs, "target": 82, "worst": 106, "days": 40, "sets": 1}
    p = client.post("/calc/api/preview", json=body).json()
    assert p["ok"] is True and p["figures"]["direction"] == "bear"
    it = client.post("/calc/api/requests", json={"symbol": "ACME", "kind": "manual", "target": 82, "worst": 106,
                                                 "horizon_days": 40, "sets": 1, "legs": legs}).json()["item"]
    c = client.post("/calc/api/requests/%d/compute" % it["id"]).json()
    f = c["results"][0]["figures"]
    for k in ("entry_value", "target_value", "worst_value", "decayed_value", "reward_over_risk"):
        assert f[k] == pytest.approx(p["figures"][k], rel=1e-6, abs=1e-9), k


def test_an_unknown_ticker_is_said_in_words(client, sdb):
    _fill(sdb)
    exp = MONTHLIES[0].isoformat()
    p = client.post("/calc/api/preview", json={"symbol": "ZZZZ", "target": 10, "worst": 8,
                                               "legs": [{"right": "C", "strike": 10, "expiry": exp, "qty": 1}]}).json()
    assert p["ok"] is False and "ZZZZ" in p["error"] and "Traceback" not in p["error"]
    sf = client.get("/calc/api/surface?symbol=zzzz").json()
    assert sf["ok"] is False and "ZZZZ" in sf["error"]
    # a contract the pass does not list is named, not a crash
    q = client.post("/calc/api/preview", json={"symbol": "ACME", "target": 120, "worst": 90,
                                               "legs": [{"right": "C", "strike": 101, "expiry": exp, "qty": 1}]}).json()
    assert q["ok"] is False and "not in the last market pass" in q["error"]
