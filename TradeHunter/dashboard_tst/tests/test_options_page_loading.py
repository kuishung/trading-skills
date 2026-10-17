"""The Options page on a fresh start: the start-up load of the option market holds the frame
lock, and ``frame.current()`` answers with the empty LOADING frame (v4.144). The page and its
status API must still render - the REAL engine, not the page tests' fake one."""

from __future__ import annotations

import pytest

from .test_options_page import client, no_screener_db, screener_url  # noqa: F401


@pytest.fixture
def market_loading():
    """Hold the frame's load lock for the test, as the start-up load does on Hermes."""
    from app.services.screener import frame as fr

    assert fr._load_lock.acquire(blocking=False), "the load lock is already held"
    try:
        yield fr
    finally:
        fr._load_lock.release()


def test_the_page_renders_while_the_market_is_still_loading(client, screener_url, market_loading):
    pytest.importorskip("numpy")
    r = client.get("/options")
    assert r.status_code == 200, r.text[:800]
    assert "loading" in r.text.lower()


def test_the_status_api_answers_while_the_market_is_still_loading(client, screener_url, market_loading):
    pytest.importorskip("numpy")
    r = client.get("/options/api/status")
    assert r.status_code == 200, r.text[:800]
