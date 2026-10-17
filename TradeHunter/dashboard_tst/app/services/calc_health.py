"""Settings -> Data Ingest -> Option Calculator (v4.151, OPTION_CALCULATOR_DESIGN.md): the
Hermes lane that recomputes every member's saved option-calculator setups after each
Massive market pass (``calc_worker``, task ``TST-Option-Calc``), in one view model for
``templates/_option_calc_health.html``.

Read-only: the lane's state file (``calc_worker.read_state``; the web app runs on Hermes
beside the lane, so it is a local read) and a few counts from tst.db (SQLAlchemy ORM only):
active setups, setups whose last computation failed, results written in the last 24 h and
the newest one. Never raises - a missing file or a database error becomes words.
"""
from __future__ import annotations

import datetime as _dt
import logging

from . import calc_worker, clock
from .option_history_view import ago_text

log = logging.getLogger(__name__)

STALE_S = 5 * 60            # the lane writes its file once a minute: 5 min without one is stale
TASK = "TST-Option-Calc"

WORDS = {"starting": "starting", "idle": "up to date", "computing": "computing",
         "error": "problem", "stopped": "stopped", "stale": "not running", "absent": "not running here"}
TONES = {"starting": "sky", "idle": "emerald", "computing": "sky", "error": "rose", "stopped": "amber",
         "stale": "amber", "absent": "amber"}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _parse_ts(v) -> _dt.datetime | None:
    """ISO text (``...Z`` / offset / naive = UTC) or a datetime -> naive UTC."""
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        t = v
    else:
        try:
            t = _dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t


def _stamp_words(v, now: _dt.datetime) -> str | None:
    """``Thu 2026-10-08 14:32 ET (3 min ago)`` for a UTC stamp."""
    t = _parse_ts(v)
    if t is None:
        return None
    et = clock.et_now(t)
    return "%s ET (%s)" % (et.strftime("%a %Y-%m-%d %H:%M"), ago_text((now - t).total_seconds()))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _counts(db, now: _dt.datetime) -> dict:
    """The counts from tst.db, or ``{"db_error": ...}`` in words."""
    from sqlalchemy import func  # noqa: PLC0415

    from ..models import OptionCalcRequest, OptionCalcResult  # noqa: PLC0415

    own = db is None
    try:
        if own:
            from ..db import SessionLocal  # noqa: PLC0415

            db = SessionLocal()
        active = db.query(func.count(OptionCalcRequest.id)).filter(OptionCalcRequest.active.is_(True)).scalar()
        errors = (db.query(func.count(OptionCalcRequest.id))
                  .filter(OptionCalcRequest.active.is_(True), OptionCalcRequest.last_error.isnot(None))
                  .scalar())
        since = now - _dt.timedelta(hours=24)
        results_24h = db.query(func.count(OptionCalcResult.id)).filter(OptionCalcResult.created_at >= since).scalar()
        newest = db.query(func.max(OptionCalcResult.created_at)).scalar()
        return {"requests_active": int(active or 0), "requests_errors": int(errors or 0),
                "results_24h": int(results_24h or 0), "newest_result": _stamp_words(newest, now),
                "db_error": None}
    except Exception as exc:  # noqa: BLE001 - shown as words in the tab
        log.debug("option calc health: no counts (%s)", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"requests_active": None, "requests_errors": None, "results_24h": None, "newest_result": None,
                "db_error": "The saved setups could not be counted (%s)." % type(exc).__name__}
    finally:
        if own and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def view(now: _dt.datetime | None = None, *, state: dict | None = None, db=None) -> dict:
    """Everything the tab shows (see ``templates/_option_calc_health.html``). ``state``
    defaults to the lane's state file, ``db`` to a fresh tst.db session."""
    now = _parse_ts(now) if now is not None else _utcnow()
    try:
        st = state if state is not None else calc_worker.read_state()
    except Exception as exc:  # noqa: BLE001
        log.debug("option calc health: state file not read (%s)", exc)
        st = None
    if st is not None and not isinstance(st, dict):
        st = None
    st = st or {}
    known = bool(st)
    hb = _parse_ts(st.get("heartbeat"))
    age_s = (now - hb).total_seconds() if hb else None
    stale = known and (age_s is None or age_s > STALE_S)
    raw = str(st.get("state") or "").strip().lower()
    if not known:
        key = "absent"
    elif stale:
        key = "stale"
    else:
        key = raw if raw in calc_worker.STATES else "idle"

    if not known:
        detail = ("The option calculator lane has not reported on this server - is the %s task running?" % TASK)
    elif stale:
        detail = ("The option calculator lane last reported %s - is the %s task running?"
                  % (ago_text(age_s) or "long ago", TASK))
    else:
        detail = " ".join(str(st.get("detail") or "").split()) or WORDS.get(key, key)

    counts = _counts(db, now)
    requests_active = counts["requests_active"]
    if requests_active is None:
        requests_active = _int(st.get("requests_active"))
    return {
        "known": known,
        "state": key,
        "word": WORDS.get(key, key),
        "tone": TONES.get(key, "slate"),
        "detail": detail,
        "age": ago_text(age_s),
        "stale": stale,
        "last_pass_id": _int(st.get("last_pass_id")),
        "last_pass_session": st.get("last_pass_session"),
        "last_pass_finished": _stamp_words(st.get("last_pass_finished"), now),
        "computed_at": _stamp_words(st.get("computed_at"), now),
        "next_look": _stamp_words(st.get("next_look"), now) if not stale else None,
        "requests_active": requests_active,
        "requests_due": _int(st.get("requests_due")),
        "requests_errors": counts["requests_errors"],
        "done": _int(st.get("done")),
        "failed": _int(st.get("failed")),
        "last_run_ms": _int(st.get("last_run_ms")),
        "last_error": " ".join(str(st.get("last_error") or "").split()) or None,
        "results_24h": counts["results_24h"],
        "newest_result": counts["newest_result"],
        "db_error": counts["db_error"],
        "version": st.get("version"),
        "pid": _int(st.get("pid")),
        "task": TASK,
        "now_et": clock.et_now(now).strftime("%Y-%m-%d %H:%M ET"),
    }


__all__ = ["view", "STALE_S", "TASK"]
