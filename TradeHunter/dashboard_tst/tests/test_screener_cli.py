"""The Options Screener collector's CLI, ``deploy/screener_collector.py`` (v4.137): a
collector that cannot start says so where the page and the tray look (the crash state in
``state/screener_collector.json`` and the status row), ``--forever`` never gives up on a
failed start, a one-off's crash outlives its final "stopped" heartbeat, ``--history`` that
read nothing exits 3 - and ``deploy/setup_screener_task.ps1`` revives a dead collector
every 15 min.

Nothing here starts the real loop or touches Massive: the collector is a stand-in, the
screener DB a temp SQLite file, the crash state a temp file, the sleeps recorded.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

from app import screener_db
from app.services import scr_collector, scr_store

from .conftest import DASH_ROOT

CLI = DASH_ROOT / "deploy" / "screener_collector.py"
PS1 = DASH_ROOT / "deploy" / "setup_screener_task.ps1"
KEY = "unit-test-key-0123456789abcdef"          # not a real key
FILE_KEY = "file-only-key-9876543210fedcba"     # the key as only app\.env has it


def _load_cli():
    spec = importlib.util.spec_from_file_location("screener_collector_cli_v4137", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCollector:
    """A ``scr_collector.Collector`` stand-in: records what ran; ``stop`` writes the
    "stopped" heartbeat the real one writes (into the CLI's state file)."""

    made: list = []
    result: dict = {"ok": True}
    raise_in: str | None = None          # "once" / "forever" / "init": raise RuntimeError there
    raises_left = 0
    state_file: Path | None = None

    def __init__(self, session_factory, log=None, **kw):
        if FakeCollector.raise_in == "init":
            raise RuntimeError("bad TST_SCREENER_WORKERS")
        self.ran: list[str] = []
        self.stopped: list[str] = []
        self.workers, self.max_rps, self.cycle_min = 8, 40.0, 30
        self.state_path = FakeCollector.state_file
        FakeCollector.made.append(self)

    def _maybe_raise(self, where):
        if FakeCollector.raise_in == where and FakeCollector.raises_left > 0:
            FakeCollector.raises_left -= 1
            raise RuntimeError("boom in %s" % where)

    def run_forever(self):
        self.ran.append("forever")
        self._maybe_raise("forever")

    def run_once(self):
        self.ran.append("once")
        self._maybe_raise("once")
        return dict(self.result)

    def run_universe(self):
        self.ran.append("universe")
        return dict(self.result)

    def run_eod(self):
        self.ran.append("eod")
        return dict(self.result)

    def run_history(self, symbols):
        self.ran.append("history:" + ",".join(symbols))
        return dict(self.result)

    def stop(self, reason="stopped"):
        self.stopped.append(reason)
        if self.state_path is not None:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"state": "stopped", "detail": reason,
                                                   "heartbeat": _dt.datetime.now(_dt.timezone.utc).isoformat()}),
                                       encoding="utf-8")


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """The CLI module aimed at a temp state file and a temp app\\.env, its sleeps recorded,
    the stand-in collector; the root logger and the screener DB put back after."""
    mod = _load_cli()
    state = tmp_path / "state" / "screener_collector.json"
    monkeypatch.setattr(mod, "STATE_FILE", state)
    monkeypatch.setattr(mod, "ENV_FILE", tmp_path / "app" / ".env")
    mod.slept = []
    monkeypatch.setattr(mod, "_sleep", mod.slept.append)
    monkeypatch.delenv("TST_MASSIVE_API_KEY", raising=False)
    monkeypatch.setattr(scr_collector, "Collector", FakeCollector)
    FakeCollector.made = []
    FakeCollector.result = {"ok": True}
    FakeCollector.raise_in, FakeCollector.raises_left = None, 0
    FakeCollector.state_file = state
    names = ("screener_collector",) + tuple(mod.APP_LOGGERS) + ("httpx", "httpcore")
    saved = {n: (logging.getLogger(n).level, logging.getLogger(n).disabled) for n in names}
    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    try:
        yield mod
    finally:
        screener_db.configure(None)
        root.setLevel(level)
        for h in list(root.handlers):
            for x in [x for x in h.filters if isinstance(x, mod.KeyScrub)]:
                h.removeFilter(x)
            if h not in before:
                root.removeHandler(h)
                h.close()
        logging.captureWarnings(False)
        for n, (lvl, dis) in saved.items():
            logging.getLogger(n).setLevel(lvl)
            logging.getLogger(n).disabled = dis


@pytest.fixture
def sdb(monkeypatch, tmp_path):
    """A migrated temp screener DB the CLI's ``app.screener_db`` points at."""
    url = "sqlite:///" + (tmp_path / "screener_cli.db").as_posix()
    monkeypatch.setenv("TST_SCREENER_DATABASE_URL", url)
    screener_db.configure(None)
    screener_db.init_screener_db()
    yield url
    screener_db.configure(None)


def _crash(cli) -> dict:
    return json.loads(cli.STATE_FILE.read_text(encoding="utf-8"))


def _row() -> dict | None:
    db = screener_db.SessionLocal()
    try:
        return scr_store.status(db)
    finally:
        db.close()


def _assert_crash(doc: dict, cli, *needles) -> None:
    assert (doc["state"], doc["error_kind"], doc["source"]) == ("error", "startup", "massive")
    assert doc["written_by"] == "dashboard_tst/deploy/screener_collector.py"
    assert doc["detail"].startswith("The collector could not start on the server: ")          # T-41
    assert doc["detail"].endswith(". - see logs\\screener_collector.log")
    hb = _dt.datetime.fromisoformat(doc["heartbeat"])
    assert hb.tzinfo is not None and abs((_dt.datetime.now(_dt.timezone.utc) - hb).total_seconds()) < 60
    assert doc["pid"] > 0
    for n in needles:
        assert n in doc["detail"] and n in doc["last_error"], (n, doc)


# ───────────────────────────────────────── exit codes ─────────────────────────────────────────

def test_cli_exit_codes_and_history_that_read_nothing_is_3(cli):
    assert (cli.EXIT_OK, cli.EXIT_SETUP, cli.EXIT_SOURCE, cli.EXIT_NOTHING) == (0, 1, 2, 3)
    FakeCollector.result = {"ok": True, "done": 0, "skipped": 2, "nothing": True}
    assert cli.main(["--no-init", "--history", "NVDA", "SPY"]) == 3
    FakeCollector.result = {"ok": True, "done": 1, "nothing": False}
    assert cli.main(["--no-init", "--history", "NVDA"]) == 0
    FakeCollector.result = {"ok": False, "error": "Massive returned an empty options list", "error_kind": "empty"}
    assert cli.main(["--no-init", "--universe-now"]) == 2
    FakeCollector.result = {"ok": False, "nothing": True, "error": scr_collector.NO_KEY_TEXT}
    assert cli.main(["--no-init", "--history", "NVDA"]) == 2           # not usable beats "nothing"
    assert [c.stopped for c in FakeCollector.made] == [["one-off --history run finished"]] * 2 + \
        [["one-off --universe-now run finished"], ["one-off --history run finished"]]
    assert not cli.STATE_FILE.exists() or _crash(cli)["state"] == "stopped"    # no crash state
    with pytest.raises(SystemExit):                                    # bad arguments are not a crash
        cli.main(["--once", "--eod-now"])
    assert "3 = --history ran but no IV history" in cli.__doc__


# ───────────────────────────────────────── could not start ─────────────────────────────────────────

def test_cli_import_failure_writes_crash_state(cli, monkeypatch):
    # what the last collector filed is kept; what was running is not
    cli.STATE_FILE.parent.mkdir(parents=True)
    cli.STATE_FILE.write_text(json.dumps({"state": "pass", "pass_id": 7, "last_pass_et": "16:40 ET",
                                          "last_pass_id": 6, "universe_n": 4512, "progress": {"pass_pct": 40}}),
                              encoding="utf-8")
    monkeypatch.setitem(sys.modules, "app.services.scr_collector", None)     # the import fails
    assert cli.main(["--no-init", "--once"]) == cli.EXIT_SETUP
    doc = _crash(cli)
    _assert_crash(doc, cli, "the app modules could not be loaded (ModuleNotFoundError")
    assert (doc["last_pass_et"], doc["last_pass_id"], doc["universe_n"]) == ("16:40 ET", 6, 4512)
    assert doc.get("pass_id") is None and doc["progress"] is None and doc["next_try"] is None
    assert FakeCollector.made == []
    # a fresh state folder gets its .gitignore
    fresh = cli.STATE_FILE.parent.parent / "fresh" / "state" / "screener_collector.json"
    assert cli._crash_state("x", path=fresh)["state"] == "error"
    assert (fresh.parent / ".gitignore").read_text(encoding="utf-8").endswith("*\n")


def test_cli_init_failure_forever_keeps_heartbeating_error(cli, sdb, monkeypatch):
    calls = []

    def bad_init():
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(screener_db, "init_screener_db", bad_init)
    writes = []
    real = cli._crash_state
    monkeypatch.setattr(cli, "_crash_state", lambda reason, **kw: writes.append(kw.get("retry_s"))
                        or real(reason, **kw))
    assert cli.main(["--forever"], max_rounds=2) == cli.EXIT_SETUP        # only the test hook ends it
    assert len(calls) == 2                                                 # tried again after 5 min
    assert cli.slept == [60.0] * 5                                         # in 60 s steps ...
    assert writes == [300.0, 240.0, 180.0, 120.0, 60.0, 300.0]             # ... each a fresh crash state
    doc = _crash(cli)
    _assert_crash(doc, cli, "the screener database could not be prepared (RuntimeError: database is locked)")
    nt = _dt.datetime.fromisoformat(doc["next_try"])
    assert 290 <= (nt - _dt.datetime.fromisoformat(doc["heartbeat"])).total_seconds() <= 301
    row = _row()                                                           # the status row says the same
    assert (row["state"], row["error_kind"]) == ("error", "startup") and row["detail"] == doc["detail"]
    assert row["next_try"] is not None and FakeCollector.made == []


def test_cli_forever_runs_the_loop_once_the_start_works(cli, sdb, monkeypatch):
    real_init = screener_db.init_screener_db
    left = {"n": 1}

    def flaky_init():
        if left["n"]:
            left["n"] -= 1
            raise RuntimeError("the web app is migrating")
        real_init()

    monkeypatch.setattr(screener_db, "init_screener_db", flaky_init)
    assert cli.main(["--forever"]) == cli.EXIT_OK
    assert cli.slept == [60.0] * 5 and [c.ran for c in FakeCollector.made] == [["forever"]]
    # the loop crashed once: reported, stopped, tried again 5 min later
    FakeCollector.made, cli.slept[:] = [], []
    FakeCollector.raise_in, FakeCollector.raises_left = "forever", 1
    assert cli.main(["--forever", "--no-init"]) == cli.EXIT_OK
    assert [c.ran for c in FakeCollector.made] == [["forever"], ["forever"]]
    assert FakeCollector.made[0].stopped == ["crashed"] and FakeCollector.made[1].stopped == []
    assert cli.slept == [60.0] * 5
    _assert_crash(_crash(cli), cli, "the collector crashed (RuntimeError: boom in forever)")
    assert _row()["error_kind"] == "startup"                       # the crash reached the status row
    assert "the collector crashed (RuntimeError: boom in forever)" in _row()["last_error"]


def test_cli_one_off_crash_error_survives_stop(cli, sdb):
    FakeCollector.raise_in, FakeCollector.raises_left = "once", 1
    assert cli.main(["--once"]) == cli.EXIT_SETUP
    col = FakeCollector.made[0]
    assert col.stopped == ["one-off --once run finished"]           # stop() wrote "stopped" first ...
    doc = _crash(cli)                                               # ... and the crash came after it
    _assert_crash(doc, cli, "the collector crashed (RuntimeError: boom in once)")
    assert doc["next_try"] is None                                  # a one-off does not try again
    assert _row()["state"] == "error"
    # a collector that cannot even be built is a crash too
    FakeCollector.raise_in = "init"
    assert cli.main(["--no-init", "--eod-now"]) == cli.EXIT_SETUP
    _assert_crash(_crash(cli), cli, "RuntimeError: bad TST_SCREENER_WORKERS")


def test_cli_crash_state_never_carries_the_key(cli, sdb, monkeypatch, caplog):
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    cli.ENV_FILE.parent.mkdir(parents=True)
    cli.ENV_FILE.write_text('TST_MASSIVE_API_KEY="%s"\n' % FILE_KEY, encoding="utf-8")

    def bad_init():
        raise RuntimeError("could not open postgresql://u:%s@h/db with %s" % (KEY, FILE_KEY))

    monkeypatch.setattr(screener_db, "init_screener_db", bad_init)
    with caplog.at_level(logging.INFO):
        assert cli.main(["--once"]) == cli.EXIT_SETUP
    assert KEY not in caplog.text                                  # the log masks the key it holds
    text = cli.STATE_FILE.read_text(encoding="utf-8")
    assert KEY not in text and FILE_KEY not in text and "postgresql://u:***@h/db with ***" in text
    row = _row()
    assert KEY not in json.dumps(row, default=str) and FILE_KEY not in json.dumps(row, default=str)
    assert row["error_kind"] == "startup"
    assert cli._mask("a %s b %s" % (KEY, FILE_KEY)) == "a *** b ***"


def test_cli_last_guard_and_the_atomic_write(cli, monkeypatch):
    def broken_logging(level, log_file=None):
        raise OSError("the log folder is read-only")

    monkeypatch.setattr(cli, "_logging", broken_logging)
    assert cli.main(["--once"]) == cli.EXIT_SETUP                  # a one-off without its log: exit 1
    _assert_crash(_crash(cli), cli, "the collector crashed (OSError: the log folder is read-only)")
    assert cli.main(["--forever"], max_rounds=2) == cli.EXIT_SETUP     # --forever keeps trying
    _assert_crash(_crash(cli), cli, "the log file could not be opened (OSError: the log folder is read-only)")
    assert cli.slept == [60.0] * 5 and FakeCollector.made == []
    # the tray holds the file open: Windows refuses the rename, the file is written directly
    import os  # noqa: PLC0415

    def refuse(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", refuse)
    assert cli._crash_state("again")["last_error"] == "again"
    assert _crash(cli)["last_error"] == "again"
    assert not cli.STATE_FILE.with_name(cli.STATE_FILE.name + ".tmp").exists()
    # it never raises, even when nothing can be written
    assert cli._crash_state("x", path=cli.STATE_FILE.parent / "nul\0") is None


def test_a_long_crash_reason_keeps_the_log_hint_whole_on_the_tray(cli):
    from .test_scr_collector import _tray_function  # noqa: PLC0415 - the tray's own function

    reason = "the screener database could not be prepared (OperationalError: " + "unable to open " * 20 + ")"
    assert len(reason) >= 200
    doc = cli._crash_state(reason)
    assert len(doc["detail"]) <= cli.DETAIL_MAX == 220 and doc["detail"].endswith(cli.LOG_HINT)
    assert doc["detail"].startswith(cli.CRASH_PREFIX + reason[:120])
    assert doc["last_error"] == reason                              # the whole reason stays for the page
    got = _tray_function(cli.STATE_FILE)(now=_dt.datetime.now(_dt.timezone.utc))
    assert got["state"] == "error" and got["detail"] == doc["detail"]       # nothing cut on the tray
    assert got["detail"].endswith(" - see logs\\screener_collector.log")


def test_one_collector_at_a_time(cli, sdb, monkeypatch):
    """The 15-min revive trigger starts the task whenever it is not running: a second
    collector (a hand run, or the revived task during one) never writes beside the first."""
    import os  # noqa: PLC0415

    first = cli.InstanceLock(cli._lock_path())
    assert first.acquire()
    try:
        assert first.holder() == os.getpid()
        assert (cli.STATE_FILE.parent / ".gitignore").read_text(encoding="utf-8").endswith("*\n")
        running = json.dumps({"state": "pass", "detail": "the running collector's pass"})
        cli.STATE_FILE.write_text(running, encoding="utf-8")
        # a one-off gives up at once: exit 1, the state file and the status row untouched
        assert cli.main(["--no-init", "--eod-now"]) == cli.EXIT_SETUP
        assert cli.main(["--once"]) == cli.EXIT_SETUP
        assert FakeCollector.made == [] and cli.STATE_FILE.read_text(encoding="utf-8") == running
        assert _row() is None
        # even a one-off whose log cannot open never writes its crash over the running one's state
        with monkeypatch.context() as m:
            m.setattr(cli, "_logging", lambda level, log_file=None: (_ for _ in ()).throw(OSError("read-only")))
            assert cli.main(["--once"]) == cli.EXIT_SETUP
        assert cli.STATE_FILE.read_text(encoding="utf-8") == running
        # --forever waits, writing nothing and starting nothing
        assert cli.main(["--forever", "--no-init"], max_lock_waits=3) == cli.EXIT_SETUP
        assert cli.slept == [cli.LOCK_WAIT_S] * 3 and FakeCollector.made == []
        assert cli.STATE_FILE.read_text(encoding="utf-8") == running and _row() is None
        assert first.holder() == os.getpid()                         # a refused run never writes its pid
        cli.slept[:] = []

        def free_on_second(s):
            cli.slept.append(s)
            if len(cli.slept) == 2:
                first.release()                                      # the other collector ends

        monkeypatch.setattr(cli, "_sleep", free_on_second)
        assert cli.main(["--forever", "--no-init"]) == cli.EXIT_OK      # ... and this one takes over
        assert [c.ran for c in FakeCollector.made] == [["forever"]] and len(cli.slept) == 2
    finally:
        first.release()
    assert cli.main(["--no-init", "--once"]) == cli.EXIT_OK          # the lock went with that run
    doc = cli.__doc__
    assert "Disable-ScheduledTask -TaskName TST-Options-Screener" in doc and "stop the task first" not in doc


def test_cli_state_file_is_the_collectors():
    cli = _load_cli()
    assert cli.STATE_FILE == scr_collector.STATE_PATH
    assert cli.ENV_FILE == scr_collector.APP_ENV_PATH
    src = CLI.read_bytes().decode("ascii")                         # the CLI stays ASCII
    head = src.split("def _crash_state", 1)[0]
    assert "from app" not in head and "import app" not in head     # the crash writer needs no app import


# ───────────────────────────────────────── the task script ─────────────────────────────────────────

def test_screener_task_script_revives_a_dead_collector_every_15_min():
    src = PS1.read_bytes().decode("ascii")
    for needle in ("$revive = New-ScheduledTaskTrigger -Once -At (Get-Date)",
                   "-RepetitionInterval (New-TimeSpan -Minutes 15)",
                   "-RepetitionDuration (New-TimeSpan -Days 9999)",
                   "-Trigger @($atStartup, $daily, $revive)",
                   "-MultipleInstances IgnoreNew", "-AtStartup", "-Daily -At $At"):
        assert needle in src, needle
    assert "&&" not in src and "[TimeSpan]::MaxValue" not in src.split("$revive =")[1]
    assert "every 15 min" in src.split("[CmdletBinding()]")[0]       # the header says so
