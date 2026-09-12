"""Portfolio — the member's own open option spreads, monitored daily.

What this page is for
---------------------
The Options tab (``routes/options.py``) answers "is this a trade worth putting
on?" against a chain the member's browser read from their own TWS. This page
answers the question that comes after: **"is the trade I already have still
alright, and is today the day to do something about it?"**

Four exit lines are graded every day, per the member's own settings:

* the short put's **delta** reaching their roll line (default 0.30),
* unrealised loss reaching a fraction of **max loss** (default 20%),
* the **profit target** — a fraction of the credit captured (default 50%), and
* the **DTE floor** — days left at which to close or roll regardless (default 21).

The first two defend the trade; the last two take it off while it is still a
winner. Each has a member default and a per-trade override.

Legs are captured as the member filled them (v4.62): the short put at its sale
price and the long put at its cost, per share. The net credit is derived from
those, and the chain's delta/IV for both legs are stored at entry as the baseline
every later daily check is read against.

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
            "spot": r.spot, "long_delta": r.long_delta, "net_delta": r.net_delta,
            "theta": r.theta, "iv": r.short_iv, "profit_pct": r.profit_pct,
            "mark": r.mark,
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
    # Position-level greeks, summed where every row could be priced.
    tot_delta = sum(it["snap"]["net_delta"] for it in items
                    if it["snap"].get("net_delta") is not None)
    tot_theta = sum(it["snap"]["theta"] for it in items
                    if it["snap"].get("theta") is not None)

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
                   "priced": priced, "count": len(items),
                   "net_delta": tot_delta, "theta": tot_theta},
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
    snap = spread_monitor.snapshot_rows([r], prefs=prefs)[0]["snap"]

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


def _opt_float(v, hi: float, *, allow_zero: bool = False):
    """An optional numeric form field. Blank/garbage -> None (= use default).
    ``allow_zero`` lets 0 through, which for the winning-side lines means
    "switch this line off for this trade"."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    lo_ok = f >= 0 if allow_zero else f > 0
    return f if lo_ok and f <= hi else None


def _entry_greeks(sym: str, expiry: str, short_strike: float, long_strike: float) -> dict:
    """The chain's delta/IV for both legs right now — the baseline stored with a
    new trade. Best effort: a chain that cannot be read leaves them None rather
    than blocking the member from recording a fill they already have."""
    out = {"short_entry_delta": None, "long_entry_delta": None, "entry_iv": None}
    try:
        ch = option_quotes.fetch_chain(sym)
    except option_quotes.ChainError:
        return out
    s = option_quotes.leg(ch, expiry, "P", short_strike)
    l = option_quotes.leg(ch, expiry, "P", long_strike)
    if s is not None:
        out["short_entry_delta"] = None if s.get("delta") is None else abs(s["delta"])
        out["entry_iv"] = s.get("iv")
    if l is not None:
        out["long_entry_delta"] = None if l.get("delta") is None else abs(l["delta"])
    return out


@router.post("/add", response_class=HTMLResponse)
def portfolio_add(request: Request,
                  symbol: str = Form(...),
                  expiry: str = Form(...),
                  short_strike: float = Form(...),
                  long_strike: float = Form(...),
                  short_price: str = Form(""),
                  long_price: str = Form(""),
                  credit: str = Form(""),
                  contracts: int = Form(1),
                  roll_delta: str = Form(""),
                  loss_stop_pct: str = Form(""),
                  profit_target_pct: str = Form(""),
                  dte_floor: str = Form(""),
                  note: str = Form(""),
                  user: User = Depends(require_user),
                  db: Session = Depends(get_db)):
    """Record a spread the member has already opened in their own broker.

    Legs first: the short put's sale price and the long put's cost, per share, as
    filled. The credit is DERIVED from those when both are given; the plain
    credit field remains for a member who only knows the net. If both are given
    and disagree, the legs win — they are the fills, the net is arithmetic.

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

    sp = _opt_float(short_price, 1e6)
    lp = _opt_float(long_price, 1e6, allow_zero=True)
    cr = _opt_float(credit, 1e6, allow_zero=True)
    if not err and (short_price.strip() or long_price.strip()) and (sp is None or lp is None):
        err = ("Enter BOTH leg prices (what the short put sold for and what the long "
               "put cost, per share) — or leave both blank and enter the net credit.")
    if not err and sp is not None and lp is not None:
        if lp >= sp:
            err = ("The long put cost more than the short put sold for — that is a "
                   "debit, not a bull put spread. Check the two prices.")
        else:
            cr = round(sp - lp, 4)
    if not err and cr is None:
        err = "Enter the two leg prices, or the net credit per share."
    if not err and cr < 0:
        err = "Credit is what you received; enter it as a positive number."
    if not err and cr >= (short_strike - long_strike):
        err = ("Credit cannot be more than the width of the spread — that would be "
               "a risk-free trade. Prices are per share (e.g. 4.04), not per contract.")

    if err:
        ctx = _list_context(db, user)
        ctx["form_error"] = err
        return templates.TemplateResponse(request, "_portfolio_list.html", ctx)

    greeks = _entry_greeks(sym, expiry.strip(), short_strike, long_strike)
    db.add(OptionSpread(
        user_id=user.id, symbol=sym, strategy="bull_put",
        expiry=expiry.strip(), short_strike=short_strike, long_strike=long_strike,
        short_price=sp, long_price=lp,
        credit=cr or None, contracts=max(1, contracts), status="open",
        roll_delta=_opt_float(roll_delta, 1.0),
        loss_stop_pct=_opt_float(loss_stop_pct, 100.0),
        profit_target_pct=_opt_float(profit_target_pct, 100.0, allow_zero=True),
        dte_floor=(None if _opt_float(dte_floor, 365.0, allow_zero=True) is None
                   else int(float(dte_floor))),
        entry_delta=greeks["short_entry_delta"],
        note=(note or "").strip() or None,
        **greeks,
    ))
    db.commit()
    return templates.TemplateResponse(request, "_portfolio_list.html",
                                      _list_context(db, user))


@router.post("/prefs", response_class=HTMLResponse)
def portfolio_prefs(request: Request,
                    roll_delta: str = Form(""),
                    loss_stop_pct: str = Form(""),
                    profit_target_pct: str = Form(""),
                    dte_floor: str = Form(""),
                    user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """The member's DEFAULT exit lines, applied to every trade without its own."""
    _, err = tp.write(db, user, roll_delta=roll_delta or None,
                      loss_stop_pct=loss_stop_pct or None,
                      profit_target_pct=profit_target_pct or None,
                      dte_floor=dte_floor or None)
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
                    profit_target_pct: str = Form(""),
                    dte_floor: str = Form(""),
                    user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """Per-trade exit-line override. Blank clears it back to the member's
    default; 0 on a winning-side line switches that line off for this trade."""
    row = (db.query(OptionSpread)
             .filter(OptionSpread.id == spread_id,
                     OptionSpread.user_id == user.id)
             .one_or_none())
    if row is not None:
        row.roll_delta = _opt_float(roll_delta, 1.0)
        row.loss_stop_pct = _opt_float(loss_stop_pct, 100.0)
        row.profit_target_pct = _opt_float(profit_target_pct, 100.0, allow_zero=True)
        df = _opt_float(dte_floor, 365.0, allow_zero=True)
        row.dte_floor = None if df is None else int(df)
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
        "urgent": sum(1 for s in states if s in ("ROLL", "CLOSE", "TAKE")),
        "watch": sum(1 for s in states if s == "WATCH"),
        "checked_on": max((r.checked_on for r in latest.values()), default=None),
        "stale": bool(latest) and max(r.checked_on for r in latest.values()) < day,
    }
