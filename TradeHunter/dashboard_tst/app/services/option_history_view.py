"""Settings -> Data Ingest -> Option History (v4.139, OPTIONS_SCREENER_DESIGN.md §14): how
Massive's options day files are kept up to date on Hermes - the DOWNLOAD (the screener
collector's flat-file lane), the IV readings filed from them, and the CONVERSION to Parquet
(the collector's parquet lane, ``opt_history``) - in one view model for
``templates/_option_history.html``.

Read-only: the flat-file and Parquet manifests on this PC (``opt_history.Store.summary``)
and the collector's heartbeat row (``scr_status``: state, detail, ``progress.flat_*`` /
``pq_*``). The web app runs on Hermes beside the collector, so both are local reads.
"""
from __future__ import annotations

import datetime as _dt
import logging

from . import clock, opt_history

log = logging.getLogger(__name__)

STALE_S = 5 * 60                     # the collector beats every 15 s: 5 min without one is stale
FLAT_READY_AT = _dt.time(11, 15)     # a day's file is asked for from 11:15 ET the next day (scr_collector)
SHOW_MISSING = 30                    # missing days listed by name


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def size_text(n) -> str:
    """Bytes in words: 812 KB / 3.9 MB / 1.85 GB."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n >= 1e9:
        return "%.2f GB" % (n / 1e9)
    if n >= 1e6:
        return "%.1f MB" % (n / 1e6)
    if n >= 1e3:
        return "%.0f KB" % (n / 1e3)
    return "%d B" % n


def ago_text(seconds) -> str | None:
    if seconds is None:
        return None
    s = max(0.0, float(seconds))
    if s < 90:
        return "%d s ago" % s
    if s < 5400:
        return "%d min ago" % round(s / 60)
    if s < 48 * 3600:
        return "%.1f h ago" % (s / 3600)
    return "%d days ago" % round(s / 86400)


def dur_text(seconds) -> str | None:
    if seconds is None:
        return None
    s = max(0.0, float(seconds))
    if s < 90:
        return "%d s" % s
    if s < 5400:
        return "%d min" % round(s / 60)
    return "%.1f h" % (s / 3600)


def _parse_ts(v) -> _dt.datetime | None:
    """ISO text (``...Z`` / offset / naive = UTC) or a datetime -> naive UTC."""
    if isinstance(v, _dt.datetime):
        t = v
    else:
        try:
            t = _dt.datetime.fromisoformat(str(v or "").strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t


def _stamp_words(v, now: _dt.datetime) -> str | None:
    """``2026-10-12 23:40 MYT (2.1 h ago)`` for a UTC stamp."""
    t = _parse_ts(v)
    if t is None:
        return None
    myt = clock.myt_now(t)
    return "%s MYT (%s)" % (myt.strftime("%Y-%m-%d %H:%M"), ago_text((now - t).total_seconds()))


def _collector() -> dict | None:
    """The screener collector's heartbeat row as a dict (None when the DB is not there)."""
    try:
        from .. import screener_db  # noqa: PLC0415
        from . import scr_store  # noqa: PLC0415

        with screener_db.session() as s:
            return scr_store.status(s)
    except Exception as exc:  # noqa: BLE001 - no screener DB on this PC, a locked file
        log.debug("option history view: no collector status (%s)", exc)
        return None


def _window(now: _dt.datetime) -> list[str]:
    """The sessions Massive serves day files for (the collector's own window)."""
    try:
        from .scr_collector import flat_days  # noqa: PLC0415

        return flat_days(now)
    except Exception as exc:  # noqa: BLE001
        log.debug("option history view: no window (%s)", exc)
        return []


def _next_file(newest_local: str | None, now: _dt.datetime) -> dict:
    """The next day file the collector will download: its session and when it is due
    (11:15 ET the calendar day after the session), in ET and MYT. ``late`` = its due time
    has passed (it is asked for hourly until Massive publishes it)."""
    try:
        from .scr_collector import flat_newest_day  # noqa: PLC0415

        expected = flat_newest_day(now)
    except Exception:  # noqa: BLE001
        return {}
    have = _dt.date.fromisoformat(newest_local) if newest_local else None
    session = expected if (have is None or have < expected) else clock.next_trading_day(expected)
    due_day = session + _dt.timedelta(days=1)
    et = clock.et_now(now)
    due_et = _dt.datetime.combine(due_day, FLAT_READY_AT, tzinfo=et.tzinfo)
    due_utc = due_et.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return {"session": session.isoformat(),
            "due_et": due_et.strftime("%a %Y-%m-%d %H:%M ET"),
            "due_myt": clock.myt_now(due_utc).strftime("%a %H:%M MYT"),
            "late": now >= due_utc}


def _pct(a, b) -> int:
    try:
        return max(0, min(100, int(round(100.0 * float(a) / float(b))))) if b else 0
    except (TypeError, ValueError):
        return 0


def view(now: _dt.datetime | None = None, *, store: opt_history.Store | None = None,
         collector: dict | None = None, window: list[str] | None = None) -> dict:
    """Everything the tab shows (see ``templates/_option_history.html``)."""
    now = now or _utcnow()
    win = window if window is not None else _window(now)
    st = store or opt_history.Store()
    s = st.summary(window=win)
    col = collector if collector is not None else _collector()
    prog = (col or {}).get("progress") or {}
    hb = _parse_ts((col or {}).get("heartbeat"))
    age = (now - hb).total_seconds() if hb else None
    flat, pq = s["flat"], s["pq"]
    total = s["window"]["sessions"] or flat["days"]
    pq_state = prog.get("pq_state")
    if age is None or age > STALE_S:
        pq_state = pq_state or "unknown"
    failing = set(prog.get("pq_failed") or ())           # the collector's own record (this process has none)
    for r in s["recent"]:
        if r["state"] == "pending" and r["day"] in failing:
            r["state"] = "failed"
    return {
        "s": s,
        "available": s["available"],
        "collector": {
            "known": col is not None, "state": (col or {}).get("state"), "detail": (col or {}).get("detail"),
            "age": ago_text(age), "stale": age is None or age > STALE_S,
        },
        "download": {
            "done": flat["days"], "total": total, "pct": _pct(flat["days"], total),
            "size": size_text(flat["bytes"]), "oldest": flat["oldest"], "newest": flat["newest"],
            "last": _stamp_words(flat["last_download"], now),
            "state": prog.get("flat_state"), "error": prog.get("flat_error"), "kind": prog.get("flat_kind"),
            "missing": s["missing"][-SHOW_MISSING:][::-1], "n_missing": len(s["missing"]),
            "next": _next_file(flat["newest"], now),
        },
        "iv": {
            "done": prog.get("flat_days_done"), "total": prog.get("flat_days_total"),
            "pct": _pct(prog.get("flat_days_done"), prog.get("flat_days_total")),
            "eta": dur_text(prog.get("flat_eta_s")),
        },
        "parquet": {
            "done": pq["days"], "total": flat["days"], "pct": _pct(pq["days"], flat["days"]),
            "rows": pq["rows"], "size": size_text(pq["bytes"]), "oldest": pq["oldest"], "newest": pq["newest"],
            "last": _stamp_words(pq["last_converted"], now), "behind": len(s["behind"]),
            "state": pq_state, "error": prog.get("pq_error"), "eta": dur_text(prog.get("pq_eta_s")),
            "running_done": prog.get("pq_days_done"), "running_total": prog.get("pq_days_total"),
            "failed": [{"day": d} for d in (prog.get("pq_failed") or [])] or s["failed"],
        },
        "months": [dict(m, gz=size_text(m["gz_bytes"]), pqs=size_text(m["pq_bytes"])) for m in s["months"]],
        "recent": [dict(r, gz=size_text(r["gz_bytes"]) if r["gz_bytes"] is not None else None,
                        pqs=size_text(r["pq_bytes"]) if r["pq_bytes"] is not None else None,
                        dl=_stamp_words(r["downloaded_at"], now), cv=_stamp_words(r["converted_at"], now))
                   for r in s["recent"]],
        "root": s["root"], "flat_root": s["flat_root"],
        "now_myt": clock.myt_now(now).strftime("%Y-%m-%d %H:%M MYT"),
    }


__all__ = ["view", "size_text", "ago_text", "dur_text"]
