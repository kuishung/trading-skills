"""Research page — members co-design a research topic with the LLM, record the
agreed plan as Markdown, and queue/schedule it for the Nous agent to run.

Architecture (see RESEARCH_DESIGN.md):
  - Planning chat -> DeepSeek DIRECTLY from here (the agent is outbound-only).
  - Runs -> queued here; the agent polls /api/research/due, executes with its
    corpus, writes md to AI-Hermes, and POSTs results back. (agent side = Phase 2)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    RESEARCH_COMPANY,
    RESEARCH_MACRO,
    RT_ARCHIVED,
    RT_DRAFT,
    RT_PLANNED,
    RT_SCHEDULED,
    RUN_QUEUED,
    ResearchMessage,
    ResearchRun,
    ResearchTopic,
    User,
)
from ..security import require_user
from ..services import research_llm

router = APIRouter(prefix="/research", tags=["research"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_KINDS = (RESEARCH_COMPANY, RESEARCH_MACRO)
_SOURCE_OPTIONS = ("edgar", "matp", "bars", "web")


def _topic_context(t: ResearchTopic) -> dict:
    """Minimal topic info the agent runner needs to ground the chat."""
    return {"kind": t.kind, "subject": t.subject, "title": t.title}


def _get_topic(db: Session, topic_id: int, user: User) -> ResearchTopic:
    """Fetch a topic the user may see/edit (owner, or any moderator/admin)."""
    t = db.get(ResearchTopic, topic_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    if t.owner_id != user.id and not user.can_moderate:
        raise HTTPException(status_code=403, detail="Not your topic")
    return t


@router.get("", response_class=HTMLResponse)
def research_home(
    request: Request,
    kind: str = Query(""),    # Investing > Macro / Company filter the list by kind
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    q = db.query(ResearchTopic)
    if not user.can_moderate:
        q = q.filter(ResearchTopic.owner_id == user.id)
    if kind in _KINDS:
        q = q.filter(ResearchTopic.kind == kind)
    topics = q.order_by(ResearchTopic.updated_at.desc()).all()
    return templates.TemplateResponse(
        request, "research.html",
        {"user": user, "topics": topics, "kinds": _KINDS,
         "active_kind": kind if kind in _KINDS else "",
         "chat_ready": research_llm.is_configured(),
         "chat_mode": research_llm.chat_mode()},
    )


@router.post("")
def create_topic(
    request: Request,
    title: str = Form(...),
    kind: str = Form(RESEARCH_COMPANY),
    subject: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    title = title.strip()
    if not title:
        return RedirectResponse(url="/research", status_code=303)
    if kind not in _KINDS:
        kind = RESEARCH_COMPANY
    t = ResearchTopic(
        owner_id=user.id, title=title[:200], kind=kind,
        subject=(subject.strip()[:120] or None), status=RT_DRAFT,
        sources=list(_SOURCE_OPTIONS),
    )
    db.add(t)
    db.commit()
    return RedirectResponse(url=f"/research/{t.id}", status_code=303)


@router.get("/{topic_id}", response_class=HTMLResponse)
def topic_detail(
    topic_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    t = _get_topic(db, topic_id, user)
    return templates.TemplateResponse(
        request, "research_detail.html",
        {"user": user, "t": t, "messages": t.messages, "runs": t.runs,
         "source_options": _SOURCE_OPTIONS,
         "chat_ready": research_llm.is_configured(),
         "chat_mode": research_llm.chat_mode()},
    )


@router.post("/{topic_id}/chat")
def post_chat(
    topic_id: int,
    request: Request,
    message: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    t = _get_topic(db, topic_id, user)
    msg = message.strip()
    if not msg:
        return RedirectResponse(url=f"/research/{t.id}", status_code=303)

    next_seq = (max((m.seq for m in t.messages), default=-1)) + 1
    db.add(ResearchMessage(topic_id=t.id, seq=next_seq, role="user", content=msg))
    db.commit()

    history = [{"role": m.role, "content": m.content}
               for m in db.query(ResearchMessage)
               .filter(ResearchMessage.topic_id == t.id)
               .order_by(ResearchMessage.seq).all()]
    reply = research_llm.chat(history, topic=_topic_context(t))

    db.add(ResearchMessage(topic_id=t.id, seq=next_seq + 1, role="assistant",
                           content=reply["content"]))
    db.commit()
    return RedirectResponse(url=f"/research/{t.id}#bottom", status_code=303)


@router.post("/{topic_id}/chat.json")
def post_chat_json(
    topic_id: int,
    message: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Async chat for the JS chatbot: persist the user turn, get the assistant
    reply (agent runner or DeepSeek-direct), persist it, return JSON. Same
    persistence as the form POST above, but no redirect — the page appends the
    reply in place (no reload). The form POST remains as a no-JS fallback."""
    t = _get_topic(db, topic_id, user)
    msg = message.strip()
    if not msg:
        return JSONResponse({"ok": False, "content": "", "error": "empty message"},
                            status_code=400)

    next_seq = (max((m.seq for m in t.messages), default=-1)) + 1
    db.add(ResearchMessage(topic_id=t.id, seq=next_seq, role="user", content=msg))
    db.commit()

    history = [{"role": m.role, "content": m.content}
               for m in db.query(ResearchMessage)
               .filter(ResearchMessage.topic_id == t.id)
               .order_by(ResearchMessage.seq).all()]
    reply = research_llm.chat(history, topic=_topic_context(t))

    db.add(ResearchMessage(topic_id=t.id, seq=next_seq + 1, role="assistant",
                           content=reply["content"]))
    db.commit()
    return JSONResponse({"ok": bool(reply.get("ok", True)),
                         "content": reply.get("content", ""),
                         "via": reply.get("via")})


@router.post("/{topic_id}/plan/draft")
def draft_plan(
    topic_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ask the LLM to distil the conversation into a runnable plan and store it
    (the user can then edit + Save)."""
    t = _get_topic(db, topic_id, user)
    history = [{"role": m.role, "content": m.content} for m in t.messages]
    history.append({"role": "user", "content": (
        "Summarise everything we agreed as the research PLAN for this topic, in "
        "Markdown: a one-line objective, numbered research steps, the data sources "
        "to use, and what the final output should answer. Plan only — no preamble.")})
    reply = research_llm.chat(history, topic=_topic_context(t))
    if reply["ok"]:
        t.plan_md = reply["content"]
        if t.status == RT_DRAFT:
            t.status = RT_PLANNED
        db.commit()
    return RedirectResponse(url=f"/research/{t.id}#plan", status_code=303)


@router.post("/{topic_id}/plan")
def save_plan(
    topic_id: int,
    plan_md: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    t = _get_topic(db, topic_id, user)
    t.plan_md = plan_md.strip() or None
    if t.plan_md and t.status == RT_DRAFT:
        t.status = RT_PLANNED
    db.commit()
    return RedirectResponse(url=f"/research/{t.id}#plan", status_code=303)


@router.post("/{topic_id}/run")
def run_now(
    topic_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Queue a manual run. The Nous agent picks it up via /api/research/due."""
    t = _get_topic(db, topic_id, user)
    if not t.plan_md:
        return RedirectResponse(url=f"/research/{t.id}#plan", status_code=303)
    db.add(ResearchRun(topic_id=t.id, status=RUN_QUEUED, trigger="manual"))
    db.commit()
    return RedirectResponse(url=f"/research/{t.id}#runs", status_code=303)


@router.post("/{topic_id}/schedule")
def set_schedule(
    topic_id: int,
    schedule_cron: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set/clear the per-topic cron. The agent registers the hermes cron from
    this field on its next poll (Phase 2)."""
    t = _get_topic(db, topic_id, user)
    cron = schedule_cron.strip()
    t.schedule_cron = cron or None
    if cron:
        t.status = RT_SCHEDULED
    elif t.status == RT_SCHEDULED:
        t.status = RT_PLANNED if t.plan_md else RT_DRAFT
    db.commit()
    return RedirectResponse(url=f"/research/{t.id}#schedule", status_code=303)


@router.post("/{topic_id}/archive")
def archive_topic(
    topic_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    t = _get_topic(db, topic_id, user)
    t.status = RT_ARCHIVED
    t.enabled = False
    t.schedule_cron = None
    db.commit()
    return RedirectResponse(url="/research", status_code=303)
