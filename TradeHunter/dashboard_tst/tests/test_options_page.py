"""The Options Screener page (v4.136, OPTIONS_SCREENER_DESIGN.md §7-§8):
``routes/options_page.py`` + ``templates/options.html`` + ``models.OptionScreen``.

Most tests run against a FAKE engine (``options_page._load_engine`` is replaced), so they
do not depend on the real engine being finished; one test runs the real engine (when it
imports) on an empty screener database. The v4.135 removals stay removed and the Massive
data pipeline keeps importing.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.routes import options_page
from app.security import current_user

from .conftest import downgrade, make_engine, table_names

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# ───────────────────────────────────────── a fake engine ─────────────────────────────────────────

FAMILIES = [
    ("Single options", ["Options Screener", "Long Call", "Long Put"]),
    ("Income", ["Covered Call", "Naked Put"]),
    ("Verticals", ["Bull Call Spread", "Bear Call Spread", "Bear Put Spread", "Bull Put Spread"]),
    ("Protection", ["Married Put", "Protective Collar"]),
    ("Straddles & strangles", ["Long Straddle", "Short Straddle", "Long Strangle", "Short Strangle"]),
    ("Calendars & diagonals", ["Long Call Calendar", "Long Put Calendar", "Long Call Diagonal",
                               "Short Call Diagonal", "Long Put Diagonal", "Short Put Diagonal"]),
    ("Butterflies & condors", ["Long Call Butterfly", "Short Call Butterfly", "Long Put Butterfly",
                               "Short Put Butterfly", "Long Iron Butterfly", "Short Iron Butterfly",
                               "Long Call Condor", "Short Call Condor", "Long Put Condor",
                               "Short Put Condor", "Long Iron Condor", "Short Iron Condor"]),
]


def _slug(label: str) -> str:
    return label.lower().replace(" ", "-")


class FakeEngine:
    """The §5 API with canned answers; records what the routes pass in."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail = False

    def screens(self):
        return [{"family": fam, "screens": [{"key": _slug(l), "label": l, "desc": f"{l} - test blurb",
                                             "legs": [{}] * (1 if fam == "Single options" else 2)}
                                            for l in labels]}
                for fam, labels in FAMILIES]

    def spec(self, key):
        label = next(l for _, ls in FAMILIES for l in ls if _slug(l) == key)
        return {
            "screen": {"key": key, "label": label, "desc": f"{label} - test blurb"},
            "defaults": {"filters": [{"f": "dte", "op": "between", "lo": 0, "hi": 60},
                                     {"f": "right", "op": "in", "v": ["C", "P"]},
                                     "volume"],
                         "sort": {"col": "volume", "dir": "desc"}},
            "fields": [
                {"key": "dte", "label": "Days to Expiration", "group": "Option Info", "level": "contract",
                 "kind": "range", "unit": "d", "presets": [{"label": "< 60", "hi": 60}, ("60-100", 60, 100)]},
                {"key": "right", "label": "Option Type", "group": "Option Info", "kind": "choice",
                 "choices": [("C", "Call"), ("P", "Put")]},
                {"key": "volume", "label": "Option Volume", "group": "Price & Volume", "kind": "range",
                 "help": "Contracts traded today"},
                {"key": "weekly", "label": "Weekly", "group": "Option Info", "kind": "bool"},
                {"key": "expiry", "label": "Expiration Date", "group": "Option Info", "kind": "date",
                 "ops": ["in", "eq", "between", "within", "gte", "lte"], "within": "days",
                 "presets": [{"label": "7 days", "v": 7}],
                 "choices": [{"v": "2026-10-16", "label": "2026-10-16"}]},
            ],
            "views": {"main": [{"key": "symbol", "label": "Symbol"}, {"key": "volume", "label": "Volume",
                                                                       "fmt": "int"}],
                      "greeks": {"label": "Greeks", "columns": ["delta"]}},
        }

    def run(self, key, payload, *, page=1, per_page=100):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(("run", key, payload, page, per_page))
        return {"screen": key, "total": 2, "page": page, "pages": 1,
                "rows": [{"symbol": "AAPL", "volume": 1200, "iv": float("nan"),
                          "legs": [{"side": "sell", "right": "P", "strike": 95, "price": 1.2},
                                   {"side": "buy", "right": "P", "strike": 90, "price": 0.4}]},
                         {"symbol": "MSFT", "volume": 800, "iv": float("inf")}],
                "columns": [{"key": "symbol", "label": "Symbol"}, {"key": "volume", "label": "Volume"}],
                "truncated": False, "ms": 4, "data": {"empty": False, "n_contracts": 10},
                "warnings": ["unknown field 'zzz' ignored"]}

    def csv(self, key, payload, *, limit=1000):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(("csv", key, payload, limit))
        return "Symbol,Volume\r\nAAPL,1200\r\n"

    def frame_meta(self):
        return {"pass_id": 7, "n_underlyings": 4512, "n_contracts": 1_020_000, "empty": False}


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """The collector's status file the page falls back to - a temp path (absent unless a test
    writes it), never this PC's real dashboard_tst/state/screener_collector.json."""
    p = tmp_path / "state" / "screener_collector.json"
    monkeypatch.setattr(options_page, "STATE_PATH", p)
    return p


@pytest.fixture
def fake(monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(options_page, "_load_engine", lambda: (eng, None))
    return eng


@pytest.fixture
def no_screener_db(tmp_path):
    """Aim the screener DB at a file that does not exist (restored after)."""
    from app import screener_db

    screener_db.configure("sqlite:///" + (tmp_path / "absent" / "screener.db").as_posix())
    yield tmp_path / "absent" / "screener.db"
    screener_db.configure(None)


@pytest.fixture
def screener_url(tmp_path):
    """An empty screener DB with the scr_* tables (created from the models, test-only)."""
    from app import screener_db
    from app.screener_models import ScrBase

    url = "sqlite:///" + (tmp_path / "screener.db").as_posix()
    eng = sa.create_engine(url, future=True)
    ScrBase.metadata.create_all(eng)
    eng.dispose()
    screener_db.configure(url)
    yield url
    screener_db.configure(None)


def _client(engine, uid):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        return s.get(models.User, uid)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(engine, user, no_screener_db):
    try:
        yield _client(engine, user.id)
    finally:
        app.dependency_overrides.clear()


def _boot(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="scrBoot">(.*?)</script>', html, re.S)
    assert m, "no boot block"
    return json.loads(m.group(1))


def _no_spec(*_a, **_k):
    raise AssertionError("the page built a screener spec")


# ───────────────────────────────────────── the page ─────────────────────────────────────────

def test_the_page_is_the_volatility_surface_only(client, fake, monkeypatch):
    """v4.159 (user: "remove the Set filter, result and Contract History. We only have the
    Volatility Surface"): /options shows the surface and nothing of the screener - no Go-to
    picker, no tabs, no filter / results panels, no contract history on the page."""
    monkeypatch.setattr(fake, "spec", _no_spec)
    r = client.get("/options")
    assert r.status_code == 200
    html = r.text
    assert 'id="optPage"' in html and 'data-mode="surface"' in html
    for needle in ('id="volPanelSurface"', 'id="volForm"', 'id="volSym"', 'id="volDay"', 'id="volTable"',
                   'id="volPop"', ">Volatility surface</h1>"):
        assert needle in html, needle
    # v4.171: the one tab list is the surface's own (Volatility Surface / Interpolated) - no screener tabs
    assert html.count('role="tablist"') == 1 and 'id="volTabs" class="vol-tabs shrink-0" role="tablist"' in html
    for gone in ('id="scrGoto"', 'class="scr-tab"', "data-tab=", 'id="scrPanelFilters"',
                 'id="scrPanelResults"', 'id="scrNf"', 'id="scrNr"', "Set filters", "Add a filter", "See results",
                 'id="volPanelHistory"', 'id="chForm"', "<optgroup", "optShowTab", 'id="scrEngineErr"',
                 # the old blank page and the v2 page are gone too
                 "optBasket", "optStatus", "/options/rules", "/options/basket", "/options/payoff"):
        assert gone not in html, gone
    assert "Massive · 15-min delayed · prices estimated from IV" in html      # the status line, behind the dot
    boot = _boot(html)
    assert boot["mode"] == "surface" and boot["engine_ok"] is True
    for gone in ("spec", "screen", "saved", "default_id", "chart_url", "per_page"):
        assert gone not in boot, gone


def test_the_page_never_runs_a_screen_on_open(client, fake, monkeypatch):
    """v4.159: opening the page no longer screens the market - no /options/api/run call is in
    the page, and the engine is never asked to run or to build a spec."""
    monkeypatch.setattr(fake, "spec", _no_spec)
    html = client.get("/options").text
    assert "/options/api/run" not in html and "/options/api/csv" not in html and "run(1)" not in html
    assert not [c for c in fake.calls if c[0] in ("run", "csv")]


def test_an_old_screen_link_still_opens_the_surface(client, fake, monkeypatch):
    """A bookmarked /options?screen=... still answers - with the surface; the screen is ignored."""
    monkeypatch.setattr(fake, "spec", _no_spec)
    for q in ("bull-put-spread", "nope", "OPTIONS-SCREENER<script>"):
        r = client.get("/options", params={"screen": q})
        assert r.status_code == 200 and _boot(r.text)["mode"] == "surface"
        assert "OPTIONS-SCREENER<script>" not in r.text


def test_saved_screeners_stay_in_the_api(client, fake, db, user):
    """The page no longer loads saved screeners, but the saved-screener API keeps them."""
    row = models.OptionScreen(user_id=user.id, screen_key="long-call", name="Mine",
                              payload={"filters": [{"f": "dte", "op": "lte", "hi": 30}]}, is_default=True)
    db.add(row)
    db.commit()
    items = client.get("/options/api/saved?screen=long-call").json()["items"]
    assert items[0]["id"] == row.id and items[0]["is_default"] is True
    assert items[0]["payload"]["filters"] == [{"f": "dte", "op": "lte", "hi": 30}]
    assert "saved" not in _boot(client.get("/options").text)


# ───────────────────────────────────────── the engine API ─────────────────────────────────────────

def test_screens_and_spec(client, fake, db, user):
    j = client.get("/options/api/screens").json()
    assert j["ok"] and len(j["screens"]) == 33 and len(j["families"]) == len(FAMILIES)
    assert j["screens"][0] == {"key": "options-screener", "label": "Options Screener",
                               "family": "Single options", "desc": "Options Screener - test blurb",
                               "strategy": False}
    assert j["screens"][3]["strategy"] is True
    db.add(models.OptionScreen(user_id=user.id, screen_key="naked-put", name="A", payload={}))
    db.commit()
    s = client.get("/options/api/spec?screen=naked-put").json()
    assert s["ok"] and s["screen"]["label"] == "Naked Put" and [i["name"] for i in s["saved"]] == ["A"]
    r = client.get("/options/api/spec?screen=nope")
    assert r.status_code == 404 and r.json()["ok"] is False


def test_run_cleans_the_payload_and_returns_json_safe_rows(client, fake):
    body = {"screen": "bull-put-spread", "page": 2, "per_page": 9999,
            "payload": {"filters": [{"f": "dte", "op": "between", "lo": 0, "hi": 45, "junk": 1},
                                    {"f": "right", "op": "in", "v": ["P"]},
                                    {"f": ""}, "not-a-dict"],
                        "sort": {"col": "volume", "dir": "asc"}, "view": "greeks", "flag_earnings": 1,
                        "extra": "dropped"}}
    r = client.post("/options/api/run", json=body)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["total"] == 2 and j["rows"][0]["iv"] is None and j["rows"][1]["iv"] is None
    assert j["warnings"] == ["unknown field 'zzz' ignored"]
    _, key, payload, page, per_page = fake.calls[-1]
    assert key == "bull-put-spread" and page == 2 and per_page == options_page.MAX_PER_PAGE
    assert payload == {"filters": [{"f": "dte", "op": "between", "lo": 0, "hi": 45},
                                   {"f": "right", "op": "in", "v": ["P"]}],
                       "sort": {"col": "volume", "dir": "asc"}, "view": "greeks", "flag_earnings": True}


def test_run_rejects_bad_requests_with_plain_errors(client, fake):
    too_many = {"filters": [{"f": "dte", "op": "gte", "lo": i} for i in range(61)]}
    r = client.post("/options/api/run", json={"screen": "long-call", "payload": too_many})
    assert r.status_code == 400 and "at most 60" in r.json()["error"]
    r = client.post("/options/api/run", json={"screen": "nope", "payload": {}})
    assert r.status_code == 404 and r.json()["code"] == "unknown_screen"
    r = client.post("/options/api/run", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["ok"] is False
    r = client.post("/options/api/run", json={"screen": "long-call", "payload": ["x"]})
    assert r.status_code == 400
    fake.fail = True
    r = client.post("/options/api/run", json={"screen": "long-call", "payload": {}})
    assert r.status_code == 500 and r.json()["code"] == "engine_failed"


def test_csv_download_json_and_form(client, fake):
    r = client.post("/options/api/csv", json={"screen": "covered-call", "payload": {"filters": []},
                                              "limit": 50_000})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    from app.services import clock
    assert r.headers["content-disposition"] == \
        f'attachment; filename="tradehunter-covered-call-{clock.et_today()}.csv"'
    assert r.text.startswith("Symbol,Volume")
    assert fake.calls[-1] == ("csv", "covered-call", {"filters": [], "view": "main", "flag_earnings": False},
                              options_page.CSV_LIMIT)
    r = client.post("/options/api/csv", data={"screen": "long-put", "limit": "10",
                                              "payload": json.dumps({"filters": [{"f": "dte", "op": "lte", "hi": 9}]})})
    assert r.status_code == 200 and fake.calls[-1][1] == "long-put" and fake.calls[-1][3] == 10
    assert fake.calls[-1][2]["filters"] == [{"f": "dte", "op": "lte", "hi": 9}]
    fake.fail = True
    assert client.post("/options/api/csv", json={"screen": "long-put", "payload": {}}).status_code == 500


def test_the_api_paths_win_over_the_legacy_symbol_catch_all(client, fake):
    paths = [getattr(r, "path", "") for r in app.routes]
    assert paths.index("/options/api/status") < paths.index("/options/{symbol}")
    r = client.get("/options/api/status")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")


# ───────────────────────────────────────── status ─────────────────────────────────────────

def test_status_without_a_screener_db_does_not_fail(client, fake, no_screener_db):
    j = client.get("/options/api/status").json()
    assert j["ok"] and j["dot"] == "slate" and "not started" in j["label"]
    assert j["engine_ok"] is True and j["frame"]["n_contracts"] == 1_020_000
    assert "4,512 underlyings" in j["line"] and "1.02M contracts" in j["line"]
    assert not no_screener_db.exists()          # never creates the file


def test_status_reads_the_collector_heartbeat(client, fake, screener_url):
    from app import screener_db
    from app.screener_models import ScrPass, ScrStatus

    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    detail = ("Reading the option market (live, 15-min delayed): 1,200 of 4,512 underlyings, 31% of "
              "contracts, about 6 min left.")
    with screener_db.session() as s:
        s.add(ScrStatus(id=1, state="pass", detail=detail, heartbeat=now, symbols_total=4512,
                        symbols_done=1200, universe_n=4512, history_done_n=1204, history_total=4512,
                        progress={"pass_pct": 31, "pass_eta_s": 360}))
        s.add(ScrPass(kind="cycle", session="2026-10-09", started=now - _dt.timedelta(minutes=30),
                      finished=now - _dt.timedelta(minutes=20), n_symbols=4500, n_ok=4490, n_contracts=990_000))
        s.add(ScrPass(kind="cycle", session="2026-10-09", started=now - _dt.timedelta(minutes=5)))
        s.commit()
    j = client.get("/options/api/status").json()
    assert j["dot"] == "green" and j["label"] == "Collector: " + detail and j["alert"] == j["label"]
    # the fake frame (pass 7) carries no finish time: the line names the last finished pass
    assert "· data: " in j["line"] and " ET · " in j["line"]
    assert "IV rank: building (1,204 / 4,512)" in j["line"]
    assert j["last_pass"]["n_contracts"] == 990_000 and j["last_pass"]["finished"].endswith("Z")
    assert j["running_pass"]["id"] == j["last_pass"]["id"] + 1
    assert "1.02M contracts" in j["line"]             # the loaded frame's count wins over the pass's
    assert j["heartbeat"].endswith("Z") and j["heartbeat_age_s"] < 120
    assert j["source"] == "db" and j["collector"]["progress"] == {"pass_pct": 31, "pass_eta_s": 360}
    assert j["empty"] is None                         # data on screen, a pass finished: no panel


TUE_NOON = _dt.datetime(2026, 10, 6, 16, 0)        # 12:00 ET, a trading day (naive UTC)
SUN_NOON = _dt.datetime(2026, 10, 4, 16, 0)        # a Sunday
MON_NIGHT = _dt.datetime(2026, 10, 6, 2, 0)        # Mon 22:00 ET, a weeknight
SAT_NOON = _dt.datetime(2026, 10, 10, 16, 20)      # Sat 12:20 ET
MIN = _dt.timedelta(minutes=1)


def _col(last=None, running=None, **st):
    st.setdefault("state", "idle")
    return {"available": True, "source": "db", "status": st, "last_pass": last, "running_pass": running}


def test_status_dot_rules():
    v = options_page._status_view
    assert options_page.STALE_S == 300 and not hasattr(options_page, "STALE_IDLE_S")
    assert v({}, now=TUE_NOON)["dot"] == "slate"
    assert v(_col(heartbeat=TUE_NOON - 2 * MIN), now=TUE_NOON)["dot"] == "green"
    assert v(_col(heartbeat=TUE_NOON - 6 * MIN), now=TUE_NOON)["dot"] == "amber"
    assert v(_col(heartbeat=SUN_NOON - 2 * MIN), now=SUN_NOON)["dot"] == "green"
    assert v(_col(heartbeat=SUN_NOON - 40 * MIN), now=SUN_NOON)["dot"] == "amber"   # 5 min at any hour
    assert v(_col(heartbeat=MON_NIGHT - 90 * MIN), now=MON_NIGHT)["dot"] == "amber"
    assert v(_col(state="stopped", heartbeat=TUE_NOON), now=TUE_NOON)["dot"] == "amber"
    err = v(_col(state="error", last_error="plan has no options", heartbeat=TUE_NOON), now=TUE_NOON)
    assert err["dot"] == "rose" and err["label"] == "The market collector hit an unexpected problem."
    assert v({}, now=TUE_NOON)["line"].startswith("Massive · 15-min delayed · prices estimated from IV · no market pass yet")
    stale = v(_col(state="stocks", heartbeat=TUE_NOON - 120 * MIN), now=TUE_NOON)
    assert stale["label"] == ("The collector has not reported for 2 h (it was loading daily stock prices) - it has "
                              "probably stopped, so the data is not being refreshed.")


def test_working_label_is_the_collectors_own_detail():
    v = options_page._status_view
    d = ("Step 1 of 2: reading Massive's list of optionable stocks - page 312 (1,840 stocks so far, 4 min). "
         "Results start appearing as soon as the first stocks are read.")
    j = v(_col(state="universe", detail=d, heartbeat=TUE_NOON), now=TUE_NOON)
    assert j["dot"] == "green" and j["label"] == "Collector: " + d and j["alert"] == j["label"]
    assert v(_col(state="universe", heartbeat=TUE_NOON), now=TUE_NOON)["label"] == \
        "Collector refreshing the list of optionable stocks."
    # idle with no universe yet: the collector's words, never "waiting for the next market pass"
    j = v(_col(detail="Waiting for the list of optionable stocks.", heartbeat=TUE_NOON), now=TUE_NOON)
    assert j["dot"] == "green" and j["label"] == "Collector: Waiting for the list of optionable stocks."
    assert len(v(_col(state="stocks", detail="x" * 900, heartbeat=TUE_NOON), now=TUE_NOON)["label"]) == 240
    # jobs running at once are joined (" · ") and often pass the cap: cut at a word, with "…"
    joined = ("Reading the option market (Fri Oct 9 close): 2 of 3 underlyings, 89% of contracts, about 1 min "
              "left. · " + d)
    lab = v(_col(state="pass", detail=joined, heartbeat=TUE_NOON), now=TUE_NOON)["label"]
    assert len(lab) <= 240 and lab.endswith("…") and ("Collector: " + joined).startswith(lab[:-1])
    assert ("Collector: " + joined)[len(lab) - 1] == " "                 # never mid-word
    # the engine's problem rides on the label, never on the alert
    j = v(_col(state="pass", detail="Reading.", heartbeat=TUE_NOON), now=TUE_NOON, engine_error="numpy missing")
    assert j["label"] == "Collector: Reading. numpy missing" and j["alert"] == "Collector: Reading."


def test_error_label_uses_the_detail_and_the_next_try():
    v = options_page._status_view
    st = dict(state="error", error_kind="network", heartbeat=TUE_NOON, next_try=TUE_NOON + 5 * MIN,
              detail="could not reach Massive for the option chain (ConnectError: down); next try 12:05 ET",
              last_error="earnings: Nasdaq answered HTTP 500")
    j = v(_col(**st), now=TUE_NOON)
    assert j["dot"] == "rose" and j["error_kind"] == "network" and j["next_try"] == "2026-10-06T16:05:00Z"
    assert j["label"] == "The server cannot reach the market data feed right now. Retrying at 12:05 ET (in 5 min)."
    # an older collector's row (no error_kind / next_try): read from the detail's own words
    j = v(_col(**dict(st, error_kind=None, next_try=None)), now=TUE_NOON)
    assert j["error_kind"] == "network"
    assert j["label"] == "The server cannot reach the market data feed right now. Retrying at 12:05 ET."
    assert v(_col(**dict(st, next_try=TUE_NOON - MIN)), now=TUE_NOON)["label"].endswith(" Retrying now.")
    for kind, text in (("config", "The server is not connected to the market data feed yet."),
                       ("auth", "The market data feed rejected the server's access key."),
                       ("plan", "The server's data subscription does not cover part of the feed."),
                       ("empty", "The last market pass read no option data."),
                       ("http", "The market collector hit an unexpected problem."),
                       ("rate", "The market collector hit an unexpected problem.")):
        j = v(_col(state="error", error_kind=kind, detail="x", heartbeat=TUE_NOON), now=TUE_NOON)
        assert j["label"] == text and j["dot"] == "rose", kind


def test_a_plan_pause_keeps_its_reason_over_a_later_failure():
    v = options_page._status_view
    st = dict(state="error", heartbeat=TUE_NOON,
              detail="your Massive plan does not include the option chain snapshot (HTTP 403): Not entitled - "
                     "market passes paused, the rest carries on; next try 12:30 ET",
              last_error="earnings: could not read Nasdaq's calendar (ReadTimeout)")
    j = v(_col(**st), now=TUE_NOON)              # an older row: no error_kind, read from the detail
    assert j["dot"] == "amber" and j["error_kind"] == "plan"
    assert j["label"] == ("Part of the data (the option market reads) is paused; the rest keeps updating. "
                          "Retrying at 12:30 ET.")
    assert "Nasdaq" not in j["label"]
    j = v(_col(**dict(st, error_kind="plan", next_try=TUE_NOON + 30 * MIN)), now=TUE_NOON)
    assert j["label"].endswith("Retrying at 12:30 ET (in 30 min).") and j["dot"] == "amber"


def test_scope_words_match_the_collectors_and_a_dash_in_the_reason_is_not_the_scope():
    from app.services import scr_collector

    assert set(options_page._SCOPE_WORDS) == set(scr_collector._OP_WORDS.values())
    st = dict(state="error", error_kind="plan", heartbeat=TUE_NOON,
              detail="your Massive plan does not include the grouped daily bars (HTTP 403): Not entitled - "
                     "upgrade at massive.com - stock bars and reference reads paused, the rest carries on; "
                     "next try 12:30 ET")
    j = options_page._status_view(_col(**st), now=TUE_NOON)
    assert j["dot"] == "amber" and j["label"].startswith(
        "Part of the data (stock prices and names) is paused; the rest keeps updating.")


def test_member_and_admin_texts():
    v = options_page._status_view
    st = dict(state="error", error_kind="config", heartbeat=TUE_NOON, next_try=TUE_NOON + 4 * MIN,
              detail="TST_MASSIVE_API_KEY is not set on this PC; next try 12:04 ET")
    member = v(_col(**st), now=TUE_NOON)
    admin = v(_col(**st), now=TUE_NOON, is_admin=True)
    assert member["label"] == ("The server is not connected to the market data feed yet. Retrying at 12:04 ET "
                               "(in 4 min).")
    assert "Admin" not in member["label"] and "this PC" not in member["label"]
    assert admin["label"].startswith(member["label"] + " Admin: add TST_MASSIVE_API_KEY to app\\.env on Hermes "
                                                       "(re-read within 5 min).")
    assert "Details: TST_MASSIVE_API_KEY is not set on this PC" in admin["label"]
    assert member["empty"]["admin"] == "" and admin["empty"]["admin"].startswith("Admin: add TST_MASSIVE_API_KEY")
    for kind, hint in (("auth", "Admin: fix the key in app\\.env on Hermes (re-read within 5 min)."),
                       ("plan", "Admin: check the Massive subscription."),
                       ("network", "Admin: check Hermes' internet / DNS."),
                       ("http", "Admin: see logs\\screener_collector.log on Hermes.")):
        j = v(_col(state="error", error_kind=kind, detail="raw", heartbeat=TUE_NOON), now=TUE_NOON, is_admin=True)
        assert f" {hint} Details: raw" in j["label"], kind


def test_a_paused_pass_shows_where_it_stopped():
    v = options_page._status_view
    running = {"id": 5, "kind": "eod", "session": "2026-10-09", "started": TUE_NOON - 10 * MIN, "n_symbols": 4512}
    st = dict(state="error", error_kind="auth", heartbeat=TUE_NOON, next_try=TUE_NOON + 5 * MIN, pass_id=5,
              symbols_done=325, symbols_total=4512, universe_n=4512,
              detail="Massive rejected the API key (HTTP 401); next try 12:05 ET")
    rows = {"pass_id": None, "n_underlyings": 300, "n_contracts": 50_000, "empty": False}
    j = v(_col(running=running, **st), rows, now=TUE_NOON)
    assert j["label"] == ("The market data feed rejected the server's access key. Retrying at 12:05 ET (in 5 min). "
                          "Pass paused at 325 / 4,512 underlyings; results already loaded stay on screen.")
    j = v(_col(running=running, **st), {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert j["label"].endswith(" Pass paused at 325 / 4,512 underlyings.")
    assert j["empty"]["title"] == "Reading the option market is paused at 325 of 4,512 underlyings."
    assert j["empty"]["body"] == ("The market data feed rejected the server's access key. It resumes where it "
                                  "stopped at 12:05 ET (in 5 min).")
    # pass_paused_at is a TIME (the collector's "paused since"): the count is symbols_done
    http = dict(st, error_kind="http", symbols_done=320, detail="Massive answered HTTP 502 for the option chain",
                progress={"pass_paused_at": "2026-10-06T16:00:00+00:00"})
    j = v(_col(running=running, **http), rows, now=TUE_NOON)
    assert "Pass paused at 320 / 4,512 underlyings" in j["label"]
    # an idle collector reports its LAST pass's counts: no "paused at" then
    idle = dict(st, state="idle", error_kind=None, detail="Up to date.", next_try=None)
    assert "paused" not in v(_col(**idle), rows, now=TUE_NOON)["label"]


@pytest.mark.parametrize("kind", ["http", "plan"])
def test_a_universe_alert_during_the_first_pass_is_not_a_pause(kind):
    """A first start: one page of the list failed (the walk resumes in 60 s) while the pass
    reads the stocks filed so far - the pass is NOT paused, the panel shows it reading."""
    v = options_page._status_view
    running = {"id": 1, "kind": "eod", "session": "2026-10-09", "started": TUE_NOON - 5 * MIN, "n_symbols": 2}
    st = dict(state="error", error_kind=kind, heartbeat=TUE_NOON, next_try=TUE_NOON + MIN, pass_id=1,
              symbols_done=0, symbols_total=2, universe_n=2, progress={"pass_paused_at": None},
              detail="universe: Massive answered HTTP 502 for the options contracts list - nothing can be "
                     "screened until the list of optionable stocks is read; next try 12:01 ET")
    j = v(_col(running=running, **st), {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert "paused" not in j["label"].lower()
    assert j["empty"]["title"] == "Step 2 of 2: reading the option market (Fri Oct 9 close)."
    assert j["empty"]["body"].startswith("0 of 2 underlyings read.")
    # the collector's newer wording (stocks filed: "the universe refresh paused, the rest carries on")
    st["detail"] = ("universe: Massive answered HTTP 502 for the options contracts list - the universe refresh "
                    "paused, the rest carries on; next try 12:01 ET")
    j = v(_col(running=running, **st), {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert "paused at" not in j["label"] and not j["empty"]["title"].startswith("Reading the option market is paused")


def test_an_empty_list_of_optionable_stocks_is_not_called_an_empty_pass():
    from app.services import scr_collector

    v = options_page._status_view
    st = dict(state="error", error_kind="empty", heartbeat=TUE_NOON, next_try=TUE_NOON + 15 * MIN, universe_n=0,
              detail=scr_collector.UNIVERSE_EMPTY_TEXT + scr_collector.NO_UNIVERSE_SUFFIX + "; next try 12:15 ET",
              last_error=scr_collector.UNIVERSE_EMPTY_TEXT)
    j = v(_col(**st), {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert j["dot"] == "rose" and "market pass" not in j["label"]
    assert j["label"] == ("The market data feed returned an empty list of optionable stocks. Retrying at 12:15 ET "
                          "(in 15 min).")
    assert j["empty"]["title"] == "No option data yet - the list of optionable stocks could not be read."
    # an older collector's row (no error_kind): the same words from its text
    j = v(_col(**dict(st, error_kind=None)), {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert j["error_kind"] == "empty" and j["label"].startswith("The market data feed returned an empty list")
    # with a pass reading the stocks filed so far: the panel shows the pass, not "the last pass read nothing"
    running = {"id": 1, "kind": "eod", "session": "2026-10-09", "n_symbols": 2}
    j = v(_col(running=running, **dict(st, pass_id=1, symbols_done=1, symbols_total=2, universe_n=2)),
          {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert j["label"].startswith("The market data feed returned an empty list") and "paused" not in j["label"]
    assert j["empty"]["title"] == "Step 2 of 2: reading the option market (Fri Oct 9 close)."
    # an empty MARKET PASS keeps T-47
    j = v(_col(state="error", error_kind="empty", heartbeat=TUE_NOON, universe_n=3,
               detail="eod pass 3 read no option data: Massive returned no contracts (3 of 3 underlyings) - "
                      "market passes paused, the rest carries on"), now=TUE_NOON)
    assert j["label"] == "The last market pass read no option data."


def test_the_data_line_skips_a_pass_that_read_nothing():
    """v4.136 left a finished EOD pass that read nothing; the first real pass runs now. The
    line names neither the empty pass's session nor its finish - it says the first pass runs."""
    v = options_page._status_view
    head = "Massive · 15-min delayed · prices estimated from IV · "
    zero = {"id": 1, "kind": "eod", "session": "2026-10-09", "finished": TUE_NOON - 60 * MIN, "n_symbols": 4512,
            "n_ok": 0, "n_contracts": 0}
    running = {"id": 2, "kind": "eod", "session": "2026-10-09", "n_symbols": 4512}
    st = dict(state="pass", pass_id=2, symbols_done=1240, symbols_total=4512, universe_n=4512, heartbeat=TUE_NOON)
    meta = {"pass_id": 1, "pass_kind": "eod", "session": "2026-10-09", "finished": "2026-10-06T15:00:00Z",
            "pass_contracts": 0, "real_pass_id": None, "real_pass_kind": None, "real_session": None,
            "real_finished": None, "n_contracts": 52_000, "n_underlyings": 1200, "empty": False}
    col = dict(_col(last=zero, running=running, **st), real_pass=None)
    j = v(col, meta, now=TUE_NOON)
    assert j["line"] == head + "first pass: 1,240 / 4,512 read · 1,200 underlyings · 52,000 contracts"
    assert v(col, None, now=TUE_NOON)["line"].startswith(head + "first pass: 1,240 / 4,512 read")
    # an older real pass under the newer empty one: the data comes from the real one
    real = dict(zero, id=0, session="2026-10-08", finished=_dt.datetime(2026, 10, 5, 20, 30), n_ok=4500,
                n_contracts=990_000)
    meta2 = dict(meta, real_pass_id=0, real_pass_kind="eod", real_session="2026-10-08",
                 real_finished="2026-10-05T20:30:00Z")
    col2 = dict(_col(last=zero, **dict(st, state="idle")), real_pass=real)
    assert "· data: Thu Oct 8 close (read Mon 16:30 ET) ·" in v(col2, meta2, now=TUE_NOON)["line"]
    assert "· data: Thu Oct 8 close (read Mon 16:30 ET)" in v(col2, None, now=TUE_NOON)["line"]


def test_the_page_reads_the_frame_once_and_builds_no_spec(client, fake, monkeypatch):
    """An empty frame makes each frame read wait up to 3 s for a reload: the page (v4.159: the
    surface only) builds no screener spec and reads the frame summary exactly once."""
    reads = []

    def one_read():
        reads.append(1)
        return {"n_contracts": 0, "empty": True, "reloading": True}

    monkeypatch.setattr(fake, "spec", _no_spec)
    monkeypatch.setattr(fake, "frame_meta", one_read)
    boot = _boot(client.get("/options").text)
    assert len(reads) == 1
    assert boot["status"]["frame"] == {"n_contracts": 0, "empty": True, "reloading": True}
    assert boot["status"]["empty"]["title"]                  # the panel is there on the first paint


def test_the_crash_file_shows_when_the_collector_could_not_start(client, fake, state_file):
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({
        "state": "error", "error_kind": "startup",
        "detail": "The collector could not start on the server: ModuleNotFoundError: No module named 'numpy'. "
                  "- see logs\\screener_collector.log",
        "last_error": "ModuleNotFoundError: No module named 'numpy'",
        "heartbeat": "2026-10-10T04:00:00+00:00", "pid": 4242, "source": "massive",
        "written_by": "dashboard_tst/deploy/screener_collector.py"}), encoding="utf-8")
    j = client.get("/options/api/status").json()
    assert j["source"] == "file" and j["dot"] == "rose"
    assert j["alert"] == "The collector could not start on the server: ModuleNotFoundError: No module named 'numpy'."
    assert j["empty"]["title"] == "No option data yet - the market data collector could not start."
    assert j["empty"]["body"] == j["alert"] and j["empty"]["admin"] == ""
    html = client.get("/options").text
    m = re.search(r'<div id="scrCollectorAlert"([^>]*)>(.*?)</div>', html, re.S)
    assert m and "hidden" not in m.group(1) and "scr-alert-rose" in m.group(1)
    assert "could not start on the server" in m.group(2)
    # the deploy script's own file, with no kind and only the detail's tail: still the start failure
    v = options_page._status_view(_col(state="error", written_by="dashboard_tst/deploy/screener_collector.py",
                                       last_error="init_screener_db failed: locked", heartbeat=TUE_NOON - 600 * MIN),
                                  now=TUE_NOON)
    assert v["dot"] == "rose" and v["alert"] == "The collector could not start on the server: init_screener_db failed: locked."


def test_a_newer_status_file_wins_over_an_older_db_row(client, fake, screener_url, state_file):
    from app import screener_db
    from app.screener_models import ScrStatus

    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    with screener_db.session() as s:
        s.add(ScrStatus(id=1, state="idle", detail="from the db", heartbeat=now - _dt.timedelta(hours=1)))
        s.commit()
    state_file.parent.mkdir(parents=True)

    def write(hb, detail):
        state_file.write_text(json.dumps({
            "state": "universe", "detail": detail, "heartbeat": hb.isoformat() + "+00:00",
            "progress": {"universe_pages": 3}, "written_by": "dashboard_tst/app/services/scr_collector.py"}),
            encoding="utf-8")

    write(now, "from the file")
    j = client.get("/options/api/status").json()
    assert j["source"] == "file" and j["state"] == "universe" and j["dot"] == "green"
    assert j["label"] == "Collector: from the file" and j["collector"]["progress"] == {"universe_pages": 3}
    write(now - _dt.timedelta(hours=2), "older file")
    j = client.get("/options/api/status").json()
    assert j["source"] == "db" and j["state"] == "idle" and j["dot"] == "amber"      # the db row, an hour old
    state_file.write_text("{not json", encoding="utf-8")
    assert client.get("/options/api/status").json()["source"] == "db"


def test_the_empty_panel_for_each_branch():
    v = options_page._status_view
    E = {"n_contracts": 0, "empty": True}

    def p(col, meta=E, **kw):
        return v(col, meta, now=TUE_NOON, **kw)["empty"]

    e = p({})                                                                    # no status row
    assert e == {"title": "No option data yet - the market data collector is not running.",
                 "body": "TradeHunter reads the whole US option market from Massive with a collector on the "
                         "server. It has never reported in, so there is nothing to screen yet.", "admin": ""}
    assert p({}, is_admin=True)["admin"] == (
        "Hermes: powershell -ExecutionPolicy Bypass -File deploy\\setup_screener_task.ps1 -StartNow, then read "
        "logs\\screener_collector.log.")
    stale = _col(state="pass", heartbeat=TUE_NOON - 120 * MIN)                  # stale
    assert p(stale)["title"] == ("No option data yet - the collector stopped reporting 2 h ago (it was reading the "
                                 "option market).")
    assert p(stale, is_admin=True)["admin"] == "Hermes: Start-ScheduledTask -TaskName TST-Options-Screener."
    prog = {"universe_pages": 312, "universe_symbols": 1840,                    # universe running
            "universe_started": (TUE_NOON - 4 * MIN).isoformat() + "+00:00"}
    e = p(_col(state="universe", heartbeat=TUE_NOON, progress=prog))
    assert e["title"] == "Step 1 of 2: reading the list of optionable stocks."
    assert e["body"] == ("Page 312 of Massive's option contract list (1,840 stocks so far, started 4 min ago). "
                         "Results appear here as soon as the first stocks' option chains are read; this page "
                         "refreshes by itself.")
    assert p(_col(state="universe", heartbeat=TUE_NOON))["body"].startswith("Reading Massive's option contract list")
    e = p(_col(state="error", error_kind="http", heartbeat=TUE_NOON, next_try=TUE_NOON + MIN,   # universe error
               detail="universe: Massive answered HTTP 502 for the options contracts list - the universe refresh "
                      "paused, the rest carries on; next try 12:01 ET", progress={"universe_pages": 40}))
    assert e["title"] == "No option data yet - the list of optionable stocks could not be read."
    assert e["body"] == ("The market collector hit an unexpected problem. The collector tries again at 12:01 ET "
                         "(in 1 min), continuing from page 41.")
    e = p(_col(state="error", error_kind="empty", heartbeat=TUE_NOON, detail="Massive returned an empty options "
               "list (/v3/reference/options/contracts) - the universe refresh paused, the rest carries on"))
    assert e["body"] == "Massive returned an empty options list. The collector tries again by itself."
    running = {"id": 1, "kind": "eod", "session": "2026-10-09", "n_symbols": 4512}
    e = p(_col(running=running, state="pass", pass_id=1, symbols_done=1240, symbols_total=4512,   # pass running
               universe_n=4512, heartbeat=TUE_NOON))
    assert e["title"] == "Step 2 of 2: reading the option market (Fri Oct 9 close)."
    assert e["body"] == ("1,240 of 4,512 underlyings read. Results appear within a minute and grow every couple of "
                         "minutes as more are read; this page refreshes by itself.")
    cyc = dict(running, kind="cycle", session="2026-10-06")
    assert p(_col(running=cyc, state="pass", pass_id=1, symbols_total=4512, universe_n=4512,
                  heartbeat=TUE_NOON))["title"] == "Step 2 of 2: reading the option market (live, 15-min delayed)."
    e = p(_col(running=running, state="error", error_kind="network", pass_id=1, symbols_done=325,  # paused
               symbols_total=4512, universe_n=4512, heartbeat=TUE_NOON, next_try=TUE_NOON + 5 * MIN,
               detail="could not reach Massive for the option chain; next try 12:05 ET"))
    assert e["title"] == "Reading the option market is paused at 325 of 4,512 underlyings."
    assert e["body"] == ("The server cannot reach the market data feed right now. It resumes where it stopped at "
                         "12:05 ET (in 5 min).")
    last0 = {"id": 3, "kind": "eod", "session": "2026-10-09", "finished": TUE_NOON - 30 * MIN,     # an empty pass
             "n_symbols": 4512, "n_ok": 0, "n_contracts": 0}
    e = p(_col(last=last0, state="error", error_kind="empty", universe_n=4512, heartbeat=TUE_NOON,
               next_try=TUE_NOON + 10 * MIN,
               detail="eod pass 3 read no option data: Massive answered HTTP 502 for the option chain (4,512 of "
                      "4,512 underlyings) - market passes paused, the rest carries on; next try 12:10 ET"))
    assert e["title"] == "The last market pass (Fri Oct 9 close) read no option data."
    assert e["body"] == ("eod pass 3 read no option data: Massive answered HTTP 502 for the option chain (4,512 "
                         "of 4,512 underlyings). The collector tries again at 12:10 ET (in 10 min).")
    e = p(_col(last=last0, universe_n=4512, heartbeat=TUE_NOON, detail="Up to date."))           # ... idle
    assert e["title"] == "The last market pass (Fri Oct 9 close) read no option data."
    full = dict(last0, n_ok=4500, n_contracts=1_200_000)                                          # loading
    e = p(_col(last=full, universe_n=4512, heartbeat=TUE_NOON, detail="Up to date."),
          {"n_contracts": 0, "empty": True, "reloading": True})
    assert e is None      # v4.170: the surface reads the pass from the DB - no "loading into the screener" panel
    e = p(_col(universe_n=4512, heartbeat=TUE_NOON, detail="Waiting for the next market pass."))  # idle, nothing
    assert e == {"title": "No option data yet.", "body": "Collector: Waiting for the next market pass.", "admin": ""}
    # data on screen and a finished pass: no panel
    meta = {"pass_id": 3, "pass_kind": "eod", "session": "2026-10-09", "finished": "2026-10-06T15:30:00Z",
            "n_underlyings": 4500, "n_contracts": 1_200_000, "empty": False}
    assert v(_col(last=full, universe_n=4512, heartbeat=TUE_NOON), meta, now=TUE_NOON)["empty"] is None


def test_warnings_and_silent_failures_are_amber():
    v = options_page._status_view
    w = "Universe refresh failed 07:31 ET (Massive answered HTTP 502); next try 07:46 ET - yesterday's list in use."
    j = v(_col(state="pass", detail="Reading the option market.", warn=w, heartbeat=TUE_NOON, universe_n=4512),
          now=TUE_NOON)
    assert j["dot"] == "amber" and j["warn"] == w and j["label"] == "Collector: Reading the option market. " + w
    j = v(_col(detail="waiting for the universe", heartbeat=TUE_NOON,
               last_error="universe: Massive answered HTTP 502 for the options contracts list"), now=TUE_NOON)
    assert j["dot"] == "amber"
    assert j["label"] == ("Collector: waiting for the universe. The last attempt failed: universe: Massive answered "
                          "HTTP 502 for the options contracts list.")
    last0 = {"id": 3, "kind": "eod", "session": "2026-10-09", "finished": TUE_NOON - 30 * MIN, "n_contracts": 0}
    j = v(_col(last=last0, universe_n=4512, heartbeat=TUE_NOON, detail="Up to date."),
          {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert j["dot"] == "amber" and j["label"].endswith("The last market pass (Fri Oct 9 close) read no option data.")


def test_the_data_line_wording():
    v = options_page._status_view
    eod = {"id": 3, "kind": "eod", "session": "2026-10-09", "finished": _dt.datetime(2026, 10, 10, 16, 9),
           "n_symbols": 4512, "n_ok": 4500, "n_contracts": 1_020_000}
    meta = {"pass_id": 3, "pass_kind": "eod", "session": "2026-10-09", "finished": "2026-10-10T16:09:00Z",
            "n_underlyings": 4500, "n_contracts": 1_020_000, "empty": False}
    head = "Massive · 15-min delayed · prices estimated from IV · "
    col = _col(last=eod, heartbeat=SAT_NOON, universe_n=4512)
    j = v(col, meta, now=SAT_NOON)
    assert j["line"] == head + "data: Fri Oct 9 close (read Sat 12:09 ET) · 4,500 underlyings · 1.02M contracts"
    assert v(col, None, now=SAT_NOON)["line"] == j["line"]          # no engine: the same words from the pass
    # the frame is still empty though the pass stored rows: the pass's counts, never "0 contracts"
    assert v(col, {"n_contracts": 0, "n_underlyings": 0, "empty": True}, now=SAT_NOON)["line"] == j["line"]
    cyc = dict(eod, kind="cycle", session="2026-10-12", finished=_dt.datetime(2026, 10, 12, 15, 45))
    cmeta = dict(meta, pass_kind="cycle", session="2026-10-12", finished="2026-10-12T15:45:00Z")
    now = _dt.datetime(2026, 10, 12, 15, 50)
    assert "· data: Mon Oct 12 11:45 ET ·" in v(_col(last=cyc, heartbeat=now), cmeta, now=now)["line"]
    j = v(_col(universe_n=4512, heartbeat=TUE_NOON), {"n_contracts": 0, "empty": True}, now=TUE_NOON)
    assert j["line"] == head + "list of optionable stocks 4,512 · none read yet"
    running = {"id": 1, "kind": "eod", "session": "2026-10-09", "n_symbols": 4512}
    first = dict(state="pass", pass_id=1, symbols_done=1240, symbols_total=4512, universe_n=4512, heartbeat=TUE_NOON)
    j = v(_col(running=running, **first), {"n_contracts": 52_000, "n_underlyings": 1200, "empty": False,
                                           "pass_id": None}, now=TUE_NOON)
    assert j["line"] == head + "first pass: 1,240 / 4,512 read · 1,200 underlyings · 52,000 contracts"
    hist = dict(first, history_total=4512, history_done_n=0, progress={"stock_days_pending": 312})
    assert v(_col(running=running, **hist), None, now=TUE_NOON)["line"].endswith(
        "· IV history starts after the stock prices")
    hist.update(history_done_n=1204, progress={})
    assert v(_col(running=running, **hist), None, now=TUE_NOON)["line"].endswith("· IV rank: building (1,204 / 4,512)")
    hist.update(history_done_n=4512)
    assert v(_col(running=running, **hist), None, now=TUE_NOON)["line"].endswith("· IV history 4,512 / 4,512")


def test_the_day_file_iv_history_on_the_line_the_label_and_the_panel():
    """v4.138: the collector's flat lane (progress.flat_*) on the page - its progress on the
    data line and in the empty panel, Massive refusing the files as an amber warning (plain
    words for members, the Flat Files keys for admins); an older collector changes nothing."""
    v = options_page._status_view
    eod = {"id": 3, "kind": "eod", "session": "2026-10-09", "finished": _dt.datetime(2026, 10, 10, 16, 9),
           "n_symbols": 4512, "n_ok": 4500, "n_contracts": 1_020_000}
    meta = {"pass_id": 3, "pass_kind": "eod", "session": "2026-10-09", "finished": "2026-10-10T16:09:00Z",
            "n_underlyings": 4500, "n_contracts": 1_020_000, "empty": False}
    prog = {"flat_state": "building", "flat_days_done": 187, "flat_days_total": 252, "flat_eta_s": 360,
            "flat_error": None}
    st = dict(state="history", heartbeat=TUE_NOON, universe_n=4512, history_total=4512, history_done_n=1204,
              detail="Building IV history from Massive's daily files: 187 of 252 days, about 6 min left.",
              progress=prog)
    j = v(_col(last=eod, **st), meta, now=TUE_NOON)
    assert j["line"].endswith(" · 1.02M contracts · IV rank: building from daily files (187 / 252 days)")
    assert j["dot"] == "green" and j["label"] == "Collector: " + st["detail"]
    assert j["flat"] == {"state": "building", "done": 187, "total": 252, "error": None, "kind": None,
                         "building": True, "blocked": False}
    for state in ("waiting", "retry"):                       # waiting for stock prices / retrying: still building
        got = v(_col(last=eod, **dict(st, progress=dict(prog, flat_state=state))), meta, now=TUE_NOON)
        assert got["line"].endswith("IV rank: building from daily files (187 / 252 days)") and got["dot"] == "green"
    done = dict(prog, flat_state="ready", flat_days_done=252)                    # every day filed
    assert v(_col(last=eod, **dict(st, progress=done)), meta, now=TUE_NOON)["line"].endswith(
        "· IV rank: building (1,204 / 4,512)")
    for old in (None, {}, {"history_left": 3}, {"flat_state": "off"}):          # an older collector / the lane off
        got = v(_col(last=eod, **dict(st, progress=old)), meta, now=TUE_NOON)
        assert got["line"].endswith("· IV rank: building (1,204 / 4,512)") and got["dot"] == "green", old
    # Massive refused the files: amber, plain words for members, the keys + the reason for admins
    err = ("Massive did not accept the Flat Files keys for the file list for 2026-10 (HTTP 403, InvalidAccessKeyId) "
           "- copy the Access Key ID and the Secret Access Key again from massive.com/dashboard/keys into app\.env")
    bad = dict(st, state="idle", detail="Up to date with the Fri Oct 9 close; next market pass Mon Oct 12 09:45 ET.",
               progress={"flat_state": "error", "flat_error": err, "flat_days_done": 40, "flat_days_total": 252})
    m = v(_col(last=eod, **bad), meta, now=TUE_NOON)
    a = v(_col(last=eod, **bad), meta, now=TUE_NOON, is_admin=True)
    assert m["dot"] == a["dot"] == "amber" and m["alert"] == m["label"]
    assert m["label"] == "Collector: " + bad["detail"] + " " + options_page._FLAT_PAUSED
    assert "TST_MASSIVE_S3" not in m["label"] and "Admin" not in m["label"] and "403" not in m["label"]
    assert a["label"].startswith(m["label"] + " Admin: check TST_MASSIVE_S3_KEY_ID and TST_MASSIVE_S3_SECRET in "
                                              "app\.env on Hermes")
    assert a["label"].endswith(" Details: " + err[:300])
    assert m["line"].endswith("· IV rank: building (1,204 / 4,512)")       # (no "building from daily files")
    # the collector's own error wins over the files' warning
    e = v(_col(**dict(bad, state="error", error_kind="network", detail="could not reach Massive")), now=TUE_NOON)
    assert e["dot"] == "rose" and options_page._FLAT_PAUSED not in e["label"]
    # the empty panel says the same
    E = {"n_contracts": 0, "empty": True}
    e = v(_col(**dict(st, state="idle", detail="Waiting for the next market pass.")), E, now=TUE_NOON)["empty"]
    assert e == {"title": "No option data yet.", "admin": "",
                 "body": "Collector: Waiting for the next market pass. IV rank: building from daily files (187 / "
                         "252 days)."}
    running = {"id": 1, "kind": "eod", "session": "2026-10-09", "n_symbols": 4512}
    e = v(_col(running=running, state="pass", pass_id=1, symbols_done=1240, symbols_total=4512, universe_n=4512,
               heartbeat=TUE_NOON, progress=prog), E, now=TUE_NOON)["empty"]
    assert e["body"].endswith("this page refreshes by itself. IV rank: building from daily files (187 / 252 days).")
    member = v(_col(**dict(bad, detail="Waiting for the next market pass.")), E, now=TUE_NOON)["empty"]
    admin = v(_col(**dict(bad, detail="Waiting for the next market pass.")), E, now=TUE_NOON, is_admin=True)["empty"]
    assert member["body"] == "Collector: Waiting for the next market pass. " + options_page._FLAT_PAUSED
    assert member["admin"] == "" and admin["admin"] == options_page._FLAT_ADMIN + " Details: " + err


def test_the_day_file_warnings_say_what_is_wrong():
    """v4.138: three day-file warnings, each in its own words - Massive refused the files
    (auth / plan, or no kind from an older collector), the files keep failing for another
    reason (network / http / missing / a bug: the REST reads took over), the lane off WITH a
    reason (keys set but a setup problem on Hermes) - amber, plain words for members and the
    fix plus the collector's full reason for admins, on the label and on the empty panel. Off
    without a reason (no keys) and a short retry stay green."""
    v = options_page._status_view
    eod = {"id": 3, "kind": "eod", "session": "2026-10-09", "finished": _dt.datetime(2026, 10, 10, 16, 9),
           "n_symbols": 4512, "n_ok": 4500, "n_contracts": 1_020_000}
    meta = {"pass_id": 3, "pass_kind": "eod", "session": "2026-10-09", "finished": "2026-10-10T16:09:00Z",
            "n_underlyings": 4500, "n_contracts": 1_020_000, "empty": False}
    idle = dict(state="idle", heartbeat=TUE_NOON, universe_n=4512, history_total=4512, history_done_n=1204,
                detail="Up to date with the Fri Oct 9 close; next market pass Mon Oct 12 09:45 ET.")
    boto = "boto3 is not installed on this PC - run: pip install -r app\\requirements.txt"
    net = ("could not reach Massive Flat Files for the file list for 2026-10 after 5 tries "
           "(EndpointConnectionError: Could not connect)")
    cases = [
        ({"flat_state": "error", "flat_kind": "plan", "flat_error": "Massive refused ..."},
         options_page._FLAT_PAUSED, options_page._FLAT_ADMIN),
        ({"flat_state": "error", "flat_kind": "network", "flat_error": net},
         options_page._FLAT_FAILING, options_page._FLAT_FAIL_ADMIN),
        ({"flat_state": "error", "flat_kind": "http", "flat_error": "Massive Flat Files answered HTTP 409"},
         options_page._FLAT_FAILING, options_page._FLAT_FAIL_ADMIN),
        ({"flat_state": "error", "flat_kind": "error", "flat_error": "boom"},
         options_page._FLAT_FAILING, options_page._FLAT_BUG_ADMIN),
        ({"flat_state": "off", "flat_kind": "config", "flat_error": boto},
         options_page._FLAT_SETUP, options_page._FLAT_SETUP_ADMIN),
    ]
    E = {"n_contracts": 0, "empty": True}
    for prog, member_words, admin_words in cases:
        prog = dict(prog, flat_days_done=40, flat_days_total=252)
        st = dict(idle, progress=prog)
        m = v(_col(last=eod, **st), meta, now=TUE_NOON)
        a = v(_col(last=eod, **st), meta, now=TUE_NOON, is_admin=True)
        assert m["dot"] == a["dot"] == "amber", prog
        assert m["label"] == "Collector: " + idle["detail"] + " " + member_words, prog
        assert "Admin" not in m["label"] and "TST_MASSIVE_S3" not in m["label"] and "pip" not in m["label"]
        assert a["label"] == m["label"] + " " + admin_words + " Details: " + prog["flat_error"], prog
        assert m["line"].endswith("· IV rank: building (1,204 / 4,512)")        # the REST progress, not the files'
        assert m["flat"]["kind"] == prog["flat_kind"] and m["flat"]["blocked"] is (prog["flat_state"] == "off")
        em = v(_col(**dict(st, detail="Waiting for the next market pass.")), E, now=TUE_NOON)["empty"]
        ea = v(_col(**dict(st, detail="Waiting for the next market pass.")), E, now=TUE_NOON, is_admin=True)["empty"]
        assert em["body"] == "Collector: Waiting for the next market pass. " + member_words and em["admin"] == ""
        assert ea["admin"] == admin_words + " Details: " + prog["flat_error"]
    # the setup reason reaches the admin whole (the fix included)
    a = v(_col(last=eod, **dict(idle, progress={"flat_state": "off", "flat_kind": "config", "flat_error": boto})),
          meta, now=TUE_NOON, is_admin=True)
    assert a["label"].endswith("Details: " + boto)
    # off WITHOUT a reason (no keys) and a short retry (not handed over yet) stay green
    for quiet in ({"flat_state": "off", "flat_error": None},
                  {"flat_state": "retry", "flat_kind": "network", "flat_error": net, "flat_days_done": 40,
                   "flat_days_total": 252}):
        got = v(_col(last=eod, **dict(idle, progress=quiet)), meta, now=TUE_NOON, is_admin=True)
        assert got["dot"] == "green" and "Admin" not in got["label"], quiet
    got = v(_col(last=eod, **dict(idle, progress={"flat_state": "retry", "flat_kind": "network", "flat_error": net,
                                                   "flat_days_done": 40, "flat_days_total": 252})), meta, now=TUE_NOON)
    assert got["line"].endswith("· IV rank: building from daily files (40 / 252 days)")


def test_the_status_api_and_the_first_paint_carry_the_day_file_progress(client, fake, state_file):
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({
        "state": "history", "detail": "Building IV history from Massive's daily files: 12 of 252 days.",
        "heartbeat": _dt.datetime.now(_dt.timezone.utc).isoformat(), "universe_n": 4512,
        "progress": {"flat_state": "building", "flat_days_done": 12, "flat_days_total": 252, "flat_eta_s": None,
                     "flat_error": None},
        "written_by": "dashboard_tst/app/services/scr_collector.py"}), encoding="utf-8")
    j = client.get("/options/api/status").json()
    assert j["source"] == "file" and j["line"].endswith("IV rank: building from daily files (12 / 252 days)")
    assert j["flat"]["building"] is True and j["dot"] == "green"
    assert "IV rank: building from daily files (12 / 252 days)" in client.get("/options").text


def test_the_first_paint_and_the_first_poll_say_the_same(client, fake, screener_url):
    from app import screener_db
    from app.screener_models import ScrPass, ScrStatus

    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    with screener_db.session() as s:
        s.add(ScrStatus(id=1, state="pass", detail="Reading the option market.", heartbeat=now, universe_n=4512,
                        pass_id=2, symbols_done=10, symbols_total=4512))
        s.add(ScrPass(id=1, kind="eod", session="2026-10-09", started=now - _dt.timedelta(hours=2),
                      finished=now - _dt.timedelta(hours=1), n_symbols=4512, n_ok=4500, n_contracts=990_000))
        s.add(ScrPass(id=2, kind="cycle", session="2026-10-12", started=now))
        s.commit()
    boot = _boot(client.get("/options").text)
    j = client.get("/options/api/status").json()
    for k in ("dot", "label", "alert", "line", "empty", "state"):
        assert boot["status"][k] == j[k], k
    assert boot["is_admin"] is False and boot["no_data"]


def test_the_template_carries_the_alert_the_panel_and_the_boot_status(client, fake):
    html = client.get("/options").text
    for i in ("scrCollectorAlert", "scrEmptyPanel", "scrEmptyTitle", "scrEmptyBody", "scrEmptyAdmin",
              "scrDataState", "scrDot", "scrDataLine"):
        assert f'id="{i}"' in html, i
    boot = _boot(html)
    assert boot["status"]["dot"] == "slate" and boot["status"]["empty"]["title"]
    assert boot["no_data"] == options_page._no_data_text()
    assert "setInterval(pollStatus" not in html and "schedulePoll(pollDelay" in html
    assert "fetch('/options/api/status')" in html          # the polls the surface's countdown reads
    m = re.search(r'<div id="scrCollectorAlert"([^>]*)>(.*?)</div>', html, re.S)
    assert m and "hidden" not in m.group(1) and "The market collector has not started yet" in m.group(2)
    # the fake frame holds data: the panel stays hidden on the first paint
    m = re.search(r'<div id="scrEmptyPanel" class="([^"]*)"', html)
    assert m and "hidden" in m.group(1)


def test_the_panel_is_painted_on_the_server_when_the_market_is_empty(client, fake, monkeypatch):
    monkeypatch.setattr(fake, "frame_meta", lambda: {"n_contracts": 0, "empty": True})
    html = client.get("/options").text
    m = re.search(r'<div id="scrEmptyPanel" class="([^"]*)"', html)
    assert m and "hidden" not in m.group(1)
    assert ">No option data yet - the market data collector is not running.<" in html
    assert _boot(html)["status"]["empty"]["admin"] == ""          # a member: no admin line


def test_admins_get_the_fix_in_the_panel(client, fake, monkeypatch, db, user):
    u = db.get(models.User, user.id)
    u.role = models.ROLE_ADMIN
    db.commit()
    monkeypatch.setattr(fake, "frame_meta", lambda: {"n_contracts": 0, "empty": True})
    j = client.get("/options/api/status").json()
    assert j["empty"]["admin"].startswith("Hermes: powershell -ExecutionPolicy Bypass")
    html = client.get("/options").text
    assert _boot(html)["is_admin"] is True and "setup_screener_task.ps1 -StartNow" in html


# ───────────────────────────────────────── numpy missing ─────────────────────────────────────────

def test_numpy_missing_gives_a_clear_notice_and_the_page_still_renders(client, monkeypatch):
    why = options_page._missing_reason(ModuleNotFoundError("No module named 'numpy'"))
    assert "numpy" in why and "pip install -r app\\requirements.txt" in why
    monkeypatch.setattr(options_page, "_load_engine", lambda: (None, why))
    r = client.get("/options")
    assert r.status_code == 200
    # v4.159: the page is the volatility surface (no screener on it), so the screener's notice is
    # not shown there; the screener API still says why
    assert 'id="volPanelSurface"' in r.text and 'id="scrEngineErr"' not in r.text
    boot = _boot(r.text)
    assert boot["engine_ok"] is False
    for method, url, kw in (("post", "/options/api/run", {"json": {"screen": "long-call", "payload": {}}}),
                            ("post", "/options/api/csv", {"json": {"screen": "long-call", "payload": {}}}),
                            ("get", "/options/api/screens", {}),
                            ("get", "/options/api/spec?screen=long-call", {})):
        res = getattr(client, method)(url, **kw)
        assert res.status_code == 503 and res.json()["code"] == "engine_missing" and "numpy" in res.json()["error"]
    st = client.get("/options/api/status").json()
    assert st["ok"] and st["engine_ok"] is False and "numpy" in st["label"]
    # saved screeners do not need the engine
    assert client.get("/options/api/saved?screen=long-call").json()["items"] == []


def test_the_real_import_path_turns_a_missing_numpy_into_a_reason(monkeypatch):
    for name in [m for m in sys.modules if m == "app.services.screener" or m.startswith("app.services.screener.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "numpy", None)        # "import numpy" now raises ImportError
    eng, why = options_page._load_engine()
    if not (APP_DIR / "services" / "screener" / "engine.py").exists():
        assert eng is None and why                           # the engine is not deployed: a reason, no crash
        return
    if eng is not None:
        pytest.skip("this engine imports numpy lazily (inside its functions)")
    assert "numpy" in why and "pip install" in why


# ───────────────────────────────────────── saved screeners ─────────────────────────────────────────

def _save(client, **body):
    body.setdefault("screen", "bull-put-spread")
    body.setdefault("payload", {"filters": [{"f": "dte", "op": "lte", "hi": 45}]})
    return client.post("/options/api/saved", json=body)


def test_saved_create_update_default_delete(client, db, user):
    j = _save(client, name="  Weekly   puts ").json()
    assert j["ok"] and j["created"] and j["item"]["name"] == "Weekly puts" and j["item"]["is_default"] is False
    a = j["item"]["id"]
    j = _save(client, name="weekly PUTS", payload={"filters": [], "view": "greeks"}).json()
    assert j["created"] is False and j["item"]["id"] == a and j["item"]["name"] == "weekly PUTS"
    assert j["item"]["payload"] == {"filters": [], "view": "greeks", "flag_earnings": False}
    b = _save(client, name="Second", is_default=True).json()["item"]["id"]
    items = client.get("/options/api/saved?screen=bull-put-spread").json()["items"]
    assert [(i["name"], i["is_default"]) for i in items] == [("Second", True), ("weekly PUTS", False)]
    j = client.post(f"/options/api/saved/{a}/default").json()
    assert {i["id"]: i["is_default"] for i in j["items"]} == {a: True, b: False}
    j = client.post(f"/options/api/saved/{a}/default", json={"on": False}).json()
    assert not any(i["is_default"] for i in j["items"])
    j = client.post(f"/options/api/saved/{b}/delete").json()
    assert [i["id"] for i in j["items"]] == [a]
    assert db.query(models.OptionScreen).count() == 1
    # another screen keeps its own list
    assert client.get("/options/api/saved?screen=long-call").json()["items"] == []


def test_saved_validation_and_limits(client, db, user):
    assert _save(client, name="").status_code == 400
    assert _save(client, name="x" * 81).status_code == 400
    assert _save(client, name="ok", screen="Not A Slug!").status_code == 404
    too_many = {"filters": [{"f": "dte", "op": "gte", "lo": i} for i in range(61)]}
    r = _save(client, name="big", payload=too_many)
    assert r.status_code == 400 and "at most 60" in r.json()["error"]
    assert client.post("/options/api/saved", json=["not", "a", "dict"]).status_code == 400
    db.add_all([models.OptionScreen(user_id=user.id, screen_key="bull-put-spread", name=f"s{i:02d}", payload={})
                for i in range(options_page.MAX_SAVED_PER_SCREEN)])
    db.commit()
    r = _save(client, name="one more")
    assert r.status_code == 400 and r.json()["code"] == "limit"
    assert _save(client, name="S07").json()["created"] is False        # updating at the limit still works
    assert _save(client, name="other screen", screen="long-call").json()["created"] is True


def test_saved_screeners_are_private(client, db, user):
    other = models.User(email="other@local.test", display_name="Other", role=models.ROLE_MEMBER,
                        status=models.APPROVED)
    db.add(other)
    db.commit()
    theirs = models.OptionScreen(user_id=other.id, screen_key="bull-put-spread", name="Theirs", payload={})
    db.add(theirs)
    db.commit()
    assert client.get("/options/api/saved?screen=bull-put-spread").json()["items"] == []
    assert client.post(f"/options/api/saved/{theirs.id}/default").status_code == 404
    assert client.post(f"/options/api/saved/{theirs.id}/delete").status_code == 404
    mine = _save(client, name="Theirs").json()          # same name, my own row
    assert mine["created"] is True and mine["item"]["id"] != theirs.id
    db.expire_all()
    assert db.get(models.OptionScreen, theirs.id).user_id == other.id


def test_saved_rows_go_with_the_member(engine, db, user):
    db.add(models.OptionScreen(user_id=user.id, screen_key="long-call", name="A", payload={}))
    db.commit()
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM users WHERE id = :i"), {"i": user.id})
    assert db.query(models.OptionScreen).count() == 0


# ───────────────────────────────────────── the migration ─────────────────────────────────────────

def test_the_migration_creates_and_drops_option_screens(migrated_url):
    assert "option_screens" in table_names(migrated_url)
    eng = make_engine(migrated_url)
    try:
        cols = {c["name"] for c in sa.inspect(eng).get_columns("option_screens")}
        uniq = sa.inspect(eng).get_unique_constraints("option_screens")
    finally:
        eng.dispose()
    assert cols == {"id", "user_id", "screen_key", "name", "payload", "is_default", "created_at", "updated_at"}
    assert any(set(u["column_names"]) == {"user_id", "screen_key", "name"} for u in uniq)
    downgrade(migrated_url, "7c1e5a9d2b40")
    assert "option_screens" not in table_names(migrated_url)


# ───────────────────────────────────────── the real engine (when it imports) ─────────────────────────────────────────

def test_the_real_engine_on_an_empty_screener_db(client, screener_url):
    try:
        importlib.import_module("app.services.screener.engine")
    except ImportError as exc:
        pytest.skip(f"the screener engine does not import here: {exc}")
    from app.services.screener import frame

    frame.reset()                       # load from the empty test DB, not a cached frame
    try:
        _real_engine_checks(client)
    finally:
        frame.reset()


def _real_engine_checks(client):
    j = client.get("/options/api/screens").json()
    assert j["ok"] and len(j["screens"]) == 33
    keys = [s["key"] for s in j["screens"]]
    assert "options-screener" in keys and "bull-put-spread" in keys
    html = client.get("/options").text
    boot = _boot(html)
    assert boot["engine_ok"] is True and boot["mode"] == "surface"
    # v4.137: the status carries the frame's reloading flag, and the empty panel is painted on the server
    assert boot["status"]["frame"]["empty"] is True and boot["status"]["frame"]["reloading"] is False
    assert boot["no_data"] == "No option data loaded yet."
    m = re.search(r'<div id="scrEmptyPanel" class="([^"]*)"', html)
    assert m and "hidden" not in m.group(1)
    assert boot["status"]["empty"]["title"] == "No option data yet - the market data collector is not running."
    for key in ("options-screener", "bull-put-spread", "long-call-calendar", "short-iron-condor"):
        s = client.get(f"/options/api/spec?screen={key}").json()
        assert s["ok"] and s["fields"] and s["views"], key
        r = client.post("/options/api/run", json={"screen": key, "payload": s["defaults"], "page": 1})
        assert r.status_code == 200, (key, r.text)
        assert r.json()["total"] == 0 and r.json()["rows"] == []
    r = client.post("/options/api/csv", json={"screen": "options-screener", "payload": {}})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert client.get("/options/api/status").json()["ok"] is True


# ───────────────────────────────────────── what v4.135 removed stays removed ─────────────────────────────────────────

def test_the_router_serves_the_screener_paths():
    paths = {(r.path, tuple(sorted(r.methods))) for r in options_page.router.routes}
    assert paths == {
        ("/options", ("GET",)),
        ("/options/history", ("GET",)),                     # v4.159: one contract's history, its own window
        ("/options/api/watchlists", ("GET",)),              # v4.165: the surface's watchlist pane
        ("/options/api/liquidity", ("GET",)),               # v4.186: the pane's option-liquidity verdicts
        ("/options/api/screens", ("GET",)),
        ("/options/api/spec", ("GET",)),
        ("/options/api/run", ("POST",)),
        ("/options/api/csv", ("POST",)),
        ("/options/api/status", ("GET",)),
        ("/options/api/saved", ("GET",)),
        ("/options/api/saved", ("POST",)),
        ("/options/api/saved/{sid}/default", ("POST",)),
        ("/options/api/saved/{sid}/delete", ("POST",)),
        ("/options/api/surface", ("GET",)),                 # v4.139: the volatility surface
        ("/options/api/surface/history", ("GET",)),         # v4.141: each cell's own history behind it
        ("/options/api/surface/cm", ("GET",)),              # v4.171: the interpolated (fixed-horizon) surface
        ("/options/api/history/days", ("GET",)),            # v4.139: the contract history
        ("/options/api/history/chain", ("GET",)),
        ("/options/api/history/contract", ("GET",)),
    }


def test_the_v2_screener_and_page_files_are_gone():
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

    assert ("options", "Volatility Surface", "Options", "/options") in menus.MENUS


def test_a_payload_without_filters_means_the_screen_defaults():
    """No "filters" key = the screen's defaults (the engine's rule, §5); [] = no filters."""
    from app.routes import options_page as op

    clean, err = op._clean_payload({"sort": {"col": "volume", "dir": "desc"}})
    assert err is None and "filters" not in clean and clean["sort"] == {"col": "volume", "dir": "desc"}
    clean, err = op._clean_payload({"filters": []})
    assert err is None and clean["filters"] == []
    clean, err = op._clean_payload(None)
    assert err is None and "filters" not in clean


# ─────────────────── v4.139: the VOLATILITY SURFACE + CONTRACT HISTORY tabs, Option History ───────────────────

def test_the_surface_cells_open_the_contract_history_in_a_new_window(client, fake):
    """v4.159: a click on a surface cell opens /options/history?symbol=&expiry=&right=&strike=&day=
    in a new window - the right is the cell's own (out of the money: put below the price, call
    above), the day the surface's own date or, on the latest pass, the newest history day."""
    html = client.get("/options").text
    for needle in ('id="volPanelSurface"', 'id="volForm"', "/options/api/surface",
                   "'/options/history?' + q.toString()", "window.open(historyUrl(sym, expiry, right, strike, day), '_blank', 'noopener')",
                   "openContract(j.symbol, ex.date, sideOf(j, k), k, day)",
                   "j.source === 'latest' ? ((H.days && H.days.length) ? H.days[0] : null) : j.on",
                   "return k < j.spot ? 'P' : 'C';", "click: contract history in a new window",
                   'class="vol-iv"', 'class="vol-l2"', 'class="vol-l3"'):
        assert needle in html, needle
    assert ".vol-grid td.vol-cell { cursor: pointer; }" in html
    # squarish cells, the IV figure centred both ways; an empty reserved line takes no room
    assert "vertical-align: middle; text-align: center; position: relative;" in html
    assert ".vol-grid .vol-l2:empty, .vol-grid .vol-l3:empty { display: none; }" in html
    # the old #history link is sent to the history window
    assert "location.replace('/options/history'" in html


def test_the_surface_tiles_are_configurable(client, fake):
    """v4.166 (user: "configurable tiles in the vol surface ... the top to show the greeks. the
    bottom will show the last price"): a Tile block in the controls pane - Delta / Gamma / Theta /
    Vega for the top line (Delta + Theta on), the IV in the middle, Last / Model price / OI /
    Volume / Nothing for the bottom (Last on) - remembered in th.vol.tile; the cell renderer and
    the hover card read j.extra; a change repaints without a new request."""
    html = client.get("/options").text
    for needle in ('<details id="volTile"', 'id="volTileSum"', 'id="volTileTop"', 'id="volTileBottom"',
                   'id="volTileNote"', "Greeks and prices are on the latest pass only",
                   "'th.vol.tile'", "var TILE_DEFAULT = {g: ['d', 'g', 't', 'v'], b: 'last'};",
                   "var x = extraAt(j, r, c);", "'<div class=\"vol-l2\">' + tileTop(x) + '</div><div class=\"vol-iv\">'",
                   "'<div class=\"vol-l3\">' + tileBottom(x) + '</div>' + liqBar(x) + '</td>'",
                   "figsHtml(j, xf)) + liqHtml(j, xf);", "j.has_greeks === false",
                   "$('volTile').addEventListener('change', function () { tileRead(); paintTiles(); });",
                   "Model price", "Open interest", "'Delta'", "'Gamma'", "'Theta'", "'Vega'",
                   "g[2].charAt(0) + ' ' + greekText("):      # v4.168: the initials D G T V, not the signs
        assert needle in html, needle
    # the defaults in the markup: Delta + Theta ticked, Gamma + Vega not, Last price picked
    for k in ("D", "G", "T", "V"):          # v4.167: all four greeks on by default
        assert 'id="volTile%s" data-g="%s" checked' % (k, k.lower()) in html
    assert 'name="volTileB" value="last" checked' in html
    for v in ("price", "oi", "vol", "none"):
        assert f'name="volTileB" value="{v}">' in html, v
    # the Tile block sits in the controls pane, after the form
    ctl = html[html.index('id="volCtlPane"'):html.index('id="volLegend"')]
    assert ctl.index('id="volForm"') < ctl.index('id="volTile"')
    # a repaint does not ask the server again
    paint = html[html.index("function paintTiles("):html.index("function figsHtml(")]
    assert "getJSON" not in paint and "fetch(" not in paint and "runSurface" not in paint
    # the greeks: 2 decimals, no leading zero; OI / volume in thousands with k
    assert "if (s.charAt(0) === '0') s = s.slice(1);" in html and "+ 'k';" in html
    assert ".vol-grid .vol-l2 { font-size: 9px;" in html and ".vol-grid .vol-l3 { font-size: 9px;" in html


def test_the_history_window_renders_the_contract_from_the_url(client, fake):
    """v4.159: /options/history renders the same template in history mode - only the contract
    history (pickers, table, CSV), the contract in the boot so the page draws it on load, a
    header naming it and a link back to the surface."""
    r = client.get("/options/history", params={"symbol": "acme", "expiry": "2026-12-18", "right": "c",
                                               "strike": "100", "day": "2026-10-16"})
    assert r.status_code == 200
    html = r.text
    boot = _boot(html)
    assert boot["mode"] == "history"
    assert boot["contract"] == {"symbol": "ACME", "expiry": "2026-12-18", "right": "C", "strike": 100.0,
                                "day": "2026-10-16", "label": "ACME 2026-12-18 100 C"}
    assert "Contract history &middot; ACME 2026-12-18 100 C</h1>" in html
    assert 'href="/options?symbol=ACME#surface"' in html and "&larr; Volatility surface" in html
    for needle in ('id="volPanelHistory"', 'id="chForm"', 'id="chSym"', 'id="chDay"', 'id="chExp"',
                   'id="chRight"', 'id="chStrike"', 'id="chCsv"', 'id="chMsg"', "/options/api/history/contract",
                   "/options/api/history/chain", "openFromBoot(BOOT.contract || {})",
                   "C.want = full ? {expiry: c.expiry, right: c.right, strike: Number(c.strike)} : null",
                   "is not in the list of contracts that traded on"):
        assert needle in html, needle
    for gone in ('id="volPanelSurface"', 'id="volForm"', 'id="volTable"', 'id="scrDot"', 'id="scrGoto"',
                 "Set filters"):
        assert gone not in html, gone
    assert "/options/api/run" not in html


def test_the_history_window_drops_bad_values(client, fake):
    r = client.get("/options/history", params={"symbol": "<script>", "expiry": "2026-13-40", "right": "X",
                                               "strike": "abc", "day": "nope"})
    assert r.status_code == 200
    c = _boot(r.text)["contract"]
    assert c == {"symbol": "", "expiry": "", "right": "", "strike": None, "day": "", "label": ""}
    assert 'href="/options#surface"' in r.text and "<script>alert" not in r.text
    assert ">Contract history</h1>" in r.text
    # a strike like 182.5 keeps its decimals; inf / negative are dropped
    assert options_page._clean_contract("nvda", "2026-11-20", "P", "182.5")["label"] == "NVDA 2026-11-20 182.5 P"
    for bad in ("inf", "-5", "0"):
        assert options_page._clean_contract("NVDA", strike=bad)["strike"] is None, bad
    # only the symbol: the pickers start on it (no contract drawn)
    c = _boot(client.get("/options/history", params={"symbol": "NVDA"}).text)["contract"]
    assert c["symbol"] == "NVDA" and c["expiry"] == "" and c["label"] == "NVDA"


def test_the_surface_api_answers_with_the_grid_or_a_plain_reason(client, fake, screener_url):
    from app import screener_db
    from app.screener_models import ScrContract, ScrUnderlying

    asof = _dt.datetime(2026, 10, 7, 19, 30)
    with screener_db.session() as s:
        s.add(ScrUnderlying(symbol="AAPL", name="Apple", sec_type="stock", spot=100.0, history_done=False,
                            history_tries=0))
        for k in (90.0, 100.0, 110.0):
            for rt in "CP":
                s.add(ScrContract(symbol="AAPL", expiry="2026-11-20", right=rt, strike=k, weekly=False, iv=0.3,
                                  session="2026-10-07", as_of=asof, delta=(0.45 if rt == "C" else -0.55),
                                  theta=-0.03, last=2.5, oi=12400, volume=None))
        s.commit()
    j = client.get("/options/api/surface", params={"symbol": "aapl"}).json()
    assert j["ok"] is True and j["symbol"] == "AAPL" and j["strikes"] == [90.0, 100.0, 110.0]
    assert j["cells"] == [[30.0], [30.0], [30.0]] and j["expiries"][0]["date"] == "2026-11-20"
    # v4.166: the tile figures beside the cells (the same contract: put below 100, call at / above)
    assert j["has_greeks"] is True
    assert [[x["d"] for x in row] for row in j["extra"]] == [[-0.55], [0.45], [0.45]]
    assert j["extra"][0][0] == {"d": -0.55, "g": None, "t": -0.03, "v": None, "last": 2.5, "price": None,
                                "chg": None, "oi": 12400, "vol": None, "liq": 0.5, "rk": 1}   # v4.181 / v4.184: ranked among the chain
    assert j["source"] == "latest" and j["as_of"] == "2026-10-07T19:30:00Z"
    assert j["levels"] == {}                                           # v4.192: no MATP row for AAPL on the board
    assert j["common"] is True and j["hidden_strikes"] == 0
    assert client.get("/options/api/surface", params={"symbol": "AAPL", "common": "0"}).json()["common"] is False
    # v4.141: the per-cell history for the same surface (no day converted here: an honest empty answer)
    h = client.get("/options/api/surface/history", params={"symbol": "AAPL"}).json()
    assert h["ok"] is True and h["cells"] == {} and h["days"] == 0 and "converted" in h["note"] and h["on"] == "2026-10-07"
    h = client.get("/options/api/surface/history", params={"symbol": "NOPE"}).json()
    assert h["ok"] is False and "NOPE" in h["error"]
    j = client.get("/options/api/surface", params={"symbol": "NOPE"}).json()
    assert j["ok"] is False and "NOPE" in j["error"]
    j = client.get("/options/api/surface", params={"symbol": "AAPL", "day": "2026-10-02"}).json()
    assert j["ok"] is False and "contract history" in j["error"]


def test_the_surface_apis_carry_the_tickers_matp_and_mbp(client, fake, screener_url, db):
    """v4.192 (user: "add an MATP and MBP in the tile so that user will know the strike of the MBP and
    MATP"): both surface answers carry ``levels`` from the Watchlist's MATP board - an active row wins
    over a dropped one; a missing figure is left out; a ticker off the board gets ``{}``."""
    from app import screener_db
    from app.screener_models import ScrContract, ScrUnderlying

    asof = _dt.datetime(2026, 10, 7, 19, 30)
    with screener_db.session() as s:
        s.add(ScrUnderlying(symbol="AMD", name="AMD", sec_type="stock", spot=100.0, history_done=False, history_tries=0))
        for exp in ("2026-11-20", "2026-12-18"):
            for k in (90.0, 100.0, 110.0):
                for rt in "CP":
                    s.add(ScrContract(symbol="AMD", expiry=exp, right=rt, strike=k, weekly=False, iv=0.3,
                                      session="2026-10-07", as_of=asof, oi=100, volume=5))
        s.commit()
    db.add(models.MATPLevel(symbol="AMD", exchange="NASDAQ", matp=107.5, mbp=93.25, status="dropped"))
    db.add(models.MATPLevel(symbol="AMD", exchange="NASDAQ", matp=108.0, mbp=None, status="active"))
    db.commit()
    j = client.get("/options/api/surface", params={"symbol": "amd"}).json()
    assert j["ok"] is True and j["levels"] == {"matp": 108.0}
    j = client.get("/options/api/surface/cm", params={"symbol": "AMD", "horizons": "30,60"}).json()
    assert j["ok"] is True and j["levels"] == {"matp": 108.0}
    # the page: a line per level between the strike rows (orange MATP, green MBP) and a tag on the nearest strike
    html = client.get("/options").text
    for needle in ("var LVL = {matp: {word: 'MATP', cls: 'vol-lvl-matp'", "function levelsBefore(st, k, n)",
                   "function levelTags(j, rows)", "h += levelsRest(lst, vw.cols.length);",
                   "h += levelsBefore(lst, j.spot, vw.cols.length) + spotRow(j, vw.cols.length); spotDone = true; }",
                   "h += levelsBefore(lst, j.spot, vw.cols.length) + cmSpotRow(j, vw.cols.length); spotDone = true; }",
                   "esc(strikeText(k)) + tagHtml(ltags[r]) + '</th>'", "esc(mText(row)) + tagHtml(ltags[r]) + '</th>'",
                   ".vol-grid tr.vol-lvl.vol-lvl-matp th, .vol-grid tr.vol-lvl.vol-lvl-matp td { color: #fb923c; border-bottom-color: #f97316; }",
                   ".vol-grid tbody tr:not(.vol-spot):not(.vol-lvl) > td {",
                   '<span class="vol-leg-matp">MATP</span> / <span class="vol-leg-mbp">MBP</span> lines from the Watchlist'):
        assert needle in html, needle
    assert html.count("lines from the Watchlist") == 2                  # both tabs' legends


def test_the_history_days_api_lists_the_converted_sessions(client, fake, monkeypatch, tmp_path):
    pytest.importorskip("pyarrow")
    from app.services import opt_history

    root = tmp_path / "pq"
    monkeypatch.setenv("TST_OPTION_HISTORY_DIR", str(root))
    days = ("2026-10-05", "2026-10-06")
    for d in days:
        p = opt_history.day_path(d, root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    (root / "manifest.json").write_text(json.dumps({d: {"fmt": opt_history.FORMAT} for d in days}), encoding="utf-8")
    j = client.get("/options/api/history/days").json()
    assert j["ok"] is True and j["available"] is True and j["days"] == ["2026-10-06", "2026-10-05"]


def test_the_contract_apis_say_why_when_they_cannot(client, fake, screener_url):
    j = client.get("/options/api/history/chain", params={"symbol": "", "day": "2026-10-06"}).json()
    assert j["ok"] is False and "symbol" in j["error"]
    j = client.get("/options/api/history/contract",
                   params={"symbol": "AAPL", "expiry": "x", "right": "C", "strike": "1"}).json()
    assert j["ok"] is False and j["error"]


def test_the_data_ingest_page_has_the_option_history_tab(engine, db, no_screener_db, monkeypatch, tmp_path):
    """Settings -> Data Ingest is admin-only (main.py); the new tab and its fragment come with it."""
    from app.services import flat_files

    monkeypatch.setattr(flat_files, "DEFAULT_ROOT", tmp_path / "flat")
    admin = models.User(email="admin@local.test", display_name="Admin", role=models.ROLE_ADMIN,
                        status=models.APPROVED, created_at=_dt.datetime(2026, 1, 5, tzinfo=_dt.timezone.utc))
    db.add(admin)
    db.commit()
    try:
        client = _client(engine, admin.id)
        html = client.get("/finviz").text
        assert 'data-tab="opthist"' in html and 'id="ohBox"' in html and "/finviz/option-history" in html
        frag = client.get("/finviz/option-history")
        assert frag.status_code == 200
        for needle in ("1 &middot; Download from Massive", "2 &middot; IV Rank history",
                       "3 &middot; Convert to Parquet", "By month", "How it updates", "has not reported on this server"):
            assert needle in frag.text, needle
    finally:
        app.dependency_overrides.clear()


def test_every_surface_helper_the_page_calls_is_defined():
    """v4.146: the v4.145 patch dropped ``sideOf()`` along with the band helpers it replaced, and
    the hover card threw a ReferenceError in the browser. Every helper the surface and history
    scripts call must be defined exactly once in the page."""
    html = (APP_DIR / "templates" / "options.html").read_text(encoding="utf-8")
    script = html[html.index("VOLATILITY SURFACE"):]
    for name in ("sideOf", "rankHue", "rankColor", "rankText", "rankBar", "cellStyle", "sameGrid",
                 "paintHistory", "loadHistory", "spotRow", "renderSurface", "popoverHtml", "sparkline",
                 "showPop", "hidePop", "runSurface", "openContract", "strikeText", "dayNum",
                 # v4.171: the tabs, the interpolated surface, the tidy-up
                 "activeLast", "tabSet", "drawIfStale", "syncTick", "legends", "runCm", "renderCm", "cmView",
                 "cmPopHtml", "showCmPop", "hideCmPop", "cmBottom", "cmNear", "cmExact", "placePop", "hzChosen",
                 "hzLoad", "surfaceParams", "cmParams", "passNow", "cleanOn", "tidy", "fullView", "allIdx",
                 "youngCols", "youngKey", "surfaceView", "tidyNote", "cmSpotRow"):
        assert re.search("(?<![A-Za-z0-9_])" + name + "[(]", script), f"{name} is never called - update this list"
        assert html.count(f"function {name}(") == 1, f"{name} must be defined exactly once"


def test_the_surface_opens_on_every_expiry_and_reads_the_atm_line_the_right_way(client, fake):
    """v4.149: every expiry by default (monthly one click away); the ATM line says what is above it
    (the strikes below the price: calls in the money, puts out); the countdown to the next market
    pass is wired to the status polls."""
    html = client.get("/options").text
    # v4.160 (user: "i still want them"): the Show / Strikes / Expiries / Common-strikes controls
    # are back, defaulting to what v4.159 had fixed: out of the money, every strike, every
    # expiry, common strikes on; a change redraws
    # v4.164 (user: "by default the Volatility surface load Strike +%, on monthly"): strikes
    # within 20 % of the price and the monthly expiries open first
    for back in ('<select id="volSide"', '<option value="otm" selected>', '<select id="volBand"',
                 '<option value="20" selected>&plusmn;20%</option>', '<select id="volDte"', '<select id="volExp"',
                 '<option value="monthly" selected>monthly</option>', 'type="checkbox" id="volCommon" checked',
                 'id="volGo"', "['volSide', 'volBand', 'volDte', 'volExp', 'volCommon'].forEach"):
        assert back in html, back
    assert 'type="hidden" id="volSide"' not in html
    assert "above: calls in the money, puts out &middot; below: calls out of the money, puts in" in html
    for needle in ('id="volTick"', 'id="volInfoRow"', "window.optOnStatus", "window.optGetStatus",
                   "window.optPollNow", "next_pass_in_s", "stored day &middot; fixed",
                   "next pass: waiting for the collector", "market pass paused"):
        assert needle in html, needle
    # v4.178 (user: "this line is too long ... just put the ticker name in bigger font size, apply to both
    # surface"): both grids head with the ticker large; the old line and the method are the tooltip
    head = html[html.index("function infoHead("):html.index("function renderSurface(")]
    assert "el.title = (detail ? detail + '\\n' : '') + (j.method || '');" in head
    assert "'<span class=\"vol-head\"><b>' + esc(j.symbol) + '</b>'" in head and "vol-head-name" in head
    assert html.count("infoHead($('volInfo'), j, detail);") == 1 and html.count("infoHead($('cmInfo'), j, detail);") == 1
    assert "$('volInfo').innerHTML" not in html and "$('cmInfo').innerHTML" not in html
    assert ".vol-head > b { font-size: 1.25rem;" in html
    # v4.177 (user: "if the edge of the table is showing blank, trim it"): the box shrinks to its grid
    assert ".vol-wrap { width: fit-content; max-width: 100%; align-self: center; }" in html
    assert "@media (min-width: 1024px) { .vol-wrap { flex: 0 1 auto; min-height: 0; } }" in html
    # v4.176 (user: "this should be show on the tab header"): the countdown sits in the tab row, not the left panel
    tabs = html[html.index('<div id="volTabs"'):html.index('id="scrCollectorAlert"')]
    assert '<span id="volTick" class="vol-tick ml-auto hidden"></span>' in tabs
    assert html.count('id="volTick"') == 1 and ".vol-tabs .vol-tick { align-self: center; margin-left: auto;" in html


def test_the_page_no_longer_says_options_screener():
    """v4.149: the user - "remove the word option scanner". The generic screen is "All contracts";
    the template's rendered text never says "Options Screener" (Jinja comments aside)."""
    src = (APP_DIR / "templates" / "options.html").read_text(encoding="utf-8")
    assert "Options Screener" not in re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    pytest.importorskip("numpy")
    from app.services.screener import screens

    assert screens.SCREENS["options-screener"].label == "All contracts"
    assert "Options Screener" not in Path(options_page.__file__).read_text(encoding="utf-8").split('"label": ')[1][:40]


def test_symbol_in_the_url_opens_the_surface_for_it(client, fake):
    """v4.154: /options?symbol=NVDA#surface (the Watchlist's "Vol surface" button) fills the
    symbol in and draws the surface without typing - the hand-off script is on the page."""
    r = client.get("/options", params={"symbol": "NVDA"})
    assert r.status_code == 200
    html = r.text
    assert "URLSearchParams(location.search).get('symbol')" in html
    assert "loadDays().then(function () { runSurface(); })" in html
    assert "optShowTab" not in html                     # v4.159: no tabs - the surface is the page


def test_watchlist_chart_pane_links_to_the_surface_for_its_ticker(client, fake):
    """v4.154: the selected ticker's pane carries a "Vol surface" link to the options page
    in a new window, with the ticker on screen - nothing to type."""
    r = client.get("/matp/chart", params={"symbol": "nvda"})
    assert r.status_code == 200
    html = r.text
    assert 'href="/options?symbol=NVDA#surface"' in html
    assert 'target="_blank"' in html and "Vol surface" in html
    assert 'href="/calc?view=sheet&amp;symbol=NVDA"' in html and "Calculator" in html   # v4.157


# ─────────────────────────────── the surface's watchlist (v4.165) ───────────────────────────────

def _seed_watchlists(db, user):
    f1 = models.FinvizFilter(description="Big caps", url="https://finviz.com/screener.ashx?v=1", is_active=True)
    f2 = models.FinvizFilter(description="Switched off", url="https://finviz.com/screener.ashx?v=2", is_active=False)
    f3 = models.FinvizFilter(description="Empty screen", url="https://finviz.com/screener.ashx?v=3", is_active=True)
    db.add_all([f1, f2, f3])
    db.flush()
    db.add_all([
        models.MATPLevel(symbol="NVDA", matp=210.5, filter_id=f1.id, status="active"),
        models.MATPLevel(symbol="AAPL", matp=None, filter_id=f1.id, status="active"),
        models.MATPLevel(symbol="OFF1", matp=50.0, filter_id=f2.id, status="active"),
        models.MATPLevel(symbol="SOLO", matp=12.25, filter_id=None, status="active"),
        models.MATPLevel(symbol="GONE", matp=9.0, filter_id=f1.id, status="dropped"),
    ])
    db.add_all([models.UserWatchlist(user_id=user.id, symbol="NVDA"),
                models.UserWatchlist(user_id=user.id, symbol="ZZZZ")])      # starred, no MATP row
    db.commit()
    return f1, f2, f3


def test_the_watchlists_api_lists_the_members_lists(client, db, user):
    """v4.165 (user: "in the Vol surface the left side panel i need to add a watchlist"): one call
    carries every list the Watchlist page offers - My Watchlist first, then one per ACTIVE Finviz
    screener (an empty one left out), Selective (no screener), All - each ticker with its MATP."""
    f1, _f2, f3 = _seed_watchlists(db, user)
    r = client.get("/options/api/watchlists")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["default"] == "mine"            # the member has starred tickers
    keys = [x["key"] for x in j["lists"]]
    assert keys == ["mine", str(f1.id), "individual", "all"], keys
    assert str(f3.id) not in keys                                 # an empty screener is left out
    by = {x["key"]: x for x in j["lists"]}
    assert by["mine"]["label"] == "My Watchlist"
    assert {s["symbol"]: s["matp"] for s in by["mine"]["symbols"]} == {"NVDA": 210.5, "ZZZZ": None}
    assert by[str(f1.id)]["label"] == "Big caps"
    assert by[str(f1.id)]["symbols"] == [{"symbol": "AAPL", "matp": None}, {"symbol": "NVDA", "matp": 210.5}]
    assert by["individual"] == {"key": "individual", "label": "Selective", "n": 1,
                                "symbols": [{"symbol": "SOLO", "matp": 12.25}]}
    assert by["all"]["label"] == "All"
    assert [s["symbol"] for s in by["all"]["symbols"]] == ["AAPL", "NVDA", "OFF1", "SOLO"]   # active rows, sorted
    for x in j["lists"]:
        assert x["n"] == len(x["symbols"])


def test_the_watchlists_api_without_any_list_keeps_mine_and_all(client, db, user):
    """A member with no stars and an empty universe still gets My Watchlist and All (empty), and
    the default falls back to All - the Watchlist page's own rule."""
    j = client.get("/options/api/watchlists").json()
    assert j["ok"] is True and j["default"] == "all"
    assert [(x["key"], x["n"]) for x in j["lists"]] == [("mine", 0), ("all", 0)]


def test_the_watchlists_api_answers_an_error_in_plain_json(client, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(options_page, "_watchlists_payload", boom)
    r = client.get("/options/api/watchlists")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False and "could not be read" in j["error"]


def test_the_left_panel_has_the_watchlist_on_top_and_an_adjustable_split(client, fake):
    """v4.165 (user: "up panel is watchlist and the up and down panel adjustable"): the left panel
    is the watchlist pane (list picker + tickers), a horizontal drag handle, then the controls; a
    click on a ticker fills the Ticker box and draws the surface; the list and the split are
    remembered per browser."""
    html = client.get("/options").text
    for needle in ('id="volAside"', 'id="volWlPane"', '<select id="volWl"', 'id="volWlList"', 'id="volWlCount"',
                   'id="volSplit" role="separator" aria-orientation="horizontal"', "cursor-row-resize",
                   'id="volCtlPane"', "'/options/api/watchlists'", "'th.vol.wl'", "'th.vol.split'",
                   "No tickers in this list yet.", "bg-emerald-500/15", "text-emerald-300",
                   "$('volSym').value = row.dataset.sym;", "touchstart", "mousedown", "--vol-wl-h"):
        assert needle in html, needle
    # the order in the panel: watchlist pane, handle, controls (form), then the right panel
    order = [html.index(x) for x in ('id="volWlPane"', 'id="volSplit"', 'id="volCtlPane"', 'id="volForm"',
                                     'id="volLegend"', 'id="volPanelSurface"')]
    assert order == sorted(order), order
    # a click on a ticker row calls runSurface; runSurface marks the drawn ticker in the list
    script = html[html.index("THE SURFACE'S WATCHLIST"):]
    click = script[script.index("$('volWlList').addEventListener('click'"):]
    assert "runSurface();" in click[:400]
    run = html[html.index("function runSurface("):html.index("function historyUrl(")]
    assert "wlMark(sym);" in run
    for name in ("wlMark", "wlPaint", "wlLoad", "splitInit", "splitSet", "splitClamp"):
        assert html.count(f"function {name}(") == 1, name
    # the split keeps the controls in reach: top pane at least 6rem, at most the panel less 14rem
    assert "6 * r" in html and "h - 14 * r" in html
    assert "height: var(--vol-wl-h, 45%)" in html and ".vol-wlpane { flex: none; height: 12rem; }" in html


# ─────────────────── v4.171: the INTERPOLATED VOLATILITY SURFACE tab + "Hide new and thinly listed" ───────────────────

def _seed_cm(screener_db):
    from app.screener_models import ScrContract, ScrUnderlying

    asof = _dt.datetime(2026, 10, 7, 19, 30)
    with screener_db.session() as s:
        s.add(ScrUnderlying(symbol="AAPL", name="Apple", sec_type="stock", spot=100.0, history_done=False,
                            history_tries=0))
        for exp, iv in (("2026-11-20", 0.34), ("2026-12-18", 0.32)):          # 44 and 72 days out
            for k in (90.0, 100.0, 110.0):
                for rt in "CP":
                    s.add(ScrContract(symbol="AAPL", expiry=exp, right=rt, strike=k, weekly=False, iv=iv,
                                      session="2026-10-07", as_of=asof, delta=(0.45 if rt == "C" else -0.55),
                                      gamma=0.02, theta=-0.03, vega=0.1, last=2.5, oi=100, volume=5))
        s.commit()


def test_the_interpolated_surface_api(client, fake, screener_url):
    """GET /options/api/surface/cm: the same rows on fixed horizons (default 30 ... 365 days; ``horizons=``
    picks), each cell interpolated in total variance between the two listed expiries around it."""
    from app import screener_db

    _seed_cm(screener_db)
    j = client.get("/options/api/surface/cm", params={"symbol": "aapl"}).json()
    assert j["ok"] is True and j["symbol"] == "AAPL" and j["source"] == "latest" and j["has_greeks"] is True
    hz = [30, 60, 90, 180, 270, 365, 456, 547, 638, 730]               # v4.175: out to two years
    assert [h["days"] for h in j["horizons"]] == hz and j["horizons_requested"] == hz
    T1, T2, T = 44 / 365, 72 / 365, 60 / 365
    w = (T2 - T) / (T2 - T1)
    want = math.sqrt((0.34 ** 2 * T1 * w + 0.32 ** 2 * T2 * (1 - w)) / T) * 100
    # v4.175: the primary rows are fixed moneyness levels - 100 x m inside the listed 90 ... 110
    assert j["rows_mode"] == "moneyness" and j["n_expiries_used"] == 2
    assert [r["pct"] for r in j["rows"]] == [90.0, 95.0, 97.5, 100.0, 102.5, 105.0, 110.0]
    assert j["rows"][2] == {"m": 0.975, "pct": 97.5, "strike": 97.5} and j["spot_row"] == 3
    for row in j["cells"]:
        assert row[0] is None and row[2] is None and row[5] is None and row[9] is None   # nothing extrapolated
        assert row[1] == pytest.approx(want, abs=1e-9)               # a flat smile stays flat by strike
    b = j["brackets"][2][1]                                            # 97.5: between 90 and 100, 44d and 72d
    assert b["lo"]["date"] == "2026-11-20" and b["hi"]["date"] == "2026-12-18" and b["w"] == pytest.approx(w)
    assert b["k_lo"]["strike"] == 90.0 and b["k_hi"]["strike"] == 100.0 and b["wk"] == pytest.approx(0.25)
    assert j["nearest"][2][1] == {"date": "2026-12-18", "dte": 72, "strike": 100.0}
    assert j["exact"][3] == [False] * 10 and j["exact_k"][3][1] is True   # 100 %: a listed strike, not a listed expiry
    x = j["extra"][0][1]                                               # 90 %: the listed 90 put
    assert set(x) == {"d", "g", "t", "v", "price"} and x["d"] == pytest.approx(-0.55) and x["price"] > 0
    # 97.5 % is a put: the 100 call's delta (0.45) is restated as the 100 put's by parity (0.45 - 1)
    assert j["extra"][2][1]["d"] == pytest.approx(-0.55 * 0.25 + (0.45 - 1.0) * 0.75)
    assert "linear in total variance" in j["method"] and "linear in strike" in j["method"]
    # the by-strike grid rides along (the page's "Rows: strikes" needs no second request)
    bs_ = j["by_strike"]
    assert bs_["strikes"] == [90.0, 100.0, 110.0] and bs_["spot_row"] == 1
    assert bs_["nearest"][0][1] == {"date": "2026-12-18", "dte": 72}   # 12 days from 72, 16 from 44
    assert bs_["cells"][0][1] == pytest.approx(want, abs=1e-9)
    k = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "rows": "strike"}).json()
    assert k["ok"] is True and k["rows_mode"] == "strike" and k["rows"] is None and k["strikes"] == [90.0, 100.0, 110.0]
    assert k["cells"] == bs_["cells"]
    m = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "rows": "moneyness"}).json()
    assert m["ok"] is True and m["rows_mode"] == "moneyness"
    r = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "rows": "bad"})
    assert r.status_code == 400 and r.json()["ok"] is False and "rows" in r.json()["error"]
    # v4.175: the Expiries setting (all / monthly) does not reach this surface - every listed expiry is a leg
    mo = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "exp": "monthly"}).json()
    assert mo["ok"] is True and mo["exp_mode"] == "all" and mo["cells"] == j["cells"]
    one = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "horizons": "60, 60"}).json()
    assert one["ok"] is True and [h["days"] for h in one["horizons"]] == [60]
    for bad in ("abc", "0", "-30", "12.5", "60,x"):
        r = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "horizons": bad})
        assert r.status_code == 400 and r.json()["ok"] is False and "horizon" in r.json()["error"], bad
    none = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "horizons": "30,365"}).json()
    assert none["ok"] is False and "bracket" in none["error"]
    assert client.get("/options/api/surface/cm", params={"symbol": "NOPE"}).json()["ok"] is False
    j = client.get("/options/api/surface/cm", params={"symbol": "AAPL", "day": "2026-10-02"}).json()
    assert j["ok"] is False and "contract history" in j["error"]


def test_the_page_has_the_two_tabs_and_the_horizons(client, fake):
    """v4.171 (user: "put the current Vol Surface into a tab called Volatility Surface ... duplicate it into a
    new tab called Interpolated Volatility Surface. Both surfaces will reuse the left hand panel"): two tabs
    over the right panel (remembered in th.vol.tab, ?tab=cm), the Horizons checkboxes in the shared left
    panel (all six ticked, th.vol.horizons, shown on the interpolated tab only), the interpolated grid's own
    ids, the exact mark, and a click on a cell opening the NEARER bracketing contract."""
    html = client.get("/options", params={"tab": "cm"}).text
    js = html[html.index("function cmMoneyWork("):]
    for needle in ('id="volTabSurface" role="tab" data-vtab="surface" aria-selected="true"',
                   'id="volTabCm" role="tab" data-vtab="cm" aria-selected="false"',
                   ">Volatility Surface</button>", ">Interpolated Volatility Surface</button>",
                   'id="volPanelCm"', 'id="cmInfoRow"', 'id="cmInfo"', 'id="cmMsg"', 'id="cmWrap"', 'id="cmTable"',
                   'id="cmPop"', 'id="cmLegend"', 'id="volHz"', "'th.vol.tab'", "'th.vol.horizons'",
                   "get('tab')", "'/options/api/surface/cm?' + q.toString()",
                   "if (TAB === 'cm') { runCm(quiet); return; }",
                   "show($('volHz'), TAB === 'cm');",
                   "a listed expiry falls on this horizon - this is that contract\\'s own IV",
                   "openContract(j.symbol, nearest.date, right, nearest.strike, day)",
                   "weighted by time (linear in total variance) = <b>", "') falls on ' + hz.days + 'd - its own IV",
                   "model price",
                   "'d</th>'", "linear in total variance · legs: every listed expiry at least 7 days out "
                               "(the Expiries setting applies to the other tab)",
                   "rows at fixed moneyness (strike / price), interpolated by strike",
                   "Tick at least one horizon above the grid (30 days to 2 years)."):
        assert needle in html, needle
    # v4.175: out to two years, every horizon ticked by default; the new ones say their months
    for h in (30, 60, 90, 180, 270, 365, 456, 547, 638, 730):
        assert 'id="volHz%d" data-h="%d" checked' % (h, h) in html, h
    assert '<label class="scr-check" title="15 months"><input type="checkbox" id="volHz456"' in html
    assert '<label class="scr-check" title="24 months (2 years)"><input type="checkbox" id="volHz730"' in html
    assert "var HZ_ALL = [30, 60, 90, 180, 270, 365, 456, 547, 638, 730];" in html
    assert "456: '15 months'" in html and "(HZ_MONTHS[d] ? HZ_MONTHS[d] + ' - ' : '')" in html
    # a member's remembered choice from before keeps the new horizons ticked ("chosen|offered")
    hzl = html[html.index("function hzLoad("):html.index("function hzSave(")]
    assert "seen.indexOf(h) < 0 || want.indexOf(h) >= 0" in hzl and "HZ_OLD" in hzl
    assert "lsSet(HZ_KEY, hzChosen().join(',') + '|' + HZ_ALL.join(','))" in html
    # v4.174: the horizons sit above the interpolated grid, inside its tab (not in the left panel's form)
    form = html[html.index('id="volForm"'):html.index('id="volGo"')]
    assert 'id="volHz"' not in form
    cm = html[html.index('<section id="volPanelCm"'):html.index('id="cmTable"')]
    assert cm.index('id="volHz"') < cm.index('id="cmRows"') < cm.index('id="cmInfoRow"')
    order = [html.index(x) for x in ('id="volTabs"', 'id="volPanelSurface"', 'id="volPanelCm"')]
    assert order == sorted(order)
    # v4.175: the rows toggle - moneyness (default) or strikes, remembered, repainting without a request
    assert '<input type="radio" name="cmRows" id="cmRowsM" value="moneyness" checked> moneyness' in cm
    assert '<input type="radio" name="cmRows" id="cmRowsK" value="strike"> strikes' in cm
    assert "var ROWS_KEY = 'th.vol.cmrows';" in html
    rows = html[html.index("$('cmRows').addEventListener('change'"):]
    rows = rows[:rows.index("});") + 3]
    assert "lsSet(ROWS_KEY, cmRowsMode())" in rows and "renderCm(CM.last)" in rows and "getJSON" not in rows
    assert "Object.assign({}, j, j.by_strike, {rows: null, rows_mode: 'strike'})" in html
    # the row label "97.5% · 327.11", its title, and the 100% row as the at-the-money line
    assert "function mText(row) { return pctText(row.pct) + '% \\u00B7 ' + num(row.strike, 2); }" in html
    rcm0 = html[html.index("function renderCm("):html.index("function cmMoneyWork(")]
    assert "'strike / price = ' + pctText(row.pct) + ' % - strike ' + num(row.strike, 2) + ' at price ' + spot" in rcm0
    assert "'at the money - price ' + spot" in rcm0 and "' class=\"vol-atm\"'" in rcm0
    assert "var spotDone = atmShown;" in rcm0 and ".vol-grid tr.vol-atm > th" in html
    # the hover card: the strike step after the time step
    assert "' % = strike ' + esc(num(row.strike, 2)) + ': between '" in js and "'), by strike = <b>'" in js
    # the click: the nearer listed expiry AND the nearer listed strike, the cell's own right
    click = html[html.index("$('cmTable').addEventListener('click'"):]
    click = click[:click.index("openContract(") + 80]
    assert "nearest = cmNear(g, r, c)" in click and "var right = sideOf(g, k);" in click
    assert "nearest = Object.assign({}, nearest, {strike: k})" in click
    # the cm request never carries the Expiries setting (all legs, always)
    cmp = html[html.index("function cmParams("):html.index("function syncTick(")]
    assert "exp:" not in cmp and "dte" not in cmp.split("return")[1]
    assert '<section id="volPanelCm" class="hidden' in html
    # the interpolated tiles: the bottom line is the MODEL price (last / OI / volume do not exist at a horizon)
    cmb = html[html.index("function cmBottom("):html.index("function cmView(")]
    assert "TILE.b === 'last' || TILE.b === 'price'" in cmb and "x.price" in cmb
    # no past-year rank on the interpolated grid: tint (v4.172) and the slim bottom bar (v4.176) both come
    # from the cell's level within this surface, never from cellStyle / the other tab's rank
    rcm = html[html.index("function renderCm("):html.index("function cmMoneyWork(")]
    assert "rankColor" not in rcm and "rankBar" not in rcm and "cellStyle" not in rcm and "cmSpotRow(j" in rcm
    assert "cmTint(j, v)" in rcm and "'</div>' + cmBar(j, v) + '</td>'" in rcm
    bar = html[html.index("function cmBar("):html.index("function renderCm(")]
    assert "var l = cmLevel(j, v); return l == null ? '' : rankBar(l);" in bar
    assert "the bar under each figure shows the same level" in html
    assert 'class="vol-x"' in rcm and ".vol-grid .vol-x { position: absolute;" in html
    assert "'<div class=\"vol-l2\">' + tileTop(x) + '</div><div class=\"vol-iv\">'" in rcm
    # a tab switch draws the newly shown tab only when its last answer is for other settings / an older pass
    stale = html[html.index("function drawIfStale("):html.index("function tabSet(")]
    assert "S.want !== want || older" in stale
    # the tiles repaint both grids
    paint = html[html.index("function paintTiles("):html.index("function figsHtml(")]
    assert "['#cmTable', cmGrid(CM.last), cmBottom]" in paint and "getJSON" not in paint


def test_the_hide_new_and_thinly_listed_tick(client, fake):
    """v4.171 (user, on SNDK): "Hide new and thinly listed contracts" under Common strikes, on by default,
    remembered in th.vol.clean: an expiry whose cells mostly have under 10 days of history is left out (once
    the history is in), then every strike with a gap; the info row says how many; the tick repaints both
    tabs from the answers on hand."""
    html = client.get("/options").text
    assert '<input type="checkbox" id="volClean" checked> Hide new and thinly listed contracts' in html
    assert ("Leaves out expiries listed less than 10 days ago, which have no history to colour yet, and strikes "
            "that only some expiries carry. The grid then shows no grey cells and no gaps. On a past day only the "
            "contracts that traded are on file, so an empty cell means no trade - there a strike stays while at "
            "least half of the expiries have a value. Untick to see everything.") in html
    form = html[html.index('id="volForm"'):html.index('id="volGo"')]
    assert form.index('id="volCommon"') < form.index('id="volClean"')
    assert "'th.vol.clean'" in html and "var MIN_HIST = 10;" in html
    young = html[html.index("function youngCols("):html.index("function surfaceView(")]
    # v4.175: a column goes when fewer than half of its cells are RANKED in the chosen rank window
    assert "s.rank != null" in young and "old * 2 < n" in young and "sameGrid(j, hs)" in young
    assert "'|' + winChosen()" in html[html.index("function youngKey("):html.index("function youngCols(")]
    # the interpolated tab: a horizon goes only when fewer than half of the rows WITH ANY VALUE reach it
    cv = html[html.index("function cmView("):html.index("function cmLevel(")]
    assert "got * 2 < live" in cv and "j.cells[r].some(function (v) { return v != null; })" in cv
    td = html[html.index("function tidy("):html.index("function fullView(")]
    assert "cols.every(function (c) { return cells[r][c] != null; })" in td
    # v4.181 (user: "a liquidity indicator on the tiles ... on/off button ... right edge bar ... and fading"): the
    # Volatility Surface tab only - a 1.5 px soft-slate bar on the right edge, the thinnest quarter faded, a
    # "Show liquidity" tick (th.vol.liq), the figures in the hover card; nothing on the Interpolated tab
    assert '<input type="checkbox" id="volLiq" checked> Show liquidity' in html
    # v4.182 (user: "a fuel gauge ... a side edge with a pointer ... pointing the edge not inside ... 3px with dark
    # blue"): a 3 px dark-blue track, the fill from the bottom, an arrow inside the tile pointing out at the level
    # v4.183 (user: "why some edge bar looks bolder and some thinner?"): the gauge is the cell's own background-image
    # (snaps with the cell edge - the same 3 px in every column), the tint a background-COLOR beneath it, the
    # pointer an arrow inside the tile; the colour's repaint keeps --lq
    assert ".vol-grid td.vol-cell[data-lq] {" in html and "background-size: 3px var(--lq, 0%), 3px 100%;" in html
    assert "background-position: right bottom, right top; background-repeat: no-repeat; }" in html
    assert "border-left: 5px solid rgba(30,64,175,.9); }" in html and ".vol-grid.vol-nolq td.vol-cell[data-lq] { background-image: none; }" in html
    assert "'background-color:hsla(' + rankHue(rank) + ',70%,45%,.35)'" in html and "var GREY = 'background-color:hsla(0,0%,50%,.18)';" in html
    assert "' data-lq=\"' + p + '\" style=\"--lq:' + p + '%\"'" in html
    assert "td.style.cssText = cellStyle(s) + liqVar(td);" in html
    lb = html[html.index("function liqBar("):html.index("function liqWord(")]
    assert "'<b class=\"vol-lq-pt\" style=\"top:' + (100 - p) + '%\"></b>'" in lb
    assert "'\"' + liqAttr(x) + ' data-r=\"' + r + '\"" in html[html.index("function renderSurface("):html.index("function tileLoad(")]
    assert ".vol-grid td.vol-cell.vol-thin { opacity: .38; }" in html and ".vol-grid.vol-nolq .vol-lq-pt { display: none; }" in html
    assert "var LIQ_KEY = 'th.vol.liq', LIQ_THIN = 0.25;" in html
    rs = html[html.index("function renderSurface("):html.index("function tileLoad(")]
    assert "'<td class=\"vol-cell' + (liqThin(x) ? ' vol-thin' : '') + '\"" in rs and "+ liqBar(x) + '</td>'" in rs
    assert "$('volTable').classList.toggle('vol-nolq', !liqOn());" in rs
    lq = html[html.index("function liqHtml("):html.index("function figsHtml(")]
    assert "Liquidity is on the latest pass only" in lq
    # v4.184 (user: "in the tooltip i need xx/xx of the liquidity ranking"): rank / chain size in the chip, 1 = deepest
    assert "intText(x.rk) + ' / ' + intText(j.liq_n)" in lq and "(rk ? ' · rank ' + rk : '')" in lq
    assert "(1 = the deepest)" in lq
    # v4.189 (user: "when the interface is toggled to dark, the chart background is still white"): the toggle in
    # base.html tells the page, and the charts repaint in place
    assert "document.dispatchEvent(new CustomEvent('th:theme', {detail: {light: light}}));" in html
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[1] / "app" / "templates"
    pc = (tpl / "_price_chart.html").read_text(encoding="utf-8")
    assert "document.addEventListener('th:theme', onTheme);" in pc and "document.addEventListener('th:theme', onThemeDraw);" in pc
    assert "chart.applyOptions({ layout: { background: { color: l ? '#ffffff' : '#0b1220' }, textColor: l ? '#334155' : '#94a3b8' } });" in pc
    assert "if (!el.isConnected) { document.removeEventListener('th:theme', onTheme); return; }" in pc
    assert "IDLE_FG = light ? '#334155' : '#cbd5e1';\n      try { updateBtns(); } catch (e) {}\n      try { redraw(); } catch (e) {}" in pc
    pd = (tpl / "pattern_detail.html").read_text(encoding="utf-8")
    assert "layout: { background: { color: _lightPD ? '#ffffff' : '#0b1220' }, textColor: _lightPD ? '#334155' : '#94a3b8' }," in pd
    assert "document.addEventListener('th:theme', function (ev) {" in pd
    # v4.186: the watchlist pane's per-ticker verdict (Liquid / Fair / Thin) from /options/api/liquidity
    wp = html[html.index("function wlPaint("):html.index("function wlLoad(")]
    assert "'<span class=\"vol-wl-lq text-[10px] whitespace-nowrap text-slate-600 ml-auto\" data-sym=\"' + esc(r.symbol) + '\"></span>'" in wp
    assert "wlLiq(l.symbols.map(function (r) { return r.symbol; }));" in wp
    assert "getJSON('/options/api/liquidity?symbols=' + encodeURIComponent(syms.join(',')))" in wp
    assert "var LQ_CLS = {liquid: 'text-emerald-300', fair: 'text-amber-300', thin: 'text-slate-500'};" in wp
    assert "el.textContent = (v.num && v.num !== '-' ? v.num + ' ' : '') + v.label;" in wp   # v4.187: the near OI, then the verdict
    # v4.190 (user: "the liquidity hover tooltip also need to be the same with the vol surface page"): the table popover
    assert '<div id="volLqPop" class="vol-pop hidden" role="tooltip" style="width:19rem"></div>' in html
    # v4.191 (user: the pane "does not show liquidity tooltip table"): the popover sits OUTSIDE both tab panels -
    # inside the hidden Interpolated panel it could never show on the Surface tab
    cm_end = html.index("</section>", html.index('<section id="volPanelCm"'))
    assert html.index('id="volLqPop"') > cm_end
    lt = html[html.index("function lqTipHtml("):html.index("function lqPopPlace(")]
    assert "<th>Fair &ge;</th><th>Liquid &ge;</th>" in lt and "lq-name" in lt and "(LQ_VAL[f.level] || '')" in lt
    assert "Near the money = ' + esc(v.near_words || '')" in lt
    assert "var row = e.target.closest ? e.target.closest('.vol-wl-row') : null, v = row && LQV[row.dataset.sym];" in html
    assert "pop.innerHTML = lqTipHtml(row.dataset.sym, v);" in html and ".vol-pop .lq-tbl { width: 100%;" in html
    # v4.185: the hover card's liquidity bar in the edge gauge's blue
    assert ".vol-pop .vol-pos > i.vol-pos-lq { background: rgba(30,64,175,.72); }" in html
    assert ".vol-pop .vol-pos.vol-pos-grey { background: rgba(30,58,138,.16); }" in html
    assert "Turnover (volume / open interest)" in lq and "Last trade vs model price" in lq
    assert "+ liqHtml(j, xf);" in html and 'id="volLegLiq"' in html
    assert "show($('volLegLiq'), liqOn() && !!V.last && V.last.source === 'latest');" in html
    rcm_ = html[html.index("function renderCm("):html.index("function cmMoneyWork(")]
    assert "liqBar" not in rcm_ and "vol-thin" not in rcm_
    # v4.179 (user, AMD on 2026-10-13: "only 2 strike listed"): a past day's grid holds only the contracts that
    # traded, so a gap is no trade - a row stays while half of the shown expiries have a value; the latest pass
    # keeps the every-column rule
    assert "function tidy(cells, nCols, dropCol, minShare)" in td
    assert "if (minShare == null) return cols.every(" in td and "return got >= Math.ceil(cols.length * minShare);" in td
    sv = html[html.index("function surfaceView("):html.index("function tidyNote(")]
    assert "tidy(j.cells || [], n, youngCols(j), j.source === 'latest' ? null : 0.5)" in sv
    assert "an empty cell: that contract did not trade that day" in html
    assert "' hidden by the tick'" in html and "tidyNote(vw, 'expiry', 'expiries')" in html
    assert "tidyNote(vw, 'horizon', 'horizons')" in html and "tidyNote(vw, 'horizon', 'horizons', 'row', 'rows')" in html
    # the history's arrival redraws the tidy grid; the tick repaints without a request
    assert "if (cleanOn()) renderSurface(j); else paintHistory(j, h);" in html
    tick = html[html.index("$('volClean').addEventListener('change'"):]
    tick = tick[:tick.index("});") + 3]
    assert "renderSurface(V.last)" in tick and "renderCm(CM.last)" in tick and "getJSON" not in tick


def test_the_rank_window_api(client, fake, screener_url, monkeypatch):
    """v4.175: GET /options/api/surface/history?window=N - one rank window for the whole surface, in
    calendar days (30 / 60 / 90 / 180 / 270 / 365, default 365); a bad value is a 400."""
    from app.routes import options_page

    seen = []

    def fake_history(s, symbol, day, window_days=None, **o):
        seen.append(window_days)
        return {"cells": {}, "days": 0, "window": None, "window_days": window_days}

    monkeypatch.setattr(options_page, "_surface_history", fake_history)
    j = client.get("/options/api/surface/history", params={"symbol": "AAPL"}).json()
    assert j["ok"] is True and j["window_days"] == 365 and seen[-1] == 365
    j = client.get("/options/api/surface/history", params={"symbol": "AAPL", "window": "90"}).json()
    assert j["ok"] is True and j["window_days"] == 90 and seen[-1] == 90
    for bad in ("45", "abc", "0", "400", "90.5"):
        r = client.get("/options/api/surface/history", params={"symbol": "AAPL", "window": bad})
        assert r.status_code == 400 and r.json()["ok"] is False and "window" in r.json()["error"], bad
    assert len(seen) == 2


def test_the_page_has_the_rank_window(client, fake):
    """v4.175 (user: a contract's colour must be over the same span for every cell): a radio row at the top
    of the Volatility Surface tab - 30 ... 365 days, 365 by default, remembered in th.vol.window; a change
    re-fetches the history only (no new surface) and repaints; the legend and the hover card name the window."""
    html = client.get("/options").text
    panel = html[html.index('<section id="volPanelSurface"'):html.index('id="volTable"')]
    assert panel.index('id="volWin"') < panel.index('id="volInfoRow"')
    assert "Rank window (days):" in panel
    for w in (30, 60, 90, 180, 270):
        assert '<input type="radio" name="volWin" id="volWin%d" value="%d">' % (w, w) in panel, w
    assert '<input type="radio" name="volWin" id="volWin365" value="365" checked>' in panel
    assert "var WIN_KEY = 'th.vol.window', WINS = [30, 60, 90, 180, 270, 365];" in html
    assert '<span id="volLegWin">IV vs own past 365 days:</span>' in html
    assert "'IV vs own past ' + winChosen() + ' days:'" in html
    # the history request carries the window; a change re-fetches the history only
    assert "getJSON('/options/api/surface/history?' + histParams(q).toString())" in html
    assert "h.set('window', String(winChosen()));" in html
    ch = html[html.index("$('volWin').addEventListener('change'"):]
    ch = ch[:ch.index("});") + 3]
    assert "lsSet(WIN_KEY, String(winChosen()))" in ch and "loadHistory(V.last, V.histQ, V.seq)" in ch
    assert "/options/api/surface?" not in ch and "runSurface" not in ch
    assert "hseq !== V.histSeq" in html                                   # the last window picked wins
    # the hover card: the window in words, the percentile over it, the reason a cell stays grey
    pop = html[html.index("function popoverHtml("):html.index("function showPop(")]
    assert "'<div class=\"vol-pop-row\"><span>Past ' + wd + ' days, ' + intText(s.n) + ' sessions'" in pop
    assert "' sits in that ' + wd + '-day range</span>" in pop
    assert "No colour: ' + esc(s.why || 'under 10 days of history')" in pop
    # v4.180 (user: "i need a colour progress bar to show where it sit today"): the bar under that row, low to
    # high of the window, filled to today's position in its hue
    assert "posBar(s.rank, s.lo, s.hi) +" in pop
    pb = html[html.index("function posBar("):html.index("function sideOf(")]
    assert "'<div class=\"vol-pos\"><i style=\"width:' + pct + '%;background:hsla(' + rankHue(rank) + ',70%,45%,.9)\"></i>'" in pb
    assert "'<b style=\"left:' + pct + '%\"></b></div>'" in pb and "vol-pos-ends" in pb
    assert ".vol-pop .vol-pos { position: relative; height: 6px;" in html
    cmp_ = html[html.index("function cmPopHtml("):html.index("function placePop(")]
    assert "posBar(lv, j.scale ? j.scale[0] : null, j.scale ? j.scale[1] : null, 'lowest', 'highest')" in cmp_
