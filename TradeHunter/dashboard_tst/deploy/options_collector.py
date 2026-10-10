r"""Hermes options collector - the always-on Massive reader behind the Options page
(OPTIONS_V2_DESIGN.md section 13.4). The loop itself is ``app/services/opt_collector.py``;
this file wires it to the app DB and the log file.

Data: Massive (formerly Polygon.io) - Options Starter (the whole-chain snapshot with
greeks / IV / open interest, 15 min delayed, no bid/ask) and Stocks Basic (end-of-day
daily bars, about 5 requests a minute). The key is ``TST_MASSIVE_API_KEY`` in
``app\.env`` - it is never printed or logged (the startup line says only "key set" /
"key MISSING"). Plain HTTPS: any Python the app runs on works (the dashboard venv).

Runs on **Hermes** (Windows Server 2019, PowerShell) as the scheduled task
``TST-Options-Collector`` (``deploy\setup_options_collector_task.ps1``), which starts
it with ``--forever --log-file logs\options_collector.log`` at startup and daily 07:00:
the log rotates at 5 MB and keeps 5 old files (``options_collector.log.1`` ... ``.5``),
so it never grows past ~30 MB. Without ``--log-file`` the log goes to stdout. By hand,
from the same box (stop the task first, so two loops do not read the same chains):

    cd C:\trading-skills\TradeHunter\dashboard_tst
    .\.venv\Scripts\python.exe deploy\options_collector.py               # --forever, log on screen
    .\.venv\Scripts\python.exe deploy\options_collector.py --once        # one full pass, then exit
    .\.venv\Scripts\python.exe deploy\options_collector.py --history NVDA LRCX   # re-read their history
    .\.venv\Scripts\python.exe deploy\options_collector.py --eod-now     # the end-of-day pass now
    .\.venv\Scripts\python.exe deploy\options_collector.py --once -v --log-file logs\options_collector.log

Environment (app\.env):
    TST_MASSIVE_API_KEY     the Massive key (required)
    TST_MASSIVE_BASE_URL    default https://api.massive.com
    TST_OPTIONS_CYCLE_MIN   minutes between passes in the US session, default 15

Exit codes: 0 = ran (single tickers that failed are logged, not fatal; --forever stopped
by Ctrl+C); 1 = could not start (the app or the DB not loadable, a crash); 2 = Massive
not usable for a one-off run (--once, --history, --eod-now): no key on this PC, the key
rejected, the plan lacks an endpoint, or Massive not reachable. --forever never exits on
those - it reports them (state error on the Options page and the tray) and retries.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

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

log = logging.getLogger("options_collector")

EXIT_OK, EXIT_SETUP, EXIT_SOURCE = 0, 1, 2
_FMT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024     # --log-file rotates at 5 MB ...
LOG_BACKUPS = 5                     # ... and keeps 5 old files (at most ~30 MB in all)
KEY_ENV = "TST_MASSIVE_API_KEY"
# Libraries that log one line per HTTP request (the request URL, cursor included): a
# pass over 100 tickers is ~1,000-2,500 requests. Kept to warnings and errors.
QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "hpack")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--forever", action="store_true",
                      help="run the loop until stopped (the default; what the scheduled task runs)")
    mode.add_argument("--once", action="store_true",
                      help="one full pass now - history where missing, then every basket ticker - then exit")
    mode.add_argument("--history", nargs="+", metavar="SYM",
                      help="read 2 years of daily bars + the IV history (and the chain) of these tickers, then exit")
    mode.add_argument("--eod-now", action="store_true",
                      help="run the end-of-day pass now (chains, the day's record, stock bars, earnings, prune), then exit")
    ap.add_argument("--no-init", action="store_true",
                    help="skip init_db() (the Alembic upgrade at start)")
    ap.add_argument("--log-file", default=None, metavar="PATH",
                    help="log to this file instead of stdout, rotated at 5 MB with 5 old files kept "
                         "(what the scheduled task passes: logs\\options_collector.log)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="debug lines too (one per ticker of every session pass)")
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
    """The log file's rotator. Windows will not rename a file another process holds
    open - a ``Get-Content -Wait`` tail does - so then the file is copied and emptied
    instead: the rollover still happens and no line is lost."""
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
    LOG_BACKUPS). Called again after init_db: Alembic's fileConfig resets the root
    handlers (closing the file's), adds its own stderr console and disables every
    logger that existed before it ran - so the file handler is put back and, with a log
    file, the console is dropped (nothing reads the task's console). Every root handler
    gets ``KeyScrub``; the per-request HTTP loggers stay at WARNING."""
    root = logging.getLogger()
    if log_file:
        path = Path(log_file).resolve()
        for h in [h for h in root.handlers if _is_console(h)]:
            root.removeHandler(h)
        if not any(isinstance(h, RotatingFileHandler)
                   and os.path.normcase(h.baseFilename) == os.path.normcase(str(path))
                   for h in root.handlers):
            root.addHandler(_log_handler(path))
        logging.captureWarnings(True)            # Python warnings land in the file too
    elif not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(_FMT))
        root.addHandler(h)
    for h in root.handlers:
        if not any(isinstance(f, KeyScrub) for f in h.filters):
            h.addFilter(KeyScrub())
    root.setLevel(level)
    for name in ("options_collector", "app.services.opt_collector", "app.services.opt_massive",
                 "app.services.massive", "app.services.opt_store", "app.services.option_store",
                 "app.services.prices"):
        lg = logging.getLogger(name)
        lg.disabled = False
        lg.setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv=None) -> int:
    args = parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    _logging(level, args.log_file)
    try:
        from app.db import SessionLocal, init_db
        from app.services import massive, opt_collector
    except Exception:  # noqa: BLE001
        log.exception("could not load the app modules")
        return EXIT_SETUP
    if not args.no_init:
        try:
            init_db()
        except Exception:  # noqa: BLE001
            log.exception("init_db (the Alembic upgrade) failed")
            return EXIT_SETUP
    _logging(level, args.log_file)

    col = opt_collector.Collector(SessionLocal, log=log)
    mode = ("once" if args.once else "history" if args.history else
            "eod-now" if args.eod_now else "forever")
    log.info("options collector %s pid %d: mode %s, Massive %s (key %s), a pass every %d min in "
             "the US session, state file %s", opt_collector.COLLECTOR_VERSION, os.getpid(), mode,
             massive.base_url(), "set" if massive.api_key() else "MISSING", col.cycle_min,
             col.state_path)

    try:
        if mode == "forever":
            col.run_forever()           # stops (and heartbeats "stopped") on Ctrl+C
            return EXIT_OK
        if mode == "once":
            res = col.run_once()
        elif mode == "history":
            res = col.run_history(args.history)
        else:
            res = col.run_eod()
        log.info("%s: %s", mode, res)
        if not res.get("ok"):
            log.error("Massive not usable: %s", res.get("error") or "unknown error")
            return EXIT_SOURCE
        return EXIT_OK
    except KeyboardInterrupt:
        log.info("interrupted")
        return EXIT_OK
    except Exception:  # noqa: BLE001
        log.exception("the collector crashed")
        return EXIT_SETUP
    finally:
        if mode != "forever":
            col.stop("one-off --%s run finished" % mode)


if __name__ == "__main__":
    raise SystemExit(main())
