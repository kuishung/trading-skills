"""Options > Spread — the bull put spread screener.

Barchart's "Bull Put Spread" screen, rebuilt on the platform's own data so the
member never leaves the site to find a trade and never retypes one to track it:

* the nightly scan (``deploy/spread_scan.py`` -> ``services/spread_scan``)
  files every candidate spread on the S&P 500 + watchlists into
  ``spread_candidates``, one column per Barchart screen field;
* this page filters those rows with the member's saved criteria (defaults =
  the user's Barchart settings), sorts them, charts one on the same component
  Portfolio uses, and **Track** turns a row into a monitored ``OptionSpread``
  in one click, legs and entry greeks prefilled.

Scope: **screening and tracking, not execution.** Nothing here places an order.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MATPLevel, OptionSpread, SpreadCandidate, User
from ..security import require_user
from ..services import spread_scan as ss

router = APIRouter(prefix="/spreads", tags=["spreads"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

PREF_KEY = "spread_screen"      # User.prefs[PREF_KEY] = the member's saved filter


def _saved_filter(user: User) -> dict:
    prefs = getattr(user, "prefs", None) or {}
    return ss.clean_filter(prefs.get(PREF_KEY) or {})


def _save_filter(db: Session, user: User, f: dict) -> None:
    prefs = dict(getattr(user, "prefs", None) or {})
    prefs[PREF_KEY] = f
    user = db.merge(user)       # attach to THIS session whichever one loaded it
    user.prefs = prefs          # reassign so SQLAlchemy sees the change
    db.commit()


def _list_context(db: Session, user: User, *, f: dict | None = None,
                  symbol: str = "", order: str = "credit_pct", desc: bool = True,
                  focus: int | None = None) -> dict:
    f = f or _saved_filter(user)
    scan = ss.latest_scan(db)
    on = scan.scan_on if scan else None
    rows = ss.query_candidates(db, on, f, symbol=symbol or None,
                               order=order, desc=desc) if on else []
    # Symbols already tracked in Portfolio, so a row can say "you have this one".
    tracked = {(r.symbol, r.expiry, r.short_strike, r.long_strike)
               for r in db.query(OptionSpread.symbol, OptionSpread.expiry,
                                 OptionSpread.short_strike, OptionSpread.long_strike)
                          .filter(OptionSpread.user_id == user.id,
                                  OptionSpread.status == "open").all()}
    n_syms = len({r.symbol for r in rows})
    iv_known = sum(1 for r in rows if r.iv_pct is not None)
    return {
        "user": user, "rows": rows, "f": f, "symbol": symbol,
        "order": order, "desc": desc, "focus": focus,
        "scan": scan, "scan_on": on, "today_et": ss.et_today(),
        "n_symbols": n_syms, "iv_known": iv_known,
        "tracked": tracked, "defaults": ss.DEFAULT_FILTER,
        "quote_source": "Cboe delayed (~15 min), scanned nightly",
    }


@router.get("", response_class=HTMLResponse)
def spreads_home(request: Request, user: User = Depends(require_user)):
    """Shell only; the results load lazily like Portfolio's board."""
    return templates.TemplateResponse(request, "spreads.html", {"user": user})


@router.get("/list", response_class=HTMLResponse)
def spreads_list(request: Request, symbol: str = "", order: str = "credit_pct",
                 desc: int = 1, focus: int = 0,
                 user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx = _list_context(db, user, symbol=symbol.strip().upper(), order=order,
                        desc=bool(desc), focus=focus or None)
    return templates.TemplateResponse(request, "_spreads_list.html", ctx)


@router.post("/filter", response_class=HTMLResponse)
async def spreads_filter(request: Request, user: User = Depends(require_user),
                         db: Session = Depends(get_db)):
    """Save the member's criteria and re-run the query. Checkboxes that are off
    are simply absent from the form, so every boolean is rebuilt explicitly."""
    form = await request.form()
    raw = {k: form.get(k) for k in ss.DEFAULT_FILTER}
    for k, dflt in ss.DEFAULT_FILTER.items():
        if isinstance(dflt, bool):
            raw[k] = form.get(k) is not None
    f = ss.clean_filter(raw)
    _save_filter(db, user, f)
    ctx = _list_context(db, user, f=f, symbol=(form.get("symbol") or "").strip().upper(),
                        order=form.get("order") or "credit_pct",
                        desc=(form.get("desc") or "1") == "1")
    return templates.TemplateResponse(request, "_spreads_list.html", ctx)


@router.post("/filter/reset", response_class=HTMLResponse)
def spreads_filter_reset(request: Request, user: User = Depends(require_user),
                         db: Session = Depends(get_db)):
    f = ss.clean_filter({})
    _save_filter(db, user, f)
    return templates.TemplateResponse(request, "_spreads_list.html",
                                      _list_context(db, user, f=f))


@router.get("/chart", response_class=HTMLResponse)
def spreads_chart(request: Request, row: int = 0,
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """One candidate on the chart pane: both strikes and the breakeven drawn,
    the expiry badged, same component as Portfolio and Curated."""
    c = db.get(SpreadCandidate, row)
    if c is None:
        return HTMLResponse('<div class="flex-1 flex items-center justify-center text-xs '
                            'text-slate-500">Candidate not found.</div>')
    from .matp import _chart_context

    sel = db.query(MATPLevel).filter(MATPLevel.symbol == c.symbol).first()
    cc = _chart_context(db, sel)
    credit = c.credit if c.credit is not None else 0.0
    return templates.TemplateResponse(request, "_spreads_chart.html", {
        "user": user, "symbol": c.symbol, "sel": sel,
        "sel_band": cc["sel_band"], "sel_patterns": cc["sel_patterns"],
        "c": c,
        "spread": {
            "short": c.short_strike, "long": c.long_strike,
            "breakeven": c.short_strike - credit, "expiry": c.expiry,
            "label": "%g/%gP" % (c.short_strike, c.long_strike),
        },
    })


@router.post("/{cand_id}/track")
def spreads_track(cand_id: int, contracts: int = Form(1),
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Turn a candidate into a tracked Portfolio position: short leg at its bid,
    long leg at its ask (the prices the credit was computed from), entry greeks
    from the scan. Then send the browser to Portfolio."""
    c = db.get(SpreadCandidate, cand_id)
    if c is None:
        return Response(status_code=404)
    credit = c.credit if c.credit is not None else c.credit_mid
    db.add(OptionSpread(
        user_id=user.id, symbol=c.symbol, strategy="bull_put",
        expiry=c.expiry, short_strike=c.short_strike, long_strike=c.long_strike,
        short_price=c.short_bid, long_price=c.long_ask,
        credit=credit, contracts=max(1, int(contracts or 1)), status="open",
        entry_delta=c.short_delta, short_entry_delta=c.short_delta,
        long_entry_delta=c.long_delta, entry_iv=c.short_iv,
        note=f"from Spread screen {c.scan_on}"
             + (f", IV pct {c.iv_pct:.0f}" if c.iv_pct is not None else ""),
    ))
    db.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/portfolio"})


@router.post("/rescan", response_class=HTMLResponse)
def spreads_rescan(request: Request, symbol: str = Form(...),
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Re-read ONE symbol's chain live and replace its rows in the latest scan,
    so a candidate can be checked during the day without a full run."""
    sym = symbol.strip().upper()
    scan = ss.latest_scan(db)
    on = scan.scan_on if scan else ss.et_today()
    err = None
    if sym:
        from ..services import option_quotes

        option_quotes.clear_cache()
        rows, err = ss.scan_symbol(db, sym, on=on, today=_dt.date.fromisoformat(on))
        if not err:
            ss.replace_candidates(db, on, sym, rows)
            db.commit()
    ctx = _list_context(db, user, symbol=sym)
    if err:
        ctx["form_error"] = f"{sym}: {err}"
    elif not rows:
        ctx["form_error"] = (f"{sym}: chain read, but no spread inside the stored range "
                             f"(DTE {ss.STORE_DTE_MIN}-{ss.STORE_DTE_MAX}, short put 0 to "
                             f"{abs(ss.STORE_MONEYNESS_MIN):g}% below price).")
    return templates.TemplateResponse(request, "_spreads_list.html", ctx)


@router.get("/status")
def spreads_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    scan = ss.latest_scan(db)
    if scan is None:
        return {"scan_on": None, "stale": True}
    return {"scan_on": scan.scan_on, "symbols": scan.symbols, "priced": scan.priced,
            "candidates": scan.candidates, "errors": scan.errors,
            "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
            "stale": scan.scan_on < ss.et_today()}
