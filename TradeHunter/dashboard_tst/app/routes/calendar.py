"""Calendar — the Economic calendar and the Earnings calendar.

Two sibling pages under one nav group (app/menus.py "Calendar"):

  * ``/calendar/economic`` — scheduled macro releases: time, country, importance,
    actual vs forecast vs previous.
  * ``/calendar/earnings`` — who reports on a given US market day, biggest first,
    with the consensus EPS and last year's print for comparison. Rows carry the
    My-Watchlist star, so a name you spot here is one click from your watchlist.

Both pages are the same shell (``calendar.html``) with a different panel
fragment, and both drive off the same three controls: the anchor DAY, the RANGE
(one day / the whole week) and the page's own filters. Every control is a plain
HTMX link that re-requests the panel — no client state, so a reload or a shared
URL always reproduces what you were looking at.

Data comes from ``services/calendars.py`` (live HTTP, cached, soft-fail). Dates
are US MARKET dates throughout: the reader is in Malaysia, where the US session
straddles local midnight, so "Thursday's calendar" has to mean Thursday in New
York or nothing lines up. Clock times still render in the viewer's timezone via
the shared ``localtime`` macro.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import require_user
from ..services import calendars as cal

router = APIRouter(prefix="/calendar", tags=["calendar"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# Importance filter: label -> the minimum TradingView importance it lets through.
IMPORTANCE_FILTERS = [("high", "High only", 1), ("med", "Medium+", 0), ("all", "All", -1)]
_IMP_MIN = {k: v for k, _, v in IMPORTANCE_FILTERS}

# Market-cap filter for the earnings tab: label -> floor in dollars.
CAP_FILTERS = [("all", "All", 0.0), ("1b", "≥ $1B", 1e9), ("10b", "≥ $10B", 1e10),
               ("100b", "≥ $100B", 1e11)]
_CAP_MIN = {k: v for k, _, v in CAP_FILTERS}


def _day(day: str | None) -> _dt.date:
    """Parse the anchor date, defaulting to today in New York."""
    if day:
        try:
            return _dt.date.fromisoformat(day.strip())
        except ValueError:
            pass
    return cal.today_et()


def _span(anchor: _dt.date, rng: str) -> tuple[_dt.date, _dt.date]:
    """The days a panel covers. `week` = the anchor's Mon–Sun, so paging is
    predictable (a rolling 7-day window makes "last week" ambiguous)."""
    if rng == "week":
        start = anchor - _dt.timedelta(days=anchor.weekday())
        return start, start + _dt.timedelta(days=6)
    return anchor, anchor


def _step(anchor: _dt.date, rng: str, direction: int) -> _dt.date:
    return anchor + _dt.timedelta(days=(7 if rng == "week" else 1) * direction)


def _urls(base: str, params: dict) -> dict:
    """Every control on the panel as a ready-made URL, so the template does no
    query-string arithmetic."""
    def url(**over):
        q = {**params, **over}
        return f"{base}?{urlencode(q)}"
    return {"url": url}


# ─────────────────────────────── pages ───────────────────────────────
@router.get("", response_class=HTMLResponse)
def calendar_home(user: User = Depends(require_user)):
    """Bare /calendar lands on the economic tab."""
    return RedirectResponse("/calendar/economic", status_code=303)


@router.get("/economic", response_class=HTMLResponse)
def economic_page(
    request: Request,
    day: str | None = None,
    rng: str = Query("day", alias="range"),
    countries: str = "US",
    imp: str = "med",
    user: User = Depends(require_user),
):
    """Economic calendar shell. The panel lazy-loads so a slow source never
    blocks the page render."""
    q = {"day": _day(day).isoformat(), "range": rng, "countries": countries, "imp": imp}
    return templates.TemplateResponse(
        request, "calendar.html",
        {"user": user, "tab": "economic", "panel_url": f"/calendar/economic/panel?{urlencode(q)}"},
    )


@router.get("/earnings", response_class=HTMLResponse)
def earnings_page(
    request: Request,
    day: str | None = None,
    rng: str = Query("day", alias="range"),
    mine: int = 0,
    cap: str = "all",
    user: User = Depends(require_user),
):
    """Earnings calendar shell."""
    q = {"day": _day(day).isoformat(), "range": rng, "mine": int(bool(mine)), "cap": cap}
    return templates.TemplateResponse(
        request, "calendar.html",
        {"user": user, "tab": "earnings", "panel_url": f"/calendar/earnings/panel?{urlencode(q)}"},
    )


# ───────────────────────── panels (HTMX fragments) ─────────────────────────
@router.get("/economic/panel", response_class=HTMLResponse)
def economic_panel(
    request: Request,
    day: str | None = None,
    rng: str = Query("day", alias="range"),
    countries: str = "US",
    imp: str = "med",
    user: User = Depends(require_user),
):
    """Controls + the grouped release table. Returned whole (controls included)
    so every control can simply re-request this endpoint."""
    anchor = _day(day)
    rng = "week" if rng == "week" else "day"
    imp = imp if imp in _IMP_MIN else "med"
    picked = [c.strip().upper() for c in (countries or "").split(",") if c.strip()] or ["US"]
    start, end = _span(anchor, rng)

    try:
        events = cal.economic_events(start, end, picked, _IMP_MIN[imp])
        failed = False
    except Exception:  # noqa: BLE001 — defensive; the service already soft-fails
        events, failed = [], True

    # Group by market date, preserving the service's time ordering.
    days: list[tuple[_dt.date, list[dict]]] = []
    for e in events:
        if not days or days[-1][0] != e["date"]:
            days.append((e["date"], []))
        days[-1][1].append(e)

    params = {"day": anchor.isoformat(), "range": rng,
              "countries": ",".join(picked), "imp": imp}
    ctx = {
        "user": user, "tab": "economic",
        "days": days, "failed": failed,
        "anchor": anchor, "start": start, "end": end, "rng": rng,
        "today": cal.today_et(),
        "picked": picked, "imp": imp,
        "countries_all": cal.COUNTRIES,
        "importance_filters": IMPORTANCE_FILTERS,
        "n_high": sum(1 for e in events if e["importance"] == 1),
        "n_total": len(events),
        "prev_day": _step(anchor, rng, -1).isoformat(),
        "next_day": _step(anchor, rng, +1).isoformat(),
        "params": params,
        "base": "/calendar/economic/panel",
        **_urls("/calendar/economic/panel", params),
    }
    return templates.TemplateResponse(request, "_cal_economic.html", ctx)


@router.get("/earnings/panel", response_class=HTMLResponse)
def earnings_panel(
    request: Request,
    day: str | None = None,
    rng: str = Query("day", alias="range"),
    mine: int = 0,
    cap: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Controls + the day-by-day earnings roster."""
    anchor = _day(day)
    rng = "week" if rng == "week" else "day"
    cap = cap if cap in _CAP_MIN else "all"
    mine = int(bool(mine))
    start, end = _span(anchor, rng)
    dates = [start + _dt.timedelta(days=i) for i in range((end - start).days + 1)]

    # My Watchlist drives both the star state and the "mine only" filter.
    try:
        from ..services.user_watchlist import symbol_set

        my_syms = symbol_set(db, user)
    except Exception:  # noqa: BLE001
        my_syms = set()

    try:
        fetched = cal.earnings_range(dates)
        failed = False
    except Exception:  # noqa: BLE001
        fetched, failed = [], True

    floor = _CAP_MIN[cap]
    days: list[tuple[_dt.date, list[dict]]] = []
    n_total = n_mine = 0
    for d, rows in fetched:
        keep = []
        for r in rows:
            if mine and r["symbol"] not in my_syms:
                continue
            if floor and (r["market_cap"] or 0) < floor:
                continue
            keep.append(r)
            if r["symbol"] in my_syms:
                n_mine += 1
        n_total += len(keep)
        if keep or rng == "day":
            days.append((d, keep))

    params = {"day": anchor.isoformat(), "range": rng, "mine": mine, "cap": cap}
    ctx = {
        "user": user, "tab": "earnings",
        "days": days, "failed": failed,
        "anchor": anchor, "start": start, "end": end, "rng": rng,
        "today": cal.today_et(),
        "mine": mine, "cap": cap, "cap_filters": CAP_FILTERS,
        "my_syms": my_syms, "n_total": n_total, "n_mine": n_mine,
        "prev_day": _step(anchor, rng, -1).isoformat(),
        "next_day": _step(anchor, rng, +1).isoformat(),
        "params": params,
        "base": "/calendar/earnings/panel",
        **_urls("/calendar/earnings/panel", params),
    }
    return templates.TemplateResponse(request, "_cal_earnings.html", ctx)
