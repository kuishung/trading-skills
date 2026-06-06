"""Parquet ingest health — a freshness check for the Data Ingest page.

IMPORTANT (CLAUDE.md scope rule): parquet is BACKTEST-ONLY and must NOT be a
data source for the live UI. This module therefore reads **file metadata only**
(existence, count, mtime, size) of the price_history store — it never opens a
parquet file. It just answers "did the ingest run / how fresh is the data?" so a
moderator can eyeball it daily.

Configure ``TST_PRICE_HISTORY_DIR`` (e.g. C:\\HermesSync\\MarketData\\price_history
on Hermes). Unset/missing -> returns None and the page shows "not configured".
"""
from __future__ import annotations

import datetime as _dt
import os
import time

from ..config import settings

# Timeframes we actually seed (set 2026-06-06). 1min/15min are NOT seeded
# (the seed/supervisor pull 3min/5min/daily), so listing them just showed
# alarming "never" rows while hiding the real 3min row.
_TIMEFRAMES = ("3min", "5min", "daily")


def _tier(age: float | None) -> int:
    return 2 if age is None else (0 if age < 26 * 3600 else (1 if age < 74 * 3600 else 2))


def report_to_display(report: dict, received_at) -> dict:
    """Turn a PUSHED report (timeframes carry `newest_epoch`, UTC unix) into
    display data with current 'ago' strings + freshness tiers, plus how long ago
    the report itself was received (so a dead reporter cron is obvious)."""
    now = time.time()
    tfs = []
    newest_overall = 0.0
    for t in (report.get("timeframes") or []):
        ne = t.get("newest_epoch") or 0
        newest_overall = max(newest_overall, ne)
        age = (now - ne) if ne else None
        tfs.append({
            "tf": t.get("tf", "?"), "symbols": t.get("symbols", 0), "mb": t.get("mb", 0),
            "newest_ago": _ago(age) if age is not None else None, "tier": _tier(age),
        })
    overall_age = (now - newest_overall) if newest_overall else None
    rec_ago = None
    if received_at is not None:
        ra = received_at if received_at.tzinfo else received_at.replace(tzinfo=_dt.timezone.utc)
        rec_ago = _ago((_dt.datetime.now(_dt.timezone.utc) - ra).total_seconds())
    return {
        "source": "pushed", "host": report.get("host"), "root": report.get("root"),
        "received_ago": rec_ago, "timeframes": tfs, "log_tail": report.get("log_tail") or [],
        "newest_ago": _ago(overall_age) if overall_age is not None else None,
        "tier": _tier(overall_age),
    }


def _ago(secs: float) -> str:
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


def parquet_health() -> dict | None:
    """Per-timeframe freshness of the parquet store, or None if not configured /
    the directory isn't present on this host. Soft-fail throughout."""
    root = (settings.price_history_dir or "").strip()
    if not root or not os.path.isdir(root):
        return None
    now = time.time()
    tfs = []
    newest_overall = 0.0
    for tf in _TIMEFRAMES:
        d = os.path.join(root, tf)
        count = 0
        newest = 0.0
        total = 0
        if os.path.isdir(d):
            try:
                for e in os.scandir(d):
                    if e.is_file() and e.name.endswith(".parquet"):
                        count += 1
                        try:
                            st = e.stat()
                        except OSError:
                            continue
                        total += st.st_size
                        if st.st_mtime > newest:
                            newest = st.st_mtime
            except OSError:
                pass
        newest_overall = max(newest_overall, newest)
        age = (now - newest) if newest else None
        tfs.append({
            "tf": tf,
            "symbols": count,
            "mb": round(total / 1048576, 1),
            "newest_ago": _ago(age) if age is not None else None,
            # freshness tier for colouring: 0 ok / 1 warn / 2 stale|missing
            "tier": 2 if age is None else (0 if age < 26 * 3600 else (1 if age < 74 * 3600 else 2)),
        })
    overall_age = (now - newest_overall) if newest_overall else None
    return {
        "root": root,
        "timeframes": tfs,
        "newest_ago": _ago(overall_age) if overall_age is not None else None,
        "tier": 2 if overall_age is None else (0 if overall_age < 26 * 3600 else (1 if overall_age < 74 * 3600 else 2)),
    }
