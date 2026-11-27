"""Shared option setups (v4.279, services/option_studies.py): the page a Discord / Telegram link opens, the
share endpoint the Option Finder's right-click menu calls, and the Option setups panel on the Curated page.

  POST /setups/option                     moderators: JSON {symbol, expiry, strike, right, spot, entry, entry_src,
                                          target, worst, mode, valued_on, figures, note, image (PNG data URL),
                                          channels: ["discord","telegram"]} -> {ok, id, url, links, errors}
  GET  /setups/option/{id}                the setup's page: snapshot, details, the discussion
  GET  /setups/option/{id}/snapshot.png   the chart snapshot
  GET  /setups/option/{id}/chat           the discussion strip (self-polls; posts go to /discussions/{d}/post)
  GET  /setups/panel?sel=                 the Curated page's panel: recent setups + the picked one's discussion

The prefix is /setups (not /studies) so it never collides with /studies/{sid:int}.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import OptionStudy, User
from ..security import require_moderator, require_user
from ..services import discussions, option_studies
from .discussions import _chat_ctx

router = APIRouter(prefix="/setups", tags=["option-studies"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _ctx(db: Session, user: User, s: OptionStudy, *, fresh: bool = True) -> dict:
    d = option_studies.discussion_of(db, s)
    return {
        "user": user, "s": s, "d": d, "title": option_studies.title_of(s),
        "details": option_studies.details_lines(s), "has_snapshot": option_studies.snapshot_path(s) is not None,
        "rr_url": option_studies.rr_url(s), "history_url": option_studies.history_url(s),
        "finder_url": f"/options?symbol={s.symbol}&tab=find",
        "links": discussions.links(d) if d else {}, "errors": (d.channel_errors or {}) if d else {},
        "by": getattr(s.creator, "display_name", None) or getattr(s.creator, "email", None) or "a moderator",
        "chat": _chat_ctx(db, user, d, fresh=fresh),
    }


@router.post("/option")
async def share_option(request: Request, user: User = Depends(require_moderator), db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "the request body is not JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "the request body is not an object"}, status_code=400)
    chans = payload.get("channels")
    channels = tuple(c for c in discussions.CHANNELS if (not isinstance(chans, list)) or c in chans)
    image = option_studies.decode_image(payload.get("image"))
    try:
        s = option_studies.create(db, user, payload, image=image, channels=channels)
    except option_studies.StudyError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    d = option_studies.discussion_of(db, s)
    return {"ok": True, "id": s.id, "url": option_studies.page_url(s), "title": option_studies.title_of(s),
            "snapshot": bool(s.snapshot), "links": discussions.links(d) if d else {},
            "errors": (d.channel_errors or {}) if d else {}, "discussion_id": s.discussion_id}


@router.get("/option/{sid}", response_class=HTMLResponse)
def option_study_page(sid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    s = db.get(OptionStudy, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="No such option setup")
    return templates.TemplateResponse(request, "option_study.html", _ctx(db, user, s))


@router.get("/option/{sid}/snapshot.png")
def option_study_snapshot(sid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    s = db.get(OptionStudy, sid)
    p = option_studies.snapshot_path(s) if s is not None else None
    if p is None:
        raise HTTPException(status_code=404, detail="No snapshot")
    return FileResponse(str(p), media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/option/{sid}/chat", response_class=HTMLResponse)
def option_study_chat(sid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    s = db.get(OptionStudy, sid)
    d = option_studies.discussion_of(db, s) if s is not None else None
    return templates.TemplateResponse(request, "_discussion_chat.html", _chat_ctx(db, user, d))


@router.get("/panel", response_class=HTMLResponse)
def option_studies_panel(request: Request, sel: int | None = None, user: User = Depends(require_user),
                         db: Session = Depends(get_db)):
    """The Curated page's Option setups panel (user: "In the curated list, we need studies panel to show the
    curated list and also the user messages for the discussion")."""
    rows = option_studies.recent(db, limit=40)
    s = db.get(OptionStudy, sel) if sel else (rows[0] if rows else None)
    d = option_studies.discussion_of(db, s) if s is not None else None
    return templates.TemplateResponse(request, "_option_studies_panel.html", {
        "user": user, "rows": rows, "s": s, "d": d, "sel": s.id if s else None,
        "title_of": option_studies.title_of, "strike_text": option_studies.strike_text,
        "links": discussions.links(d) if d else {}, "chat": _chat_ctx(db, user, d),
        "details": option_studies.details_lines(s) if s else [],
        "has_snapshot": option_studies.snapshot_path(s) is not None if s else False,
        "rr_url": option_studies.rr_url(s) if s else None,
    })
