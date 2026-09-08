"""Curated setups — did the plan trigger, is it still running, what did it make.

A curated ticker is a PLAN: a level to get in at, a level to get out at if it is
wrong, and a level to take profit. This module answers those three questions from
daily bars alone, so nothing has to be marked off by hand.

``evaluate()`` is PURE (bars in, verdict out) — the rules are testable without a
network. ``rows_for()`` is the thin live-fetching wrapper around it. NOTHING derived
is stored: trigger dates and P/L are recomputed from price history on every read, so
they can never drift out of sync with the prices the way a cached column would.
Prices come LIVE from Yahoo via services.prices, never parquet (CLAUDE.md).

Entry style is INFERRED, not asked for. An entry above the price when you curated it
is a breakout (a resting buy-stop): it triggers when the day's HIGH reaches it. An
entry below is a pullback (a resting buy-limit): it triggers when the LOW reaches it.
Shorts mirror both. This matters because the obvious rule -- "the day's range
contains the entry" -- silently never triggers on a gap THROUGH the level, which is
exactly the move a breakout plan is waiting for. Fills honour the gap too: a plan
that gapped past its entry fills at the open, not at the level you wrote down, and
the P/L is measured from that real fill.
"""
from __future__ import annotations

import datetime as _dt
from concurrent.futures import ThreadPoolExecutor

from .prices import fetch_daily_ohlc

# Statuses, in the order they appear in a setup's life.
WAITING = "waiting"   # curated, entry not reached yet
OPEN = "open"         # triggered, neither stop nor target hit
TARGET = "target"     # profit target reached
STOP = "stop"         # stop loss reached
INVALID = "invalid"   # the three levels don't describe a trade


def plan_kind(entry: float, stop: float, target: float) -> tuple[str, str] | None:
    """('long'|'short', reason) — or None if these levels aren't a coherent plan.

    Direction comes from the stop: a stop BELOW the entry is a long, above is a
    short. The target must then sit on the profitable side, otherwise the "plan"
    would book a loss on success.
    """
    if entry is None or stop is None or target is None:
        return None
    if entry <= 0 or stop <= 0 or target <= 0:
        return None
    if stop == entry:
        return None
    if stop < entry:
        return ("long", "stop below entry") if target > entry else None
    return ("short", "stop above entry") if target < entry else None


def evaluate(*, entry: float, stop: float, target: float, curated_on: str,
             bars: list[dict]) -> dict:
    """Walk the bars from `curated_on` forward and report what happened.

    Returns a dict with `status`, `triggered_on`, `fill`, `closed_on`, `exit`,
    `r_multiple`, `pct`, `last`, `bars_waiting`, `bars_held`, `ambiguous`, `note`.
    Every numeric field is None when it cannot be known yet, never 0 — a setup that
    has not triggered has no P/L, and showing 0.0 would read as breakeven.
    """
    kind = plan_kind(entry, stop, target)
    out: dict = {
        "status": INVALID, "direction": None, "triggered_on": None, "fill": None,
        "closed_on": None, "exit": None, "r_multiple": None, "pct": None,
        "last": None, "bars_waiting": None, "bars_held": None, "ambiguous": False,
        "planned_rr": None,
        "note": "Entry, stop and target don't describe a trade.",
    }
    if kind is None:
        return out
    direction = kind[0]
    long_ = direction == "long"
    out["direction"] = direction
    out["planned_rr"] = round(abs(target - entry) / abs(entry - stop), 2)

    after = [b for b in (bars or []) if b.get("time") and b["time"] >= curated_on]
    if not after:
        out["status"] = WAITING
        out["note"] = "No price history since it was curated."
        return out
    out["last"] = after[-1].get("close")

    # Where was price when this was curated? That decides whether the entry is a
    # breakout (reached from below) or a pullback (reached from above), and so
    # which side of the bar triggers it.
    ref = after[0].get("open") or after[0].get("close")
    if long_:
        breakout = entry > ref
    else:
        breakout = entry < ref

    # Four order types, spelled out rather than derived from a parity trick:
    #   long  + breakout  = buy stop    -> reached from below, gap fills WORSE (higher)
    #   long  + pullback  = buy limit   -> reached from above, gap fills BETTER (lower)
    #   short + breakdown = sell stop   -> reached from above, gap fills WORSE (lower)
    #   short + rally     = sell limit  -> reached from below, gap fills BETTER (higher)
    def touched(b) -> bool:
        # A gap straight past the level still fills — it just fills at the open.
        if long_:
            return b["high"] >= entry if breakout else b["low"] <= entry
        return b["low"] <= entry if breakout else b["high"] >= entry

    def fill_price(b) -> float:
        o = b.get("open")
        if o is None:
            return entry
        if long_:
            return max(entry, o) if breakout else min(entry, o)
        return min(entry, o) if breakout else max(entry, o)

    ti = next((i for i, b in enumerate(after) if touched(b)), None)
    if ti is None:
        out["status"] = WAITING
        out["bars_waiting"] = len(after)
        out["note"] = ("Waiting: price has not reached the entry in "
                       f"{len(after)} session(s).")
        return out

    trig = after[ti]
    fill = fill_price(trig)
    out["triggered_on"] = trig["time"]
    out["fill"] = round(fill, 2)

    risk = abs(fill - stop)
    if risk <= 0:
        out["status"] = INVALID
        out["note"] = "Filled at the stop — no risk to measure the result against."
        return out

    def result(exit_px: float, closed_on: str | None, status: str, note: str):
        gain = (exit_px - fill) if long_ else (fill - exit_px)
        out["status"] = status
        out["closed_on"] = closed_on
        out["exit"] = round(exit_px, 2)
        out["r_multiple"] = round(gain / risk, 2)
        out["pct"] = round(gain / fill * 100.0, 2)
        out["bars_held"] = len(after) - ti
        out["note"] = note
        return out

    # From the trigger bar onward — the same bar can stop you out or pay you.
    for b in after[ti:]:
        hit_stop = b["low"] <= stop if long_ else b["high"] >= stop
        hit_target = b["high"] >= target if long_ else b["low"] <= target
        if hit_stop and hit_target:
            # Daily bars don't say which came first. Take the loss: assuming the
            # win would flatter every result that ever gapped around.
            out["ambiguous"] = True
            return result(stop, b["time"], STOP,
                          "Stop and target were both inside one day's range — "
                          "counted as stopped, because daily bars can't say which "
                          "came first.")
        if hit_stop:
            return result(stop, b["time"], STOP, "Stopped out.")
        if hit_target:
            return result(target, b["time"], TARGET, "Target reached.")

    last = after[-1].get("close")
    return result(last, None, OPEN,
                  f"Open since {trig['time']} — marked at the last close.")


def rows_for(items: list[dict], *, rng: str = "2y") -> list[dict]:
    """Evaluate many curated setups, fetching each symbol's daily bars in parallel.

    `items` are dicts carrying at least symbol/entry/stop/target/curated_on; each
    returned dict is the input plus an "eval" key. Soft-fails per row: one symbol
    Yahoo won't serve leaves that row unevaluated rather than emptying the page.
    """
    syms = sorted({(it.get("symbol") or "").strip().upper() for it in items if it.get("symbol")})
    bars: dict[str, list[dict]] = {}
    if syms:
        try:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for s, b in zip(syms, ex.map(lambda q: fetch_daily_ohlc(q, rng=rng), syms)):
                    bars[s] = b or []
        except Exception:  # noqa: BLE001
            bars = {}
    out = []
    for it in items:
        row = dict(it)
        sym = (it.get("symbol") or "").strip().upper()
        try:
            row["eval"] = evaluate(
                entry=it["entry"], stop=it["stop"], target=it["target"],
                curated_on=it["curated_on"], bars=bars.get(sym) or [],
            )
        except Exception:  # noqa: BLE001
            row["eval"] = {"status": WAITING, "note": "Could not read prices for this one.",
                           "direction": None, "triggered_on": None, "fill": None,
                           "closed_on": None, "exit": None, "r_multiple": None,
                           "pct": None, "last": None, "bars_waiting": None,
                           "bars_held": None, "ambiguous": False, "planned_rr": None}
        out.append(row)
    return out


def by_month(rows: list[dict]) -> list[dict]:
    """Group evaluated rows into months, newest first, with a summary per month.

    The summary counts only DECIDED setups (target or stop) for the win rate --
    an open trade has no outcome yet, and folding it in either way would move the
    number around for a reason that has nothing to do with how the month went.
    """
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault((r.get("curated_on") or "")[:7], []).append(r)

    out = []
    for month in sorted(buckets, reverse=True):
        rs = sorted(buckets[month], key=lambda r: (r.get("curated_on") or ""), reverse=True)
        decided = [r for r in rs if r["eval"]["status"] in (TARGET, STOP)]
        wins = [r for r in decided if r["eval"]["status"] == TARGET]
        rmults = [r["eval"]["r_multiple"] for r in rs
                  if r["eval"].get("r_multiple") is not None]
        out.append({
            "month": month,
            "label": _month_label(month),
            "rows": rs,
            "n": len(rs),
            "n_waiting": sum(1 for r in rs if r["eval"]["status"] == WAITING),
            "n_open": sum(1 for r in rs if r["eval"]["status"] == OPEN),
            "n_decided": len(decided),
            "win_rate": round(len(wins) / len(decided) * 100.0) if decided else None,
            "total_r": round(sum(rmults), 2) if rmults else None,
            "avg_r": round(sum(rmults) / len(rmults), 2) if rmults else None,
        })
    return out


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _month_label(month: str) -> str:
    try:
        y, m = month.split("-")
        return f"{_MONTHS[int(m) - 1]} {y}"
    except Exception:  # noqa: BLE001
        return month or "Undated"


# ---------------------------------------------------------------------------
# Storage. This module is the single place per-user isolation is enforced: every
# query below filters on user_id, so one member can never read or change another
# member's curated list. Portable ORM only (CLAUDE.md data-handling rule) --
# query-then-write, no SQLite-specific upsert, so the Postgres swap stays a
# config change.

def _clean_symbol(s: str) -> str:
    return (s or "").strip().upper()


def list_for(db, user) -> list[dict]:
    """This member's curated tickers as plain dicts, newest call first.

    Dicts rather than ORM objects so the evaluation layer above stays free of the
    database and can be exercised with hand-written rows in a test.
    """
    from ..models import CuratedTicker

    rows = (db.query(CuratedTicker)
              .filter(CuratedTicker.user_id == user.id)
              .order_by(CuratedTicker.curated_on.desc(), CuratedTicker.id.desc())
              .all())
    return [{"id": r.id, "symbol": r.symbol, "curated_on": r.curated_on,
             "entry": r.entry, "stop": r.stop, "target": r.target,
             "note": r.note or ""} for r in rows]


def add(db, user, *, symbol: str, curated_on: str, entry: float, stop: float,
        target: float, note: str = "", source: str = "created") -> tuple[bool, str]:
    """Store one curated call. Returns (ok, message).

    Validation refuses what cannot be judged later rather than storing it and
    showing "invalid" forever: the three levels must describe a trade, and the
    date must be a real ISO date (it anchors every trigger test).

    An opening revision is written with the call, so its history is complete from
    the start instead of beginning at the first edit. `source` records where the
    levels came from -- "chart" when they were drawn with the trade-setup tool.
    """
    from ..models import CuratedRevision, CuratedTicker

    sym = _clean_symbol(symbol)
    if not sym:
        return False, "Enter a ticker."
    try:
        entry, stop, target = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return False, "Entry, stop and target must be numbers."
    if plan_kind(entry, stop, target) is None:
        return False, ("Those levels don't describe a trade: the stop and the target "
                       "must sit on opposite sides of the entry.")
    d = (curated_on or "").strip()
    try:
        _dt.date.fromisoformat(d)
    except ValueError:
        return False, "Curated date must be a real date."

    note = (note or "").strip() or None
    row = CuratedTicker(user_id=user.id, symbol=sym, curated_on=d, entry=entry,
                        stop=stop, target=target, note=note)
    db.add(row)
    db.flush()          # need the id before the revision can point at it
    db.add(CuratedRevision(curated_id=row.id, symbol=sym, entry=entry, stop=stop,
                           target=target, note=note, source=source))
    db.commit()
    return True, f"{sym} curated."


def latest_for_symbol(db, user, symbol: str) -> dict | None:
    """This member's most recent curated call on one ticker, or None.

    "Most recent" is by curated date, then id -- the call you would be revising if
    you changed your mind about this ticker today. A ticker can carry several calls
    (the same name set up again months later), and the chart deliberately speaks
    only about the newest: it is the one whose levels are still live.
    """
    from ..models import CuratedTicker

    sym = _clean_symbol(symbol)
    if not sym:
        return None
    row = (db.query(CuratedTicker)
             .filter(CuratedTicker.user_id == user.id, CuratedTicker.symbol == sym)
             .order_by(CuratedTicker.curated_on.desc(), CuratedTicker.id.desc())
             .first())
    if row is None:
        return None
    return {"id": row.id, "symbol": row.symbol, "curated_on": row.curated_on,
            "entry": row.entry, "stop": row.stop, "target": row.target,
            "note": row.note or ""}


def update(db, user, row_id: int, **fields) -> tuple[bool, str]:
    """Edit one of this member's curated calls, recording the change as history.

    ``curated_on`` is NOT editable. It anchors every trigger test, so moving it
    would re-judge the call against a window it was never made in -- the one field
    that must stay honest. Anything passed for it is ignored rather than rejected,
    so a stale form cannot fail a legitimate edit.

    A revision row is written only when something actually changed; re-submitting
    identical levels does not manufacture history.

    ``source`` says where the new levels came from -- "chart" when they were drawn
    with the trade-setup tool, "edit" when typed into the table. It is popped out
    of ``fields`` because it describes the revision, not the call.
    """
    from ..models import CuratedRevision, CuratedTicker

    source = fields.pop("source", "edit")

    row = (db.query(CuratedTicker)
             .filter(CuratedTicker.id == row_id, CuratedTicker.user_id == user.id)
             .one_or_none())
    if row is None:
        return False, "Not found."
    try:
        entry = float(fields.get("entry", row.entry))
        stop = float(fields.get("stop", row.stop))
        target = float(fields.get("target", row.target))
    except (TypeError, ValueError):
        return False, "Entry, stop and target must be numbers."
    if plan_kind(entry, stop, target) is None:
        return False, ("Those levels don't describe a trade: the stop and the target "
                       "must sit on opposite sides of the entry.")
    sym = _clean_symbol(fields.get("symbol") or row.symbol)
    if not sym:
        return False, "Enter a ticker."
    note = ((fields.get("note") or "").strip() or None) if "note" in fields else row.note

    if (sym, entry, stop, target, note) == (row.symbol, row.entry, row.stop,
                                            row.target, row.note):
        return True, "No change."

    row.symbol, row.entry, row.stop, row.target, row.note = sym, entry, stop, target, note
    db.add(CuratedRevision(curated_id=row.id, symbol=sym, entry=entry, stop=stop,
                           target=target, note=note, source=source))
    db.commit()
    return True, f"{sym} updated."


def get_one(db, user, row_id: int) -> dict | None:
    """One of this member's curated calls, or None. Same user scoping as the rest."""
    from ..models import CuratedTicker

    r = (db.query(CuratedTicker)
           .filter(CuratedTicker.id == row_id, CuratedTicker.user_id == user.id)
           .one_or_none())
    if r is None:
        return None
    return {"id": r.id, "symbol": r.symbol, "curated_on": r.curated_on,
            "entry": r.entry, "stop": r.stop, "target": r.target, "note": r.note or ""}


def revisions_for(db, user, row_id: int) -> list[dict]:
    """The edit history of one call, NEWEST FIRST, with the current version marked.

    Joined through curated_tickers on user_id: a revision is only readable by the
    member who owns the call it belongs to, so guessing a revision id gets nothing.
    """
    from ..models import CuratedRevision, CuratedTicker

    rows = (db.query(CuratedRevision)
              .join(CuratedTicker, CuratedTicker.id == CuratedRevision.curated_id)
              .filter(CuratedRevision.curated_id == row_id,
                      CuratedTicker.user_id == user.id)
              .order_by(CuratedRevision.created_at.desc(), CuratedRevision.id.desc())
              .all())
    out = []
    for i, r in enumerate(rows):
        out.append({"id": r.id, "symbol": r.symbol, "entry": r.entry, "stop": r.stop,
                    "target": r.target, "note": r.note or "", "source": r.source,
                    "created_at": r.created_at, "current": i == 0,
                    "n": len(rows) - i})
    return out


def revisions_for_many(db, user, row_ids: list[int]) -> dict[int, list[dict]]:
    """{call id -> its revisions, newest first} for a whole page of calls.

    One query for the lot, because the table renders every call's history inline as
    child rows: calling ``revisions_for`` per row would fire a query per row for a
    list that is already paying a price fetch each. Same shape and same scoping as
    ``revisions_for`` -- joined through curated_tickers on user_id, so a call this
    member does not own contributes nothing.
    """
    from ..models import CuratedRevision, CuratedTicker

    ids = [i for i in (row_ids or []) if i]
    if not ids:
        return {}
    rows = (db.query(CuratedRevision)
              .join(CuratedTicker, CuratedTicker.id == CuratedRevision.curated_id)
              .filter(CuratedRevision.curated_id.in_(ids),
                      CuratedTicker.user_id == user.id)
              .order_by(CuratedRevision.created_at.desc(), CuratedRevision.id.desc())
              .all())
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r.curated_id, []).append(r)

    out: dict[int, list[dict]] = {}
    for cid, rs in grouped.items():
        out[cid] = [
            {"id": r.id, "symbol": r.symbol, "entry": r.entry, "stop": r.stop,
             "target": r.target, "note": r.note or "", "source": r.source,
             "created_at": r.created_at, "current": i == 0, "n": len(rs) - i}
            for i, r in enumerate(rs)
        ]
    return out


def get_revision(db, user, rev_id: int) -> tuple[dict, dict] | None:
    """(call, revision) for one revision id, or None. Scoped through the call's
    owner, so a guessed revision id belonging to someone else returns nothing."""
    from ..models import CuratedRevision, CuratedTicker

    hit = (db.query(CuratedRevision, CuratedTicker)
             .join(CuratedTicker, CuratedTicker.id == CuratedRevision.curated_id)
             .filter(CuratedRevision.id == rev_id, CuratedTicker.user_id == user.id)
             .one_or_none())
    if hit is None:
        return None
    rev, row = hit
    all_revs = revisions_for(db, user, row.id)
    meta = next((r for r in all_revs if r["id"] == rev.id), None) or {}
    return (
        {"id": row.id, "symbol": row.symbol, "curated_on": row.curated_on,
         "entry": row.entry, "stop": row.stop, "target": row.target,
         "note": row.note or ""},
        {"id": rev.id, "symbol": rev.symbol, "entry": rev.entry, "stop": rev.stop,
         "target": rev.target, "note": rev.note or "", "source": rev.source,
         "created_at": rev.created_at,
         "n": meta.get("n"), "current": bool(meta.get("current"))},
    )


def remove(db, user, row_id: int) -> bool:
    """Delete one of this member's curated calls. True if a row went."""
    from ..models import CuratedTicker

    row = (db.query(CuratedTicker)
             .filter(CuratedTicker.id == row_id, CuratedTicker.user_id == user.id)
             .one_or_none())
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def overall(months: list[dict]) -> dict:
    """Totals across every month, for the header strip."""
    rows = [r for m in months for r in m["rows"]]
    decided = [r for r in rows if r["eval"]["status"] in (TARGET, STOP)]
    wins = [r for r in decided if r["eval"]["status"] == TARGET]
    rmults = [r["eval"]["r_multiple"] for r in rows
              if r["eval"].get("r_multiple") is not None]
    return {
        "n": len(rows),
        "n_waiting": sum(1 for r in rows if r["eval"]["status"] == WAITING),
        "n_open": sum(1 for r in rows if r["eval"]["status"] == OPEN),
        "n_decided": len(decided),
        "win_rate": round(len(wins) / len(decided) * 100.0) if decided else None,
        "total_r": round(sum(rmults), 2) if rmults else None,
        "avg_r": round(sum(rmults) / len(rmults), 2) if rmults else None,
    }


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def calendar_index(rows: list[dict]) -> dict:
    """{year: [12 counts]} — how many calls were curated in each month of each year.

    Drives the Jan-Dec tab strip. Every month of a shown year gets a slot even when
    it is empty: a fixed twelve-tab row is the point, so the reader can see at a
    glance which months are bare rather than hunting through a variable list.
    """
    out: dict = {}
    for r in rows:
        d = (r.get("curated_on") or "")
        if len(d) < 7:
            continue
        try:
            y, m = int(d[:4]), int(d[5:7])
        except ValueError:
            continue
        if not 1 <= m <= 12:
            continue
        out.setdefault(y, [0] * 12)[m - 1] += 1
    return out
