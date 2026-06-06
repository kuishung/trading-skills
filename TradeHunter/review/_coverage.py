"""Data-sufficiency pre-flight for backtests.

A backtest over a window the parquet store doesn't fully cover silently
under-trades — symbols with missing/partial history just produce no candidates
or no bars, and the headline R looks fine while half the universe never traded.
This module answers, BEFORE the run, "does the store actually cover
[start, end] for this universe and timeframe?" so the summary can flag it.

Metadata-only: uses ``bars_store.available_range_fast`` (parquet row-group
statistics, ~1ms/symbol) and ``bar_session_date_et`` for ET session dates — it
never materialises bar data. Mirrors the spirit of the dashboard's
universe-health check, but scoped to one backtest window.
"""
from __future__ import annotations

from datetime import date

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "resources", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import bars_store  # noqa: E402


def _as_date(x) -> date:
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def check_coverage(symbols, timeframe: str, start, end) -> dict:
    """Classify each symbol's parquet coverage of [start, end] (inclusive).

    Returns a summary dict:
      {timeframe, window, n, ok, partial, missing,
       partial_detail:[{symbol, first, last}], missing_symbols:[...],
       worst_start_gap_days, fully_covered (bool)}

    ok      = data spans the whole window (first <= start AND last >= end)
    partial = has some data but not the full span
    missing = no parquet / unreadable
    """
    start = _as_date(start)
    end = _as_date(end)
    syms = sorted({s.upper() for s in symbols})
    ok = partial = 0
    partial_detail: list[dict] = []
    missing_symbols: list[str] = []
    worst_start_gap = 0
    for s in syms:
        try:
            rng = bars_store.available_range_fast(s, timeframe=timeframe)
        except Exception:
            rng = None
        if not rng:
            missing_symbols.append(s)
            continue
        try:
            first = bars_store.bar_session_date_et(rng[0])
            last = bars_store.bar_session_date_et(rng[1])
        except (TypeError, ValueError):
            missing_symbols.append(s)
            continue
        if first <= start and last >= end:
            ok += 1
        else:
            partial += 1
            partial_detail.append({"symbol": s, "first": first.isoformat(),
                                   "last": last.isoformat()})
            if first > start:
                worst_start_gap = max(worst_start_gap, (first - start).days)
    return {
        "timeframe": timeframe,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "n": len(syms),
        "ok": ok,
        "partial": partial,
        "missing": len(missing_symbols),
        "partial_detail": partial_detail[:50],
        "missing_symbols": missing_symbols[:50],
        "worst_start_gap_days": worst_start_gap,
        "fully_covered": (partial == 0 and not missing_symbols),
    }


def headline(cov: dict) -> str:
    w = cov["window"]
    tag = "OK" if cov["fully_covered"] else "PARTIAL"
    return (f"# coverage[{cov['timeframe']}] {w['start']}..{w['end']}: "
            f"{tag} - {cov['ok']} full / {cov['partial']} partial / "
            f"{cov['missing']} missing of {cov['n']}"
            + (f" · worst start gap {cov['worst_start_gap_days']}d"
               if cov['worst_start_gap_days'] else ""))
