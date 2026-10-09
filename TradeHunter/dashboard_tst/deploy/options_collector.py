r"""Hermes options collector - the always-on IBKR reader behind the Options page
(OPTIONS_V2_DESIGN.md section 4). The loop itself is ``app/services/opt_collector.py``;
this file wires it to IB Gateway (``bridge/th_ibkr.py`` + ib_insync) and the app DB.

Runs on **Hermes** (Windows Server 2019, PowerShell) as the scheduled task
``TST-Options-Collector`` (``deploy\setup_options_collector_task.ps1``), which starts
it with ``--forever --log-file logs\options_collector.log`` at startup and daily 07:00:
the log rotates at 5 MB and keeps 5 old files (``options_collector.log.1`` ... ``.5``),
so it never grows past ~30 MB. Without ``--log-file`` the log goes to stdout. By hand,
from the same box (stop the task first - two collectors would fight over clientId 89):

    cd C:\trading-skills\TradeHunter\dashboard_tst
    .\.venv\Scripts\python.exe deploy\options_collector.py               # --forever, log on screen
    .\.venv\Scripts\python.exe deploy\options_collector.py --once        # one full pass, then exit
    .\.venv\Scripts\python.exe deploy\options_collector.py --history NVDA LRCX   # re-pull their history
    .\.venv\Scripts\python.exe deploy\options_collector.py --eod-now     # the EOD pass now
    .\.venv\Scripts\python.exe deploy\options_collector.py --once -v --port 4002
    .\.venv\Scripts\python.exe deploy\options_collector.py --history NVDA --ignore-ingest
    .\.venv\Scripts\python.exe deploy\options_collector.py --once --log-file logs\options_collector.log

Living with the ingest supervisor (scripts\ingest_supervisor.py, which owns the Hermes
IB Gateway): the Gateway is OFF Mon-Fri 08:00-20:10 ET for the user's manual trading,
opened at 20:10 ET for the nightly top-up and closed again when it is done. While it is
down by design the collector's state is "waiting" (no error, a try every minute) - the
members' IBKR connectors carry the session. Historical requests (first-time history,
the EOD increments) share IBKR's per-login pacing with the top-up, so they wait until
the supervisor's state file shows tonight's top-up done (or a weekend; or no supervisor
on this PC). Chain quotes are never held back. --ignore-ingest overrides the wait for a
one-off run (--once / --history / --eod-now) - only when no top-up is running.

IBKR workload, so Python 3.12 (CLAUDE.md): the dashboard venv is built with py -3.12
and lists ib_insync; ``py -3.12`` with the app requirements works too. Python 3.14
cannot import ib_insync.

Environment (app\.env or the task's environment):
    TST_IBKR_PORT                    the API port; unset = try 4002, 4001, 7497, 7496
    TST_OPTIONS_COLLECTOR_CLIENT_ID  default 89 (CLAUDE.md clientId table)
    TST_OPTIONS_MAX_LINES            market-data lines per wave, default 60

Exit codes: 0 = ran (single symbols that failed are logged, not fatal; --forever
stopped by Ctrl+C; --once / --eod-now with their history left for later are logged);
1 = could not start (ib_insync missing / wrong Python, the app or the DB not loadable,
a crash); 2 = IB Gateway / TWS not reachable (--once, --history, --eod-now only -
--forever keeps retrying, 60 s doubling to 5 min; every 60 s while the Gateway is down
by design); 3 = --history not run: the history waits for tonight's ingest top-up
(rerun after it, or add --ignore-ingest).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Windows consoles and redirected files default to cp1252; log lines stay ASCII but a
# symbol or an IBKR error text may not.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DASH = Path(__file__).resolve().parent.parent
for _p in (str(DASH), str(DASH / "bridge")):     # the app package; th_ibkr (shipped in bridge/)
    if _p not in sys.path:
        sys.path.insert(0, _p)

log = logging.getLogger("options_collector")

EXIT_OK, EXIT_SETUP, EXIT_GATEWAY, EXIT_DEFERRED = 0, 1, 2, 3
_FMT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024     # --log-file rotates at 5 MB ...
LOG_BACKUPS = 5                     # ... and keeps 5 old files (at most ~30 MB in all)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--forever", action="store_true",
                      help="run the loop until stopped (the default; what the scheduled task runs)")
    mode.add_argument("--once", action="store_true",
                      help="one full pass now - history where missing, then every basket symbol - then exit")
    mode.add_argument("--history", nargs="+", metavar="SYM",
                      help="pull 2 years of bars + 1 year of IV (and a chain) for these symbols, then exit")
    mode.add_argument("--eod-now", action="store_true",
                      help="run the end-of-day pass now (snapshot, history increments, earnings, prune), then exit")
    ap.add_argument("--port", type=int, default=None,
                    help="IB API port (default: TST_IBKR_PORT, else try 4002, 4001, 7497, 7496)")
    ap.add_argument("--client-id", type=int, default=None,
                    help="IB client id (default: TST_OPTIONS_COLLECTOR_CLIENT_ID, else 89)")
    ap.add_argument("--ignore-ingest", action="store_true",
                    help="one-off runs: pull history even while the ingest supervisor's nightly "
                         "top-up is pending (they share IBKR's historical pacing - use only when "
                         "no top-up is running)")
    ap.add_argument("--no-init", action="store_true",
                    help="skip init_db() (the Alembic upgrade at start)")
    ap.add_argument("--log-file", default=None, metavar="PATH",
                    help="log to this file instead of stdout, rotated at 5 MB with 5 old files kept "
                         "(what the scheduled task passes: logs\\options_collector.log)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


class IbLogNoise(logging.Filter):
    """Drops ib_insync's per-attempt chatter, which the collector already reports in
    its own (throttled) lines:

    * ``ib_insync.client`` connection lines - every connect attempt logs "Connecting
      to ...", "Disconnecting", and on a refused port two ERRORs ("API connection
      failed: ConnectionRefusedError", "Make sure API port on TWS/IBG is open"). While
      the supervisor keeps the Gateway down (most of every weekday) the collector tries
      every minute, up to four ports: ~2 MB a day of ERROR lines burying real errors.
      ("Peer closed connection ... clientId already in use?" is kept.)
    * ``Unknown contract: Option(...)`` (ib_insync.ib) and ``Error 200 ... No security
      definition`` (ib_insync.wrapper) - one each per union strike an expiry does not
      list; the chain read counts them (``unknown``).

    Every other ib_insync line (pacing 162, HMDS 354 / 10197, farm status) is kept."""

    _CLIENT = ("Connecting to ", "Connected", "Disconnecting", "Disconnected",
               "API connection failed", "API connection ready", "Make sure API port")

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        if not name.startswith("ib_insync"):
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if name == "ib_insync.client":
            return not msg.startswith(self._CLIENT)
        if name == "ib_insync.ib":
            return not msg.startswith("Unknown contract")
        if name == "ib_insync.wrapper":
            return not msg.startswith("Error 200,")
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
    gets ``IbLogNoise``."""
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
        if not any(isinstance(f, IbLogNoise) for f in h.filters):
            h.addFilter(IbLogNoise())
    root.setLevel(level)
    for name in ("options_collector", "app.services.opt_collector", "app.services.opt_store",
                 "app.services.option_store", "app.services.prices"):
        lg = logging.getLogger(name)
        lg.disabled = False
        lg.setLevel(level)


def main(argv=None) -> int:
    args = parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    _logging(level, args.log_file)

    # ib_insync's eventkit asks for the current event loop at import: make it first,
    # and keep it - the IB connection lives on it for the life of the process.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return _run(args, level, loop)
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass
        asyncio.set_event_loop(None)


def _run(args, level: int, loop) -> int:
    try:
        import ib_insync  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        log.error("ib_insync cannot be imported (%s: %s). Run with the dashboard venv "
                  "(.venv, built with py -3.12) or py -3.12 with ib_insync installed - "
                  "Python 3.14 cannot load it.", type(exc).__name__, exc)
        return EXIT_SETUP
    try:
        import th_ibkr
        from app.db import SessionLocal, init_db
        from app.services import opt_collector
    except Exception:  # noqa: BLE001
        log.exception("could not load the app / bridge modules")
        return EXIT_SETUP
    if not args.no_init:
        try:
            init_db()
        except Exception:  # noqa: BLE001
            log.exception("init_db (the Alembic upgrade) failed")
            return EXIT_SETUP
    _logging(level, args.log_file)

    ports = (args.port,) if args.port else opt_collector.ib_ports()
    cid = args.client_id if args.client_id is not None else opt_collector.env_client_id()
    connect = opt_collector.ib_connector(ports=ports, client_id=cid, log=log)
    col = opt_collector.Collector(SessionLocal, fetch=th_ibkr, connect=connect, log=log, loop=loop)
    mode = ("once" if args.once else "history" if args.history else
            "eod-now" if args.eod_now else "forever")
    log.info("options collector %s (th_ibkr %s) pid %d: mode %s, clientId %d, ports %s, "
             "%d lines per wave, state file %s", opt_collector.COLLECTOR_VERSION,
             getattr(th_ibkr, "VERSION", "?"), os.getpid(), mode, cid,
             ", ".join(str(p) for p in ports), col.max_lines, col.state_path)

    try:
        if mode == "forever":
            col.run_forever()           # stops (and heartbeats "stopped") on Ctrl+C
            return EXIT_OK
        if mode == "once":
            res = col.run_once(ignore_ingest=args.ignore_ingest)
        elif mode == "history":
            res = col.run_history(args.history, ignore_ingest=args.ignore_ingest)
        else:
            res = col.run_eod(ignore_ingest=args.ignore_ingest)
        log.info("%s: %s", mode, res)
        if res.get("deferred"):
            log.warning("history not pulled: %s. Rerun after tonight's top-up, or add "
                        "--ignore-ingest when no top-up is running.", res.get("why"))
            return EXIT_DEFERRED
        if res.get("history_deferred"):
            log.warning("history of %s symbol(s) left for later: %s (the collector catches "
                        "it up once the top-up is done).", res["history_deferred"],
                        opt_collector.HISTORY_WAIT_TEXT)
        if not res.get("connected"):
            log.error("IB Gateway / TWS not reachable: %s", res.get("error"))
            return EXIT_GATEWAY
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
