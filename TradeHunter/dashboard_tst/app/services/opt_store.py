"""The Options v2 data layer (OPTIONS_V2_DESIGN.md §2): ORM reads and writes for the
IBKR-only shared option data.

What lives here
---------------
* the shared quote pool (``opt_quote``): ``upsert_quotes`` (the merge rule, §2.3) and
  ``chain_view`` (what the screeners read - DTE / age pre-filters, a Core select and an
  in-process cache keyed by the symbol's newest ``opt_refresh_log`` id);
* member contributions: ``validate_contribution`` (§2.4), ``validate_history`` and
  ``check_rate``; the spot band is anchored to IBKR data members cannot move
  (``spot_reference``);
* the stock facts (``opt_underlying``, ``opt_underlying_daily``): ``set_spot``,
  ``upsert_daily``, ``recompute_underlying`` (§4.1), ``set_earnings``,
  ``mark_history_done``, ``underlying`` / ``underlyings``;
* freshness and work sharing: ``freshness`` (newest ``opt_refresh_log`` per symbol, one
  bounded query), ``universe``, ``next_for_member`` (chunked reads: at most 6 expiries,
  stalest first) with its 240 s in-process leases, ``release_lease`` (owner only) and the
  per-symbol member back-off ``report_failure``;
* the collector heartbeat (``opt_collector_status`` row 1);
* the EOD record and retention: ``snapshot_eod`` (into ``option_chain_snapshot``) and
  ``prune_v2``.

Rules this module enforces so nothing else has to
--------------------------------------------------
* Every quote carries ``as_of`` (naive UTC), ``source`` (hermes | member),
  ``source_user_id`` (member only) and ``mdt`` (live | frozen | delayed |
  delayed_frozen).
* Newer wins per contract; on an equal ``as_of`` a better market-data type wins (live >
  frozen > delayed > delayed_frozen); contracts not in a payload are untouched.
* A member's ``as_of`` is the server's receive time: a member write is stamped with
  server now (a value passed in is kept only when it lies within the last 120 s). Member
  ``delayed`` / ``delayed_frozen`` data (IBKR's 15-minute delayed feed) is stamped
  receive time - 15 min, quotes and spot alike, so it ages naturally and never replaces
  a live quote taken after the moment it shows.
* ``mid`` is always (bid + ask) / 2 - a posted mid is ignored.
* Per-contract ``iv`` is a FRACTION; per-day vol figures (``iv30``, ``hv20``, ``iv_lo``
  ...) are PERCENT.

Portable ORM only (the platform data-handling rule): query-then-update/insert upserts,
plain ``delete(synchronize_session=False)`` prunes, no SQLite-specific syntax. Every
public write commits (one unit of work per call) and retries once on a unique-key race
with another writer.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import logging
import math
import re
import threading
import time

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.exc import IntegrityError

from ..models import (OptCollectorStatus, OptionBasket, OptionChainSnapshot, OptQuote,
                      OptRefreshLog, OptUnderlying, OptUnderlyingDaily, User)
from . import clock, option_metrics

log = logging.getLogger(__name__)

MDT = ("live", "frozen", "delayed", "delayed_frozen")
SOURCES = ("hermes", "member")
KINDS = ("history", "cycle", "eod", "member", "trade")
MDT_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}   # IBKR reqMarketDataType codes
_MDT_RANK = {m: i for i, m in enumerate(MDT)}                            # lower = better on a tie

# §2.4 validation of member data
MAX_CONTRACTS = 4000
# The spot band. Its reference is IBKR data a member cannot move (``spot_reference``: the
# newest Hermes spot, else the newest Hermes daily close) and its width is ticker-relative:
# max(15%, 4 x sigma_daily x sqrt(trading days since the reference + 1)), sigma_daily =
# (iv30 or hv20 or 40) / 100 / sqrt(252). With no Hermes reference a contribution is
# accepted (a first read); a member's spot never becomes the reference.
SPOT_BAND_MIN = 0.15
SPOT_BAND_K = 4.0
SPOT_BAND_VOL_DEFAULT = 40.0  # PERCENT, when neither iv30 nor hv20 is known
# Legacy (the flat band before the review; kept for any caller still importing them).
SPOT_MAX_MOVE = 0.20
SPOT_RECENT_DAYS = 3
STRIKE_LO_X = 0.2             # strikes kept within [0.2 x spot, 5 x spot] ...
STRIKE_HI_X = 5.0
STRIKE_STEP = 0.5             # ... and on a 0.50 grid (a listed strike)
MAX_EXPIRY_DAYS = 1100        # an expiry at most this many days out, never on a weekend
PRICE_CAP_X = 1.05            # a call's bid / ask <= spot x 1.05, a put's <= strike x 1.05
INTRINSIC_TOL = 0.25          # an ask may sit at most max($0.25, 2% of spot) under intrinsic
INTRINSIC_TOL_PCT = 0.02
IV_LO = 0.01                  # per-contract iv kept strictly inside (0.01, 5.0), FRACTION
IV_HI = 5.0
MEMBER_ASOF_SLACK_S = 120     # a member as_of passed in is kept only within the last 120 s
DELAYED_MDT = ("delayed", "delayed_frozen")
DELAY_S = 900                 # IBKR's delayed feed is 15 min old: member delayed data is back-dated

# Drop reasons (one per dropped row, first failure only)
DROP_REASONS = ("bad_row", "right", "expiry", "strike", "bad_number", "crossed",
                "negative_price", "price", "iv", "delta", "negative_size")

# §2.4 rate limit (in-process)
RATE_SYMBOL_S = 20.0          # one contribution per member per symbol per 20 s
RATE_MINUTE_N = 30            # 30 contributions per member per minute

# §2.5 leases + §3.2 the fetch window spec
LEASE_S = 240.0               # longer than a worst-case chunked read (the connector's 150 s)
MIN_AGE_S = 60                # next_for_member skips a symbol refreshed in the last minute
SPEC_DEFAULTS = {"max_weekly_dte": 63, "max_dte": 1100, "sigma_k": 2.5,
                 "min_side": 6, "max_side": 40}
MEMBER_MAX_SIDE = 25          # a member read is chunked so it fits the connector's time limit:
MEMBER_MAX_EXPIRIES = 6       # at most 6 expiries, 25 strikes a side
BACKOFF_FIRST_S = 600         # a symbol whose member read failed: skipped 10 min,
BACKOFF_MAX_S = 7200          # doubling per consecutive failure up to 2 h

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
STALE_QUOTE_DAYS = 7          # a quote nobody refreshed for 7 days (unlisted / fabricated keys)

_SYM_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
_PRICE_FIELDS = ("bid", "ask", "mid", "last", "und_price")
_GREEK_FIELDS = ("delta", "gamma", "theta", "vega")
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


def _epoch(now=None) -> float:
    """Seconds since the epoch for the in-process clocks (leases, rate limit)."""
    if now is None:
        return time.time()
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return float(now)
    return _now(now).replace(tzinfo=_dt.timezone.utc).timestamp()


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


def _trading_days_counter(dates):
    """A function ``n(a, b)`` = the number of NYSE trading days ``d`` with
    ``min(a, b) < d <= max(a, b)`` (``YYYY-MM-DD`` strings) for any two of ``dates`` -
    the trading calendar between them is walked once (clock's holiday rules)."""
    ds = sorted({_dt.date.fromisoformat(d) for d in dates if d})
    ords: list[int] = []
    if ds:
        d, end = ds[0], ds[-1]
        while d <= end:
            if clock.is_trading_day(d):
                ords.append(d.toordinal())
            d += _dt.timedelta(days=1)

    def n(a: str, b: str) -> int:
        x, y = sorted((_dt.date.fromisoformat(a).toordinal(), _dt.date.fromisoformat(b).toordinal()))
        return bisect.bisect_right(ords, y) - bisect.bisect_right(ords, x)

    return n


def norm_mdt(v) -> str | None:
    """A market-data type as its name: accepts the names, the IBKR codes 1-4 (int or
    digit string) and ``delayed-frozen`` spellings. None when unknown."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return MDT_NAMES.get(v)
    s = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s.isdigit():
        return MDT_NAMES.get(int(s))
    return s if s in _MDT_RANK else None


def _wins(new_as_of, new_mdt, old_as_of, old_mdt) -> bool:
    """The merge rule (§2.3): write when nothing is stored, or the incoming ``as_of``
    is later, or it is equal and the incoming data type is at least as good."""
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

def _norm_row(r) -> dict | None:
    """A th_ibkr row as the stored shape, or None when the contract key is unusable.
    Negative prices / sizes and non-positive iv are not data (IBKR's -1 "no value"):
    they become None. ``mid`` is always (bid + ask) / 2 (None without both) - a posted
    mid is never stored, so it cannot disagree with the quote it belongs to."""
    if not isinstance(r, dict):
        return None
    expiry = _date_str(r.get("expiry"))
    right = str(r.get("right") or "").strip().upper()[:1]
    strike = _f(r.get("strike"))
    if not expiry or right not in ("C", "P") or strike is None or strike <= 0:
        return None
    out = {"expiry": expiry, "right": right, "strike": round(strike, 4)}
    for k in _PRICE_FIELDS:
        v = _f(r.get(k))
        out[k] = v if (v is not None and v >= 0) else None
    for k in ("bid_size", "ask_size", "volume", "oi"):
        v = _i(r.get(k, r.get("open_interest") if k == "oi" else None))
        out[k] = v if (v is not None and v >= 0) else None
    iv = _f(r.get("iv"))
    out["iv"] = iv if (iv is not None and iv > 0) else None
    for k in _GREEK_FIELDS:
        out[k] = _f(r.get(k))
    out["mid"] = _mid(out["bid"], out["ask"])
    return out


def _mid(bid, ask) -> float | None:
    return round((bid + ask) / 2.0, 4) if (bid is not None and ask is not None) else None


def upsert_quotes(db, symbol, rows, *, source, mdt, user_id=None, as_of=None, kind="cycle",
                  ms=None, spot=None, error=None, now=None) -> dict:
    """Merge one fetch's contract rows (th_ibkr row dicts, ``iv`` a FRACTION) into the
    shared pool under the §2.3 rule, write one ``opt_refresh_log`` row and, when
    ``spot`` is given, the underlying's spot fields. Commits.

    ``as_of`` defaults to server now; for ``source="member"`` it is always the server's
    receive time (a passed value is kept only when within the last 120 s), minus 15 min
    when the member's data is ``delayed`` / ``delayed_frozen`` (IBKR's delayed feed shows
    the market 15 min ago, so it is filed at that moment - the merge rule then never lets
    it replace a live quote taken after it, and its age reads true). Rows without a usable
    contract key are skipped (``skipped_bad``). Duplicate contracts in one payload: the
    last one wins. A member write with at least one usable row clears the symbol's
    member back-off (``report_failure``).

    Returns ``{"stored", "skipped_older", "skipped_bad", "n_expiries", "log_id"}``
    (``n_expiries`` counts the expiries that had a row written).
    """
    sym = _sym(symbol)
    if not sym:
        raise ValueError("upsert_quotes: no symbol")
    if source not in SOURCES:
        raise ValueError("upsert_quotes: source must be one of %s" % (SOURCES,))
    mdt_name = norm_mdt(mdt)
    if mdt_name is None:
        raise ValueError("upsert_quotes: unknown market data type %r" % (mdt,))
    server_now = _now(now)
    if source == "member":
        uid = _i(user_id)
        stamp = _member_stamp(as_of, mdt_name, server_now)
    else:
        uid = None
        stamp = _naive_utc(as_of) or server_now
    spot_f = _f(spot)
    spot_f = spot_f if (spot_f is not None and spot_f > 0) else None

    incoming: dict[tuple, dict] = {}
    bad = 0
    for r in rows or ():
        n = _norm_row(r)
        if n is None:
            bad += 1
            continue
        incoming[(n["expiry"], n["right"], n["strike"])] = n

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
            if q is not None and not _wins(stamp, mdt_name, q.as_of, q.mdt):
                skipped += 1
                continue
            if q is None:
                q = OptQuote(symbol=sym, expiry=key[0], right=key[1], strike=key[2])
                db.add(q)
            for f in _QUOTE_FIELDS:
                setattr(q, f, n[f])
            if q.und_price is None:
                q.und_price = spot_f
            q.as_of = stamp
            q.source = source
            q.source_user_id = uid
            q.mdt = mdt_name
            q.updated_at = server_now
            stored += 1
            written_exps.add(key[0])
        if spot_f is not None:
            _set_spot(db, sym, spot_f, source=source, mdt=mdt_name, user_id=uid, as_of=stamp,
                      now=server_now)
        entry = OptRefreshLog(symbol=sym, as_of=stamp, source=source, source_user_id=uid,
                              mdt=mdt_name, kind=str(kind or "cycle")[:10], n_contracts=stored,
                              n_expiries=len(written_exps), ms=_i(ms),
                              error=(str(error) if error else None))
        db.add(entry)
        db.flush()
        return {"stored": stored, "skipped_older": skipped, "skipped_bad": bad,
                "n_expiries": len(written_exps), "log_id": entry.id}

    out = _txn(db, apply)
    if source == "member" and incoming:
        _clear_backoff(sym)
    return out


def _member_stamp(as_of, mdt_name: str, server_now: _dt.datetime) -> _dt.datetime:
    """A member write's ``as_of``: the receive time (``as_of`` when it lies within the
    last 120 s, else server now), back-dated 15 min for delayed data."""
    stamp = _naive_utc(as_of)
    if stamp is None or not (0 <= (server_now - stamp).total_seconds() <= MEMBER_ASOF_SLACK_S):
        stamp = server_now
    if mdt_name in DELAYED_MDT:
        stamp -= _dt.timedelta(seconds=DELAY_S)
    return stamp


# ────────────────────────────────── member contributions ──────────────────────────────────

def _check_row(r, spot: float, today: str) -> tuple[dict | None, str | None]:
    """One contributed row: ``(clean_row, None)`` or ``(None, reason)`` - the FIRST
    §2.4 rule it breaks, in the design's order. Beyond §2.4 (the review of v4.133): an
    expiry on a weekend or more than 1100 days out (``expiry``); a strike off the 0.50
    grid (``strike``); a call's bid / ask above spot x 1.05, a put's above its strike x
    1.05, or an ask more than max($0.25, 2% of spot) under intrinsic (``price``); a
    delta whose sign contradicts the right (``delta``). A posted ``mid`` is ignored -
    the clean row's mid is (bid + ask) / 2."""
    if not isinstance(r, dict):
        return None, "bad_row"
    right = str(r.get("right") or "").strip().upper()
    if right in ("CALL", "PUT"):
        right = right[:1]
    if right not in ("C", "P"):
        return None, "right"
    expiry = _date_str(r.get("expiry"))
    if expiry is None or expiry < today:
        return None, "expiry"
    exp_d = _dt.date.fromisoformat(expiry)
    if exp_d.weekday() >= 5 or _dte(expiry, today) > MAX_EXPIRY_DAYS:
        return None, "expiry"
    strike = _f(r.get("strike"))
    if strike is None or strike <= 0 or strike < STRIKE_LO_X * spot or strike > STRIKE_HI_X * spot:
        return None, "strike"
    steps = strike / STRIKE_STEP
    if abs(steps - round(steps)) > 1e-6:
        return None, "strike"
    nums: dict[str, float | None] = {}
    for k in _PRICE_FIELDS + ("iv",) + _GREEK_FIELDS + ("oi", "volume", "bid_size", "ask_size"):
        raw = r.get(k)
        v = _f(raw)
        if raw is not None and v is None:
            return None, "bad_number"
        nums[k] = v
    bid, ask = nums["bid"], nums["ask"]
    if bid is not None and ask is not None and bid > ask:
        return None, "crossed"
    if any(nums[k] is not None and nums[k] < 0 for k in _PRICE_FIELDS):
        return None, "negative_price"
    cap = PRICE_CAP_X * (spot if right == "C" else strike)
    if (bid is not None and bid > cap) or (ask is not None and ask > cap):
        return None, "price"
    intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if ask is not None and ask < intrinsic - max(INTRINSIC_TOL, INTRINSIC_TOL_PCT * spot):
        return None, "price"
    if nums["iv"] is not None and not (IV_LO < nums["iv"] < IV_HI):
        return None, "iv"
    delta = nums["delta"]
    if delta is not None and (abs(delta) > 1.0 or (right == "C" and delta < 0)
                              or (right == "P" and delta > 0)):
        return None, "delta"
    if any(nums[k] is not None and nums[k] < 0 for k in ("oi", "volume")):
        return None, "negative_size"
    clean = {"expiry": expiry, "right": right, "strike": round(strike, 4)}
    for k in _QUOTE_FIELDS:
        v = nums[k]
        if k in ("bid_size", "ask_size", "volume", "oi"):
            v = None if (v is None or v < 0) else int(round(v))
        clean[k] = v
    clean["mid"] = _mid(bid, ask)
    return clean, None


def drop_reasons(rows, spot, *, today=None) -> dict[str, int]:
    """``{reason: count}`` over ``rows`` under the §2.4 row rules - the breakdown
    behind ``validate_contribution``'s ``dropped`` count (diagnostics, tests)."""
    out: dict[str, int] = {}
    s = _f(spot) or 0.0
    day = _day(today)
    for r in rows or ():
        _clean, why = _check_row(r, s, day)
        if why:
            out[why] = out.get(why, 0) + 1
    return out


def validate_contribution(db, payload, *, user_id, today=None, now=None):
    """Check a member's chain contribution (§2.4). Never raises.

    Returns ``(clean, None, dropped)`` with ``clean = {"symbol", "spot", "mdt",
    "rows"}`` (rows normalised to the stored shape, ``mdt`` as its name), or
    ``(None, error, dropped)`` when the whole payload is rejected: symbol missing or
    in no member's active basket; rows not a list or more than 4000; spot missing or
    <= 0, or outside the spot band around IBKR's reference (``spot_reference`` /
    ``spot_band``; no reference = accepted); unknown market data type; no usable
    contract left after the row rules. ``dropped`` counts the single rows removed
    (``drop_reasons`` gives the breakdown)."""
    try:
        return _validate(db, payload, today=today, now=now)
    except Exception as e:  # noqa: BLE001 - a contribution must never 500 the server
        log.warning("validate_contribution (user %s): %s", user_id, e, exc_info=True)
        return None, "the contribution could not be read", 0


def _held(db, sym: str) -> bool:
    return (db.query(OptionBasket.id)
              .filter(OptionBasket.symbol == sym, OptionBasket.active.is_(True))
              .first()) is not None


def spot_reference(db, symbol) -> dict | None:
    """IBKR's reference price for ``symbol`` - data a member cannot move:
    ``{"spot", "on" (its ET date), "from" ("spot" | "daily"), "sigma_daily"}`` or None.

    The newest (by ET date; on the same date in this order) of: the stored spot when
    Hermes wrote it (``opt_underlying.spot`` with ``spot_source`` hermes); the spot
    Hermes stamped on its newest surviving quote (``opt_quote.und_price`` of a
    ``source`` hermes row - the fallback once a member's newer spot has replaced Hermes's
    in ``opt_underlying`` on a ticker with no Hermes daily history yet); the newest
    Hermes-sourced daily close (``opt_underlying_daily``). A member's spot is never the
    reference (it would let one member walk the band). ``sigma_daily`` = (iv30 or hv20
    or 40) / 100 / sqrt(252) from the ``opt_underlying`` row, the band's ticker-relative
    scale."""
    sym = _sym(symbol)
    und = _und_row(db, sym, create=False)
    cands: list[dict] = []
    if (und is not None and und.spot_source == "hermes" and und.spot_as_of is not None
            and _f(und.spot) and float(und.spot) > 0):
        cands.append({"spot": float(und.spot), "on": clock.et_today(_naive_utc(und.spot_as_of)),
                      "from": "spot"})
    Q = OptQuote
    q = (db.query(Q.und_price, Q.as_of)
           .filter(Q.symbol == sym, Q.source == "hermes", Q.und_price.isnot(None), Q.und_price > 0)
           .order_by(Q.as_of.desc())
           .first())
    if q is not None and q.as_of is not None:
        cands.append({"spot": float(q.und_price), "on": clock.et_today(_naive_utc(q.as_of)),
                      "from": "quote"})
    D = OptUnderlyingDaily
    d = (db.query(D.on, D.close)
           .filter(D.symbol == sym, D.source == "hermes", D.close.isnot(None), D.close > 0)
           .order_by(D.on.desc())
           .first())
    if d is not None:
        cands.append({"spot": float(d.close), "on": d.on, "from": "daily"})
    best = None
    for c in cands:                        # newest date wins; a tie keeps the earlier source
        if best is None or c["on"] > best["on"]:
            best = c
    if best is None:
        return None
    vol = None
    if und is not None:
        for v in (und.iv30, und.hv20):
            v = _f(v)
            if v is not None and v > 0:
                vol = v
                break
    best["sigma_daily"] = (vol or SPOT_BAND_VOL_DEFAULT) / 100.0 / math.sqrt(252.0)
    return best


def spot_band(ref: dict, trading_days: int) -> float:
    """The band's half-width as a fraction of the reference: max(15%, 4 x sigma_daily x
    sqrt(trading days between the reference and the price + 1))."""
    return max(SPOT_BAND_MIN,
               SPOT_BAND_K * float(ref["sigma_daily"]) * math.sqrt(max(0, int(trading_days)) + 1))


def _validate(db, payload, *, today=None, now=None):
    if not isinstance(payload, dict):
        return None, "the contribution must be a JSON object", 0
    sym = _sym(payload.get("symbol"))
    if not sym or not _SYM_RE.match(sym):
        return None, "symbol missing or not valid", 0
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None, "rows must be a list", 0
    if len(rows) > MAX_CONTRACTS:
        return None, "too many contracts (%d; at most %d)" % (len(rows), MAX_CONTRACTS), 0
    if not _held(db, sym):
        return None, "%s is not in any member's basket" % sym, 0
    spot = _f(payload.get("spot"))
    if spot is None or spot <= 0:
        return None, "spot missing or not positive", 0
    day = _today(today, now)
    ref = spot_reference(db, sym)
    if ref is not None:
        width = spot_band(ref, _trading_days_counter([ref["on"], day])(ref["on"], day))
        move = abs(spot / ref["spot"] - 1.0)
        if move > width:
            return (None, "spot %.2f is %.0f%% from IBKR's price %.2f (%s); at most %.0f%% is accepted"
                    % (spot, move * 100, ref["spot"], ref["on"], width * 100), 0)
    mdt = norm_mdt(payload.get("mdt"))
    if mdt is None:
        return None, "market data type missing or unknown", 0
    clean_rows: list[dict] = []
    dropped = 0
    for r in rows:
        clean, why = _check_row(r, spot, day)
        if why:
            dropped += 1
        else:
            clean_rows.append(clean)
    if not clean_rows:
        return None, "no usable contracts (%d dropped)" % dropped, dropped
    return {"symbol": sym, "spot": spot, "mdt": mdt, "rows": clean_rows}, None, dropped


def validate_history(db, payload, *, user_id, today=None, now=None):
    """Check a member's daily history post (``{"symbol", "bars", "iv_series"}``, §6
    step 4). Never raises. Returns ``(clean, None)`` with ``clean = {"symbol", "bars",
    "iv_series"}`` ready for ``upsert_daily(db, clean["symbol"], clean["bars"],
    clean["iv_series"], source="member", ...)``, or ``(None, error)``.

    The whole post is rejected when: not an object; symbol missing / not valid / in no
    member's active basket; ``bars`` missing, empty or not a list; ``iv_series`` not a
    list; more than 800 points in either; a point whose date is unreadable, on a weekend
    or after today (ET); a bar whose close is not positive, a high / low / volume that is
    not a number, high < low or a negative volume; any close outside the spot band around
    IBKR's reference (``spot_reference``; the band widens with the trading days between
    the bar and the reference; no reference = accepted). A single IV point outside
    0.1-1000 (PERCENT) is left out."""
    try:
        return _validate_history(db, payload, today=today, now=now)
    except Exception as e:  # noqa: BLE001 - a contribution must never 500 the server
        log.warning("validate_history (user %s): %s", user_id, e, exc_info=True)
        return None, "the history could not be read"


def _point_day(raw, day: str) -> tuple[str | None, str | None]:
    """``(YYYY-MM-DD, None)`` or ``(None, why)`` for one history point's date."""
    on = _date_str(raw) if raw else None
    if on is None:
        return None, "unreadable date %r" % (str(raw)[:20],)
    if _dt.date.fromisoformat(on).weekday() >= 5:
        return None, "%s is a weekend" % on
    if on > day:
        return None, "%s is after today" % on
    return on, None


def _validate_history(db, payload, *, today=None, now=None):
    if not isinstance(payload, dict):
        return None, "the history must be a JSON object"
    sym = _sym(payload.get("symbol"))
    if not sym or not _SYM_RE.match(sym):
        return None, "symbol missing or not valid"
    bars, ivs = payload.get("bars"), payload.get("iv_series")
    ivs = [] if ivs is None else ivs
    if not isinstance(bars, list) or not bars:
        return None, "bars missing or not a list"
    if not isinstance(ivs, list):
        return None, "iv_series must be a list"
    if len(bars) > DAILY_MAX_POINTS or len(ivs) > DAILY_MAX_POINTS:
        return None, "at most %d points each" % DAILY_MAX_POINTS
    if not _held(db, sym):
        return None, "%s is not in any member's basket" % sym
    day = _today(today, now)
    clean_bars: list[dict] = []
    for i, b in enumerate(bars):
        if not isinstance(b, dict):
            return None, "bar %d is not an object" % i
        on, why = _point_day(b.get("on") or b.get("date") or b.get("time"), day)
        if why:
            return None, "bar %d: %s" % (i, why)
        close = _f(b.get("close"))
        if close is None or close <= 0:
            return None, "bar %s: close missing or not positive" % on
        vals = {}
        for k in ("open", "high", "low", "volume"):
            raw = b.get(k)
            v = _f(raw)
            if raw is not None and v is None:
                return None, "bar %s: %s is not a number" % (on, k)
            vals[k] = v
        if vals["high"] is not None and vals["low"] is not None and vals["high"] < vals["low"]:
            return None, "bar %s: high below low" % on
        if vals["volume"] is not None and vals["volume"] < 0:
            return None, "bar %s: negative volume" % on
        clean_bars.append({"on": on, "open": vals["open"], "high": vals["high"], "low": vals["low"],
                           "close": close, "volume": vals["volume"]})
    clean_ivs: list[dict] = []
    for i, p in enumerate(ivs):
        if isinstance(p, dict):
            raw_on, v = p.get("on") or p.get("date"), _f(p.get("iv", p.get("iv30")))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            raw_on, v = p[0], _f(p[1])
        else:
            return None, "iv point %d is not readable" % i
        on, why = _point_day(raw_on, day)
        if why:
            return None, "iv point %d: %s" % (i, why)
        if v is not None and DAILY_IV_LO <= v <= DAILY_IV_HI:
            clean_ivs.append({"on": on, "iv": v})
    ref = spot_reference(db, sym)
    if ref is not None:
        n = _trading_days_counter([ref["on"], day] + [b["on"] for b in clean_bars])
        for b in clean_bars:
            width = spot_band(ref, n(ref["on"], b["on"]))
            move = abs(b["close"] / ref["spot"] - 1.0)
            if move > width:
                return (None, "the close %.2f on %s is %.0f%% from IBKR's price %.2f (%s); at most "
                              "%.0f%% is accepted" % (b["close"], b["on"], move * 100, ref["spot"],
                                                      ref["on"], width * 100))
    return {"symbol": sym, "bars": clean_bars, "iv_series": clean_ivs}, None


_rate_lock = threading.Lock()
_rate_last: dict[tuple, float] = {}          # (user_id, symbol, bucket) -> last accepted time
_rate_hits: dict[int, list[float]] = {}      # user_id -> accepted times in the last minute


def check_rate(user_id, symbol, *, now=None, bucket: str = "chain") -> str | None:
    """The §2.4 rate limit: one contribution per member per symbol per 20 s and 30 per
    member per minute. Returns the reason when over the limit, else None and records
    the hit. ``bucket`` keeps a symbol's chain and history contributions apart (the
    page posts both for one symbol back to back); the per-minute cap is shared."""
    t = _epoch(now)
    uid = int(user_id) if user_id is not None else 0
    sym = _sym(symbol)
    key = (uid, sym, str(bucket))
    with _rate_lock:
        last = _rate_last.get(key)
        if last is not None and t - last < RATE_SYMBOL_S:
            return ("%s: one contribution per %d s - try again in %d s"
                    % (sym, int(RATE_SYMBOL_S), max(1, int(math.ceil(RATE_SYMBOL_S - (t - last))))))
        hits = [h for h in _rate_hits.get(uid, ()) if t - h < 60.0]
        if len(hits) >= RATE_MINUTE_N:
            _rate_hits[uid] = hits
            return "at most %d contributions a minute - try again shortly" % RATE_MINUTE_N
        hits.append(t)
        _rate_hits[uid] = hits
        _rate_last[key] = t
        if len(_rate_last) > 5000:              # keep the dict bounded on a long-running server
            for k in [k for k, v in _rate_last.items() if t - v >= RATE_SYMBOL_S]:
                _rate_last.pop(k, None)
    return None


# ────────────────────────────────── chain + underlying reads ──────────────────────────────────

_Q = OptQuote
_CHAIN_COLS = (_Q.expiry, _Q.right, _Q.strike, _Q.bid, _Q.ask, _Q.last, _Q.bid_size,
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
        mid = _mid(r.bid, r.ask)            # never a stored mid (a row filed before the rule)
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
    / ``source`` / ``source_name`` (the contributor's display name) / ``mdt``. The spot
    comes from ``opt_underlying``; when that has none, from the newest loaded quote's
    ``und_price``.

    Pre-filters (each optional): only expiries with DTE in ``[dte_min, dte_max]``; only
    rows with ``as_of >= now - max_age_h`` hours. ``max_age_h`` is WALL-clock and coarse
    (the cutoff is floored to the hour, so a little more is loaded, never less): the
    screener passes its own limit + 96 h so a weekend never empties the chain, and
    applies the exact market-time age itself.

    The rows are read with a Core select of the needed columns (users joined for the
    name) and cached in-process under (database, symbol, ET day, DTE window, age bucket,
    the symbol's newest ``opt_refresh_log`` id) - every write through ``upsert_quotes``
    or ``report_failure`` logs a row, so a write changes the key. Entries also expire
    after 5 min (a prune in another process, or a write by a transaction that committed
    behind a newer id on a multi-writer database). Callers get their own copies."""
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


def _set_spot(db, sym, spot, *, source, mdt, user_id, as_of, now) -> bool:
    """Write the spot fields when the reading is not older than the stored one
    (the same rule as a quote). Flushes, does not commit."""
    u = _und_row(db, sym, create=True, now=now)
    if u.spot is not None and not _wins(as_of, mdt, u.spot_as_of, u.spot_mdt):
        return False
    u.spot = float(spot)
    u.spot_as_of = as_of
    u.spot_source = source
    u.spot_user_id = user_id if source == "member" else None
    u.spot_mdt = mdt
    u.updated_at = now
    db.flush()
    return True


def set_spot(db, symbol, spot, *, source, mdt, user_id=None, as_of=None, now=None) -> None:
    """Record a stock price for ``symbol`` (newer wins, as for quotes; a member's
    ``as_of`` is the receive time, back-dated 15 min for delayed data, exactly as
    ``upsert_quotes``). Creates the ``opt_underlying`` row when missing. Commits."""
    sym = _sym(symbol)
    s = _f(spot)
    if not sym or s is None or s <= 0:
        raise ValueError("set_spot: need a symbol and a positive spot")
    if source not in SOURCES:
        raise ValueError("set_spot: source must be one of %s" % (SOURCES,))
    mdt_name = norm_mdt(mdt)
    if mdt_name is None:
        raise ValueError("set_spot: unknown market data type %r" % (mdt,))
    server_now = _now(now)
    if source == "member":
        stamp = _member_stamp(as_of, mdt_name, server_now)
    else:
        stamp = _naive_utc(as_of) or server_now
    uid = _i(user_id) if source == "member" else None
    _txn(db, lambda: _set_spot(db, sym, s, source=source, mdt=mdt_name, user_id=uid,
                               as_of=stamp, now=server_now))


# ────────────────────────────────── daily history + stats ──────────────────────────────────

def upsert_daily(db, symbol, bars=None, iv_series=None, *, source, user_id=None,
                 today=None, now=None) -> int:
    """File IBKR daily history for ``symbol`` into ``opt_underlying_daily``.

    ``bars`` = ``[{"on", "open", "high", "low", "close", "volume"}]`` (TRADES, oldest
    first); ``iv_series`` = ``[{"on", "iv"}]`` with ``iv`` in PERCENT. A day already on
    file is updated field by field (bars set close / high / low / volume, the series
    sets iv30), so a later value for a day replaces the earlier one and neither input
    erases the other's columns. Points are skipped when the date is unreadable or after
    ``today`` (ET), the close is not positive, high < low, or iv is outside 0.1-1000.
    At most the newest 800 points of each input are read. ``user_id`` is accepted for
    the caller's symmetry; the table records the source only.

    ``source="hermes"`` REPLACES member history inside the span it covers: on a
    member-sourced row dated within the bars' first..last day that Hermes sent no bar
    for, the bar columns are cleared; within the IV series' span with no Hermes IV
    point, ``iv30`` is cleared; a member row left with neither a close nor an iv30 is
    deleted. So a member's invented day (a holiday, a fake bar) cannot outlive the first
    Hermes pull that covers it. Commits; returns the number of day rows written."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("upsert_daily: no symbol")
    if source not in SOURCES:
        raise ValueError("upsert_daily: source must be one of %s" % (SOURCES,))
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

    def replace_member_rows() -> None:
        D = OptUnderlyingDaily
        b_span = (min(bar_by_day), max(bar_by_day)) if bar_by_day else None
        i_span = (min(iv_by_day), max(iv_by_day)) if iv_by_day else None
        lo = min(s[0] for s in (b_span, i_span) if s)
        hi = max(s[1] for s in (b_span, i_span) if s)
        for r in (db.query(D)
                    .filter(D.symbol == sym, D.source == "member", D.on >= lo, D.on <= hi)
                    .all()):
            if b_span and b_span[0] <= r.on <= b_span[1] and r.on not in bar_by_day:
                r.close = r.high = r.low = r.volume = None
            if i_span and i_span[0] <= r.on <= i_span[1] and r.on not in iv_by_day:
                r.iv30 = None
            if r.on not in bar_by_day and r.on not in iv_by_day and r.close is None and r.iv30 is None:
                db.delete(r)
        db.flush()

    def apply() -> int:
        if source == "hermes":
            replace_member_rows()
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
    IBKR value) and ``iv_rank`` / ``iv_pct`` / ``iv_n`` / ``iv_lo`` / ``iv_hi`` from
    ``option_metrics.iv_rank_pct`` over the last 252 iv30 values. A figure without
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
    """The next earnings date (free source - the one non-IBKR figure, §2.1) or None
    when there is none. Accepts a date, ``YYYY-MM-DD`` or ``{"date": ...}``. Commits."""
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
    """Record that the one-time 1-year IV + 2-year bars pull for ``symbol`` happened.
    Commits."""
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


# ────────────────────────────────── freshness, universe, leases ──────────────────────────────────

def freshness(db, symbols, *, now=None) -> dict[str, dict]:
    """``{symbol: {"as_of", "source", "source_user_id", "source_name", "mdt", "n",
    "kind", "age_min"}}`` from the newest ``opt_refresh_log`` row per symbol that wrote
    at least one contract and was not a ``trade`` refresh (a few legs re-read for one
    trade do not make the whole chain fresh). Ties on ``as_of`` go to the later row. A
    symbol with no such row is absent.

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


_lease_lock = threading.Lock()
_leases: dict[str, tuple[float, int | None]] = {}     # symbol -> (expires epoch s, user_id)
_backoff_lock = threading.Lock()
_backoff: dict[str, tuple[float, int]] = {}           # symbol -> (until epoch s, consecutive failures)


def is_leased(symbol, *, now=None) -> bool:
    """True while a member's connector holds the 240 s lease on ``symbol``."""
    t = _epoch(now)
    with _lease_lock:
        held = _leases.get(_sym(symbol))
        return held is not None and held[0] > t


def release_lease(symbol, user_id=None) -> None:
    """Drop the lease on ``symbol`` (its contribution arrived, or the fetch failed) -
    only when ``user_id`` is None or is the member holding it, so one member's post
    (a trade refresh, a refused contribution) never frees another member's read."""
    sym = _sym(symbol)
    uid = _i(user_id)
    with _lease_lock:
        held = _leases.get(sym)
        if held is not None and (uid is None or held[1] == uid):
            _leases.pop(sym, None)


def backoff_until(symbol, *, now=None) -> float | None:
    """Epoch seconds until which member reads of ``symbol`` are backed off, or None."""
    t = _epoch(now)
    with _backoff_lock:
        held = _backoff.get(_sym(symbol))
    return held[0] if (held is not None and held[0] > t) else None


def _clear_backoff(sym: str) -> None:
    with _backoff_lock:
        _backoff.pop(sym, None)


def report_failure(symbol, user_id, error, *, now=None, db=None) -> int:
    """A member's connector could not read ``symbol`` (an error, a timeout, an empty
    read): back the symbol off for every member's background loop - 10 min, doubling
    per consecutive failure up to 2 h, cleared by the next member write with usable
    rows (``upsert_quotes``) - release THIS member's lease, and log an
    ``opt_refresh_log`` row (kind ``member``, ``n_contracts`` 0, the error) that
    ``freshness`` ignores. A failure reported while the symbol is already backed off
    does not double it again (two members failing together count once). Returns the
    back-off seconds left.

    ``db``: the caller's session for the log row; without one a short-lived session of
    the app's own database is used. The log row is best effort - the back-off and the
    lease release never depend on it."""
    sym = _sym(symbol)
    if not sym:
        return 0
    t = _epoch(now)
    with _backoff_lock:
        until, n = _backoff.get(sym, (0.0, 0))
        if until > t:
            secs = int(math.ceil(until - t))
        else:
            n = min(n + 1, 16)
            secs = int(min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2 ** (n - 1)))
            _backoff[sym] = (t + secs, n)
    release_lease(sym, user_id)
    _log_failure(db, sym, _i(user_id), error, _now(now))
    return secs


def _log_failure(db, sym: str, uid, error, stamp: _dt.datetime) -> None:
    def write(s) -> None:
        s.add(OptRefreshLog(symbol=sym, as_of=stamp, source="member", source_user_id=uid,
                            mdt=None, kind="member", n_contracts=0, n_expiries=0,
                            error=(str(error or "") or "member read failed")[:2000]))
        s.commit()

    own = None
    try:
        if db is None:
            from ..db import SessionLocal  # noqa: PLC0415 - only when the caller has no session
            own = db = SessionLocal()
        write(db)
    except Exception as e:  # noqa: BLE001 - the back-off already stands
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning("report_failure %s: the log row could not be written: %s", sym, e)
    finally:
        if own is not None:
            own.close()


def _stalest_expiries(db, sym: str, day: str, n: int) -> list[str]:
    """Up to ``n`` of the expiries ``sym`` has in ``opt_quote`` (``expiry >= day``),
    the one whose oldest row is oldest first (ties by expiry)."""
    Q = OptQuote
    oldest = func.min(Q.as_of)
    rows = db.execute(select(Q.expiry, oldest)
                      .where(Q.symbol == sym, Q.expiry >= day)
                      .group_by(Q.expiry)
                      .order_by(oldest, Q.expiry)
                      .limit(int(n))).all()
    return [r[0] for r in rows]


def next_for_member(db, user, *, now=None, min_age_s: float = MIN_AGE_S) -> dict | None:
    """The next symbol this member's connector should read: the member's ACTIVE basket
    ordered stalest first (never-read symbols first, in basket order, then the oldest
    freshness ``as_of``), skipping symbols another fetch holds a lease on, symbols in a
    member back-off (``report_failure``) and symbols refreshed within ``min_age_s`` (by
    receive time - a delayed read's back-dated 15 min do not count). Leases the chosen
    symbol for 240 s.

    The read is CHUNKED so it fits the connector's time limit: ``spec`` is the §3.2
    fetch window from the stored spot and iv30 (``iv_hint`` = iv30 / 100, a FRACTION;
    both None when unknown) with ``max_side`` 25 and EITHER ``expiries`` = up to 6
    stored expiries (``expiry >= today``), the stalest first (by their oldest row's
    ``as_of``), OR - no quotes stored yet - ``expiries`` None and ``max_expiries`` 6
    (``th_ibkr.plan`` then takes the nearest 6 eligible expiries).

    Returns ``{"symbol", "spec", "history_done"}`` or None when there is nothing to
    read now."""
    uid = getattr(user, "id", None)
    if uid is None:
        return None
    basket = (db.query(OptionBasket.symbol)
                .filter(OptionBasket.owner_key == "u%d" % int(uid),
                        OptionBasket.active.is_(True))
                .order_by(OptionBasket.pos, OptionBasket.id)
                .all())
    syms: list[str] = []
    for (s,) in basket:
        s = _sym(s)
        if s and s not in syms:
            syms.append(s)
    if not syms:
        return None
    fr = freshness(db, syms, now=now)
    t_now = _now(now)
    t = _epoch(now)
    order = sorted(range(len(syms)),
                   key=lambda i: (0, i) if syms[i] not in fr else (1, fr[syms[i]]["as_of"], i))
    with _backoff_lock:
        backed = {s for s, v in _backoff.items() if v[0] > t}
    chosen = None
    with _lease_lock:
        for k in [k for k, v in _leases.items() if v[0] <= t]:
            _leases.pop(k, None)
        for i in order:
            sym = syms[i]
            if sym in _leases or sym in backed:
                continue
            f = fr.get(sym)
            if f is not None and min_age_s:
                received = f["as_of"]
                if f.get("source") == "member" and f.get("mdt") in DELAYED_MDT:
                    received += _dt.timedelta(seconds=DELAY_S)
                if (t_now - received).total_seconds() < min_age_s:
                    continue
            _leases[sym] = (t + LEASE_S, int(uid))
            chosen = sym
            break
    if chosen is None:
        return None
    und = _und_row(db, chosen, create=False)
    spot = und.spot if und is not None else None
    iv30 = und.iv30 if und is not None else None
    expiries = _stalest_expiries(db, chosen, _today(None, now), MEMBER_MAX_EXPIRIES)
    spec = {"symbol": chosen, "spot": spot,
            "iv_hint": (round(float(iv30) / 100.0, 4) if iv30 else None),
            **SPEC_DEFAULTS, "max_side": MEMBER_MAX_SIDE}
    if expiries:
        spec["expiries"] = expiries
    else:
        spec["expiries"] = None
        spec["max_expiries"] = MEMBER_MAX_EXPIRIES
    return {"symbol": chosen, "spec": spec,
            "history_done": bool(und is not None and und.history_done)}


def reset_state() -> None:
    """Clear the in-process leases, back-offs, rate-limit counters and the chain_view
    cache (tests, a restart)."""
    with _lease_lock:
        _leases.clear()
    with _backoff_lock:
        _backoff.clear()
    with _rate_lock:
        _rate_last.clear()
        _rate_hits.clear()
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
    ``option_chain_snapshot`` as the day's record (``kind="eod"``, ``source="ibkr"``,
    ``snap_on = on``, ``dte`` from the expiry vs ``on``, ``iv`` kept a FRACTION),
    replacing that (symbol, on, eod). With no quote rows nothing is replaced. Commits;
    returns the number of rows written."""
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
        db.add_all([S(symbol=sym, snap_on=day, kind="eod", source="ibkr",
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
    (``quotes_expired``) and ``opt_quote`` rows nobody refreshed for 7 days
    (``quotes_stale`` - a listed contract is re-read at least every EOD pass, so these
    are contracts no feed lists any more: unlisted or invented keys); then the snapshot
    retention of ``option_store.prune`` when that function exists. Commits; returns the
    counts (``snapshots`` is that prune's dict, or None)."""
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
    out["quotes_stale"] = (db.query(OptQuote)
                             .filter(OptQuote.as_of < base - _dt.timedelta(days=STALE_QUOTE_DAYS))
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
