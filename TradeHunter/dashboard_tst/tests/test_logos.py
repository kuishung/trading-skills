"""Ticker logos (v4.221, ``services/logos.py`` + ``GET /logo/<SYM>`` + ``ticker_logo`` in the templates): fetched once
from a public logo service, kept on disk, a letters badge when there is none - shown wherever a ticker is."""
from __future__ import annotations

import os
import time

import pytest

from tests.test_options_page import client, fake, no_screener_db, screener_url  # noqa: F401 - a signed-in client

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


def test_the_route_and_the_template_helper(client, monkeypatch):
    monkeypatch.setattr(logos, "logo_png", lambda sym: PNG if sym.upper() == "AMD" else None)
    r = client.get("/logo/AMD")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert "max-age=604800" in r.headers["cache-control"]
    r = client.get("/logo/ZZZZQ")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and b">ZZ</text>" in r.content
    from app.main import _ticker_logo
    tag = str(_ticker_logo("brk.b", 20, "tk-lg"))
    assert tag == '<img class="tk-logo tk-lg" src="/logo/BRK-B" width="20" height="20" alt="" loading="lazy" decoding="async">'
    assert str(_ticker_logo("../x")) == ""
