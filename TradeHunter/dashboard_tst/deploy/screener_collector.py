r"""Hermes Options Screener collector - the always-on Massive reader behind the Options
screener page (OPTIONS_SCREENER_DESIGN.md section 4). The loop itself is
``app/services/scr_collector.py``; this file wires it to the screener database
(``app/screener_db.py``, its own Alembic environment ``alembic_screener/``) and the log.

Data: Massive (formerly Polygon.io) - Options Starter (the whole-chain snapshot of every
optionable US underlying, option daily bars for the IV history, the contracts list) and
Stocks Basic (grouped daily bars, the ticker list; about 5 requests a minute). The key is
``TST_MASSIVE_API_KEY`` in ``app\.env`` - it is never printed or logged (the startup line
says only "key set" / "key MISSING"). Plain HTTPS: the dashboard venv runs it.

Runs on **Hermes** (Windows Server 2019, PowerShell) as the scheduled task
``TST-Options-Screener`` (``deploy\setup_screener_task.ps1``), which starts it with
``--forever --log-file logs\screener_collector.log`` at startup, daily 07:00 and every
15 min (the last two only revive a collector that died): the log rotates at 5 MB and keeps
5 old files, so it never grows past ~30 MB.

ONE collector at a time: each run takes ``state\screener_collector.lock`` (an OS file lock,
released when the process ends - even when it is killed). A ``--forever`` that finds it
taken waits (writing nothing, starting nothing) and takes over when the other one ends; a
one-off that finds it taken exits 1 without touching the state file. The 15-min trigger
restarts the task whenever it is not running, so a run BY HAND must DISABLE the task
first (``schtasks /End`` alone is not enough - the trigger would start it again). Hermes,
PowerShell as Administrator:

    cd C:\trading-skills\TradeHunter\dashboard_tst
    Disable-ScheduledTask -TaskName TST-Options-Screener
    schtasks /End /TN TST-Options-Screener
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'screener_collector\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    .\.venv\Scripts\python.exe deploy\screener_collector.py --universe-now  # refresh the universe, exit
    .\.venv\Scripts\python.exe deploy\screener_collector.py --once          # one full market pass, exit
    .\.venv\Scripts\python.exe deploy\screener_collector.py --eod-now -v    # the end-of-day pass now
    .\.venv\Scripts\python.exe deploy\screener_collector.py --history NVDA SPY   # re-read their IV history
    .\.venv\Scripts\python.exe deploy\screener_collector.py --once -v --log-file logs\screener_collector.log
    Enable-ScheduledTask -TaskName TST-Options-Screener
    Start-ScheduledTask -TaskName TST-Options-Screener

(``deploy\setup_screener_task.ps1 -StartNow`` after the hand run does the same as the last
two lines: it re-registers the task, enabled.) A bare ``--forever`` by hand (the log on the
screen) is for a PC where the task is not installed.

Environment (app\.env; only the key is required):
    TST_MASSIVE_API_KEY         the Massive key (required)
    TST_MASSIVE_BASE_URL        default https://api.massive.com
    TST_SCREENER_DATABASE_URL   default sqlite:///<dashboard_tst>/screener.db
    TST_SCREENER_CYCLE_MIN      minutes between market passes in the session, default 30
    TST_SCREENER_WORKERS        reader threads sharing one client, default 8
    TST_SCREENER_MAX_RPS        Massive requests a second at most, default 40
    TST_SCREENER_MAX_DTE        farthest expiry read, in days, default 1100

When it cannot start (the app modules do not load, the screener DB cannot be migrated, the
collector crashes) it says so where the page and the tray look: ``state\screener_collector.json``
gets state ``error``, error_kind ``startup`` and the detail "The collector could not start on
the server: <reason>. - see logs\screener_collector.log" (the reason cut so the detail fits
the tray's 220 characters with the log hint whole; the key masked; written with the
standard library only, so it works when nothing else loads), and the screener DB's status
row gets the same when the DB module loaded. ``--forever`` then never exits: it rewrites
that state every 60 s (a fresh heartbeat, so the page shows the reason rather than "not
reporting") and tries the log file, the imports and the DB set-up again every 5 min (the
web app may be migrating the same DB at that moment), then runs the loop; a crash of the
running loop is reported and retried the same way.

Exit codes: 0 = ran (single underlyings that failed are logged, not fatal; --forever
stopped by Ctrl+C); 1 = a one-off could not start (another collector is running - the
lock; the app or the DB not loadable; a crash - the crash state survives the one-off's
final "stopped" heartbeat); 2 = a one-off
run failed (--once, --universe-now, --eod-now, --history): no key on this PC, the key
rejected, the plan lacks an endpoint, Massive not reachable, the list of optionable stocks
could not be read, or the pass read no option data; 3 = --history ran but no IV history
came out done (e.g. the stock bars are not loaded yet). --forever never exits on any of
these - it reports them (state error on the page and the tray) and retries.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import logging
import os
import re
import shutil
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:                                    # the single-instance lock: Windows (Hermes) ...
    import msvcrt
except ImportError:                     # ... or POSIX (the tests run anywhere)
    msvcrt = None
    import fcntl

# Windows consoles and redirected files default to cp1252; log lines stay ASCII but a
# symbol or a server's error text may not.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DASH = Path(__file__).resolve().parent.parent
if str(DASH) not in sys.path:                      # the app package
    sys.path.insert(0, str(DASH))

log = logging.getLogger("screener_collector")

EXIT_OK, EXIT_SETUP, EXIT_SOURCE, EXIT_NOTHING = 0, 1, 2, 3
_FMT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024     # --log-file rotates at 5 MB ...
LOG_BACKUPS = 5                     # ... and keeps 5 old files (at most ~30 MB in all)
KEY_ENV = "TST_MASSIVE_API_KEY"
ENV_FILE = DASH / "app" / ".env"
# Libraries that log one line per HTTP request (the URL, cursor included): a market pass
# is tens of thousands of requests. Kept to warnings and errors.
QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "hpack")
APP_LOGGERS = ("screener_collector", "app.services.scr_collector", "app.services.scr_store",
               "app.services.opt_massive", "app.services.massive", "app.services.calendars")

# The crash state: the same file the collector heartbeats into (scr_collector.STATE_PATH).
STATE_FILE = DASH / "state" / "screener_collector.json"
WRITTEN_BY = "dashboard_tst/deploy/screener_collector.py"
CRASH_PREFIX = "The collector could not start on the server: "      # T-41
LOG_HINT = " - see logs\\screener_collector.log"
DETAIL_MAX = 220            # the tray's OPTIONS_SCREENER_DETAIL_MAX: the crash detail fits it, hint whole
TASK_NAME = "TST-Options-Screener"
LOCK_NAME = "screener_collector.lock"   # next to the state file: one collector at a time
LOCK_WAIT_S = 60.0          # --forever finding another collector running looks again this often
CRASH_BEAT_S = 60.0         # --forever rewrites the crash state this often (a fresh heartbeat)
SETUP_RETRY_S = 300.0       # ... and tries the imports + the DB set-up again this often
# what a crash state keeps from the file it replaces: what is stored, not what was running
_KEEP = ("last_pass_id", "last_pass_kind", "last_pass_session", "last_pass_finished", "last_pass_et",
         "last_pass_contracts", "last_eod_session", "universe_n", "universe_on", "universe_done",
         "history_done_n", "history_total", "earnings_on")
_sleep = time.sleep         # tests replace it


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--forever", action="store_true",
                      help="run the loop until stopped (the default; what the scheduled task runs)")
    mode.add_argument("--once", action="store_true",
                      help="one full market pass now (the universe first when there is none), then exit")
    mode.add_argument("--universe-now", action="store_true",
                      help="refresh the universe (Massive's options list) now, then exit")
    mode.add_argument("--eod-now", action="store_true",
                      help="run the end-of-day pass now (every chain + the day's IV30), then exit")
    mode.add_argument("--history", nargs="+", metavar="SYM",
                      help="re-read the IV history of these underlyings now, then exit")
    ap.add_argument("--no-init", action="store_true",
                    help="skip init_screener_db() (the Alembic upgrade of the screener DB at start)")
    ap.add_argument("--log-file", default=None, metavar="PATH",
                    help="log to this file instead of stdout, rotated at 5 MB with 5 old files kept "
                         "(what the scheduled task passes: logs\\screener_collector.log)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug lines too")
    return ap.parse_args(argv)


class KeyScrub(logging.Filter):
    """Masks the Massive key in every log line and traceback - a second guard: the client
    never puts it in a message (it travels only in the Authorization header)."""

    def filter(self, record: logging.LogRecord) -> bool:
        key = (os.environ.get(KEY_ENV) or "").strip()
        if len(key) < 4:
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if key in msg:
            record.msg, record.args = msg.replace(key, "***"), None
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                pass
        if record.exc_text and key in record.exc_text:
            record.exc_text = record.exc_text.replace(key, "***")
        return True


def _rotate(source: str, dest: str) -> None:
    """The log file's rotator. Windows will not rename a file another process holds open
    (a ``Get-Content -Wait`` tail): the file is then copied and emptied instead."""
    if not os.path.exists(source):
        return
    try:
        os.replace(source, dest)
    except PermissionError:
        shutil.copyfile(source, dest)
        with open(source, "r+b") as fh:
            fh.truncate(0)


def _log_handler(path: Path) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    h.rotator = _rotate
    h.setFormatter(logging.Formatter(_FMT))
    return h


def _is_console(h: logging.Handler) -> bool:
    return type(h) is logging.StreamHandler and getattr(h, "stream", None) in (
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)


def _logging(level: int, log_file: str | None = None) -> None:
    """Log to stdout, or with ``log_file`` to that file, rotated (LOG_MAX_BYTES x
    LOG_BACKUPS). Safe to call twice (once more after the migrations): the file handler is
    added once, a console Alembic may have added is dropped when logging to a file, every
    root handler gets ``KeyScrub``, the per-request HTTP loggers stay at WARNING."""
    root = logging.getLogger()
    if log_file:
        path = Path(log_file).resolve()
        for h in [h for h in root.handlers if _is_console(h)]:
            root.removeHandler(h)
        if not any(isinstance(h, RotatingFileHandler)
                   and os.path.normcase(h.baseFilename) == os.path.normcase(str(path))
                   for h in root.handlers):
            root.addHandler(_log_handler(path))
        logging.captureWarnings(True)
    elif not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(_FMT))
        root.addHandler(h)
    for h in root.handlers:
        if not any(isinstance(f, KeyScrub) for f in h.filters):
            h.addFilter(KeyScrub())
    root.setLevel(level)
    for name in APP_LOGGERS:
        lg = logging.getLogger(name)
        lg.disabled = False
        lg.setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------- the crash state

def _secrets() -> list[str]:
    """The Massive key as the process holds it and as ``app\\.env`` has it (the app may
    not have loaded the file yet) - the values a crash text must never carry."""
    out = []
    v = (os.environ.get(KEY_ENV) or "").strip()
    if len(v) >= 4:
        out.append(v)
    try:
        text = ENV_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for m in re.finditer(r"(?m)^\s*(?:export\s+)?" + KEY_ENV + r"\s*=\s*['\"]?([^'\"\s#]+)", text):
        if len(m.group(1)) >= 4 and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _mask(text: str) -> str:
    text = str(text or "")
    for s in _secrets():
        text = text.replace(s, "***")
    return text


def _why(what: str, exc: BaseException) -> str:
    """``what (ExcType: message)``, on one line, the key masked."""
    msg = " ".join(str(exc).split())
    return _mask("%s (%s%s)" % (what, type(exc).__name__, (": " + msg) if msg else ""))[:500]


def _iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).isoformat()


def _crash_doc(reason: str, now: _dt.datetime, retry_s: float | None) -> dict:
    reason = " ".join(_mask(reason or "unknown reason").split()) or "unknown reason"
    # cut so prefix + reason + "." + the log hint fit the tray's DETAIL_MAX (140 characters)
    short = reason[:DETAIL_MAX - len(CRASH_PREFIX) - 1 - len(LOG_HINT)].rstrip().rstrip(".")
    return {"state": "error", "error_kind": "startup",
            "detail": "%s%s.%s" % (CRASH_PREFIX, short, LOG_HINT),               # T-41 + the log
            "last_error": reason[:500], "heartbeat": _iso(now), "pid": os.getpid(),
            "next_try": _iso(now + _dt.timedelta(seconds=retry_s)) if retry_s else None,
            "api_ok": None, "warn": None, "progress": None, "source": "massive",
            "written_by": WRITTEN_BY}


def _crash_state(reason: str, *, retry_s: float | None = None, path=None, now=None) -> dict | None:
    """Write ``state\\screener_collector.json`` as "could not start" (standard library only:
    it must work when nothing else loads). The key is masked; what the old file says is
    stored (the last pass, the universe, the IV history) is kept; ``retry_s`` = when
    --forever tries again (``next_try``). Atomic (a temp file + ``os.replace``; a direct
    write when Windows refuses the rename because the tray has the file open); a fresh
    ``state`` folder gets its ``.gitignore``. Never raises; returns the document written,
    or None."""
    p = Path(path) if path is not None else STATE_FILE
    now = now or _dt.datetime.now(_dt.timezone.utc)
    try:
        doc = {}
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                doc = {k: old.get(k) for k in _KEEP if k in old}
        except (OSError, ValueError):
            pass
        doc.update(_crash_doc(reason, now, retry_s))
        fresh = not p.parent.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        if fresh and p.parent.name == "state":
            gi = p.parent / ".gitignore"
            if not gi.exists():
                gi.write_text("# runtime state (collector heartbeats) - never committed\n*\n", encoding="utf-8")
        text = json.dumps(doc, indent=1, sort_keys=True, default=str)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, p)
        except PermissionError:              # the tray had it open this instant (Windows)
            p.write_text(text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
        return doc
    except Exception as exc:  # noqa: BLE001 - the log still has the reason
        try:
            log.warning("could not write the crash state to %s: %s", p, exc)
        except Exception:  # noqa: BLE001
            pass
        return None


def _crash_db(screener_db, reason: str, *, retry_s: float | None = None) -> bool:
    """The same "could not start" on the screener DB's status row, best-effort (the DB may
    be what failed). Returns True when written."""
    if screener_db is None:
        return False
    try:
        scr_store = importlib.import_module("app.services.scr_store")
        now = _dt.datetime.now(_dt.timezone.utc)
        doc = _crash_doc(reason, now, retry_s)
        db = screener_db.SessionLocal()
        try:
            scr_store.set_status(db, state="error", error_kind="startup", detail=doc["detail"],
                                 last_error=doc["last_error"], pid=doc["pid"], heartbeat=now,
                                 next_try=doc["next_try"], api_ok=None, warn=None, progress=None)
        finally:
            db.close()
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("could not write the crash state to the screener DB: %s", exc)
        return False


# ---------------------------------------------------------------- one collector at a time

class InstanceLock:
    """An OS lock on ``state\\screener_collector.lock`` held for the life of the process (the
    OS releases it when the process ends, even when it is killed). The holder writes its pid
    into the file - only for the message another run shows. Standard library only: one byte
    far past the text is locked (``msvcrt``), so a second process can still read the pid;
    ``fcntl.flock`` where there is no ``msvcrt``."""

    OFFSET = 1 << 20

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def acquire(self) -> bool:
        """Take the lock now (never waits). False when another process holds it."""
        if self.fd is not None:
            return True
        p = self.path
        fresh = not p.parent.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        if fresh and p.parent.name == "state":
            gi = p.parent / ".gitignore"
            if not gi.exists():
                gi.write_text("# runtime state (collector heartbeats) - never committed\n*\n", encoding="utf-8")
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
        try:                                  # only now: a refused run never clobbers the pid
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        except OSError:
            pass
        self.fd = fd
        return True

    def holder(self) -> int | None:
        """The pid the holder wrote (None when unknown)."""
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


def _lock_path() -> Path:
    return STATE_FILE.with_name(LOCK_NAME)


def _hand_run_hint() -> str:
    return ("Before a run by hand: Disable-ScheduledTask -TaskName %s; schtasks /End /TN %s"
            % (TASK_NAME, TASK_NAME))


def _instance_lock(mode: str, max_waits: int | None = None):
    """The lock, or None. A one-off that finds another collector running gives up at once
    (the caller exits 1, nothing written). ``--forever`` waits for it, looking again every
    ``LOCK_WAIT_S`` - writing nothing meanwhile - and goes on when it is free
    (``max_waits``: the tests' hook; None = forever)."""
    lk = InstanceLock(_lock_path())
    if lk.acquire():
        return lk
    pid = lk.holder()
    if mode != "forever":
        log.error("the collector is already running (pid %s). %s", pid or "?", _hand_run_hint())
        return None
    log.warning("another screener collector holds %s (pid %s) - waiting; looked at again every %d s",
                lk.path.name, pid or "?", LOCK_WAIT_S)
    n = 0
    while max_waits is None or n < max_waits:
        _sleep(LOCK_WAIT_S)
        n += 1
        if lk.acquire():
            log.info("the other screener collector has ended - this one starts")
            return lk
    return None


def _crash_if_free(lock, reason: str) -> None:
    """The crash state - only when this run holds the lock or can take it now: a run that
    could not start must never overwrite a RUNNING collector's state."""
    if lock is not None and lock.fd is not None:
        _crash_state(reason)
        return
    lk = InstanceLock(_lock_path())
    try:
        if lk.acquire():
            _crash_state(reason)
    finally:
        lk.release()


# ---------------------------------------------------------------- the runs

def _load_app():
    """(screener_db, massive, scr_collector) - imported fresh on a retry (a module that
    failed is not left in sys.modules)."""
    importlib.invalidate_caches()
    return (importlib.import_module("app.screener_db"), importlib.import_module("app.services.massive"),
            importlib.import_module("app.services.scr_collector"))


def _mode(args) -> str:
    return ("once" if args.once else "universe-now" if args.universe_now else
            "eod-now" if args.eod_now else "history" if args.history else "forever")


def _setup(args, level):
    """Open the log, import the app and migrate the screener DB. Returns (modules, None)
    or (screener_db or None, reason) - the reason already logged with its traceback."""
    try:
        _logging(level, args.log_file)
    except Exception as exc:  # noqa: BLE001
        return None, _why("the log file could not be opened", exc)
    try:
        mods = _load_app()
    except Exception as exc:  # noqa: BLE001
        log.exception("could not load the app modules")
        return None, _why("the app modules could not be loaded", exc)
    if not args.no_init:
        try:
            mods[0].init_screener_db()
        except Exception as exc:  # noqa: BLE001
            log.exception("init_screener_db (the Alembic upgrade of the screener DB) failed")
            return mods[0], _why("the screener database could not be prepared", exc)
    _logging(level, args.log_file)       # Alembic's fileConfig may have replaced the handlers
    return mods, None


def _start(mods, mode: str):
    screener_db, massive, scr_collector = mods
    col = scr_collector.Collector(screener_db.SessionLocal, log=log)
    log.info("screener collector %s pid %d: mode %s, Massive %s (key %s), %d workers, %.0f req/s, "
             "a pass every %d min in the US session, DB %s, state file %s",
             scr_collector.COLLECTOR_VERSION, os.getpid(), mode, massive.base_url(),
             "set" if massive.api_key() else "MISSING", col.workers, col.max_rps, col.cycle_min,
             _db_label(screener_db.database_url()), col.state_path)
    return col


def _stop(col, reason: str) -> None:
    if col is None:
        return
    try:
        col.stop(reason)
    except Exception:  # noqa: BLE001
        log.exception("stopping the collector failed")


def _hold(reason: str) -> None:
    """--forever after a failed start: the crash state rewritten every ``CRASH_BEAT_S``
    until ``SETUP_RETRY_S`` has passed (the next try)."""
    left = SETUP_RETRY_S
    while left > 0:
        step = min(CRASH_BEAT_S, left)
        _sleep(step)
        left -= step
        if left > 0:
            _crash_state(reason, retry_s=left)


def _forever(args, level: int, max_rounds: int | None) -> int:
    """The loop that never gives up: a failed start (imports, the DB set-up, a crash of
    the loop) is reported and tried again every 5 min. Returns EXIT_OK when stopped by
    Ctrl+C; EXIT_SETUP only through the test hook ``max_rounds``."""
    rounds = 0
    while True:
        rounds += 1
        col, sdb = None, None
        try:
            mods, reason = _setup(args, level)
            if reason is None:
                sdb = mods[0]
                col = _start(mods, "forever")
                col.run_forever()             # stops (and heartbeats "stopped") on Ctrl+C
                return EXIT_OK
            sdb = mods
        except KeyboardInterrupt:
            log.info("interrupted")
            _stop(col, "interrupted")
            return EXIT_OK
        except Exception as exc:  # noqa: BLE001
            log.exception("the screener collector crashed")
            reason = _why("the collector crashed", exc)
            _stop(col, "crashed")
        _crash_state(reason, retry_s=SETUP_RETRY_S)          # after stop(): the cause stays
        _crash_db(sdb, reason, retry_s=SETUP_RETRY_S)
        if max_rounds is not None and rounds >= max_rounds:
            return EXIT_SETUP
        log.warning("--forever: could not start (%s); trying again in %d min", reason, SETUP_RETRY_S // 60)
        try:
            _hold(reason)
        except KeyboardInterrupt:
            log.info("interrupted")
            return EXIT_OK


def _one_off(args, mode: str, level: int) -> int:
    mods, reason = _setup(args, level)
    if reason is not None:
        _crash_state(reason)
        _crash_db(mods, reason)
        return EXIT_SETUP
    col, crash = None, None
    try:
        col = _start(mods, mode)
        if mode == "once":
            res = col.run_once()
        elif mode == "universe-now":
            res = col.run_universe()
        elif mode == "eod-now":
            res = col.run_eod()
        else:
            res = col.run_history(args.history)
        log.info("%s: %s", mode, res)
        if not res.get("ok"):
            log.error("the --%s run failed: %s", mode, res.get("error") or "unknown error")
            return EXIT_SOURCE
        if res.get("nothing"):
            log.warning("the --%s run read nothing that came out done", mode)
            return EXIT_NOTHING
        return EXIT_OK
    except KeyboardInterrupt:
        log.info("interrupted")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        log.exception("the screener collector crashed")
        crash = _why("the collector crashed", exc)
        return EXIT_SETUP
    finally:
        _stop(col, "one-off --%s run finished" % mode)
        if crash is not None:                                # after stop(): the cause stays
            _crash_state(crash)
            _crash_db(mods[0], crash)


def main(argv=None, *, max_rounds: int | None = None, max_lock_waits: int | None = None) -> int:
    """The CLI. ``max_rounds`` (tests only) ends --forever after that many failed starts;
    ``max_lock_waits`` (tests only) ends a --forever that waits for another collector."""
    lock = None
    try:
        args = parse_args(argv)
        mode = _mode(args)
        level = logging.DEBUG if args.verbose else logging.INFO
        try:
            _logging(level, args.log_file)
        except Exception:  # noqa: BLE001
            if mode != "forever":
                raise
            # --forever: _setup opens it again each round and reports it until it works
        lock = _instance_lock(mode, max_lock_waits)      # before ANY state / status write
        if lock is None:
            return EXIT_SETUP
        if mode == "forever":
            return _forever(args, level, max_rounds)
        return _one_off(args, mode, level)
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - the last guard (e.g. the log file cannot be opened)
        try:
            log.exception("the screener collector crashed")
        except Exception:  # noqa: BLE001
            pass
        _crash_if_free(lock, _why("the collector crashed", exc))
        return EXIT_SETUP
    finally:
        if lock is not None:
            lock.release()


def _db_label(url: str) -> str:
    """The DB URL without a password (a Postgres URL may carry one)."""
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return url.split("@")[-1]


if __name__ == "__main__":
    raise SystemExit(main())
