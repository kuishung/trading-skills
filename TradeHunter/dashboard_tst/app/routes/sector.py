"""Sector & Industry — sector rotation (ETF leaders, RRG, correlation) and, later,
industry KPI / peer views.

The sector-rotation cards reuse the existing /today/* fragment endpoints (etf-leaders,
correlation, rrg) via HTMX — no data duplication. The industry-KPI view is Phase 2
(agent-computed peer scorecards; see COMPANY_ANALYSIS_DESIGN.md).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, Form, Request
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


def _sector_filter_url(user: User) -> str:
    """The user's saved Finviz screener URL for the Symbol-panel filter ('' if none)."""
    prefs = getattr(user, "prefs", None) or {}
    return prefs.get("sector_finviz_filter") or ""


def _sector_extra_filters(user: User) -> str:
    """The sanitized Finviz `f=` criteria codes from the user's saved filter URL."""
    from ..services.industry import parse_finviz_filters

    return parse_finviz_filters(_sector_filter_url(user))


def _filter_fragment(request: Request, user: User) -> HTMLResponse:
    url = _sector_filter_url(user)
    codes = _sector_extra_filters(user)
    return templates.TemplateResponse(
        request, "_sector_filter.html",
        {"user": user, "filter_url": url, "codes": codes},
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
    industry loads its symbols into the Symbol panel. Honours the user's active
    Symbol-panel Finviz filter so the counts reflect only matching tickers."""
    from ..services.industry import sector_industries

    return templates.TemplateResponse(
        request, "_sector_industry_headers.html",
        sector_industries(sector, _sector_extra_filters(user)),
    )


@router.get("/symbols", response_class=HTMLResponse)
def sector_symbols_panel(
    request: Request, sector: str = "", industry: str = "",
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Fragment: the tickers of one selected sector+industry (Symbol / Full Name /
    Last Price), rendered into the bottom Symbol panel. When the user has an active
    Finviz filter, only tickers matching the criteria (within the industry) show.
    Each row carries a My-Watchlist star, pre-filled from this user's list."""
    from ..services.industry import sector_industries
    from ..services import user_watchlist as uwl

    data = sector_industries(sector, _sector_extra_filters(user))
    match = next((i for i in data["industries"] if i["name"] == industry), None)
    return templates.TemplateResponse(request, "_sector_symbols.html", {
        "sector": data["sector"], "sector_name": data["name"],
        "industry": industry, "tickers": (match["tickers"] if match else []),
        "filtered": data.get("filtered", False),
        "my_syms": uwl.symbol_set(db, user),
    })


@router.get("/chart", response_class=HTMLResponse)
def sector_chart(
    request: Request,
    symbol: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The inline Chart tab fragment for ONE ticker — the same watchlist chart
    (EMA20/50/200 + MATP/MBP lines + analyst band when the ticker is on the MATP
    board; a plain price chart otherwise). Rendered into #sectorChartBody when a
    Symbol-panel ticker is clicked, so the chart shows in-page (no new window).
    Reuses matp's _chart_context so it matches the Watchlist exactly."""
    from ..models import MATPLevel
    from .matp import _chart_context

    sym = (symbol or "").strip().upper()
    sel = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    cc = _chart_context(db, sel)
    return templates.TemplateResponse(request, "_sector_chart.html", {
        "user": user, "symbol": sym, "sel": sel,
        "sel_band": cc["sel_band"], "sel_patterns": cc["sel_patterns"],
    })


@router.get("/filter", response_class=HTMLResponse)
def sector_filter_control(request: Request, user: User = Depends(require_user)):
    """The Symbol-panel filter control (toggle button + URL form), reflecting the
    user's currently-saved Finviz screener filter. Loaded into the Symbol pane header."""
    return _filter_fragment(request, user)


@router.post("/filter", response_class=HTMLResponse)
def sector_set_filter(
    request: Request,
    url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save (or clear, when empty / no criteria) the user's Symbol-panel Finviz
    filter URL. Returns the refreshed control and fires `sector-filter-changed` so
    the page re-fetches the currently-selected industry's (now filtered) symbols."""
    from ..services.industry import parse_finviz_filters

    u = db.get(User, user.id)
    if u is None:
        return _filter_fragment(request, user)
    prefs = dict(u.prefs or {})
    codes = parse_finviz_filters(url)
    if codes:
        prefs["sector_finviz_filter"] = url.strip()
    else:
        prefs.pop("sector_finviz_filter", None)
    u.prefs = prefs
    db.commit()
    resp = _filter_fragment(request, u)
    resp.headers["HX-Trigger"] = "sector-filter-changed"
    return resp
