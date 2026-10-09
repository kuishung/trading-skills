"""What is left of the v1 Options store after Options v2 (``OPTIONS_V2_DESIGN.md`` §1)
removed the v1 page, its engines, its nightly job and the Telegram ideas push. The v2
data layer is ``opt_store.py``; this module keeps only:

* ``prune`` - retention of the ``option_chain_snapshot`` daily EOD record (the v2
  collector's EOD pass writes it; ``opt_store.prune_v2`` calls this) and of the old v1
  tables, which stay in the DB unused and drain here: ``option_signal``,
  ``option_trade_checks``, ``option_jobs``, ``option_idea_push``. ``iv_daily`` is never
  pruned.
* ``basket_universe`` - the distinct ACTIVE basket symbols over all owners (plus any
  symbol with an open ``option_trades`` row).

Portable ORM only (the platform data-handling rule): plain
``delete(synchronize_session=False)`` prunes and ``func.abs`` for the thinning rule;
nothing SQLite-specific. ``prune`` is a whole unit of work and commits itself.
"""
from __future__ import annotations

import datetime as _dt
import logging

from sqlalchemy import func, or_

from ..config import settings
from ..models import (OptionBasket, OptionChainSnapshot, OptionIdeaPush, OptionJob,
                      OptionSignal, OptionTrade, OptionTradeCheck)
from . import clock

log = logging.getLogger(__name__)

# Retention (II.2.1 / A2.4). The snapshot and full-width windows come from settings.
THIN_DELTA_LO = 0.03         # rows with |delta| below this are thinned after options_full_days
THIN_DELTA_HI = 0.97         # ... and above this
EXPIRED_GRACE_DAYS = 7       # an expired contract keeps its rows for a week
CHECKS_KEEP_DAYS = 90        # option_trade_checks
JOBS_KEEP = 180              # option_jobs: the newest N rows
PUSH_KEEP_DAYS = 45          # option_idea_push


# ────────────────────────────────── small helpers ──────────────────────────────────

def _day(d) -> str:
    """An ET date as ``YYYY-MM-DD`` whatever the caller passed."""
    if d is None:
        return clock.et_today()
    if isinstance(d, _dt.datetime):
        return d.date().isoformat()
    if isinstance(d, _dt.date):
        return d.isoformat()
    return str(d)[:10]


def _minus(day: str, days: int) -> str:
    return (_dt.date.fromisoformat(day) - _dt.timedelta(days=days)).isoformat()


# ────────────────────────────────── basket + retention ──────────────────────────────────

def basket_universe(db) -> list[str]:
    """Distinct ACTIVE basket symbols over all owners, plus every symbol with an OPEN
    ``option_trades`` row (a tracked position's chain is always fresh). Sorted."""
    syms = {s for (s,) in db.query(OptionBasket.symbol)
                             .filter(OptionBasket.active.is_(True)).distinct()}
    syms |= {s for (s,) in db.query(OptionTrade.symbol)
                             .filter(OptionTrade.status == "open").distinct()}
    return sorted(s for s in syms if s)


def prune(db, today=None) -> dict:
    """Snapshot + v1-table retention (II.2.1 / A2.4), run by ``opt_store.prune_v2`` at the
    collector's EOD pass (it was step 5 of the removed v1 nightly job). Deletes, in order:
    snapshots older than ``options_snapshot_days``; rows older than ``options_full_days``
    with ``delta`` None / |delta| < 0.03 / > 0.97 (expiries are never thinned); intraday
    rows of past days; contracts expired more than 7 days; ``option_signal`` rows older
    than the snapshot window; ``option_trade_checks`` older than 90 days (the trade row
    itself is never pruned); all but the newest 180 ``option_jobs``; ``option_idea_push``
    older than 45 days. ``iv_daily`` is never pruned. Commits; returns the counts."""
    today = _day(today)
    snap_days = int(settings.options_snapshot_days or 90)
    full_days = int(settings.options_full_days or 7)
    out: dict[str, int] = {}

    # "90 days" means the newest 90 snapshot days INCLUDING today, so the cut sits
    # at today - 89: 100 synthetic days prune to exactly 90 (A8).
    oldest_kept = _minus(today, max(snap_days - 1, 0))
    S = OptionChainSnapshot
    out["snapshots_old"] = (db.query(S).filter(S.snap_on < oldest_kept)
                              .delete(synchronize_session=False))
    out["snapshots_thinned"] = (
        db.query(S)
          .filter(S.snap_on < _minus(today, full_days),
                  or_(S.delta.is_(None),
                      func.abs(S.delta) < THIN_DELTA_LO,
                      func.abs(S.delta) > THIN_DELTA_HI))
          .delete(synchronize_session=False))
    out["snapshots_intraday"] = (db.query(S).filter(S.kind == "intraday", S.snap_on < today)
                                   .delete(synchronize_session=False))
    out["snapshots_expired"] = (db.query(S).filter(S.expiry < _minus(today, EXPIRED_GRACE_DAYS))
                                  .delete(synchronize_session=False))
    out["signals"] = (db.query(OptionSignal)
                        .filter(OptionSignal.snap_on < oldest_kept)
                        .delete(synchronize_session=False))
    out["trade_checks"] = (db.query(OptionTradeCheck)
                             .filter(OptionTradeCheck.checked_on < _minus(today, CHECKS_KEEP_DAYS))
                             .delete(synchronize_session=False))
    old_jobs = [i for (i,) in db.query(OptionJob.id).order_by(OptionJob.id.desc())
                                .offset(JOBS_KEEP).all()]
    out["jobs"] = 0
    for i in range(0, len(old_jobs), 500):
        out["jobs"] += (db.query(OptionJob).filter(OptionJob.id.in_(old_jobs[i:i + 500]))
                          .delete(synchronize_session=False))
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=PUSH_KEEP_DAYS)).replace(tzinfo=None)
    out["idea_push"] = (db.query(OptionIdeaPush).filter(OptionIdeaPush.sent_at < cutoff)
                          .delete(synchronize_session=False))
    db.commit()
    return out
