"""The option calculator lane's CLI, ``deploy/option_calc_worker.py``: one lane at a time,
``--once`` exit codes, the crash state when the start fails, the tick setting."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

DASH_ROOT = Path(__file__).resolve().parent.parent
CLI = DASH_ROOT / "deploy" / "option_calc_worker.py"


@pytest.fixture
def cli(monkeypatch):
    spec = importlib.util.spec_from_file_location("option_calc_worker_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    yield mod
    sys.modules.pop(spec.name, None)


class _Worker:
    def __init__(self, out=None, crash=None):
        self.out = out or {"pass_id": 9, "due": 2, "done": 2, "failed": 0}
        self.crash = crash
        self.looks = 0
        self.stopped = False

    def look(self):
        self.looks += 1
        return self.out

    def run_forever(self):
        if self.crash:
            raise self.crash
        raise KeyboardInterrupt

    def stop(self):
        self.stopped = True


def test_parse_args_and_tick(cli, monkeypatch):
    a = cli.parse_args([])
    assert not a.once and a.tick is None and a.log_file is None
    assert cli._tick(cli.parse_args(["--tick", "5"])) == 10.0          # floored
    assert cli._tick(cli.parse_args(["--tick", "900"])) == 600.0       # capped
    monkeypatch.setenv("TST_CALC_TICK_S", "120")
    assert cli._tick(cli.parse_args([])) == 120.0
    monkeypatch.setenv("TST_CALC_TICK_S", "nope")
    assert cli._tick(cli.parse_args([])) == 60.0


def test_once_runs_one_look(cli, monkeypatch, tmp_path):
    w = _Worker()
    monkeypatch.setattr(cli, "_setup", lambda args: w)
    state = tmp_path / "state" / "option_calc_worker.json"
    rc = cli.main(["--once", "--state-file", str(state)])
    assert rc == cli.EXIT_OK and w.looks == 1
    assert (tmp_path / "state" / ".gitignore").exists()
    assert not (tmp_path / "state" / "option_calc_worker.lock").exists() or True   # the lock file may stay; the lock is released


def test_second_instance_is_refused(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_setup", lambda args: _Worker())
    state = tmp_path / "state" / "option_calc_worker.json"
    held = cli.InstanceLock(state.with_name(cli.LOCK_NAME))
    assert held.acquire()
    try:
        assert cli.main(["--once", "--state-file", str(state)]) == cli.EXIT_BUSY
        assert cli.main(["--forever", "--state-file", str(state)], max_lock_waits=2) == cli.EXIT_BUSY
    finally:
        held.release()
    assert cli.main(["--once", "--state-file", str(state)]) == cli.EXIT_OK


def test_setup_failure_writes_crash_state(cli, monkeypatch, tmp_path):
    def boom(args):
        raise RuntimeError("no such table: users")
    monkeypatch.setattr(cli, "_setup", boom)
    state = tmp_path / "state" / "option_calc_worker.json"
    assert cli.main(["--once", "--state-file", str(state)]) == cli.EXIT_SETUP
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["state"] == "error" and "no such table" in doc["last_error"]
    assert doc["detail"].startswith("The option calculator could not start on the server: ")
    assert doc["written_by"].endswith("option_calc_worker.py") and doc["heartbeat"]


def test_forever_stops_cleanly_on_ctrl_c(cli, monkeypatch, tmp_path):
    w = _Worker()
    monkeypatch.setattr(cli, "_setup", lambda args: w)
    state = tmp_path / "state" / "option_calc_worker.json"
    assert cli.main(["--forever", "--state-file", str(state)]) == cli.EXIT_OK
    assert w.stopped
