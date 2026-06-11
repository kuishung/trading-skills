"""Studies — curated tickers + discussion (the collaboration core).

A curator (moderator/admin) adds a ticker as a Study with a rationale; members
discuss it (comments) and it moves draft -> discussing -> agreed -> closed.
Reuses the Setup + Comment models — one Study == one curated ticker. Members
view + comment; only moderators curate (create / set status / delete). Creating
a study also posts it to Discord (if a webhook is configured) — the curate->
discuss doorbell.
"""
from __future__ import annotations

import math
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Comment, MATPLevel, Setup, User
from ..security import require_admin, require_moderator, require_user
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


def _page_ctx(db: Session, user: User, sel) -> dict:
    """Context for the 3-panel Studies page: left basket cards (all studies, open
    first), the selected study + its level/R:R, statuses."""
    counts = dict(
        db.query(Comment.setup_id, func.count(Comment.id)).group_by(Comment.setup_id).all()
    )
    levels = {lv.symbol: lv for lv in db.query(MATPLevel).all()}
    basket = [
        {"id": x.id, "symbol": x.symbol, "status": x.status,
         "rr": _rr(x.entry, x.stop_loss, x.profit_target), "comments": counts.get(x.id, 0)}
        for x in db.query(Setup).all()
    ]
    basket.sort(key=lambda b: (_STATUS_RANK.get(b["status"], 9), b["symbol"]))
    return {
        "user": user, "basket": basket, "sel": sel,
        "level": levels.get(sel.symbol) if sel else None,
        "rr": _rr(sel.entry, sel.stop_loss, sel.profit_target) if sel else None,
        "statuses": STATUSES,
    }


@router.get("", response_class=HTMLResponse)
def studies_home(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Land straight on a study's chart (no separate list page): pick the newest
    open study (else the newest of any), or an empty state when there are none."""
    sel = (
        db.query(Setup).filter(Setup.status != "closed").order_by(Setup.id.desc()).first()
        or db.query(Setup).order_by(Setup.id.desc()).first()
    )
    return templates.TemplateResponse(request, "studies.html", _page_ctx(db, user, sel))


@router.get("/basket", response_class=HTMLResponse)
def studies_basket(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX fragment: the curation 'watchlist basket' — every OPEN (non-closed)
    study as a compact row with live price/signal + its trade plan + R:R. Lazy-
    loaded so the live price fetch never blocks the Studies page. Declared BEFORE
    GET /{sid} so '/basket' isn't captured as a study id."""
    from .matp import _watchlist_signals  # cached + bounded; reuse the MATP path

    open_setups = (
        db.query(Setup).filter(Setup.status != "closed").all()
    )
    levels = {lv.symbol: lv for lv in db.query(MATPLevel).all()}
    live = _watchlist_signals([s.symbol for s in open_setups])
    items = [
        {
            "s": s, "level": levels.get(s.symbol),
            "rr": _rr(s.entry, s.stop_loss, s.profit_target),
            "live": live.get(s.symbol, {}) or {},
        }
        for s in open_setups
    ]
    items.sort(key=lambda it: (_STATUS_RANK.get(it["s"].status, 9), it["s"].symbol))
    return templates.TemplateResponse(
        request, "_studies_basket.html", {"user": user, "items": items}
    )


def _strike_step(spot: float) -> float:
    """Listed-options-style strike spacing — picks the increment that yields
    ~22-28 strikes across the ±27.5% ladder the analyser shows (a $208 stock
    gets $5 strikes, like its real chain — not $10)."""
    raw = spot * 0.02
    for cand in (0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0):
        if raw <= cand:
            return cand
    return 200.0


@router.get("/api/black-scholes")
def black_scholes_grid(
    S: float,
    sigma_pct: float,
    days: float,
    r_pct: float = 4.5,
    q_pct: float = 0.0,
    kind: str = "call",
    target: float | None = None,
    user: User = Depends(require_user),
):
    """Strike-ladder JSON for the Studies option analyser (the UI feeds the
    inputs straight into ``services.black_scholes``). Per strike: premium,
    delta, risk-neutral P(expire ITM), P(profit) = P(expire beyond breakeven),
    and — when a target price is given — P/L + ROI if the underlying sits at
    the target on expiry day. ``best_strike`` maximises ROI x P(profit), so a
    lottery-ticket far-OTM strike can't win on ROI alone."""
    from ..services.black_scholes import black_scholes

    kind = "put" if (kind or "").strip().lower() == "put" else "call"
    if not (S > 0 and 0 < days <= 1095 and 0 < sigma_pct <= 500):
        return {"error": "Spot, DTE (max 1095) and IV must all be positive."}
    T = days / 365.0
    r, q, sigma = r_pct / 100.0, q_pct / 100.0, sigma_pct / 100.0
    step = _strike_step(S)
    K = max(step, math.floor(S * 0.725 / step) * step)
    rows = []
    while K <= S * 1.275 + 1e-9:
        bs = black_scholes(S, K, T, r, sigma, kind=kind, q=q)
        prem = bs.price
        be = K + prem if kind == "call" else K - prem
        prob_profit = (
            black_scholes(S, be, T, r, sigma, kind=kind, q=q).prob_itm if be > 0 else None
        )
        row = {
            "strike": round(K, 2),
            "premium": round(prem, 2),
            "delta": round(bs.delta, 2),
            "prob_itm": round(bs.prob_itm, 4),
            "prob_profit": round(prob_profit, 4) if prob_profit is not None else None,
            "breakeven": round(be, 2),
            "pl_target": None,
            "roi_target": None,
            "score": None,
        }
        if target is not None and target > 0 and prem >= 0.01:
            intrinsic = max(target - K, 0.0) if kind == "call" else max(K - target, 0.0)
            pl = intrinsic - prem
            row["pl_target"] = round(pl, 2)
            row["roi_target"] = round(pl / prem * 100.0, 1)
            if prob_profit is not None:
                row["score"] = round((pl / prem) * prob_profit, 4)
        rows.append(row)
        K = round(K + step, 10)
    atm = min(rows, key=lambda x: abs(x["strike"] - S))["strike"] if rows else None
    winners = [x for x in rows if x["score"] is not None and x["pl_target"] > 0]
    best = max(winners, key=lambda x: x["score"])["strike"] if winners else None
    return {"kind": kind, "atm_strike": atm, "best_strike": best, "rows": rows}


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
def study_page(
    sid: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The 3-panel study page with this study selected (left basket · middle
    chart+levels · right chatroom)."""
    sel = db.get(Setup, sid)
    if sel is None:
        return RedirectResponse("/studies", status_code=303)
    return templates.TemplateResponse(request, "studies.html", _page_ctx(db, user, sel))


def _render_chat(request: Request, db: Session, user: User, sid: int):
    """Render the right-panel chat fragment: the study's Discord thread messages
    MERGED with the on-platform comments, time-sorted as chat bubbles."""
    s = db.get(Setup, sid)
    msgs: list[dict] = []
    if s is not None:
        for m in (discord.fetch_thread_messages(s.discord_thread_id) if s.discord_thread_id else None) or []:
            text = m.get("content") or m.get("embed_text") or ("(attachment)" if m.get("has_attach") else "")
            msgs.append({"author": m["author"], "text": text, "ts": m["ts"],
                         "source": "discord", "me": False})
        for c in db.query(Comment).filter(Comment.setup_id == sid).all():
            msgs.append({
                "author": (c.user.display_name or c.user.email) if c.user else "member",
                "text": c.body,
                "ts": c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "",
                "source": "web", "me": (c.user_id == user.id),
            })
    msgs.sort(key=lambda x: x["ts"] or "")
    # ts is UTC wall-clock ("YYYY-MM-DD HH:MM:SS"); expose a UTC-marked ISO string
    # so the browser can render each message in the VIEWER's local time.
    for x in msgs:
        x["ts_iso"] = (x["ts"].replace(" ", "T") + "Z") if x["ts"] else ""
    return templates.TemplateResponse(
        request, "_study_chat.html",
        {
            "user": user, "s": s, "messages": msgs,
            "bot_configured": discord.bot_configured(),
            "thread_link": discord.thread_link(s.discord_thread_id) if s else None,
        },
    )


@router.get("/{sid}/chat", response_class=HTMLResponse)
def study_chat(
    sid: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Right-panel chatroom fragment (Discord + platform comments merged)."""
    return _render_chat(request, db, user, sid)


@router.post("/{sid}/comment", response_class=HTMLResponse)
def add_comment(
    sid: int,
    request: Request,
    body: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    s = db.get(Setup, sid)
    text = (body or "").strip()
    if s is not None and text:
        name = user.display_name or user.email or "member"
        # Two-way bridge: if this study has a Discord thread, the bot posts the
        # message INTO the thread (so it shows in Discord / the phone app) and the
        # chat reads it back — no platform Comment, so it isn't shown twice. If
        # there's no thread or the post fails, fall back to a platform comment so
        # the message is never lost.
        mirrored = False
        if s.discord_thread_id and discord.bot_configured():
            mirrored = discord.post_thread_message(s.discord_thread_id, f"{name}: {text[:1900]}")
        if not mirrored:
            db.add(Comment(setup_id=sid, user_id=user.id, body=text[:4000]))
            db.commit()
    # HTMX composer → return the refreshed chat fragment (instant, in-place);
    # no-JS fallback → full-page redirect back to the study.
    if request.headers.get("HX-Request"):
        return _render_chat(request, db, user, sid)
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
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin-only — deletes the study + its on-platform comments (the Discord
    thread + MATP data are untouched)."""
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
