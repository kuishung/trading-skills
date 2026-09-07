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


def _list_context(db: Session, user: User, msg: str = "", err: str = "") -> dict:
    """Everything the list fragment needs. Shared verbatim by the GET and every
    POST, so a form post re-renders exactly what a refresh would show."""
    months = cur.by_month(cur.rows_for(cur.list_for(db, user)))
    return {"user": user, "months": months, "totals": cur.overall(months),
            "msg": msg, "err": err, "today": _dt.date.today().isoformat()}


@router.get("", response_class=HTMLResponse)
def curated_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request, "curated.html",
        {"user": user, "today": _dt.date.today().isoformat()},
    )


@router.get("/list", response_class=HTMLResponse)
def curated_list(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "_curated_list.html", _list_context(db, user))


@router.post("/add", response_class=HTMLResponse)
def curated_add(
    request: Request,
    symbol: str = Form(...),
    curated_on: str = Form(...),
    entry: str = Form(...),
    stop: str = Form(...),
    target: str = Form(...),
    note: str = Form(""),
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
    ctx = _list_context(db, user, msg=message if ok else "", err="" if ok else message)
    return templates.TemplateResponse(request, "_curated_list.html", ctx)


@router.post("/{row_id}/edit", response_class=HTMLResponse)
def curated_edit(
    request: Request,
    row_id: int,
    symbol: str = Form(""),
    curated_on: str = Form(""),
    entry: str = Form(""),
    stop: str = Form(""),
    target: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    fields = {k: v for k, v in
              {"symbol": symbol, "curated_on": curated_on, "entry": entry,
               "stop": stop, "target": target, "note": note}.items() if v != ""}
    ok, message = cur.update(db, user, row_id, **fields)
    ctx = _list_context(db, user, msg=message if ok else "", err="" if ok else message)
    return templates.TemplateResponse(request, "_curated_list.html", ctx)


@router.post("/{row_id}/delete", response_class=HTMLResponse)
def curated_delete(request: Request, row_id: int,
                   user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    gone = cur.remove(db, user, row_id)
    ctx = _list_context(db, user, msg="Removed." if gone else "")
    return templates.TemplateResponse(request, "_curated_list.html", ctx)
