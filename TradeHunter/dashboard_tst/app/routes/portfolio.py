"""Portfolio — the member's own open option spreads, monitored daily.

What this page is for
---------------------
The Options tab (``routes/options.py``) answers "is this a trade worth putting
on?" against a chain the member's browser read from their own TWS. This page
answers the question that comes after: **"is the trade I already have still
alright, and is today the day to do something about it?"**

Two exit lines are graded every day, per the member's own settings:

* the short put's **delta** reaching their roll line (default 0.30), and
* unrealised loss reaching a fraction of **max loss** (default 20%).

Where the numbers come from
---------------------------
``services/option_quotes.py`` reads Cboe's public delayed feed SERVER-side, with
real greeks. That is the whole reason this page can claim to monitor anything: a
check that only runs when the member happens to open the page is not a daily
check, and the TWS bridge is unreachable from the server by design. The bridge
can still overwrite a row with exact broker numbers while the page is open — but
nothing depends on it being up.

Scope: **tracking, not execution.** Nothing here places, modifies or cancels an
order. Rows record what the member says they opened; closing is their own action
in their own broker, and "Mark closed" only updates this board.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from fastapi.templating import Jinja2Templates

from ..db import get_db
from ..models import MATPLevel, OptionSpread, SpreadCheck, User, _utcnow
from ..security import require_user
from ..services import option_quotes, spread_monitor
from ..services import trade_prefs as tp

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# How many days of check history to hand the row's sparkline. Long enough to show
# a drift, short enough that the page does not carry a year of rows it never draws.
HISTORY_DAYS = 45


def _history(db: Session, spread_ids: list[int]) -> dict[int, list[dict]]:
    """Recent checks per spread, oldest first — the series behind the sparkline."""
    if not spread_ids:
        return {}
    cutoff = (_dt.date.today() - _dt.timedelta(days=HISTORY_DAYS)).isoformat()
    rows = (db.query(SpreadCheck)
              .filter(SpreadCheck.spread_id.in_(spread_ids),
                      SpreadCheck.checked_on >= cutoff)
              .order_by(SpreadCheck.checked_on)
              .all())
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r.spread_id, []).append({
            "on": r.checked_on, "delta": r.short_delta, "loss_pct": r.loss_pct,
            "pl": r.pl, "state": r.state, "dte": r.dte, "source": r.source,
            "spot": r.spot,
        })
    return out


def _list_context(db: Session, user: User, *, status: str = "open",
                  record: bool = True, focus: int | None = None) -> dict:
    """The board: every tracked spread with today's quotes and today's verdict.

    ``record=True`` files the result as today's check, so simply opening the page
    keeps the history alive even if the nightly sweep never ran. That is the
    difference between a monitor and a report.
    """
    prefs = tp.read(user)
    q = db.query(OptionSpread).filter(OptionSpread.user_id == user.id)
    if status in ("open", "closed"):
        q = q.filter(OptionSpread.status == status)
    rows = q.order_by(OptionSpread.expiry, OptionSpread.symbol).all()

    items = spread_monitor.snapshot_rows(rows, prefs=prefs)

    if record and status == "open":
        day = spread_monitor.et_today()
        for it in items:
            # An un-priceable row is not evidence about the trade, it is evidence
            # about the feed — filing it would put a hole in the series that looks
            # like a delta reading of "nothing".
            if it["snap"].get("short_delta") is None and it["snap"].get("pl") is None:
                continue
            spread_monitor.record_check(db, it["row"], it["snap"],
                                        source="cboe", on=day)
        db.commit()

    hist = _history(db, [it["row"].id for it in items])
    for it in items:
        it["history"] = hist.get(it["row"].id, [])

    urgent = [it for it in items if (it["snap"].get("verdict") or {}).get("urgent")]
    watch = [it for it in items
             if (it["snap"].get("verdict") or {}).get("state") == "WATCH"]

    # Portfolio-level totals. Credit received and max loss are certainties; the
    # open P/L is a mark, so it is labelled as one in the template rather than
    # presented next to them as if it were settled.
    tot_credit = sum((it["snap"]["max_profit"] or 0.0) for it in items)
    tot_risk = sum((it["snap"]["max_loss"] or 0.0) for it in items)
    tot_pl = sum((it["snap"]["pl"] or 0.0) for it in items
                 if it["snap"].get("pl") is not None)
    priced = sum(1 for it in items if it["snap"].get("pl") is not None)

    return {
        "user": user, "items": items, "status": status, "prefs": prefs,
        "urgent": urgent, "watch": watch, "focus": focus,
        "totals": {"credit": tot_credit, "risk": tot_risk, "pl": tot_pl,
                   "priced": priced, "count": len(items)},
        "checked_on": spread_monitor.et_today(),
        "quote_source": "Cboe delayed (~15 min)",
    }


@router.get("", response_class=HTMLResponse)
def portfolio_home(request: Request, user: User = Depends(require_user)):
    """Shell only. The list is lazy: rendering it fetches a chain per underlying,
    and the page shell must never wait on that."""
    return templates.TemplateResponse(request, "portfolio.html", {"user": user})


@router.get("/list", response_class=HTMLResponse)
def portfolio_list(request: Request, status: str = "open", focus: int = 0,
                   user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    ctx = _list_context(db, user, status=status, focus=focus or None)
    return templates.TemplateResponse(request, "_portfolio_list.html", ctx)


@router.post("/refresh", response_class=HTMLResponse)
def portfolio_refresh(request: Request, status: str = Form("open"),
                      user: User = Depends(require_user),
                      db: Session = Depends(get_db)):
    """Re-read every chain now, bypassing the 15-minute quote cache."""
    option_quotes.clear_cache()
    ctx = _list_context(db, user, status=status)
    return templates.TemplateResponse(request, "_portfolio_list.html", ctx)


@router.get("/chart", response_class=HTMLResponse)
def portfolio_chart(request: Request, row: int = 0,
                    user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """One tracked spread on the Curated page's chart pane.

    Declared before ``/{spread_id}/...`` so "chart" is never read as an id — the
    same ordering trap curated.py documents.

    The strikes mount as price lines and the expiry as a date-axis badge, so the
    question the chart is actually being asked — "how far is price from my short
    strike, and how many candles has it got left to get there?" — is answered by
    looking at it rather than by reading the row underneath.
    """
    r = (db.query(OptionSpread)
           .filter(OptionSpread.id == row, OptionSpread.user_id == user.id)
           .one_or_none())
    if r is None:
        return HTMLResponse(
            '<div class="flex-1 flex items-center justify-center text-xs '
            'text-slate-500">Trade not found.</div>')

    prefs = tp.read(user)
    snap = spread_monitor.snapshot(
        symbol=r.symbol, expiry=r.expiry, short_strike=r.short_strike,
        long_strike=r.long_strike, credit=r.credit or 0.0, contracts=r.contracts or 1,
        roll_delta=r.roll_delta or prefs["roll_delta"],
        loss_fraction=(r.loss_stop_pct / 100.0) if r.loss_stop_pct else prefs["loss_fraction"],
    )

    # Same MATP/analyst context the Watchlist, Sector and Curated charts use, so a
    # tracked ticker that is also on the board keeps its MATP/MBP reference lines.
    from .matp import _chart_context

    sel = db.query(MATPLevel).filter(MATPLevel.symbol == r.symbol).first()
    cc = _chart_context(db, sel)
    return templates.TemplateResponse(request, "_portfolio_chart.html", {
        "user": user, "symbol": r.symbol, "sel": sel,
        "sel_band": cc["sel_band"], "sel_patterns": cc["sel_patterns"],
        "row": r, "snap": snap,
        "spread": {
            "short": r.short_strike, "long": r.long_strike,
            "breakeven": snap["breakeven"], "expiry": r.expiry,
            "label": "%g/%gP" % (r.short_strike, r.long_strike),
        },
    })


@router.post("/add", response_class=HTMLResponse)
def portfolio_add(request: Request,
                  symbol: str = Form(...),
                  expiry: str = Form(...),
                  short_strike: float = Form(...),
                  long_strike: float = Form(...),
                  credit: float = Form(0.0),
                  contracts: int = Form(1),
                  roll_delta: str = Form(""),
                  loss_stop_pct: str = Form(""),
                  note: str = Form(""),
                  user: User = Depends(require_user),
                  db: Session = Depends(get_db)):
    """Record a spread the member has already opened in their own broker.

    Validated here rather than trusted: a long strike above the short is a bear
    put spread, not a bull put one, and silently storing it would grade it with
    the wrong rule for the rest of its life.
    """
    sym = (symbol or "").strip().upper()
    err = ""
    try:
        _dt.date.fromisoformat(expiry.strip())
    except (TypeError, ValueError):
        err = "Expiry must be a date (YYYY-MM-DD)."
    if not err and long_strike >= short_strike:
        err = ("For a bull put spread the long put must be BELOW the short put. "
               "Check the two strikes.")
    if not err and (credit or 0) < 0:
        err = "Credit is what you received; enter it as a positive number."
    if not err and (credit or 0) >= (short_strike - long_strike):
        err = ("Credit cannot be more than the width of the spread — that would be "
               "a risk-free trade. Credit is per share (e.g. 0.36), not per contract.")

    if err:
        ctx = _list_context(db, user)
        ctx["form_error"] = err
        return templates.TemplateResponse(request, "_portfolio_list.html", ctx)

    def _opt(v, hi):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if 0 < f <= hi else None

    db.add(OptionSpread(
        user_id=user.id, symbol=sym, strategy="bull_put",
        expiry=expiry.strip(), short_strike=short_strike, long_strike=long_strike,
        credit=credit or None, contracts=max(1, contracts), status="open",
        roll_delta=_opt(roll_delta, 1.0),
        loss_stop_pct=_opt(loss_stop_pct, 100.0),
        note=(note or "").strip() or None,
    ))
    db.commit()
    return templates.TemplateResponse(request, "_portfolio_list.html",
                                      _list_context(db, user))


@router.post("/prefs", response_class=HTMLResponse)
def portfolio_prefs(request: Request,
                    roll_delta: str = Form(""),
                    loss_stop_pct: str = Form(""),
                    user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """The member's DEFAULT exit lines, applied to every trade without its own."""
    _, err = tp.write(db, user, roll_delta=roll_delta or None,
                      loss_stop_pct=loss_stop_pct or None)
    ctx = _list_context(db, user)
    if err:
        ctx["form_error"] = err
    return templates.TemplateResponse(request, "_portfolio_list.html", ctx)


@router.post("/{spread_id}/close", response_class=HTMLResponse)
def portfolio_close(spread_id: int, request: Request,
                    user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    row = (db.query(OptionSpread)
             .filter(OptionSpread.id == spread_id,
                     OptionSpread.user_id == user.id)    # scoped: never another member's
             .one_or_none())
    if row is not None:
        row.status = "closed"
        row.closed_at = _utcnow()
        db.commit()
    return templates.TemplateResponse(request, "_portfolio_list.html",
                                      _list_context(db, user))


@router.post("/{spread_id}/reopen", response_class=HTMLResponse)
def portfolio_reopen(spread_id: int, request: Request,
                     user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    """Undo a mis-click on Mark closed, without retyping the trade."""
    row = (db.query(OptionSpread)
             .filter(OptionSpread.id == spread_id,
                     OptionSpread.user_id == user.id)
             .one_or_none())
    if row is not None:
        row.status = "open"
        row.closed_at = None
        db.commit()
    return templates.TemplateResponse(request, "_portfolio_list.html",
                                      _list_context(db, user))


@router.post("/{spread_id}/lines", response_class=HTMLResponse)
def portfolio_lines(spread_id: int, request: Request,
                    roll_delta: str = Form(""),
                    loss_stop_pct: str = Form(""),
                    user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """Per-trade exit-line override. Blank clears it back to the member's default."""
    row = (db.query(OptionSpread)
             .filter(OptionSpread.id == spread_id,
                     OptionSpread.user_id == user.id)
             .one_or_none())
    if row is not None:
        def _opt(v, hi):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if 0 < f <= hi else None
        row.roll_delta = _opt(roll_delta, 1.0)
        row.loss_stop_pct = _opt(loss_stop_pct, 100.0)
        db.commit()
    return templates.TemplateResponse(request, "_portfolio_list.html",
                                      _list_context(db, user, focus=spread_id))


@router.post("/{spread_id}/delete", response_class=HTMLResponse)
def portfolio_delete(spread_id: int, request: Request,
                     user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    """Remove a trade entered by mistake. Its checks go with it (cascade) —
    a history of a trade that never existed is worse than no history."""
    row = (db.query(OptionSpread)
             .filter(OptionSpread.id == spread_id,
                     OptionSpread.user_id == user.id)
             .one_or_none())
    if row is not None:
        db.delete(row)
        db.commit()
    return templates.TemplateResponse(request, "_portfolio_list.html",
                                      _list_context(db, user))


@router.get("/badge")
def portfolio_badge(user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """How many open spreads need action, for the nav badge.

    Reads the STORED checks rather than re-pricing: the badge is polled from
    every page, and making it fetch a chain per underlying would put the whole
    site behind Cboe. The nightly sweep and every visit to Portfolio keep those
    rows current.
    """
    ids = [r.id for r in db.query(OptionSpread.id)
             .filter(OptionSpread.user_id == user.id,
                     OptionSpread.status == "open").all()]
    if not ids:
        return {"urgent": 0, "watch": 0, "checked_on": None}

    day = spread_monitor.et_today()
    rows = (db.query(SpreadCheck)
              .filter(SpreadCheck.spread_id.in_(ids))
              .order_by(SpreadCheck.checked_on.desc())
              .all())
    latest: dict[int, SpreadCheck] = {}
    for r in rows:
        latest.setdefault(r.spread_id, r)
    states = [r.state for r in latest.values()]
    return {
        "urgent": sum(1 for s in states if s in ("ROLL", "CLOSE")),
        "watch": sum(1 for s in states if s == "WATCH"),
        "checked_on": max((r.checked_on for r in latest.values()), default=None),
        "stale": bool(latest) and max(r.checked_on for r in latest.values()) < day,
    }
