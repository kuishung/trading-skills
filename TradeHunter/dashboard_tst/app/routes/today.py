"""Today Overview — the post-login landing page for all approved users.

A market-highlights board: news, economic calendar, market sentiment, company
news, ETF rotation (RRG), ETF leaders, and a correlation matrix. Each card
lazy-loads its own endpoint (added phase by phase) so a slow source never blocks
the page. All data is fetched LIVE (Yahoo / httpx) — this is a "now" view, never
parquet (see CLAUDE.md).

Phase 0: page scaffold + the embedded economic-calendar card. The remaining cards
are styled placeholders whose lazy endpoints land in later phases.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MATPLevel, User
from ..security import require_user

router = APIRouter(prefix="/today", tags=["today"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def today_home(request: Request, user: User = Depends(require_user)):
    """The Today Overview landing — a responsive grid of market-highlight cards.
    Each data card lazy-loads its own endpoint below so a slow source never blocks
    the page."""
    return templates.TemplateResponse(request, "today.html", {"user": user})


# --- lazy card fragments (HTMX-loaded; all live-fetched, cached, soft-fail) ---

@router.get("/sentiment", response_class=HTMLResponse)
def today_sentiment(request: Request, user: User = Depends(require_user)):
    """Market sentiment card: CNN Fear & Greed gauge + the VIX level."""
    from ..services.market import fear_greed
    from ..services.prices import fetch_quote

    return templates.TemplateResponse(
        request, "_today_sentiment.html",
        {"fg": fear_greed(), "vix": fetch_quote("^VIX")},
    )


@router.get("/news", response_class=HTMLResponse)
def today_news(request: Request, user: User = Depends(require_user)):
    """Broad-market news headlines (Yahoo RSS, ^GSPC)."""
    from ..services.market import market_news

    return templates.TemplateResponse(request, "_today_news.html", {"news": market_news()})


@router.get("/company-news", response_class=HTMLResponse)
def today_company_news(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Per-ticker headlines for the active watchlist (signals first), merged."""
    from ..services.market import company_news

    levels = (
        db.query(MATPLevel)
        .filter((MATPLevel.status == "active") | (MATPLevel.status.is_(None)))
        .all()
    )
    # signals (HOT/WARM/WATCHING) bubble up so the news is for the names in play
    rank = {"HOT": 0, "WARM": 1, "WATCHING": 2}
    levels.sort(key=lambda lv: (rank.get((lv.signal or "").upper(), 3), lv.symbol))
    syms = [lv.symbol for lv in levels]
    return templates.TemplateResponse(
        request, "_today_company_news.html", {"news": company_news(syms)},
    )
