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
    # The LOCAL corpus (readable report bodies) for the Earnings tab — present when
    # the server has the QuarterlyReport folder synced (cfg.edgar_dir). Empty -> the
    # tab lists the pushed inventory only.
    local_filings, corpus_ok = [], False
    if sym:
        from ..services.edgar_reports import corpus_available, list_filings

        corpus_ok = corpus_available()
        local_filings = list_filings(sym)
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
            "local_filings": local_filings,
            "corpus_ok": corpus_ok,
        },
    )


@router.get("/{symbol}/report", response_class=HTMLResponse)
def company_report(
    request: Request,
    symbol: str,
    period: str,
    user: User = Depends(require_user),
):
    """The Earnings-tab viewer for ONE filing. Prefers the HTML filing (shown with
    its real formatting in a sandboxed iframe); falls back to the cleaned Markdown."""
    from ..services.edgar_reports import filing_meta, read_file

    sym = (symbol or "").strip().upper()
    meta = filing_meta(sym, period)
    # only read the MD text inline when there's no HTML to iframe
    md_text = read_file(sym, period, "md") if (meta and not meta["has_html"]) else None
    return templates.TemplateResponse(
        request, "_edgar_report.html",
        {"symbol": sym, "period": period, "meta": meta, "md_text": md_text},
    )


# Financials trend-chart panels (annual). Each: (title, fmt, [(series name, color, metric)]).
# metric is looked up in ratios first, then level series. fmt: usd (millions) / shares
# (millions) / pct / days.
_FIN_PANELS = [
    ("Revenue · Operating · Net Income", "usd", [
        ("Revenue", "#3b82f6", "revenue"),
        ("Operating Income", "#f97316", "operating_income"),
        ("Net Income", "#84cc16", "net_income")]),
    ("Cash Flow", "usd", [
        ("Net Operating Cash Flow", "#f97316", "ocf"),
        ("Free Cash Flow", "#84cc16", "fcf"),
        ("Net Income", "#22c55e", "net_income"),
        ("Stock-Based Comp", "#ec4899", "sbc")]),
    ("Cash & Debt", "usd", [
        ("Cash & ST Investments", "#22c55e", "cash_and_sti"),
        ("Total Debt", "#ef4444", "total_debt")]),
    ("Shares Outstanding", "shares", [
        ("Shares Outstanding", "#eab308", "shares_out")]),
    ("Cash Conversion Cycle", "days", [
        ("Cash Conversion Cycle", "#a855f7", "ccc")]),
    ("Revenue vs Net Accounts Receivable", "usd", [
        ("Revenue", "#3b82f6", "revenue"),
        ("Net Accounts Receivable", "#f472b6", "receivables")]),
    ("Margins", "pct", [
        ("Gross Margin", "#3b82f6", "gross_margin"),
        ("Operating Margin", "#f97316", "operating_margin"),
        ("Net Margin", "#84cc16", "net_margin")]),
    ("Returns", "pct", [
        ("ROE", "#3b82f6", "roe"),
        ("ROA", "#f59e0b", "roa")]),
]


@router.get("/{symbol}/financials", response_class=HTMLResponse)
def company_financials(
    request: Request,
    symbol: str,
    user: User = Depends(require_user),
):
    """The Financials tab: trend-chart panels built from SEC EDGAR XBRL
    (services/sec_xbrl.annual_financials). Lazy-loaded when the tab is first shown."""
    from ..services.sec_xbrl import annual_financials

    sym = (symbol or "").strip().upper()
    try:
        d = annual_financials(sym)
    except Exception:  # noqa: BLE001
        d = {"symbol": sym, "years": [], "series": {}, "ratios": {}}
    years = d.get("years", [])

    def val(metric, fy):
        r = d.get("ratios", {}).get(metric)
        if r is not None and fy in r:
            return r[fy]
        return d.get("series", {}).get(metric, {}).get(fy)

    panels = []
    for title, fmt, sers in _FIN_PANELS:
        out = []
        for name, color, metric in sers:
            row = []
            for fy in years:
                v = val(metric, fy)
                if v is not None and fmt in ("usd", "shares"):
                    v = round(v / 1e6, 2)  # -> millions
                row.append(v)
            out.append({"name": name, "color": color, "data": row})
        panels.append({"title": title, "fmt": fmt, "series": out})

    return templates.TemplateResponse(request, "_financials_tab.html", {
        "symbol": sym, "years": years, "panels": panels, "has_data": bool(years),
    })


@router.get("/{symbol}/report.html", response_class=HTMLResponse)
def company_report_html(
    symbol: str,
    period: str,
    user: User = Depends(require_user),
):
    """Serve ONE filing's raw HTML for the viewer's sandboxed <iframe src>. A strict
    CSP blocks scripts/objects, and the iframe is sandboxed too, so the untrusted SEC
    markup renders (tables, styling) but can't execute or reach the app."""
    from ..services.edgar_reports import read_file

    html = read_file((symbol or "").strip().upper(), period, "html")
    if html is None:
        return HTMLResponse(
            "<p style='font:14px system-ui;padding:1rem;color:#64748b'>Report HTML not found.</p>",
            status_code=404,
        )
    return HTMLResponse(html, headers={
        "Content-Security-Policy": "script-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
        "X-Content-Type-Options": "nosniff",
    })


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
