"""MATP board (Median Analyst Target Price) + per-ticker history.

GET /matp           -> current MATP/MBP per symbol (from MATPLevel)
GET /matp/{symbol}  -> how that symbol's MATP evolved (MATPHistory) + a chart

Data is pushed in by the Nous Hermes agent via /api/matp (this process runs no
LLM and does no scraping). Approved members only.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import (
    FinvizFilter,
    MATPHistory,
    MATPLevel,
    MATPRefreshRequest,
    MATPTarget,
    User,
)
from ..security import require_moderator, require_user
from ..services import discord

# request states that mean "the agent hasn't finished this yet"
_OPEN_STATES = ("pending", "running")

# Board sort: actionable bounce signals float to the top.
_SIGNAL_RANK = {"HOT": 0, "WARM": 1, "WATCHING": 2}


def _signal_key(lv):
    return (_SIGNAL_RANK.get((lv.signal or "").upper(), 3), lv.symbol)

router = APIRouter(prefix="/matp", tags=["matp"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _chart_context(db: Session, sel) -> dict:
    """The selected ticker's consensus band + analyst summary + runtime patterns.
    Shared by the full board (matp_home) and the lazy chart-pane swap (matp_chart)
    so clicking a ticker recomputes only THAT ticker, never the whole watchlist."""
    sel_band, sel_targets, sel_patterns = None, [], []
    if sel is not None:
        sel_targets = _ticker_targets(db, sel.symbol, sel.last_earnings_date)
        analysis = _ticker_analysis(sel.symbol)
        sel_patterns = analysis["patterns"]
        latest = (
            db.query(MATPHistory)
            .filter(MATPHistory.symbol == sel.symbol)
            .order_by(MATPHistory.as_of.desc())
            .first()
        )
        if latest is not None:
            incl = [
                {"brokerage": t["brokerage"], "price": t["target_price"]}
                for t in sel_targets if t["included"]
            ]
            sel_band = _build_band(
                latest.target_low, latest.target_high, sel.mbp, sel.matp,
                analysts=incl, current=analysis["current"],
            )
    return {"sel_band": sel_band, "sel_targets": sel_targets, "sel_patterns": sel_patterns}


@router.get("", response_class=HTMLResponse)
def matp_home(
    request: Request,
    symbol: str | None = None,  # ?symbol=NVDA -> show its chart inline on the board
    wl: str | None = None,      # ?wl=all | individual | <filter_id>
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    all_levels = db.query(MATPLevel).all()
    active = sorted(
        [lv for lv in all_levels if (lv.status or "active") == "active"],
        key=_signal_key,
    )
    dropped = sorted(
        [lv for lv in all_levels if (lv.status or "active") == "dropped"],
        key=lambda lv: lv.symbol,
    )
    filters = db.query(FinvizFilter).all()
    # filter id -> description, so the board can label which screen sourced a name
    filt_names = {f.id: f.description for f in filters}
    active_filters = [f for f in filters if f.is_active]

    # open (pending/running) refresh requests -> show status; suppress dup buttons
    open_reqs = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status.in_(_OPEN_STATES))
        .all()
    )
    open_symbols = {r.symbol for r in open_reqs if r.scope == "ticker" and r.symbol}
    open_filter_ids = {r.filter_id for r in open_reqs if r.scope == "filter"}

    # watchlist rail: group active tickers by their source filter (active order
    # carries through, so signals stay on top within each watchlist)
    by_filter: dict = {}
    for lv in active:
        by_filter.setdefault(lv.filter_id, []).append(lv)
    unfiled = by_filter.get(None, [])

    # selected watchlist: ?wl = all | individual | <filter_id>. "all" shows every
    # active ticker; "individual" shows the ad-hoc (no-filter) tickers.
    valid_ids = {str(f.id) for f in active_filters}
    sel_wl = (wl or "all").strip()
    if sel_wl == "individual":
        shown_tickers = list(unfiled)
    elif sel_wl in valid_ids:
        shown_tickers = list(by_filter.get(int(sel_wl), []))
    else:
        sel_wl = "all"
        shown_tickers = list(active)

    # selected ticker: ?symbol=… , else the first ticker of the shown watchlist
    # (so a chart shows on load without a click).
    sel = None
    if symbol:
        sym = symbol.strip().upper()
        sel = next((lv for lv in all_levels if lv.symbol == sym), None)
    if sel is None:
        sel = shown_tickers[0] if shown_tickers else (active[0] if active else None)

    # selected ticker's consensus band + analyst summary + live analysis
    ctx = _chart_context(db, sel)

    return templates.TemplateResponse(
        request,
        "matp.html",
        {
            "user": user,
            "active": active,
            "dropped": dropped,
            "filt_names": filt_names,
            "active_filters": active_filters,
            "open_reqs": open_reqs,
            "open_symbols": open_symbols,
            "open_filter_ids": open_filter_ids,
            "sel_wl": sel_wl,
            "sel": sel,
            **ctx,
        },
    )


@router.get("/ticker-search")
def ticker_search(q: str = "", user: User = Depends(require_user)):
    """Typeahead suggestions for the ad-hoc ticker box: US tickers + company
    names matching the query (Yahoo search). Returns [] on failure."""
    from ..services.prices import search_tickers

    return {"results": search_tickers(q)}


@router.get("/watchlist", response_class=HTMLResponse)
def matp_watchlist(
    request: Request,
    wl: str | None = None,    # all | individual | <filter_id>
    sym: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy-loaded watchlist grid with RUNTIME trend/signal + a price>MBP split.
    Loaded via HTMX after the page renders so computing live signals never
    freezes the main view (cached + bounded concurrency)."""
    all_levels = db.query(MATPLevel).all()
    active = sorted(
        [lv for lv in all_levels if (lv.status or "active") == "active"], key=_signal_key
    )
    dropped = sorted(
        [lv for lv in all_levels if (lv.status or "active") == "dropped"],
        key=lambda lv: lv.symbol,
    )
    active_filters = [f for f in db.query(FinvizFilter).all() if f.is_active]
    open_reqs = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status.in_(_OPEN_STATES))
        .all()
    )
    open_symbols = {r.symbol for r in open_reqs if r.scope == "ticker" and r.symbol}
    by_filter: dict = {}
    for lv in active:
        by_filter.setdefault(lv.filter_id, []).append(lv)
    unfiled = by_filter.get(None, [])

    valid_ids = {str(f.id) for f in active_filters}
    sel_wl = (wl or "all").strip()
    if sel_wl == "individual":
        shown = list(unfiled)
    elif sel_wl in valid_ids:
        shown = list(by_filter.get(int(sel_wl), []))
    else:
        shown = list(active)

    live = _watchlist_signals([lv.symbol for lv in shown])

    # "Refreshed" stamp for this watchlist: for a Finviz-filter watchlist it's
    # the filter's last completed run (advanced by the scheduled MATP run); for
    # All / Selective it falls back to the newest ticker as_of in the set.
    wl_refreshed = None
    if sel_wl in valid_ids:
        f = next((x for x in active_filters if str(x.id) == sel_wl), None)
        wl_refreshed = f.last_run_at if f else None
    if wl_refreshed is None:
        stamps = [lv.as_of for lv in shown if lv.as_of is not None]
        wl_refreshed = max(stamps) if stamps else None

    # split: disqualified = live price ABOVE the max-buy price (MBP)
    qualified, disqualified = [], []
    for lv in shown:
        cur = live.get(lv.symbol, {}).get("current")
        if cur is not None and lv.mbp is not None and cur > lv.mbp:
            disqualified.append(lv)
        else:
            qualified.append(lv)

    return templates.TemplateResponse(
        request,
        "_watchlist.html",
        {
            "user": user,
            "qualified": qualified,
            "disqualified": disqualified,
            "dropped": dropped,
            "open_symbols": open_symbols,
            "sel_sym": (sym or "").strip().upper(),
            "sel_wl": sel_wl,
            "live": live,
            "wl_refreshed": wl_refreshed,
        },
    )


@router.get("/chart", response_class=HTMLResponse)
def matp_chart(
    request: Request,
    symbol: str,
    wl: str | None = None,      # kept for the pushed URL only; not used server-side
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Just the chart pane for ONE ticker. Swapped into #chartPane via HTMX when a
    watchlist row is clicked, so selecting a ticker recomputes only that ticker —
    not the whole watchlist's live trend/signal grid (the slow part)."""
    sym = (symbol or "").strip().upper()
    sel = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    return templates.TemplateResponse(
        request,
        "_chart_pane.html",
        {"user": user, "sel": sel, **_chart_context(db, sel)},
    )


def _open_run_items(db: Session):
    """Build the active-runs list (pending + running), newest first, each tagged
    with how long it's waited/run and whether it looks stale."""
    runs = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status.in_(_OPEN_STATES))
        .order_by(MATPRefreshRequest.created_at.desc())
        .all()
    )
    now = _dt.datetime.now(_dt.timezone.utc)

    def _mins(ts):
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return max(0, int((now - ts).total_seconds() // 60))

    items = []
    for r in runs:
        waited = _mins(r.created_at)
        ran = _mins(r.claimed_at)
        # 'pending' too long, or 'running' with no progress for a while = likely stuck
        stale = (
            (r.status == "pending" and waited is not None and waited >= 15)
            or (r.status == "running" and ran is not None and ran >= 8 and not r.progress_done)
        )
        items.append({"r": r, "waited": waited, "ran": ran, "stale": stale})
    return items


def _runs_context(db: Session, user: User, wl: str = "", sym: str = "", poll: int = 0) -> dict:
    """Template context for the runs panel, including an adaptive self-poll
    cadence so the page doesn't refresh forever:
      - a run actively *running* → poll 5s (watch the progress bar)
      - a fresh *pending* run     → poll 10s (waiting for the agent to claim it)
      - everything stale, or no runs → poll_in = 0 → STOP auto-refreshing
        (a stale run means the agent likely isn't polling — hammering won't help;
        the manual ↻ refresh / ↻ retry buttons resume it).

    The cadence is deliberately calm (no fast progress-bar churn): we poll just
    often enough to NOTICE completion, then reload the board ONCE. `poll`
    distinguishes a self-poll from the initial page load, so an empty result
    from a poll means "a run just finished → reload the watchlist", whereas an
    empty result on first load just means nothing is running yet."""
    items = _open_run_items(db)
    running_live = any(it["r"].status == "running" and not it["stale"] for it in items)
    pending_live = any(it["r"].status == "pending" and not it["stale"] for it in items)
    poll_in = 10 if running_live else (20 if pending_live else 0)
    completed = bool(poll) and not items  # a watched run just finished
    return {
        "user": user, "items": items, "poll_in": poll_in,
        "wl": wl or "", "sym": sym or "", "completed": completed,
        "reload_url": "/matp/watchlist?wl=" + (wl or "") + "&sym=" + (sym or ""),
    }


@router.get("/runs", response_class=HTMLResponse)
def matp_runs(
    request: Request,
    wl: str = "",
    sym: str = "",
    poll: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX fragment: the active-runs panel. Self-polls calmly while a run is in
    flight, then reloads the board once when it finishes (see _runs_context)."""
    return templates.TemplateResponse(
        request, "_runs_panel.html", _runs_context(db, user, wl=wl, sym=sym, poll=poll)
    )


@router.post("/runs/{rid}/retry", response_class=HTMLResponse)
def retry_run(
    rid: int,
    request: Request,
    wl: str = Form(""),
    sym: str = Form(""),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Re-queue a stuck request so the agent's next poll picks it up again.

    The agent is outbound-only — we can't push it a 'go now'. All we can do is
    reset the request to a fresh 'pending' (clear the claim + progress and reset
    the wait clock); the agent re-claims it on its next poll. If NOTHING is
    polling, this won't help — that's a cron/agent problem, surfaced on /agent.
    """
    r = db.get(MATPRefreshRequest, rid)
    if r is not None and r.status in _OPEN_STATES:
        now = _dt.datetime.now(_dt.timezone.utc)
        r.status = "pending"
        r.claimed_at = None
        r.completed_at = None
        r.progress_done = None
        r.progress_total = None
        r.created_at = now  # reset the "waited Nm" clock
        r.note = "re-queued by " + (user.display_name or user.email or "a moderator")
        db.commit()
    return templates.TemplateResponse(
        request, "_runs_panel.html", _runs_context(db, user, wl=wl, sym=sym, poll=1)
    )


def _build_chart(points, width=600, height=170, pad=28):
    """points: list of (date_str, value) ascending by time. Returns an SVG-ready
    dict (polyline + area path + dots) or None if <2 points."""
    if len(points) < 2:
        return None
    vals = [v for _, v in points]
    vmin, vmax = min(vals), max(vals)
    span = (vmax - vmin) or 1.0
    n = len(points)
    iw, ih = width - 2 * pad, height - 2 * pad
    dots = []
    for i, (_, v) in enumerate(points):
        x = round(pad + iw * i / (n - 1), 1)
        y = round(pad + ih * (1 - (v - vmin) / span), 1)
        dots.append({"x": x, "y": y, "v": v})
    polyline = " ".join(f"{d['x']},{d['y']}" for d in dots)
    area = (
        f"M {dots[0]['x']},{height - pad} "
        + " ".join(f"L {d['x']},{d['y']}" for d in dots)
        + f" L {dots[-1]['x']},{height - pad} Z"
    )
    return {
        "width": width, "height": height, "pad": pad,
        "polyline": polyline, "area": area, "dots": dots,
        "vmin": vmin, "vmax": vmax,
        "first_date": points[0][0], "last_date": points[-1][0],
    }


def _ticker_targets(db: Session, sym: str, earn):
    """Analyst targets for `sym`, ordered **relevant-and-high first**: the
    included (post-earnings, MATP-counting) targets come first, then the dropped
    ones, and within each group by target price descending (highest first).
    `included` is computed against `earn` so it never goes stale."""
    rows = db.query(MATPTarget).filter(MATPTarget.symbol == sym).all()
    out = [
        {
            "brokerage": t.brokerage,
            "target_price": t.target_price,
            "target_date": t.target_date,
            "as_of": t.as_of,                       # when WE extracted/recorded it
            "included": bool(earn and t.target_date and t.target_date > earn),
        }
        for t in rows
    ]
    # relevant (included) first, then highest target first within each group
    out.sort(key=lambda t: (not t["included"], -(t["target_price"] or 0.0)))
    return out


@router.get("/{symbol}/targets", response_class=HTMLResponse)
def matp_targets_modal(
    symbol: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX fragment: analyst targets for a ticker, rendered into the pop-out
    modal (so the board/detail screen stays clean)."""
    sym = symbol.strip().upper()
    level = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    earn = level.last_earnings_date if level else None
    return templates.TemplateResponse(
        request,
        "_targets_modal.html",
        {"user": user, "symbol": sym, "targets": _ticker_targets(db, sym, earn), "earnings": earn},
    )


@router.post("/{symbol}/share-discord", response_class=HTMLResponse)
def share_discord(
    symbol: str,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Moderator 'Share to Discord' — post this ticker's pick (price, MATP, MBP,
    signal, next earnings) to the channel on demand. HTMX fragment result.
    Synchronous (user-initiated) and soft-fail."""
    if not discord.configured():
        return HTMLResponse('<span class="text-rose-300">No Discord webhook configured.</span>')
    from ..services.prices import fetch_daily_ohlc, fetch_next_earnings

    sym = symbol.strip().upper()
    lv = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    bars = fetch_daily_ohlc(sym)
    price = bars[-1]["close"] if bars else None
    ok = discord.post_embed(
        **discord.build_ticker_embed(
            symbol=sym,
            matp=lv.matp if lv else None, mbp=lv.mbp if lv else None,
            signal=lv.signal if lv else None, price=price,
            next_earnings=fetch_next_earnings(sym),
            last_earnings=lv.last_earnings_date if lv else None,
            note=f"Shared by {user.display_name or user.email}",
            title_prefix="📌 Pick", public_url=settings.public_url,
        )
    )
    return HTMLResponse(
        '<span class="text-emerald-300">Shared to Discord ✓</span>' if ok
        else '<span class="text-rose-300">Discord post failed — see server log.</span>'
    )


_ANALYSIS_CACHE: dict = {}   # symbol -> (expiry_epoch, result)
_ANALYSIS_TTL = 900.0        # 15 min — runtime trend/signal/patterns are cached


def _ticker_analysis(symbol: str) -> dict:
    """RUNTIME detection from the shared resources.patterns on live daily bars:
    trend (up/down/sideways), a bounce-style signal (HOT/WARM/WATCHING), pattern
    flags, and the current price. Cached per symbol (15 min) and soft-fail, so
    the watchlist can compute these without freezing the dashboard."""
    import time

    sym = symbol.strip().upper()
    hit = _ANALYSIS_CACHE.get(sym)
    if hit and hit[0] > time.time():
        return hit[1]

    out: dict = {"current": None, "patterns": [], "trend": None, "signal": None}
    try:
        from ..services import resources_bridge  # noqa: F401  (puts TradeHunter on sys.path)
        from ..services.prices import fetch_daily_ohlc

        raw = fetch_daily_ohlc(sym)
        if not raw:
            return out
        out["current"] = raw[-1]["close"]
        bars = [
            {"t": b["time"], "o": b["open"], "h": b["high"],
             "l": b["low"], "c": b["close"], "v": 0}
            for b in raw
        ]
        from resources import patterns

        d = (patterns.trend(bars) or {}).get("direction")          # up/down/sideways
        consol = (patterns.consolidation(bars) or {}).get("is_consol")
        flag = (patterns.bull_flag(bars) or {}).get("detected")
        out["trend"] = d
        if d == "up" and flag:
            out["signal"] = "HOT"
        elif d == "up" and consol:
            out["signal"] = "WARM"
        elif d == "up":
            out["signal"] = "WATCHING"
        pats = []
        if d in ("up", "down", "sideways"):
            pats.append({"name": "Trend", "value": d, "good": d == "up"})
        if consol:
            pats.append({"name": "Consolidation", "value": "tight range", "good": True})
        if flag:
            pats.append({"name": "Bull flag", "value": "", "good": True})
        out["patterns"] = pats
        _ANALYSIS_CACHE[sym] = (time.time() + _ANALYSIS_TTL, out)
    except Exception:
        pass
    return out


def _watchlist_signals(symbols) -> dict:
    """Runtime {symbol: {trend, signal}} for a list of tickers, computed in a
    bounded thread pool (cached). Bounded concurrency + the 15-min cache keep
    this from hammering Yahoo or blocking. Soft-fail per symbol."""
    from concurrent.futures import ThreadPoolExecutor

    syms = list(dict.fromkeys(s for s in symbols if s))  # unique, ordered
    out: dict = {}
    if not syms:
        return out

    def work(s):
        a = _ticker_analysis(s)
        return s, {"trend": a.get("trend"), "signal": a.get("signal"), "current": a.get("current")}

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for s, r in ex.map(work, syms):
                out[s] = r
    except Exception:
        out = {s: {"trend": None, "signal": None, "current": None} for s in syms}
    return out


def _build_band(low, high, mbp, matp, analysts=None, current=None):
    """Levels band. The DISPLAY range extends past the analyst low->high to
    include the current price (and MBP/MATP, and any analyst outlier) so an
    out-of-range value shows proportionately instead of clamped at the edge.
    Each analyst target becomes a vertical line (``band["lines"]``) positioned
    as a %-offset within the display range — clustered lines read as
    concentration, no heatmap needed. None unless we have a low->high range.

    ``analysts`` = list of ``{"brokerage": str, "price": float}`` (the included,
    post-earnings targets)."""
    if low is None or high is None or high <= low:
        return None

    analysts = [a for a in (analysts or []) if a.get("price") is not None]
    apx = [a["price"] for a in analysts]

    # display range = analyst range, extended to include current/mbp/matp + any
    # analyst outlier, + pad
    los = [low] + [v for v in (current, mbp) if v is not None] + ([min(apx)] if apx else [])
    his = [high] + [v for v in (current, matp) if v is not None] + ([max(apx)] if apx else [])
    dlo, dhi = min(los), max(his)
    pad = (dhi - dlo) * 0.04 or 1.0
    dlo -= pad
    dhi += pad
    dspan = dhi - dlo

    def pct(v):
        if v is None:
            return None
        return round(max(0.0, min(100.0, (v - dlo) / dspan * 100.0)), 2)

    band = {
        "low": low, "high": high, "mbp": mbp, "matp": matp, "current": current,
        "mbp_pct": pct(mbp), "matp_pct": pct(matp), "current_pct": pct(current),
        "low_pct": pct(low), "high_pct": pct(high),
    }

    # one vertical line per analyst target — density shows concentration.
    lines = [
        {"price": a["price"], "pct": pct(a["price"]),
         "brokerage": a.get("brokerage") or "Analyst"}
        for a in analysts
    ]
    lines.sort(key=lambda x: x["price"])
    band["lines"] = lines
    band["n"] = len(lines)
    return band


@router.get("/{symbol}/prices")
def matp_prices(symbol: str, user: User = Depends(require_user)):
    """Daily OHLC for the price chart (lightweight-charts shape), fetched LIVE
    from Yahoo (cached ~10 min), plus the next earnings date (cached, soft-fail).
    Returns an empty list / null on any failure so the chart degrades gracefully."""
    from ..services.prices import fetch_daily_ohlc, fetch_next_earnings, fetch_quote

    sym = symbol.strip().upper()
    return {
        "symbol": sym,
        "bars": fetch_daily_ohlc(sym),
        "next_earnings": fetch_next_earnings(sym),
        "quote": fetch_quote(sym),     # {name, price, change, change_pct} for the header
    }


@router.get("/{symbol}", response_class=HTMLResponse)
def matp_detail(
    symbol: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    sym = symbol.strip().upper()
    level = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
    history = (
        db.query(MATPHistory)
        .filter(MATPHistory.symbol == sym)
        .order_by(MATPHistory.as_of.asc())
        .all()
    )
    points = [(h.as_of.strftime("%Y-%m-%d") if h.as_of else "", h.matp) for h in history]
    chart = _build_chart(points)

    # evidence: the individual analyst targets, newest issue date first.
    earn = level.last_earnings_date if level else None
    targets = _ticker_targets(db, sym, earn)

    # levels band: MBP/MATP against the analyst low->high range (latest snapshot),
    # plus a consensus histogram from the post-earnings target prices.
    latest = history[-1] if history else None
    band = None
    if level and latest:
        incl_analysts = [
            {"brokerage": t["brokerage"], "price": t["target_price"]}
            for t in targets if t["included"]
        ]
        band = _build_band(
            latest.target_low, latest.target_high, level.mbp, level.matp,
            analysts=incl_analysts,
        )
    # archived runs that included this ticker (raw extraction, newest first)
    from ..services.matp_archive import runs_for_symbol

    archive_runs = []
    for r in runs_for_symbol(sym):
        it = r["item"]
        earn_r = it.get("last_earnings_date")
        rows_r = sorted(
            it.get("targets", []),
            key=lambda t: (t.get("target_date") or ""),
            reverse=True,
        )
        archive_runs.append(
            {
                "file": r["file"],
                "saved_at": r["saved_at"],
                "source": r["source"],
                "filter_desc": r["filter_desc"],
                "matp": it.get("matp"),
                "mbp": it.get("mbp"),
                "n_targets": it.get("n_targets"),
                "last_earnings_date": earn_r,
                "targets": [
                    {
                        "brokerage": t.get("brokerage"),
                        "target_price": t.get("target_price"),
                        "target_date": t.get("target_date"),
                        "included": bool(
                            earn_r and t.get("target_date") and t["target_date"] > earn_r
                        ),
                    }
                    for t in rows_r
                ],
            }
        )

    # latest ad-hoc refresh request for this ticker (status banner)
    last_req = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.scope == "ticker", MATPRefreshRequest.symbol == sym)
        .order_by(MATPRefreshRequest.created_at.desc())
        .first()
    )
    return templates.TemplateResponse(
        request,
        "matp_detail.html",
        {
            "user": user, "symbol": sym, "level": level,
            "history": list(reversed(history)),  # newest-first table
            "chart": chart,
            "band": band,
            "targets": targets,
            "earnings": earn,
            "last_req": last_req,
            "req_open": bool(last_req and last_req.status in _OPEN_STATES),
            "archive_runs": archive_runs,
            "discord_configured": discord.configured(),
        },
    )


# ---------------------------------------------------------------------------
# Ad-hoc refresh queue (moderators/admins) — enqueue only; the Nous Hermes
# agent polls /api/refresh-queue, does the work, and marks rows done.
# ---------------------------------------------------------------------------
def _enqueue(db: Session, scope: str, *, symbol=None, filter_id=None, user: User):
    """Create a pending request unless an identical one is already open."""
    q = db.query(MATPRefreshRequest).filter(
        MATPRefreshRequest.scope == scope,
        MATPRefreshRequest.status.in_(_OPEN_STATES),
    )
    q = q.filter(MATPRefreshRequest.symbol == symbol) if scope == "ticker" \
        else q.filter(MATPRefreshRequest.filter_id == filter_id)
    if q.first() is not None:
        return False  # already queued/running — don't duplicate
    db.add(
        MATPRefreshRequest(
            scope=scope, symbol=symbol, filter_id=filter_id,
            requested_by=user.id, status="pending",
        )
    )
    db.commit()
    return True


@router.post("/{symbol}/refresh")
def request_ticker_refresh(
    symbol: str,
    request: Request,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    sym = symbol.strip().upper()
    _enqueue(db, "ticker", symbol=sym, user=user)
    return RedirectResponse(url=f"/matp/{sym}", status_code=303)


@router.post("/run-ticker")
def run_ticker(
    request: Request,
    symbol: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ad-hoc single US ticker run — available to any approved member. Enqueues a
    ticker-scope request (no prune); the agent fetches just that ticker."""
    sym = (symbol or "").strip().upper()
    valid = 1 <= len(sym) <= 6 and all(c.isalpha() or c in ".-" for c in sym)
    if valid:
        _enqueue(db, "ticker", symbol=sym, user=user)
        return RedirectResponse(url=f"/matp?symbol={sym}", status_code=303)
    return RedirectResponse(url="/matp", status_code=303)


@router.post("/filter/{filter_id}/refresh")
def request_filter_refresh(
    filter_id: int,
    request: Request,
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    _enqueue(db, "filter", filter_id=filter_id, user=user)
    return RedirectResponse(url="/matp", status_code=303)


@router.post("/run-filter")
def run_filter_from_select(
    request: Request,
    filter_id: int = Form(...),
    next: str = Form("/matp"),
    user: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Filter-selector form (MATP board + admin console). Only active filters
    are runnable. Redirects back to the page that submitted (`next`, internal
    paths only)."""
    f = db.get(FinvizFilter, filter_id)
    if f is not None and f.is_active:
        _enqueue(db, "filter", filter_id=filter_id, user=user)
    dest = next if next.startswith("/") else "/matp"
    return RedirectResponse(url=dest, status_code=303)
