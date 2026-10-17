"""Settings -> Data Ingest -> Option Calculator (v4.151): the view model of the Hermes lane
(``services/calc_health``) and the tab + fragment on the Data Ingest page."""
from __future__ import annotations

import datetime as _dt
import json

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.security import current_user
from app.services import calc_health, calc_worker

NOW = _dt.datetime(2026, 10, 8, 18, 33)          # naive UTC = 14:33 ET (EDT)
STATE = {"version": "1.0", "pid": 4242, "heartbeat": "2026-10-08T18:32:40+00:00", "state": "idle",
         "detail": "up to date with pass 812 · 2 active setups", "last_look": "2026-10-08T18:32:40+00:00",
         "next_look": "2026-10-08T18:33:40+00:00", "last_pass_id": 812, "last_pass_session": "2026-10-08",
         "last_pass_finished": "2026-10-08T18:31:00+00:00", "requests_active": 2, "requests_due": 0,
         "done": 20, "failed": 1, "last_run_ms": 120, "last_error": None,
         "computed_at": "2026-10-08T18:32:05+00:00", "written_by": "option_calc_worker"}


@pytest.fixture(autouse=True)
def state_path(tmp_path, monkeypatch):
    """The lane's state file - a temp path (absent unless a test writes it), never this PC's real
    dashboard_tst/state/option_calc_worker.json."""
    p = tmp_path / "state" / "option_calc_worker.json"
    monkeypatch.setattr(calc_worker, "STATE_PATH", p)
    return p


def _write(p, doc):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def setups(db):
    """A member with three saved setups: two active (one whose last computation failed), one
    switched off; two results, one older than a day."""
    u = models.User(email="member@local.test", display_name="Member", role=models.ROLE_MEMBER,
                    status=models.APPROVED, created_at=_dt.datetime(2026, 1, 5))
    db.add(u)
    db.flush()
    ok = models.OptionCalcRequest(user_id=u.id, symbol="AAPL", kind="pair", target=200, worst=180)
    bad = models.OptionCalcRequest(user_id=u.id, symbol="MSFT", kind="manual", target=450, worst=420,
                                   legs=[], last_error="the 2026-11-20 450 call is no longer listed")
    off = models.OptionCalcRequest(user_id=u.id, symbol="TSLA", kind="pair", target=300, worst=250, active=False,
                                   last_error="old")
    db.add_all([ok, bad, off])
    db.flush()
    db.add_all([models.OptionCalcResult(request_id=ok.id, pass_id=812, family="vertical", rank=1, legs=[],
                                        figures={}, created_at=_dt.datetime(2026, 10, 8, 18, 30)),
                models.OptionCalcResult(request_id=ok.id, pass_id=700, family="vertical", rank=1, legs=[],
                                        figures={}, created_at=_dt.datetime(2026, 10, 6, 18, 30))])
    db.commit()
    return u


def test_view_without_a_state_file_says_it_is_not_running(db):
    v = calc_health.view(NOW, db=db)
    assert (v["known"], v["state"], v["word"], v["tone"]) == (False, "absent", "not running here", "amber")
    assert "has not reported on this server" in v["detail"] and "TST-Option-Calc" in v["detail"]
    assert v["requests_active"] == 0 and v["results_24h"] == 0 and v["newest_result"] is None
    assert v["computed_at"] is None and v["db_error"] is None


def test_view_with_a_fresh_idle_file_and_the_counts(db, setups, state_path):
    _write(state_path, STATE)
    v = calc_health.view(NOW, db=db)
    assert (v["known"], v["state"], v["word"], v["tone"], v["stale"]) == (True, "idle", "up to date", "emerald", False)
    assert v["detail"] == "up to date with pass 812 · 2 active setups" and v["age"] == "20 s ago"
    assert v["computed_at"] == "Thu 2026-10-08 14:32 ET (55 s ago)"
    assert v["last_pass_id"] == 812 and v["last_pass_finished"].startswith("Thu 2026-10-08 14:31 ET")
    assert (v["requests_active"], v["requests_errors"], v["results_24h"]) == (2, 1, 1)
    assert v["newest_result"] == "Thu 2026-10-08 14:30 ET (3 min ago)"
    assert (v["done"], v["failed"], v["last_run_ms"], v["last_error"]) == (20, 1, 120, None)
    assert v["now_et"] == "2026-10-08 14:33 ET"


def test_view_with_a_stale_file(db, state_path):
    _write(state_path, dict(STATE, heartbeat="2026-10-08T18:26:00+00:00"))
    v = calc_health.view(NOW, db=db)
    assert (v["state"], v["word"], v["tone"], v["stale"]) == ("stale", "not running", "amber", True)
    assert v["detail"] == "The option calculator lane last reported 7 min ago - is the TST-Option-Calc task running?"
    assert v["next_look"] is None


def test_view_with_an_error(db, state_path):
    _write(state_path, dict(STATE, state="error", detail="the look failed; again in 60 s",
                            last_error="OperationalError:   database is locked"))
    v = calc_health.view(NOW, db=db)
    assert (v["state"], v["word"], v["tone"]) == ("error", "problem", "rose")
    assert v["detail"] == "the look failed; again in 60 s"
    assert v["last_error"] == "OperationalError: database is locked"


def test_view_never_raises_on_a_broken_file_or_database(state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json", encoding="utf-8")

    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("no such table")

        def rollback(self):
            pass

    v = calc_health.view(NOW, db=Broken())
    assert v["state"] == "absent" and v["requests_active"] is None
    assert v["db_error"] == "The saved setups could not be counted (RuntimeError)."
    v = calc_health.view(NOW, state=dict(STATE), db=Broken())       # the file's own count stands in
    assert v["requests_active"] == 2 and v["state"] == "idle"


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
def admin_client(engine, db):
    """Settings -> Data Ingest is admin-only (main.py)."""
    admin = models.User(email="admin@local.test", display_name="Admin", role=models.ROLE_ADMIN,
                        status=models.APPROVED, created_at=_dt.datetime(2026, 1, 5))
    db.add(admin)
    db.commit()
    try:
        yield _client(engine, admin.id)
    finally:
        app.dependency_overrides.clear()


def test_the_data_ingest_page_has_the_option_calculator_tab(admin_client):
    html = admin_client.get("/finviz").text
    assert 'data-tab="optcalc"' in html and ">Option Calculator</button>" in html
    assert 'data-panel="optcalc"' in html and 'id="ocBox"' in html
    assert "/finviz/option-calc" in html and "window.loadOptCalc" in html
    assert "if (name === 'optcalc') loadOptCalc();" in html


def test_the_fragment_renders_absent_and_fresh(admin_client, state_path):
    frag = admin_client.get("/finviz/option-calc")
    assert frag.status_code == 200
    for needle in ("not running here", "has not reported on this server", "How to start it",
                   "setup_option_calc_task.ps1 -StartNow", "TST-Option-Calc", "How it updates"):
        assert needle in frag.text, needle
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    _write(state_path, dict(STATE, heartbeat=now.isoformat(), state="error",
                            last_error="OperationalError: database is locked"))
    frag = admin_client.get("/finviz/option-calc")
    assert frag.status_code == 200
    assert "problem" in frag.text and "Last error: OperationalError: database is locked" in frag.text
    assert "text-rose-300" in frag.text and "How to start it" in frag.text
    _write(state_path, dict(STATE, heartbeat=now.isoformat()))
    frag = admin_client.get("/finviz/option-calc").text
    assert "up to date" in frag and "How to start it" not in frag and "Saved setups kept up to date" in frag
