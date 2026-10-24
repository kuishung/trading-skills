"""v4.195: a Finviz screen's tickers appear on its watchlist at once, price only - when the filter is
added in Data Ingest and on the first look at an untouched screen - instead of after a MATP run."""
from __future__ import annotations

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.routes import matp as matp_routes
from app.security import current_user
from app.services import matp_queue


@pytest.fixture
def moderator(db):
    u = models.User(email="mod@local.test", display_name="Mod", role=models.ROLE_ADMIN, status=models.APPROVED)   # /finviz is admin-only
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def mclient(engine, moderator):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        return s.get(models.User, moderator.id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


ETF_URL = "https://finviz.com/screener/etf?v=181&t=BOTZ%2CROBO%2CARKQ&preset=s153982030"


def test_adding_a_filter_in_data_ingest_lists_its_tickers_at_once(mclient, db, monkeypatch):
    monkeypatch.setattr(matp_queue, "filter_symbols", lambda f, *, force_refresh=False: (["BOTZ", "ROBO", "ARKQ"], None))
    r = mclient.post("/finviz/filters", data={"description": "Robotics", "url": ETF_URL, "is_active": "on"})
    assert r.status_code == 303
    f = db.query(models.FinvizFilter).filter(models.FinvizFilter.description == "Robotics").one()
    rows = db.query(models.MATPLevel).filter(models.MATPLevel.filter_id == f.id).all()
    assert sorted(lv.symbol for lv in rows) == ["ARKQ", "BOTZ", "ROBO"]
    assert all(lv.matp is None and lv.mbp is None and lv.n_targets is None and lv.status == "active" for lv in rows)
    # the watchlist shows them (price only), in the qualified list
    html = mclient.get("/matp/watchlist", params={"wl": str(f.id)}).text
    for sym in ("ARKQ", "BOTZ", "ROBO"):
        assert ">%s<" % sym in html
    assert "No tickers here yet" not in html and 'title="No MATP: not computed yet"' in html


def test_the_first_look_at_an_untouched_screen_seeds_it_and_a_run_screen_is_left_alone(db, user, monkeypatch):
    f = models.FinvizFilter(description="Chips", url="https://finviz.com/screener/stocks?t=NVDA,AMD", is_active=True,
                            created_by=user.id)
    g = models.FinvizFilter(description="Run before", url="https://finviz.com/screener/stocks?t=INTC", is_active=True,
                            created_by=user.id)
    db.add_all([f, g])
    db.commit()
    db.add(models.MATPLevel(symbol="INTC", filter_id=g.id, matp=30.0, mbp=26.09, status="dropped"))   # a run touched g
    db.commit()
    calls = []

    def fake(flt, *, force_refresh=False):
        calls.append(flt.id)
        return (["NVDA", "AMD"], None) if flt.id == f.id else (["INTC"], None)

    monkeypatch.setattr(matp_queue, "filter_symbols", fake)
    monkeypatch.setattr(matp_routes, "_watchlist_signals_fast", lambda levels: {})
    monkeypatch.setattr(matp_routes, "_watchlist_liquidity", lambda levels, user: {})
    ctx = matp_routes._watchlist_context(db, user, str(f.id), None)
    assert [lv.symbol for lv in ctx["qualified"]] == ["AMD", "NVDA"] and calls == [f.id]
    assert db.query(models.MATPLevel).filter(models.MATPLevel.filter_id == f.id).count() == 2
    ctx = matp_routes._watchlist_context(db, user, str(g.id), None)      # every row dropped: not re-seeded
    assert ctx["qualified"] == [] and calls == [f.id]
    # the seeder never duplicates a symbol that already has a row anywhere
    assert matp_queue.seed_filter_members(db, f) == []


def test_a_screen_that_cannot_be_read_seeds_nothing(db, user, monkeypatch):
    f = models.FinvizFilter(description="Broken", url="https://finviz.com/screener/stocks?v=111&f=nope", is_active=True,
                            created_by=user.id)
    db.add(f)
    db.commit()

    def boom(flt, *, force_refresh=False):
        raise RuntimeError("finviz down")

    monkeypatch.setattr(matp_queue, "filter_symbols", boom)
    assert matp_queue.seed_filter_members(db, f) == []
    assert matp_routes._seed_filter(db, f.id) == [] and matp_routes._seed_filter(db, 999_999) == []
