"""Daily monitoring sweep for every member's open option spreads.

Run once a day, after the US close. For each open spread it reads the underlying's
Cboe chain, grades the position against that member's exit lines (short-put delta
and percent of max loss) and files the result as that trading day's
``SpreadCheck`` row.

Why a separate process rather than a thread inside the web app: the sweep is the
one thing that must happen whether or not anybody opens the site, and a background
task inside uvicorn dies with the worker, doubles up if the app is ever run with
more than one worker, and is invisible when it fails. A scheduled task has an exit
code and a log.

    # Hermes (Windows Server 2019) - PowerShell
    C:\\trading-skills\\TradeHunter\\dashboard_tst\\.venv\\Scripts\\python.exe `
        C:\\trading-skills\\TradeHunter\\dashboard_tst\\deploy\\portfolio_daily_check.py

Exit codes: 0 = swept (even if some chains failed - a missing quote is a normal
Tuesday, not a job failure), 1 = the sweep itself could not run.

Nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# The verdict strings carry em-dashes (they are written for the page, and the log
# quotes them verbatim so the two can be compared). Windows consoles and redirected
# files default to cp1252, which turns those into "?" and makes the log harder to
# read than the UI it is supposed to mirror. Ask for UTF-8 and carry on if the
# stream does not support reconfiguring.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a TextIOWrapper, or already closed
        pass

# dashboard_tst/deploy/x.py -> dashboard_tst/ on the path, so `app` imports work
# the same way they do under uvicorn.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("portfolio_check")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user-id", type=int, default=None,
                    help="limit the sweep to one member (default: everyone)")
    ap.add_argument("--on", default=None,
                    help="file the checks under this YYYY-MM-DD instead of today (ET)")
    ap.add_argument("--dry-run", action="store_true",
                    help="grade and print, write nothing")
    ap.add_argument("--no-discord", action="store_true",
                    help="skip the Discord post even if a webhook is configured")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    from app.db import SessionLocal, init_db
    from app.models import OptionSpread
    from app.services import spread_monitor

    # The app normally runs migrations at startup; the sweep may be the first
    # thing to touch a fresh database, so it must not assume that happened.
    init_db()

    # Alembic's env.py calls logging.config.fileConfig(), which defaults to
    # disable_existing_loggers=True AND resets the root level to alembic.ini's
    # WARN. Between them the sweep runs correctly and reports nothing at INFO,
    # which is the worst failure mode available to a job whose entire output is a
    # log. Re-enabling is not enough; the level has to be re-asserted too, on THIS
    # logger (a handler on root still emits, because ancestor logger levels are
    # not re-checked once a record is created).
    log.disabled = False
    log.setLevel(level)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level,
                            format="%(asctime)s %(levelname)-7s %(message)s")

    db = SessionLocal()
    try:
        if args.dry_run:
            q = db.query(OptionSpread).filter(OptionSpread.status == "open")
            if args.user_id is not None:
                q = q.filter(OptionSpread.user_id == args.user_id)
            rows = q.order_by(OptionSpread.symbol).all()
            log.info("dry run: %d open spread(s)", len(rows))
            for it in spread_monitor.snapshot_rows(rows):
                r, sn = it["row"], it["snap"]
                v = sn.get("verdict") or {}
                log.info("  %-6s %g/%gP %s  d=%s  pl=%s  %-8s %s",
                         r.symbol, r.short_strike, r.long_strike, r.expiry,
                         ("%.3f" % sn["short_delta"]) if sn.get("short_delta") is not None else "n/a",
                         ("%+.0f" % sn["pl"]) if sn.get("pl") is not None else "n/a",
                         v.get("state"), sn.get("error") or "")
            return 0

        res = spread_monitor.sweep(db, user_id=args.user_id, on=args.on, fresh=True)
        log.info("checked %d spread(s) for %s: %s",
                 res["spreads"], res["checked_on"],
                 ", ".join("%s=%d" % kv for kv in sorted(res["states"].items())) or "nothing")

        # The actionable ones go to the log individually. In-app is where the
        # member sees them (the Portfolio banner and the nav badge read the rows
        # this just wrote); this is the operator's copy, for when someone asks
        # "did it actually run last Thursday?".
        for a in res["actionable"]:
            log.warning("ACTION user=%s %s %g/%gP %s -> %s: %s",
                        a["user_id"], a["symbol"], a["short_strike"],
                        a["long_strike"], a["expiry"], a["state"], a["action"])
        if not res["actionable"]:
            log.info("nothing at a line")

        # Push copy (v4.62). The in-app badge/banner only reach a member who
        # opens the site; a spread at a line at 06:00 MYT should reach the phone.
        # Discord because its webhook is already wired for MATP refreshes —
        # soft-fail, the sweep's exit code never depends on it.
        if not args.no_discord and res["actionable"]:
            from app.services import discord
            if discord.configured():
                ok = _post_discord(res)
                log.info("discord: %s", "posted" if ok else "not posted")
        return 0
    except Exception:  # noqa: BLE001
        log.exception("sweep failed")
        return 1
    finally:
        db.close()


def _post_discord(res: dict) -> bool:
    """One embed for the whole sweep: a line per actionable spread, grouped by
    state so the defensive ones (ROLL/CLOSE) read before the pleasant one (TAKE)."""
    from app.config import settings
    from app.services import discord

    order = {"CLOSE": 0, "ROLL": 1, "TAKE": 2}
    rows = sorted(res["actionable"], key=lambda a: (order.get(a["state"], 9), a["symbol"]))
    lines = []
    for a in rows:
        bits = []
        if a.get("dte") is not None:
            bits.append("%dd" % a["dte"])
        if a.get("short_delta") is not None:
            bits.append("Δ %.2f" % a["short_delta"])
        if a.get("pl") is not None:
            bits.append("P/L %+.0f" % a["pl"])
        # a winner is described by its credit captured, a loser by its budget used
        if a.get("profit_pct") is not None and a["profit_pct"] > 0:
            bits.append("%.0f%% of credit" % (a["profit_pct"] * 100))
        elif a.get("loss_pct"):
            bits.append("%.0f%% of max loss" % (a["loss_pct"] * 100))
        lines.append("**%s** %s %g/%gP %s ×%s · %s\n%s" % (
            a["state"], a["symbol"], a["short_strike"], a["long_strike"],
            a["expiry"], a.get("contracts") or 1, " · ".join(bits), a["action"]))
    worst = min(order.get(a["state"], 9) for a in rows)
    color = (discord.COLOR_ROSE if worst == 0
             else discord.COLOR_AMBER if worst == 1 else discord.COLOR_EMERALD)
    n = len(rows)
    return discord.post_embed(
        title="Portfolio · %d position%s at a line (%s)" % (n, "" if n == 1 else "s",
                                                             res["checked_on"]),
        description="\n\n".join(lines)[:4000],
        url=(settings.public_url.rstrip("/") + "/portfolio") if settings.public_url else None,
        color=color,
    )


if __name__ == "__main__":
    raise SystemExit(main())
