"""Insights (v4.242) - the Macro / Industry / Company dropdown.

Macro and Company open their existing pages (``routes/macro.py``, ``routes/company_analysis.py``). Industry has no
page of its own yet: the Sector & Industry page became the top-level "Sector Rotation" (user, 2026-11-06), so this is
an under-construction page until the Insights revamp fills it.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user

router = APIRouter(prefix="/insights", tags=["insights"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("/industry", response_class=HTMLResponse)
def industry_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request, "strategy.html",              # the shared under-construction layout
        {"user": user, "title": "Industry",
         "blurb": "Industry insights are being rebuilt as part of the Insights menu. Until then, the sector and "
                  "industry rotation tree is under Sector Rotation in the menu bar."},
    )
