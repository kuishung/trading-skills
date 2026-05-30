"""Finviz filter manager.

Moderators+ curate a list of saved Finviz screener filters: a URL + a
description + an active/inactive flag. This page ONLY manages the list --
it does not run any scan. The active filters are consumed by the scanning
step elsewhere/later. Members can view the list (read-only).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import FinvizFilter, User
from ..security import require_moderator, require_user

router = APIRouter(prefix="/finviz", tags=["finviz"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def finviz_home(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    filters = (
        db.query(FinvizFilter)
        .order_by(FinvizFilter.is_active.desc(), FinvizFilter.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "finviz.html",
        {"user": user, "filters": filters, "can_edit": user.can_moderate},
    )


@router.post("/filters")
def add_filter(
    request: Request,
    description: str = Form(...),
    url: str = Form(...),
    is_active: str | None = Form(None),  # checkbox: present only when checked
    mod: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    desc, link = description.strip(), url.strip()
    if desc and link:
        db.add(
            FinvizFilter(
                description=desc,
                url=link,
                is_active=is_active is not None,
                created_by=mod.id,
            )
        )
        db.commit()
    return RedirectResponse(url="/finviz", status_code=303)


@router.post("/filters/{fid}/toggle")
def toggle_filter(fid: int, mod: User = Depends(require_moderator), db: Session = Depends(get_db)):
    f = db.get(FinvizFilter, fid)
    if f:
        f.is_active = not f.is_active
        db.commit()
    return RedirectResponse(url="/finviz", status_code=303)


@router.post("/filters/{fid}/delete")
def delete_filter(fid: int, mod: User = Depends(require_moderator), db: Session = Depends(get_db)):
    f = db.get(FinvizFilter, fid)
    if f:
        db.delete(f)
        db.commit()
    return RedirectResponse(url="/finviz", status_code=303)
