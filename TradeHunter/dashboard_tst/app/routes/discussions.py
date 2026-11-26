"""Discussions (v4.275, DISCUSSIONS_DESIGN.md): the page that lists every discussion, opens one by hand
(moderators), and shows a discussion's contributions from Discord, Telegram and the app merged in time
order, with a composer that mirrors into the chats. The structure the user asked for first; which
subjects open a discussion automatically is left to decide (design §6).

  GET  /discussions?kind=&status=&id=   the page (list left, the picked discussion right)
  GET  /discussions/{id}/chat           the chat strip fragment (self-polls)
  POST /discussions/new                 moderators: kind, ref, title, body, which chats
  POST /discussions/{id}/post           a member's post (stored, mirrored)
  POST /discussions/{id}/close|reopen   moderators
  POST /discussions/{id}/sync           moderators: re-read the Discord thread now
  GET  /discussions/health              the Data Ingest card (the sync's state)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DISCUSSION_KINDS, Discussion, User
from ..security import require_moderator, require_user
from ..services import discord, discussions, telegram

router = APIRouter(prefix="/discussions", tags=["discussions"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

STATUSES = ("open", "closed", "all")


def _chat_ctx(db: Session, user: User, d: Discussion | None, *, fresh: bool = True) -> dict:
    """The chat strip's context. ``fresh`` re-reads the Discord thread when it was last read more
    than 10 s ago, so the strip stays live even if the sync thread is not running on this PC."""
    msgs: list[dict] = []
    if d is not None:
        if fresh and d.discord_thread_id and discord.bot_configured():
            import datetime as _dt

            age = (_dt.datetime.utcnow() - d.last_synced_at).total_seconds() if d.last_synced_at else 1e9
            if age > 10:
                try:
                    discussions.sync_discord(db, d)
                except Exception:  # noqa: BLE001
                    pass
        msgs = discussions.messages(db, d, me=user)
    return {"user": user, "d": d, "messages": msgs,
            "links": discussions.links(d) if d else {}, "kind_labels": discussions.KIND_LABELS,
            "subject_url": discussions.subject_url(d.kind, d.ref) if d else None,
            "can_moderate": bool(getattr(user, "can_moderate", False))}


@router.get("", response_class=HTMLResponse)
def discussions_home(request: Request, kind: str = "", status: str = "open", id: int | None = None,
                     user: User = Depends(require_user), db: Session = Depends(get_db)):
    kind = kind if kind in DISCUSSION_KINDS else ""
    status = status if status in STATUSES else "open"
    q = db.query(Discussion)
    if kind:
        q = q.filter(Discussion.kind == kind)
    if status != "all":
        q = q.filter(Discussion.status == status)
    rows = q.order_by(Discussion.last_activity_at.desc().nullslast(), Discussion.id.desc()).limit(200).all()
    sel = db.get(Discussion, id) if id else (rows[0] if rows else None)
    ctx = {
        "user": user, "rows": rows, "sel": sel, "kind": kind, "status": status,
        "kinds": [(k, discussions.KIND_LABELS[k]) for k in DISCUSSION_KINDS], "kind_labels": discussions.KIND_LABELS,
        "statuses": STATUSES, "links_of": discussions.links, "subject_url_of": discussions.subject_url,
        "discord_ready": discord.bot_configured(), "telegram_ready": bool(telegram.configured() and telegram.group_id()),
        "telegram_token": telegram.configured(),
        "chat": _chat_ctx(db, user, sel),
    }
    return templates.TemplateResponse(request, "discussions.html", ctx)


@router.get("/{did}/chat", response_class=HTMLResponse)
def discussion_chat(did: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    d = db.get(Discussion, did)
    return templates.TemplateResponse(request, "_discussion_chat.html", _chat_ctx(db, user, d))


@router.post("/new")
def discussion_new(request: Request, kind: str = Form("topic"), ref: str = Form(""), title: str = Form(...),
                   body: str = Form(""), ch_discord: str = Form(""), ch_telegram: str = Form(""),
                   user: User = Depends(require_moderator), db: Session = Depends(get_db)):
    kind = kind if kind in DISCUSSION_KINDS else "topic"
    channels = tuple(c for c, on in (("discord", ch_discord), ("telegram", ch_telegram)) if on)
    d = discussions.open_discussion(db, kind=kind, ref=ref, title=title, body=body, user=user, channels=channels)
    return RedirectResponse(f"/discussions?id={d.id}&status=all", status_code=303)


@router.post("/{did}/post", response_class=HTMLResponse)
def discussion_post(did: int, request: Request, body: str = Form(...), user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    d = db.get(Discussion, did)
    if d is not None and d.status == "open":
        discussions.post(db, d, body, user)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_discussion_chat.html", _chat_ctx(db, user, d, fresh=False))
    return RedirectResponse(f"/discussions?id={did}", status_code=303)


@router.post("/{did}/close")
def discussion_close(did: int, user: User = Depends(require_moderator), db: Session = Depends(get_db)):
    d = db.get(Discussion, did)
    if d is not None:
        discussions.close(db, d)
    return RedirectResponse(f"/discussions?id={did}&status=all", status_code=303)


@router.post("/{did}/reopen")
def discussion_reopen(did: int, user: User = Depends(require_moderator), db: Session = Depends(get_db)):
    d = db.get(Discussion, did)
    if d is not None:
        discussions.reopen(db, d)
    return RedirectResponse(f"/discussions?id={did}", status_code=303)


@router.post("/{did}/sync", response_class=HTMLResponse)
def discussion_sync(did: int, request: Request, user: User = Depends(require_moderator), db: Session = Depends(get_db)):
    d = db.get(Discussion, did)
    if d is not None:
        try:
            discussions.sync_discord(db, d)
        except Exception:  # noqa: BLE001
            pass
    return templates.TemplateResponse(request, "_discussion_chat.html", _chat_ctx(db, user, d, fresh=False))


@router.get("/health", response_class=HTMLResponse)
def discussions_health(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The Data Ingest card: the sync thread's state (Telegram poll, Discord passes, groups seen)."""
    from ..services import discussion_sync

    st = discussion_sync.status()
    n_open = db.query(Discussion).filter(Discussion.status == "open").count()
    return templates.TemplateResponse(request, "_discussions_health.html",
                                      {"user": user, "st": st, "n_open": n_open,
                                       "telegram_token": telegram.configured(), "telegram_group": telegram.group_id(),
                                       "discord_bot": discord.bot_configured()})
