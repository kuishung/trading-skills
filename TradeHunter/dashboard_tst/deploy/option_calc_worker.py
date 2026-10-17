r"""Hermes Option Calculator lane - recomputes every member's saved option setups after each
Massive market pass (OPTION_CALCULATOR_DESIGN.md steps 2-3). The loop itself is
``app/services/calc_worker.py``; this file wires it to the two databases (the setups and
results in tst.db - ``app/db.py``; the chain in screener.db - ``app/screener_db.py``), the
log, and a single-instance lock.

Runs on **Hermes** (Windows Server 2019, PowerShell) as the scheduled task
``TST-Option-Calc`` (``deploy\setup_option_calc_task.ps1``), which starts it with
``--forever --log-file logs\option_calc_worker.log`` at startup, daily 07:00 and every
15 min (the last two only revive a lane that died). The log rotates at 5 MB, 5 old files.

ONE lane at a time: each run takes ``state\option_calc_worker.lock`` (an OS file lock,
released when the process ends). A ``--forever`` that finds it taken waits and takes over
when the other one ends; ``--once`` exits 1. The 15-min trigger restarts the task whenever
it is not running, so a run BY HAND must DISABLE the task first. Hermes, PowerShell as
Administrator:

    cd C:\trading-skills\TradeHunter\dashboard_tst
    Disable-ScheduledTask -TaskName TST-Option-Calc
    schtasks /End /TN TST-Option-Calc
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'option_calc_worker\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    .\.venv\Scripts\python.exe deploy\option_calc_worker.py --once -v     # one look, exit
    Enable-ScheduledTask -TaskName TST-Option-Calc
    Start-ScheduledTask -TaskName TST-Option-Calc

Environment (app\.env): TST_DATABASE_URL and TST_SCREENER_DATABASE_URL as for the web app;
``TST_CALC_TICK_S`` seconds between looks (default 60, 10..600). No API key: the lane reads
only what the screener collector stored.

Status: ``state\option_calc_worker.json`` (the tray and Settings -> Data Ingest -> Option
Calculator read it). A start that fails writes ``state: error`` there with the reason.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import msvcrt
except ImportError:                                # not Windows
    msvcrt = None
    import fcntl

DASH = Path(__file__).resolve().parent.parent
if str(DASH) not in sys.path:                      # the app package
    sys.path.insert(0, str(DASH))

log = logging.getLogger("option_calc_worker")

EXIT_OK, EXIT_SETUP, EXIT_BUSY = 0, 1, 2
_FMT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
TASK_NAME = "TST-Option-Calc"
STATE_FILE = DASH / "state" / "option_calc_worker.json"
LOCK_NAME = "option_calc_worker.lock"
LOCK_WAIT_S = 60.0
SETUP_RETRY_S = 300.0
QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "sqlalchemy.engine")
APP_LOGGERS = ("option_calc_worker", "app.services.calc_worker", "app.services.calc_pair",
               "app.services.calc_store", "app.services.calc_chain")
_sleep = time.sleep


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--forever", action="store_true", help="run until stopped (the default; the scheduled task)")
    mode.add_argument("--once", action="store_true", help="one look at the newest pass, then exit")
    ap.add_argument("--tick", type=float, default=None, metavar="S", help="seconds between looks (default TST_CALC_TICK_S or 60)")
    ap.add_argument("--no-init", action="store_true", help="skip the Alembic upgrades of both databases at start")
    ap.add_argument("--log-file", default=None, metavar="PATH", help="append to this log file (rotating, 5 MB x 5)")
    ap.add_argument("--state-file", default=None, metavar="PATH", help="(tests) the state file to write")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug lines too")
    return ap.parse_args(argv)


# ---------------------------------------------------------------- logging

def _configure_logging(path: str | None, level: int) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if not any(isinstance(h, logging.StreamHandler) and getattr(h, "_calc_console", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(_FMT))
        h.setLevel(level)
        h._calc_console = True          # type: ignore[attr-defined]
        root.addHandler(h)
    if path and not any(isinstance(h, RotatingFileHandler) and Path(getattr(h, "baseFilename", "")) == Path(path).resolve()
                        for h in root.handlers):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(p, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_FMT))
        fh.setLevel(level)
        root.addHandler(fh)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in APP_LOGGERS:
        logging.getLogger(name).setLevel(level)


# ---------------------------------------------------------------- one lane at a time

class InstanceLock:
    """An OS lock on the lock file, held for the life of the process (released by the OS
    when the process ends, even when killed). The holder writes its pid into the file."""

    OFFSET = 1 << 20

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def acquire(self) -> bool:
        if self.fd is not None:
            return True
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        gi = p.parent / ".gitignore"
        if p.parent.name == "state" and not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if msvcrt is not None:
                os.lseek(fd, self.OFFSET, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        except OSError:
            pass
        self.fd = fd
        return True

    def holder(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="ascii", errors="replace").split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if msvcrt is not None:
                os.lseek(fd, self.OFFSET, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------- the crash state

def _crash_state(state_file: Path, reason: str) -> None:
    """The lane could not start: say so where the tray and the Settings page look."""
    doc = {"version": None, "pid": os.getpid(), "heartbeat": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "state": "error", "detail": ("The option calculator could not start on the server: " + reason)[:300],
           "last_error": reason[:2000], "written_by": "dashboard_tst/deploy/option_calc_worker.py"}
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- set-up + run

def _tick(args) -> float:
    if args.tick:
        return max(10.0, min(600.0, float(args.tick)))
    try:
        return max(10.0, min(600.0, float(os.environ.get("TST_CALC_TICK_S") or 60)))
    except ValueError:
        return 60.0


def _setup(args):
    """Import the app, upgrade both databases (unless --no-init), build the worker."""
    from app import db as tst_db                       # noqa: PLC0415 - loads app/.env
    from app import screener_db                        # noqa: PLC0415
    from app.services import calc_worker               # noqa: PLC0415

    if not args.no_init:
        tst_db.init_db()
        screener_db.init_screener_db()
    state = Path(args.state_file) if args.state_file else calc_worker.STATE_PATH
    return calc_worker.CalcWorker(tst_sessions=tst_db.SessionLocal, scr_sessions=screener_db.SessionLocal,
                                  state_path=state, tick_s=_tick(args))


def main(argv=None, *, max_lock_waits: int | None = None) -> int:
    args = parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    _configure_logging(args.log_file, level)
    state_file = Path(args.state_file) if args.state_file else STATE_FILE
    lock = InstanceLock(state_file.with_name(LOCK_NAME))
    waits = 0
    while not lock.acquire():
        if args.once:
            log.error("another option calculator lane is running (pid %s); nothing done. Before a run by hand: "
                      "Disable-ScheduledTask -TaskName %s; schtasks /End /TN %s", lock.holder(), TASK_NAME, TASK_NAME)
            return EXIT_BUSY
        waits += 1
        if max_lock_waits is not None and waits > max_lock_waits:
            return EXIT_BUSY
        log.warning("another option calculator lane is running (pid %s); waiting %.0f s", lock.holder(), LOCK_WAIT_S)
        _sleep(LOCK_WAIT_S)
    try:
        while True:
            try:
                worker = _setup(args)
            except Exception as exc:                   # imports, migrations, config
                reason = "%s: %s" % (type(exc).__name__, exc)
                log.exception("the lane could not start")
                _crash_state(state_file, reason)
                if args.once:
                    return EXIT_SETUP
                _sleep(SETUP_RETRY_S)
                continue
            if args.once:
                out = worker.look()
                log.info("one look: pass %s, %d due, %d computed, %d not", out["pass_id"], out["due"], out["done"], out["failed"])
                return EXIT_OK
            try:
                worker.run_forever()
                return EXIT_OK
            except KeyboardInterrupt:
                worker.stop()
                return EXIT_OK
            except Exception as exc:
                log.exception("the lane crashed; restarting in %.0f s", SETUP_RETRY_S)
                _crash_state(state_file, "%s: %s" % (type(exc).__name__, exc))
                _sleep(SETUP_RETRY_S)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
