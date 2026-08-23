"""Options tab — chain + Greeks for the selected watchlist ticker, from TWS.

One HTMX endpoint returning a fragment, because the tab is swapped into the
watchlist's right-hand pane rather than being its own page.

The service never raises on a dead TWS, so this route always renders: either the
chain, or a panel explaining exactly what to switch on.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user
from ..services import ibkr_options

router = APIRouter(prefix="/options", tags=["options"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("/{symbol}", response_class=HTMLResponse)
def options_tab(symbol: str, request: Request,
                exp: str | None = None,
                user: User = Depends(require_user)):
    """Chain fragment for `symbol` (optionally a specific expiry)."""
    sym = (symbol or "").strip().upper()
    chain = ibkr_options.get_chain(sym, exp)
    # request-first signature, matching the rest of the app (current Starlette)
    return templates.TemplateResponse(
        request,
        "_options_tab.html",
        {"user": user, "sym": sym, "chain": chain},
    )
