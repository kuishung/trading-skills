"""The Options page is blank since v4.135: ``routes/options_page.py`` serves only
``GET /options``; the v2 page, its fragments and its screener are gone, and the Massive
data pipeline (``massive``, ``opt_massive``, ``opt_store``, ``opt_collector``) stays."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.security import current_user

APP_DIR = Path(__file__).resolve().parent.parent / "app"


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


def test_the_page_is_blank(client):
    r = client.get("/options")
    assert r.status_code == 200
    html = r.text
    assert 'id="optPage"' in html and "Options" in html
    for gone in ("optBasket", "optStatus", "/options/status", "/options/rules", "/options/results",
                 "/options/basket", "/options/payoff"):
        assert gone not in html, gone


def test_the_router_serves_only_the_page():
    from app.routes import options_page

    paths = {(r.path, tuple(sorted(r.methods))) for r in options_page.router.routes}
    assert paths == {("/options", ("GET",))}


def test_the_screener_and_page_files_are_gone():
    for mod in ("opt_rules", "opt_screen", "payoff", "opt_legs"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"app.services.{mod}")
    for name in ("_opt_basket", "_opt_help", "_opt_results", "_opt_rules", "_opt_status", "_opt_trade",
                 "_payoff_chart"):
        assert not (APP_DIR / "templates" / f"{name}.html").exists(), name
    assert "--po-" not in (APP_DIR / "templates" / "base.html").read_text(encoding="utf-8")


def test_the_massive_pipeline_stays():
    from app.services import black_scholes, massive, opt_collector, opt_massive, opt_store  # noqa: F401

    assert opt_massive.implied_vol is black_scholes.implied_vol
    # the solver round-trips a Black-Scholes price at the platform rate
    from app.services.opt_constants import RISK_FREE

    p = black_scholes.black_scholes(100.0, 95.0, 45 / 365, RISK_FREE, 0.32, "put").price
    assert abs(black_scholes.implied_vol(p, 100.0, 95.0, 45 / 365, "P") - 0.32) < 1e-6
    assert black_scholes.implied_vol(0.0, 100.0, 95.0, 45 / 365, "P") is None
    assert black_scholes.implied_vol(p, 100.0, 95.0, 0, "P") is None
    assert black_scholes.implied_vol(None, 100.0, 95.0, 45 / 365, "C") is None


def test_the_menu_entry_stays():
    """The nav keeps its Options item (the page keeps its require_menu("options") gate in main.py)."""
    from app import menus

    assert ("options", "Options", None, "/options") in menus.MENUS
