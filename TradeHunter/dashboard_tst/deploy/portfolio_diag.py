r"""Read-only diagnosis of "the Portfolio board never loads".

The page shell renders and then asks for ``/portfolio/list``; if that request
fails or hangs the member only ever sees "Checking your positions...". This
script does what that request does, step by step, against the SAME database
the app uses, and prints the first thing that goes wrong instead of swallowing
it: the migration state, the columns the code expects, how long each Cboe chain
fetch takes, and the full traceback of the render if there is one.

Run on **Hermes** (where the production tst.db lives):

    cd C:\trading-skills\TradeHunter\dashboard_tst
    .\.venv\Scripts\python.exe deploy\portfolio_diag.py

Read-only: it opens a session, SELECTs, renders in memory, and closes. It does
NOT write today's check row (the page-load path would).
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    import sqlalchemy as sa

    from app import __version__
    from app.config import settings
    from app.db import SessionLocal, engine

    print(f"app version   : {__version__}")
    print(f"database_url  : {settings.database_url}")

    insp = sa.inspect(engine)
    tables = set(insp.get_table_names())
    try:
        with engine.connect() as c:
            stamped = c.execute(sa.text("select version_num from alembic_version")).scalar()
    except Exception as exc:  # noqa: BLE001
        stamped = f"(no alembic_version table: {exc})"
    print(f"alembic stamp : {stamped}   (v4.62 expects c0d1e2f3a4b5)")

    want = {
        "option_spreads": ["roll_delta", "loss_stop_pct", "profit_target_pct", "dte_floor",
                           "short_price", "long_price", "short_entry_delta",
                           "long_entry_delta", "entry_iv"],
        "spread_checks": ["long_delta", "net_delta", "theta", "long_iv", "profit_pct"],
    }
    ok = True
    for t, cols in want.items():
        if t not in tables:
            print(f"TABLE MISSING : {t}")
            ok = False
            continue
        have = {c["name"] for c in insp.get_columns(t)}
        missing = [c for c in cols if c not in have]
        print(f"{t:14}: {'all columns present' if not missing else 'MISSING ' + ', '.join(missing)}")
        ok = ok and not missing
    if not ok:
        print("\n=> The migration did not apply. The app's startup runs it; check dashboard.log "
              "for an alembic error, or run:  .\\.venv\\Scripts\\alembic.exe upgrade head")
        return 1

    from app.models import OptionSpread, User
    from app.services import option_quotes

    db = SessionLocal()
    try:
        rows = db.query(OptionSpread).order_by(OptionSpread.user_id, OptionSpread.symbol).all()
        print(f"\nspreads       : {len(rows)} total, "
              f"{sum(1 for r in rows if r.status == 'open')} open")
        for r in rows:
            print(f"  #{r.id} user={r.user_id} {r.symbol} {r.short_strike:g}/{r.long_strike:g}P "
                  f"{r.expiry} credit={r.credit} x{r.contracts} {r.status}")

        syms = sorted({r.symbol for r in rows if r.status == "open"})
        print(f"\ncboe fetch    : {len(syms)} underlying(s)")
        for s in syms:
            t0 = time.time()
            try:
                ch = option_quotes.fetch_chain(s)
                print(f"  {s:6} ok  spot={ch.get('spot')} legs={len(ch.get('legs') or {})} "
                      f"{time.time() - t0:.1f}s")
            except Exception as exc:  # noqa: BLE001
                print(f"  {s:6} FAIL {type(exc).__name__}: {exc}  {time.time() - t0:.1f}s")

        # Render exactly what /portfolio/list renders, per member, without writing.
        from fastapi import Request

        from app.routes import portfolio as pf

        users = {r.user_id for r in rows}
        print(f"\nrender        : {len(users)} member(s) with spreads")
        scope = {"type": "http", "method": "GET", "path": "/portfolio/list", "headers": [],
                 "query_string": b"", "scheme": "http", "server": ("localhost", 8000),
                 "client": ("127.0.0.1", 0), "root_path": ""}
        for uid in sorted(users):
            u = db.get(User, uid)
            t0 = time.time()
            try:
                ctx = pf._list_context(db, u, record=False)
                html = pf.templates.TemplateResponse(Request(scope), "_portfolio_list.html", ctx)
                print(f"  user {uid} ({u.email}): OK, {len(html.body)} bytes, "
                      f"{len(ctx['items'])} row(s), {time.time() - t0:.1f}s")
            except Exception:  # noqa: BLE001
                print(f"  user {uid} ({u.email}): FAILED after {time.time() - t0:.1f}s")
                traceback.print_exc()
                return 1
        db.rollback()
    finally:
        db.close()
    print("\nNo failure reproduced here. If the page still hangs, it is the browser side: "
          "check dashboard.log for the /portfolio/list status, and the browser's "
          "Network tab for that request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
