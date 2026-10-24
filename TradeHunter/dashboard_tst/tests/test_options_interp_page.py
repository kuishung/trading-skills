"""The Interpolation page (v4.209): `/options/interpolation` and its two APIs, `/options/api/cm/point`
and `/options/api/cm/series`, against the surface tests' fixtures."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.test_options_page import client, fake, no_screener_db, screener_url  # noqa: F401 - the page fixtures live there
from tests.test_vol_surface import hist, sdb  # noqa: F401 - the Parquet history fixture

_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)


def test_the_page_boots_from_the_query_and_has_three_tabs(client, fake):
    r = client.get("/options/interpolation", params={"symbol": "amd", "day": "latest", "h": "90", "m": "0.95", "side": "otm", "window": "90"})
    assert r.status_code == 200
    html = r.text
    for needle in ('data-tab="work"', 'data-tab="charts"', 'data-tab="hist"', ">Working on the contract table<",
                   ">Term structure &amp; smile<", ">Interpolated history<", '"symbol": "AMD"', '"h": 90', '"m": 0.95',
                   '"window": 90', "fetch('/options/api/cm/point?'", "fetch('/options/api/cm/series?'",
                   "function drawOverlay()", "function termSvg(p, tr)", "function smileSvg(p)", "function renderHist(s)",
                   'href="/options?symbol=AMD&amp;tab=cm"', 'id="ipCsv"'):
        assert needle in html, needle
    # a bad level falls back to at-the-money; a strike instead of a moneyness
    assert '"m": 1.0' in client.get("/options/interpolation", params={"symbol": "AMD", "m": "abc"}).text
    assert '"k": 420.0' in client.get("/options/interpolation", params={"symbol": "AMD", "k": "420"}).text
    if shutil.which("node"):
        for i, m in enumerate(_SCRIPT_RE.finditer(html)):
            if "src=" in m.group("attrs") or "application/json" in m.group("attrs") or not m.group("body").strip():
                continue
            import tempfile, pathlib
            path = pathlib.Path(tempfile.mkdtemp()) / ("s%d.js" % i)
            path.write_text(m.group("body"), encoding="utf-8")
            chk = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            assert chk.returncode == 0, chk.stderr[:1200]


def test_the_point_and_series_apis_answer_or_say_why(client, fake, screener_url, hist, monkeypatch):
    from app import screener_db
    from app.screener_models import ScrContract, ScrUnderlying, ScrUnderlyingDaily
    import datetime as _dt

    asof = _dt.datetime(2026, 10, 7, 19, 30)
    with screener_db.session() as s:
        s.add(ScrUnderlying(symbol="AAPL", name="Apple", sec_type="stock", spot=101.0, history_done=False, history_tries=0))
        for exp in ("2026-11-20", "2026-12-18"):
            for k in (90.0, 100.0, 110.0):
                for rt in "CP":
                    s.add(ScrContract(symbol="AAPL", expiry=exp, right=rt, strike=k, weekly=False, iv=0.3,
                                      session="2026-10-07", as_of=asof, delta=(0.45 if rt == "C" else -0.55), oi=100, volume=5))
        for day, c in (("2026-10-05", 98.0), ("2026-10-06", 99.0), ("2026-10-07", 101.0)):   # the stock closes the history prices against
            s.add(ScrUnderlyingDaily(symbol="AAPL", on=day, close=c, close_raw=c))
        s.commit()
    j = client.get("/options/api/cm/point", params={"symbol": "aapl", "h": "60", "m": "0.95"}).json()
    assert j["ok"] is True and j["symbol"] == "AAPL" and j["h"] == 60 and j["iv"] is not None
    assert j["time"]["lo"]["date"] == "2026-11-20" and j["time"]["hi"]["date"] == "2026-12-18" and j["table"]["lo_col"] is not None
    assert j["strike"]["k_lo"]["strike"] == 90.0 and j["strike"]["k_hi"]["strike"] == 100.0
    # a strike cell; a bad horizon; neither m nor k
    assert client.get("/options/api/cm/point", params={"symbol": "AAPL", "h": "60", "k": "100"}).json()["fixed"] == "strike"
    bad = client.get("/options/api/cm/point", params={"symbol": "AAPL", "h": "abc", "m": "0.95"})
    assert bad.status_code == 400 and "horizon" in bad.json()["error"]
    assert client.get("/options/api/cm/point", params={"symbol": "AAPL", "h": "60"}).status_code == 400
    assert client.get("/options/api/cm/point", params={"symbol": "NOPE", "h": "60", "m": "1"}).json()["ok"] is False
    # the series over the converted history (the fixture's three sessions)
    monkeypatch.setenv("TST_OPTION_HISTORY_DIR", str(hist))
    from app.services import vol_cm_series
    vol_cm_series.clear_cache()
    s = client.get("/options/api/cm/series", params={"symbol": "AAPL", "h": "60", "m": "0.95", "window": "365"}).json()
    assert s["ok"] is True and s["days"] >= 1 and s["stats"] and s["rows"][0]["iv"] is not None
    assert client.get("/options/api/cm/series", params={"symbol": "AAPL", "h": "60", "m": "0.95", "window": "7"}).status_code == 400
