"""Sector & Industry — sector rotation (ETF leaders, RRG, correlation) and, later,
industry KPI / peer views.

The sector-rotation cards reuse the existing /today/* fragment endpoints (etf-leaders,
correlation, rrg) via HTMX — no data duplication. The industry-KPI view is Phase 2
(agent-computed peer scorecards; see COMPANY_ANALYSIS_DESIGN.md).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user

router = APIRouter(prefix="/sector", tags=["sector"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def sector_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "sector.html", {"user": user})


@router.get("/returns", response_class=HTMLResponse)
def sector_returns_panel(request: Request, user: User = Depends(require_user)):
    """Left-panel fragment: per-sector 1/2/4/8-month returns (HTMX-loaded)."""
    from ..services.etf import sector_returns

    return templates.TemplateResponse(request, "_sector_returns.html", sector_returns())


@router.get("/rrg", response_class=HTMLResponse)
def sector_rrg(request: Request, user: User = Depends(require_user)):
    """Interactive RRG fragment: full weekly RS-Ratio/RS-Momentum series per sector,
    with a scrubbable tail (HTMX-loaded into the center card)."""
    from ..services.etf import rrg_series

    return templates.TemplateResponse(request, "_sector_rrg.html", rrg_series())


@router.get("/industries", response_class=HTMLResponse)
def sector_industries_panel(
    request: Request, sector: str = "", user: User = Depends(require_user)
):
    """Fragment: the picked sector's tickers grouped by industry (HTMX-loaded into
    the left-column Industries panel when a sector row is clicked)."""
    from ..services.industry import sector_industries

    return templates.TemplateResponse(request, "_sector_industries.html", sector_industries(sector))
