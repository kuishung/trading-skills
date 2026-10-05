"""The Options module's job ledger: ``start / finish / latest / missed`` over
``option_jobs`` (``models.OptionJob``), the ONE job table of the module.

Why a module for four functions
--------------------------------
The status strip, the nav badge and the nightly job itself must agree on what
"did it run?" and "was it missed?" mean. One definition here, read by all three.
``SpreadScan`` plays the same role for the Spread page; this ledger is the
Options page's, and it also records refreshes, the Live IV bootstrap, the
``iv_history`` backfill and the Telegram poll so an operator can see every write
the module made and when.

``start`` commits at once, so a crash leaves a row with ``started_at`` set and
``finished_at`` NULL - "started, never finished" - which the strip reads as a
crash after two hours instead of pretending the job simply has not run.
"""
from __future__ import annotations

import datetime as _dt

from ..models import OptionJob, _utcnow
from . import clock

JOBS = ("nightly", "refresh", "bootstrap", "backfill", "telegram_poll")

# A nightly run is "running" while unfinished and younger than this (the task's
# own execution limit is 30 minutes; a little slack for a slow Cboe night).
RUNNING_MINUTES = 40

# The nightly task fires at 07:15 MYT; by this hour it has had its chance.
MISSED_DEADLINE_HOUR_MYT = 8


def start(db, job: str, run_on: str, source: str | None = None) -> OptionJob:
    """Open a run row and COMMIT it immediately. ``run_on`` is the ET date the run
    describes (``spread_monitor.et_today()`` or the ``--on`` override)."""
    if job not in JOBS:
        raise ValueError("unknown job %r (one of %s)" % (job, ", ".join(JOBS)))
    row = OptionJob(job=job, run_on=run_on, source=source, started_at=_utcnow(), detail={})
    db.add(row)
    db.commit()
    return row


def finish(db, run: OptionJob, *, ok: int = 0, errors: int = 0, rows: int = 0,
           pushed: int = 0, note: str | None = None, detail: dict | None = None,
           symbols: int | None = None) -> OptionJob:
    """Close a run row: counts, the Telegram count, the per-symbol detail, a note.
    Commits. ``symbols`` defaults to ``ok + errors`` when not given."""
    run.finished_at = _utcnow()
    run.ok = int(ok or 0)
    run.errors = int(errors or 0)
    run.rows = int(rows or 0)
    run.pushed = int(pushed or 0)
    run.symbols = int(symbols) if symbols is not None else run.ok + run.errors
    if note is not None:
        run.note = note
    if detail is not None:
        run.detail = dict(detail)        # reassign: SQLAlchemy won't see an in-place mutation
    db.commit()
    return run


def latest(db, job: str = "nightly") -> OptionJob | None:
    """The newest FINISHED run of ``job`` (None until one has finished)."""
    return (db.query(OptionJob)
              .filter(OptionJob.job == job, OptionJob.finished_at.isnot(None))
              .order_by(OptionJob.finished_at.desc(), OptionJob.id.desc())
              .first())


def latest_any(db, job: str = "nightly") -> OptionJob | None:
    """The newest run of ``job`` whether or not it finished (for "running" and
    "crashed" states)."""
    return (db.query(OptionJob)
              .filter(OptionJob.job == job)
              .order_by(OptionJob.started_at.desc(), OptionJob.id.desc())
              .first())


def _age_minutes(ts: _dt.datetime | None) -> float | None:
    """Minutes since ``ts``. The column is naive UTC on SQLite and aware on
    Postgres while ``_utcnow()`` is aware, so both sides are made naive UTC."""
    if ts is None:
        return None
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return (now - ts).total_seconds() / 60.0


def running(db, job: str = "nightly", *, within_minutes: int = RUNNING_MINUTES) -> bool:
    """An unfinished run started less than ``within_minutes`` ago."""
    row = latest_any(db, job)
    if row is None or row.finished_at is not None:
        return False
    age = _age_minutes(row.started_at)
    return age is not None and age < within_minutes


def due_day(now: _dt.datetime | None = None) -> _dt.date:
    """The last ET trading day whose nightly run is DUE: a session that closed at
    16:00 ET (04:00 / 05:00 MYT the next calendar day) must have its run finished
    by 08:00 MYT that morning. So the newest session due is the last trading day
    on or before the MYT calendar day *before* ``(now_myt - 8 h)``: at 07:00 MYT
    Tuesday that is Friday (Monday's run is not yet due), at 09:00 MYT Tuesday it
    is Monday, and on Monday evening it is still Friday rather than Monday."""
    myt = clock.myt_now(now)
    cutoff = (myt - _dt.timedelta(hours=MISSED_DEADLINE_HOUR_MYT)).date() - _dt.timedelta(days=1)
    return clock.last_trading_day(cutoff)


def missed(db, job: str = "nightly", *, now: _dt.datetime | None = None) -> bool:
    """True when no FINISHED run of ``job`` covers the last ET trading day whose
    deadline (08:00 MYT the morning after the close) has passed - the one
    definition the status strip, the badge and the job share. Weekends and NYSE
    holidays are stepped over, so a Monday evening is not flagged for a Monday
    run that cannot have happened yet."""
    row = latest(db, job)
    due = due_day(now).isoformat()
    return row is None or (row.run_on or "") < due
