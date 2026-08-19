"""Calendar — the month view: economic releases and earnings on ONE wall calendar.

  * ``/calendar/month`` — a Mon-first month grid where every day carries both
    feeds, colour-coded (economic = sky, earnings = amber), with a detail panel
    for the selected day (today by default). Rows in the detail carry the
    My-Watchlist star, so a name you spot here is one click from your watchlist.

The standalone Economic and Earnings pages were removed 2026-08-20 (user): the
month view already answers the question they answered, and the day panel gives
the full release/reporter detail the tabs used to. Their filters survive as the
month view's controls (importance, market cap, "mine only", countries).

Every control is a plain HTMX button that re-requests the panel — no client
state, so a reload or a shared URL always reproduces what you were looking at.

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

from fastapi import APIRouter, Depends, Request
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

# Market-cap filter for the earnings feed: label -> floor in dollars.
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


def _month_bounds(anchor: _dt.date) -> tuple[_dt.date, _dt.date]:
    first = anchor.replace(day=1)
    last = (first + _dt.timedelta(days=32)).replace(day=1) - _dt.timedelta(days=1)
    return first, last


def _step(anchor: _dt.date, direction: int) -> _dt.date:
    """Prev / next month. Steps go through the 1st so a 31st never skids into the
    wrong month (31 Mar + 1 month has to be April, not May)."""
    first = anchor.replace(day=1)
    return ((first + _dt.timedelta(days=32)).replace(day=1) if direction > 0
            else (first - _dt.timedelta(days=1)).replace(day=1))


def _weeks(start: _dt.date, end: _dt.date) -> list[list[_dt.date]]:
    """The month grid: whole Mon–Sun rows covering [start, end]. Leading/trailing
    days belong to the neighbouring months and render dimmed."""
    first = start - _dt.timedelta(days=start.weekday())
    last = end + _dt.timedelta(days=6 - end.weekday())
    out, cur = [], first
    while cur <= last:
        out.append([cur + _dt.timedelta(days=i) for i in range(7)])
        cur += _dt.timedelta(days=7)
    return out


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
    """Bare /calendar lands on the combined month view."""
    return RedirectResponse("/calendar/month", status_code=303)


@router.get("/month", response_class=HTMLResponse)
def month_page(
    request: Request,
    day: str | None = None,
    sel: str | None = None,
    countries: str = "US",
    imp: str = "med",
    cap: str = "all",
    mine: int = 0,
    user: User = Depends(require_user),
):
    """The month view: ONE wall calendar carrying both feeds, colour-coded, with a
    detail panel for the selected day (today by default)."""
    q = {"day": _day(day).isoformat(), "sel": (sel or ""), "countries": countries,
         "imp": imp, "cap": cap, "mine": int(bool(mine))}
    return templates.TemplateResponse(
        request, "calendar.html",
        {"user": user, "panel_url": f"/calendar/month/panel?{urlencode(q)}"},
    )


# ───────────────────────── panels (HTMX fragments) ─────────────────────────
def _econ_for(start, end, picked, imp) -> tuple[dict, bool]:
    """{market date -> that day's releases, loudest first}, plus a failure flag."""
    try:
        events = cal.economic_events(start, end, picked, _IMP_MIN[imp])
    except Exception:  # noqa: BLE001 — defensive; the service already soft-fails
        return {}, True
    out: dict[_dt.date, list[dict]] = {}
    for e in events:
        out.setdefault(e["date"], []).append(e)
    # A cell fits ~3 lines, so show the loudest first rather than the earliest —
    # the 08:30 CPI matters more than the 07:00 mortgage-rate print.
    return ({d: sorted(rows, key=lambda e: (-e["importance"], e["when"]))
             for d, rows in out.items()}, False)


def _earn_for(dates, floor, mine, my_syms) -> tuple[dict, bool]:
    """{market date -> that day's reporters, biggest first}, plus a failure flag."""
    try:
        fetched = cal.earnings_range(dates)
    except Exception:  # noqa: BLE001
        return {}, True
    out: dict[_dt.date, list[dict]] = {}
    for d, rows in fetched:
        out[d] = [r for r in rows
                  if not (mine and r["symbol"] not in my_syms)
                  and not (floor and (r["market_cap"] or 0) < floor)]
    return out, False


def _my_symbols(db: Session, user: User) -> set:
    try:
        from ..services.user_watchlist import symbol_set

        return symbol_set(db, user)
    except Exception:  # noqa: BLE001
        return set()


@router.get("/month/panel", response_class=HTMLResponse)
def month_panel(
    request: Request,
    day: str | None = None,
    sel: str | None = None,
    countries: str = "US",
    imp: str = "med",
    cap: str = "all",
    mine: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The combined month grid + the selected day's detail panel.

    Both feeds land in ONE calendar, colour-coded (economic = sky, earnings =
    amber), because that's the question the month view answers: what is happening
    to the market on each day — not "what does source X have".
    """
    anchor = _day(day)
    imp = imp if imp in _IMP_MIN else "med"
    cap = cap if cap in _CAP_MIN else "all"
    mine = int(bool(mine))
    picked = [c.strip().upper() for c in (countries or "").split(",") if c.strip()] or ["US"]
    start, end = _month_bounds(anchor)
    today = cal.today_et()

    # The detail panel defaults to TODAY, and to the 1st when today is in another
    # month — a panel about a day you can't see on the grid would be a puzzle.
    sel_d = _day(sel) if sel else today
    if not (start <= sel_d <= end):
        sel_d = today if start <= today <= end else start

    my_syms = _my_symbols(db, user)
    econ, econ_failed = _econ_for(start, end, picked, imp)
    # Nasdaq is one HTTP call per day; US companies don't report at the weekend,
    # so the month asks for weekdays only — 31 calls become 22 and the cells that
    # would have been filled are empty anyway.
    weekdays = [start + _dt.timedelta(days=i) for i in range((end - start).days + 1)]
    weekdays = [d for d in weekdays if d.weekday() < 5]
    earn, earn_failed = _earn_for(weekdays, _CAP_MIN[cap], mine, my_syms)

    by_date = {d: {"econ": econ.get(d, []), "earn": earn.get(d, [])}
               for d in {*econ, *earn}}
    counts = {d: len(v["econ"]) + len(v["earn"]) for d, v in by_date.items()}

    params = {"day": anchor.isoformat(), "sel": sel_d.isoformat(),
              "countries": ",".join(picked), "imp": imp, "cap": cap, "mine": mine}
    _url = _urls("/calendar/month/panel", params)["url"]
    ctx = {
        # clicking any day SELECTS it (the detail panel follows); it does not
        # navigate away, so the grid stays put while you read the day.
        "cell_href": lambda d: _url(sel=d.isoformat()),
        "user": user,
        "anchor": anchor, "start": start, "end": end, "today": today, "sel": sel_d,
        "weeks": _weeks(start, end), "by_date": by_date, "counts": counts,
        "sel_econ": econ.get(sel_d, []), "sel_earn": earn.get(sel_d, []),
        "failed": econ_failed and earn_failed,
        "econ_failed": econ_failed, "earn_failed": earn_failed,
        "picked": picked, "imp": imp, "cap": cap, "mine": mine,
        "countries_all": cal.COUNTRIES,
        "importance_filters": IMPORTANCE_FILTERS, "cap_filters": CAP_FILTERS,
        "my_syms": my_syms,
        "n_econ": sum(len(v["econ"]) for v in by_date.values()),
        "n_earn": sum(len(v["earn"]) for v in by_date.values()),
        "prev_day": _step(anchor, -1).isoformat(),
        "next_day": _step(anchor, +1).isoformat(),
        "params": params,
        **_urls("/calendar/month/panel", params),
    }
    return templates.TemplateResponse(request, "_cal_month.html", ctx)
