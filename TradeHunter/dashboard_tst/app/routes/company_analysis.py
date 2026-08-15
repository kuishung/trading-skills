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


@router.get("/{symbol}/overview", response_class=HTMLResponse)
def company_overview(
    request: Request,
    symbol: str,
    user: User = Depends(require_user),
):
    """The Overview tab — a snapshot combining SEC XBRL fundamentals + Yahoo profile /
    market data. Lazy-loaded on first tab activation."""
    from ..services.sec_xbrl import annual_financials, quarterly_financials
    from ..services.overview import overview

    sym = (symbol or "").strip().upper()
    try:
        a = annual_financials(sym)
        q = quarterly_financials(sym)
        ov = overview(sym, a, q.get("ttm", {}))
    except Exception:  # noqa: BLE001
        ov = {"symbol": sym, "sections": [], "description": None}
    return templates.TemplateResponse(request, "_overview_tab.html", {"symbol": sym, "ov": ov})


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
    """The Financials tab: trend-chart panels (Annual + Quarterly, each with a TTM
    point) + ratio tables (per-year + Current[TTM] + 5Y/10Y avg), all from SEC EDGAR
    XBRL. Lazy-loaded when the tab is first shown."""
    from ..services.sec_xbrl import annual_financials, quarterly_financials

    sym = (symbol or "").strip().upper()
    try:
        a = annual_financials(sym)
    except Exception:  # noqa: BLE001
        a = {"symbol": sym, "years": [], "series": {}, "ratios": {}}
    try:
        q = quarterly_financials(sym)
    except Exception:  # noqa: BLE001
        q = {"quarters": [], "labels": [], "series": {}, "ttm": {}}
    years = a.get("years", [])
    qt = q.get("ttm", {})

    def _d(x, y):
        return (x / y) if (x is not None and y) else None

    def aval(metric, fy):
        r = a.get("ratios", {}).get(metric)
        if r is not None and fy in r:
            return r[fy]
        return a.get("series", {}).get(metric, {}).get(fy)

    def qval(metric, e):
        return q.get("series", {}).get(metric, {}).get(e)

    def ttmv(metric):
        # TTM value: margins from summed TTM flows; level flows/latest-stocks from the
        # quarterly TTM dict; rolling ratios (roe/roa/ccc) from the latest quarter.
        if metric in ("gross_margin", "operating_margin", "net_margin", "ocf_margin", "fcf_margin"):
            rev = qt.get("revenue")
            gp = qt.get("gross_profit")
            if gp is None and rev is not None and qt.get("cost_of_revenue") is not None:
                gp = rev - qt["cost_of_revenue"]
            num = {"gross_margin": gp, "operating_margin": qt.get("operating_income"),
                   "net_margin": qt.get("net_income"), "ocf_margin": qt.get("ocf"),
                   "fcf_margin": qt.get("fcf")}[metric]
            v = _d(num, rev)
            return round(v * 100, 2) if v is not None else None
        if metric in qt:
            return qt[metric]
        qs = q.get("quarters") or []
        return q.get("series", {}).get(metric, {}).get(qs[-1]) if qs else None

    def build(get, keys):
        panels = []
        for title, fmt, sers in _FIN_PANELS:
            out = []
            for name, color, metric in sers:
                row = []
                for k in keys:
                    v = ttmv(metric) if k == "__TTM__" else get(metric, k)
                    if v is not None and fmt in ("usd", "shares"):
                        v = round(v / 1e6, 2)  # -> millions
                    row.append(v)
                out.append({"name": name, "color": color, "data": row})
            panels.append({"title": title, "fmt": fmt, "series": out})
        return panels

    q_ends = q.get("quarters") or []
    chart = {
        "annual": {
            "labels": [str(y) for y in years] + (["TTM"] if years else []),
            "panels": build(aval, years + (["__TTM__"] if years else [])),
        },
        "quarterly": {
            "labels": (q.get("labels") or []) + (["TTM"] if q_ends else []),
            "panels": build(qval, q_ends + (["__TTM__"] if q_ends else [])),
        },
    }

    # ── ratio tables (per-year + Current[TTM] + 5Y/10Y avg) ──
    def stat(metric):
        r = a.get("ratios", {}).get(metric, {})
        cur = ttmv(metric)
        if cur is None:
            cur = r.get(years[-1]) if years else None

        def avgn(n):
            vals = [r[fy] for fy in years[-n:] if r.get(fy) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None
        return {
            "by_year": {fy: r.get(fy) for fy in years},
            "current": cur, "avg5": avgn(5), "avg10": avgn(10),
        }

    tables = []
    for title, rows in _RATIO_TABLES:
        tables.append({"title": title, "rows": [
            {"label": label, "fmt": fmt, **stat(metric)} for (label, metric, fmt) in rows
        ]})

    # ── Price ratios (PE/PS/PB) — SEC fundamentals × live Yahoo prices ──
    valn = {}
    try:
        from ..services.valuation import valuation
        valn = valuation(sym, a, qt)
    except Exception:  # noqa: BLE001
        valn = {}
    by_year_v = valn.get("by_year", {})
    cur_v = valn.get("current", {})

    def pr_stat(k):
        by = {fy: by_year_v.get(fy, {}).get(k) for fy in years}
        cur = cur_v.get(k)

        def avgn(n):
            vals = [by[fy] for fy in years[-n:] if by.get(fy) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None
        return {"by_year": by, "current": cur, "avg5": avgn(5), "avg10": avgn(10)}

    if by_year_v or cur_v:
        tables.append({"title": "Price Ratios", "rows": [
            {"label": "Price / Earnings (PE)", "fmt": "x", **pr_stat("pe")},
            {"label": "Price / Sales (PS)", "fmt": "x", **pr_stat("ps")},
            {"label": "Price / Book (PB)", "fmt": "x", **pr_stat("pb")},
        ]})

    # detailed financial statements (Income / Balance / Cash Flow)
    stmts = {}
    try:
        from ..services.statements import statements
        stmts = statements(sym).get("statements", {})
    except Exception:  # noqa: BLE001
        stmts = {}

    return templates.TemplateResponse(request, "_financials_tab.html", {
        "symbol": sym, "years": years, "chart": chart,
        "tables": tables, "has_data": bool(years), "valuation": valn,
        "statements": stmts,
    })


# ratio tables: (section title, [(row label, metric, fmt)]). fmt: pct / x (ratio) / d (days).
_RATIO_TABLES = [
    ("Profitability Ratios", [
        ("Gross Profit Margin %", "gross_margin", "pct"),
        ("Operating Profit Margin %", "operating_margin", "pct"),
        ("Net Profit Margin %", "net_margin", "pct"),
        ("Operating Cash Flow Margin %", "ocf_margin", "pct"),
        ("Free Cash Flow Margin %", "fcf_margin", "pct"),
        ("Return on Assets (ROA) %", "roa", "pct"),
        ("Return on Equity (ROE) %", "roe", "pct"),
        ("Return on Invested Capital (ROIC) %", "roic", "pct"),
    ]),
    ("Debt & Liquidity Ratios", [
        ("Cash Ratio", "cash_ratio", "x"),
        ("Current Ratio", "current_ratio", "x"),
        ("Interest Coverage", "interest_coverage", "x"),
        ("Total Debt / EBITDA", "debt_to_ebitda", "x"),
    ]),
    ("Efficiency Ratios", [
        ("Asset Turnover", "asset_turnover", "x"),
        ("Fixed Asset Turnover", "fixed_asset_turnover", "x"),
        ("Inventory Turnover", "inventory_turnover", "x"),
        ("Receivables Turnover", "receivables_turnover", "x"),
        ("Days Inventory Outstanding", "dio", "d"),
        ("Days Sales Outstanding", "dso", "d"),
        ("Days Payables Outstanding", "dpo", "d"),
        ("Cash Conversion Cycle", "ccc", "d"),
        ("CapEx to Revenue", "capex_to_revenue", "x"),
        ("CapEx to Operating Cash Flow", "capex_to_ocf", "x"),
        ("CapEx to Operating Income", "capex_to_opinc", "x"),
    ]),
]


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
