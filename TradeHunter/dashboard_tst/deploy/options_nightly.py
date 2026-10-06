r"""Nightly Options job - the run behind the Options page's cards.

Walks every basket ticker (plus every open option trade's underlying): the
delayed chain from Cboe (or Alpaca) into ``option_chain_snapshot``, the day's vol
statistics into ``iv_daily``, one ``option_signal`` row per distinct rule set, then
the Positions sweep, the retention prune and the Telegram ideas push; records an
``option_jobs`` row the status strip and the nav badge read (II.2.16).

Scheduled on **Hermes** (Windows Server 2019, PowerShell) by
``deploy\setup_options_nightly_task.ps1`` at 07:15 local (after the 06:00
Portfolio check and the 06:30 Spread scan, so two jobs never hit Cboe at once).
Run by hand from the same box:

    cd C:\trading-skills\TradeHunter\dashboard_tst
    .\.venv\Scripts\python.exe deploy\options_nightly.py                 # every basket ticker
    .\.venv\Scripts\python.exe deploy\options_nightly.py NVDA LRCX       # just these
    .\.venv\Scripts\python.exe deploy\options_nightly.py --no-backfill   # skip the IV-history backfill (on by default)
    .\.venv\Scripts\python.exe deploy\options_nightly.py --no-push       # skip Telegram entirely
    .\.venv\Scripts\python.exe deploy\options_nightly.py --telegram-dry-run   # compose + log, send nothing
    .\.venv\Scripts\python.exe deploy\options_nightly.py --on 2026-10-02 --source alpaca -v
    .\.venv\Scripts\python.exe deploy\options_nightly.py --no-engines    # snapshot + metrics only

The scheduled task appends stdout + stderr to ``logs\options_nightly.log``; by
hand the same lines go to the console. Exit 0 = ran (a missing chain is counted,
not fatal), 1 = could not run (an unknown ``--source``, a bad ``--on``, the DB
unreachable, a crash before the job row).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# The Telegram text and the verdict strings carry arrows and em-dashes; Windows
# consoles and redirected files default to cp1252, which turns those into "?".
# Ask for UTF-8 and carry on if the stream does not support reconfiguring.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a TextIOWrapper, or already closed
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("options_nightly")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", help="limit to these symbols (default: the basket universe)")
    ap.add_argument("--on", default=None, help="file under this YYYY-MM-DD instead of today (ET)")
    ap.add_argument("--source", default=None,
                    help="chain source for this run: cboe | alpaca (default: TST_OPTIONS_SOURCE)")
    ap.add_argument("--backfill", action="store_true",
                    help="(the default since v4.131; kept for old scripts) copy the screener's iv_history "
                         "into iv_daily and seed a year from IB Gateway for tickers still short")
    ap.add_argument("--no-backfill", action="store_true",
                    help="skip the IV-history backfill step entirely")
    ap.add_argument("--no-push", action="store_true", help="skip the Telegram push entirely")
    ap.add_argument("--telegram-dry-run", action="store_true",
                    help="compose and log the Telegram messages, send nothing (dedupe rows still written)")
    ap.add_argument("--no-engines", action="store_true",
                    help="snapshot + metrics only: no chart read, no signal rows (also what happens "
                         "on its own when the engine modules are missing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s")

    from app.db import SessionLocal, init_db
    from app.services import option_data, option_nightly
    from app.services.option_quotes import ChainError

    # A typo in --source (or TST_OPTIONS_SOURCE) must fail loudly BEFORE a job row is
    # written, not per symbol - exit 1 so the task history shows it.
    try:
        src = option_data.source(args.source)
    except ChainError as exc:
        log.error("%s", exc)
        return 1

    init_db()
    # alembic's fileConfig resets logging - re-assert (same as portfolio_daily_check)
    log.disabled = False
    log.setLevel(level)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s")
    # fileConfig also DISABLES every logger that existed before it ran - the app's
    # module loggers were created on import, so the per-symbol lines would vanish.
    for name in ("app.services.option_nightly", "app.services.telegram_push",
                 "app.services.telegram", "app.services.option_store", "app.services.option_data"):
        lg = logging.getLogger(name)
        lg.disabled = False
        lg.setLevel(level)

    db = SessionLocal()
    t0 = time.time()
    done = {"n": 0, "err": 0}

    def progress(sym, err):
        done["n"] += 1
        if err:
            done["err"] += 1
        if done["n"] % 10 == 0 or err:
            log.info("  %4d done (%d errors) %s%s", done["n"], done["err"], sym,
                     f": {err}" if err else "")

    try:
        syms = [s.upper() for s in args.symbols] or None
        res = option_nightly.run_nightly(
            db, symbols=syms, on=args.on, source=src, push=not args.no_push,
            telegram_dry_run=args.telegram_dry_run, backfill=not args.no_backfill,
            engines=not args.no_engines, progress=progress, log=log)
        tg = res.get("telegram") or {}
        log.info("nightly %s (job #%s): %d symbols, %d ok, %d errors, %s rows, %d signal rows, "
                 "engines %s, pushed %d (%s), %.0fs",
                 res["run_on"], res["job_id"], res["symbols"], res["ok"], res["errors"],
                 f"{res['rows']:,}", res["signals"], res["engines"], res["pushed"],
                 "dry run" if args.telegram_dry_run else ("off" if args.no_push else
                                                          f"composed {tg.get('ideas', 0)}"),
                 time.time() - t0)
        return 0
    except Exception:  # noqa: BLE001
        log.exception("nightly run failed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
