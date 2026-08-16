"""Machine-to-machine API — authenticated by a shared key (NOT a user session).

This is the seam between the **Nous Hermes agent** (the LLM/web-research worker
on the Linux box) and TradeHunter. The agent does the fuzzy work (browse Finviz
+ MarketBeat, compute the faithful post-earnings MATP via DeepSeek) and pushes
the results here; this process only validates + stores them. No LLM runs in
TradeHunter.

Endpoints (header `X-API-Key: <TST_INGEST_API_KEY>` required):
  GET  /api/filters         -> active Finviz filter URLs the agent should screen
  POST /api/matp            -> upsert computed MATP/MBP levels into MATPLevel
  POST /api/agent/heartbeat -> agent liveness + its crontab (shown on /agent)
"""
from __future__ import annotations

import datetime as _dt
import hmac

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from datetime import timedelta

from ..config import settings
from ..db import get_db
from ..models import (
    CA_SECTIONS,
    RUN_INTERVALS,
    AgentHeartbeat,
    CompanyAnalysis,
    EdgarIngestHealth,
    FinvizFilter,
    IngestHealth,
    MATPHistory,
    MATPLevel,
    MATPRefreshRequest,
    MATPTarget,
    _utcnow,
    get_selective_schedule,
)
from ..services import discord

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
def _manual_tickers(db: Session) -> list[str]:
    """Ad-hoc 'individual' watchlist symbols — active MATPLevels with no source
    filter. The agent should refresh these alongside the screen filters (no URL)."""
    rows = (
        db.query(MATPLevel)
        .filter(MATPLevel.filter_id.is_(None),
                (MATPLevel.status == "active") | (MATPLevel.status.is_(None)))
        .order_by(MATPLevel.symbol)
        .all()
    )
    return [r.symbol for r in rows]


@router.get("/filters")
def list_active_filters(_: bool = Depends(require_api_key), db: Session = Depends(get_db)):
    """The active Finviz screener filters whose tickers the agent should refresh,
    plus `manual_tickers` — ad-hoc names (no source filter) to refresh too."""
    rows = db.query(FinvizFilter).filter(FinvizFilter.is_active.is_(True)).all()
    # `id` lets the agent post each run back with filter_id so we can diff the
    # universe (mark tickers that fell out of the screen as 'dropped').
    return {
        "filters": [{"id": f.id, "description": f.description, "url": f.url} for f in rows],
        "manual_tickers": _manual_tickers(db),
    }


@router.get("/due-filters")
def due_filters(_: bool = Depends(require_api_key), db: Session = Depends(get_db)):
    """Active filters whose SCHEDULE is due (next_run_at <= now). The agent runs
    these full-universe on its poll cron; the finalizing /api/matp push advances
    each filter's schedule. Interval is set per filter in the dashboard.
    `manual_tickers` (ad-hoc, no-filter names) are returned so a routine poll
    refreshes them too."""
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = (
        db.query(FinvizFilter)
        .filter(FinvizFilter.is_active.is_(True), FinvizFilter.run_interval != "off")
        .all()
    )
    due = []
    for f in rows:
        nxt = f.next_run_at
        if nxt is not None and nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=_dt.timezone.utc)
        if nxt is None or nxt <= now:
            due.append({"id": f.id, "description": f.description, "url": f.url, "interval": f.run_interval})

    # the Selective set has its OWN schedule (independent of any filter being due).
    sched = get_selective_schedule(db)
    snxt = sched.next_run_at
    if snxt is not None and snxt.tzinfo is None:
        snxt = snxt.replace(tzinfo=_dt.timezone.utc)
    selective_due = sched.run_interval != "off" and (snxt is None or snxt <= now)

    manual = _manual_tickers(db)
    return {
        "filters": due,
        # manual tickers ride along when a filter is due OR when the selective
        # schedule itself fires.
        "manual_tickers": manual if (due or selective_due) else [],
        # the agent runs the selective set when due, then posts /api/matp with
        # selective=true (final) so its schedule advances.
        "selective": {"due": selective_due, "interval": sched.run_interval, "tickers": manual},
    }


@router.get("/matp-queue")
def matp_queue(
    due_only: bool = True,
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """The DEDUPLICATED list of tickers to compute MATP for — the agent's work list.

    Replaces "walk each due filter's universe" (which recomputed a ticker once per
    filter that contained it). Screener filters are containers; this endpoint
    unions their memberships, dedupes to one row per symbol, and returns that.
    A ticker in three filters is computed ONCE.

    Each entry carries `filter_ids` so the agent can still post per-filter
    attribution, and `advance_filter_ids` lists the filters whose schedule this
    run should advance when it finalizes.

    `due_only=false` returns the full union regardless of schedule (for ad-hoc runs).
    """
    from ..services.matp_queue import build_queue

    q = build_queue(db, due_only=due_only)
    return {
        "tickers": [
            {"symbol": t["symbol"],
             "filter_ids": [f["id"] for f in t["filters"]],
             "manual": t["manual"]}
            for t in q["tickers"]
        ],
        "count": q["counts"]["tickers"],
        "duplicates_avoided": q["counts"]["saved"],
        "advance_filter_ids": [f["id"] for f in q["filters"] if f.get("due")],
        "selective": q["selective"],
        # surfaced so a filter whose URL failed to resolve is visible to the agent
        # (and in its run notes) rather than silently contributing zero tickers
        "filter_errors": [
            {"id": f["id"], "description": f["description"], "error": f["error"]}
            for f in q["filters"] if f.get("error")
        ],
    }


# ---------------------------------------------------------------------------
# Agent heartbeat — the outbound-only Nous Hermes agent self-reports liveness +
# its crontab on each poll. TradeHunter can't reach into the Linux box, so this
# is how the /agent page knows the agent is alive and which crons it's running.
class CronJobIn(BaseModel):
    id: str | None = None
    schedule: str | None = None       # e.g. "*/10 * * * *"
    skills: str | None = None         # e.g. "matp"
    prompt: str | None = None         # the FULL prompt the cron runs
    next_run: str | None = None
    active: bool | None = None


class HeartbeatIn(BaseModel):
    agent: str = "nous_hermes"
    version: str | None = None
    crons: str | None = None          # raw `hermes cron list` text (fallback)
    cron_jobs: list[CronJobIn] | None = None  # structured, with full prompts
    host: str | None = None
    polled_at: str | None = None      # ISO-8601 from the agent's own clock


@router.post("/agent/heartbeat")
def agent_heartbeat(
    payload: HeartbeatIn,
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Upsert one heartbeat row per agent name (portable query-then-update)."""
    name = (payload.agent or "nous_hermes").strip() or "nous_hermes"
    now = _dt.datetime.now(_dt.timezone.utc)
    polled = None
    if payload.polled_at:
        try:
            polled = _dt.datetime.fromisoformat(payload.polled_at.replace("Z", "+00:00"))
        except ValueError:
            polled = None
    row = db.query(AgentHeartbeat).filter(AgentHeartbeat.agent == name).first()
    if row is None:
        row = AgentHeartbeat(agent=name)
        db.add(row)
    row.version = payload.version
    row.crons = payload.crons
    row.cron_jobs = (
        [j.model_dump() for j in payload.cron_jobs]
        if payload.cron_jobs is not None else None
    )
    row.host = payload.host
    row.polled_at = polled
    row.received_at = now
    db.commit()
    return {"ok": True, "agent": name, "received_at": now.isoformat()}


@router.post("/ingest/health")
def ingest_health(
    payload: dict = Body(...),
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Hermes-side reporter cron pushes the parquet-ingest health report here
    (freshness per timeframe + recent ingest-log tail). Upsert one row per host;
    the Data Ingest page shows the latest. Body is the raw report JSON, e.g.
    {host, generated_epoch, timeframes:[{tf,symbols,mb,newest_epoch}], log_tail:[]}.
    """
    host = (str(payload.get("host") or "hermes")).strip()[:120] or "hermes"
    row = db.query(IngestHealth).filter(IngestHealth.host == host).first()
    if row is None:
        row = IngestHealth(host=host)
        db.add(row)
    row.report = payload
    row.received_at = _dt.datetime.now(_dt.timezone.utc)
    db.commit()
    return {"ok": True, "host": host}


@router.post("/ingest/edgar")
def ingest_edgar(
    payload: dict = Body(...),
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """AI-Hermes reporter pushes the EDGAR earnings-filing corpus health here:
    per-ticker latest seeded period + earnings dates + stub-MD flags. The EDGAR
    files live on AI-Hermes (the web app can't read that box's filesystem), so
    it scans + reports in, like the parquet /ingest/health above. Upsert one row
    per host; the Data Ingest page computes DUE/OVERDUE and shows the latest.
    Body, e.g. {host, generated_epoch, root,
    tickers:[{ticker, latest_period, newest_epoch, html, md, stub_md,
    last_earnings, next_earnings}], log_tail:[...]}.
    """
    host = (str(payload.get("host") or "ai-hermes")).strip()[:120] or "ai-hermes"
    row = db.query(EdgarIngestHealth).filter(EdgarIngestHealth.host == host).first()
    if row is None:
        row = EdgarIngestHealth(host=host)
        db.add(row)
    row.report = payload
    row.received_at = _dt.datetime.now(_dt.timezone.utc)
    db.commit()
    return {"ok": True, "host": host}


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
    progress_done: int | None = None   # tickers processed so far
    progress_total: int | None = None  # tickers in this run


def _notify_refresh_done(rid: int) -> None:
    """Background task: post a rich Discord embed for a COMPLETED refresh. Opens
    its own DB session (the request's is closed by now) and does the live price /
    next-earnings lookups here so the request returns immediately. Soft-fail."""
    from ..db import SessionLocal
    from ..services.prices import fetch_daily_ohlc, fetch_next_earnings

    db = SessionLocal()
    try:
        r = db.get(MATPRefreshRequest, rid)
        if r is None:
            return
        base = settings.public_url
        if r.scope == "ticker" and r.symbol:
            sym = r.symbol
            lv = db.query(MATPLevel).filter(MATPLevel.symbol == sym).first()
            bars = fetch_daily_ohlc(sym)
            price = bars[-1]["close"] if bars else None
            discord.post_embed(
                **discord.build_ticker_embed(
                    symbol=sym,
                    matp=lv.matp if lv else None, mbp=lv.mbp if lv else None,
                    signal=lv.signal if lv else None, price=price,
                    next_earnings=fetch_next_earnings(sym),
                    last_earnings=lv.last_earnings_date if lv else None,
                    note=r.note, title_prefix="✅ MATP refreshed", public_url=base,
                )
            )
        else:
            desc = (r.filter.description if r.filter else None) or (
                f"filter #{r.filter_id}" if r.filter_id else "filter")
            n = (
                db.query(MATPLevel)
                .filter(MATPLevel.filter_id == r.filter_id, MATPLevel.status == "active")
                .count()
                if r.filter_id is not None else None
            )
            body = r.note or (f"{n} names updated" if n is not None else "Refresh complete")
            discord.post_embed(
                title=f"✅ MATP refreshed · {desc}", description=body,
                url=f"{base}/matp" + (f"?wl={r.filter_id}" if r.filter_id is not None else ""),
            )
    except Exception:  # noqa: BLE001 — notifications must never break ingest
        pass
    finally:
        db.close()


@router.post("/refresh-queue/{rid}/status")
def update_refresh_status(
    rid: int,
    payload: RefreshStatusIn,
    background: BackgroundTasks,
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Agent reports progress: 'running' (with optional done/total counts) when
    it starts and as it works, then 'done'/'failed'. On 'done' we post a summary
    to Discord (if a webhook is configured) via a background task (soft-fail)."""
    if payload.status not in _REQUEST_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad status {payload.status!r}")
    r = db.get(MATPRefreshRequest, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such request")
    now = _dt.datetime.now(_dt.timezone.utc)
    r.status = payload.status
    if payload.note is not None:
        r.note = payload.note
    if payload.progress_total is not None:
        r.progress_total = payload.progress_total
    if payload.progress_done is not None:
        r.progress_done = payload.progress_done
    if payload.status == "running" and r.claimed_at is None:
        r.claimed_at = now
    if payload.status in ("done", "failed"):
        r.completed_at = now
        # snap the bar to full on a clean finish
        if payload.status == "done" and r.progress_total:
            r.progress_done = r.progress_total
    db.commit()

    # On a clean finish, notify Discord (outbound, soft-fail, non-blocking).
    if payload.status == "done" and discord.configured():
        background.add_task(_notify_refresh_done, rid)
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
    # Incremental population: the agent pushes processed tickers as it goes with
    # final=False (no archive, no prune) so the table fills live; the closing
    # push sets final=True (+ prune=True) — that one is archived as the run file.
    final: bool = True
    # Set on the closing push of a SELECTIVE run (the ad-hoc no-filter set ran
    # because its own schedule was due) so we advance the selective schedule.
    selective: bool = False


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

    # archive the raw run as one JSON file in the MATP folder (the "cream"),
    # only on the finalizing push (so incremental population doesn't spam files).
    # Soft-fail: archiving must never break ingest.
    archived = None
    if payload.final:
        filter_desc = None
        if payload.filter_id is not None:
            f = db.get(FinvizFilter, payload.filter_id)
            filter_desc = f.description if f else None
            # stamp the filter on a completed run. last_run_at advances on EVERY
            # finished run (scheduled or manual / "off"); next_run_at only when the
            # filter is on a schedule. Decoupled so the watchlist's "last refreshed"
            # date is trustworthy regardless of interval.
            if f is not None:
                f.last_run_at = now
                iv = RUN_INTERVALS.get(f.run_interval)
                if iv:
                    f.next_run_at = now + timedelta(days=iv)
                db.commit()
        # selective run finished -> advance the selective schedule the same way.
        if payload.selective:
            sched = get_selective_schedule(db)
            sched.last_run_at = now
            siv = RUN_INTERVALS.get(sched.run_interval)
            if siv:
                sched.next_run_at = now + timedelta(days=siv)
            db.commit()
        from ..services.matp_archive import save_run

        archived = save_run(payload, now, filter_desc=filter_desc)

    return {
        "ok": True, "upserted": upserted,
        "history_appended": appended, "targets_added": targets_added,
        "dropped": dropped, "archived": archived,
    }


# ── Company Analysis push (agent → platform) ───────────────────────────────────
class CompanyAnalysisIn(BaseModel):
    body: str | None = None            # long-form prose (qualitative sections)
    content: dict | list | None = None  # structured payload (tables/lists/tiers/scorecard)
    sources: list | None = None         # [{title, url, accession, kind}]
    confidence: str | None = None       # high | medium | low (esp. inferred tier-2)
    industry: str | None = None


@router.post("/company-analysis/{symbol}/{section}")
def push_company_analysis(
    symbol: str,
    section: str,
    payload: CompanyAnalysisIn = Body(...),
    _: bool = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """The Nous agent pushes a generated section (from EDGAR + industry knowledge).
    Upsert by (symbol, section). See dashboard_tst/COMPANY_ANALYSIS_DESIGN.md."""
    sym = (symbol or "").strip().upper()
    if section not in CA_SECTIONS:
        raise HTTPException(status_code=400, detail=f"unknown section: {section}")
    row = (
        db.query(CompanyAnalysis)
        .filter(CompanyAnalysis.symbol == sym, CompanyAnalysis.section == section)
        .first()
    )
    if row is None:
        row = CompanyAnalysis(symbol=sym, section=section)
        db.add(row)
    row.body = payload.body
    row.content = payload.content
    row.sources = payload.sources
    row.confidence = payload.confidence
    if payload.industry:
        row.industry = payload.industry
    row.source_kind = "agent"
    row.as_of = _utcnow()
    row.updated_by = "nous_hermes"
    db.commit()
    return {"ok": True, "symbol": sym, "section": section}
