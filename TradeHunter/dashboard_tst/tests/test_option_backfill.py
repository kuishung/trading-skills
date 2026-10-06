"""services/option_backfill - the first-time data backfill for a basket ticker
(v4.131; user, 2026-10-06: "when a new ticker is added in the option basket, all the
data backfill required to compute the result need to be backfill in for the first time").

No network, no subprocess: the IB seeder, the interpreter check and the port probe are
stubbed where a test needs them to have "run" (the seeder stub writes iv_history like
the real one), and conftest pins ``TST_IV_SEED_IBKR=0`` so nothing here ever dials a
port or spawns an interpreter by accident.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app import models
from app.services import job_runs, option_store
from app.services import option_backfill as ob
from app.services.opt_constants import IV_RANK_MIN_OBS

TODAY = option_store._day(None)


def _hist(db, sym: str, n: int, start=_dt.date(2025, 10, 1), iv=30.0, source="cboe"):
    """``n`` screener readings for ``sym`` from ``start`` (daily, weekends included - the
    rank window counts rows, not calendar days)."""
    for i in range(n):
        db.add(models.IVHistory(symbol=sym, on=(start + _dt.timedelta(days=i)).isoformat(),
                                iv30=iv + i % 7, spot=100.0, source=source))
    db.commit()


def _daily(db, sym: str, on: str, iv=35.0):
    db.add(models.IVDaily(symbol=sym, on=on, kind="eod", source="cboe", iv30=iv))
    db.commit()


def _snapshot_and_signal(db, sym: str, on: str):
    chain = {"symbol": sym, "snap_on": on, "kind": "eod", "source": "cboe", "as_of": None, "spot": 100.0,
             "iv30": 60.0, "partial": False,
             "rows": [{"expiry": "2026-11-20", "right": "P", "strike": 95.0, "bid": 1.0, "ask": 1.1,
                       "iv": 0.6, "delta": -0.2, "oi": 10, "volume": 1}]}
    option_store.replace_snapshot(db, chain)
    option_store.upsert_signal(db, {"status": "ok", "headline": "h", "setup": {}, "iv": {}, "strategies": [],
                                    "picks": {}, "computed_ms": 1, "engine_version": "t"},
                               prefs_hash="house", symbol=sym, snap_on=on, kind="eod")
    db.commit()


@pytest.fixture
def seeding(monkeypatch):
    """The seed step switched ON with its two environment probes stubbed: an interpreter
    that passes preflight and a Gateway answering on 4002. The seeder itself is left to
    each test. The per-process memo starts empty."""
    monkeypatch.setenv("TST_IV_SEED_IBKR", "1")
    monkeypatch.delenv("TST_IBKR_PORT", raising=False)
    monkeypatch.setattr(ob, "resolve_ibkr_python", lambda: (["py", "-3.12"], ""))
    monkeypatch.setattr(ob, "gateway_port", lambda timeout=1.5: (4002, [4002]))
    monkeypatch.setattr(ob, "_seed_memo", {})
    return monkeypatch


# ───────────────────────────────── counts ─────────────────────────────────

def test_counts_every_requested_symbol_is_a_key(db):
    _hist(db, "AAA", 5)
    _daily(db, "AAA", "2026-10-05")
    assert ob.counts(db, ["aaa", "BBB", "", None, "aaa"]) == {"AAA": 1, "BBB": 0}
    assert ob.counts(db, ["AAA"], models.IVHistory) == {"AAA": 5}
    assert ob.counts(db, []) == {}


# ───────────────────────────────── the screener copy ─────────────────────────────────

def test_copy_from_the_screener_recomputes_the_rank_marks_stale_and_writes_a_job_row(db):
    """AAA has a year in iv_history and today's own reading: the copy lands, today's rank
    is recomputed from the now-full window, the latest signal row goes stale_iv (the next
    card read recomputes the gauge) and ONE backfill job row carries the note. BBB has
    nothing anywhere: still short, said so. A second run changes nothing and writes no row."""
    _hist(db, "AAA", 300)
    _daily(db, "AAA", TODAY, iv=60.0)                 # today's reading is the window's high
    _snapshot_and_signal(db, "AAA", TODAY)

    out = ob.backfill(db, ["AAA", "bbb"], seed=False)
    assert out["symbols"] == ["AAA", "BBB"]
    assert out["copied"] == 300 and out["gained"] == 300
    assert out["per"]["AAA"] == {"before": 1, "after": 301, "gained": 300, "rank": pytest.approx(100.0), "stale_marked": 1}
    assert out["per"]["BBB"] == {"before": 0, "after": 0, "gained": 0}
    assert out["short"] == ["BBB"] and out["seed"] == {"status": "off", "why": "seed=False"}
    assert db.query(models.OptionSignal).filter_by(symbol="AAA").one().status == "stale_iv"
    hdr = db.query(models.IVDaily).filter_by(symbol="AAA", on=TODAY).one()
    assert hdr.iv_rank == pytest.approx(100.0) and hdr.iv_n >= IV_RANK_MIN_OBS
    job = job_runs.latest(db, "backfill")
    assert job is not None and job.id == out["job_id"]
    assert (job.source, job.symbols, job.ok, job.errors, job.rows) == ("iv_history", 2, 1, 0, 300)
    assert job.detail["_short"] == ["BBB"] and job.detail["_seed"]["status"] == "off" and job.detail["AAA"]["gained"] == 300
    assert "2 tickers: 300 days of IV history added" in job.note
    assert f"1 still under {IV_RANK_MIN_OBS} days (BBB)" in job.note and "press Live" in job.note
    assert job.note == out["note"]

    again = ob.backfill(db, ["AAA", "BBB"], seed=False)
    assert again["copied"] == 0 and again["gained"] == 0 and again["job_id"] is None
    assert db.query(models.OptionJob).filter_by(job="backfill").count() == 1


def test_empty_symbols_is_a_no_op(db):
    out = ob.backfill(db, [])
    assert out["symbols"] == [] and out["job_id"] is None and out["note"] == ""


def test_job_source_always_fits_the_column(db):
    """OptionJob.source is String(12) - Postgres enforces it, SQLite does not."""
    limit = models.OptionJob.source.type.length
    assert len("iv_history") <= limit and len("hist+ibkr") <= limit
    run = job_runs.start(db, "backfill", TODAY, source="a-source-name-far-too-long")
    assert len(run.source) <= limit


# ───────────────────────────────── the IB Gateway seed ─────────────────────────────────

def test_seed_is_off_by_env(db, monkeypatch):
    monkeypatch.setenv("TST_IV_SEED_IBKR", "0")
    monkeypatch.setattr(ob, "resolve_ibkr_python", lambda: pytest.fail("must not check an interpreter"))
    monkeypatch.setattr(ob, "gateway_port", lambda timeout=1.5: pytest.fail("must not probe"))
    out = ob.backfill(db, ["CCC"])
    assert out["seed"] == {"status": "off", "why": "TST_IV_SEED_IBKR=0"} and out["short"] == ["CCC"]
    assert out["job_id"] is None                      # nothing changed: no row


def test_seed_unavailable_without_an_ibkr_interpreter(db, seeding):
    seeding.setattr(ob, "resolve_ibkr_python", lambda: (None, "no interpreter can run the IB seeder (py -3.12: No module named sqlalchemy) - py -3.12 -m pip install ..."))
    seeding.setattr(ob, "run_seeder", lambda *a, **k: pytest.fail("must not run"))
    out = ob.backfill(db, ["CCC"])
    assert out["seed"]["status"] == "unavailable" and "No module named sqlalchemy" in out["seed"]["why"]
    assert "No module named sqlalchemy" in out["note"] and out["job_id"] is None


def test_seed_skipped_when_nothing_listens_and_the_ports_tried_are_named(db, seeding):
    seeding.setattr(ob, "gateway_port", lambda timeout=1.5: (None, [4002, 4001, 7497, 7496]))
    seeding.setattr(ob, "run_seeder", lambda *a, **k: pytest.fail("must not run"))
    out = ob.backfill(db, ["CCC"])
    assert out["seed"]["status"] == "skipped"
    assert "not reachable on 127.0.0.1 (tried 4002, 4001, 7497, 7496)" in out["seed"]["why"]
    assert out["job_id"] is None


def test_seed_runs_for_the_still_short_symbols_only_then_copies(db, seeding):
    """AAA already has a year in the screener's table (copied, never seeded); DDD has
    nothing, so the seeder runs for DDD alone - on the port that answered, with the web
    client id, under the app's own database URL - its rows are copied, and the job row
    says 'seeded from IB Gateway for DDD'."""
    _hist(db, "AAA", 300)
    seeded: list = []

    def fake_seeder(symbols, *, port, python, client_id, timeout=None):
        seeded.append((list(symbols), port, python, client_id))
        _hist(db, symbols[0], 260, source="ibkr")     # what the real one does: iv_history rows
        return {"ok": True, "rc": 0, "seconds": 1.2, "tail": "seeded 1, skipped 0, failed 0",
                "err": None, "cmd": "py -3.12 iv_seed_ibkr.py"}

    seeding.setattr(ob, "run_seeder", fake_seeder)
    out = ob.backfill(db, ["AAA", "DDD"], recompute=False)
    assert seeded == [(["DDD"], 4002, ["py", "-3.12"], ob.SEED_CLIENT_ID_WEB)]
    assert out["seed"]["status"] == "ok" and out["seed"]["symbols"] == ["DDD"] and out["seed"]["empty"] == []
    assert out["seed"]["port"] == 4002 and out["seed"]["client_id"] == 88
    assert out["copied"] == 560 and out["gained"] == 560 and out["short"] == []
    assert ob.counts(db, ["DDD"]) == {"DDD": 260}
    job = job_runs.latest(db, "backfill")
    assert job.source == "hist+ibkr" and job.ok == 2 and job.errors == 0 and job.rows == 560
    assert "a year seeded from IB Gateway for DDD" in job.note and job.detail["_seed"]["symbols"] == ["DDD"]
    # the nightly passes its own client id
    out = ob.backfill(db, ["EEE"], recompute=False, client_id=ob.SEED_CLIENT_ID_NIGHTLY)
    assert seeded[-1][3] == 87


def test_seed_that_returns_nothing_is_said_so_and_not_retried_for_hours(db, seeding):
    """IB has no IV bars for a symbol: the seeder exits 0 with nothing written. The note
    says 'no IB history for X' (never 'a year seeded'), no job row is written (nothing
    landed), and the symbol is not re-seeded on the next call."""
    calls: list = []
    seeding.setattr(ob, "run_seeder", lambda symbols, **kw: calls.append(list(symbols)) or
                    {"ok": True, "rc": 0, "seconds": 0.8, "tail": "seeded 0, skipped 0, failed 1", "err": None, "cmd": "py"})
    out = ob.backfill(db, ["EEE"])
    assert calls == [["EEE"]]
    assert out["seed"]["status"] == "ok" and out["seed"]["symbols"] == [] and out["seed"]["empty"] == ["EEE"]
    assert "no IB history for EEE" in out["note"] and "a year seeded" not in out["note"]
    assert out["job_id"] is None and out["short"] == ["EEE"]
    again = ob.backfill(db, ["EEE"])
    assert calls == [["EEE"]] and again["seed"]["status"] == "recent" and "tried within the last 6 h" in again["seed"]["why"]


def test_seed_failure_is_recorded_logged_not_raised(db, seeding):
    seeding.setattr(ob, "run_seeder", lambda symbols, **kw: {"ok": False, "rc": 1, "seconds": 20.0,
                                                            "tail": "ERROR cannot connect to IB on 127.0.0.1:4002",
                                                            "err": "cannot connect to IB on 127.0.0.1:4002", "cmd": "py"})

    class Rec:                                   # the caller's logger (the nightly passes its own): what reaches /admin/log
        lines: list = []

        def info(self, msg, *a, **k):
            self.lines.append(("info", msg % a if a else msg))

        def warning(self, msg, *a, **k):
            self.lines.append(("warning", msg % a if a else msg))

    rec = Rec()
    out = ob.backfill(db, ["EEE"], log=rec)
    assert out["seed"]["status"] == "error" and out["short"] == ["EEE"]
    assert any(lvl == "warning" and "IB Gateway seed FAILED" in m and "cannot connect" in m for lvl, m in rec.lines)
    job = job_runs.latest(db, "backfill")                 # a failed attempt IS recorded (the strip shows it)
    assert job is not None and job.source == "hist+ibkr" and job.errors == 1
    assert "the IB Gateway seed failed (cannot connect" in job.note and "1 still under" in job.note


def test_one_seed_at_a_time(db, seeding):
    """The seeder runs under the module lock - a second caller waits instead of colliding
    on the IB client id."""
    import threading

    seen: list = []

    def fake_seeder(symbols, **kw):
        seen.append(ob._seed_lock.locked())
        return {"ok": True, "rc": 0, "seconds": 0.1, "tail": "", "err": None, "cmd": "py"}

    seeding.setattr(ob, "run_seeder", fake_seeder)
    ob.backfill(db, ["FFF"])
    assert seen == [True] and not ob._seed_lock.locked()
    assert isinstance(ob._seed_lock, type(threading.Lock()))


def test_run_seeder_with_a_missing_interpreter_is_soft():
    r = ob.run_seeder(["AAA"], port=4002, python=["no-such-python-interpreter-xyz"], client_id=88, timeout=5)
    assert r["ok"] is False and r["rc"] is None and "not found" in r["err"] and "TST_IBKR_PYTHON" in r["err"]


def test_settings_from_the_environment(monkeypatch):
    monkeypatch.delenv("TST_IV_SEED_IBKR", raising=False)
    assert ob.seeder_enabled() is True
    monkeypatch.setenv("TST_IV_SEED_IBKR", "off")
    assert ob.seeder_enabled() is False
    monkeypatch.delenv("TST_IBKR_PORT", raising=False)
    assert ob.ib_port() is None and ob.ib_ports() == (4002, 4001, 7497, 7496)
    monkeypatch.setenv("TST_IBKR_PORT", "4001")
    assert ob.ib_port() == 4001 and ob.ib_ports() == (4001,)
    monkeypatch.setenv("TST_IBKR_PORT", "nope")
    assert ob.ib_port() is None
    monkeypatch.setenv("TST_IBKR_PYTHON", "py -3.12")
    assert ob.python_candidates() == [["py", "-3.12"]]
    monkeypatch.setenv("TST_IBKR_PYTHON", "C:\\Python312\\python.exe")
    assert ob.python_candidates() == [["C:\\Python312\\python.exe"]]
    monkeypatch.delenv("TST_IBKR_PYTHON", raising=False)
    cands = ob.python_candidates()
    assert len(cands) == 2 and cands[1] in (["py", "-3.12"], ["python3.12"])
    assert ob.gateway_reachable(port=1) is False           # a port nothing listens on, in milliseconds


def test_gateway_port_probes_the_candidates_in_order(monkeypatch):
    monkeypatch.delenv("TST_IBKR_PORT", raising=False)
    asked: list = []
    monkeypatch.setattr(ob, "gateway_reachable", lambda host, port, timeout: asked.append(port) or port == 7497)
    assert ob.gateway_port() == (7497, [4002, 4001, 7497])
    asked.clear()
    monkeypatch.setattr(ob, "gateway_reachable", lambda host, port, timeout: asked.append(port) or False)
    assert ob.gateway_port() == (None, [4002, 4001, 7497, 7496])


def test_preflight_is_cached_and_soft(monkeypatch):
    monkeypatch.setattr(ob, "_preflight", {})
    ok, why = ob.preflight(["no-such-python-interpreter-xyz"], timeout=5)
    assert ok is False and "not found" in why
    assert ob._preflight[("no-such-python-interpreter-xyz",)][1] is False
    assert ob.preflight(["no-such-python-interpreter-xyz"]) == (False, why)     # the cache answers


def test_child_database_url_is_absolute(monkeypatch):
    monkeypatch.setattr(ob, "settings", None, raising=False)
    monkeypatch.setenv("TST_DATABASE_URL", "sqlite:///./tst.db")
    from app import config
    monkeypatch.setattr(config.settings, "database_url", "sqlite:///./tst.db")
    url = ob._db_url_for_child()
    assert url.startswith("sqlite:///") and url.endswith("/tst.db") and "./" not in url
    monkeypatch.setattr(config.settings, "database_url", "postgresql://u:p@h/db")
    assert ob._db_url_for_child() == "postgresql://u:p@h/db"


# ───────────────────────────────── first_read ─────────────────────────────────

def test_first_read_backfills_then_reads_each_symbol_paced(db, user, monkeypatch):
    from app.services import option_nightly

    order: list = []
    monkeypatch.setattr(ob, "backfill", lambda db_, syms, **kw: order.append(("backfill", list(syms), kw.get("recompute"))) or {"note": "n"})
    monkeypatch.setattr(option_nightly, "refresh_symbol",
                        lambda db_, s, u: order.append(("read", s, getattr(u, "id", None))) or {"status": "ok", "n": 3, "signals": 2})
    monkeypatch.setattr(option_nightly, "_source", lambda s: object())
    monkeypatch.setattr(option_nightly, "_pacing", lambda src: 1.5)
    slept: list = []
    done: list = []
    out = ob.first_read(db, ["aaa", "BBB", "aaa"], user, on_done=lambda *a: done.append(a), sleep=slept.append)
    assert order == [("backfill", ["AAA", "BBB"], True), ("read", "AAA", user.id), ("read", "BBB", user.id)]
    assert slept == [1.5] and done == [("AAA", "ok", None), ("BBB", "ok", None)]
    assert out["backfill"] == {"note": "n"}
    assert out["reads"] == {"AAA": {"status": "ok", "err": None, "n": 3, "signals": 2},
                            "BBB": {"status": "ok", "err": None, "n": 3, "signals": 2}}


def test_first_read_isolates_failures(db, user, monkeypatch):
    """A backfill that raises and a symbol whose read raises are both recorded; the
    other symbol is still read, and on_done fires for every symbol with its outcome."""
    from app.services import option_nightly

    def boom_backfill(db_, syms, **kw):
        raise RuntimeError("iv_history table missing")

    def read(db_, s, u):
        if s == "BBB":
            raise ValueError("Cboe 429")
        if s == "CCC":
            return {"status": "error", "err": "CCC: no chain (403)", "n": 0, "signals": 0}
        return {"status": "ok", "n": 1, "signals": 1}

    monkeypatch.setattr(ob, "backfill", boom_backfill)
    monkeypatch.setattr(option_nightly, "refresh_symbol", read)
    monkeypatch.setattr(option_nightly, "_source", lambda s: (_ for _ in ()).throw(RuntimeError("no source")))
    done: list = []
    out = ob.first_read(db, ["AAA", "BBB", "CCC"], None, on_done=lambda *a: done.append(a), sleep=lambda s: None)
    assert out["backfill"] == {"error": "RuntimeError: iv_history table missing"}
    assert out["reads"]["AAA"]["status"] == "ok"
    assert out["reads"]["BBB"] == {"status": "error", "err": "ValueError: Cboe 429", "n": None, "signals": None}
    assert done == [("AAA", "ok", None), ("BBB", "error", "ValueError: Cboe 429"), ("CCC", "error", "CCC: no chain (403)")]
    assert ob.first_read(db, [], None) == {"backfill": None, "reads": {}}
