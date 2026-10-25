"""Ticker logos (v4.221, ``services/logos.py`` + ``GET /logo/<SYM>`` + ``ticker_logo`` in the templates): fetched once
from a public logo service, kept on disk, a letters badge when there is none - shown wherever a ticker is."""
from __future__ import annotations

import os
import time

import pytest

from tests.test_options_page import client, fake, no_screener_db, screener_url  # noqa: F401 - a signed-in client
from tests.test_calc_health import admin_client  # noqa: F401 - Settings -> Data Ingest is admin-only

from app.services import logos

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 300


def test_symbols_are_cleaned():
    assert logos.clean_symbol("brk.b") == "BRK-B" and logos.clean_symbol(" amd ") == "AMD"
    assert logos.clean_symbol("../etc") is None and logos.clean_symbol("") is None and logos.clean_symbol("A" * 20) is None


def test_a_logo_is_fetched_once_and_kept(tmp_path):
    calls = []

    def fetcher(sym):
        calls.append(sym)
        return PNG, "fmp"

    assert logos.logo_png("amd", root=tmp_path, fetcher=fetcher) == PNG
    assert logos.logo_png("AMD", root=tmp_path, fetcher=fetcher) == PNG
    assert calls == ["AMD"] and (tmp_path / "AMD.png").read_bytes() == PNG
    # after KEEP_DAYS it is fetched again; a failed refresh keeps the old one
    old = time.time() - (logos.KEEP_DAYS + 1) * 86400
    os.utime(tmp_path / "AMD.png", (old, old))
    assert logos.logo_png("AMD", root=tmp_path, fetcher=lambda s: (None, None)) == PNG


def test_a_ticker_without_a_logo_is_remembered_then_asked_again(tmp_path):
    calls = []

    def none(sym):
        calls.append(sym)
        return None, None

    assert logos.logo_png("ZZZZQ", root=tmp_path, fetcher=none) is None
    assert logos.logo_png("ZZZZQ", root=tmp_path, fetcher=none) is None
    assert calls == ["ZZZZQ"] and (tmp_path / "ZZZZQ.none").exists()
    old = time.time() - (logos.MISS_DAYS + 1) * 86400
    os.utime(tmp_path / "ZZZZQ.none", (old, old))
    assert logos.logo_png("ZZZZQ", root=tmp_path, fetcher=lambda s: (PNG, "parqet")) == PNG
    assert not (tmp_path / "ZZZZQ.none").exists()


def test_only_real_pngs_are_taken(monkeypatch):
    class R:
        def __init__(self, code, ctype, body):
            self.status_code, self.headers, self.content = code, {"content-type": ctype}, body

    import httpx
    answers = {"fmp": R(404, "text/html", b"not found"), "parqet": R(200, "image/png", PNG)}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: answers["fmp" if "financialmodelingprep" in url else "parqet"])
    assert logos.fetch("AMD") == (PNG, "parqet")
    answers["parqet"] = R(200, "image/png", b"\x89PNG tiny")
    assert logos.fetch("AMD") == (None, None)


def test_the_letters_badge():
    svg = logos.initials_svg("brk.b")
    assert svg.startswith("<svg") and ">BR</text>" in svg and 'aria-label="BRK-B"' in svg
    assert logos.initials_svg("BRK-B") == svg and ">F</text>" in logos.initials_svg("F")


def test_the_route_never_waits_and_queues_what_it_lacks(client, monkeypatch):
    """v4.222 (user: "when loading the logo it takes too long can we download it in hermes so it load fast?"): the
    stored logo at once; else the letters badge at once (kept two minutes) with the ticker queued at the FRONT."""
    monkeypatch.setattr(logos, "stored_png", lambda sym: PNG if sym.upper() == "AMD" else None)
    monkeypatch.setattr(logos, "is_fresh", lambda sym, root=None: False)
    monkeypatch.setattr(logos, "logo_png", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download in a request")))
    logos._Q.clear()
    r = client.get("/logo/AMD")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert "max-age=604800" in r.headers["cache-control"]
    logos.enqueue(["AAA"])
    r = client.get("/logo/ZZZZQ")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and b">ZZ</text>" in r.content
    assert "max-age=120" in r.headers["cache-control"] and logos.queued()[0] == "ZZZZQ"     # ahead of AAA
    # a ticker no service has (a recent "none") is not queued again and the badge is kept a day
    monkeypatch.setattr(logos, "is_fresh", lambda sym, root=None: True)
    r = client.get("/logo/QQQQX")
    assert "max-age=86400" in r.headers["cache-control"] and "QQQQX" not in logos.queued()
    logos._Q.clear()
    from app.main import _ticker_logo
    tag = str(_ticker_logo("brk.b", 20, "tk-lg"))
    assert tag == '<img class="tk-logo tk-lg" src="/logo/BRK-B" width="20" height="20" alt="" loading="lazy" decoding="async">'
    assert str(_ticker_logo("../x")) == ""


def test_the_queue_the_worker_step_and_the_status(tmp_path):
    logos._Q.clear()
    (tmp_path / "OLD.png").write_bytes(PNG)                       # fresh: never queued
    assert logos.warm(["old", "amd", "AMD", "brk.b", "../x"], root=tmp_path) == 2
    assert logos.queued() == ["AMD", "BRK-B"]
    assert logos.enqueue(["BRK-B"], front=True, root=tmp_path) == 0 and logos.queued() == ["BRK-B", "AMD"]
    sym = logos._next()
    assert sym == "BRK-B" and logos.work_one(sym, root=tmp_path, fetcher=lambda s: (PNG, "fmp")) == PNG
    assert logos.work_one("AMD", root=tmp_path, fetcher=lambda s: (None, None)) is None
    st = logos.status(root=tmp_path)
    assert st["stored"] == 2 and st["missing"] == 1 and st["queued"] == 1 and st["last_symbol"] == "AMD"
    assert st["sources"] == ["fmp", "parqet"] and st["last_warm_queued"] == 2 and st["off"] is True   # the tests switch it off
    assert logos.start_background() is False                        # TST_LOGO_WARM=0 in the tests
    logos._Q.clear()


def test_the_data_ingest_tab(admin_client, monkeypatch):
    client = admin_client
    monkeypatch.setattr(logos, "status", lambda root=None: {
        "started": "2026-10-25T10:00:00+00:00", "off": False, "stored": 1234, "queued": 56, "missing": 7,
        "fetched": 60, "found": 55, "missed": 5, "last_symbol": "AMD", "last_fetch_at": "2026-10-25T10:05:00+00:00",
        "last_warm_at": "2026-10-25T10:00:20+00:00", "last_warm_queued": 1290, "last_error": None,
        "sources": ["fmp", "parqet"], "dir": "state/logos", "keep_days": 30, "miss_days": 3, "warm_every_h": 12})
    html = client.get("/finviz/logos").text
    for needle in (">Downloading</b>", "1,234</b> logos stored", "56</b> waiting", "7</b> tickers with no logo anywhere",
                   "last AMD at", 'id="lgWarm"', "fmp or parqet"):
        assert needle in html, needle
    page = client.get("/finviz").text
    assert 'data-tab="logos"' in page and 'data-panel="logos"' in page and "window.loadLogos = function loadLogos()" in page
    monkeypatch.setattr(logos, "warm", lambda symbols=None, root=None: 42)
    assert client.post("/finviz/logos/warm").json()["queued"] == 42
