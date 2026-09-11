"""Sector & Industry — sector rotation (ETF leaders, RRG, correlation) and, later,
industry KPI / peer views.

The rotation cards HTMX-load this router's own fragments (/sector/returns, /sector/rrg,
/sector/chart) off the shared, cached services.etf helpers — no data duplication. (They
originally reused /today/* fragments; that page was removed 2026-09-07.) The
industry-KPI view is Phase 2 (agent-computed peer scorecards; see
COMPANY_ANALYSIS_DESIGN.md).
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
    # ETF_UNIVERSE drives the Sector ETFs tab's buttons, so that tab and the RRG /
    # returns panels can never list a different set of sectors. The ORDER comes from
    # the left panel (weekly, its default) so the tab reads strongest-rotation-first
    # and the two lists agree on sight; the page then keeps them in sync client-side
    # when the panel's Daily/Weekly toggle re-groups it.
    from ..services.etf import ETF_UNIVERSE, INDEX_ETFS, panel_symbol_order

    names = dict(ETF_UNIVERSE)
    etfs = [{"symbol": s, "name": names.get(s, s)} for s in panel_symbol_order("weekly")]
    # SPY / QQQ ride along as a SECOND group on the tab, kept out of the sector list
    # above so the rotation panel, the RRG and the industry drill-down still see
    # exactly the 11 sectors (see services/etf.py::INDEX_ETFS). They keep their
    # declared order — there is no rotation ranking to sort an index by.
    indexes = [{"symbol": s, "name": n} for s, n in INDEX_ETFS]
    return templates.TemplateResponse(
        request, "sector.html", {"user": user, "etfs": etfs, "indexes": indexes},
    )


@router.get("/returns", response_class=HTMLResponse)
def sector_returns_panel(
    request: Request, tf: str = "weekly", user: User = Depends(require_user)
):
    """Left-panel fragment: per-sector 1/2/4/8-month returns, grouped by RRG quadrant.

    `tf` picks the RRG timeframe the grouping uses, and it matters more than it
    sounds: measured 2026-09-07, **5 of the 11 sectors sit in a different quadrant
    daily vs weekly**. Health Care was Leading on weekly and Lagging on daily;
    Communication Services the reverse.

    Defaults to **weekly**, which is the timeframe the RRG chart is read on and what
    `services.etf.rrg()` itself defaults to. It previously defaulted to daily on the
    belief that daily was the embed's default -- it is not, and the panel therefore
    contradicted the chart beside it for half the sectors. Daily is still available
    from the toggle for anyone who switches the chart to it.
    """
    from ..services.etf import sector_returns

    ctx = sector_returns(timeframe=tf)
    ctx["user"] = user
    return templates.TemplateResponse(request, "_sector_returns.html", ctx)


@router.get("/etf-structure", response_class=HTMLResponse)
def sector_etf_structure(request: Request, user: User = Depends(require_user)):
    """Swing-structure monitor for every ETF on the Sector ETFs tab.

    User's rule (2026-09-10): *Higher High, Higher Low = Bullish Trend. Lower High
    or Lower Low = Momentum Decreased.* Implemented once in
    `services/structure.py` and read here for all thirteen symbols at once, so the
    tab answers "which sectors are still making higher lows?" at a glance instead
    of one chart at a time.

    Grouped by verdict rather than listed in sector order: the useful question is
    which names sit on each side of the line, and reading that off a mixed list
    means checking thirteen colours one by one.
    """
    from ..services.etf import ETF_UNIVERSE, INDEX_ETFS, panel_symbol_order
    from ..services.structure import structure_for_many

    names = dict(ETF_UNIVERSE) | dict(INDEX_ETFS)
    syms = panel_symbol_order("weekly") + [s for s, _ in INDEX_ETFS]
    res = structure_for_many(syms)

    buckets: dict[str, list] = {"bullish": [], "decelerated": [], "unclear": []}
    for s in syms:
        st = res.get(s) or {}
        buckets.setdefault(st.get("state", "unclear"), []).append({
            "symbol": s, "name": names.get(s, s),
            "verdict": st.get("verdict", "Unclear"), "reason": st.get("reason", ""),
            "high": st.get("high"), "low": st.get("low"),
        })
    groups = [
        {"state": "bullish", "label": "bullish", "rows": buckets["bullish"]},
        {"state": "decelerated", "label": "decelerated", "rows": buckets["decelerated"]},
        {"state": "unclear", "label": "unclear", "rows": buckets["unclear"]},
    ]
    return templates.TemplateResponse(
        request, "_sector_structure.html",
        # 5 min: the underlying structure moves on DAILY bars, so this is about
        # picking up an intraday break of the last swing soon after it happens,
        # not about churning. Every symbol is served from the 15-min price cache
        # in between, so a poll is nearly free.
        {"user": user, "groups": groups, "poll_in": 300},
    )


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
    return templates.TemplateResponse(
        request, "_sector_chart.html", _chart_ctx(db, user, symbol))


def _chart_ctx(db: Session, user: User, symbol: str) -> dict:
    """The one chart context that both the inline Chart tab and the pop-out chart
    window render, so a holding opened in its own window is the SAME chart —
    MATP/MBP, analyst band, drawing tools, Curate setup, Plot on TV — rather than a
    lookalike that drifts from it."""
    from ..models import MATPLevel
    from .matp import _chart_context

    sym = (symbol or "").strip().upper()
    sel = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    cc = _chart_context(db, sel)
    return {"user": user, "symbol": sym, "sel": sel,
            "sel_band": cc["sel_band"], "sel_patterns": cc["sel_patterns"]}


@router.get("/chart-window", response_class=HTMLResponse)
def sector_chart_window(request: Request, symbol: str = "",
                        user: User = Depends(require_user),
                        db: Session = Depends(get_db)):
    """A ticker's chart as a full page, opened in its own window from the Sector ETFs
    holdings panel (user, 2026-09-11: "when i click the tickers i will open into a
    chart into a new window, same chart function").

    chromeless: no site header / menu in the pop-out (user, 2026-09-11: "when i click
    on the ticker with the new windows pop up, do no show the heading and menu") — it
    is a chart window, not a second copy of the site. base.html honours the flag."""
    return templates.TemplateResponse(
        request, "sector_chart_window.html",
        {**_chart_ctx(db, user, symbol), "chromeless": True})


@router.get("/etf-holdings", response_class=HTMLResponse)
def sector_etf_holdings(request: Request, symbol: str = "", sort: str = "",
                        user: User = Depends(require_user)):
    """Fragment: what one ETF holds, joined to each holding's performance and sorted
    — the Sector ETFs tab's right-hand panel. Sources and caching are in
    services/etf_holdings.py (issuer holdings file + Finviz performance)."""
    from ..services import etf_holdings as eh

    return templates.TemplateResponse(
        request, "_sector_etf_holdings.html", eh.components(symbol, sort))


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
