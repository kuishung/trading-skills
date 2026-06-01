"""Studies — curated tickers + discussion (the collaboration core).

A curator (moderator/admin) adds a ticker as a Study with a rationale; members
discuss it (comments) and it moves draft -> discussing -> agreed -> closed.
Reuses the Setup + Comment models — one Study == one curated ticker. Members
view + comment; only moderators curate (create / set status / delete). Creating
a study also posts it to Discord (if a webhook is configured) — the curate->
discuss doorbell.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Comment, MATPLevel, Setup, User
from ..security import require_moderator, require_user
from ..services import discord

router = APIRouter(prefix="/studies", tags=["studies"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

STATUSES = ("draft", "discussing", "agreed", "closed")
# list order: live discussions first, then agreed, drafts, and closed last.
_STATUS_RANK = {"discussing": 0, "agreed": 1, "draft": 2, "closed": 3}


def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rr(entry, stop, target):
    """Reward:risk for a long setup = (target - entry) / (entry - stop).
    None unless all three are set and the risk is positive."""
    if entry is None or stop is None or target is None:
        return None
    risk = entry - stop
    reward = target - entry
    if risk <= 0:
        return None
    return round(reward / risk, 2)


@router.get("", response_class=HTMLResponse)
def list_studies(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    setups = db.query(Setup).all()
    levels = {lv.symbol: lv for lv in db.query(MATPLevel).all()}
    counts = dict(
        db.query(Comment.setup_id, func.count(Comment.id)).group_by(Comment.setup_id).all()
    )
    items = [
        {"s": s, "level": levels.get(s.symbol), "comments": counts.get(s.id, 0)}
        for s in setups
    ]
    items.sort(key=lambda it: (_STATUS_RANK.get(it["s"].status, 9), -it["s"].id))
    return templates.TemplateResponse(
        request, "studies.html", {"user": user, "items": items, "statuses": STATUSES}
    )


@router.post("")
def create_study(
    request: Request,
    symbol: str = Form(...),
    title: str = Form(...),
    rationale: str = Form(""),
    entry: str = Form(""),
    stop_loss: str = Form(""),
    profit_target: str = Form(""),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Curate a ticker (moderators). Opens it straight into 'discussing' and
    posts it to Discord (if configured)."""
    sym = (symbol or "").strip().upper()
    ttl = (title or "").strip()
    if not (sym and ttl):
        return RedirectResponse("/studies", status_code=303)
    s = Setup(
        symbol=sym, title=ttl, rationale=(rationale or "").strip() or None,
        entry=_to_float(entry), stop_loss=_to_float(stop_loss),
        profit_target=_to_float(profit_target),
        status="discussing", created_by=user.id,
    )
    db.add(s)
    db.commit()
    _post_new_study(db, s, user)
    _ensure_thread(db, s, user)
    return RedirectResponse(f"/studies/{s.id}", status_code=303)


@router.post("/curate")
def curate_from_matp(
    request: Request,
    symbol: str = Form(...),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """One-click curate from the MATP board/detail: create (or reopen) a study
    for this ticker and open it straight into discussion, then jump to it so the
    curator can mark support/resistance/entry/stop. Reuses an existing
    non-closed study for the same ticker instead of duplicating."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return RedirectResponse("/matp", status_code=303)
    existing = (
        db.query(Setup)
        .filter(Setup.symbol == sym, Setup.status != "closed")
        .order_by(Setup.id.desc())
        .first()
    )
    if existing is not None:
        return RedirectResponse(f"/studies/{existing.id}", status_code=303)
    s = Setup(symbol=sym, title=sym, status="discussing", created_by=user.id)
    db.add(s)
    db.commit()
    _post_new_study(db, s, user)
    _ensure_thread(db, s, user)
    return RedirectResponse(f"/studies/{s.id}", status_code=303)


@router.post("/{sid}/levels")
def set_levels(
    sid: int,
    support: str = Form(""),
    resistance: str = Form(""),
    entry: str = Form(""),
    stop_loss: str = Form(""),
    profit_target: str = Form(""),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Curator sets the study's horizontal levels + trade plan (R:R is derived
    at display time)."""
    s = db.get(Setup, sid)
    if s is not None:
        s.support = _to_float(support)
        s.resistance = _to_float(resistance)
        s.entry = _to_float(entry)
        s.stop_loss = _to_float(stop_loss)
        s.profit_target = _to_float(profit_target)
        db.commit()
    return RedirectResponse(f"/studies/{sid}", status_code=303)


def _post_new_study(db: Session, s: Setup, user: User) -> None:
    """Announce a new curated study to Discord (soft-fail, best-effort)."""
    if not discord.configured():
        return
    try:
        from ..services.prices import fetch_daily_ohlc, fetch_next_earnings

        lv = db.query(MATPLevel).filter(MATPLevel.symbol == s.symbol).first()
        bars = fetch_daily_ohlc(s.symbol)
        price = bars[-1]["close"] if bars else None
        discord.post_embed(
            **discord.build_ticker_embed(
                symbol=s.symbol,
                matp=lv.matp if lv else None, mbp=lv.mbp if lv else None,
                signal=lv.signal if lv else None, price=price,
                next_earnings=fetch_next_earnings(s.symbol),
                last_earnings=lv.last_earnings_date if lv else None,
                note=f"{s.title} — curated by {user.display_name or user.email}"
                + (f"\n{s.rationale}" if s.rationale else ""),
                title_prefix="📋 New study", public_url=settings.public_url,
                url=f"{settings.public_url}/studies/{s.id}",
            )
        )
    except Exception:  # noqa: BLE001 — never block curation on the notification
        pass


def _ensure_thread(db: Session, s: Setup, user: User) -> None:
    """Create the study's Discord thread (bot) if missing, so the study page can
    show its discussion. Soft-fail; never blocks curation."""
    if not discord.bot_configured() or s.discord_thread_id:
        return
    try:
        from ..services.prices import fetch_daily_ohlc, fetch_next_earnings

        lv = db.query(MATPLevel).filter(MATPLevel.symbol == s.symbol).first()
        bars = fetch_daily_ohlc(s.symbol)
        price = bars[-1]["close"] if bars else None
        embed = discord.build_ticker_embed(
            symbol=s.symbol,
            matp=lv.matp if lv else None, mbp=lv.mbp if lv else None,
            signal=lv.signal if lv else None, price=price,
            next_earnings=fetch_next_earnings(s.symbol),
            last_earnings=lv.last_earnings_date if lv else None,
            note=f"{s.title} — curated by {user.display_name or user.email}"
            + (f"\n{s.rationale}" if s.rationale else ""),
            title_prefix="📋 Study", public_url=settings.public_url,
            url=f"{settings.public_url}/studies/{s.id}",
        )
        tid = discord.create_study_thread(name=f"{s.symbol} — study #{s.id}", embed=embed)
        if tid:
            s.discord_thread_id = tid
            db.commit()
    except Exception:  # noqa: BLE001 — soft-fail
        pass


@router.get("/{sid}", response_class=HTMLResponse)
def study_detail(
    sid: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    s = db.get(Setup, sid)
    if s is None:
        return RedirectResponse("/studies", status_code=303)
    level = db.query(MATPLevel).filter(MATPLevel.symbol == s.symbol).first()
    comments = (
        db.query(Comment).filter(Comment.setup_id == sid).order_by(Comment.created_at.asc()).all()
    )
    return templates.TemplateResponse(
        request, "study_detail.html",
        {
            "user": user, "s": s, "level": level, "comments": comments,
            "statuses": STATUSES, "rr": _rr(s.entry, s.stop_loss, s.profit_target),
        },
    )


@router.post("/{sid}/comment")
def add_comment(
    sid: int,
    body: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    s = db.get(Setup, sid)
    text = (body or "").strip()
    if s is not None and text:
        db.add(Comment(setup_id=sid, user_id=user.id, body=text[:4000]))
        db.commit()
    return RedirectResponse(f"/studies/{sid}", status_code=303)


@router.post("/{sid}/status")
def set_status(
    sid: int,
    status: str = Form(...),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    s = db.get(Setup, sid)
    st = (status or "").strip().lower()
    if s is not None and st in STATUSES:
        s.status = st
        db.commit()
    return RedirectResponse(f"/studies/{sid}", status_code=303)


@router.post("/{sid}/delete")
def delete_study(
    sid: int,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    s = db.get(Setup, sid)
    if s is not None:
        db.delete(s)
        db.commit()
    return RedirectResponse("/studies", status_code=303)


@router.get("/{sid}/discord", response_class=HTMLResponse)
def study_discord(
    sid: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX fragment: the study's Discord-thread discussion (read via the bot).
    Self-polls so new Discord replies appear. Soft-fail to a friendly state."""
    s = db.get(Setup, sid)
    msgs = (
        discord.fetch_thread_messages(s.discord_thread_id)
        if (s and s.discord_thread_id) else None
    )
    return templates.TemplateResponse(
        request, "_discord_thread.html",
        {
            "user": user, "s": s, "messages": msgs,
            "bot_configured": discord.bot_configured(),
            "thread_link": discord.thread_link(s.discord_thread_id) if s else None,
        },
    )


@router.post("/{sid}/discord-thread")
def start_discord_thread(
    sid: int,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Manually create the Discord thread for a study that doesn't have one yet
    (e.g. curated before the bot was configured)."""
    s = db.get(Setup, sid)
    if s is not None:
        _ensure_thread(db, s, user)
    return RedirectResponse(f"/studies/{sid}", status_code=303)
