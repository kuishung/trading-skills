"""`GET /api/refresh-queue` (the Nous agent's poll): since v4.156 a filter request carries
the screener's tickers, resolved server-side, so the agent never browses Finviz (its
browse read nothing after Finviz's 2026 redesign and every filter run came back empty)."""
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


def _seed(db, user, url="https://finviz.com/screener/stocks?t=NVDA,TSM&v=121"):
    f = models.FinvizFilter(description="BI AI Tickers", url=url, is_active=True, created_by=user.id)
    db.add(f)
    db.flush()
    db.add(models.MATPRefreshRequest(scope="filter", filter_id=f.id, requested_by=user.id, status="pending"))
    db.add(models.MATPRefreshRequest(scope="ticker", symbol="AAPL", requested_by=user.id, status="pending"))
    db.commit()
    return f


def test_filter_requests_carry_their_tickers(client, db, user, monkeypatch):
    f = _seed(db, user)
    calls = []

    def fake(flt, *, force_refresh=False):
        calls.append((flt.id, force_refresh))
        return ["NVDA", "TSM"], None
    monkeypatch.setattr(matp_queue, "filter_symbols", fake)
    r = client.get("/api/refresh-queue", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    reqs = r.json()["requests"]
    flt = next(x for x in reqs if x["scope"] == "filter")
    tick = next(x for x in reqs if x["scope"] == "ticker")
    assert flt["filter_id"] == f.id and flt["filter_url"] == f.url
    assert flt["symbols"] == ["NVDA", "TSM"] and flt["n_symbols"] == 2 and flt["symbols_error"] is None
    assert "symbols" not in tick and tick["symbol"] == "AAPL"
    assert calls == [(f.id, False)]            # a good answer is not re-read


def test_an_empty_cached_answer_is_read_again(client, db, user, monkeypatch):
    f = _seed(db, user)
    answers = iter([([], None), (["NVDA"], None)])
    calls = []

    def fake(flt, *, force_refresh=False):
        calls.append(force_refresh)
        return next(answers)
    monkeypatch.setattr(matp_queue, "filter_symbols", fake)
    flt = next(x for x in client.get("/api/refresh-queue", headers={"X-API-Key": KEY}).json()["requests"]
               if x["scope"] == "filter")
    assert flt["symbols"] == ["NVDA"] and calls == [False, True]


def test_a_screener_that_resolves_nothing_says_why(client, db, user, monkeypatch):
    _seed(db, user)
    monkeypatch.setattr(matp_queue, "filter_symbols",
                        lambda flt, *, force_refresh=False: ([], "URLError: finviz unreachable"))
    flt = next(x for x in client.get("/api/refresh-queue", headers={"X-API-Key": KEY}).json()["requests"]
               if x["scope"] == "filter")
    assert flt["symbols"] == [] and flt["n_symbols"] == 0 and "unreachable" in flt["symbols_error"]


def test_the_key_is_still_required(client, db, user):
    _seed(db, user)
    assert client.get("/api/refresh-queue").status_code == 401
    assert client.get("/api/refresh-queue", headers={"X-API-Key": "wrong"}).status_code == 401
