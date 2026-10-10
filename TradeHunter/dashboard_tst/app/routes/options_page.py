"""The Options page - BLANK since v4.135 (2026-10-10), waiting for its rebuild.

v4.135 removed the whole v2 page (status strip, basket, strategy dropdown, rules panel,
trade list, legs and payoff chart) together with the screener behind it
(``opt_rules``, ``opt_screen``, ``payoff``, ``opt_legs``). What stays is the Massive
data pipeline only: the Hermes collector (TST-Options-Collector,
``services/opt_collector`` + ``deploy/options_collector.py``) reading Massive through
``services/massive`` and ``services/opt_massive`` into the ``services/opt_store`` tables.
The collector's universe is still the ``option_basket`` table, so the tickers already in
members' baskets keep being collected while the page is blank.

This router is included BEFORE ``routes/options.py`` in ``main.py`` so ``/options``
wins over that module's ``/options/{symbol}`` catch-all, and it carries the
``require_menu("options")`` gate there (a member without the grant is redirected).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..security import require_user

router = APIRouter(prefix="/options", tags=["options-page"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
def options_home(request: Request, user: User = Depends(require_user)):
    """The blank page: the menu entry stays, the content is rebuilt later."""
    return templates.TemplateResponse(request, "options.html", {"user": user})
