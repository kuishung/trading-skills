"""My Portfolio (placeholder).

Where the user's Alpaca account view will live — positions, open orders, P&L.
Stubbed as an under-construction page so the new nav (My Portfolio) has a
destination; content lands later (reads from the execution/Alpaca layer).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def portfolio_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request, "strategy.html",   # reuse the under-construction shell
        {"user": user, "title": "My Portfolio",
         "blurb": "Your Alpaca account view — open positions, working orders, and "
                  "P&L — will appear here, sourced from the execution layer."},
    )
