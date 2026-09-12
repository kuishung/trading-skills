r"""One-off seed of ``iv_history`` from IB Gateway: a year of daily implied
volatility per symbol, so the Spread screen's IV-percentile column is correct on
day one instead of after a year of nightly Cboe readings.

Run on **Hermes** (Windows Server 2019, PowerShell), where IB Gateway runs under
IBC. This is an IBKR workload, so per the CLAUDE.md rule it needs Python 3.12
with ib_insync - the TradeHunter root interpreter, NOT the dashboard venv:

    cd C:\trading-skills\TradeHunter\dashboard_tst
    py -3.12 -m pip install ib_insync sqlalchemy python-dotenv alembic   # once, if missing
    py -3.12 deploy\iv_seed_ibkr.py                 # whole universe, skips symbols already seeded
    py -3.12 deploy\iv_seed_ibkr.py NVDA AAPL MSFT  # just these
    py -3.12 deploy\iv_seed_ibkr.py --force         # re-pull even if history exists

Pacing: one request per symbol, ~1 s apart (historical IV bars are cheap and
not under the 60-per-600 s cap that OHLCV pulls are), so ~550 symbols take
about 10-15 minutes. clientId 87 (allocation table in CLAUDE.md: 84 ingest,
85 health, 86 options bridge).

Writes through the app's own SQLAlchemy session so the database URL is the
same one the web app uses (``TST_DATABASE_URL`` / app/.env). Portable upserts.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("iv_seed")

CLIENT_ID = 87
HOST, PORT = "127.0.0.1", 7496       # TWS live 7496 / paper 7497 / Gateway 4001 / 4002
ENOUGH = 200                          # rows already present -> skip unless --force


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    try:
        from ib_insync import IB, Stock
    except Exception as exc:  # noqa: BLE001
        log.error("ib_insync not importable (%s). Use py -3.12 with ib_insync installed.", exc)
        return 1

    from app.db import SessionLocal, init_db
    from app.models import IVHistory
    from app.services import spread_scan

    init_db()
    log.disabled = False
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    db = SessionLocal()
    syms = [s.upper() for s in args.symbols] or spread_scan.universe(db)
    log.info("%d symbol(s)", len(syms))

    ib = IB()
    try:
        ib.connect(HOST, args.port, clientId=CLIENT_ID, readonly=True, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log.error("cannot connect to IB on %s:%s (clientId %s): %s", HOST, args.port, CLIENT_ID, exc)
        return 1
    log.info("connected: %s", ib.client.serverVersion())

    done = skipped = failed = 0
    try:
        for i, sym in enumerate(syms, 1):
            if not args.force:
                n = db.query(IVHistory).filter(IVHistory.symbol == sym).count()
                if n >= ENOUGH:
                    skipped += 1
                    continue
            try:
                c = Stock(sym.replace(".", " "), "SMART", "USD")
                bars = ib.reqHistoricalData(
                    c, endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
                    whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True, formatDate=1,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("  %-6s failed: %s", sym, exc)
                time.sleep(args.pause)
                continue
            rows = 0
            for b in bars or []:
                d = b.date if isinstance(b.date, _dt.date) else _dt.date.fromisoformat(str(b.date)[:10])
                iv = float(b.close)
                if iv <= 0:
                    continue
                # IB returns a fraction (0.33); the table holds percent like Cboe's iv30.
                spread_scan.record_iv(db, sym, d.isoformat(), iv * 100.0, None, source="ibkr")
                rows += 1
            db.commit()
            done += 1
            log.info("  %4d/%d %-6s %d days", i, len(syms), sym, rows)
            time.sleep(args.pause)
    finally:
        ib.disconnect()
        db.close()
    log.info("seeded %d, skipped %d (already had history), failed %d", done, skipped, failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
