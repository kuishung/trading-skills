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
from ..services import trade_prefs as tp

router = APIRouter(prefix="/curated", tags=["curated"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _list_context(db: Session, user: User, *, year: str = "", month: str = "",
                  msg: str = "", err: str = "", row: str = "") -> dict:
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
    # No month asked for = first open of the page: land on THIS month, where today's
    # calls go (user, 2026-09-08: "by default when the curated is open, it will be the
    # default month selected"). "All" is a deliberate choice, so it says so with
    # month=0 rather than by omitting the parameter.
    if month is None or str(month).strip() == "":
        m = _dt.date.today().month
    else:
        try:
            m = int(month)
            m = m if 1 <= m <= 12 else 0
        except (TypeError, ValueError):
            m = 0

    sel = [i for i in items if (i.get("curated_on") or "")[:4] == str(y)]
    if m:
        sel = [i for i in sel if (i.get("curated_on") or "")[5:7] == "%02d" % m]

    months = cur.by_month(cur.rows_for(sel))

    # Position size is attached here rather than stored on the row: it depends on
    # the member's CURRENT account value and risk budget, so raising the account
    # re-sizes every idea at once. Same service the chart's trade-setup editor
    # sizes with, so the quantity on a drawing and on the call it became agree.
    prefs = tp.read(user)

    # Every call's history rides along as child rows under it, so the table can show
    # how a plan changed without a second request per call. Each revision carries its
    # OWN planned R:R and share count: the point of reading them side by side is
    # seeing what each version would have committed.
    hist = cur.revisions_for_many(
        db, user, [r["id"] for mo in months for r in mo["rows"]])
    for mo in months:
        for r in mo["rows"]:
            r["size"] = tp.size(r.get("entry"), r.get("stop"), prefs)
            revs = hist.get(r["id"], [])
            for rv in revs:
                risk = abs((rv["entry"] or 0) - (rv["stop"] or 0))
                rv["planned_rr"] = (abs((rv["target"] or 0) - (rv["entry"] or 0)) / risk
                                    if risk > 0 else None)
                rv["size"] = tp.size(rv["entry"], rv["stop"], prefs)
            r["revisions"] = revs

    # The chart pane loads one call on arrival rather than sitting empty — the first
    # row of the newest month on screen. Without it the page opens with a blank
    # half-screen and no hint that clicking a row is what fills it.
    #
    # `row` overrides that: after a revision is saved from the chart the whole panel
    # is re-rendered, and the chart has to come back on the call that was just
    # edited rather than jumping to the top of the list. Ignored when that row is
    # not in the slice on screen, so a stale id can never blank the pane.
    on_screen = {r["id"] for mo in months for r in mo["rows"]}
    try:
        want = int(row)
    except (TypeError, ValueError):
        want = 0
    first_id = want if want in on_screen else None
    if first_id is None:
        for mo in months:
            if mo["rows"]:
                first_id = mo["rows"][0]["id"]
                break

    return {"user": user, "months": months, "totals": cur.overall(months),
            "msg": msg, "err": err, "today": _dt.date.today().isoformat(),
            "years": years, "sel_year": y, "sel_month": m,
            "counts": cal.get(y) or [0] * 12, "prefs": prefs,
            "first_id": first_id,
            "month_abbr": cur.MONTH_ABBR, "any_rows": bool(items)}


@router.get("", response_class=HTMLResponse)
def curated_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request, "curated.html",
        {"user": user, "today": _dt.date.today().isoformat()},
    )


@router.get("/list", response_class=HTMLResponse)
def curated_list(request: Request, year: str = "", month: str = "", row: str = "",
                 user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "_curated_list.html",
        _list_context(db, user, year=year, month=month, row=row))


# There is no GET /curated/{id}/revisions any more (2026-09-08). A call's versions
# are CHILD ROWS of the call in the table, rendered with the list from one grouped
# query (`services.curated.revisions_for_many`) and shown by a chevron — so there is
# nothing left to fetch on expand. Clicking a child row charts that version through
# /curated/chart?rev=<id>, which is unchanged.


@router.get("/chart", response_class=HTMLResponse)
def curated_chart(request: Request, rev: int = 0, row: int = 0,
                  user: User = Depends(require_user),
                  db: Session = Depends(get_db)):
    """One curated call on the page's chart pane — its entry/stop/target as read-only
    price lines. Declared before the /{row_id}/... routes so "chart" is never read
    as a row id.

    Two ways in, because they answer different questions: `row` shows a call as it
    stands NOW (clicking it in the table), `rev` shows one specific version of it
    (clicking a chip in its history). `row` resolves to the current revision, so
    both paths render the same fragment from the same data.
    """
    from ..models import MATPLevel

    if row and not rev:
        rows = cur.revisions_for(db, user, row)
        cur_rev = next((r for r in rows if r.get("current")), None) or (rows[0] if rows else None)
        rev = cur_rev["id"] if cur_rev else 0

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
        # The chart mounts these levels as an EDITABLE trade setup so the stop and
        # the level can be moved here and saved as a revision of THIS call (user,
        # 2026-09-08: "in the curated chart when i amend the SL and level it does
        # not show the save button"). The row id is what the save targets — by id,
        # not by ticker, because the newest call on this symbol may be a different
        # one and revising that would rewrite the wrong idea.
        "row_id": row["id"],
    })


@router.get("/for-symbol/{symbol}")
def curated_for_symbol(symbol: str,
                       user: User = Depends(require_user),
                       db: Session = Depends(get_db)):
    """This member's newest curated call on one ticker, for the chart to ask about.

    The chart uses the answer for two things: whether to show its "curated" badge
    (and what the badge reveals when clicked), and whether its setup editor offers
    `Curate setup` or `Save revision`. Both need the CURRENT levels, so this
    returns them rather than a bare yes/no.
    """
    call = cur.latest_for_symbol(db, user, symbol)
    if call is None:
        return {"ok": True, "call": None}
    revs = cur.revisions_for(db, user, call["id"])
    return {"ok": True, "call": {**call, "revisions": len(revs)}}


@router.post("/from-chart")
def curated_from_chart(
    symbol: str = Form(...),
    entry: str = Form(...),
    stop: str = Form(...),
    target: str = Form(...),
    row_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Curate the trade setup drawn on a chart, or revise a call already made.

    Returns JSON — the caller is the button inside the chart's setup editor, which
    stays on the page and just reports what happened.

    With `row_id` this is a REVISION of that call: the levels change, its history
    grows a "chart" entry, and — crucially — `curated_on` does not move. A revised
    call is still the call you made on the day you made it, so it keeps being judged
    from that date; a new call would reset that clock and quietly launder a losing
    idea into a fresh one. Without `row_id` it is a new call, dated TODAY (never
    back-dated to wherever on the chart the setup was drawn, which would let a setup
    placed over old bars be judged against a move that had already happened).
    """
    rid = (row_id or "").strip()
    if rid:
        try:
            rid_int = int(rid)
        except ValueError:
            return {"ok": False, "error": "Bad curated id."}
        ok, message = cur.update(db, user, rid_int, symbol=symbol, entry=entry,
                                 stop=stop, target=target, source="chart")
        if not ok:
            return {"ok": False, "error": message}
        call = cur.latest_for_symbol(db, user, symbol)
        revs = cur.revisions_for(db, user, rid_int)
        return {"ok": True, "revised": True, "row_id": rid_int,
                "symbol": (symbol or "").strip().upper(),
                "curated_on": call["curated_on"] if call else "",
                "revisions": len(revs), "message": message}

    today = _dt.date.today().isoformat()
    ok, message = cur.add(db, user, symbol=symbol, curated_on=today, entry=entry,
                          stop=stop, target=target, source="chart",
                          note="from chart trade setup")
    if not ok:
        return {"ok": False, "error": message}
    return {"ok": True, "revised": False, "symbol": (symbol or "").strip().upper(),
            "curated_on": today, "message": message}


@router.post("/prefs", response_class=HTMLResponse)
def curated_prefs(
    request: Request,
    nlv: str = Form(""),
    risk_pct: str = Form(""),
    year: str = Form(""),
    month: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Change the account value / risk budget from the Curated page and re-render.

    The SAME two preferences the chart's trade-setup editor edits (services/
    trade_prefs.py) — deliberately not a Curated-only copy, because a member who
    raises their account there and sees old sizes here would have to guess which
    number the platform believed. Re-rendering the list is the point: every row's
    quantity is derived, so they all move at once.
    """
    _, err = tp.write(db, user, nlv=nlv, risk_pct=risk_pct)
    ctx = _list_context(db, user, year=year, month=month,
                        msg="" if err else "Position sizing updated.", err=err)
    return templates.TemplateResponse(request, "_curated_list.html", ctx)


# There is no POST /curated/add: the hand-typed form it served was removed on
# 2026-09-08 (user: "all curation must be either from the Sector and industry
# chart or the Watchlist chart"). Calls arrive through /curated/from-chart, so
# every one of them carries the levels of a setup drawn on a real chart. Editing
# an existing call's levels is still allowed below — that is a correction to a
# call already made, not a new one conjured from three typed numbers.


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
