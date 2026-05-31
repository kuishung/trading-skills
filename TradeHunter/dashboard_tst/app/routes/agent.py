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
from ..models import AgentHeartbeat, User
from ..security import require_moderator

router = APIRouter(prefix="/agent", tags=["agent"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# The agent polls ~every 10 min; allow a couple of missed beats before "stale".
STALE_AFTER = _dt.timedelta(minutes=25)


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
            "cron_lines": cron_lines,
        })
    return templates.TemplateResponse(
        request, "agent.html", {"user": user, "agents": agents}
    )
