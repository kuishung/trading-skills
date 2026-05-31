"""MATP board (Median Analyst Target Price) + per-ticker history.

GET /matp           -> current MATP/MBP per symbol (from MATPLevel)
GET /matp/{symbol}  -> how that symbol's MATP evolved (MATPHistory) + a chart

Data is pushed in by the Nous Hermes agent via /api/matp (this process runs no
LLM and does no scraping). Approved members only.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    FinvizFilter,
    MATPHistory,
    MATPLevel,
    MATPRefreshRequest,
    MATPTarget,
    User,
)
from ..security import require_moderator, require_user

# request states that mean "the agent hasn't finished this yet"
_OPEN_STATES = ("pending", "running")

# Board sort: actionable bounce signals float to the top.
_SIGNAL_RANK = {"HOT": 0, "WARM": 1, "WATCHING": 2}


def _signal_key(lv):
    return (_SIGNAL_RANK.get((lv.signal or "").upper(), 3), lv.symbol)

router = APIRouter(prefix="/matp", tags=["matp"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def matp_home(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    all_levels = db.query(MATPLevel).all()
    active = sorted(
        [lv for lv in all_levels if (lv.status or "active") == "active"],
        key=_signal_key,
    )
    dropped = sorted(
        [lv for lv in all_levels if (lv.status or "active") == "dropped"],
        key=lambda lv: lv.symbol,
    )
    filters = db.query(FinvizFilter).all()
    # filter id -> description, so the board can label which screen sourced a name
    filt_names = {f.id: f.description for f in filters}
    active_filters = [f for f in filters if f.is_active]

    # open (pending/running) refresh requests -> show status; suppress dup buttons
    open_reqs = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status.in_(_OPEN_STATES))
        .all()
    )
    open_symbols = {r.symbol for r in open_reqs if r.scope == "ticker" and r.symbol}
    open_filter_ids = {r.filter_id for r in open_reqs if r.scope == "filter"}

    # watchlist rail: group active tickers by their source filter (active order
    # carries through, so signals stay on top within each watchlist)
    by_filter: dict = {}
    for lv in active:
        by_filter.setdefault(lv.filter_id, []).append(lv)
    watchlists = [{"filter": f, "tickers": by_filter.get(f.id, [])} for f in active_filters]
    unfiled = by_filter.get(None, [])

    return templates.TemplateResponse(
        request,
        "matp.html",
        {
            "user": user,
            "active": active,
            "dropped": dropped,
            "filt_names": filt_names,
            "active_filters": active_filters,
            "open_reqs": open_reqs,
            "open_symbols": open_symbols,
            "open_filter_ids": open_filter_ids,
            "watchlists": watchlists,
            "unfiled": unfiled,
        },
    )


@router.get("/runs", response_class=HTMLResponse)
def matp_runs(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX-polled fragment: the live 'active runs' panel (pending + running),
    newest first, with progress + who triggered it."""
    runs = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status.in_(_OPEN_STATES))
        .order_by(MATPRefreshRequest.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "_runs_panel.html", {"user": user, "runs": runs}
    )


def _build_chart(points, width=600, height=170, pad=28):
    """points: list of (date_str, value) ascending by time. Returns an SVG-ready
    dict (polyline + area path + dots) or None if <2 points."""
    if len(points) < 2:
        return None
    vals = [v for _, v in points]
    vmin, vmax = min(vals), max(vals)
    span = (vmax - vmin) or 1.0
    n = len(points)
    iw, ih = width - 2 * pad, height - 2 * pad
    dots = []
    for i, (_, v) in enumerate(points):
        x = round(pad + iw * i / (n - 1), 1)
        y = round(pad + ih * (1 - (v - vmin) / span), 1)
        dots.append({"x": x, "y": y, "v": v})
    polyline = " ".join(f"{d['x']},{d['y']}" for d in dots)
    area = (
        f"M {dots[0]['x']},{height - pad} "
        + " ".join(f"L {d['x']},{d['y']}" for d in dots)
        + f" L {dots[-1]['x']},{height - pad} Z"
    )
    return {
        "width": width, "height": height, "pad": pad,
        "polyline": polyline, "area": area, "dots": dots,
        "vmin": vmin, "vmax": vmax,
        "first_date": points[0][0], "last_date": points[-1][0],
    }


def _build_band(low, high, mbp, matp):
    """Horizontal levels band: place MBP + MATP markers along the analyst
    low->high range as left% offsets. Returns None unless we have a real range."""
    if low is None or high is None or high <= low:
        return None

    def pct(v):
        if v is None:
            return None
        return round(max(0.0, min(100.0, (v - low) / (high - low) * 100.0)), 1)

    return {
        "low": low, "high": high,
        "mbp": mbp, "matp": matp,
        "mbp_pct": pct(mbp), "matp_pct": pct(matp),
    }


@router.get("/{symbol}/prices")
def matp_prices(symbol: str, user: User = Depends(require_user)):
    """Daily OHLC for the price chart (lightweight-charts shape), fetched LIVE
    from Yahoo (cached ~10 min). Returns an empty list (not an error) on any
    failure so the chart degrades to an empty state."""
    from ..services.prices import fetch_daily_ohlc

    return {"symbol": symbol.strip().upper(), "bars": fetch_daily_ohlc(symbol)}


@router.get("/{symbol}", response_class=HTMLResponse)
def matp_detail(
    symbol: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    sym = symbol.strip().upper()
    level = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    history = (
        db.query(MATPHistory)
        .filter(MATPHistory.symbol == sym)
        .order_by(MATPHistory.as_of.asc())
        .all()
    )
    points = [(h.as_of.strftime("%Y-%m-%d") if h.as_of else "", h.matp) for h in history]
    chart = _build_chart(points)

    # levels band: MBP/MATP against the analyst low->high range (latest snapshot)
    latest = history[-1] if history else None
    band = None
    if level and latest:
        band = _build_band(latest.target_low, latest.target_high, level.mbp, level.matp)

    # evidence: the individual analyst targets, newest issue date first.
    # 'included' (post-earnings) is computed here against the CURRENT earnings
    # date, so it never goes stale.
    earn = level.last_earnings_date if level else None
    rows = (
        db.query(MATPTarget)
        .filter(MATPTarget.symbol == sym)
        .order_by(MATPTarget.target_date.desc())
        .all()
    )
    targets = [
        {
            "brokerage": t.brokerage,
            "target_price": t.target_price,
            "target_date": t.target_date,
            "included": bool(earn and t.target_date and t.target_date > earn),
        }
        for t in rows
    ]
    # latest ad-hoc refresh request for this ticker (status banner)
    last_req = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.scope == "ticker", MATPRefreshRequest.symbol == sym)
        .order_by(MATPRefreshRequest.created_at.desc())
        .first()
    )
    return templates.TemplateResponse(
        request,
        "matp_detail.html",
        {
            "user": user, "symbol": sym, "level": level,
            "history": list(reversed(history)),  # newest-first table
            "chart": chart,
            "band": band,
            "targets": targets,
            "earnings": earn,
            "last_req": last_req,
            "req_open": bool(last_req and last_req.status in _OPEN_STATES),
        },
    )


# ---------------------------------------------------------------------------
# Ad-hoc refresh queue (moderators/admins) — enqueue only; the Nous Hermes
# agent polls /api/refresh-queue, does the work, and marks rows done.
# ---------------------------------------------------------------------------
def _enqueue(db: Session, scope: str, *, symbol=None, filter_id=None, user: User):
    """Create a pending request unless an identical one is already open."""
    q = db.query(MATPRefreshRequest).filter(
        MATPRefreshRequest.scope == scope,
        MATPRefreshRequest.status.in_(_OPEN_STATES),
    )
    q = q.filter(MATPRefreshRequest.symbol == symbol) if scope == "ticker" \
        else q.filter(MATPRefreshRequest.filter_id == filter_id)
    if q.first() is not None:
        return False  # already queued/running — don't duplicate
    db.add(
        MATPRefreshRequest(
            scope=scope, symbol=symbol, filter_id=filter_id,
            requested_by=user.id, status="pending",
        )
    )
    db.commit()
    return True


@router.post("/{symbol}/refresh")
def request_ticker_refresh(
    symbol: str,
    request: Request,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    sym = symbol.strip().upper()
    _enqueue(db, "ticker", symbol=sym, user=user)
    return RedirectResponse(url=f"/matp/{sym}", status_code=303)


@router.post("/filter/{filter_id}/refresh")
def request_filter_refresh(
    filter_id: int,
    request: Request,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    _enqueue(db, "filter", filter_id=filter_id, user=user)
    return RedirectResponse(url="/matp", status_code=303)


@router.post("/run-filter")
def run_filter_from_select(
    request: Request,
    filter_id: int = Form(...),
    next: str = Form("/matp"),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Filter-selector form (MATP board + admin console). Only active filters
    are runnable. Redirects back to the page that submitted (`next`, internal
    paths only)."""
    f = db.get(FinvizFilter, filter_id)
    if f is not None and f.is_active:
        _enqueue(db, "filter", filter_id=filter_id, user=user)
    dest = next if next.startswith("/") else "/matp"
    return RedirectResponse(url=dest, status_code=303)
