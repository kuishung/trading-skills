"""Agent status page — liveness + cron self-report from the Nous Hermes agent.

The agent is **outbound-only**: it polls us (/api/refresh-queue, /api/due-filters)
and pushes MATP; the dashboard never reaches into the Linux box. So this page
just shows whatever the agent last POSTed to /api/agent/heartbeat — its version,
the literal crontab it's running, and how long ago it checked in. That answers
"what cron is Nous Hermes running?" without any inbound access. Moderators+.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AgentHeartbeat, MATPRefreshRequest, User
from ..security import require_moderator

router = APIRouter(prefix="/agent", tags=["agent"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# The agent polls ~every 10 min; allow a couple of missed beats before "stale".
STALE_AFTER = _dt.timedelta(minutes=25)

# MATP refresh-request states that mean "the agent hasn't finished this yet".
_OPEN_STATES = ("pending", "running")


def _fmt_ago(ts: _dt.datetime | None, now: _dt.datetime) -> str:
    if ts is None:
        return "never"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    secs = (now - ts).total_seconds()
    if secs < 60:
        return "just now"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins} min ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h {mins % 60}m ago"
    days = hrs // 24
    return f"{days}d {hrs % 24}h ago"


@router.get("", response_class=HTMLResponse)
def agent_status(
    request: Request,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = db.query(AgentHeartbeat).order_by(AgentHeartbeat.agent).all()
    agents = []
    for r in rows:
        recv = r.received_at
        if recv is not None and recv.tzinfo is None:
            recv = recv.replace(tzinfo=_dt.timezone.utc)
        online = recv is not None and (now - recv) <= STALE_AFTER
        # structured jobs (full prompt per cron) when the agent sent them;
        # else fall back to the raw `hermes cron list` text lines.
        jobs = r.cron_jobs if isinstance(r.cron_jobs, list) else []
        cron_lines = [
            ln for ln in (r.crons or "").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        agents.append({
            "agent": r.agent,
            "version": r.version,
            "host": r.host,
            "online": online,
            "seen_ago": _fmt_ago(recv, now),
            "jobs": jobs,
            "cron_lines": cron_lines,
        })
    return templates.TemplateResponse(
        request, "agent.html", {"user": user, "agents": agents}
    )


@router.get("/pill", response_class=HTMLResponse)
def agent_pill(
    request: Request,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Tiny nav-bar pill fragment: Nous Hermes liveness dot + label, HTMX-polled
    on every page. Links to /agent for the full view."""
    now = _dt.datetime.now(_dt.timezone.utc)
    r = (
        db.query(AgentHeartbeat).filter(AgentHeartbeat.agent == "nous_hermes").first()
        or db.query(AgentHeartbeat).order_by(AgentHeartbeat.received_at.desc()).first()
    )
    recv = r.received_at if r else None
    if recv is not None and recv.tzinfo is None:
        recv = recv.replace(tzinfo=_dt.timezone.utc)
    online = recv is not None and (now - recv) <= STALE_AFTER
    if r is None:
        status_text = "no heartbeat yet"
    else:
        status_text = ("online" if online else "stale") + " · " + _fmt_ago(recv, now)
    # Is the agent actively EXECUTING a MATP run right now? (a claimed/running
    # request — not merely queued). The pill blinks only while this is true.
    working = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status == "running")
        .first()
        is not None
    )
    # Poll faster while a run executes so the blink starts/stops promptly; calm
    # when idle. The fragment carries its own next poll (see _agent_pill.html).
    poll_in = 5 if working else 20
    return templates.TemplateResponse(
        request, "_agent_pill.html",
        {
            "has_beat": r is not None, "online": online, "status_text": status_text,
            "working": working, "poll_in": poll_in,
        },
    )


# ---------------------------------------------------------------------------
# Live "working now" panel — what the agent is processing right now, with a
# moving progress bar. The agent reports progress via
# POST /api/refresh-queue/{id}/status (progress_done/progress_total); this just
# renders the latest snapshot and self-polls so the bar animates.
# ---------------------------------------------------------------------------
def _active_run_items(db: Session):
    """Active MATP refresh requests (pending/running), newest first — each with
    a progress %, a display label, and a staleness flag, so the /agent page can
    show a live bar of what the agent is working on."""
    runs = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status.in_(_OPEN_STATES))
        .order_by(MATPRefreshRequest.created_at.desc())
        .all()
    )
    now = _dt.datetime.now(_dt.timezone.utc)

    def _mins(ts):
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return max(0, int((now - ts).total_seconds() // 60))

    items = []
    for r in runs:
        total = r.progress_total or 0
        done = r.progress_done or 0
        pct = max(0, min(100, int(round(done * 100 / total)))) if total else None
        waited = _mins(r.created_at)
        ran = _mins(r.claimed_at)
        # 'pending' too long, or 'running' with no progress for a while = stuck.
        stale = (
            (r.status == "pending" and waited is not None and waited >= 15)
            or (r.status == "running" and ran is not None and ran >= 8 and not r.progress_done)
        )
        label = (
            (r.filter.description if r.filter else "filter #%s" % r.filter_id)
            if r.scope == "filter"
            else (r.symbol or "?")
        )
        items.append(
            {"r": r, "pct": pct, "label": label, "waited": waited, "ran": ran, "stale": stale}
        )
    return items


def _active_context(db: Session) -> dict:
    """Context for the live fragment, with an adaptive self-poll cadence:
      - a run actively *running* → 4s, so the progress bar visibly moves
      - a fresh *pending* run     → 8s, waiting for the agent to claim it
      - nothing running           → 15s idle heartbeat, so a newly-queued run
        still appears without a manual page reload (the user's ask: the panel
        must refresh periodically, never go fully silent)."""
    items = _active_run_items(db)
    running_live = any(it["r"].status == "running" and not it["stale"] for it in items)
    pending_live = any(it["r"].status == "pending" and not it["stale"] for it in items)
    poll_in = 4 if running_live else (8 if pending_live else 15)
    return {"items": items, "poll_in": poll_in}


@router.get("/active", response_class=HTMLResponse)
def agent_active(
    request: Request,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """HTMX fragment: the agent's live 'working now' panel — active MATP runs
    with a moving progress bar. Self-polls (see _active_context) so the bar
    animates as the agent reports progress and a newly-queued run shows up
    without reloading the page."""
    ctx = _active_context(db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "_agent_runs.html", ctx)
