"""Machine-to-machine API — authenticated by a shared key (NOT a user session).

This is the seam between the **Nous Hermes agent** (the LLM/web-research worker
on the Linux box) and TradeHunter. The agent does the fuzzy work (browse Finviz
+ MarketBeat, compute the faithful post-earnings MATP via DeepSeek) and pushes
the results here; this process only validates + stores them. No LLM runs in
TradeHunter.

Endpoints (header `X-API-Key: <TST_INGEST_API_KEY>` required):
  GET  /api/filters  -> active Finviz filter URLs the agent should screen
  POST /api/matp     -> upsert computed MATP/MBP levels into MATPLevel
"""
from __future__ import annotations

import datetime as _dt
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import (
    FinvizFilter,
    MATPHistory,
    MATPLevel,
    MATPRefreshRequest,
    MATPTarget,
)

router = APIRouter(prefix="/api", tags=["api"])


def require_api_key(x_api_key: str | None = Header(default=None)) -> bool:
    """Gate machine endpoints on a shared key. 503 if the key isn't configured
    (feature off by default); 401 on a missing/wrong key (constant-time compare)."""
    if not settings.ingest_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Ingest API not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.ingest_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return True


# ---------------------------------------------------------------------------
@router.get("/filters")
def list_active_filters(_: bool = Depends(require_api_key), db: Session = Depends(get_db)):
    """The active Finviz screener filters whose tickers the agent should refresh."""
    rows = db.query(FinvizFilter).filter(FinvizFilter.is_active.is_(True)).all()
    # `id` lets the agent post each run back with filter_id so we can diff the
    # universe (mark tickers that fell out of the screen as 'dropped').
    return {"filters": [{"id": f.id, "description": f.description, "url": f.url} for f in rows]}


# ---------------------------------------------------------------------------
# Ad-hoc refresh queue — collaborators (moderators/admins) enqueue requests in
# the web UI; the agent polls here, does the work, pushes via /api/matp, then
# marks each request done/failed.
@router.get("/refresh-queue")
def refresh_queue(_: bool = Depends(require_api_key), db: Session = Depends(get_db)):
    """Pending ad-hoc MATP refresh requests for the agent to action."""
    rows = (
        db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.status == "pending")
        .order_by(MATPRefreshRequest.created_at.asc())
        .all()
    )
    out = []
    for r in rows:
        item = {"id": r.id, "scope": r.scope, "symbol": r.symbol, "filter_id": r.filter_id}
        if r.scope == "filter" and r.filter is not None:
            item["filter_url"] = r.filter.url
            item["filter_description"] = r.filter.description
        out.append(item)
    return {"requests": out}


_REQUEST_STATES = {"running", "done", "failed"}


class RefreshStatusIn(BaseModel):
    status: str  # running | done | failed
    note: str | None = None


@router.post("/refresh-queue/{rid}/status")
def update_refresh_status(
    rid: int,
    payload: RefreshStatusIn,
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Agent reports progress: 'running' when it starts, 'done'/'failed' after."""
    if payload.status not in _REQUEST_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad status {payload.status!r}")
    r = db.get(MATPRefreshRequest, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such request")
    now = _dt.datetime.now(_dt.timezone.utc)
    r.status = payload.status
    if payload.note is not None:
        r.note = payload.note
    if payload.status == "running" and r.claimed_at is None:
        r.claimed_at = now
    if payload.status in ("done", "failed"):
        r.completed_at = now
    db.commit()
    return {"ok": True, "id": rid, "status": r.status}


# ---------------------------------------------------------------------------
class TargetIn(BaseModel):
    target_price: float
    brokerage: str | None = None
    target_date: str | None = None  # "YYYY-MM-DD" — the analyst's issue date


class MatpItem(BaseModel):
    symbol: str
    matp: float
    mbp: float | None = None  # defaults to matp / 1.15 if omitted
    exchange: str | None = None
    last_earnings_date: str | None = None  # "YYYY-MM-DD"
    n_targets: int | None = None
    trend: str | None = None
    # distribution of the post-earnings targets (B)
    target_high: float | None = None
    target_low: float | None = None
    target_mean: float | None = None
    # actionable bounce signal (EMA20/EMA50 bounce in an uptrend; target = MATP)
    signal: str | None = None  # HOT | WARM | WATCHING
    signal_entry: float | None = None
    signal_stop: float | None = None
    signal_target: float | None = None
    signal_rr: float | None = None
    # the evidence — every analyst target found (C); included is computed on read
    targets: list[TargetIn] = Field(default_factory=list)


class MatpIngest(BaseModel):
    items: list[MatpItem] = Field(default_factory=list)
    source: str = "nous_hermes"  # provenance stamped onto history rows
    # Finviz-filter drift: when the agent posts a full universe for one filter,
    # it passes that filter's id and prune=True. Tickers previously tied to that
    # filter but ABSENT from this run are marked 'dropped' (never deleted).
    # Per-ticker ad-hoc refreshes leave filter_id unset / prune=False.
    filter_id: int | None = None
    prune: bool = False


@router.post("/matp")
def ingest_matp(
    payload: MatpIngest,
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Upsert the current MATP/MBP per symbol AND append a de-duped history row
    (only when the MATP value changes). MBP defaults to MATP/1.15."""
    now = _dt.datetime.now(_dt.timezone.utc)
    upserted = 0
    appended = 0
    targets_added = 0
    posted_syms: set[str] = set()
    for it in payload.items:
        sym = it.symbol.strip().upper()
        if not sym:
            continue
        posted_syms.add(sym)
        mbp = it.mbp if it.mbp is not None else round(it.matp / 1.15, 2)

        # current snapshot (one row per symbol)
        row = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
        if row is None:
            row = MATPLevel(symbol=sym)
            db.add(row)
        if it.exchange:
            row.exchange = it.exchange
        if it.last_earnings_date:
            row.last_earnings_date = it.last_earnings_date
        if it.trend:
            row.trend = it.trend
        if it.n_targets is not None:
            row.n_targets = it.n_targets
        row.matp = it.matp
        row.mbp = mbp
        # membership: anything in this run is active and seen now.
        row.status = "active"
        row.last_seen_at = now
        if payload.filter_id is not None:
            row.filter_id = payload.filter_id
        # actionable signal — only touch when this run actually computed one,
        # so a MATP-only run doesn't wipe a signal set by the daily bounce job
        # (and vice-versa). The bounce producer sends signal + the 4 numbers.
        if it.signal is not None:
            row.signal = it.signal
            row.signal_entry = it.signal_entry
            row.signal_stop = it.signal_stop
            row.signal_target = it.signal_target
            row.signal_rr = it.signal_rr
        row.as_of = now
        upserted += 1

        # history: append only when the median changed (carry the distribution)
        last = (
            db.query(MATPHistory)
            .filter(MATPHistory.symbol == sym)
            .order_by(MATPHistory.as_of.desc())
            .first()
        )
        if last is None or last.matp != it.matp:
            db.add(
                MATPHistory(
                    symbol=sym, matp=it.matp, mbp=mbp,
                    last_earnings_date=it.last_earnings_date,
                    n_targets=it.n_targets,
                    target_high=it.target_high, target_low=it.target_low,
                    target_mean=it.target_mean,
                    source=payload.source, as_of=now,
                )
            )
            appended += 1

        # evidence: insert only genuinely-new analyst targets (unique key),
        # so re-pushing the same list each run doesn't duplicate.
        if it.targets:
            seen = {
                (b, d, p)
                for (b, d, p) in db.query(
                    MATPTarget.brokerage, MATPTarget.target_date, MATPTarget.target_price
                ).filter(MATPTarget.symbol == sym)
            }
            for tg in it.targets:
                key = (tg.brokerage, tg.target_date, tg.target_price)
                if key in seen:
                    continue
                seen.add(key)
                db.add(
                    MATPTarget(
                        symbol=sym, brokerage=tg.brokerage,
                        target_price=tg.target_price, target_date=tg.target_date,
                        as_of=now,
                    )
                )
                targets_added += 1

    # universe drift: a full-universe run for one filter marks same-filter
    # tickers that fell out of the screen as 'dropped' (data retained).
    dropped = 0
    if payload.prune and payload.filter_id is not None and posted_syms:
        dropped = (
            db.query(MATPLevel)
            .filter(
                MATPLevel.filter_id == payload.filter_id,
                MATPLevel.status == "active",
                MATPLevel.symbol.notin_(posted_syms),
            )
            .update({MATPLevel.status: "dropped"}, synchronize_session=False)
        )
    db.commit()
    return {
        "ok": True, "upserted": upserted,
        "history_appended": appended, "targets_added": targets_added,
        "dropped": dropped,
    }
