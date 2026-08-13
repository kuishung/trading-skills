"""EDGAR earnings-filing ingest health — the Data Ingest page's EDGAR card.

The EDGAR corpus (SEC 10-Q/10-K filings) is fetched by the Nous Hermes agent and
stored on AI-Hermes (the Windows file server, 192.168.1.162). The web app can't
read that box's filesystem, so AI-Hermes SCANS the corpus and PUSHES a per-ticker
completeness report to /api/ingest/edgar (stored as EdgarIngestHealth). This
module turns that pushed report into display data.

No database of "what should exist" is kept — the folder is the source of truth.
The reporter (deploy/report_edgar_health.py) derives each ticker's MISSING
quarters straight from the filenames it finds (a company files 3 ten-Qs + 1
ten-K per fiscal year, so gaps in the regular cadence are self-evident). Here we
just classify each ticker COMPLETE / GAPS / STUB and aggregate.
"""
from __future__ import annotations

import datetime as _dt
import time

# status -> freshness tier (0 ok / 1 warn / 2 problem) for the dot/text colours.
_STATUS_TIER = {"COMPLETE": 0, "STUB": 1, "GAPS": 2}
_STATUS_ORDER = {"GAPS": 0, "STUB": 1, "COMPLETE": 2}
# Cap the actionable table so 700+ tickers stay renderable; COMPLETE rows are
# summarised in the counts, not listed.
_MAX_ROWS = 200


def _ago(secs: float | None) -> str | None:
    if secs is None:
        return None
    secs = int(secs)
    if secs < 90:
        return "just now"
    mins = secs // 60
    if mins < 90:
        return f"{mins} min ago"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs}h ago"
    return f"{hrs // 24}d ago"


def _classify(t: dict) -> str:
    """COMPLETE / GAPS / STUB from one ticker's folder facts. A missing quarter is
    the most actionable problem (re-fetch), then a stub MD (regenerate the body)."""
    if t.get("missing"):
        return "GAPS"
    if t.get("stub_md"):
        return "STUB"
    return "COMPLETE"


def ticker_status(report: dict, symbol: str, received_at=None) -> dict | None:
    """Display facts for ONE ticker's downloaded EDGAR earnings filings, for the
    Company page — or ``None`` if the corpus doesn't track this ticker. Unlike
    ``report_to_display`` (which lists only the actionable, non-COMPLETE rows), this
    returns the row for any ticker, COMPLETE included."""
    if not report or not symbol:
        return None
    sym = str(symbol).strip().upper()
    t = next(
        (x for x in (report.get("tickers") or [])
         if str(x.get("ticker") or "").upper() == sym),
        None,
    )
    if t is None:
        return None
    now = time.time()
    status = _classify(t)
    ne = t.get("newest_epoch") or 0
    rec_ago = None
    if received_at is not None:
        ra = received_at if received_at.tzinfo else received_at.replace(tzinfo=_dt.timezone.utc)
        rec_ago = _ago((_dt.datetime.now(_dt.timezone.utc) - ra).total_seconds())
    return {
        "ticker": sym,
        "status": status,
        "tier": _STATUS_TIER.get(status, 0),
        "latest_period": t.get("latest_period"),
        "n_quarters": int(t.get("n_quarters") or 0),
        "has_10k": bool(t.get("has_10k")),
        "html": int(t.get("html") or 0),
        "md": int(t.get("md") or 0),
        "stub_md": bool(t.get("stub_md")),
        "missing": list(t.get("missing") or []),
        "newest_ago": _ago(now - ne) if ne else None,
        "received_ago": rec_ago,
        "host": report.get("host"),
        "root": report.get("root"),
    }


def report_to_display(report: dict, received_at) -> dict:
    """Turn a PUSHED EDGAR report into display data: per-ticker status, the
    actionable (non-COMPLETE) rows worst-first, and aggregate counts/tier."""
    now = time.time()
    tickers = report.get("tickers") or []

    counts = {"tracked": 0, "complete": 0, "gaps": 0, "stub": 0,
              "no_10k": 0, "missing_total": 0}
    rows: list[dict] = []
    newest_overall = 0.0
    for t in tickers:
        sym = str(t.get("ticker") or "?").upper()
        status = _classify(t)
        counts["tracked"] += 1
        counts[status.lower()] = counts.get(status.lower(), 0) + 1
        if not t.get("has_10k"):
            counts["no_10k"] += 1
        missing = list(t.get("missing") or [])
        counts["missing_total"] += len(missing)
        ne = t.get("newest_epoch") or 0
        if ne:
            newest_overall = max(newest_overall, ne)
        rows.append({
            "ticker": sym,
            "latest_period": t.get("latest_period"),
            "missing": missing,
            "missing_n": len(missing),
            "has_10k": bool(t.get("has_10k")),
            "html": int(t.get("html") or 0),
            "md": int(t.get("md") or 0),
            "stub_md": bool(t.get("stub_md")),
            "newest_ago": _ago(now - ne) if ne else None,
            "status": status,
            "tier": _STATUS_TIER.get(status, 0),
        })

    # Only the actionable rows go in the table (COMPLETE is summarised); worst
    # first, then most-missing, then ticker.
    actionable = [r for r in rows if r["status"] != "COMPLETE"]
    actionable.sort(key=lambda r: (_STATUS_ORDER[r["status"]], -r["missing_n"], r["ticker"]))
    truncated = max(0, len(actionable) - _MAX_ROWS)
    shown = actionable[:_MAX_ROWS]

    overall_age = (now - newest_overall) if newest_overall else None
    gen_epoch = report.get("generated_epoch") or 0
    rec_ago = None
    if received_at is not None:
        ra = received_at if received_at.tzinfo else received_at.replace(tzinfo=_dt.timezone.utc)
        rec_ago = _ago((_dt.datetime.now(_dt.timezone.utc) - ra).total_seconds())

    # Aggregate tier: any gaps -> red; else any stub -> amber; else green.
    agg_tier = 2 if counts["gaps"] else (1 if counts["stub"] else 0)

    return {
        "source": "pushed",
        "host": report.get("host"),
        "root": report.get("root"),
        "received_ago": rec_ago,
        "generated_ago": _ago(now - gen_epoch) if gen_epoch else None,
        "newest_ago": _ago(overall_age) if overall_age is not None else None,
        "tier": agg_tier,
        "counts": counts,
        "rows": shown,
        "truncated": truncated,
        "log_tail": report.get("log_tail") or [],
    }
