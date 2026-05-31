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
    sel_band = None
    sel_targets = []
    sel_patterns = []
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
            incl = [t["target_price"] for t in sel_targets if t["included"]]
            sel_band = _build_band(
                latest.target_low, latest.target_high, sel.mbp, sel.matp,
                prices=incl, current=analysis["current"],
            )

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
            "sel_band": sel_band,
            "sel_targets": sel_targets,
            "sel_patterns": sel_patterns,
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
            "live": live,
        },
    )


@router.get("/runs", response_class=HTMLResponse)
def matp_runs(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """HTMX-polled fragment: the live 'active runs' panel (pending + running),
    newest first, with progress + who triggered it."""
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

    return templates.TemplateResponse(
        request, "_runs_panel.html", {"user": user, "items": items}
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
    """Analyst targets for `sym`, newest issue date first. `included`
    (post-earnings) is computed against `earn` so it never goes stale."""
    rows = (
        db.query(MATPTarget)
        .filter(MATPTarget.symbol == sym)
        .order_by(MATPTarget.target_date.desc())
        .all()
    )
    return [
        {
            "brokerage": t.brokerage,
            "target_price": t.target_price,
            "target_date": t.target_date,
            "included": bool(earn and t.target_date and t.target_date > earn),
        }
        for t in rows
    ]


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


def _build_band(low, high, mbp, matp, prices=None, current=None, bins=18):
    """Levels band. The DISPLAY range extends past the analyst low->high to
    include the current price (and MBP) so an out-of-range price is shown
    proportionately instead of clamped at the edge. The analyst range, the
    concentration heatmap, MBP/MATP, and the current price are all positioned as
    %-offsets within the display range. None unless we have a low->high range."""
    if low is None or high is None or high <= low:
        return None

    # display range = analyst range, extended to include current/mbp/matp, + pad
    los = [low] + [v for v in (current, mbp) if v is not None]
    his = [high] + [v for v in (current, matp) if v is not None]
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

    vals = [p for p in (prices or []) if p is not None]
    if vals:
        counts = [0] * bins
        arange = high - low
        for p in vals:
            idx = int((p - low) / arange * bins)
            counts[min(bins - 1, max(0, idx))] += 1
        mx = max(counts) or 1

        # consensus zone = densest contiguous run of bins; heatmap colours it only
        top = max(range(bins), key=lambda i: counts[i])
        lo_i = hi_i = top
        while lo_i - 1 >= 0 and counts[lo_i - 1] >= max(1, counts[top] * 0.5):
            lo_i -= 1
        while hi_i + 1 < bins and counts[hi_i + 1] >= max(1, counts[top] * 0.5):
            hi_i += 1

        def _heat(i, c):
            if not (lo_i <= i <= hi_i) or not c:
                return "transparent"
            hue = round(240 * (1 - c / mx))  # 240 blue -> 0 red
            return "hsl(%d, 100%%, 60%%)" % hue

        # each bin positioned within the DISPLAY range (lo_pct..hi_pct)
        band["bins"] = [
            {
                "lo_pct": pct(low + i / bins * arange),
                "hi_pct": pct(low + (i + 1) / bins * arange),
                "color": _heat(i, c),
                "count": c,
            }
            for i, c in enumerate(counts)
        ]
        band["consensus_lo"] = round(low + lo_i / bins * arange, 2)
        band["consensus_hi"] = round(low + (hi_i + 1) / bins * arange, 2)
        band["consensus_count"] = sum(counts[lo_i : hi_i + 1])
        band["n"] = len(vals)
    return band


@router.get("/{symbol}/prices")
def matp_prices(symbol: str, user: User = Depends(require_user)):
    """Daily OHLC for the price chart (lightweight-charts shape), fetched LIVE
    from Yahoo (cached ~10 min). Returns an empty list (not an error) on any
    failure so the chart degrades to an empty state."""
    from ..services.prices import fetch_daily_ohlc

    return {"symbol": symbol.strip().upper(), "bars": fetch_daily_ohlc(symbol)}


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
        incl_prices = [t["target_price"] for t in targets if t["included"]]
        band = _build_band(
            latest.target_low, latest.target_high, level.mbp, level.matp,
            prices=incl_prices,
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
