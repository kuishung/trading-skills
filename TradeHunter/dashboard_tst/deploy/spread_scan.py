r"""Nightly bull put spread scan - the job behind Options > Spread.

Fetches every chain in the universe (S&P 500 + watchlists + the MATP board)
from Cboe's delayed feed, builds every candidate spread, files them in
``spread_candidates`` and today's IV30 in ``iv_history``, and records a
``spread_scans`` row the page shows as its freshness pill.

Scheduled on **Hermes** (Windows Server 2019, PowerShell) by
``deploy\setup_spread_scan_task.ps1`` at 06:30 local (after the 06:00 Portfolio
check). Run by hand from the same box:

    cd C:\trading-skills\TradeHunter\dashboard_tst
    .\.venv\Scripts\python.exe deploy\spread_scan.py            # full universe
    .\.venv\Scripts\python.exe deploy\spread_scan.py NVDA AAPL  # just these
    .\.venv\Scripts\python.exe deploy\spread_scan.py --pause 2 -v

Exit 0 = scanned, 1 = failed. Errors on individual symbols (no chain, Cboe
403) are counted, not fatal.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("spread_scan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbols", nargs="*", help="limit to these symbols (default: the universe)")
    ap.add_argument("--on", default=None, help="file under this YYYY-MM-DD instead of today (ET)")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel chain fetches (default 1 - Cboe rate-limits bursts)")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between fetches (default 1.5)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s")

    from app.db import SessionLocal, init_db
    from app.services import spread_scan

    init_db()
    # alembic's fileConfig resets logging - re-assert (same as portfolio_daily_check)
    log.disabled = False
    log.setLevel(level)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s")

    db = SessionLocal()
    t0 = time.time()
    done = {"n": 0, "err": 0}

    def progress(sym, err):
        done["n"] += 1
        if err:
            done["err"] += 1
        if done["n"] % 25 == 0 or err:
            log.info("  %4d fetched (%d errors) %s%s", done["n"], done["err"], sym,
                     f": {err}" if err else "")

    try:
        syms = [s.upper() for s in args.symbols] or None
        res = spread_scan.run_scan(db, symbols=syms, on=args.on, workers=args.workers,
                                   pause=args.pause, fresh=True, progress=progress)
        log.info("scan %s: %d symbols, %d priced, %d candidates, %d errors, pruned %d, %.0fs",
                 res["scan_on"], res["symbols"], res["priced"], res["candidates"],
                 res["errors"], res["pruned"], time.time() - t0)
        return 0
    except Exception:  # noqa: BLE001
        log.exception("scan failed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
