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
    for needle in ('data-tab="work"', 'data-tab="charts"', 'data-tab="hist"', ">Surface map &amp; working<",
                   ">Term structure &amp; smile<", ">Interpolated history<", '"symbol": "AMD"', '"h": 90', '"m": 0.95',
                   '"window": 90', "fetch('/options/api/cm/point?'", "fetch('/options/api/cm/series?'",
                   "function mapSvg(p)", "function mapColor(v, lo, hi)", "function termSvg(p, tr)", "function smileSvg(p)", "function renderHist(s)",
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
    assert [e["dte"] for e in j["map"]["expiries"]] == [e["dte"] for e in j["table"]["expiries"]][:0] or len(j["map"]["expiries"]) >= 2
    assert [s["strike"] for s in j["map"]["strikes"]] == [90.0, 100.0, 110.0] and j["map"]["strikes"][0]["leg"] is True and j["map"]["strikes"][2]["leg"] is False
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
    # v4.210: the hover card's chart - the series points and the earnings keys; `end` = a past-day surface's own day
    assert s["series"] and len(s["series"][0]) == 2 and "earnings" in s and s["rows"][0]["price"] is not None
    cut = client.get("/options/api/cm/series", params={"symbol": "AAPL", "h": "60", "m": "0.95", "window": "365", "end": "2026-10-06"}).json()
    assert cut["ok"] is True and cut["rows"][0]["on"] == "2026-10-06"
    assert client.get("/options/api/cm/series", params={"symbol": "AAPL", "h": "60", "m": "0.95", "end": "soon"}).json()["ok"] is True   # a bad date is ignored
    # v4.215: a job still running answers "pending" (the page asks again); an index says why
    import threading
    gate = threading.Event()
    real = vol_cm_series.series
    monkeypatch.setattr(vol_cm_series, "series", lambda *a, **kw: (gate.wait(5), real(*a, **kw))[1])
    vol_cm_series.clear_cache()
    p = client.get("/options/api/cm/series", params={"symbol": "AAPL", "h": "90", "m": "1", "window": "365", "wait": "0.05"}).json()
    assert p["ok"] is False and p["pending"] is True and p["error"].startswith("still rebuilding")
    gate.set()
    assert client.get("/options/api/cm/series", params={"symbol": "AAPL", "h": "90", "m": "1", "window": "365", "wait": "10"}).json()["ok"] is True


def test_the_charts_zoom_and_skip_colliding_labels(client, fake):
    """v4.211 (user: "the graph in term structure have to make it smaller and can be animated to zoom in and out
    so the data does not overlapped") and v4.213 ("this cannot zoom" - the smile): one zoomer per chart kind
    (the term charts share one, the smile has its own, opening on the strikes within 30 % of the price); buttons,
    the wheel about the cursor and a drag to pan; axes rescaled to the points in view; only the labels that fit."""
    html = client.get("/options/interpolation", params={"symbol": "AAPL", "h": "90", "m": "0.975"}).text
    for needle in (".ip-term, .ip-smile { max-width: 40rem; }", ".ip-term svg, .ip-smile svg { cursor: grab; touch-action: none; }",
                   "var CH = {H: 200, T: 14, B: 40};", "function zoomer(W, L, R, minSpan, pad, draw)",
                   "var TERM = zoomer(640, 46, 70, 3, 1, drawTerm), SMILE = zoomer(640, 46, 20, 0, 0, drawSmile);",
                   "function termFull(p)", "function termLegs(p)", "function smileFull(p)", "function smileNear(p)", "function smileLegs(p)",
                   "function zClamp(z, a, b)", "function zTo(z, a, b, animate)", "function zBy(z, f, at)", "function zAt(z, svg, clientX)", "function zOf(el)",
                   "function drawTerm()", "function drawSmile()", "function pickLabels(items, gap)",
                   "var k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);", "dur = 300",
                   "function zoomBar(kind, extra, note)", "'<div class=\"ip-zoom\" data-for=\"' + kind + '\">Zoom <button type=\"button\" data-z=\"in\"",
                   'data-z="out"', 'data-z="legs"', 'data-z="all"', 'data-z="near" title="Strikes within 30% of the price">Near the price</button>',
                   "zoomBar('term', '', 'wheel over a chart to zoom at the cursor, drag to pan - both term charts move together')",
                   "SMILE.full = smileFull(p); var sn = smileNear(p) || SMILE.full; SMILE.x0 = sn[0]; SMILE.x1 = sn[1];",
                   "<div class=\"ip-term\" data-i=\"' + i + '\">", "<div class=\"ip-smile\">",
                   "e.target.closest('.ip-term, .ip-smile')", "zBy(z, e.deltaY > 0 ? 1.25 : 0.8, zAt(z, box.querySelector('svg'), e.clientX));",
                   "zTo(z, drag.x0 + dx, drag.x1 + dx, false);", "if (k === 'in') zBy(z, 0.6);", "else if (k === 'all') zTo(z, z.full[0], z.full[1], true);",
                   "var pts = all.filter(function (q) { return q.dte >= x0 && q.dte <= x1; });",
                   "var pts = all.filter(function (q) { return q.strike >= k0 && q.strike <= k1; });",
                   "<clipPath id=\"' + cid + '\">", '<clipPath id="ipsm">', "pickLabels(labels, 26)", "pickLabels(tops, 48)", "pickLabels(labels, 30)",
                   "' (' + Math.round(x0) + 'd to ' + Math.round(x1) + 'd in view)</text></svg>'",
                   "num(k0, 0) + ' to ' + num(k1, 0) + ' in view)</text></svg>'",
                   "var near = legs.some(function (q) { return Math.abs(X(q.dte) - rx) < 60; });",
                   "var near = legs.some(function (q) { return Math.abs(X(q.strike) - rx) < 60; });"):
        assert needle in html, needle
    assert "i % Math.ceil(pts.length / 16) === 0" not in html         # the smile's every-n-th strike label is gone
    assert "var W = 640, H = 230" not in html                           # both charts are 200 high now
    if shutil.which("node"):
        import pathlib, tempfile
        # the clamp and the label picker, run as written
        js = html[html.index("  function zClamp(z, a, b) {"):html.index("  function zTo(")] + \
             html[html.index("  function pickLabels(items, gap) {"):html.index("  function termSvg(p, tr) {")]
        js = js + """
var z = {full: [1, 800], minSpan: 3, pad: 1};
var r = zClamp(z, -50, -48); console.log(r[0], r[1]);
var r2 = zClamp(z, 700, 900); console.log(r2[0], r2[1]);
var items = pickLabels([{x: 10, pri: 1}, {x: 20, pri: 2}, {x: 30, pri: 1}, {x: 60, pri: 1}, {x: 62, pri: 3}], 26);
console.log(items.map(function (i) { return i.ok ? 1 : 0; }).join(''));
"""
        path = pathlib.Path(tempfile.mkdtemp()) / "zoom.js"
        path.write_text(js, encoding="utf-8")
        out = subprocess.run(["node", str(path)], capture_output=True, text=True)
        assert out.stdout.split() == ["0", "3", "601", "801", "01001"], out.stdout + out.stderr
