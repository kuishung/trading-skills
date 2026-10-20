"""The Watchlist grid (`_wl_macros.html` ticker_grid, v4.158): the price-to-MATP gap in $ and %
where the Trend and Sig columns were."""
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


def test_grid_shows_the_gap_to_matp_and_no_trend_or_sig(client, db, monkeypatch):
    db.add(models.MATPLevel(symbol="NVDA", exchange="NASDAQ", matp=200.0, mbp=173.9, trend="uptrend", signal="HOT"))
    db.add(models.MATPLevel(symbol="ABOVE", exchange="NYSE", matp=100.0, mbp=87.0, trend="uptrend", signal=None))
    db.add(models.MATPLevel(symbol="NOMATP", exchange="NYSE", matp=None, mbp=None, trend=None, signal=None))
    db.commit()
    live = {"NVDA": {"current": 182.0, "trend": "uptrend", "signal": "HOT"},
            "ABOVE": {"current": 104.0, "trend": "uptrend", "signal": None},
            "NOMATP": {"current": 50.0, "trend": None, "signal": None}}
    monkeypatch.setattr(matp_routes, "_watchlist_signals_fast", lambda levels: live)
    r = client.get("/matp/watchlist", params={"wl": "all"})
    assert r.status_code == 200
    html = r.text
    # the headers
    assert "&Delta;$" in html or "Δ$" in html
    assert ">Trend<" not in html and ">Sig<" not in html
    # NVDA: 182 - 200 = -18.00, -9.0 % of the MATP, green (below the target)
    assert "-18.00" in html and "-9.0%" in html
    # ABOVE: 104 - 100 = +4.00, +4.0 %, red (past the target)
    assert "+4.00" in html and "+4.0%" in html
    i_nvda, i_above = html.index(">NVDA<"), html.index(">ABOVE<")
    assert "text-emerald-300" in html[i_nvda:i_nvda + 1500]
    assert "text-rose-300" in html[i_above:i_above + 1500]
    # no MATP -> dashes, no crash
    i_no = html.index(">NOMATP<")
    assert html[i_no:i_no + 900].count(">-<") >= 2
    # Price, MATP and MBP are still there
    assert ">Price<" in html and ">MATP<" in html and ">MBP<" in html and "173.90" in html
