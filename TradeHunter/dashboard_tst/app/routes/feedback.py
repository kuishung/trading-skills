"""Development feedback board.

Any approved member can post a comment about the build as it progresses,
and see everyone else's. This is the "collaborator comments on the
development as it goes" surface.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Feedback, User
from ..security import require_moderator, require_user

router = APIRouter(prefix="/feedback", tags=["feedback"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def list_feedback(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    items = db.query(Feedback).order_by(Feedback.created_at.desc()).all()
    return templates.TemplateResponse(
        request, "feedback.html", {"user": user, "items": items}
    )


@router.post("")
def post_feedback(
    request: Request,
    body: str = Form(...),
    topic: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    text = body.strip()
    if text:
        db.add(Feedback(user_id=user.id, topic=(topic.strip() or None), body=text))
        db.commit()
    return RedirectResponse(url="/feedback", status_code=303)


@router.post("/{fid}/delete")
def delete_feedback(
    fid: int,
    request: Request,
    mod: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Moderators and admins can remove any feedback post."""
    item = db.get(Feedback, fid)
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url="/feedback", status_code=303)
