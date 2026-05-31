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
from ..models import (
    APPROVED,
    DISABLED,
    PENDING,
    ROLE_MEMBER,
    ROLES,
    FinvizFilter,
    MATPRefreshRequest,
    User,
)
from ..security import hash_password, require_admin

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
    # Finviz-filter run control (admins can run MATP from here too)
    active_filters = (
        db.query(FinvizFilter).filter(FinvizFilter.is_active.is_(True)).all()
    )
    open_filter_ids = {
        r.filter_id
        for r in db.query(MATPRefreshRequest).filter(
            MATPRefreshRequest.scope == "filter",
            MATPRefreshRequest.status.in_(("pending", "running")),
        )
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
            "active_filters": active_filters,
            "open_filter_ids": open_filter_ids,
        },
    )


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
