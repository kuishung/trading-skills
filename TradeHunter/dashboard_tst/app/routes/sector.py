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


FILTER_ON_PREF = "sector_finviz_filter_on"   # User.prefs: bool, default True


def _sector_filter_on(user: User) -> bool:
    """Whether the saved filter is switched ON (user, 2026-09-15: "the filter user
    can turn it on or off"). Saved criteria stay saved when it is off; they just
    stop narrowing the lists until it is switched back on. Default on, so a member
    who has never touched the switch sees what they saw before it existed."""
    prefs = getattr(user, "prefs", None) or {}
    return bool(prefs.get(FILTER_ON_PREF, True))


def _sector_saved_codes(user: User) -> str:
    """The sanitized Finviz `f=` criteria codes from the user's saved filter URL —
    whether or not the filter is switched on."""
    from ..services.industry import parse_finviz_filters

    return parse_finviz_filters(_sector_filter_url(user))


def _sector_extra_filters(user: User) -> str:
    """The criteria codes to APPLY: the saved ones when the filter is on, else
    none. Every list on the page (industry symbols, industry counts, the ETF
    basket) reads this, so the switch governs all of them at once."""
    return _sector_saved_codes(user) if _sector_filter_on(user) else ""


def _filter_fragment(request: Request, user: User) -> HTMLResponse:
    url = _sector_filter_url(user)
    codes = _sector_saved_codes(user)
    return templates.TemplateResponse(
        request, "_sector_filter.html",
        {"user": user, "filter_url": url, "codes": codes, "filter_on": _sector_filter_on(user)},
    )


@router.post("/filter/toggle", response_class=HTMLResponse)
def sector_filter_toggle(
    request: Request,
    on: str = Form("1"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Switch the saved filter on or off without touching the saved URL. Returns
    the refreshed control and fires `sector-filter-changed`, so whichever list is
    showing re-fetches with (or without) the criteria."""
    u = db.get(User, user.id)
    if u is None:
        return _filter_fragment(request, user)
    prefs = dict(u.prefs or {})
    prefs[FILTER_ON_PREF] = on.strip() not in ("0", "", "false", "off")
    u.prefs = prefs
    db.commit()
    resp = _filter_fragment(request, u)
    resp.headers["HX-Trigger"] = "sector-filter-changed"
    return resp


@router.get("", response_class=HTMLResponse)
def sector_home(request: Request, user: User = Depends(require_user)):
    # ETF_UNIVERSE drives the Sector ETFs tab's buttons, so that tab and the RRG /
    # returns panels can never list a different set of sectors. The ORDER comes from
    # the left panel (weekly, its default) so the tab reads strongest-rotation-first
    # and the two lists agree on sight; the page then keeps them in sync client-side
    # when the panel's Daily/Weekly toggle re-groups it.
    from ..services.etf import ETF_UNIVERSE, INDEX_ETFS, panel_symbol_order

    from ..services.etf import etf_full_name

    names = dict(ETF_UNIVERSE)
    etfs = [{"symbol": s, "name": f"{names.get(s, s)} — {etf_full_name(s)}"}
            for s in panel_symbol_order("weekly")]
    # SPY / QQQ ride along as a SECOND group on the tab, kept out of the sector list
    # above so the rotation panel, the RRG and the industry drill-down still see
    # exactly the 11 sectors (see services/etf.py::INDEX_ETFS). They keep their
    # declared order — there is no rotation ranking to sort an index by.
    indexes = [{"symbol": s, "name": f"{n} — {etf_full_name(s)}"} for s, n in INDEX_ETFS]
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


SYM_CONDS_PREF = "sym_conds"     # User.prefs key: {c1..c4: bool}


def _symbols_context(request: Request, sector: str, industry: str, user: User,
                     db: Session) -> dict:
    """The Symbol panel's context, with the member's four setup conditions
    applied (user, 2026-09-15): tickers are scored from live daily bars against
    whichever conditions are switched on and sorted best-first; each row carries
    chips for the conditions it meets. All four off = the plain list."""
    from ..services import ema_setup as es
    from ..services import user_watchlist as uwl
    from ..services.industry import sector_industries

    data = sector_industries(sector, _sector_extra_filters(user))
    if industry:
        match = next((i for i in data["industries"] if i["name"] == industry), None)
        tickers = list(match["tickers"]) if match else []
    else:
        # No industry = the WHOLE sector (user, 2026-09-15: "when click on a
        # sector, all the stock under the filter will be shown in the ticker
        # panel"). Every industry's tickers, de-duplicated, still under the
        # member's Finviz filter because sector_industries applied it.
        seen: set[str] = set()
        tickers = []
        for ind in data["industries"]:
            for t in ind["tickers"]:
                if t["symbol"] not in seen:
                    seen.add(t["symbol"])
                    tickers.append(dict(t, industry=ind["name"]))
    prefs = getattr(user, "prefs", None) or {}
    enabled = es.clean_enabled(prefs.get(SYM_CONDS_PREF))

    if tickers and any(enabled.values()):
        setups = es.setups_for_many([t["symbol"] for t in tickers],
                                    deep=es.needs_deep(enabled))
        for t in tickers:
            st = setups.get(t["symbol"]) or es._blank()
            t["rank"] = es.rank(st, enabled)
            t["setup"] = st
        tickers.sort(key=lambda t: (
            t["rank"]["score"] is None, -(t["rank"]["score"] or 0),
            t["setup"].get("above_pct") if t["setup"].get("above_pct") is not None else 99.0,
            t["symbol"]))
    return {
        "sector": data["sector"], "sector_name": data["name"],
        "industry": industry, "tickers": tickers,
        "filtered": data.get("filtered", False),
        "my_syms": uwl.symbol_set(db, user),
        "enabled": enabled, "cond_labels": es.COND_LABELS, "cond_keys": es.COND_KEYS,
        "ranked": bool(tickers and any(enabled.values())),
    }


@router.get("/symbols", response_class=HTMLResponse)
def sector_symbols_panel(
    request: Request, sector: str = "", industry: str = "",
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Fragment: the tickers of one selected sector+industry (Symbol / Full Name /
    Last Price), rendered into the bottom Symbol panel. When the user has an active
    Finviz filter, only tickers matching the criteria (within the industry) show.
    Each row carries a My-Watchlist star, pre-filled from this user's list, and the
    list is sorted by the member's switched-on setup conditions (_symbols_context)."""
    return templates.TemplateResponse(
        request, "_sector_symbols.html", _symbols_context(request, sector, industry, user, db))


@router.post("/symbols/conds", response_class=HTMLResponse)
async def sector_symbols_conds(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Save which of the four setup conditions are on (checkboxes; an unticked box
    is absent from the form) and re-render the panel, re-sorted."""
    from ..services import ema_setup as es

    form = await request.form()
    enabled = {k: form.get(k) is not None for k in es.COND_KEYS}
    prefs = dict(getattr(user, "prefs", None) or {})
    prefs[SYM_CONDS_PREF] = enabled
    user = db.merge(user)
    user.prefs = prefs
    db.commit()
    return templates.TemplateResponse(
        request, "_sector_symbols.html",
        _symbols_context(request, form.get("sector") or "", form.get("industry") or "", user, db))


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

    from ..services import curated as cur

    sym = (symbol or "").strip().upper()
    sel = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    cc = _chart_context(db, sel)
    return {"user": user, "symbol": sym, "sel": sel,
            "sel_band": cc["sel_band"], "sel_patterns": cc["sel_patterns"],
            # This member's newest curated call on the ticker, if any: the chart
            # then mounts and frames it exactly as the Curated page does (user,
            # 2026-09-15: "if there is a curated chart i need it to focus on the
            # curation and it will look like this").
            "curated": cur.latest_for_symbol(db, user, sym)}


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
        {**_chart_ctx(db, user, symbol), "chromeless": True,
         # frame the stored trade setup on load (user, 2026-09-15: "the chart needs
         # to focus on the current candle, zoomed to the setup")
         "focus_setup": True})


@router.get("/basket", response_class=HTMLResponse)
def sector_basket(request: Request, symbol: str = "", sort: str = "",
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Fragment: the ETF basket — what one fund holds, filtered by the member's
    Finviz criteria and ranked by their setup conditions (user, 2026-09-15: "select
    by ETF basket of tickers where it will be shortlisted based on the criteria
    given"). The bottom of the ticker panel in ETF-basket mode."""
    return templates.TemplateResponse(
        request, "_sector_basket.html", _basket_context(symbol, sort, user, db))


@router.post("/basket/conds", response_class=HTMLResponse)
async def sector_basket_conds(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Save which setup conditions are on — the SAME preference the industry list
    uses (one technical setup, two ways of picking tickers) — and re-render the
    basket, re-ranked."""
    from ..services import ema_setup as es

    form = await request.form()
    enabled = {k: form.get(k) is not None for k in es.COND_KEYS}
    prefs = dict(getattr(user, "prefs", None) or {})
    prefs[SYM_CONDS_PREF] = enabled
    user = db.merge(user)
    user.prefs = prefs
    db.commit()
    return templates.TemplateResponse(
        request, "_sector_basket.html",
        _basket_context(form.get("symbol") or "", form.get("sort") or "", user, db))


def _basket_context(symbol: str, sort: str, user: User, db: Session) -> dict:
    import datetime as _dt

    from ..models import CuratedTicker
    from ..services import ema_setup as es
    from ..services import etf_holdings as eh
    from ..services import user_watchlist as uwl

    prefs = getattr(user, "prefs", None) or {}
    enabled = es.clean_enabled(prefs.get(SYM_CONDS_PREF))
    ctx = eh.components(symbol, sort, extra_filters=_sector_extra_filters(user),
                        enabled=enabled)
    # which holdings THIS member curated TODAY (user, 2026-09-13), so a name already
    # worked on this session is marked in the list
    today = _dt.date.today().isoformat()
    curated = db.query(CuratedTicker.symbol).filter(
        CuratedTicker.user_id == user.id, CuratedTicker.curated_on == today).all()
    ctx.update({
        "my_syms": uwl.symbol_set(db, user),
        "curated_today": {r[0] for r in curated},
        "enabled": enabled, "cond_labels": es.COND_LABELS, "cond_keys": es.COND_KEYS,
    })
    return ctx


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
