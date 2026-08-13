"""Sector & Industry — sector rotation (ETF leaders, RRG, correlation) and, later,
industry KPI / peer views.

The sector-rotation cards reuse the existing /today/* fragment endpoints (etf-leaders,
correlation, rrg) via HTMX — no data duplication. The industry-KPI view is Phase 2
(agent-computed peer scorecards; see COMPANY_ANALYSIS_DESIGN.md).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
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
    with a scrubbable tail (HTMX-loaded into the center card). Initializes the
    sector show/hide state from the user's saved preference. Also carries the
    leaders table (relative strength vs SPY) for the collapsed, click-to-expand
    panel below the chart — same source that orders the RRG list."""
    from ..services.etf import etf_leaders, rrg_series

    ctx = rrg_series()
    prefs = getattr(user, "prefs", None) or {}
    # list of VISIBLE sector symbols; None => all visible (default).
    ctx["visible"] = prefs.get("rrg_sectors")
    lead = etf_leaders()
    ctx["leaders"] = lead.get("rows") or []
    ctx["leaders_spy"] = lead.get("spy")
    return templates.TemplateResponse(request, "_sector_rrg.html", ctx)


@router.post("/rrg/prefs")
def sector_rrg_prefs(
    payload: dict = Body(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Persist the user's RRG sector show/hide selection (list of VISIBLE symbols)."""
    syms = payload.get("sectors")
    u = db.get(User, user.id)
    if u is None:
        return {"ok": False}
    prefs = dict(u.prefs or {})
    if isinstance(syms, list):
        prefs["rrg_sectors"] = [str(s).strip().upper() for s in syms if s][:20]
    else:
        prefs.pop("rrg_sectors", None)
    u.prefs = prefs
    db.commit()
    return {"ok": True}


@router.get("/industries", response_class=HTMLResponse)
def sector_industries_panel(
    request: Request, sector: str = "", user: User = Depends(require_user)
):
    """Fragment: the picked sector's INDUSTRY HEADERS (name + count), rendered as
    child rows under the sector in the 'Sector and Industry' tree. Clicking an
    industry loads its symbols into the Symbol panel."""
    from ..services.industry import sector_industries

    return templates.TemplateResponse(request, "_sector_industry_headers.html", sector_industries(sector))


@router.get("/symbols", response_class=HTMLResponse)
def sector_symbols_panel(
    request: Request, sector: str = "", industry: str = "", user: User = Depends(require_user)
):
    """Fragment: the tickers of one selected sector+industry (Symbol / Full Name /
    Last Price), rendered into the bottom Symbol panel."""
    from ..services.industry import sector_industries

    data = sector_industries(sector)
    match = next((i for i in data["industries"] if i["name"] == industry), None)
    return templates.TemplateResponse(request, "_sector_symbols.html", {
        "sector": data["sector"], "sector_name": data["name"],
        "industry": industry, "tickers": (match["tickers"] if match else []),
    })
