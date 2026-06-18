"""Today Overview — the post-login landing page for all approved users.

A market-highlights board: news, economic calendar, market sentiment, company
news, ETF rotation (RRG), ETF leaders, and a correlation matrix. Each card
lazy-loads its own endpoint (added phase by phase) so a slow source never blocks
the page. All data is fetched LIVE (Yahoo / httpx) — this is a "now" view, never
parquet (see CLAUDE.md).

Phase 0: page scaffold + the embedded economic-calendar card. The remaining cards
are styled placeholders whose lazy endpoints land in later phases.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user

router = APIRouter(prefix="/today", tags=["today"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def today_home(request: Request, user: User = Depends(require_user)):
    """The Today Overview landing — a responsive grid of market-highlight cards."""
    return templates.TemplateResponse(request, "today.html", {"user": user})
