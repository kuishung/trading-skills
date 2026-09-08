"""Per-user chart drawings — the shapes a member draws on a price chart.

The drawing overlay in ``_price_chart.html`` used to persist to browser
localStorage, which meant the shapes were stranded on whichever PC drew them.
These two endpoints move them to the DB so they travel with the account.

  GET  /drawings/{symbol}  -> {"shapes": [...]}
  PUT  /drawings/{symbol}  -> replace this symbol's shapes, returns the stored list

Both are session-authenticated (``require_user``) and scoped by ``user_id`` on
every read and write — one member can never see or modify another's drawings.

The PUT body is REVALIDATED here rather than trusted: the client is the only
writer today, but a JSON column will happily swallow anything, and a malformed
or oversized blob would come back to break the chart for that user on every
page load. ``_clean_shapes`` keeps only well-formed shapes and caps the count.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ChartDrawing, User, _utcnow
from ..security import require_user

router = APIRouter(prefix="/drawings", tags=["drawings"])

SHAPE_TYPES = ("hline", "tline", "rect", "trade")
MAX_SHAPES = 200          # a chart past this is noise, not analysis
MAX_SYMBOL_LEN = 20


def _num(v) -> float | None:
    """Accept only real, finite numbers (JSON gives us bool-as-int otherwise)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):   # NaN / +-inf
        return None
    return f


def _clean_point(pt) -> dict | None:
    """A date-anchored point: {t: 'YYYY-MM-DD', o: <bar offset>, p: <price>}."""
    if not isinstance(pt, dict):
        return None
    t = pt.get("t")
    if not isinstance(t, str) or not (8 <= len(t) <= 32):
        return None
    o, p = _num(pt.get("o", 0)), _num(pt.get("p"))
    if p is None:
        return None
    return {"t": t, "o": o if o is not None else 0.0, "p": p}


def _clean_shapes(raw) -> list[dict]:
    """Drop anything malformed; keep at most MAX_SHAPES. Never raises."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for s in raw[:MAX_SHAPES]:
        if not isinstance(s, dict) or s.get("type") not in SHAPE_TYPES:
            continue
        if s["type"] == "hline":
            p = _num(s.get("p"))
            if p is not None:
                out.append({"type": "hline", "p": p})
            continue
        a, b = _clean_point(s.get("a")), _clean_point(s.get("b"))
        if s["type"] == "trade":
            # a = entry anchor (a.p IS the entry price), b = the box's right edge;
            # sl / pt are plain prices. All four must be present to be usable.
            sl, pt = _num(s.get("sl")), _num(s.get("pt"))
            if a and b and sl is not None and pt is not None:
                keep = {"type": "trade", "a": a, "b": b, "sl": sl, "pt": pt}
                # A setup is now DERIVED from a support/resistance level: lvl is the
                # level itself, off the percentage the entry sits away from it, kind
                # which side of price it is. Optional so setups saved before this
                # existed still load — the editor re-derives lvl from the entry when
                # it is missing. This whitelist DROPS unknown keys, so anything the
                # chart wants to persist has to be named here.
                lvl, off = _num(s.get("lvl")), _num(s.get("off"))
                if lvl is not None:
                    keep["lvl"] = lvl
                if off is not None and 0 <= off <= 20:
                    keep["off"] = off
                if s.get("kind") in ("support", "resistance"):
                    keep["kind"] = s["kind"]
                out.append(keep)
            continue
        if a and b:
            out.append({"type": s["type"], "a": a, "b": b})
    return out


def _norm_symbol(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    if not sym or len(sym) > MAX_SYMBOL_LEN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad symbol")
    return sym


# Declared BEFORE /{symbol}: otherwise "trade-offset" is captured as a ticker and
# this route is unreachable. (Same ordering trap as matp's /my routes.)
DEFAULT_ENTRY_OFFSET_PCT = 0.3


@router.get("/trade-offset")
def get_trade_offset(user: User = Depends(require_user)):
    """How far from a support/resistance level this member's entries sit, in percent.

    Per-user because it is a style, not a fact: one trader wants to be filled 0.1%
    off the level, another gives it half a percent of room. Stored in User.prefs
    rather than a new column — it is a single UI preference, and prefs is already
    where the sector filter lives.
    """
    prefs = getattr(user, "prefs", None) or {}
    try:
        pct = float(prefs.get("trade_entry_offset_pct"))
    except (TypeError, ValueError):
        pct = DEFAULT_ENTRY_OFFSET_PCT
    if not 0 <= pct <= 20:
        pct = DEFAULT_ENTRY_OFFSET_PCT
    return {"pct": pct, "default": DEFAULT_ENTRY_OFFSET_PCT}


@router.put("/trade-offset")
def put_trade_offset(payload: dict = Body(...),
                     user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    """Remember this member's entry offset. Bounded 0-20%: beyond that the "entry
    near the level" is no longer near the level, and a fat-fingered 300 would put
    every future setup somewhere absurd."""
    try:
        pct = float(payload.get("pct"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "pct must be a number"}
    if not 0 <= pct <= 20:
        return {"ok": False, "error": "pct must be between 0 and 20"}

    prefs = dict(getattr(user, "prefs", None) or {})
    prefs["trade_entry_offset_pct"] = pct
    user.prefs = prefs          # reassign: SQLAlchemy won't see an in-place mutation
    db.commit()
    return {"ok": True, "pct": pct}


@router.get("/{symbol}")
def get_drawings(symbol: str,
                 user: User = Depends(require_user),
                 db: Session = Depends(get_db)) -> dict:
    sym = _norm_symbol(symbol)
    row = (db.query(ChartDrawing)
             .filter(ChartDrawing.user_id == user.id, ChartDrawing.symbol == sym)
             .one_or_none())
    return {"symbol": sym, "shapes": (row.shapes if row else []) or []}


@router.put("/{symbol}")
def put_drawings(symbol: str,
                 payload: dict = Body(...),
                 user: User = Depends(require_user),
                 db: Session = Depends(get_db)) -> dict:
    """Replace this symbol's shapes for this user.

    Upsert is done query-then-update/insert in portable ORM (no SQLite-only
    INSERT OR REPLACE), per the platform's data-handling rule — the same code
    has to run unchanged against Postgres.
    """
    sym = _norm_symbol(symbol)
    shapes = _clean_shapes(payload.get("shapes"))
    row = (db.query(ChartDrawing)
             .filter(ChartDrawing.user_id == user.id, ChartDrawing.symbol == sym)
             .one_or_none())
    if row is None:
        # nothing to store and nothing stored -> don't create an empty row
        if not shapes:
            return {"symbol": sym, "shapes": []}
        row = ChartDrawing(user_id=user.id, symbol=sym, shapes=shapes)
        db.add(row)
    elif shapes:
        row.shapes = shapes
        row.updated_at = _utcnow()
    else:
        db.delete(row)          # cleared the chart -> drop the row
        db.commit()
        return {"symbol": sym, "shapes": []}
    db.commit()
    return {"symbol": sym, "shapes": shapes}
