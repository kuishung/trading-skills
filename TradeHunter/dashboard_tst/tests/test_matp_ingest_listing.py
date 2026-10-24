"""v4.194: `POST /api/matp` lists, with no MATP, every ticker a covered Finviz screen holds that
the agent did not post - the user added Finviz's ETF screener (ETFs have no analyst targets, so the
agent posted nothing) and read "No tickers here yet"."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.config import settings
from app.db import get_db
from app.main import app
from app.services import matp_queue

KEY = "test-ingest-key"


@pytest.fixture
def client(engine, monkeypatch):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(settings, "ingest_api_key", KEY)
    app.dependency_overrides[get_db] = _db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def _filter(db, user, desc, url):
    f = models.FinvizFilter(description=desc, url=url, is_active=True, created_by=user.id)
    db.add(f)
    db.commit()
    return f


def test_screened_tickers_the_agent_did_not_post_are_listed_price_only(client, db, user, monkeypatch):
    etf = _filter(db, user, "Robotics", "https://finviz.com/screener/etf?v=181&t=BOTZ,ROBO,ARKQ")
    screens = {etf.id: (["BOTZ", "ROBO", "ARKQ"], None)}
    monkeypatch.setattr(matp_queue, "filter_symbols", lambda f, *, force_refresh=False: screens[f.id])
    # the agent found targets for none of them: an empty closing push
    r = client.post("/api/matp", headers={"X-API-Key": KEY},
                    json={"items": [], "filter_id": etf.id, "prune": True, "final": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["listed_without_matp"] == 3
    rows = {lv.symbol: lv for lv in db.query(models.MATPLevel).all()}
    assert set(rows) == {"BOTZ", "ROBO", "ARKQ"}
    for lv in rows.values():
        assert lv.filter_id == etf.id and lv.status == "active" and lv.matp is None and lv.mbp is None and lv.n_targets == 0
    db.expire_all()
    assert db.get(models.FinvizFilter, etf.id).last_run_at is not None          # the run still counts as done
    # the Watchlist page lists them, the MATP column a dash that says why
    from app.routes import matp as matp_routes
    ctx = matp_routes._watchlist_context(db, user, str(etf.id), None)
    assert [lv.symbol for lv in ctx["qualified"]] == ["ARKQ", "BOTZ", "ROBO"]


def test_posted_tickers_keep_their_matp_and_a_dropped_one_comes_back_price_only(client, db, user, monkeypatch):
    f = _filter(db, user, "Chips", "https://finviz.com/screener/stocks?t=NVDA,AMD,INTC")
    other = _filter(db, user, "Other screen", "https://finviz.com/screener/stocks?t=AMD")
    db.add(models.MATPLevel(symbol="INTC", filter_id=f.id, matp=30.0, mbp=26.09, n_targets=4, status="active"))
    db.add(models.MATPLevel(symbol="AMD", filter_id=other.id, matp=200.0, mbp=173.91, n_targets=9, status="active"))
    db.commit()
    monkeypatch.setattr(matp_queue, "filter_symbols", lambda flt, *, force_refresh=False: (["NVDA", "AMD", "INTC"], None))
    # the agent posts NVDA only: INTC lost its post-earnings coverage (pruned), AMD belongs to another screen
    r = client.post("/api/matp", headers={"X-API-Key": KEY},
                    json={"items": [{"symbol": "NVDA", "matp": 210.0, "n_targets": 12}],
                          "filter_id": f.id, "prune": True, "final": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["dropped"] == 1 and j["listed_without_matp"] == 1
    db.expire_all()
    rows = {lv.symbol: lv for lv in db.query(models.MATPLevel).all()}
    assert rows["NVDA"].matp == 210.0 and rows["NVDA"].status == "active"
    assert rows["INTC"].status == "active" and rows["INTC"].matp is None and rows["INTC"].n_targets == 0   # back, price-only
    assert rows["AMD"].filter_id == other.id and rows["AMD"].matp == 200.0                               # untouched
    # an incremental (non-final) push lists nothing extra
    r = client.post("/api/matp", headers={"X-API-Key": KEY},
                    json={"items": [{"symbol": "NVDA", "matp": 211.0}], "filter_id": f.id, "final": False})
    assert r.json()["listed_without_matp"] == 0


def test_a_screen_that_cannot_be_read_lists_nothing_new(client, db, user, monkeypatch):
    f = _filter(db, user, "Broken", "https://finviz.com/screener/stocks?v=111&f=nope")

    def boom(flt, *, force_refresh=False):
        raise RuntimeError("finviz down")

    monkeypatch.setattr(matp_queue, "filter_symbols", boom)
    r = client.post("/api/matp", headers={"X-API-Key": KEY}, json={"items": [], "filter_id": f.id, "final": True})
    assert r.status_code == 200 and r.json()["listed_without_matp"] == 0
