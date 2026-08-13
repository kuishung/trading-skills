"""Company Analysis — per-ticker dossier.

Sections: Business Model, Business Segment, Competitive Analysis, Suppliers,
Key Metrics (KPI). See dashboard_tst/COMPANY_ANALYSIS_DESIGN.md.

Content is agent-generated (EDGAR + industry knowledge, pushed via
POST /api/company-analysis/{symbol}/{section}) or moderator-edited here.
View = members with the `company_analysis` menu; edit = moderators.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    CA_SECTIONS,
    CA_SECTION_LABELS,
    CompanyAnalysis,
    EdgarIngestHealth,
    MATPLevel,
    User,
    _utcnow,
)
from ..security import require_moderator, require_user

router = APIRouter(prefix="/company-analysis", tags=["company-analysis"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _sections_for(db: Session, symbol: str):
    """Ordered [(key, label, row-or-None)] for the 5 canonical sections."""
    rows = {
        r.section: r
        for r in db.query(CompanyAnalysis).filter(CompanyAnalysis.symbol == symbol).all()
    }
    return [(key, CA_SECTION_LABELS[key], rows.get(key)) for key in CA_SECTIONS]


@router.get("", response_class=HTMLResponse)
def company_analysis_home(
    request: Request,
    symbol: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    sym = (symbol or "").strip().upper()
    sections = _sections_for(db, sym) if sym else []
    recent = sorted({r[0] for r in db.query(CompanyAnalysis.symbol).distinct().all()})
    # Watchlist-style chart at the top: MATP/MBP level lines + analyst band when the
    # ticker is in the MATP board; otherwise a plain price chart (sel=None → the
    # template renders price-only). Reuses matp's _chart_context so the band/patterns
    # match the Watchlist exactly.
    sel, sel_band, sel_patterns = None, None, []
    if sym:
        sel = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
        from .matp import _chart_context

        cc = _chart_context(db, sel)
        sel_band, sel_patterns = cc["sel_band"], cc["sel_patterns"]
    # Downloaded EDGAR earnings filings for this ticker (pushed by AI-Hermes; the
    # corpus files live there, we surface the inventory/status here).
    edgar = None
    if sym:
        from ..services.edgar_health import ticker_status

        erow = (
            db.query(EdgarIngestHealth)
            .order_by(EdgarIngestHealth.received_at.desc())
            .first()
        )
        if erow and erow.report:
            edgar = ticker_status(erow.report, sym, erow.received_at)
    return templates.TemplateResponse(
        request,
        "company_analysis.html",
        {
            "user": user,
            "symbol": sym,
            "sections": sections,
            "recent": recent,
            "sel": sel,
            "sel_band": sel_band,
            "sel_patterns": sel_patterns,
            "edgar": edgar,
        },
    )


@router.post("/{symbol}/{section}")
def edit_section(
    symbol: str,
    section: str,
    request: Request,
    body: str = Form(""),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Moderator edit of a section's prose (Phase 1). The agent pushes structured
    content via the API endpoint; this is the manual path so the page is useful now."""
    sym = (symbol or "").strip().upper()
    if section not in CA_SECTIONS:
        return RedirectResponse(f"/company-analysis?symbol={sym}", status_code=303)
    row = (
        db.query(CompanyAnalysis)
        .filter(CompanyAnalysis.symbol == sym, CompanyAnalysis.section == section)
        .first()
    )
    if row is None:
        row = CompanyAnalysis(symbol=sym, section=section)
        db.add(row)
    row.body = body
    row.source_kind = "manual"
    row.as_of = _utcnow()
    row.updated_by = user.display_name or user.email
    db.commit()
    return RedirectResponse(f"/company-analysis?symbol={sym}", status_code=303)
