"""The MATP work queue — the DEDUPLICATED set of tickers to compute.

The problem this solves (user, 2026-08-16): screener filters overlap. If AAPL
matches three saved filters, the agent used to walk each filter's universe in
turn and compute AAPL's MATP three times — three MarketBeat browse sessions and
three DeepSeek passes for one answer. Pure wasted token cost.

The fix is to invert who owns the ticker list. A **screener is only a
container**: it says which tickers belong to it, nothing more. The queue is the
UNION of every active container plus the manual/selective names, deduplicated to
one row per symbol, and THAT is the list the agent works through — so every
ticker is computed exactly once per cycle no matter how many filters hold it.

Membership resolution happens here, in the dashboard, via the shared
resources.finviz_screener (public-page scrape, 1h disk cache). That's the same
expansion the agent used to do per filter, done once and shared.

Everything soft-fails: a filter whose URL can't be resolved right now is
reported with `error` set and simply contributes no tickers, rather than
breaking the queue for every other filter.
"""
from __future__ import annotations

import datetime as _dt
import time

from sqlalchemy.orm import Session

from ..models import (FinvizFilter, MATPLevel, MATPRefreshRequest, UserWatchlist,
                      get_selective_schedule)
from . import resources_bridge

# Resolved-membership cache: filter_id -> (expires_at, [symbols], error|None).
# Short in-process TTL on top of finviz_screener's own 1h disk cache, so
# rendering the tab repeatedly doesn't re-walk the page list.
_TTL = 900.0
_cache: dict[int, tuple[float, list[str], str | None]] = {}


def _clean(sym: str) -> str:
    return (sym or "").strip().upper()


def filter_symbols(f: FinvizFilter, *, force_refresh: bool = False) -> tuple[list[str], str | None]:
    """Resolve one filter's URL to its ticker membership. Returns (symbols, error)."""
    hit = _cache.get(f.id)
    if hit and hit[0] > time.time() and not force_refresh:
        return hit[1], hit[2]
    symbols: list[str] = []
    error: str | None = None
    try:
        rows = resources_bridge.screen_universe(f.url, force_refresh=force_refresh)
        seen = set()
        for r in rows:
            s = _clean(r.get("symbol") if isinstance(r, dict) else r)
            if s and s not in seen:
                seen.add(s)
                symbols.append(s)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:200]
    _cache[f.id] = (time.time() + _TTL, symbols, error)
    return symbols, error


def _is_due(next_run_at, now: _dt.datetime) -> bool:
    if next_run_at is None:
        return True
    if next_run_at.tzinfo is None:
        next_run_at = next_run_at.replace(tzinfo=_dt.timezone.utc)
    return next_run_at <= now


def watchlist_uncovered(db: Session) -> set[str]:
    """Symbols a MEMBER starred that have no MATP on record yet.

    A ticker added from the Sector or Company page lands on someone's My Watchlist
    but belongs to no screener and isn't a "selective" name, so nothing in the
    scheduled pipeline would ever compute it — its MATP/MBP columns would read
    "not yet calculated" forever. These are therefore pulled into the queue
    UNCONDITIONALLY (not only when a filter is due): a ticker with no value at all
    can't sensibly wait on a screener schedule it isn't part of.

    The set is self-limiting — once a ticker HAS an MATP it drops out of here, so
    this only ever covers the first calculation. Afterwards it refreshes through
    whatever container it belongs to, or on demand via the Recalc button.
    """
    starred = {s for (s,) in db.query(UserWatchlist.symbol).distinct().all() if s}
    if not starred:
        return set()
    have = {
        lv.symbol
        for lv in db.query(MATPLevel).filter(MATPLevel.symbol.in_(starred)).all()
        if lv.matp is not None
    }
    return {_clean(s) for s in (starred - have)}


def manual_symbols(db: Session) -> list[str]:
    """Ad-hoc 'selective' names — active MATPLevels with no source filter."""
    rows = (
        db.query(MATPLevel)
        .filter(MATPLevel.filter_id.is_(None),
                (MATPLevel.status == "active") | (MATPLevel.status.is_(None)))
        .order_by(MATPLevel.symbol)
        .all()
    )
    return [r.symbol for r in rows]


def build_queue(db: Session, *, due_only: bool = False,
                force_refresh: bool = False) -> dict:
    """The deduplicated MATP work queue.

    `due_only=True` restricts the container set to filters whose schedule is due
    (plus the selective set when its own schedule is due) — that's what the agent
    asks for. `due_only=False` is the full picture the dashboard tab shows.

    Returns:
        {
          tickers:   [ {symbol, filters:[{id,description}], n_sources, manual,
                        matp, mbp, as_of, has_matp} ],   # one row per symbol
          filters:   [ {id, description, interval, due, n_symbols, error} ],
          counts:    {tickers, rows_before_dedup, saved, overlapped, filters, errors},
          selective: {due, interval, n},
        }
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    active = (
        db.query(FinvizFilter)
        .filter(FinvizFilter.is_active.is_(True))
        .order_by(FinvizFilter.description)
        .all()
    )

    sched = get_selective_schedule(db)
    selective_due = sched.run_interval != "off" and _is_due(sched.next_run_at, now)

    # symbol -> list of containing filters (ORDER of first sighting preserved)
    membership: dict[str, list[dict]] = {}
    rows_before_dedup = 0
    filt_rows, n_errors = [], 0

    for f in active:
        due = f.run_interval != "off" and _is_due(f.next_run_at, now)
        if due_only and not due:
            filt_rows.append({"id": f.id, "description": f.description,
                              "interval": f.run_interval, "due": False,
                              "n_symbols": None, "error": None,
                              "last_run_at": f.last_run_at, "next_run_at": f.next_run_at})
            continue
        syms, err = filter_symbols(f, force_refresh=force_refresh)
        if err:
            n_errors += 1
        rows_before_dedup += len(syms)
        for s in syms:
            membership.setdefault(s, []).append({"id": f.id, "description": f.description})
        # last_run_at/next_run_at are the ONLY visible proof that a "Recalculate
        # all" was actually picked up: that action marks filters due rather than
        # creating refresh requests, so the /agent "working now" panel (which
        # tracks ticker requests) stays empty and would otherwise look idle.
        filt_rows.append({"id": f.id, "description": f.description,
                          "interval": f.run_interval, "due": due,
                          "n_symbols": len(syms), "error": err,
                          "last_run_at": f.last_run_at, "next_run_at": f.next_run_at})

    # manual / selective names ride along (they have no container)
    manual = manual_symbols(db)
    include_manual = (not due_only) or selective_due or any(r["due"] for r in filt_rows)
    manual_set: set[str] = set()
    if include_manual:
        for s in manual:
            s = _clean(s)
            if not s:
                continue
            manual_set.add(s)
            rows_before_dedup += 1
            membership.setdefault(s, [])

    # Member-starred tickers with NO MATP yet — always included, even when nothing
    # is due, because they belong to no container that could ever become due.
    # See watchlist_uncovered(): the set empties itself as they get computed.
    watchlist_set = watchlist_uncovered(db)
    for s in watchlist_set:
        if s and s not in membership:
            rows_before_dedup += 1
        membership.setdefault(s, [])

    # attach what we already know about each symbol (last computed MATP)
    syms = list(membership.keys())
    levels = {}
    if syms:
        for lv in db.query(MATPLevel).filter(MATPLevel.symbol.in_(syms)).all():
            levels[lv.symbol] = lv

    # symbols with a recalculation already queued/running, so the UI can show
    # "queued" instead of offering a button that would be a no-op (_enqueue
    # refuses to create a duplicate open request).
    pending = {
        r.symbol
        for r in db.query(MATPRefreshRequest)
        .filter(MATPRefreshRequest.scope == "ticker",
                MATPRefreshRequest.status.in_(("pending", "running")))
        .all()
        if r.symbol
    }

    tickers = []
    for sym in sorted(membership):
        srcs = membership[sym]
        lv = levels.get(sym)
        tickers.append({
            "symbol": sym,
            "filters": srcs,
            "n_sources": len(srcs),
            "manual": sym in manual_set,
            "matp": getattr(lv, "matp", None),
            "mbp": getattr(lv, "mbp", None),
            "as_of": getattr(lv, "as_of", None),
            "has_matp": lv is not None and getattr(lv, "matp", None) is not None,
            "queued": sym in pending,
            # starred by a member and never calculated -> pulled in automatically
            "watchlist": sym in watchlist_set,
        })

    n_unique = len(tickers)
    overlapped = sum(1 for t in tickers if t["n_sources"] > 1)
    return {
        "tickers": tickers,
        "filters": filt_rows,
        "selective": {"due": selective_due, "interval": sched.run_interval,
                      "n": len(manual_set)},
        "counts": {
            "tickers": n_unique,
            "rows_before_dedup": rows_before_dedup,
            # MATP computations avoided by deduplicating the union
            "saved": max(0, rows_before_dedup - n_unique),
            "overlapped": overlapped,
            "filters": len(active),
            "errors": n_errors,
            # member-starred tickers awaiting their FIRST calculation
            "watchlist_new": len(watchlist_set),
        },
    }
