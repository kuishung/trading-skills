"""Options tab — bull put spread finder + chain, for the selected watchlist ticker.

One HTMX endpoint returning a fragment, because the tab is swapped into the
watchlist's right-hand pane rather than being its own page.

Everything the tab needs comes from ONE pass so opening it costs a single TWS
round-trip: the chain is fetched for the expiry that best fits the 45-60 DTE
window, and that same snapshot feeds both the spread selection and the table.

The service never raises on a dead TWS, so this route always renders: either the
analysis, or a panel explaining exactly what to switch on.

This SUGGESTS and MONITORS. It never places an order — the fill is the user's
action in TWS.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MATPLevel, OptionSpread, User, _utcnow
from ..security import require_user
from ..services import bull_put, ibkr_options

router = APIRouter(prefix="/options", tags=["options"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _earnings_date(symbol: str) -> str | None:
    """Next earnings (ISO) — feeds the 'no earnings inside the trade' gate."""
    try:
        from ..services.prices import fetch_next_earnings

        ne = fetch_next_earnings(symbol)
        return (ne or {}).get("date")
    except Exception:  # noqa: BLE001
        return None


@router.get("/{symbol}", response_class=HTMLResponse)
def options_tab(symbol: str, request: Request,
                exp: str | None = None,
                user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    """Spread suggestion + chain for `symbol` (optionally a specific expiry)."""
    sym = (symbol or "").strip().upper()

    # An explicit expiry means the user is browsing; otherwise aim at 45-60 DTE.
    if exp:
        chain = ibkr_options.get_chain(sym, exp)
    else:
        chain = ibkr_options.chain_for_dte(sym, bull_put.DTE_MIN, bull_put.DTE_MAX)

    ctx = {"user": user, "sym": sym, "chain": chain,
           "iv": None, "nlv": None, "spread": None, "earnings": None, "level": None}

    if chain.get("ok"):
        iv = ibkr_options.iv_stats(sym)
        nlv = ibkr_options.net_liquidation()
        earnings = _earnings_date(sym)
        level = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()

        ctx.update({"iv": iv, "nlv": nlv, "earnings": earnings, "level": level})
        ctx["spread"] = bull_put.select(
            symbol=sym,
            spot=chain["spot"],
            expiry=chain["expiry_label"],
            dte=chain.get("dte") or 0,
            puts=chain.get("puts") or [],
            iv_percentile=(iv or {}).get("iv_percentile"),
            earnings_date=earnings,
            net_liquidation=nlv,
        )

    return templates.TemplateResponse(request, "_options_tab.html", ctx)


# ---------------------------------------------------------------- tracking
# Tracking only. Nothing below places, modifies or cancels an order — the fill
# happens in TWS by the user's own hand; this records what they say they opened
# so the delta line can be watched against it.

@router.post("/track", response_class=HTMLResponse)
def track_spread(request: Request,
                 symbol: str = Form(...),
                 expiry: str = Form(...),
                 short_strike: float = Form(...),
                 long_strike: float = Form(...),
                 credit: float = Form(0.0),
                 contracts: int = Form(1),
                 entry_delta: float = Form(0.0),
                 user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    row = OptionSpread(
        user_id=user.id, symbol=symbol.strip().upper(), strategy="bull_put",
        expiry=expiry, short_strike=short_strike, long_strike=long_strike,
        credit=credit or None, contracts=max(1, contracts),
        entry_delta=entry_delta or None, status="open",
    )
    db.add(row)
    db.commit()
    return _positions_fragment(request, user, db)


@router.post("/{spread_id}/close", response_class=HTMLResponse)
def close_spread(spread_id: int, request: Request,
                 user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    row = (db.query(OptionSpread)
             .filter(OptionSpread.id == spread_id,
                     OptionSpread.user_id == user.id)     # scoped: never another user's
             .one_or_none())
    if row is not None:
        row.status = "closed"
        row.closed_at = _utcnow()
        db.commit()
    return _positions_fragment(request, user, db)


def _positions_fragment(request: Request, user: User, db: Session) -> HTMLResponse:
    """Open spreads, each graded against the management rule with a LIVE delta."""
    rows = (db.query(OptionSpread)
              .filter(OptionSpread.user_id == user.id, OptionSpread.status == "open")
              .order_by(OptionSpread.opened_at.desc())
              .all())
    items = []
    for r in rows:
        live = ibkr_options.short_put_delta(r.symbol, r.expiry, r.short_strike)
        dte = live.get("dte")
        if dte is None:
            try:
                import datetime as _d
                dte = (_d.date.fromisoformat(r.expiry) - _d.date.today()).days
            except ValueError:
                dte = 0
        items.append({
            "row": r,
            "delta": live.get("delta"),
            "dte": dte,
            "error": live.get("error"),
            "verdict": bull_put.review(short_delta=live.get("delta"), dte=dte),
        })
    return templates.TemplateResponse(
        request, "_options_positions.html", {"user": user, "items": items}
    )


@router.get("/positions/all", response_class=HTMLResponse)
def positions(request: Request,
              user: User = Depends(require_user),
              db: Session = Depends(get_db)):
    return _positions_fragment(request, user, db)
