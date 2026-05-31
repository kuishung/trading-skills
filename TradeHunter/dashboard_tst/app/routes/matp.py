"""MATP board (Median Analyst Target Price) + per-ticker history.

GET /matp           -> current MATP/MBP per symbol (from MATPLevel)
GET /matp/{symbol}  -> how that symbol's MATP evolved (MATPHistory) + a chart

Data is pushed in by the Nous Hermes agent via /api/matp (this process runs no
LLM and does no scraping). Approved members only.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MATPHistory, MATPLevel, MATPTarget, User
from ..security import require_user

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
    levels = db.query(MATPLevel).order_by(MATPLevel.symbol).all()
    return templates.TemplateResponse(
        request, "matp.html", {"user": user, "levels": levels}
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
    return templates.TemplateResponse(
        request,
        "matp_detail.html",
        {
            "user": user, "symbol": sym, "level": level,
            "history": list(reversed(history)),  # newest-first table
            "chart": chart,
            "targets": targets,
            "earnings": earn,
        },
    )
