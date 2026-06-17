"""Admin control plane (admin-only).

Responsibilities:
  1. Authorization. In password mode (Path B) the admin *creates* member
     accounts directly (approved on creation). In google mode (Path A)
     users self-arrive as 'pending' and the admin approves them.
     Either mode: disable / set-role.
  2. The swing-bot CONTROL surface.

CRITICAL (DESIGN.md section 6): control plane only. It never holds broker
credentials and never submits orders. The swing-bot execution plane runs
trusted-side; these endpoints will relay an authenticated signal to it.
Until that relay is built, bot actions are explicit stubs.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..menus import ALL_KEYS, MENUS, allowed_keys
from ..models import APPROVED, DISABLED, PENDING, ROLE_MEMBER, ROLES, User
from ..security import hash_password, require_admin
from ..services import discord

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def admin_home(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    pending = db.query(User).filter(User.status == PENDING).order_by(User.created_at).all()
    members = (
        db.query(User).filter(User.status != PENDING).order_by(User.status, User.email).all()
    )
    counts = {
        "total": len(pending) + len(members),
        "pending": len(pending),
        "active": sum(1 for u in members if u.status == APPROVED),
        "disabled": sum(1 for u in members if u.status == DISABLED),
    }
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": admin,
            "pending": pending,
            "members": members,
            "counts": counts,
            "auth_mode": settings.auth_mode,
            "is_google_auth": settings.is_google_auth,
            "discord_configured": discord.configured(),
            "menus": MENUS,                                  # (key,label,group,href)
            "menu_allowed": {u.id: allowed_keys(u) for u in members},  # per-user granted set
        },
    )


@router.post("/discord-test", response_class=HTMLResponse)
def discord_test(admin: User = Depends(require_admin)):
    """Fire a test post to the configured Discord webhook (HTMX fragment result).
    Lets an admin confirm the integration without waiting for a real refresh."""
    if not discord.configured():
        return HTMLResponse(
            '<span class="text-rose-300">No webhook configured — set '
            '<code>TST_DISCORD_WEBHOOK_URL</code> in app/.env and restart.</span>'
        )
    ok = discord.post_embed(
        title="🔔 TradeHunter · test",
        description=f"Test post from {admin.display_name or admin.email}. "
        "If you can read this in your channel, the Discord integration works.",
        url=settings.public_url,
    )
    if ok:
        return HTMLResponse('<span class="text-emerald-300">Sent ✓ — check your Discord channel.</span>')
    return HTMLResponse('<span class="text-rose-300">Webhook POST failed — see the server log.</span>')


def _get(db: Session, uid: int) -> User | None:
    return db.get(User, uid)


@router.post("/users")
def create_user(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: str = Form("member"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Password-mode account creation (approved immediately). No-op in google
    mode, where users self-arrive via OAuth and are approved instead."""
    if settings.is_google_auth:
        return RedirectResponse(url="/admin", status_code=303)
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first() is None:
        db.add(
            User(
                email=email,
                display_name=display_name or email,
                password_hash=hash_password(password),
                role=role if role in ROLES else ROLE_MEMBER,
                status=APPROVED,
                approved_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/approve")
def approve_user(uid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _get(db, uid)
    if u and u.status != APPROVED:
        u.status = APPROVED
        u.approved_at = _dt.datetime.now(_dt.timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/disable")
def disable_user(uid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _get(db, uid)
    if u and u.id != admin.id:  # don't let an admin lock themselves out
        u.status = DISABLED
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/role")
def set_role(
    uid: int,
    role: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = _get(db, uid)
    # validate role; don't let an admin change their own role (lockout guard)
    if u and u.id != admin.id and role in ROLES:
        u.role = role
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/menus")
def set_menus(
    uid: int,
    keys: list[str] = Form(default=[]),    # checked menu keys (unchecked = absent)
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set a user's per-page menu access (app/menus.py keys). An explicit empty list
    = no access; the granted subset hides everything else + blocks the URLs. Has no
    effect on admins/moderators (they always see all). Members only."""
    u = _get(db, uid)
    if u is not None:
        u.menu_access = [k for k in keys if k in ALL_KEYS]
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# ---- swing-bot control plane (admin-only; NO execution in this process) ----

_ALLOWED_BOT_ACTIONS = {"enable", "disable", "arm", "disarm", "status"}


@router.post("/bot/{action}")
def bot_control(action: str, admin: User = Depends(require_admin)):
    if action not in _ALLOWED_BOT_ACTIONS:
        return {"ok": False, "detail": f"unknown action {action!r}"}
    # TODO Phase 6: relay an authenticated signal to the trusted-side swing
    # orchestrator (enable/disable -> state/enabled_<swing>.flag, arm/disarm
    # -> state/armed_<swing>.flag). This process MUST NOT hold broker creds
    # or open an IBKR session.
    return {
        "ok": False,
        "action": action,
        "detail": "control-plane stub -- execution plane is trusted-side and not wired yet",
    }
