"""MATP page (Median Analyst Target Price board).

The landing page for approved members after login. Placeholder for now --
the MATP/MBP board (driven by the active Finviz filters) lands in a later
phase. Requires an approved account (pending users are blocked by
require_user and see the awaiting-approval page instead).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user

router = APIRouter(prefix="/matp", tags=["matp"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def matp_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "matp.html", {"user": user})
