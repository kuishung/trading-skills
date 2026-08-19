"""Calendar — the Economic calendar and the Earnings calendar.

Two sibling pages under one nav group (app/menus.py "Calendar"):

  * ``/calendar/economic`` — scheduled macro releases: time, country, importance,
    actual vs forecast vs previous.
  * ``/calendar/earnings`` — who reports on a given US market day, biggest first,
    with the consensus EPS and last year's print for comparison. Rows carry the
    My-Watchlist star, so a name you spot here is one click from your watchlist.

Both pages are the same shell (``calendar.html``) with a different panel
fragment, and both drive off the same three controls: the anchor DAY, the RANGE
and the page's own filters. RANGE is month (the default — a wall-calendar grid
with today ringed, each cell drilling into its day), week, or a single day. Every
control is a plain HTMX button that re-requests the panel — no client state, so a
reload or a shared URL always reproduces what you were looking at.

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


RANGES = [("day", "Day"), ("week", "Week"), ("month", "Month")]
_RANGE_KEYS = {k for k, _ in RANGES}


def _norm_range(rng: str) -> str:
    return rng if rng in _RANGE_KEYS else "month"


def _month_bounds(anchor: _dt.date) -> tuple[_dt.date, _dt.date]:
    first = anchor.replace(day=1)
    last = (first + _dt.timedelta(days=32)).replace(day=1) - _dt.timedelta(days=1)
    return first, last


def _span(anchor: _dt.date, rng: str) -> tuple[_dt.date, _dt.date]:
    """The days a panel covers. `week` = the anchor's Mon–Sun and `month` = its
    calendar month, so paging is predictable (a rolling 7- or 30-day window makes
    "last week" / "last month" ambiguous)."""
    if rng == "month":
        return _month_bounds(anchor)
    if rng == "week":
        start = anchor - _dt.timedelta(days=anchor.weekday())
        return start, start + _dt.timedelta(days=6)
    return anchor, anchor


def _step(anchor: _dt.date, rng: str, direction: int) -> _dt.date:
    """Prev / next. Month steps go through the 1st so a 31st never skids into the
    wrong month (31 Mar + 1 month has to be April, not May)."""
    if rng == "month":
        first = anchor.replace(day=1)
        return ((first + _dt.timedelta(days=32)).replace(day=1) if direction > 0
                else (first - _dt.timedelta(days=1)).replace(day=1))
    return anchor + _dt.timedelta(days=(7 if rng == "week" else 1) * direction)


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
        {"user": user, "tab": "month", "panel_url": f"/calendar/month/panel?{urlencode(q)}"},
    )


@router.get("/economic", response_class=HTMLResponse)
def economic_page(
    request: Request,
    day: str | None = None,
    rng: str = Query("month", alias="range"),
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
    rng: str = Query("month", alias="range"),
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
        "user": user, "tab": "month",
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
        "prev_day": _step(anchor, "month", -1).isoformat(),
        "next_day": _step(anchor, "month", +1).isoformat(),
        "params": params,
        **_urls("/calendar/month/panel", params),
    }
    return templates.TemplateResponse(request, "_cal_month.html", ctx)


@router.get("/economic/panel", response_class=HTMLResponse)
def economic_panel(
    request: Request,
    day: str | None = None,
    rng: str = Query("month", alias="range"),
    countries: str = "US",
    imp: str = "med",
    user: User = Depends(require_user),
):
    """Controls + the releases: a month grid, or a day-by-day table for the
    narrower ranges. Returned whole (controls included) so every control can
    simply re-request this endpoint."""
    anchor = _day(day)
    rng = _norm_range(rng)
    imp = imp if imp in _IMP_MIN else "med"
    picked = [c.strip().upper() for c in (countries or "").split(",") if c.strip()] or ["US"]
    start, end = _span(anchor, rng)

    try:
        events = cal.economic_events(start, end, picked, _IMP_MIN[imp])
        failed = False
    except Exception:  # noqa: BLE001 — defensive; the service already soft-fails
        events, failed = [], True

    # Group by market date, preserving the service's time ordering.
    by_date: dict[_dt.date, list[dict]] = {}
    days: list[tuple[_dt.date, list[dict]]] = []
    for e in events:
        if not days or days[-1][0] != e["date"]:
            days.append((e["date"], []))
        days[-1][1].append(e)
        by_date.setdefault(e["date"], []).append(e)
    # A month cell fits ~3 lines, so it shows the loudest releases first rather
    # than the earliest — the 08:30 CPI matters more than the 07:00 mortgage rate.
    cell = {d: sorted(rows, key=lambda e: (-e["importance"], e["when"]))
            for d, rows in by_date.items()}

    params = {"day": anchor.isoformat(), "range": rng,
              "countries": ",".join(picked), "imp": imp}
    ctx = {
        "user": user, "tab": "economic",
        "days": days, "by_date": cell, "failed": failed,
        "weeks": _weeks(start, end) if rng == "month" else [],
        "anchor": anchor, "start": start, "end": end, "rng": rng,
        "today": cal.today_et(),
        "picked": picked, "imp": imp,
        "countries_all": cal.COUNTRIES,
        "importance_filters": IMPORTANCE_FILTERS,
        "ranges": RANGES,
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
    rng: str = Query("month", alias="range"),
    mine: int = 0,
    cap: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Controls + the earnings roster: a month grid, or a day-by-day table."""
    anchor = _day(day)
    rng = _norm_range(rng)
    cap = cap if cap in _CAP_MIN else "all"
    mine = int(bool(mine))
    start, end = _span(anchor, rng)
    dates = [start + _dt.timedelta(days=i) for i in range((end - start).days + 1)]
    # Nasdaq is one HTTP call PER DAY, so a month would be 31. US companies don't
    # report at the weekend, so multi-day ranges skip Sat/Sun — 31 calls become 22,
    # and the cells they'd have filled are empty either way.
    if rng != "day":
        dates = [d for d in dates if d.weekday() < 5]

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
    by_date: dict[_dt.date, list[dict]] = {}
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
        by_date[d] = keep
        if keep or rng == "day":
            days.append((d, keep))

    params = {"day": anchor.isoformat(), "range": rng, "mine": mine, "cap": cap}
    ctx = {
        "user": user, "tab": "earnings",
        "days": days, "by_date": by_date, "failed": failed,
        "weeks": _weeks(start, end) if rng == "month" else [],
        "ranges": RANGES,
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
