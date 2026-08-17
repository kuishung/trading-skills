"""Read-only diagnosis of "why is the MATP recalculation never finishing?".

The progress panel can park below 100% for several very different reasons, and
the bar itself cannot tell them apart:

  * the ticker was never attempted (the agent ran out of per-poll budget),
  * it was attempted and skipped (no post-earnings analyst coverage),
  * it HAS a value that is simply older than the 24h freshness window,
  * or the whole cycle never closes because no filter's schedule ever advanced.

This prints the raw state behind each of those so the cause is decided by data
rather than inference. Read-only: it opens a session, SELECTs, and closes.

Run on **Hermes** (where the production tst.db lives):

    cd C:\\trading-skills\\TradeHunter\\dashboard_tst
    .\\.venv\\Scripts\\python.exe deploy\\matp_diag.py
    .\\.venv\\Scripts\\python.exe deploy\\matp_diag.py ADMA FTI HL SSRM YOU

With symbols, it deep-dives those; with none, it deep-dives whatever the
progress panel currently calls outstanding.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

# run from anywhere: put dashboard_tst/ on sys.path so `app` imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import (AgentHeartbeat, FinvizFilter, MATPHistory,  # noqa: E402
                        MATPLevel, MATPRefreshRequest, get_selective_schedule)
from app.services.matp_queue import build_queue, progress  # noqa: E402

NOW = _dt.datetime.now(_dt.timezone.utc)


def _aware(ts):
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=_dt.timezone.utc)
    return ts


def _ago(ts) -> str:
    ts = _aware(ts)
    if ts is None:
        return "never"
    hrs = (NOW - ts).total_seconds() / 3600
    if hrs < 1:
        return f"{int(hrs * 60)}m ago"
    if hrs < 48:
        return f"{hrs:.1f}h ago"
    return f"{hrs / 24:.1f}d ago"


def _list(syms, cap: int = 40) -> str:
    """Symbols, truncated — a 200-name universe would drown the report."""
    syms = list(syms)
    if not syms:
        return "-"
    head = ", ".join(syms[:cap])
    return head if len(syms) <= cap else f"{head}  (+{len(syms) - cap} more)"


def main(argv: list[str]) -> int:
    db = SessionLocal()
    try:
        p = progress(db)
        q = build_queue(db, due_only=False)

        print("=" * 78)
        print("PROGRESS PANEL")
        print("=" * 78)
        print(f"  done/total   : {p['done']}/{p['total']}  ({p['pct']}%)")
        print(f"  active       : {p['active']}   (drives the green 'in progress' banner)")
        print(f"  filters_due  : {p['filters_due']}")
        print(f"  pending reqs : {p['pending']}")
        print(f"  outstanding  : {_list(p['remaining'])}")
        print(f"  never calc'd : {_list(p['never'])}")

        print()
        print("=" * 78)
        print("FILTER SCHEDULES  (a filter stuck 'due' keeps the banner on forever)")
        print("=" * 78)
        for f in db.query(FinvizFilter).order_by(FinvizFilter.id).all():
            nxt = _aware(f.next_run_at)
            due = f.is_active and f.run_interval != "off" and (nxt is None or nxt <= NOW)
            print(f"  #{f.id} {f.description[:42]:<42} active={f.is_active} "
                  f"interval={f.run_interval:<9} DUE={due}")
            print(f"      last_run={_ago(f.last_run_at):<12} next_run="
                  f"{nxt.isoformat() if nxt else 'None (due now)'}")
        sched = get_selective_schedule(db)
        snxt = _aware(sched.next_run_at)
        print(f"  selective set: interval={sched.run_interval} "
              f"last_run={_ago(sched.last_run_at)} "
              f"next_run={snxt.isoformat() if snxt else 'None (due now)'}")

        print()
        print("=" * 78)
        print("AGENT HEARTBEAT")
        print("=" * 78)
        for hb in db.query(AgentHeartbeat).all():
            print(f"  {hb.agent}: version={hb.version} host={hb.host} "
                  f"seen={_ago(hb.received_at)}")
            print(f"      health={hb.health}")

        print()
        print("=" * 78)
        print("OPEN REFRESH REQUESTS")
        print("=" * 78)
        open_reqs = (
            db.query(MATPRefreshRequest)
            .filter(MATPRefreshRequest.status.in_(("pending", "running")))
            .order_by(MATPRefreshRequest.created_at.desc())
            .all()
        )
        if not open_reqs:
            print("  (none)")
        for r in open_reqs:
            print(f"  #{r.id} {r.scope} {r.symbol or ('filter %s' % r.filter_id)} "
                  f"status={r.status} created={_ago(r.created_at)} "
                  f"claimed={_ago(r.claimed_at)} {r.progress_done}/{r.progress_total}"
                  f" note={r.note!r}")

        symbols = [s.strip().upper() for s in argv if s.strip()] or list(p["remaining"])
        rows = {t["symbol"]: t for t in q["tickers"]}

        print()
        print("=" * 78)
        print(f"TICKER DEEP-DIVE  ({len(symbols)} symbol(s))")
        print("=" * 78)
        for sym in symbols:
            lv = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
            t = rows.get(sym)
            print(f"\n  {sym}")
            if t is None:
                print("    NOT IN THE QUEUE AT ALL - no active filter contains it, it is")
                print("    not a selective name, and nobody has starred it. Nothing would")
                print("    ever compute it.")
            else:
                srcs = ", ".join(f"#{s['id']} {s['description'][:28]}" for s in t["filters"])
                print(f"    in queue via : {srcs or '(no filter)'}"
                      f"{'  [selective]' if t['manual'] else ''}"
                      f"{'  [starred, uncovered]' if t.get('watchlist') else ''}")
                print(f"    queued req   : {t['queued']}")
            if lv is None:
                print("    matp_levels  : NO ROW - never successfully pushed by the agent")
            else:
                print(f"    matp_levels  : matp={lv.matp} mbp={lv.mbp} "
                      f"n_targets={lv.n_targets} status={lv.status}")
                print(f"                   exchange={lv.exchange!r} "
                      f"last_earnings={lv.last_earnings_date!r} filter_id={lv.filter_id}")
                print(f"                   as_of={_ago(lv.as_of)} "
                      f"last_seen={_ago(lv.last_seen_at)}")
            hist = (
                db.query(MATPHistory)
                .filter(MATPHistory.symbol == sym)
                .order_by(MATPHistory.as_of.desc())
                .limit(3)
                .all()
            )
            if hist:
                print("    history      : " + "; ".join(
                    f"{h.matp} @ {_ago(h.as_of)} (src={h.source})" for h in hist))
            else:
                print("    history      : (none)")
            reqs = (
                db.query(MATPRefreshRequest)
                .filter(MATPRefreshRequest.symbol == sym)
                .order_by(MATPRefreshRequest.created_at.desc())
                .limit(3)
                .all()
            )
            if reqs:
                print("    requests     : " + "; ".join(
                    f"#{r.id} {r.status} {_ago(r.created_at)} note={r.note!r}"
                    for r in reqs))
            else:
                print("    requests     : (never individually requested)")
        print()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
