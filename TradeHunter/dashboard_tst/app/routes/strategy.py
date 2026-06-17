"""Intraday — Strategy (placeholder).

The intraday strategy surface: where the Finviz-screen -> tradeable-pattern ->
Alpaca-bracket pipeline will live. Stubbed as an under-construction page so the
new nav (Intraday > Strategy) has a destination; content lands later.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user

router = APIRouter(prefix="/strategy", tags=["strategy"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def strategy_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request, "strategy.html",
        {"user": user, "title": "Strategy",
         "blurb": "The intraday strategy pipeline lives here: a Finviz Elite screen "
                  "surfaces tradeable patterns, TradeHunter computes the levels "
                  "(entry / stop / target), and Alpaca executes bracket orders."},
    )
