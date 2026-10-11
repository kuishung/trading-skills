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


def _goto_options(html: str) -> list[str]:
    m = re.search(r'<select id="scrGoto".*?</select>', html, re.S)
    assert m
    return re.findall(r'<option value="([^"]+)"', m.group(0))


# ───────────────────────────────────────── the page ─────────────────────────────────────────

def test_the_page_renders_with_go_to_and_boot(client, fake):
    r = client.get("/options")
    assert r.status_code == 200
    html = r.text
    assert 'id="optPage"' in html and 'id="scrTitle"' in html
    assert ">Options Screener<" in html
    assert len(_goto_options(html)) == 33
    assert html.count("<optgroup") == len(FAMILIES)
    assert "Set filters" in html and "Results" in html and "Add a filter" in html
    assert "Massive · 15-min delayed · prices estimated from IV" in html
    boot = _boot(html)
    assert boot["screen"] == "options-screener" and boot["engine_ok"] is True
    spec = boot["spec"]
    assert [f["key"] for f in spec["fields"]] == ["dte", "right", "volume", "weekly", "expiry"]
    assert spec["groups"] == ["Option Info", "Price & Volume"]
    dte = spec["fields"][0]
    assert dte["presets"] == [{"label": "< 60", "hi": 60, "op": "lte"},
                              {"label": "60-100", "lo": 60, "hi": 100, "op": "between"}]
    assert spec["fields"][1]["choices"] == [{"v": "C", "label": "Call"}, {"v": "P", "label": "Put"}]
    exp = spec["fields"][4]
    assert exp["ops"] == ["in", "eq", "between", "within", "gte", "lte"] and exp["within"] == "days"
    assert exp["presets"] == [{"label": "7 days", "v": 7, "op": "within"}]
    assert spec["fields"][3]["ops"] == [] and "within" not in spec["fields"][3]
    assert spec["defaults"]["filters"][2] == {"f": "volume"}
    assert spec["defaults"]["sort"] == {"col": "volume", "dir": "desc"}
    assert [v["key"] for v in spec["views"]] == ["main", "greeks"]
    assert spec["views"][1]["columns"] == [{"key": "delta", "label": "delta", "unit": "", "fmt": ""}]
    assert boot["chart_url"] == "/sector/chart-window?symbol={sym}"
    assert boot["per_page"] == 100 and boot["saved"] == [] and boot["default_id"] is None
    # the old blank page and the v2 page are gone
    for gone in ("optBasket", "optStatus", "/options/rules", "/options/basket", "/options/payoff"):
        assert gone not in html, gone


def test_the_screen_param_picks_the_screen_and_unknown_falls_back(client, fake):
    html = client.get("/options?screen=bull-put-spread").text
    assert ">Bull Put Spread<" in html and _boot(html)["screen"] == "bull-put-spread"
    assert 'value="bull-put-spread" selected' in html
    for bad in ("nope", "../../etc", "OPTIONS-SCREENER<script>"):
        assert _boot(client.get("/options", params={"screen": bad}).text)["screen"] == "options-screener"


def test_the_members_default_saved_screener_loads(client, fake, db, user):
    row = models.OptionScreen(user_id=user.id, screen_key="long-call", name="Mine",
                              payload={"filters": [{"f": "dte", "op": "lte", "hi": 30}]}, is_default=True)
    db.add(row)
    db.commit()
    boot = _boot(client.get("/options?screen=long-call").text)
    assert boot["default_id"] == row.id
    assert boot["saved"][0]["payload"]["filters"] == [{"f": "dte", "op": "lte", "hi": 30}]
    assert _boot(client.get("/options?screen=long-put").text)["default_id"] is None


def test_the_symbol_link_follows_the_members_menus(client, fake, db, user):
    u = db.get(models.User, user.id)
    u.menu_access = ["options", "matp"]
    db.commit()
    assert _boot(client.get("/options").text)["chart_url"] == "/matp?symbol={sym}"
    u.menu_access = ["options"]
    db.commit()
    assert _boot(client.get("/options").text)["chart_url"] == ""


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


def test_the_page_reads_the_frame_once_when_the_spec_carries_it(client, fake, monkeypatch):
    """An empty frame makes each frame read wait up to 3 s for a reload: the page takes the
    frame summary from the spec it just built instead of reading the frame a second time."""
    real_spec = fake.spec

    def spec_with_data(key):
        return dict(real_spec(key), data={"n_contracts": 0, "empty": True, "reloading": True})

    def no_second_read():
        raise AssertionError("the frame was read a second time")

    monkeypatch.setattr(fake, "spec", spec_with_data)
    monkeypatch.setattr(fake, "frame_meta", no_second_read)
    boot = _boot(client.get("/options").text)
    assert boot["status"]["frame"] == {"n_contracts": 0, "empty": True, "reloading": True}    # the spec's
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
    assert e["title"] == ("New market data is loading into the screener (about 10 seconds) - the results refresh "
                          "by themselves.")
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
              "scrNotice", "scrNoticeBtn", "scrDataState"):
        assert f'id="{i}"' in html, i
    boot = _boot(html)
    assert boot["status"]["dot"] == "slate" and boot["status"]["empty"]["title"]
    assert boot["no_data"] == options_page._no_data_text()
    assert "setInterval(pollStatus" not in html and "schedulePoll(pollDelay" in html
    # a first pass grows on screen even under an older finished pass that read nothing (frame real_pass_id)
    assert "function firstPassRunning(m)" in html and "m.real_pass_id == null" in html
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
    assert 'id="scrEngineErr"' in r.text and "numpy" in r.text
    boot = _boot(r.text)
    assert boot["engine_ok"] is False and "numpy" in boot["engine_error"]
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
    assert len(_goto_options(html)) == 33 and _boot(html)["engine_ok"] is True
    boot = _boot(html)
    # v4.137: the spec says it came from an empty market (the page reloads it once data lands),
    # the status carries the frame's reloading flag, and the empty panel is painted on the server
    assert boot["spec"]["data"]["empty"] is True and boot["status"]["frame"]["reloading"] is False
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
        ("/options/api/screens", ("GET",)),
        ("/options/api/spec", ("GET",)),
        ("/options/api/run", ("POST",)),
        ("/options/api/csv", ("POST",)),
        ("/options/api/status", ("GET",)),
        ("/options/api/saved", ("GET",)),
        ("/options/api/saved", ("POST",)),
        ("/options/api/saved/{sid}/default", ("POST",)),
        ("/options/api/saved/{sid}/delete", ("POST",)),
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

    assert ("options", "Options", None, "/options") in menus.MENUS


def test_a_payload_without_filters_means_the_screen_defaults():
    """No "filters" key = the screen's defaults (the engine's rule, §5); [] = no filters."""
    from app.routes import options_page as op

    clean, err = op._clean_payload({"sort": {"col": "volume", "dir": "desc"}})
    assert err is None and "filters" not in clean and clean["sort"] == {"col": "volume", "dir": "desc"}
    clean, err = op._clean_payload({"filters": []})
    assert err is None and clean["filters"] == []
    clean, err = op._clean_payload(None)
    assert err is None and "filters" not in clean
