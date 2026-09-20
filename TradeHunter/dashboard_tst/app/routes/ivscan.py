"""Options > IV Rank — the member's TWS "High IV Rank" scanner as a watchlist.

The member already runs this scan in TWS (user, 2026-09-18): US stocks, **52-week
IV rank above 30**, price above 100, volume above 200K. It lists "quite a number
of tickers", and the work that follows is the same every time: which of these
are technically set up, and what does the chart look like? This page does that
part.

How the tickers get here
------------------------
The scan runs on the MEMBER'S OWN TWS, through the local IBKR bridge
(``bridge/ibkr_bridge.py`` ``/scan`` -> ``reqScannerData`` with scan code
``SCAN_ivRank52w_DESC`` and the ``ivRank52wAbove`` / ``priceAbove`` /
``volumeAbove`` filters - the API's names for exactly the TWS fields). The server
can never reach a member's TWS, so the BROWSER calls the bridge and posts the
symbols here (the same shape as the Options tab's chain flow). The scanner does
not return the IV rank figure itself, so the page then asks the bridge's
existing ``/iv`` for each ticker (a year of daily IV -> rank and percentile) and
files those too.

What this page adds
-------------------
* the list is kept per member (``iv_scan_items``), so it is there tomorrow
  without TWS running;
* every ticker is graded against the member's switched-on SETUP CONDITIONS
  (``services/ema_setup``, the same switches as Sector & Industry), sorted
  best-first with the non-qualifiers faded;
* clicking a ticker charts it on the platform's one chart component.

Two sources for the list (user, 2026-09-19)
-------------------------------------------
"currently in the option we are scanning the whole market with IV Rank. I need the
system to be able to present a list of tickers where I will only scan the IV and
setup within these prelisted tickers."

* **My list** - the member keeps a PRE-LISTED universe (``prefs.ivscan_universe``,
  pasted in, or topped up from My Watchlist). "Scan my list" loads exactly those
  tickers, reads each one's IV rank from TWS and grades the setup. No market
  scanner runs, and the price / volume floors do not apply: the member chose the
  names. The IV-rank floor still does - a ticker under it is kept on the list but
  faded and sorted below the ones that clear it, so "which of MY names is rich
  right now" reads at a glance.
* **Whole market** - the TWS scanner described above, unchanged.

The mode is remembered (``prefs.ivscan_mode``). Both fill the same
``iv_scan_items`` rows, so the list, the chart and the bull-put-spread tab do not
care which one produced it. The universe lives in prefs, not in those rows, so a
market scan can never wipe it.

Scope: screening only. Nothing here places an order.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import IVScanItem, User, _utcnow
from ..security import require_user
from ..services import ema_setup as es
from ..services import user_watchlist as uwl

router = APIRouter(prefix="/ivscan", tags=["ivscan"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

CRITERIA_PREF = "ivscan_criteria"
CONDS_PREF = "sym_conds"            # the SAME switches as Sector & Industry
DEFAULT_CRITERIA = {"iv_rank": 30.0, "price": 100.0, "volume": 200000}
UNIVERSE_PREF = "ivscan_universe"   # the member's pre-listed tickers: [symbol, ...]
MODE_PREF = "ivscan_mode"           # "list" (pre-listed tickers) | "market" (TWS scanner)
MAX_SYMBOLS = 100
IV_FRESH_HOURS = 20                 # an IV reading younger than this is not re-asked


def _clean_symbols(raw, cap: int = MAX_SYMBOLS) -> list[str]:
    """Browser input -> upper-cased, de-duplicated, capped tickers, order kept."""
    seen: set[str] = set()
    out: list[str] = []
    for s in raw or []:
        t = (s or "").strip().upper().replace(" ", ".")
        if t and len(t) <= 12 and t.replace(".", "").replace("-", "").isalnum() and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= cap:
            break
    return out


def _universe(user: User) -> list[str]:
    raw = (getattr(user, "prefs", None) or {}).get(UNIVERSE_PREF)
    return _clean_symbols(raw if isinstance(raw, list) else [])


def _mode(user: User) -> str:
    """The remembered source. Never chosen yet -> "list" once a universe exists,
    otherwise the market scan the page has always offered."""
    m = (getattr(user, "prefs", None) or {}).get(MODE_PREF)
    if m in ("list", "market"):
        return m
    return "list" if _universe(user) else "market"


def _replace_items(db: Session, user: User, syms: list[str]) -> None:
    """Make ``iv_scan_items`` hold exactly ``syms``, in that order. A ticker that
    stays keeps its IV reading, so reloading the list does not cost a TWS round
    trip per name."""
    keep = set(syms)
    old = {r.symbol: r for r in db.query(IVScanItem).filter(IVScanItem.user_id == user.id).all()}
    now = _utcnow()
    for sym, r in old.items():
        if sym not in keep:
            db.delete(r)
    for pos, sym in enumerate(syms):
        r = old.get(sym)
        if r is None:
            r = IVScanItem(user_id=user.id, symbol=sym)
            db.add(r)
        r.pos = pos
        r.scanned_at = now
    db.commit()


def _criteria(user: User) -> dict:
    raw = (getattr(user, "prefs", None) or {}).get(CRITERIA_PREF) or {}
    out = dict(DEFAULT_CRITERIA)
    for k in out:
        try:
            v = float(raw.get(k, out[k]))
            if v >= 0:
                out[k] = v
        except (TypeError, ValueError):
            pass
    out["volume"] = int(out["volume"])
    return out


def _age_hours(ts) -> float | None:
    """Hours since ``ts``. The column is a naive UTC datetime on SQLite and an
    aware one on Postgres, and ``_utcnow()`` is aware - so both sides are made
    naive-UTC before subtracting (mixing them raises)."""
    if ts is None:
        return None
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return (now - ts).total_seconds() / 3600.0


def _save_pref(db: Session, user: User, key: str, value) -> User:
    prefs = dict(getattr(user, "prefs", None) or {})
    prefs[key] = value
    user = db.merge(user)
    user.prefs = prefs
    db.commit()
    return user


def _list_context(db: Session, user: User, *, sort: str = "setup") -> dict:
    rows = (db.query(IVScanItem).filter(IVScanItem.user_id == user.id)
              .order_by(IVScanItem.pos).all())
    enabled = es.clean_enabled((getattr(user, "prefs", None) or {}).get(CONDS_PREF))
    mode = _mode(user)
    criteria = _criteria(user)
    floor = criteria["iv_rank"]
    items = []
    if rows:
        setups = es.setups_for_many([r.symbol for r in rows], deep=es.needs_deep(enabled))
        for r in rows:
            st = setups.get(r.symbol) or es._blank()
            fresh = _age_hours(r.iv_at) is not None and _age_hours(r.iv_at) < IV_FRESH_HOURS
            # My list: TWS has not pre-filtered these names, so the IV-rank floor is
            # applied HERE. An unread IV is "unknown", never "low".
            iv_low = mode == "list" and r.iv_rank is not None and r.iv_rank < floor
            items.append({"row": r, "setup": st, "rank": es.rank(st, enabled),
                          "iv_fresh": fresh, "iv_low": iv_low})
        # under-the-floor names sink in every sort order; False sorts before True
        if sort == "iv":
            items.sort(key=lambda it: (it["iv_low"], it["row"].iv_rank is None,
                                       -(it["row"].iv_rank or 0), it["row"].pos))
        elif sort == "scan":
            items.sort(key=lambda it: (it["iv_low"], it["row"].pos))
        else:
            items.sort(key=lambda it: (
                it["iv_low"],
                it["rank"]["score"] is None, -(it["rank"]["score"] or 0),
                it["setup"].get("above_pct") if it["setup"].get("above_pct") is not None else 99.0,
                it["row"].pos))
    scanned = max((r.scanned_at for r in rows if r.scanned_at), default=None)
    return {
        "user": user, "items": items, "sort": sort if sort in ("setup", "iv", "scan") else "setup",
        "enabled": enabled, "cond_labels": es.COND_LABELS, "cond_keys": es.COND_KEYS,
        "ranked": any(enabled.values()),
        "n_qualify": sum(1 for it in items
                         if it["rank"].get("qualifies") and not it["iv_low"]),
        "n_iv_ok": sum(1 for it in items
                       if it["row"].iv_rank is not None and not it["iv_low"]),
        "scanned_at": scanned, "criteria": criteria, "mode": mode, "iv_floor": floor,
        "n_universe": len(_universe(user)),
        "my_syms": uwl.symbol_set(db, user),
    }


@router.get("", response_class=HTMLResponse)
def ivscan_home(request: Request, user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "ivscan.html", {
        "user": user, "criteria": _criteria(user), "mode": _mode(user),
        "universe": _universe(user), "n_watchlist": len(uwl.symbol_set(db, user)),
        "max_symbols": MAX_SYMBOLS,
    })


@router.get("/list", response_class=HTMLResponse)
def ivscan_list(request: Request, sort: str = "setup",
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "_ivscan_list.html",
                                      _list_context(db, user, sort=sort))


class ScanIngest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    iv_rank: float = 30.0
    price: float = 100.0
    volume: float = 200000


@router.post("/ingest", response_class=HTMLResponse)
def ivscan_ingest(payload: ScanIngest, request: Request,
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Replace this member's list with a fresh scan. The body comes from the
    member's browser, so it is cleaned, de-duplicated and capped. IV readings
    already on file for a ticker that is still in the scan are kept."""
    _replace_items(db, user, _clean_symbols(payload.symbols))
    user = _save_pref(db, user, CRITERIA_PREF, {
        "iv_rank": max(0.0, float(payload.iv_rank)), "price": max(0.0, float(payload.price)),
        "volume": max(0, int(payload.volume))})
    user = _save_pref(db, user, MODE_PREF, "market")   # these rows came from the scanner
    return templates.TemplateResponse(request, "_ivscan_list.html", _list_context(db, user))


# ---- My list: the member's pre-listed universe ------------------------------

class ModeIn(BaseModel):
    mode: str = "list"


@router.post("/mode")
def ivscan_mode(payload: ModeIn, user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    """Remember which source the page uses. The page reloads itself afterwards."""
    mode = "market" if payload.mode == "market" else "list"
    _save_pref(db, user, MODE_PREF, mode)
    return {"ok": True, "mode": mode}


class UniverseIn(BaseModel):
    text: str = ""                   # whatever was typed: commas, spaces, new lines
    add_watchlist: bool = False      # also fold in My Watchlist


@router.post("/universe")
def ivscan_universe(payload: UniverseIn, user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """Save the pre-listed tickers. Returns the cleaned list, so the editor shows
    what was actually kept (duplicates, junk and anything past the cap dropped)."""
    import re

    typed = [t for t in re.split(r"[\s,;]+", payload.text or "") if t]
    if payload.add_watchlist:
        typed += sorted(uwl.symbol_set(db, user))
    asked = len({t.strip().upper() for t in typed})
    syms = _clean_symbols(typed)
    _save_pref(db, user, UNIVERSE_PREF, syms)
    return {"ok": True, "symbols": syms, "count": len(syms),
            "dropped": max(0, asked - len(syms)), "cap": MAX_SYMBOLS}


class UniverseScan(BaseModel):
    iv_rank: float = 30.0


@router.post("/scan-universe", response_class=HTMLResponse)
def ivscan_scan_universe(payload: UniverseScan, request: Request,
                         user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Load the pre-listed tickers as the list - no market scanner involved. The
    setup grading happens here (live daily bars); the page then reads each
    ticker's IV rank from the member's TWS and files it through /ivscan/iv."""
    _replace_items(db, user, _universe(user))
    crit = _criteria(user)
    crit["iv_rank"] = min(100.0, max(0.0, float(payload.iv_rank)))
    user = _save_pref(db, user, CRITERIA_PREF, crit)
    user = _save_pref(db, user, MODE_PREF, "list")
    return templates.TemplateResponse(request, "_ivscan_list.html", _list_context(db, user))


class IVReading(BaseModel):
    symbol: str
    iv_rank: float | None = None
    iv_percentile: float | None = None
    iv_current: float | None = None


@router.post("/iv")
def ivscan_iv(payload: IVReading, user: User = Depends(require_user),
              db: Session = Depends(get_db)):
    """File one ticker's IV rank / percentile (read from the member's TWS by the
    page). Bounded, because it arrives from a browser."""
    sym = (payload.symbol or "").strip().upper()
    r = (db.query(IVScanItem)
           .filter(IVScanItem.user_id == user.id, IVScanItem.symbol == sym).one_or_none())
    if r is None:
        return Response(status_code=404)

    def _b(v, lo, hi):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if lo <= f <= hi else None

    r.iv_rank = _b(payload.iv_rank, 0, 100)
    r.iv_pct = _b(payload.iv_percentile, 0, 100)
    r.iv_current = _b(payload.iv_current, 0, 1000)
    r.iv_at = _utcnow()
    db.commit()
    return {"ok": True}


@router.post("/conds", response_class=HTMLResponse)
async def ivscan_conds(request: Request, user: User = Depends(require_user),
                       db: Session = Depends(get_db)):
    """The setup switches - the same ones, stored under the same key, as Sector &
    Industry, so a member sets their technical setup once."""
    form = await request.form()
    enabled = {k: form.get(k) is not None for k in es.COND_KEYS}
    user = _save_pref(db, user, CONDS_PREF, enabled)
    return templates.TemplateResponse(
        request, "_ivscan_list.html", _list_context(db, user, sort=form.get("sort") or "setup"))


@router.post("/clear", response_class=HTMLResponse)
def ivscan_clear(request: Request, user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    db.query(IVScanItem).filter(IVScanItem.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    return templates.TemplateResponse(request, "_ivscan_list.html", _list_context(db, user))
