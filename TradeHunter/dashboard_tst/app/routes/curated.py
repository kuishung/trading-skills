"""Curated — each member's own dated trade calls, judged automatically.

A member records a ticker with the date they called it and three levels (entry,
stop, target). This page then answers, from daily bars, whether the entry was ever
reached, whether the idea is still running, and what it made — grouped by the month
it was curated in, so a month's calls can be read as a set.

Replaces the Portfolio placeholder (2026-09-07, user: "Change the Portfolio to
Curated"). The list is per-user: `services.curated` scopes every query by user_id.

The list is a lazy HTMX fragment because rendering it fetches daily bars for every
distinct symbol. Those fetches are cached and run in parallel, but a cold page with
thirty tickers should show its shell immediately rather than block on Yahoo.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import require_user
from ..services import curated as cur

router = APIRouter(prefix="/curated", tags=["curated"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _list_context(db: Session, user: User, *, year: str = "", month: str = "",
                  msg: str = "", err: str = "") -> dict:
    """Everything the list fragment needs. Shared verbatim by the GET and every
    POST, so a form post re-renders exactly what a refresh would show.

    Filtered to one YEAR, and optionally one month of it (the Jan-Dec tabs). Only
    the selected slice is evaluated: each row costs a daily-bar fetch, so pricing a
    whole year to render one month would be work thrown away.

    The totals strip describes what is ON SCREEN. A win rate that silently covered
    rows the reader cannot see would change every time they switched tab, for no
    visible reason.
    """
    items = cur.list_for(db, user)
    cal = cur.calendar_index(items)
    years = sorted(cal, reverse=True)

    try:
        y = int(year)
    except (TypeError, ValueError):
        y = years[0] if years else _dt.date.today().year
    if years and y not in years:
        y = years[0]
    try:
        m = int(month)
        m = m if 1 <= m <= 12 else 0
    except (TypeError, ValueError):
        m = 0

    sel = [i for i in items if (i.get("curated_on") or "")[:4] == str(y)]
    if m:
        sel = [i for i in sel if (i.get("curated_on") or "")[5:7] == "%02d" % m]

    months = cur.by_month(cur.rows_for(sel))
    return {"user": user, "months": months, "totals": cur.overall(months),
            "msg": msg, "err": err, "today": _dt.date.today().isoformat(),
            "years": years, "sel_year": y, "sel_month": m,
            "counts": cal.get(y) or [0] * 12,
            "month_abbr": cur.MONTH_ABBR, "any_rows": bool(items)}


@router.get("", response_class=HTMLResponse)
def curated_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request, "curated.html",
        {"user": user, "today": _dt.date.today().isoformat()},
    )


@router.get("/list", response_class=HTMLResponse)
def curated_list(request: Request, year: str = "", month: str = "",
                 user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "_curated_list.html",
        _list_context(db, user, year=year, month=month))


@router.get("/{row_id}/revisions", response_class=HTMLResponse)
def curated_revisions(request: Request, row_id: int,
                      user: User = Depends(require_user),
                      db: Session = Depends(get_db)):
    """The edit history of one call. Clicking a revision replays THOSE levels on the
    chart, which is why each one carries its own entry/stop/target rather than a
    diff against the current values."""
    row = cur.get_one(db, user, row_id)
    if row is None:
        return HTMLResponse('<p class="text-xs text-slate-500 p-2">Not found.</p>')
    return templates.TemplateResponse(
        request, "_curated_revisions.html",
        {"user": user, "row": row, "revisions": cur.revisions_for(db, user, row_id)})


@router.get("/chart", response_class=HTMLResponse)
def curated_chart(request: Request, rev: int = 0,
                  user: User = Depends(require_user),
                  db: Session = Depends(get_db)):
    """One revision replayed on a chart — its own entry/stop/target as read-only
    price lines. Declared before the /{row_id}/... routes so "chart" is never read
    as a row id."""
    from ..models import MATPLevel

    hit = cur.get_revision(db, user, rev)
    if hit is None:
        return HTMLResponse(
            '<div class="flex-1 flex items-center justify-center text-xs '
            'text-slate-500">Revision not found.</div>')
    row, revision = hit

    # Same MATP/analyst context the Watchlist and Sector charts use, so a curated
    # ticker that is on the board keeps its MATP/MBP lines and analyst band here.
    from .matp import _chart_context

    sel = db.query(MATPLevel).filter(MATPLevel.symbol == revision["symbol"]).first()
    cc = _chart_context(db, sel)
    return templates.TemplateResponse(request, "_curated_chart.html", {
        "user": user, "symbol": revision["symbol"], "sel": sel,
        "sel_band": cc["sel_band"], "sel_patterns": cc["sel_patterns"],
        "row": row, "rev": revision,
        "levels": {"entry": revision["entry"], "stop": revision["stop"],
                   "target": revision["target"],
                   "label": "" if revision["current"] else "#%s" % revision["n"]},
    })


@router.post("/from-chart")
def curated_from_chart(
    symbol: str = Form(...),
    entry: str = Form(...),
    stop: str = Form(...),
    target: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Curate the trade setup drawn on a chart. Returns JSON — the caller is the
    chart's own chip, which stays on the page and just reports what happened.

    Dated TODAY, deliberately: this is a call being made now. Back-dating it to
    where the setup was drawn would let a setup placed over old bars be judged
    against a move that had already happened.
    """
    today = _dt.date.today().isoformat()
    ok, message = cur.add(db, user, symbol=symbol, curated_on=today, entry=entry,
                          stop=stop, target=target, source="chart",
                          note="from chart trade setup")
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "symbol": (symbol or "").strip().upper(),
            "curated_on": today, "message": message}


@router.post("/add", response_class=HTMLResponse)
def curated_add(
    request: Request,
    symbol: str = Form(...),
    curated_on: str = Form(...),
    entry: str = Form(...),
    stop: str = Form(...),
    target: str = Form(...),
    note: str = Form(""),
    year: str = Form(""),
    month: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add one curated call and re-render the list.

    A rejected entry comes back as a message ON the page rather than a 4xx — the
    form is inside the fragment being swapped, so an error status would leave the
    member looking at an unchanged page with no explanation.
    """
    ok, message = cur.add(db, user, symbol=symbol, curated_on=curated_on,
                          entry=entry, stop=stop, target=target, note=note)
    # Land on the tab the new call belongs to, not the one that happened to be open:
    # adding a January call while looking at March would otherwise do nothing visible.
    y, m = year, month
    if ok and len(curated_on or "") >= 7:
        y, m = curated_on[:4], curated_on[5:7]
    ctx = _list_context(db, user, year=y, month=m,
                        msg=message if ok else "", err="" if ok else message)
    return templates.TemplateResponse(request, "_curated_list.html", ctx)


@router.post("/{row_id}/edit", response_class=HTMLResponse)
def curated_edit(
    request: Request,
    row_id: int,
    symbol: str = Form(""),
    entry: str = Form(""),
    stop: str = Form(""),
    target: str = Form(""),
    note: str = Form(""),
    year: str = Form(""),
    month: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Edit a call's levels. The curated DATE is deliberately not a field here — it
    anchors every trigger test, so it is fixed for the life of the call. Each real
    change is kept as a revision."""
    fields = {k: v for k, v in
              {"symbol": symbol, "entry": entry, "stop": stop,
               "target": target, "note": note}.items() if v != ""}
    ok, message = cur.update(db, user, row_id, **fields)
    ctx = _list_context(db, user, year=year, month=month,
                        msg=message if ok else "", err="" if ok else message)
    return templates.TemplateResponse(request, "_curated_list.html", ctx)


@router.post("/{row_id}/delete", response_class=HTMLResponse)
def curated_delete(request: Request, row_id: int,
                   year: str = Form(""), month: str = Form(""),
                   user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    gone = cur.remove(db, user, row_id)
    ctx = _list_context(db, user, year=year, month=month,
                        msg="Removed." if gone else "")
    return templates.TemplateResponse(request, "_curated_list.html", ctx)
