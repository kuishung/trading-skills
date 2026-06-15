"""Pattern Trainer — teach a chart pattern by example on a real (parquet-backed)
chart, then (later phases) generate a Markdown spec + a Python detector and scan
the universe for it.

Architecture (see PATTERN_TRAINER_DESIGN.md):
  - Chart data is loaded FROM PARQUET (daily/3min/1min) via resources.bars_store.
    This is an OFFLINE training tool, so parquet is the correct source (see the
    CARVE-OUT in CLAUDE.md's parquet scope rule).
  - The teaching chat injects the marked region's OHLCV into the prompt
    (services/pattern_llm.py, DeepSeek-direct) — the model reasons over the bars.
  - Phase 1 (this file): page + chart + teaching chat. Phase 2: generate
    pattern.md + detect.py. Phase 3: the "Find pattern" universe scan.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    PT_ARCHIVED,
    PT_LEARNING,
    Pattern,
    PatternLesson,
    User,
)
from ..security import require_user
from ..services import pattern_llm

# Make the sibling resources/ package importable (parquet bars store live there).
_TRADEHUNTER_ROOT = Path(__file__).resolve().parents[3]
if str(_TRADEHUNTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRADEHUNTER_ROOT))
try:
    from resources import bars_store  # noqa: E402
except Exception:  # pragma: no cover - surfaced at request time
    bars_store = None

router = APIRouter(prefix="/patterns", tags=["patterns"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_TIMEFRAMES = ("daily", "3min", "1min")
# default lookback window per timeframe (keeps the payload bounded)
_WINDOW_DAYS = {"daily": 1825, "3min": 60, "1min": 10}
# cap the default-window bar count per timeframe (bounds the JSON payload)
_MAX_BARS = {"daily": 2500, "3min": 5000, "1min": 3000}


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "pattern"


def _unique_slug(db: Session, base: str) -> str:
    slug, i = base, 2
    while db.query(Pattern).filter(Pattern.slug == slug).first() is not None:
        slug = f"{base}-{i}"
        i += 1
    return slug


def _get_pattern(db: Session, pattern_id: int, user: User) -> Pattern:
    p = db.get(Pattern, pattern_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if p.owner_id != user.id and not user.can_moderate:
        raise HTTPException(status_code=403, detail="Not your pattern")
    return p


def _to_lwc_time(t_iso: str, tf: str):
    """Convert a bars_store ISO timestamp to a Lightweight-Charts time value:
    'YYYY-MM-DD' for daily, epoch seconds (UTC) for intraday."""
    if tf == "daily":
        return t_iso[:10]
    dt = _dt.datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
    return int(dt.timestamp())


def _load_window(symbol: str, tf: str, start: str | None, end: str | None) -> list[dict]:
    """Load bars for symbol@tf. With explicit start+end (a marked region) load
    exactly that range. Otherwise load the recent default window for that
    timeframe (bounded by days + a bar-count cap).

    NB: we deliberately do NOT use `available_range_fast` to find the window —
    it returns None for parquet files written without `t` column statistics
    (the intraday seeds), which would wrongly yield "no bars". We read the file
    and slice from the actual last bar instead, so it works regardless."""
    if bars_store is None:
        return []
    symbol = symbol.upper().strip()
    if not symbol:
        return []
    if start and end:
        return bars_store.load_bars(symbol, start=start, end=end, timeframe=tf)

    bars = bars_store.load_bars(symbol, timeframe=tf)
    if not bars:
        return []
    last_iso = bars[-1]["t"]
    try:
        last_dt = _dt.datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
        cutoff = (last_dt - _dt.timedelta(days=_WINDOW_DAYS.get(tf, 60))).isoformat()
        bars = [b for b in bars if b["t"] >= cutoff]
    except Exception:
        pass
    return bars[-_MAX_BARS.get(tf, 5000):]


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
def patterns_home(request: Request, user: User = Depends(require_user),
                  db: Session = Depends(get_db)):
    q = db.query(Pattern).filter(Pattern.status != PT_ARCHIVED)
    if not user.can_moderate:
        q = q.filter(Pattern.owner_id == user.id)
    patterns = q.order_by(Pattern.updated_at.desc()).all()
    return templates.TemplateResponse(
        request, "patterns.html",
        {"user": user, "patterns": patterns,
         "chat_ready": pattern_llm.is_configured(),
         "bars_ok": bars_store is not None},
    )


@router.post("")
def create_pattern(request: Request, name: str = Form(...),
                   description: str = Form(""),
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/patterns", status_code=303)
    slug = _unique_slug(db, _slugify(name))
    p = Pattern(owner_id=user.id, name=name[:120], slug=slug,
                description=(description.strip()[:400] or None), status=PT_LEARNING)
    db.add(p)
    db.commit()
    return RedirectResponse(url=f"/patterns/{p.id}", status_code=303)


@router.get("/{pattern_id}", response_class=HTMLResponse)
def pattern_detail(pattern_id: int, request: Request,
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    return templates.TemplateResponse(
        request, "pattern_detail.html",
        {"user": user, "p": p, "lessons": p.lessons, "timeframes": _TIMEFRAMES,
         "chat_ready": pattern_llm.is_configured(),
         "bars_ok": bars_store is not None},
    )


# --------------------------------------------------------------------------- #
# Chart data (parquet)
# --------------------------------------------------------------------------- #
@router.get("/{pattern_id}/bars")
def pattern_bars(pattern_id: int,
                 symbol: str = Query(...),
                 tf: str = Query("daily"),
                 start: str | None = Query(None),
                 end: str | None = Query(None),
                 user: User = Depends(require_user), db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    if tf not in _TIMEFRAMES:
        tf = "daily"
    if bars_store is None:
        return JSONResponse({"ok": False, "error": "bars store unavailable", "bars": []},
                            status_code=503)
    raw = _load_window(symbol, tf, start, end)
    bars = [{"time": _to_lwc_time(b["t"], tf), "t": b["t"],
             "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
             "volume": b["v"]} for b in raw]

    # remember where the user was teaching, so the page reopens here
    p.chart_symbol = symbol.upper().strip()[:20]
    p.chart_timeframe = tf
    db.commit()

    return JSONResponse({"ok": True, "symbol": symbol.upper().strip(), "tf": tf,
                         "count": len(bars), "bars": bars})


# --------------------------------------------------------------------------- #
# Teaching chat
# --------------------------------------------------------------------------- #
def _marked_context(symbol: str | None, tf: str | None,
                    start: str | None, end: str | None) -> dict | None:
    """Re-load the marked region's bars from parquet for prompt injection."""
    if not (symbol and tf and start and end):
        return None
    bars = _load_window(symbol, tf, start, end)
    if not bars:
        return None
    return {"symbol": symbol.upper().strip(), "timeframe": tf, "bars": bars,
            "start": start, "end": end, "n": len(bars)}


def _do_chat(db: Session, p: Pattern, msg: str, ctx: dict | None) -> dict:
    next_seq = (max((m.seq for m in p.lessons), default=-1)) + 1
    marked_meta = None
    if ctx:
        marked_meta = {"symbol": ctx["symbol"], "timeframe": ctx["timeframe"],
                       "start": ctx["start"], "end": ctx["end"], "n": ctx["n"]}
    db.add(PatternLesson(pattern_id=p.id, seq=next_seq, role="user",
                         content=msg, marked=marked_meta))
    db.commit()

    history = [{"role": m.role, "content": m.content}
               for m in db.query(PatternLesson)
               .filter(PatternLesson.pattern_id == p.id)
               .order_by(PatternLesson.seq).all()]
    reply = pattern_llm.chat(history, context=ctx)

    db.add(PatternLesson(pattern_id=p.id, seq=next_seq + 1, role="assistant",
                         content=reply["content"]))
    db.commit()
    return reply


@router.post("/{pattern_id}/chat.json")
def post_chat_json(pattern_id: int,
                   message: str = Form(...),
                   symbol: str = Form(""), tf: str = Form(""),
                   start: str = Form(""), end: str = Form(""),
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    msg = message.strip()
    if not msg:
        return JSONResponse({"ok": False, "content": "", "error": "empty message"},
                            status_code=400)
    ctx = _marked_context(symbol or None, tf or None, start or None, end or None)
    reply = _do_chat(db, p, msg, ctx)
    return JSONResponse({"ok": bool(reply.get("ok", True)),
                         "content": reply.get("content", ""),
                         "marked": bool(ctx)})


@router.post("/{pattern_id}/chat")
def post_chat(pattern_id: int, message: str = Form(...),
              symbol: str = Form(""), tf: str = Form(""),
              start: str = Form(""), end: str = Form(""),
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    """No-JS fallback (full-page POST)."""
    p = _get_pattern(db, pattern_id, user)
    msg = message.strip()
    if msg:
        ctx = _marked_context(symbol or None, tf or None, start or None, end or None)
        _do_chat(db, p, msg, ctx)
    return RedirectResponse(url=f"/patterns/{p.id}#bottom", status_code=303)


@router.post("/{pattern_id}/archive")
def archive_pattern(pattern_id: int, user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    p.status = PT_ARCHIVED
    db.commit()
    return RedirectResponse(url="/patterns", status_code=303)
