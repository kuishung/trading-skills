"""Finviz filter manager.

Moderators+ curate a list of saved Finviz screener filters: a URL + a
description + an active/inactive flag. This page ONLY manages the list --
it does not run any scan. The active filters are consumed by the scanning
step elsewhere/later. Members can view the list (read-only).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import RUN_INTERVALS, FinvizFilter, User
from ..security import require_moderator, require_user

router = APIRouter(prefix="/finviz", tags=["finviz"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


# --- Finviz screener URL -> readable filter criteria ------------------------
# Decode the `f=` query parameter (comma-separated filter tokens like
# `cap_largeover`, `sh_avgvol_o500`, `ta_sma200_pa`) into human-readable chips
# so the list shows what a filter actually screens for, not a meaningless URL.
_FINVIZ_CATS = {
    "exch": "Exchange", "idx": "Index", "sec": "Sector", "ind": "Industry",
    "geo": "Country", "cap": "Market Cap",
    "sh_price": "Price", "sh_avgvol": "Avg Volume", "sh_relvol": "Rel Volume",
    "sh_curvol": "Volume", "sh_float": "Float", "sh_short": "Short Float",
    "sh_outstanding": "Shares Out",
    "fa_div": "Dividend", "fa_pe": "P/E", "fa_fpe": "Fwd P/E", "fa_peg": "PEG",
    "fa_ps": "P/S", "fa_pb": "P/B", "fa_roe": "ROE", "fa_roa": "ROA",
    "fa_debteq": "Debt/Eq", "fa_curratio": "Current Ratio",
    "fa_grossmargin": "Gross Margin", "fa_opermargin": "Oper Margin",
    "fa_netmargin": "Net Margin", "fa_epsyoy": "EPS grth (yr)",
    "fa_eps5years": "EPS grth (5y)", "fa_sales5years": "Sales grth (5y)",
    "ta_sma20": "SMA20", "ta_sma50": "SMA50", "ta_sma200": "SMA200",
    "ta_perf2": "Performance", "ta_perf": "Performance",
    "ta_volatility": "Volatility", "ta_rsi": "RSI", "ta_gap": "Gap",
    "ta_change": "Change", "ta_highlow52w": "52W Range",
    "an_recom": "Analyst Recom", "targetprice": "Target Price",
    "earningsdate": "Earnings", "ipodate": "IPO Date", "news": "News",
}
_FINVIZ_PREFIXES = sorted(_FINVIZ_CATS, key=len, reverse=True)  # longest match first


def _decode_finviz_value(v: str) -> str:
    if not v:
        return ""
    if v[0] == "o" and v[1:].replace(".", "", 1).isdigit():
        return f"> {v[1:]}"
    if v[0] == "u" and v[1:].replace(".", "", 1).isdigit():
        return f"< {v[1:]}"
    if "to" in v:
        a, _, b = v.partition("to")
        return f"{a}-{b}"
    return v.replace("_", " ")


def parse_finviz_criteria(url: str) -> list[str]:
    """Return readable criteria chips from a Finviz screener URL (empty if none)."""
    try:
        f = parse_qs(urlparse(url).query).get("f", [])
    except Exception:
        return []
    if not f:
        return []
    chips = []
    for tok in (t for t in f[0].split(",") if t):
        label = tok
        for p in _FINVIZ_PREFIXES:
            if tok.startswith(p):
                val = _decode_finviz_value(tok[len(p):].lstrip("_"))
                label = f"{_FINVIZ_CATS[p]}: {val}" if val else _FINVIZ_CATS[p]
                break
        chips.append(label)
    return chips


@router.get("", response_class=HTMLResponse)
def finviz_home(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    filters = (
        db.query(FinvizFilter)
        .order_by(FinvizFilter.is_active.desc(), FinvizFilter.created_at.desc())
        .all()
    )
    for f in filters:  # attach decoded criteria chips for the template
        f.criteria = parse_finviz_criteria(f.url)
    from ..models import IngestHealth
    from ..services.ingest_health import parquet_health, report_to_display

    # Prefer the report PUSHED by the Hermes reporter cron; fall back to a live
    # local read (works when the dashboard is co-located with the bars store).
    row = db.query(IngestHealth).order_by(IngestHealth.received_at.desc()).first()
    ingest = report_to_display(row.report, row.received_at) if (row and row.report) else parquet_health()

    return templates.TemplateResponse(
        request,
        "finviz.html",
        {
            "user": user,
            "filters": filters,
            "can_edit": user.can_moderate,
            "intervals": list(RUN_INTERVALS.keys()),
            "ingest": ingest,
        },
    )


@router.post("/filters/{fid}/interval")
def set_interval(
    fid: int,
    interval: str = Form(...),
    mod: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Set a filter's scheduled-run cadence (the agent's poll cron runs it when
    due). Setting an interval makes it due immediately (next_run_at cleared)."""
    f = db.get(FinvizFilter, fid)
    if f is not None and interval in RUN_INTERVALS:
        f.run_interval = interval
        f.next_run_at = None  # due now (then advances on the next completed run)
        db.commit()
    return RedirectResponse(url="/finviz", status_code=303)


@router.post("/filters")
def add_filter(
    request: Request,
    description: str = Form(...),
    url: str = Form(...),
    is_active: str | None = Form(None),  # checkbox: present only when checked
    mod: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    desc, link = description.strip(), url.strip()
    if desc and link:
        db.add(
            FinvizFilter(
                description=desc,
                url=link,
                is_active=is_active is not None,
                created_by=mod.id,
            )
        )
        db.commit()
    return RedirectResponse(url="/finviz", status_code=303)


@router.post("/filters/{fid}/toggle")
def toggle_filter(fid: int, mod: User = Depends(require_moderator), db: Session = Depends(get_db)):
    f = db.get(FinvizFilter, fid)
    if f:
        f.is_active = not f.is_active
        db.commit()
    return RedirectResponse(url="/finviz", status_code=303)


@router.post("/filters/{fid}/delete")
def delete_filter(fid: int, mod: User = Depends(require_moderator), db: Session = Depends(get_db)):
    f = db.get(FinvizFilter, fid)
    if f:
        db.delete(f)
        db.commit()
    return RedirectResponse(url="/finviz", status_code=303)
