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
    with screener_db.session() as s:
        s.add(ScrStatus(id=1, state="pass", detail="market pass", heartbeat=now, symbols_total=4512,
                        symbols_done=1200, universe_n=4512, history_done_n=1204, history_total=4512))
        s.add(ScrPass(kind="cycle", session="2026-10-09", started=now - _dt.timedelta(minutes=30),
                      finished=now - _dt.timedelta(minutes=20), n_symbols=4500, n_ok=4490, n_contracts=990_000))
        s.add(ScrPass(kind="cycle", session="2026-10-09", started=now - _dt.timedelta(minutes=5)))
        s.commit()
    j = client.get("/options/api/status").json()
    assert j["dot"] == "green" and "1,200 / 4,512" in j["label"]
    assert "last market pass" in j["line"] and "IV history 1,204 / 4,512" in j["line"]
    assert j["last_pass"]["n_contracts"] == 990_000 and j["last_pass"]["finished"].endswith("Z")
    assert j["running_pass"]["id"] == j["last_pass"]["id"] + 1
    assert "1.02M contracts" in j["line"]             # the loaded frame's count wins over the pass's
    assert j["heartbeat"].endswith("Z") and j["heartbeat_age_s"] < 120


def test_status_dot_rules():
    v = options_page._status_view
    tue_noon = _dt.datetime(2026, 10, 6, 16, 0)            # 12:00 ET, a trading day
    sun_noon = _dt.datetime(2026, 10, 4, 16, 0)            # a Sunday
    col = lambda **st: {"available": True, "status": {"state": "idle", **st}, "last_pass": None,  # noqa: E731
                        "running_pass": None}
    assert v({}, now=tue_noon)["dot"] == "slate"
    assert v(col(heartbeat=tue_noon - _dt.timedelta(minutes=2)), now=tue_noon)["dot"] == "green"
    assert v(col(heartbeat=tue_noon - _dt.timedelta(minutes=11)), now=tue_noon)["dot"] == "amber"
    assert v(col(heartbeat=sun_noon - _dt.timedelta(minutes=40)), now=sun_noon)["dot"] == "green"
    assert v(col(heartbeat=sun_noon - _dt.timedelta(hours=4)), now=sun_noon)["dot"] == "amber"
    assert v(col(state="stopped", heartbeat=tue_noon), now=tue_noon)["dot"] == "amber"
    err = v(col(state="error", last_error="plan has no options", heartbeat=tue_noon), now=tue_noon)
    assert err["dot"] == "rose" and "plan has no options" in err["label"]
    assert v({}, now=tue_noon)["line"].startswith("Massive · 15-min delayed · prices estimated from IV · no market pass yet")


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
