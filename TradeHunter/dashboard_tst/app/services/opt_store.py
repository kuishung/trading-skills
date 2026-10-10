"""The Options data layer (OPTIONS_V2_DESIGN.md §2, §13): ORM reads and writes for the
option and stock data the Hermes collector reads from Massive (formerly Polygon.io).

What lives here
---------------
* the quote pool (``opt_quote``): ``upsert_quotes`` (newer wins per contract, each row
  stamped with the feed's own time) and ``chain_view`` (what the screeners read - DTE /
  age pre-filters, a Core select and an in-process cache keyed by the symbol's newest
  ``opt_refresh_log`` id);
* the stock facts (``opt_underlying``, ``opt_underlying_daily``): ``set_spot``,
  ``upsert_daily``, ``recompute_underlying`` (§4.1), ``set_earnings``,
  ``mark_history_done``, ``underlying`` / ``underlyings``;
* freshness and the universe: ``freshness`` (newest ``opt_refresh_log`` per symbol, one
  bounded query) and ``universe`` (every active basket symbol, most-held first);
* the collector heartbeat (``opt_collector_status`` row 1);
* the EOD record and retention: ``snapshot_eod`` (into ``option_chain_snapshot``) and
  ``prune_v2``.

Since v4.134 (§13) ONE vendor writes everything - the collector's Massive client, through
``opt_massive`` (the Options Starter chain snapshot, 15 min delayed, no bid/ask; Stocks
Basic end-of-day bars). Members no longer contribute data, so the v4.133 member machinery
(contribution validation, rate limits, leases, back-off, the IBKR-anchored spot band) is
gone. Rows the IBKR build wrote (``source`` ``hermes`` / ``member``, see
``LEGACY_SOURCES``) are still READ like any other until ``prune_v2``'s 7-day rule removes
them, and a Massive write always replaces one, whatever its time.

Rules this module enforces so nothing else has to
--------------------------------------------------
* Every quote carries ``as_of`` (naive UTC - the snapshot's read time minus the 15-min
  delay, or a quote's own time on a plan with quotes, so the screener's market-time age is
  the data's age),
  ``source`` (``massive``) and ``mdt`` (``delayed`` on the Starter plan; ``live`` on a
  real-time plan; ``eod`` for a closing value).
* Newer wins per contract; on an equal ``as_of`` a better data type wins (live > delayed
  > eod); contracts not in a payload are untouched.
* ``mid`` is (bid + ask) / 2 whenever both are present (a posted mid is then ignored);
  without a two-sided quote - the Starter plan has none - ``mid`` is the price the caller
  posted (``opt_massive.model_price``: Black-Scholes from the contract's own IV).
* Row sanity on write - data from one trusted vendor needs only that: a row with no
  usable expiry / right / strike, a negative price, an iv outside (0.01, 5) or
  |delta| > 1 is dropped (counted in ``skipped_bad``, not stored).
* Per-contract ``iv`` is a FRACTION; per-day vol figures (``iv30``, ``hv20``, ``iv_lo``
  ...) are PERCENT.

Portable ORM only (the platform data-handling rule): query-then-update/insert upserts,
plain ``delete(synchronize_session=False)`` prunes, no SQLite-specific syntax. Every
public write commits (one unit of work per call) and retries once on a unique-key race
with another writer.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import threading
import time

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.exc import IntegrityError

from ..models import (OptCollectorStatus, OptionBasket, OptionChainSnapshot, OptQuote,
                      OptRefreshLog, OptUnderlying, OptUnderlyingDaily, User)
from . import clock, option_metrics

log = logging.getLogger(__name__)

# What is WRITTEN (§13.1): every figure comes from Massive.
SOURCES = ("massive",)
# The IBKR build's rows (v4.133): read like any other, never written, always replaced by a
# Massive write; ``prune_v2`` removes them once nobody refreshed them for 7 days.
LEGACY_SOURCES = ("hermes", "member")
# Data types written: ``delayed`` (Options Starter, 15 min), ``live`` (a real-time plan),
# ``eod`` (a closing value - e.g. the Stocks Basic close as the spot).
MDT = ("live", "delayed", "eod")
LEGACY_MDT = ("frozen", "delayed_frozen")          # IBKR types old rows may carry (read only)
KINDS = ("history", "cycle", "eod", "manual")       # opt_refresh_log.kind written now
# A tie on as_of goes to the better type (lower rank); legacy types keep their old place.
_MDT_RANK = {m: i for i, m in enumerate(("live", "frozen", "delayed", "delayed_frozen", "eod"))}

# Row sanity on write
IV_LO = 0.01                  # per-contract iv kept strictly inside (0.01, 5.0), FRACTION
IV_HI = 5.0
DELTA_MAX = 1.0               # |delta| <= 1

# chain_view's in-process cache
CHAIN_CACHE_TTL_S = 300.0     # a safety net: the key already changes with every refresh-log row
CHAIN_CACHE_MAX = 512

# §4.1 stats + daily history bounds
ATR_N = 14
HV_SHORT, HV_LONG = 20, 60
AVG_VOL_N = 20
IV_WINDOW = 252
DAILY_MAX_POINTS = 800        # per call, newest kept (2 years of bars is ~504)
DAILY_IV_LO, DAILY_IV_HI = 0.1, 1000.0   # PERCENT bounds for a daily iv30 point

# Retention
LOG_KEEP_DAYS = 30
EXPIRED_GRACE_DAYS = 7
STALE_QUOTE_DAYS = 7          # a quote nobody refreshed for 7 days (delisted keys, legacy IBKR rows)

_PRICE_FIELDS = ("bid", "ask", "mid", "last", "und_price")
_GREEK_FIELDS = ("delta", "gamma", "theta", "vega")
_SIZE_FIELDS = ("bid_size", "ask_size", "volume", "oi")
_QUOTE_FIELDS = ("bid", "ask", "mid", "last", "bid_size", "ask_size", "volume", "oi",
                 "iv", "delta", "gamma", "theta", "vega", "und_price")


# ────────────────────────────────── small helpers ──────────────────────────────────

def _sym(s) -> str:
    return str(s or "").strip().upper()[:20]


def _f(v) -> float | None:
    """A finite float, else None (bools, NaN, inf and junk are None)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _i(v) -> int | None:
    f = _f(v)
    return None if f is None else int(round(f))


def _naive_utc(ts) -> _dt.datetime | None:
    """A timestamp as naive UTC: aware input is converted, naive is taken as UTC,
    ISO strings are parsed; anything else is None."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = _dt.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(ts, _dt.datetime):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return ts


def _now(now=None) -> _dt.datetime:
    """Naive UTC now (or ``now`` normalised; epoch seconds are accepted too)."""
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return _dt.datetime.fromtimestamp(float(now), _dt.timezone.utc).replace(tzinfo=None)
    return _naive_utc(now) or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _day(d=None) -> str:
    """An ET date as ``YYYY-MM-DD``; None means today in New York (clock)."""
    if d is None:
        return clock.et_today()
    if isinstance(d, _dt.datetime):
        return d.date().isoformat()
    if isinstance(d, _dt.date):
        return d.isoformat()
    return str(d)[:10]


def _date_str(v) -> str | None:
    """``YYYY-MM-DD`` from a date, a datetime, ``YYYY-MM-DD...`` or ``YYYYMMDD``; else None."""
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    s = str(v or "").strip()
    if len(s) == 8 and s.isdigit():
        s = "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _today(today=None, now=None) -> str:
    """The ET date: ``today`` when given, else the ET date of ``now`` (else of the wall
    clock)."""
    if today is not None:
        return _day(today)
    return clock.et_today(_now(now)) if now is not None else clock.et_today()


def _dte(expiry: str, on: str) -> int:
    return (_dt.date.fromisoformat(expiry) - _dt.date.fromisoformat(on)).days


def _minus(day: str, days: int) -> str:
    return (_dt.date.fromisoformat(day) - _dt.timedelta(days=days)).isoformat()


def _plus(day: str, days: int) -> str:
    return (_dt.date.fromisoformat(day) + _dt.timedelta(days=days)).isoformat()


def norm_mdt(v) -> str | None:
    """A data type that may be WRITTEN, as its name (``live`` | ``delayed`` | ``eod``;
    case, ``-`` and space spellings forgiven). None when unknown - the IBKR types of the
    old rows (``frozen``, ``delayed_frozen``) and IBKR's numeric codes are not written
    any more."""
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip().lower().replace("-", "_").replace(" ", "_")
    return s if s in MDT else None


def _check_source(fn: str, source) -> str:
    if source not in SOURCES:
        raise ValueError("%s: source must be one of %s (%s rows are read, never written)"
                         % (fn, SOURCES, "/".join(LEGACY_SOURCES)))
    return source


def _wins(new_as_of, new_mdt, old_as_of, old_mdt, old_source=None) -> bool:
    """The merge rule: write when nothing is stored, when the stored value is a legacy
    IBKR one (``LEGACY_SOURCES`` - a Massive read always replaces it), when the incoming
    ``as_of`` is later, or when it is equal and the incoming data type is at least as
    good (live > delayed > eod)."""
    if old_source in LEGACY_SOURCES:
        return True
    old = _naive_utc(old_as_of)
    if old is None:
        return True
    if new_as_of > old:
        return True
    if new_as_of < old:
        return False
    return _MDT_RANK.get(new_mdt, 99) <= _MDT_RANK.get(old_mdt, 99)


def _txn(db, fn):
    """Run ``fn`` then commit; on a unique-key race with another writer roll back
    and run it once more (the second pass re-reads what the other writer stored)."""
    for attempt in (1, 2):
        try:
            out = fn()
            db.commit()
            return out
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
            log.info("opt_store: unique-key race, retrying once")
    return None   # pragma: no cover


def _row_dict(obj, model) -> dict:
    return {c.name: getattr(obj, c.name) for c in model.__table__.columns}


def _und_row(db, sym: str, *, create: bool, now: _dt.datetime | None = None):
    row = db.query(OptUnderlying).filter(OptUnderlying.symbol == sym).one_or_none()
    if row is None and create:
        t = now or _now()
        row = OptUnderlying(symbol=sym, first_seen=t, history_done=False, updated_at=t)
        db.add(row)
        db.flush()
    return row


# ────────────────────────────────── quote rows ──────────────────────────────────

def _mid(bid, ask) -> float | None:
    return round((bid + ask) / 2.0, 4) if (bid is not None and ask is not None) else None


def _norm_row(r) -> dict | None:
    """One feed row (``opt_massive`` shape, ``iv`` a FRACTION) as the stored shape plus
    its own ``as_of`` (``as_of`` or ``last_updated``; None when the row has none), or
    None when it fails the write sanity rules: no usable expiry / right (C | P, or
    call / put) / strike > 0, a negative price (bid, ask, mid, last, und_price), an iv
    outside (0.01, 5) or |delta| > 1. Junk numbers (NaN, text) read as absent; a
    negative size reads as absent. ``mid`` = (bid + ask) / 2 when both are present,
    else the posted mid (the model price)."""
    if not isinstance(r, dict):
        return None
    expiry = _date_str(r.get("expiry"))
    right = str(r.get("right") or "").strip().upper()
    if right in ("CALL", "PUT"):
        right = right[:1]
    strike = _f(r.get("strike"))
    if not expiry or right not in ("C", "P") or strike is None or strike <= 0:
        return None
    out: dict = {"expiry": expiry, "right": right, "strike": round(strike, 4)}
    for k in _PRICE_FIELDS:
        v = _f(r.get(k))
        if v is not None and v < 0:
            return None
        out[k] = v
    for k in _SIZE_FIELDS:
        v = _i(r.get(k, r.get("open_interest") if k == "oi" else None))
        out[k] = v if (v is not None and v >= 0) else None
    iv = _f(r.get("iv"))
    if iv is not None and not (IV_LO < iv < IV_HI):
        return None
    out["iv"] = iv
    for k in _GREEK_FIELDS:
        out[k] = _f(r.get(k))
    if out["delta"] is not None and abs(out["delta"]) > DELTA_MAX:
        return None
    if out["bid"] is not None and out["ask"] is not None:
        out["mid"] = _mid(out["bid"], out["ask"])
    out["as_of"] = _naive_utc(r.get("as_of") if r.get("as_of") is not None else r.get("last_updated"))
    return out


def upsert_quotes(db, symbol, rows, *, source="massive", mdt="delayed", as_of=None, kind="cycle",
                  ms=None, spot=None, error=None, now=None) -> dict:
    """Merge one read's contract rows into the pool, write one ``opt_refresh_log`` row
    and, when ``spot`` is given, the underlying's spot fields. Commits.

    Each row is stamped with its OWN ``as_of`` (the feed's ``last_updated``; also read
    from a ``last_updated`` key), else the call's ``as_of``, else server now. Per
    contract the newer stamp wins (equal: the better data type; a legacy IBKR row is
    always replaced); contracts not in ``rows`` are untouched. The log row's ``as_of``
    is the call's ``as_of``, else the NEWEST row stamp of the read (the time the data
    shows, which is what ``freshness`` reports), else server now; the spot is filed at
    that stamp too. Rows failing the sanity rules (``_norm_row``) are skipped
    (``skipped_bad``); duplicate contracts in one payload: the last one wins.

    Only ``source="massive"`` is written (``SOURCES``); ``mdt`` is one of ``MDT``.
    Returns ``{"stored", "skipped_older", "skipped_bad", "n_expiries", "log_id"}``
    (``n_expiries`` counts the expiries that had a row written)."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("upsert_quotes: no symbol")
    _check_source("upsert_quotes", source)
    mdt_name = norm_mdt(mdt)
    if mdt_name is None:
        raise ValueError("upsert_quotes: unknown data type %r (one of %s)" % (mdt, MDT))
    server_now = _now(now)
    call_stamp = _naive_utc(as_of)
    spot_f = _f(spot)
    spot_f = spot_f if (spot_f is not None and spot_f > 0) else None

    incoming: dict[tuple, dict] = {}
    bad = 0
    for r in rows or ():
        n = _norm_row(r)
        if n is None:
            bad += 1
            continue
        n["as_of"] = n["as_of"] or call_stamp or server_now
        incoming[(n["expiry"], n["right"], n["strike"])] = n
    newest = max((n["as_of"] for n in incoming.values()), default=None)
    log_stamp = call_stamp or newest or server_now

    def apply() -> dict:
        exps = sorted({k[0] for k in incoming})
        existing: dict[tuple, OptQuote] = {}
        if exps:
            for q in (db.query(OptQuote)
                        .filter(OptQuote.symbol == sym, OptQuote.expiry.in_(exps))):
                existing[(q.expiry, q.right, round(float(q.strike), 4))] = q
        stored = skipped = 0
        written_exps: set[str] = set()
        for key, n in incoming.items():
            q = existing.get(key)
            if q is not None and not _wins(n["as_of"], mdt_name, q.as_of, q.mdt, q.source):
                skipped += 1
                continue
            if q is None:
                q = OptQuote(symbol=sym, expiry=key[0], right=key[1], strike=key[2])
                db.add(q)
            for f in _QUOTE_FIELDS:
                setattr(q, f, n[f])
            if q.und_price is None:
                q.und_price = spot_f
            q.as_of = n["as_of"]
            q.source = source
            q.source_user_id = None
            q.mdt = mdt_name
            q.updated_at = server_now
            stored += 1
            written_exps.add(key[0])
        if spot_f is not None:
            _set_spot(db, sym, spot_f, source=source, mdt=mdt_name, as_of=log_stamp, now=server_now)
        entry = OptRefreshLog(symbol=sym, as_of=log_stamp, source=source, source_user_id=None,
                              mdt=mdt_name, kind=str(kind or "cycle")[:10], n_contracts=stored,
                              n_expiries=len(written_exps), ms=_i(ms),
                              error=(str(error) if error else None))
        db.add(entry)
        db.flush()
        return {"stored": stored, "skipped_older": skipped, "skipped_bad": bad,
                "n_expiries": len(written_exps), "log_id": entry.id}

    return _txn(db, apply)


# ────────────────────────────────── chain + underlying reads ──────────────────────────────────

_Q = OptQuote
_CHAIN_COLS = (_Q.expiry, _Q.right, _Q.strike, _Q.bid, _Q.ask, _Q.mid, _Q.last, _Q.bid_size,
               _Q.ask_size, _Q.volume, _Q.oi, _Q.iv, _Q.delta, _Q.gamma, _Q.theta, _Q.vega,
               _Q.und_price, _Q.as_of, _Q.source, _Q.source_user_id, _Q.mdt,
               User.display_name.label("source_name"))

_chain_lock = threading.Lock()
_chain_cache: dict[tuple, tuple[float, list, tuple | None]] = {}   # key -> (built, expiries, newest)


def _bind_key(db) -> str:
    try:
        return str(db.get_bind().url)
    except Exception:  # noqa: BLE001 - a bind-less session: key on the session itself
        return "session:%d" % id(db)


def _chain_rows(db, sym: str, lo: str, hi: str | None, cutoff, day: str):
    """Core select of the columns the view needs (no ORM entities), ordered by expiry,
    right, strike -> ``(expiries, newest)``; ``newest`` = (und_price, as_of, source, mdt)
    of the newest row that carries an und_price, or None."""
    stmt = (select(*_CHAIN_COLS)
            .select_from(_Q)
            .outerjoin(User, User.id == _Q.source_user_id)
            .where(_Q.symbol == sym, _Q.expiry >= lo))
    if hi is not None:
        stmt = stmt.where(_Q.expiry <= hi)
    if cutoff is not None:
        stmt = stmt.where(_Q.as_of >= cutoff)
    stmt = stmt.order_by(_Q.expiry, _Q.right, _Q.strike)
    out: list[dict] = []
    cur = None
    newest = None
    for r in db.execute(stmt):
        if cur is None or cur["expiry"] != r.expiry:
            cur = {"expiry": r.expiry, "dte": _dte(r.expiry, day), "calls": [], "puts": []}
            out.append(cur)
        # the midpoint whenever both sides are quoted (never a stored mid that could
        # disagree with them); without a two-sided quote, the stored model price
        mid = _mid(r.bid, r.ask) if (r.bid is not None and r.ask is not None) else r.mid
        as_of = _naive_utc(r.as_of)
        (cur["calls"] if r.right == "C" else cur["puts"]).append({
            "strike": r.strike, "bid": r.bid, "ask": r.ask, "mid": mid, "last": r.last,
            "bid_size": r.bid_size, "ask_size": r.ask_size, "volume": r.volume, "oi": r.oi,
            "iv": r.iv, "delta": r.delta, "gamma": r.gamma, "theta": r.theta, "vega": r.vega,
            "und_price": r.und_price, "as_of": as_of, "source": r.source,
            "source_user_id": r.source_user_id, "source_name": (r.source_name or None),
            "mdt": r.mdt})
        if r.und_price is not None and as_of is not None and (newest is None or as_of > newest[1]):
            newest = (r.und_price, as_of, r.source, r.mdt)
    return out, newest


def chain_view(db, symbol, *, today=None, dte_min=None, dte_max=None, max_age_h=None,
               now=None) -> dict:
    """The stored chain for ``symbol``: expiries ascending (only ``expiry >= today``,
    the ET date), calls and puts by strike ascending, each row with its own ``as_of``
    / ``source`` / ``mdt`` (and ``source_user_id`` / ``source_name``, set only on a
    legacy member row). ``mid`` is the bid/ask midpoint when both are present, else the
    stored model price. The spot comes from ``opt_underlying``; when that has none, from
    the newest loaded quote's ``und_price``.

    Pre-filters (each optional): only expiries with DTE in ``[dte_min, dte_max]``; only
    rows with ``as_of >= now - max_age_h`` hours. ``max_age_h`` is WALL-clock and coarse
    (the cutoff is floored to the hour, so a little more is loaded, never less): the
    screener passes its own limit + 96 h so a weekend never empties the chain, and
    applies the exact market-time age itself.

    The rows are read with a Core select of the needed columns and cached in-process
    under (database, symbol, ET day, DTE window, age bucket, the symbol's newest
    ``opt_refresh_log`` id) - every ``upsert_quotes`` logs a row, so a write changes the
    key. Entries also expire after 5 min (a prune in another process, or a write by a
    transaction that committed behind a newer id on a multi-writer database). Callers
    get their own copies."""
    sym = _sym(symbol)
    t_now = _now(now)
    day = _today(today, now)
    lo = day
    if dte_min is not None and _f(dte_min) is not None:
        lo = max(day, _plus(day, int(math.ceil(_f(dte_min)))))
    hi = None
    if dte_max is not None and _f(dte_max) is not None:
        hi = _plus(day, int(math.floor(_f(dte_max))))
    cutoff = None
    age_h = _f(max_age_h)
    if max_age_h is not None and age_h is not None and age_h >= 0:
        cutoff = (t_now - _dt.timedelta(hours=age_h)).replace(minute=0, second=0, microsecond=0)
    L = OptRefreshLog
    log_id = db.execute(select(func.max(L.id)).where(L.symbol == sym)).scalar()
    key = (_bind_key(db), sym, day, lo, hi, cutoff, log_id)
    mono = time.monotonic()
    with _chain_lock:
        hit = _chain_cache.get(key)
    if hit is not None and mono - hit[0] < CHAIN_CACHE_TTL_S:
        expiries, newest = hit[1], hit[2]
    else:
        expiries, newest = ([], None) if (hi is not None and hi < lo) else \
            _chain_rows(db, sym, lo, hi, cutoff, day)
        with _chain_lock:
            _chain_cache[key] = (mono, expiries, newest)
            if len(_chain_cache) > CHAIN_CACHE_MAX:
                for k in [k for k, v in _chain_cache.items() if mono - v[0] >= CHAIN_CACHE_TTL_S]:
                    _chain_cache.pop(k, None)
                while len(_chain_cache) > CHAIN_CACHE_MAX:
                    _chain_cache.pop(next(iter(_chain_cache)), None)
    und = _und_row(db, sym, create=False)
    if und is not None and und.spot:
        spot, spot_as_of, spot_source, spot_mdt = und.spot, und.spot_as_of, und.spot_source, und.spot_mdt
    elif newest is not None:
        spot, spot_as_of, spot_source, spot_mdt = newest
    else:
        spot = spot_as_of = spot_source = spot_mdt = None
    return {"symbol": sym, "spot": spot, "spot_as_of": _naive_utc(spot_as_of),
            "spot_source": spot_source, "spot_mdt": spot_mdt,
            "expiries": [{"expiry": e["expiry"], "dte": e["dte"],
                          "calls": [dict(r) for r in e["calls"]],
                          "puts": [dict(r) for r in e["puts"]]} for e in expiries]}


def underlying(db, symbol) -> dict | None:
    """Every ``opt_underlying`` column of ``symbol`` by name, or None."""
    row = _und_row(db, _sym(symbol), create=False)
    return _row_dict(row, OptUnderlying) if row is not None else None


def underlyings(db, symbols) -> dict[str, dict]:
    """``{symbol: underlying dict}`` for every symbol that has a row (one query)."""
    syms = sorted({_sym(s) for s in symbols or () if _sym(s)})
    if not syms:
        return {}
    rows = db.query(OptUnderlying).filter(OptUnderlying.symbol.in_(syms)).all()
    return {r.symbol: _row_dict(r, OptUnderlying) for r in rows}


def _set_spot(db, sym, spot, *, source, mdt, as_of, now) -> bool:
    """Write the spot fields when the reading is not older than the stored one (the
    same rule as a quote; a legacy IBKR spot is always replaced). Flushes, does not
    commit."""
    u = _und_row(db, sym, create=True, now=now)
    if u.spot is not None and not _wins(as_of, mdt, u.spot_as_of, u.spot_mdt, u.spot_source):
        return False
    u.spot = float(spot)
    u.spot_as_of = as_of
    u.spot_source = source
    u.spot_user_id = None
    u.spot_mdt = mdt
    u.updated_at = now
    db.flush()
    return True


def set_spot(db, symbol, spot, *, source="massive", mdt="delayed", as_of=None, now=None) -> bool:
    """Record a stock price for ``symbol`` - the snapshot's underlying price or an
    estimate (``mdt`` ``delayed``) or the Stocks Basic close (``mdt`` ``eod``). Newer
    wins, as for quotes (a legacy IBKR spot is always replaced); ``as_of`` defaults to
    server now. Creates the ``opt_underlying`` row when missing. Commits; returns
    whether the spot was written."""
    sym = _sym(symbol)
    s = _f(spot)
    if not sym or s is None or s <= 0:
        raise ValueError("set_spot: need a symbol and a positive spot")
    _check_source("set_spot", source)
    mdt_name = norm_mdt(mdt)
    if mdt_name is None:
        raise ValueError("set_spot: unknown data type %r (one of %s)" % (mdt, MDT))
    server_now = _now(now)
    stamp = _naive_utc(as_of) or server_now
    return bool(_txn(db, lambda: _set_spot(db, sym, s, source=source, mdt=mdt_name,
                                           as_of=stamp, now=server_now)))


# ────────────────────────────────── daily history + stats ──────────────────────────────────

def upsert_daily(db, symbol, bars=None, iv_series=None, *, source="massive", today=None,
                 now=None) -> int:
    """File daily history for ``symbol`` into ``opt_underlying_daily``.

    ``bars`` = ``[{"on", "open", "high", "low", "close", "volume"}]`` (Massive Stocks
    Basic daily bars, oldest first); ``iv_series`` = ``[{"on", "iv"}]`` (or ``(on, iv)``
    pairs) with ``iv`` the day's 30-day IV in PERCENT (``opt_massive``). A day already on
    file is updated field by field (bars set close / high / low / volume, the series
    sets iv30), so a later value for a day replaces the earlier one and neither input
    erases the other's columns; the row's ``source`` becomes ``massive``. Points are
    skipped when the date is unreadable or after ``today`` (ET), the close is not
    positive, high < low, or iv is outside 0.1-1000. At most the newest 800 points of
    each input are read. Commits; returns the number of day rows written."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("upsert_daily: no symbol")
    _check_source("upsert_daily", source)
    day = _day(today)
    stamp = _now(now)

    bar_by_day: dict[str, dict] = {}
    for b in list(bars or ())[-DAILY_MAX_POINTS:]:
        if not isinstance(b, dict):
            continue
        on = _date_str(b.get("on") or b.get("date") or b.get("time"))
        close = _f(b.get("close"))
        if on is None or on > day or close is None or close <= 0:
            continue
        high, low = _f(b.get("high")), _f(b.get("low"))
        if high is not None and low is not None and high < low:
            continue
        vol = _f(b.get("volume"))
        bar_by_day[on] = {"close": close, "high": high, "low": low,
                          "volume": vol if (vol is not None and vol >= 0) else None}
    iv_by_day: dict[str, float] = {}
    for p in list(iv_series or ())[-DAILY_MAX_POINTS:]:
        if isinstance(p, dict):
            on, v = _date_str(p.get("on") or p.get("date")), _f(p.get("iv", p.get("iv30")))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            on, v = _date_str(p[0]), _f(p[1])
        else:
            continue
        if on is None or on > day or v is None or not (DAILY_IV_LO <= v <= DAILY_IV_HI):
            continue
        iv_by_day[on] = v
    days = sorted(set(bar_by_day) | set(iv_by_day))
    if not days:
        return 0

    def apply() -> int:
        existing: dict[str, OptUnderlyingDaily] = {}
        for i in range(0, len(days), 500):
            for r in (db.query(OptUnderlyingDaily)
                        .filter(OptUnderlyingDaily.symbol == sym,
                                OptUnderlyingDaily.on.in_(days[i:i + 500]))):
                existing[r.on] = r
        n = 0
        for on in days:
            r = existing.get(on)
            if r is None:
                r = OptUnderlyingDaily(symbol=sym, on=on, source=source)
                db.add(r)
            bar = bar_by_day.get(on)
            if bar is not None:
                for k, v in bar.items():
                    if v is not None:
                        setattr(r, k, v)
            if on in iv_by_day:
                r.iv30 = iv_by_day[on]
            r.source = source
            r.as_of = stamp
            n += 1
        db.flush()
        return n

    return _txn(db, apply)


def _wilder_atr(highs, lows, closes, period: int = ATR_N) -> float | None:
    """The last Wilder ATR: SMA of the first ``period`` true ranges, then the running
    average (support_bounce.atr_series' definition). None under period + 1 bars."""
    n = len(closes)
    if n < period + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, n)]
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def recompute_underlying(db, symbol, *, now=None) -> dict:
    """§4.1: the stock statistics of ``symbol`` from its ``opt_underlying_daily`` rows -
    ``atr14`` (Wilder, high / low / close), ``hv20`` / ``hv60`` (``option_metrics.hv`` on
    the closes, PERCENT), ``avg_vol20`` (mean of the last 20 volumes), ``iv30`` (the last
    daily IV30 on file) and ``iv_rank`` / ``iv_pct`` / ``iv_n`` / ``iv_lo`` / ``iv_hi``
    from ``option_metrics.iv_rank_pct`` over the last 252 iv30 values. A figure without
    enough history is None. Creates the row when missing. Commits; returns the
    underlying dict."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("recompute_underlying: no symbol")
    stamp = _now(now)

    def apply() -> dict:
        rows = (db.query(OptUnderlyingDaily)
                  .filter(OptUnderlyingDaily.symbol == sym)
                  .order_by(OptUnderlyingDaily.on)
                  .all())
        bars = [r for r in rows if r.close is not None]
        closes = [float(r.close) for r in bars]
        hl = [r for r in bars if r.high is not None and r.low is not None]
        atr = _wilder_atr([float(r.high) for r in hl], [float(r.low) for r in hl],
                          [float(r.close) for r in hl])
        vols = [float(r.volume) for r in bars if r.volume is not None]
        ivs = [float(r.iv30) for r in rows if r.iv30 is not None]
        iv30 = ivs[-1] if ivs else None
        rk = option_metrics.iv_rank_pct(ivs[-IV_WINDOW:], iv30)

        u = _und_row(db, sym, create=True, now=stamp)
        u.atr14 = atr
        u.hv20 = option_metrics.hv(closes, HV_SHORT)
        u.hv60 = option_metrics.hv(closes, HV_LONG)
        u.avg_vol20 = (sum(vols[-AVG_VOL_N:]) / AVG_VOL_N) if len(vols) >= AVG_VOL_N else None
        bar_stamps = [r.as_of for r in bars if r.as_of is not None]
        u.bars_as_of = max(bar_stamps) if bar_stamps else None
        u.iv30 = iv30
        u.iv_rank = rk.get("iv_rank")
        u.iv_pct = rk.get("iv_pct")
        u.iv_n = rk.get("iv_n")
        u.iv_lo = rk.get("lo")
        u.iv_hi = rk.get("hi")
        iv_stamps = [r.as_of for r in rows if r.iv30 is not None and r.as_of is not None]
        u.iv_as_of = max(iv_stamps) if iv_stamps else None
        u.updated_at = stamp
        db.flush()
        return _row_dict(u, OptUnderlying)

    return _txn(db, apply)


def set_earnings(db, symbol, date_or_none, *, src: str = "yahoo", now=None) -> None:
    """The next earnings date (free source - the one figure not from Massive, §13.0)
    or None when there is none. Accepts a date, ``YYYY-MM-DD`` or ``{"date": ...}``.
    Commits."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("set_earnings: no symbol")
    val = date_or_none.get("date") if isinstance(date_or_none, dict) else date_or_none
    on = _date_str(val) if val else None
    stamp = _now(now)

    def apply() -> None:
        u = _und_row(db, sym, create=True, now=stamp)
        u.earnings_date = on
        u.earnings_src = str(src or "")[:8] or None
        u.earnings_as_of = stamp
        u.updated_at = stamp
        db.flush()

    _txn(db, apply)


def mark_history_done(db, symbol, *, now=None) -> None:
    """Record that the one-time history pull for ``symbol`` (2 years of daily bars and
    the IV30 series, ``opt_massive.backfill_history``) happened. Commits."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("mark_history_done: no symbol")
    stamp = _now(now)

    def apply() -> None:
        u = _und_row(db, sym, create=True, now=stamp)
        u.history_done = True
        u.updated_at = stamp
        db.flush()

    _txn(db, apply)


# ────────────────────────────────── freshness + universe ──────────────────────────────────

def freshness(db, symbols, *, now=None) -> dict[str, dict]:
    """``{symbol: {"as_of", "source", "source_user_id", "source_name", "mdt", "n",
    "kind", "age_min"}}`` from the newest ``opt_refresh_log`` row per symbol that wrote
    at least one contract (an empty or failed read never makes a symbol look fresh) and
    is not a legacy ``trade`` refresh (the IBKR build's few-legs re-read). ``as_of`` is
    the time the data shows (``upsert_quotes``: the newest row's feed stamp), so
    ``age_min`` (wall clock) includes the feed's delay. Ties on ``as_of`` go to the later
    row. A symbol with no such row is absent.

    One bounded query: per requested symbol a ``LIMIT 1`` subquery walks the
    ``(symbol, as_of)`` index from the newest row and stops at the first qualifying
    one, so the cost does not grow with the 30 days of retained log rows."""
    syms = sorted({_sym(s) for s in symbols or () if _sym(s)})
    if not syms:
        return {}
    L = OptRefreshLog
    picks = [select(L.id)
             .where(L.symbol == s, L.n_contracts > 0, L.kind != "trade")
             .order_by(L.as_of.desc(), L.id.desc())
             .limit(1)
             .scalar_subquery()
             for s in syms]
    stmt = (select(L, User.display_name)
            .outerjoin(User, User.id == L.source_user_id)
            .where(L.id.in_(picks)))
    t = _now(now)
    out: dict[str, dict] = {}
    for row, name in db.execute(stmt).all():
        as_of = _naive_utc(row.as_of)
        out[row.symbol] = {
            "as_of": as_of, "source": row.source, "source_user_id": row.source_user_id,
            "source_name": name or None, "mdt": row.mdt, "n": row.n_contracts,
            "kind": row.kind,
            "age_min": round(max(0.0, (t - as_of).total_seconds()) / 60.0, 1),
        }
    return out


def universe(db) -> list[tuple[str, int]]:
    """``[(symbol, n_holders)]`` over every ACTIVE ``option_basket`` row of every owner,
    most-held first, then by symbol. ``n_holders`` counts members (a system-owned row
    keeps the symbol in the universe without counting as a holder)."""
    B = OptionBasket
    rows = (db.query(B.symbol, func.count(func.distinct(B.user_id)))
              .filter(B.active.is_(True))
              .group_by(B.symbol)
              .all())
    return sorted(((s, int(n or 0)) for s, n in rows if s), key=lambda t: (-t[1], t[0]))


def reset_state() -> None:
    """Clear the in-process state - the ``chain_view`` cache (tests, a restart)."""
    with _chain_lock:
        _chain_cache.clear()


# ────────────────────────────────── collector status ──────────────────────────────────

_STATUS_ID = 1


def collector_status(db) -> dict | None:
    """The collector's heartbeat row (``id = 1``) as a dict, or None before its first
    write."""
    row = db.get(OptCollectorStatus, _STATUS_ID)
    return _row_dict(row, OptCollectorStatus) if row is not None else None


def set_collector_status(db, **fields) -> None:
    """Update the heartbeat row (created on first use) with any of its columns;
    unknown keys are ignored with a warning. Datetimes are stored naive UTC, strings
    cut to the column width. ``heartbeat`` is set to now (or the ``now`` keyword, a
    test clock, not a column) unless given. Commits."""
    cols = {c.name: c for c in OptCollectorStatus.__table__.columns if c.name != "id"}
    stamp = _now(fields.pop("now", None))

    def apply() -> None:
        row = db.get(OptCollectorStatus, _STATUS_ID)
        if row is None:
            row = OptCollectorStatus(id=_STATUS_ID)
            db.add(row)
        for k, v in fields.items():
            col = cols.get(k)
            if col is None:
                log.warning("set_collector_status: unknown field %r ignored", k)
                continue
            if v is not None and isinstance(col.type, DateTime):
                v = _naive_utc(v)
            elif v is not None and isinstance(col.type, String) and col.type.length:
                v = str(v)[:col.type.length]
            setattr(row, k, v)
        if "heartbeat" not in fields:
            row.heartbeat = stamp
        db.flush()

    _txn(db, apply)


# ────────────────────────────────── EOD record + retention ──────────────────────────────────

def snapshot_eod(db, symbol, on) -> int:
    """Copy ``symbol``'s ``opt_quote`` rows with ``expiry >= on`` into
    ``option_chain_snapshot`` as the day's record (``kind="eod"``, ``snap_on = on``,
    ``dte`` from the expiry vs ``on``, ``iv`` kept a FRACTION, ``source`` ``massive`` -
    ``ibkr`` for a legacy row not yet replaced), replacing that (symbol, on, eod). With
    no quote rows nothing is replaced. Commits; returns the number of rows written."""
    sym = _sym(symbol)
    day = _day(on)
    quotes = (db.query(OptQuote)
                .filter(OptQuote.symbol == sym, OptQuote.expiry >= day)
                .order_by(OptQuote.expiry, OptQuote.right, OptQuote.strike)
                .all())
    if not quotes:
        return 0

    def apply() -> int:
        S = OptionChainSnapshot
        (db.query(S).filter(S.symbol == sym, S.snap_on == day, S.kind == "eod")
           .delete(synchronize_session=False))
        db.add_all([S(symbol=sym, snap_on=day, kind="eod",
                      source=("ibkr" if q.source in LEGACY_SOURCES else (q.source or "massive")),
                      expiry=q.expiry, dte=_dte(q.expiry, day), right=q.right, strike=q.strike,
                      bid=q.bid, ask=q.ask, mid=q.mid, last=q.last,
                      bid_size=q.bid_size, ask_size=q.ask_size, iv=q.iv, delta=q.delta,
                      gamma=q.gamma, theta=q.theta, vega=q.vega, oi=q.oi, volume=q.volume)
                    for q in quotes])
        db.flush()
        return len(quotes)

    return _txn(db, apply)


def prune_v2(db, today=None, *, now=None) -> dict:
    """Retention for the v2 tables: ``opt_refresh_log`` rows older than 30 days,
    ``opt_quote`` rows whose expiry is more than 7 days past ``today`` (ET)
    (``quotes_expired``) and ``opt_quote`` rows nobody WROTE for 7 days (``updated_at``,
    else ``as_of``; ``quotes_stale`` - a listed contract is re-read at least every EOD
    pass, so these are contracts outside every read's window or no longer listed, and
    the legacy IBKR rows Massive never replaced); then the snapshot retention of ``option_store.prune`` when that
    function exists. Commits; returns the counts (``snapshots`` is that prune's dict, or
    None)."""
    day = _day(today)
    if now is not None:
        base = _now(now)
    elif today is not None:
        base = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time())
    else:
        base = _now()
    cutoff = base - _dt.timedelta(days=LOG_KEEP_DAYS)
    out: dict = {}
    out["refresh_log"] = (db.query(OptRefreshLog).filter(OptRefreshLog.as_of < cutoff)
                            .delete(synchronize_session=False))
    out["quotes_expired"] = (db.query(OptQuote)
                               .filter(OptQuote.expiry < _minus(day, EXPIRED_GRACE_DAYS))
                               .delete(synchronize_session=False))
    # "refreshed" = written (``updated_at``), not the feed's stamp: a contract that has not
    # traded for a week keeps an old ``as_of`` although every pass re-reads it
    written = func.coalesce(OptQuote.updated_at, OptQuote.as_of)
    out["quotes_stale"] = (db.query(OptQuote)
                             .filter(written < base - _dt.timedelta(days=STALE_QUOTE_DAYS))
                             .delete(synchronize_session=False))
    db.commit()
    out["snapshots"] = None
    try:
        from . import option_store      # noqa: PLC0415 - optional; Part G reduces that module
    except Exception:  # noqa: BLE001
        option_store = None
    fn = getattr(option_store, "prune", None) if option_store is not None else None
    if callable(fn):
        try:
            out["snapshots"] = fn(db, day)
        except Exception as e:  # noqa: BLE001 - the v2 prune already committed
            db.rollback()
            log.warning("prune_v2: option_store.prune failed: %s", e)
            out["snapshots_error"] = str(e)
    return out
