"""Options tab — bull put spread finder + chain, per member, off THEIR OWN TWS.

Architecture (changed in v4.26)
------------------------------
TWS runs on each member's PC under their own login, so the SERVER never connects
to a broker. The browser fetches from a bridge on ``127.0.0.1:9224``
(``dashboard_tst/bridge/ibkr_bridge.py``) and POSTs the result here; this module
only evaluates rules and renders. Same shape as the TradingView bridge.

    browser ──fetch──> 127.0.0.1:9224 ──> that member's TWS
            ──POST───> here: rule evaluation + rendering only

That is what makes this multi-user: every member is graded against their own
chain, their own IV history and their own net liquidation, and nobody's broker
session is reachable from the server.

Nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MATPLevel, OptionSpread, User, _utcnow
from ..security import require_user
from ..services import bull_put

router = APIRouter(prefix="/options", tags=["options"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

BRIDGE_PORT = 9224      # 9223 is the TradingView bridge
# Shown in the panel when the "Start the bridge" button finds no URL handler, so
# the member can run the one-time setup without hunting for the path.
BRIDGE_SETUP_PATH = r"dashboard_tstridge\install_bridge.ps1"


def _earnings_date(symbol: str) -> str | None:
    """Next earnings (ISO) — feeds the 'no earnings inside the trade' gate.

    Stays server-side: it is public data with no broker involved, so there is no
    reason to make every member's bridge fetch it.
    """
    try:
        from ..services.prices import fetch_next_earnings

        return (fetch_next_earnings(symbol) or {}).get("date")
    except Exception:  # noqa: BLE001
        return None


@router.get("/{symbol}", response_class=HTMLResponse)
def options_tab(symbol: str, request: Request,
                user: User = Depends(require_user)):
    """Shell for the tab. The browser fills it from the member's local bridge."""
    sym = (symbol or "").strip().upper()
    return templates.TemplateResponse(
        request, "_options_tab.html",
        {"user": user, "sym": sym, "bridge_port": BRIDGE_PORT,
         "dte_min": bull_put.DTE_MIN, "dte_max": bull_put.DTE_MAX, "diag": None,
         "bridge_setup_path": BRIDGE_SETUP_PATH},
    )


@router.post("/{symbol}/analyze", response_class=HTMLResponse)
def analyze(symbol: str, request: Request,
            payload: dict = Body(...),
            user: User = Depends(require_user),
            db: Session = Depends(get_db)):
    """Grade a chain the member's browser fetched from their own TWS.

    The body is whatever their bridge returned, so it is treated as untrusted
    input: every number is re-validated inside ``bull_put.select`` before use.
    """
    sym = (symbol or "").strip().upper()
    chain = payload.get("chain") or {}
    iv = payload.get("iv") or {}
    nlv = payload.get("nlv")
    try:
        nlv = float(nlv) if nlv is not None else None
    except (TypeError, ValueError):
        nlv = None

    ctx = {"user": user, "sym": sym, "chain": chain, "iv": iv, "nlv": nlv,
           "spread": None, "earnings": None, "level": None,
           "nlv_source": payload.get("nlv_source") or "account",
           # what the BROWSER reported when the loopback fetch failed — shown in
           # the panel, because "not running" and "browser blocked it" are
           # indistinguishable from the page and guessing between them cost time
           "diag": payload.get("diag") or None,
           "bridge_setup_path": BRIDGE_SETUP_PATH}

    if chain.get("ok") and chain.get("spot"):
        earnings = _earnings_date(sym)
        ctx["earnings"] = earnings
        ctx["level"] = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
        ctx["spread"] = bull_put.select(
            symbol=sym,
            spot=float(chain["spot"]),
            expiry=chain.get("expiry_label") or "",
            dte=int(chain.get("dte") or 0),
            puts=chain.get("puts") or [],
            iv_percentile=iv.get("iv_percentile"),
            earnings_date=earnings,
            net_liquidation=nlv,
        )

    return templates.TemplateResponse(request, "_options_analysis.html", ctx)


# ---------------------------------------------------------------- tracking
# Tracking only. Nothing below places, modifies or cancels an order — the fill
# happens in TWS by the member's own hand; this records what they say they
# opened so the delta line can be watched against it.

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
    db.add(OptionSpread(
        user_id=user.id, symbol=symbol.strip().upper(), strategy="bull_put",
        expiry=expiry, short_strike=short_strike, long_strike=long_strike,
        credit=credit or None, contracts=max(1, contracts),
        entry_delta=entry_delta or None, status="open",
    ))
    db.commit()
    return _positions(request, user, db, {})


@router.post("/{spread_id}/close", response_class=HTMLResponse)
def close_spread(spread_id: int, request: Request,
                 user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    row = (db.query(OptionSpread)
             .filter(OptionSpread.id == spread_id,
                     OptionSpread.user_id == user.id)   # scoped: never another member's
             .one_or_none())
    if row is not None:
        row.status = "closed"
        row.closed_at = _utcnow()
        db.commit()
    return _positions(request, user, db, {})


def _positions(request: Request, user: User, db: Session, deltas: dict) -> HTMLResponse:
    """Open spreads graded against the management rule.

    ``deltas`` maps spread id -> live |delta|, supplied by the browser from the
    member's own bridge. Without it the rows still render (DTE is computed here):
    seeing what you hold must never depend on the feed being up.
    """
    import datetime as _d

    rows = (db.query(OptionSpread)
              .filter(OptionSpread.user_id == user.id, OptionSpread.status == "open")
              .order_by(OptionSpread.opened_at.desc())
              .all())
    items = []
    for r in rows:
        try:
            dte = (_d.date.fromisoformat(r.expiry) - _d.date.today()).days
        except ValueError:
            dte = 0
        d = deltas.get(str(r.id), deltas.get(r.id))
        try:
            d = abs(float(d)) if d is not None else None
        except (TypeError, ValueError):
            d = None
        items.append({"row": r, "delta": d, "dte": dte, "error": None,
                      "verdict": bull_put.review(short_delta=d, dte=dte)})
    return templates.TemplateResponse(
        request, "_options_positions.html",
        {"user": user, "items": items, "bridge_port": BRIDGE_PORT},
    )


@router.get("/positions/all", response_class=HTMLResponse)
def positions(request: Request,
              user: User = Depends(require_user),
              db: Session = Depends(get_db)):
    """Open spreads without live deltas (first paint, and the TWS-off case)."""
    return _positions(request, user, db, {})


@router.post("/positions/grade", response_class=HTMLResponse)
def positions_grade(request: Request,
                    payload: dict = Body(...),
                    user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """Re-grade open spreads from deltas the browser read off its own bridge."""
    return _positions(request, user, db, payload.get("deltas") or {})
