"""The Options Screener's data layer (OPTIONS_SCREENER_DESIGN.md §3-§4): the ONLY reader
and writer of the ``scr_*`` tables for the Hermes screener collector
(``services/scr_collector.py``). The web app reads the same tables through
``services/screener/frame.py`` (read only).

What lives here
---------------
* contracts: ``replace_contracts`` (one symbol per transaction - delete, then an ORM bulk
  insert, carrying ``vol_prev`` / ``oi_prev`` across a new session), ``newest_session``;
* the underlying: ``update_underlying_pass`` (spot, IV30, the option-wide figures of a
  pass, the IV figures), ``recompute_technicals`` (from the daily bars),
  ``recompute_iv`` (IV30 / IV rank / IV percentile over the last 252 daily IV30s),
  ``set_identity`` (name / security type / exchange), ``set_earnings``;
* daily history: ``upsert_daily`` (one symbol, field by field), ``file_grouped_day`` (one
  session of grouped daily bars for every universe symbol), ``file_iv_day`` (one session's
  IV30 for every stock, from Massive's options day file - v4.138), ``stock_day_status``,
  ``iv_day_status``, ``daily_counts``, ``raw_bars``, ``last_close``; for the flat-file
  lane (v4.138): ``day_closes_raw`` (one day's unadjusted closes), ``iv_counts`` /
  ``raw_close_counts`` (points per symbol), ``sec_types``;
* where a day's IV30 came from (``iv30_src``, v4.138): ``live`` (the EOD pass,
  ``update_underlying_pass(file_iv=True)``), ``rest`` (the per-stock REST history through
  ``upsert_daily``), ``flat`` (``file_iv_day``). A live reading always wins: neither
  history writer replaces one. NULL = filed before v4.138 (unknown, kept);
* the universe: ``upsert_universe`` (a streamed, partial save with ``deactivate=False``
  never deactivates), ``pass_symbols`` / ``pass_symbols_weighted`` (the same order, with
  each symbol's contract count - the pass percent weights), ``universe_info``;
* the IV figures of many symbols at once: ``recompute_iv_many`` (one commit per chunk);
* passes: ``start_pass`` / ``finish_pass`` / ``last_pass`` (``min_contracts`` skips a pass
  that stored nothing; ``partial`` tells a pass read on a partial list of optionable stocks
  from one read on a complete list);
* the heartbeat row: ``status`` / ``set_status`` (the JSON ``progress`` block is made
  JSON-safe on the way in);
* IV-history bookkeeping: ``history_queue``, ``mark_history`` (30 min back-off doubling to
  24 h), ``mark_history_many`` (many at once, v4.138), ``history_not_done``,
  ``history_counts`` - an index underlying (``sec_type`` index, or an ``I:``
  prefix) has no IV history to build (no index closes on Stocks Basic) and is left out
  of both;
* retention: ``prune`` (universe symbols inactive more than 10 days and every row of
  theirs, expired contracts, daily rows past ~3 years, pass rows past 30 days).

Rules (CLAUDE.md data-handling rule): SQLAlchemy ORM only - no raw SQL, no SQLite-only
syntax; upserts are query-then-update/insert; bulk inserts are ORM ``insert(Model)`` in
chunks; every public write commits (one unit of work) and retries once on a unique-key
race with another writer. Inside ONE process the four writers that can insert the same
``scr_underlying_daily`` (symbol, day) row - ``file_grouped_day``, ``upsert_daily``,
``file_iv_day`` and the EOD branch of ``update_underlying_pass`` - also serialize on
``_DAILY_LOCK`` (taken before
the transaction, never inside it), so the collector's pass workers and its stocks lane
never race each other into an IntegrityError; the retry stays as the backstop for another
process. All datetimes naive UTC; a contract's ``iv`` is a FRACTION, every per-underlying
IV / HV figure PERCENT.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import threading

from sqlalchemy import JSON, DateTime, String, func, insert, or_
from sqlalchemy.exc import IntegrityError

from ..screener_models import (ScrContract, ScrEarnings, ScrEarningsDay, ScrPass, ScrStatus, ScrUnderlying,
                               ScrUnderlyingDaily, ScrUniverse)
from . import clock, option_metrics

log = logging.getLogger(__name__)

CHUNK = 1000                     # rows per bulk insert / IN (...) list
IV_WINDOW = 252                  # the IV-rank window: the last 252 daily IV30 readings
HISTORY_MIN_POINTS = 20          # history_done needs this many IV30 points
HISTORY_RETRY_S = 1800.0         # a history that failed / came back short: 30 min ...
HISTORY_RETRY_MAX_S = 24 * 3600.0  # ... doubling to 24 h
INACTIVE_KEEP_DAYS = 10          # a symbol gone from Massive's list stays in passes 10 days, then is dropped
DAILY_KEEP_DAYS = 1100           # daily rows older than ~3 years are pruned (2 years are used)
PASS_KEEP_DAYS = 30              # scr_pass rows
STATUS_ID = 1
DAILY_IV_LO, DAILY_IV_HI = 0.1, 1000.0   # PERCENT bounds of a daily IV30 point
CONTRACT_IV_MAX = 5.0            # a contract IV (FRACTION) above this is junk
ATR_N, RSI_N = 14, 14
TREND_MIN_BARS = 210             # MATP classify_trend: EMA200 needs this many closes
TREND_SLOPE_WINDOW = 20          # MATP classify_trend: EMA50 slope over 20 bars
SEC_TYPES = ("stock", "etf", "index", "other")
WHEN_WORDS = {"pre": "bmo", "bmo": "bmo", "before": "bmo", "post": "amc", "amc": "amc", "after": "amc"}
IV_SRC_LIVE, IV_SRC_REST, IV_SRC_FLAT = "live", "rest", "flat"   # scr_underlying_daily.iv30_src
IV_SRC_REPLACEABLE = (IV_SRC_FLAT, IV_SRC_REST)   # history readings a history writer may redo
EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "INDEX", "OTHER")

C, U, D, UNI, P = ScrContract, ScrUnderlying, ScrUnderlyingDaily, ScrUniverse, ScrPass

# One process's writers of scr_underlying_daily (see the module docstring). Re-entrant so
# a writer that calls another one never deadlocks itself.
_DAILY_LOCK = threading.RLock()


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


def _pos(v) -> float | None:
    f = _f(v)
    return f if (f is not None and f > 0) else None


def _count(v) -> int | None:
    f = _f(v)
    return int(round(f)) if (f is not None and f >= 0) else None


def _naive_utc(ts) -> _dt.datetime | None:
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
    return _naive_utc(now) or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _day_str(v) -> str | None:
    """``YYYY-MM-DD`` from a date, a datetime or a string; else None."""
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


def _src(v, default: str) -> str:
    """An ``iv30_src`` tag: lower-case, at most 8 characters; empty -> ``default``."""
    return (str(v or "").strip().lower() or default)[:8]


def _chunks(items, n: int = CHUNK):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _txn(db, fn):
    """Run ``fn`` then commit; on a unique-key race with another writer roll back and run
    it once more (the second run re-reads what the other writer stored). Any other error
    rolls back and propagates."""
    for attempt in (1, 2):
        try:
            out = fn()
            db.commit()
            return out
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
            log.info("scr_store: unique-key race, retrying once")
        except Exception:
            db.rollback()
            raise
    return None   # pragma: no cover


def _row_dict(obj, model) -> dict:
    return {c.name: getattr(obj, c.name) for c in model.__table__.columns}


def close_time_utc(on) -> _dt.datetime | None:
    """16:00 ET on session ``on`` as naive UTC (when a daily close is known)."""
    d = _day_str(on)
    if d is None:
        return None
    day = _dt.date.fromisoformat(d)
    off = clock.et_now(_dt.datetime(day.year, day.month, day.day, 20, 0)).utcoffset() or _dt.timedelta(hours=-5)
    return _dt.datetime.combine(day, clock.SESSION_CLOSE) - off


def spot_session(as_of) -> str | None:
    """v4.220: the ET session a stock price read at ``as_of`` (naive UTC) belongs to - that day when it was read on
    a trading day at or after the 09:30 open, else the trading day before (a weekend or pre-open read)."""
    if not isinstance(as_of, _dt.datetime):
        return None
    et = clock.et_now(as_of)
    d = et.date()
    if not clock.is_trading_day(d) or et.time() < clock.SESSION_OPEN:
        d = clock.prev_trading_day(d)
    return d.isoformat()


def settle_spots(db, *, symbols=None) -> int:
    """v4.220 (user: *"use the official close once the session has closed, keep the option-implied price only
    during market hours, and show where the price came from"*). A pass prices the stock from the options
    (put-call parity, ``spot_src = "parity"``) or a stock snapshot (``"massive"``); the end-of-day pass runs right
    after the 16:00 ET close, but Stocks Basic publishes the day's official closes only at 20:00 ET, so that
    session's price stayed the option-implied estimate (AMD 515.95 against a 516.79 close). Here every underlying
    whose price is not already a close, and whose session's close (unadjusted ``close_raw``, else the adjusted
    ``close``) - or a later one - is now on file, takes that close: ``spot_src = "close"``, ``spot_as_of`` = 16:00 ET
    of that session, the day change against the session before. A price read during today's session keeps its
    estimate until today's close is published. ``symbols`` limits the look (None = every underlying). Commits;
    returns how many prices were settled."""
    q = db.query(U).filter(U.spot.isnot(None), or_(U.spot_src.is_(None), U.spot_src != "close"))
    if symbols is not None:
        syms = sorted({_sym(s) for s in symbols if _sym(s)})
        cands = []
        for part in _chunks(syms, 500):
            cands.extend(q.filter(U.symbol.in_(part)).all())
    else:
        cands = q.all()
    want = {}
    for u in cands:
        sess = spot_session(u.spot_as_of)
        if sess:
            want[u.symbol] = (u, sess)
    if not want:
        return 0
    lo = min(s for _u, s in want.values())
    best: dict[str, tuple[str, float]] = {}
    for part in _chunks(sorted(want), 500):
        for sym, on, raw, adj in (db.query(D.symbol, D.on, D.close_raw, D.close)
                                    .filter(D.symbol.in_(part), D.on >= lo)):
            val = _pos(raw) or _pos(adj)
            if val is None or on < want[sym][1]:
                continue
            if sym not in best or on > best[sym][0]:
                best[sym] = (on, val)
    if not best:
        return 0

    def apply() -> int:
        n = 0
        for sym, (on, val) in best.items():
            u = want[sym][0]
            u.spot, u.spot_src, u.spot_as_of = val, "close", close_time_utc(on)
            pc = _prev_close(db, sym, on)
            u.prev_close = pc
            u.chg_pct = ((val / pc - 1.0) * 100.0) if pc else None
            n += 1
        db.flush()
        return n

    return _txn(db, apply)


def _und(db, sym: str, *, create: bool, now: _dt.datetime | None = None):
    row = db.query(U).filter(U.symbol == sym).one_or_none()
    if row is None and create:
        row = U(symbol=sym, history_done=False, history_tries=0, updated_at=now or _now())
        db.add(row)
        db.flush()
    return row


def underlying(db, symbol) -> dict | None:
    """One ``scr_underlying`` row as a dict, or None."""
    row = db.query(U).filter(U.symbol == _sym(symbol)).one_or_none()
    return _row_dict(row, U) if row is not None else None


# ────────────────────────────────── trend (MATP rule) ──────────────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    """EMA seeded with the first value (MATP's ``ema``)."""
    if not values:
        return []
    a = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def classify_trend(closes: list[float], slope_window: int = TREND_SLOPE_WINDOW) -> str | None:
    """The MATP trend rule, ported from ``resources/MATP/scripts/daily_bounce_alert.py``
    (``classify_trend``, itself "the same rule as classify_trend.py"): strict stacking of
    the close and EMA20 / EMA50 / EMA200 plus the EMA50 slope over ``slope_window`` bars.
    ``close > EMA20 > EMA50 > EMA200`` with EMA50 rising -> ``"up"``; the mirror with EMA50
    falling -> ``"down"``; anything else -> ``"sideways"``; fewer than 210 closes -> None
    (MATP's "Unknown"). Returned in the screener's spelling (up | down | sideways)."""
    vals = [c for c in (_f(x) for x in (closes or ())) if c is not None]
    if len(vals) < TREND_MIN_BARS:
        return None
    e20, e50, e200 = _ema(vals, 20), _ema(vals, 50), _ema(vals, 200)
    c = vals[-1]
    slope_idx = -1 - int(slope_window)
    if abs(slope_idx) > len(e50):
        return None
    slope_up = e50[-1] > e50[slope_idx]
    slope_dn = e50[-1] < e50[slope_idx]
    if c > e20[-1] > e50[-1] > e200[-1] and slope_up:
        return "up"
    if c < e20[-1] < e50[-1] < e200[-1] and slope_dn:
        return "down"
    return "sideways"


# ────────────────────────────────── technicals (pure) ──────────────────────────────────

def _wilder_atr(highs, lows, closes, period: int = ATR_N) -> float | None:
    """The last Wilder ATR: SMA of the first ``period`` true ranges, then the running
    average. None under ``period + 1`` bars."""
    n = len(closes)
    if n < period + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, n)]
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def _wilder_rsi(closes, period: int = RSI_N) -> float | None:
    """The last Wilder RSI: average gain / loss seeded over the first ``period`` changes,
    then smoothed. None under ``period + 1`` closes."""
    n = len(closes)
    if n < period + 1:
        return None
    ch = [closes[i] - closes[i - 1] for i in range(1, n)]
    ag = sum(max(x, 0.0) for x in ch[:period]) / period
    al = sum(max(-x, 0.0) for x in ch[:period]) / period
    for x in ch[period:]:
        ag = (ag * (period - 1) + max(x, 0.0)) / period
        al = (al * (period - 1) + max(-x, 0.0)) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _sma(vals, n: int) -> float | None:
    return (sum(vals[-n:]) / n) if len(vals) >= n else None


def _perf(closes, n: int) -> float | None:
    if len(closes) < n + 1 or closes[-1 - n] <= 0:
        return None
    return (closes[-1] / closes[-1 - n] - 1.0) * 100.0


def technicals(bars) -> dict:
    """Every stock figure of §3 from daily bars ``[{"on", "high", "low", "close",
    "volume"}]`` oldest first (split-adjusted): sma20 / 50 / 200, rsi14 (Wilder), atr14
    (Wilder) and atr_pct, hv20 / hv60 (``option_metrics.hv``, PERCENT), avg_vol20 / 50,
    hi52 / lo52 (the last 252 sessions' high / low), perf5 / perf20 (PERCENT), the last
    session's volume, the trend (MATP rule). A figure without enough bars is None. Pure."""
    good = [b for b in bars or () if _pos(b.get("close")) is not None]
    closes = [float(b["close"]) for b in good]
    highs = [_pos(b.get("high")) or float(b["close"]) for b in good]
    lows = [_pos(b.get("low")) or float(b["close"]) for b in good]
    vols = [float(b["volume"]) for b in good if _f(b.get("volume")) is not None]
    atr = _wilder_atr(highs, lows, closes)
    last = closes[-1] if closes else None
    yr_h, yr_l = highs[-252:], lows[-252:]
    return {
        "sma20": _sma(closes, 20), "sma50": _sma(closes, 50), "sma200": _sma(closes, 200),
        "rsi14": _wilder_rsi(closes), "atr14": atr,
        "atr_pct": (atr / last * 100.0) if (atr is not None and last) else None,
        "hv20": option_metrics.hv(closes, 20), "hv60": option_metrics.hv(closes, 60),
        "avg_vol20": _sma(vols, 20), "avg_vol50": _sma(vols, 50),
        "hi52": max(yr_h) if yr_h else None, "lo52": min(yr_l) if yr_l else None,
        "perf5": _perf(closes, 5), "perf20": _perf(closes, 20),
        "stock_volume": (_f(good[-1].get("volume")) if good else None),
        "trend": classify_trend(closes),
    }


# ────────────────────────────────── contracts ──────────────────────────────────

_CONTRACT_FLOATS = ("chg_pct", "delta", "gamma", "theta", "vega")


def _contract_key(expiry, right, strike):
    e = _day_str(expiry)
    r = str(right or "").strip().upper()[:1]
    k = _pos(strike)
    if e is None or r not in ("C", "P") or k is None:
        return None
    return e, r, round(k, 4)


def _norm_contract(r: dict) -> tuple[tuple, dict] | None:
    key = _contract_key(r.get("expiry"), r.get("right"), r.get("strike"))
    if key is None:
        return None
    iv = _pos(r.get("iv"))
    out = {"weekly": bool(r.get("weekly")), "price": _pos(r.get("price")), "last": _pos(r.get("last")),
           "volume": _count(r.get("volume")), "oi": _count(r.get("oi")),
           "iv": iv if (iv is not None and iv <= CONTRACT_IV_MAX) else None,
           "last_trade": _naive_utc(r.get("last_trade"))}
    for k in _CONTRACT_FLOATS:
        out[k] = _f(r.get(k))
    return key, out


def replace_contracts(db, symbol, rows, *, session_day, as_of) -> int:
    """Replace every ``scr_contract`` row of ``symbol`` with ``rows`` in ONE transaction:
    the old rows are deleted, the new ones bulk-inserted (chunks of ``CHUNK``).

    ``rows`` = ``[{"expiry", "right", "strike", "weekly", "price", "last", "chg_pct",
    "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "last_trade"}]``; a row
    with an unusable key is dropped, a repeated key keeps the last one. Each row is stamped
    ``session = session_day`` and ``as_of``. ``vol_prev`` / ``oi_prev`` per contract: when
    the stored row describes an OLDER session, its final ``volume`` / ``oi`` become the
    previous session's; when it is the same (or a newer) session, its ``vol_prev`` /
    ``oi_prev`` are kept; a new contract has none. Returns the rows written."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("replace_contracts: no symbol")
    session = _day_str(session_day)
    if session is None:
        raise ValueError("replace_contracts: session_day must be a date")
    stamp = _naive_utc(as_of) or _now()
    new: dict[tuple, dict] = {}
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        got = _norm_contract(r)
        if got is not None:
            new[got[0]] = got[1]

    def apply() -> int:
        old: dict[tuple, tuple] = {}
        for e, rt, k, ses, vol, oi, vp, op in (
                db.query(C.expiry, C.right, C.strike, C.session, C.volume, C.oi, C.vol_prev, C.oi_prev)
                  .filter(C.symbol == sym)):
            key = _contract_key(e, rt, k)
            if key is not None:
                old[key] = (ses or "", vol, oi, vp, op)
        out = []
        for key, d in new.items():
            o = old.get(key)
            if o is None:
                vp = op = None
            elif o[0] < session:
                vp, op = o[1], o[2]
            else:
                vp, op = o[3], o[4]
            out.append(dict(d, symbol=sym, expiry=key[0], right=key[1], strike=key[2],
                            vol_prev=vp, oi_prev=op, session=session, as_of=stamp))
        db.query(C).filter(C.symbol == sym).delete(synchronize_session=False)
        for part in _chunks(out):
            db.execute(insert(C), part)
        return len(out)

    return _txn(db, apply)


def newest_session(db, symbol) -> str | None:
    """The newest ET session (``YYYY-MM-DD``) any stored contract row of ``symbol``
    describes, or None when it has none - how recent the stored chain is."""
    v = db.query(func.max(C.session)).filter(C.symbol == _sym(symbol)).scalar()
    return str(v)[:10] if v else None


def delete_contracts(db, symbol) -> int:
    """Remove every contract row of ``symbol``. Commits; returns the rows deleted."""
    sym = _sym(symbol)
    return _txn(db, lambda: db.query(C).filter(C.symbol == sym).delete(synchronize_session=False))


# ────────────────────────────────── daily history ──────────────────────────────────

def last_close(db, symbol) -> tuple[float | None, str | None]:
    """The newest stored (adjusted) close of ``symbol`` and its session."""
    row = (db.query(D.on, D.close).filter(D.symbol == _sym(symbol), D.close.isnot(None))
             .order_by(D.on.desc()).first())
    if row is None:
        return None, None
    return _pos(row[1]), row[0]


def _prev_close(db, sym: str, session: str | None) -> float | None:
    """The newest close of a session BEFORE ``session`` (the reference of the day change);
    without a session, the newest close."""
    q = db.query(D.close).filter(D.symbol == sym, D.close.isnot(None))
    if session:
        q = q.filter(D.on < session)
    row = q.order_by(D.on.desc()).first()
    return _pos(row[0]) if row is not None else None


def raw_bars(db, symbol, n: int = 260) -> list[dict]:
    """The last ``n`` sessions' UNADJUSTED closes ``[{"on", "close"}]`` oldest first (what
    the IV history prices options against - an expired contract kept its pre-split
    strike). Under 20 of them, the adjusted closes are returned instead."""
    sym = _sym(symbol)
    rows = (db.query(D.on, D.close_raw, D.close).filter(D.symbol == sym)
              .order_by(D.on.desc()).limit(max(1, int(n))).all())
    rows = list(reversed(rows))
    raw = [{"on": on, "close": float(cr)} for on, cr, _ in rows if _pos(cr) is not None]
    if len(raw) >= HISTORY_MIN_POINTS:
        return raw
    return [{"on": on, "close": float(c)} for on, _, c in rows if _pos(c) is not None]


def upsert_daily(db, symbol, bars=None, *, raw_bars=None, iv_series=None, overwrite_iv=True,
                 today=None, source: str = IV_SRC_REST) -> int:
    """File daily history for ONE symbol into ``scr_underlying_daily``, field by field: a
    day already on file is updated, a new one inserted, and no input erases another's
    columns. ``bars`` = adjusted ``[{"on", "open", "high", "low", "close", "volume"}]``;
    ``raw_bars`` = unadjusted bars (their ``close`` becomes ``close_raw``); ``iv_series`` =
    ``[{"on", "iv"}]`` (PERCENT; ``overwrite_iv=False`` fills only days without one).
    Each IV filed is tagged ``iv30_src = source`` (default ``rest``: the per-stock REST
    history is the series' one caller). A ``live`` reading (the EOD pass) is never
    replaced by another source, not even with ``overwrite_iv``. Points after ``today``
    (when given), with no positive close, with high < low, or an IV outside 0.1-1000 are
    skipped. Commits; returns the day rows touched."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("upsert_daily: no symbol")
    src = _src(source, IV_SRC_REST)
    last_day = _day_str(today) if today is not None else None

    def ok_day(v) -> str | None:
        on = _day_str(v)
        return on if (on is not None and (last_day is None or on <= last_day)) else None

    adj: dict[str, dict] = {}
    for b in bars or ():
        if not isinstance(b, dict):
            continue
        on, close = ok_day(b.get("on")), _pos(b.get("close"))
        hi, lo = _pos(b.get("high")), _pos(b.get("low"))
        if on is None or close is None or (hi is not None and lo is not None and hi < lo):
            continue
        vol = _f(b.get("volume"))
        adj[on] = {"open": _pos(b.get("open")), "high": hi, "low": lo, "close": close,
                   "volume": vol if (vol is not None and vol >= 0) else None}
    raw: dict[str, float] = {}
    for b in raw_bars or ():
        if isinstance(b, dict):
            on, close = ok_day(b.get("on")), _pos(b.get("close"))
            if on is not None and close is not None:
                raw[on] = close
    ivs: dict[str, float] = {}
    for p in iv_series or ():
        if isinstance(p, dict):
            on, v = ok_day(p.get("on")), _f(p.get("iv", p.get("iv30")))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            on, v = ok_day(p[0]), _f(p[1])
        else:
            continue
        if on is not None and v is not None and DAILY_IV_LO <= v <= DAILY_IV_HI:
            ivs[on] = v
    days = sorted(set(adj) | set(raw) | set(ivs))
    if not days:
        return 0

    def apply() -> int:
        existing: dict[str, ScrUnderlyingDaily] = {}
        for part in _chunks(days, 500):
            for r in db.query(D).filter(D.symbol == sym, D.on.in_(part)):
                existing[r.on] = r
        new = []
        for on in days:
            r = existing.get(on)
            vals: dict = {}
            for k, v in (adj.get(on) or {}).items():
                if v is not None:
                    vals[k] = v
            if on in raw:
                vals["close_raw"] = raw[on]
            if on in ivs and (r is None or r.iv30 is None or (
                    overwrite_iv and (r.iv30_src != IV_SRC_LIVE or src == IV_SRC_LIVE))):
                vals["iv30"] = ivs[on]
                vals["iv30_src"] = src
            if r is None:
                new.append(dict({"open": None, "high": None, "low": None, "close": None,
                                 "close_raw": None, "volume": None, "iv30": None, "iv30_src": None},
                                symbol=sym, on=on, **vals))
            else:
                for k, v in vals.items():
                    setattr(r, k, v)
        for part in _chunks(new):
            db.execute(insert(D), part)
        db.flush()
        return len(days)

    with _DAILY_LOCK:
        return _txn(db, apply)


def file_grouped_day(db, day, bars, *, adjusted: bool = True, symbols=None) -> int:
    """File ONE session of grouped daily bars (``massive.Client.grouped_daily``) for every
    symbol in ``symbols`` (the universe; None = every bar): adjusted bars set open / high /
    low / close / volume, unadjusted ones ``close_raw`` - field by field, so the two reads
    of a day fill one row. Commits; returns the symbols filed."""
    on = _day_str(day)
    if on is None:
        raise ValueError("file_grouped_day: a date is needed")
    keep = set(_sym(s) for s in symbols) if symbols is not None else None
    by_sym: dict[str, dict] = {}
    for b in bars or ():
        if not isinstance(b, dict):
            continue
        sym = _sym(b.get("symbol"))
        close = _pos(b.get("close"))
        if not sym or close is None or (keep is not None and sym not in keep):
            continue
        hi, lo = _pos(b.get("high")), _pos(b.get("low"))
        if hi is not None and lo is not None and hi < lo:
            continue
        if adjusted:
            vol = _f(b.get("volume"))
            by_sym[sym] = {"open": _pos(b.get("open")), "high": hi, "low": lo, "close": close,
                           "volume": vol if (vol is not None and vol >= 0) else None}
        else:
            by_sym[sym] = {"close_raw": close}
    if not by_sym:
        return 0
    syms = sorted(by_sym)

    def apply() -> int:
        existing: dict[str, ScrUnderlyingDaily] = {}
        for part in _chunks(syms, 500):
            for r in db.query(D).filter(D.on == on, D.symbol.in_(part)):
                existing[r.symbol] = r
        new = []
        for sym in syms:
            vals = {k: v for k, v in by_sym[sym].items() if v is not None}
            r = existing.get(sym)
            if r is None:
                new.append(dict({"open": None, "high": None, "low": None, "close": None,
                                 "close_raw": None, "volume": None, "iv30": None, "iv30_src": None},
                                symbol=sym, on=on, **vals))
            else:
                for k, v in vals.items():
                    setattr(r, k, v)
        for part in _chunks(new):
            db.execute(insert(D), part)
        db.flush()
        return len(syms)

    with _DAILY_LOCK:
        return _txn(db, apply)


def stock_day_status(db, start, end) -> dict[str, tuple[int, int]]:
    """``{on: (rows with an adjusted close, rows with an unadjusted close)}`` for the
    sessions between ``start`` and ``end`` (inclusive) - which grouped days are on file."""
    a, b = _day_str(start), _day_str(end)
    rows = (db.query(D.on, func.count(D.close), func.count(D.close_raw))
              .filter(D.on >= a, D.on <= b).group_by(D.on).all())
    return {on: (int(n1 or 0), int(n2 or 0)) for on, n1, n2 in rows}


def file_iv_day(db, day, values: dict, *, source: str = IV_SRC_FLAT) -> int:
    """File ONE session's IV30 for many stocks (``values`` = ``{symbol: IV30 PERCENT}``,
    from Massive's options day file - ``scr_flativ.day_iv30``), tagged ``iv30_src =
    source``. A day is filled only when it has no IV30 yet or its IV30 is itself a history
    reading (``flat`` / ``rest`` - a re-published day file redoes it); a ``live`` reading
    (the EOD pass) always wins, and so does an untagged one (filed before v4.138 - it may
    be live). ``source="live"`` replaces anything. A symbol without a daily row for the
    day gets one. Values that are not numbers, or outside 0.1-1000, are skipped. Under
    ``_DAILY_LOCK``; commits; returns the readings written."""
    on = _day_str(day)
    if on is None:
        raise ValueError("file_iv_day: a date is needed")
    src = _src(source, IV_SRC_FLAT)
    clean: dict[str, float] = {}
    for k, v in (values or {}).items():
        sym, f = _sym(k), _f(v)
        if sym and f is not None and DAILY_IV_LO <= f <= DAILY_IV_HI:
            clean[sym] = f
    if not clean:
        return 0
    syms = sorted(clean)

    def apply() -> int:
        existing: dict[str, ScrUnderlyingDaily] = {}
        for part in _chunks(syms, 500):
            for r in db.query(D).filter(D.on == on, D.symbol.in_(part)):
                existing[r.symbol] = r
        new, n = [], 0
        for sym in syms:
            r = existing.get(sym)
            if r is None:
                new.append({"symbol": sym, "on": on, "open": None, "high": None, "low": None,
                            "close": None, "close_raw": None, "volume": None, "iv30": clean[sym],
                            "iv30_src": src})
                n += 1
            elif r.iv30 is None or r.iv30_src in IV_SRC_REPLACEABLE or src == IV_SRC_LIVE:
                r.iv30, r.iv30_src = clean[sym], src
                n += 1
        for part in _chunks(new):
            db.execute(insert(D), part)
        db.flush()
        return n

    with _DAILY_LOCK:
        return _txn(db, apply)


def iv_day_status(db, start, end, *, symbols=None) -> dict[str, dict[str, int]]:
    """``{on: {source: IV30 readings}}`` for the sessions between ``start`` and ``end``
    (inclusive) - which days have IV30s and from where (``live`` / ``flat`` / ``rest``;
    ``none`` = untagged, filed before v4.138). ``symbols`` limits the count."""
    a, b = _day_str(start), _day_str(end)
    q = (db.query(D.on, D.iv30_src, func.count(D.id))
           .filter(D.on >= a, D.on <= b, D.iv30.isnot(None)))
    if symbols is not None:
        keep = sorted({_sym(s) for s in symbols if _sym(s)})
        if not keep:
            return {}
        out: dict[str, dict[str, int]] = {}
        for part in _chunks(keep, 500):
            for on, src, n in q.filter(D.symbol.in_(part)).group_by(D.on, D.iv30_src).all():
                cell = out.setdefault(on, {})
                key = src or "none"
                cell[key] = cell.get(key, 0) + int(n or 0)
        return out
    out = {}
    for on, src, n in q.group_by(D.on, D.iv30_src).all():
        out.setdefault(on, {})[src or "none"] = int(n or 0)
    return out


def daily_counts(db) -> dict[str, int]:
    """``{symbol: adjusted closes on file}`` for every symbol with daily rows."""
    return {s: int(n or 0) for s, n in db.query(D.symbol, func.count(D.close)).group_by(D.symbol)}


def day_closes_raw(db, day, symbols=None) -> dict[str, float]:
    """``{symbol: that day's UNADJUSTED close}`` (``close_raw``) for ``day`` - what an
    options day file is priced against (``scr_flativ.day_iv30``; v4.138). ``symbols``
    limits it. A symbol without a positive raw close that day is left out (never the
    adjusted close: a split would pick the wrong strike)."""
    on = _day_str(day)
    if on is None:
        raise ValueError("day_closes_raw: a date is needed")
    q = db.query(D.symbol, D.close_raw).filter(D.on == on, D.close_raw.isnot(None))
    out: dict[str, float] = {}
    if symbols is None:
        rows = q.all()
    else:
        keep = sorted({_sym(s) for s in symbols if _sym(s)})
        rows = [r for part in _chunks(keep, 500) for r in q.filter(D.symbol.in_(part)).all()]
    for sym, c in rows:
        f = _pos(c)
        if f is not None:
            out[sym] = f
    return out


def _counts_by_symbol(db, col, symbols, start, end) -> dict[str, int]:
    q = db.query(D.symbol, func.count(col)).filter(col.isnot(None))
    a, b = _day_str(start), _day_str(end)
    if a:
        q = q.filter(D.on >= a)
    if b:
        q = q.filter(D.on <= b)
    if symbols is None:
        return {s: int(n or 0) for s, n in q.group_by(D.symbol).all()}
    keep = sorted({_sym(s) for s in symbols if _sym(s)})
    out: dict[str, int] = {}
    for part in _chunks(keep, 500):
        for s, n in q.filter(D.symbol.in_(part)).group_by(D.symbol).all():
            out[s] = int(n or 0)
    return out


def iv_counts(db, symbols=None, *, start=None, end=None) -> dict[str, int]:
    """``{symbol: daily IV30 readings}`` (any source) between ``start`` and ``end``
    (inclusive, either open) - how many points an IV history has (v4.138: the flat-file
    lane marks a history done at ``HISTORY_MIN_POINTS``). ``symbols`` limits it."""
    return _counts_by_symbol(db, D.iv30, symbols, start, end)


def raw_close_counts(db, symbols=None, *, start=None, end=None) -> dict[str, int]:
    """``{symbol: unadjusted closes on file}`` between ``start`` and ``end`` - whether an
    IV history can be built yet (v4.138). ``symbols`` limits it."""
    return _counts_by_symbol(db, D.close_raw, symbols, start, end)


def sec_types(db, symbols=None) -> dict[str, str | None]:
    """``{symbol: security type}`` of the stored underlyings (None = not named yet) - one
    query for the flat-file lane's symbol map (v4.138). ``symbols`` limits it."""
    q = db.query(U.symbol, U.sec_type)
    if symbols is None:
        return {s: t for s, t in q.all()}
    keep = sorted({_sym(s) for s in symbols if _sym(s)})
    out: dict[str, str | None] = {}
    for part in _chunks(keep, 500):
        for s, t in q.filter(U.symbol.in_(part)).all():
            out[s] = t
    return out


def big_moves(db, day, *, lo: float, hi: float, symbols=None) -> list[str]:
    """Symbols whose adjusted close on ``day`` is outside ``lo``-``hi`` times the close of
    the session before it - a split filed after the older bars were (the stored history is
    then no longer adjusted for it). ``symbols`` limits the check."""
    on = _day_str(day)
    prev = clock.prev_trading_day(on).isoformat()
    today = dict(db.query(D.symbol, D.close).filter(D.on == on, D.close.isnot(None)))
    before = dict(db.query(D.symbol, D.close).filter(D.on == prev, D.close.isnot(None)))
    keep = set(symbols) if symbols is not None else None
    out = []
    for sym, c in today.items():
        p = before.get(sym)
        if p is None or (keep is not None and sym not in keep):
            continue
        c, p = _pos(c), _pos(p)
        if c is None or p is None:
            continue
        if not (lo <= c / p <= hi):
            out.append(sym)
    return sorted(out)


# ────────────────────────────────── the underlying ──────────────────────────────────

def _pass_session(db, u) -> str | None:
    if u is None or u.pass_id is None:
        return None
    p = db.get(P, u.pass_id)
    return p.session if p is not None else None


def _apply_iv(db, u, session: str | None) -> None:
    """The IV figures of ``u`` from its daily IV30 series (the last 252 readings, today
    included - option_metrics' A3.3 convention): ``iv30`` (the current reading: the pass's
    own, else the one filed for ``session``, else the newest), ``iv30_prev`` (the reading
    of the session before), ``iv_rank`` / ``iv_pct`` / ``iv_n`` / ``iv_lo`` / ``iv_hi``."""
    rows = (db.query(D.on, D.iv30).filter(D.symbol == u.symbol, D.iv30.isnot(None))
              .order_by(D.on.desc()).limit(IV_WINDOW + 1).all())
    rows = [(on, float(v)) for on, v in reversed(rows)]
    current = _pos(u.iv30)
    if session is None and rows:
        session = rows[-1][0]
    past = [v for on, v in rows if session is None or on < session]
    if current is None:
        at = [v for on, v in rows if on == session]
        current = at[-1] if at else None
    past = past[-(IV_WINDOW - 1):]
    window = past + ([current] if current is not None else [])
    rk = option_metrics.iv_rank_pct(window, current)
    u.iv30 = current
    u.iv30_prev = past[-1] if past else None
    u.iv_rank = rk.get("iv_rank")
    u.iv_pct = rk.get("iv_pct")
    u.iv_n = rk.get("iv_n")
    u.iv_lo = rk.get("lo")
    u.iv_hi = rk.get("hi")


def update_underlying_pass(db, symbol, *, session_day, pass_id=None, spot=None, spot_src=None,
                           spot_as_of=None, iv30=None, exp_move30=None, call_vol=None, put_vol=None,
                           call_oi=None, put_oi=None, n_contracts=None, file_iv: bool = False,
                           now=None) -> dict:
    """What one market pass learnt about ``symbol`` (§4.2), in one transaction: the spot
    (kept from before when this pass had none) with its source and time, the day change
    against the close of the session before ``session_day``, IV30 (PERCENT; kept when
    this pass could not read one) and the one-sigma 30-day move, call / put volume and open
    interest, the contracts kept, the pass id. ``file_iv`` (the EOD pass) also files this
    pass's IV30 as ``session_day``'s daily reading, tagged ``iv30_src = live`` (under
    ``_DAILY_LOCK``; it replaces any history reading of that day). Then the IV figures
    (``_apply_iv``). Creates the row when missing. Commits; returns the row as a dict."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("update_underlying_pass: no symbol")
    session = _day_str(session_day)
    stamp = _now(now)
    s = _pos(spot)
    v30 = _pos(iv30)

    def apply() -> dict:
        u = _und(db, sym, create=True, now=stamp)
        if s is not None:
            u.spot = s
            u.spot_src = (str(spot_src or "")[:8] or None)
            u.spot_as_of = _naive_utc(spot_as_of) or stamp
        if v30 is not None:
            u.iv30 = v30
            u.exp_move30 = _f(exp_move30) if exp_move30 is not None else v30 * math.sqrt(30.0 / 365.0)
        u.call_vol, u.put_vol = _count(call_vol), _count(put_vol)
        u.call_oi, u.put_oi = _count(call_oi), _count(put_oi)
        u.n_contracts = _count(n_contracts)
        pc = _prev_close(db, sym, session)
        u.prev_close = pc
        u.chg_pct = ((u.spot / pc - 1.0) * 100.0) if (u.spot and pc) else None
        if file_iv and v30 is not None and session is not None and DAILY_IV_LO <= v30 <= DAILY_IV_HI:
            r = db.query(D).filter(D.symbol == sym, D.on == session).one_or_none()
            if r is None:
                db.add(D(symbol=sym, on=session, iv30=v30, iv30_src=IV_SRC_LIVE))
            else:
                r.iv30, r.iv30_src = v30, IV_SRC_LIVE
            db.flush()
        if pass_id is not None:
            u.pass_id = int(pass_id)
        _apply_iv(db, u, session)
        u.updated_at = stamp
        db.flush()
        return _row_dict(u, U)

    if file_iv:                       # it may insert a scr_underlying_daily row
        with _DAILY_LOCK:
            return _txn(db, apply)
    return _txn(db, apply)


def recompute_iv(db, symbol, *, session_day=None, now=None) -> dict:
    """The IV figures of ``symbol`` again (after its IV history or an EOD reading was
    filed): ``_apply_iv`` against ``session_day`` (default: the session of its latest
    pass). Commits; returns the row as a dict."""
    sym = _sym(symbol)
    stamp = _now(now)

    def apply() -> dict:
        u = _und(db, sym, create=True, now=stamp)
        _apply_iv(db, u, _day_str(session_day) if session_day else _pass_session(db, u))
        u.updated_at = stamp
        db.flush()
        return _row_dict(u, U)

    return _txn(db, apply)


def recompute_iv_many(db, symbols, *, now=None, chunk: int = 200, stop=None) -> int:
    """``recompute_iv`` for many symbols (after a day file filed IV30s for all of them),
    one commit per ``chunk`` instead of one per symbol; each symbol against the session of
    its latest pass, as ``recompute_iv`` does by default. ``stop()`` returning True ends it
    early. Creates missing rows. Returns the symbols done."""
    stamp = _now(now)
    syms = sorted({s for s in (_sym(x) for x in symbols or ()) if s})
    done = 0
    for part in _chunks(syms, max(1, int(chunk))):
        if stop is not None and stop():
            break

        def apply(part=part) -> int:
            have = {u.symbol: u for u in db.query(U).filter(U.symbol.in_(part))}
            for sym in part:
                u = have.get(sym)
                if u is None:
                    u = U(symbol=sym, history_done=False, history_tries=0, updated_at=stamp)
                    db.add(u)
                    db.flush()
                _apply_iv(db, u, _pass_session(db, u))
                u.updated_at = stamp
            db.flush()
            return len(part)

        done += _txn(db, apply) or 0
    return done


def _recompute_tech_row(db, u, stamp) -> None:
    rows = (db.query(D.on, D.open, D.high, D.low, D.close, D.volume)
              .filter(D.symbol == u.symbol, D.close.isnot(None)).order_by(D.on).all())
    bars = [{"on": on, "open": o, "high": h, "low": lo, "close": c, "volume": v}
            for on, o, h, lo, c, v in rows]
    t = technicals(bars)
    for k, v in t.items():
        setattr(u, k, v)
    session = _pass_session(db, u)
    if session:
        pc = _prev_close(db, u.symbol, session)
    else:
        pc = _pos(bars[-1]["close"]) if bars else None
    u.prev_close = pc
    u.chg_pct = ((u.spot / pc - 1.0) * 100.0) if (u.spot and pc) else None
    u.bars_as_of = close_time_utc(bars[-1]["on"]) if bars else None
    u.updated_at = stamp


def recompute_technicals(db, symbol, *, now=None) -> dict:
    """The stock figures of ``symbol`` from its adjusted daily bars (``technicals``) plus
    ``prev_close`` / ``chg_pct`` (the close of the session before its latest pass, or the
    newest close before any pass) and ``bars_as_of`` (the newest bar's 16:00 ET). Creates
    the row when missing. Commits; returns the row as a dict."""
    sym = _sym(symbol)
    if not sym:
        raise ValueError("recompute_technicals: no symbol")
    stamp = _now(now)

    def apply() -> dict:
        u = _und(db, sym, create=True, now=stamp)
        _recompute_tech_row(db, u, stamp)
        db.flush()
        return _row_dict(u, U)

    return _txn(db, apply)


def recompute_technicals_many(db, symbols, *, now=None, chunk: int = 200, stop=None) -> int:
    """``recompute_technicals`` for many symbols, one commit per ``chunk``; ``stop()``
    returning True ends it early. Returns the symbols done."""
    stamp = _now(now)
    syms = [s for s in (_sym(x) for x in symbols or ()) if s]
    done = 0
    for part in _chunks(syms, max(1, int(chunk))):
        if stop is not None and stop():
            break

        def apply(part=part) -> int:
            have = {u.symbol: u for u in db.query(U).filter(U.symbol.in_(part))}
            for sym in part:
                u = have.get(sym)
                if u is None:
                    u = U(symbol=sym, history_done=False, history_tries=0, updated_at=stamp)
                    db.add(u)
                    db.flush()
                _recompute_tech_row(db, u, stamp)
            db.flush()
            return len(part)

        done += _txn(db, apply) or 0
    return done


def set_identity(db, records, *, only=None, now=None) -> int:
    """Name / security type / exchange for the underlyings: ``records`` =
    ``[{"symbol", "name", "sec_type", "exchange"}]`` (``sec_type`` in ``SEC_TYPES``,
    ``exchange`` in ``EXCHANGES``; anything else is stored as None / OTHER). ``only`` (a
    set of symbols - the universe) limits the write and creates missing rows for them.
    Commits; returns the rows written."""
    stamp = _now(now)
    keep = set(_sym(s) for s in only) if only is not None else None
    by: dict[str, dict] = {}
    for r in records or ():
        if not isinstance(r, dict):
            continue
        sym = _sym(r.get("symbol"))
        if not sym or (keep is not None and sym not in keep):
            continue
        st = str(r.get("sec_type") or "").strip().lower() or None
        ex = str(r.get("exchange") or "").strip().upper() or None
        by[sym] = {"name": (str(r.get("name") or "").strip()[:160] or None),
                   "sec_type": st if st in SEC_TYPES else None,
                   "exchange": (ex if ex in EXCHANGES else "OTHER") if ex else None}
    if not by:
        return 0
    syms = sorted(by)

    def apply() -> int:
        have: dict[str, ScrUnderlying] = {}
        for part in _chunks(syms, 500):
            for u in db.query(U).filter(U.symbol.in_(part)):
                have[u.symbol] = u
        n = 0
        new = []
        for sym in syms:
            u = have.get(sym)
            vals = by[sym]
            if u is None:
                if keep is None:
                    continue
                new.append({"symbol": sym, "history_done": False, "history_tries": 0,
                            "updated_at": stamp, **vals})
            else:
                for k, v in vals.items():
                    if v is not None or k != "name":
                        setattr(u, k, v)
                u.updated_at = stamp
            n += 1
        for part in _chunks(new):
            db.execute(insert(U), part)
        db.flush()
        return n

    return _txn(db, apply)


def identity_missing(db) -> bool:
    """Is there an underlying without a security type yet (identity never read for it)?"""
    return db.query(U.id).filter(U.sec_type.is_(None)).first() is not None


def set_earnings(db, dates: dict, *, today, src: str = "nasdaq", now=None) -> int:
    """The next earnings date per underlying: ``dates`` = ``{symbol: YYYY-MM-DD}`` (the
    nearest date on or after ``today``). A symbol in ``dates`` gets it (``earnings_src``
    = ``src``); a stored date already past ``today`` is cleared; any other stored date is
    kept (a source that missed a day must not wipe it). Commits; returns the rows changed."""
    day = _day_str(today)
    stamp = _now(now)
    clean = {}
    for k, v in (dates or {}).items():
        sym, on = _sym(k), _day_str(v)
        if sym and on and on >= day:
            clean[sym] = min(on, clean.get(sym, on))

    def apply() -> int:
        n = 0
        for u in db.query(U):
            want = clean.get(u.symbol)
            if want is not None:
                if u.earnings_date != want or u.earnings_src != src:
                    u.earnings_date, u.earnings_src, u.updated_at = want, str(src)[:8], stamp
                    n += 1
            elif u.earnings_date and u.earnings_date < day:
                u.earnings_date, u.earnings_src, u.updated_at = None, None, stamp
                n += 1
        db.flush()
        return n

    return _txn(db, apply)


def file_earnings_day(db, day, rows, *, src: str = "nasdaq", now=None) -> int:
    """Keep the earnings dates seen on one calendar day (v4.143): ``rows`` = Nasdaq-shaped
    ``{symbol, when}`` dicts (``when`` as ``calendars`` spells it: pre / post / tbd, or bmo /
    amc; stored as ``bmo`` / ``amc`` / NULL) -> ``scr_earnings`` (a date already there keeps
    its first sighting; ``when_`` filled in when it was unknown), and the day is marked
    fetched in ``scr_earnings_day`` (even with no rows: a holiday, or nobody reports).
    Commits; returns the dates added."""
    on = _day_str(day)
    if on is None:
        raise ValueError("file_earnings_day: a date is needed")
    stamp = _now(now)
    seen: dict[str, str | None] = {}
    for r in rows or ():
        sym = _sym((r or {}).get("symbol"))
        if sym:
            w = WHEN_WORDS.get(str((r or {}).get("when") or "").strip().lower())
            if sym not in seen or (seen[sym] is None and w):
                seen[sym] = w

    def apply() -> int:
        have = {e.symbol: e for e in db.query(ScrEarnings).filter(ScrEarnings.on == on)}
        n = 0
        for sym, w in seen.items():
            e = have.get(sym)
            if e is None:
                db.add(ScrEarnings(symbol=sym, on=on, when_=w, src=str(src)[:8], first_seen=stamp))
                n += 1
            elif not e.when_ and w:
                e.when_ = w
        d = db.query(ScrEarningsDay).filter(ScrEarningsDay.on == on).first()
        if d is None:
            db.add(ScrEarningsDay(on=on, fetched_at=stamp, n_rows=len(seen)))
        else:
            d.fetched_at, d.n_rows = stamp, len(seen)
        db.flush()
        return n

    return _txn(db, apply)


def earnings_days_empty(db, days) -> set[str]:
    """Which of ``days`` were fetched and had NO rows - a holiday, nobody reporting, or (before
    v4.143's strict fetch) a failed request filed as empty; the backfill reads a trading day
    among them once more."""
    want = sorted({_day_str(d) for d in days or () if _day_str(d)})
    out: set[str] = set()
    for part in _chunks(want, 500):
        out.update(on for (on,) in db.query(ScrEarningsDay.on)
                   .filter(ScrEarningsDay.on.in_(part), ScrEarningsDay.n_rows == 0))
    return out


def earnings_days_fetched(db, days) -> set[str]:
    """Which of ``days`` (YYYY-MM-DD) already had their earnings list fetched."""
    want = sorted({_day_str(d) for d in days or () if _day_str(d)})
    out: set[str] = set()
    for part in _chunks(want, 500):
        out.update(on for (on,) in db.query(ScrEarningsDay.on).filter(ScrEarningsDay.on.in_(part)))
    return out


def earnings_dates(db, symbol, start=None, end=None) -> list[dict]:
    """``[{on, when}]`` of ``symbol``'s earnings dates between ``start`` and ``end``
    (inclusive, either open), oldest first."""
    q = db.query(ScrEarnings.on, ScrEarnings.when_).filter(ScrEarnings.symbol == _sym(symbol))
    a, b = _day_str(start), _day_str(end)
    if a:
        q = q.filter(ScrEarnings.on >= a)
    if b:
        q = q.filter(ScrEarnings.on <= b)
    return [{"on": on, "when": w} for on, w in q.order_by(ScrEarnings.on).all()]


# ────────────────────────────────── the universe ──────────────────────────────────

def upsert_universe(db, counts: dict, *, now=None, min_keep_ratio: float = 0.5,
                    deactivate: bool = True) -> dict:
    """Massive's list of optionable underlyings (``{symbol: contracts}``) into
    ``scr_universe``: listed symbols are active (``n_contracts``, ``last_seen`` = now; new
    ones ``first_seen`` too, plus an empty ``scr_underlying`` row), every other symbol
    inactive. A list that is empty, or shorter than ``min_keep_ratio`` of the active one
    (a partial answer), adds and updates but deactivates nothing - yesterday's list holds.

    ``deactivate=False`` is a streamed save of a walk still in progress (the first pages of
    the contracts list): it adds and updates, never deactivates, whatever its length. The
    final save of a COMPLETE walk passes ``deactivate=True`` and ``now`` = the walk's
    completion time (``last_seen`` then says when the full list was known, not when the
    walk started).

    Commits; returns ``{"n", "new", "inactive", "partial"}`` - ``partial`` is True when
    nothing could be deactivated (a short / empty list, or ``deactivate=False``)."""
    stamp = _now(now)
    got = {}
    for k, v in (counts or {}).items():
        sym = _sym(k)
        if sym:
            got[sym] = (_count(v) or 0) + got.get(sym, 0)

    def apply() -> dict:
        rows = {u.symbol: u for u in db.query(UNI)}
        n_active = sum(1 for u in rows.values() if u.active)
        partial = (not deactivate) or (not got) or len(got) < min_keep_ratio * n_active
        new = []
        for sym, n in got.items():
            u = rows.get(sym)
            if u is None:
                new.append({"symbol": sym, "n_contracts": n, "first_seen": stamp, "last_seen": stamp,
                            "active": True})
            else:
                u.n_contracts, u.last_seen, u.active = n, stamp, True
        inactive = 0
        if not partial:
            for sym, u in rows.items():
                if sym not in got and u.active:
                    u.active = False
                    inactive += 1
        for part in _chunks(new):
            db.execute(insert(UNI), part)
        have = set()
        for part in _chunks(sorted(got), 500):
            have |= {s for (s,) in db.query(U.symbol).filter(U.symbol.in_(part))}
        und_new = [{"symbol": s, "history_done": False, "history_tries": 0, "updated_at": stamp}
                   for s in sorted(got) if s not in have]
        for part in _chunks(und_new):
            db.execute(insert(U), part)
        db.flush()
        return {"n": len(got), "new": len(new), "inactive": inactive, "partial": bool(partial)}

    return _txn(db, apply)


def pass_symbols_weighted(db, *, now=None, keep_days: int = INACTIVE_KEEP_DAYS) -> list[tuple[str, int]]:
    """``[(symbol, n_contracts)]`` in pass order, most contracts first: every active
    universe symbol plus those gone from Massive's list less than ``keep_days`` ago. The
    counts weight a pass's percent and ETA (v4.137) - they come from the stored universe,
    so they survive a restart."""
    cutoff = _now(now) - _dt.timedelta(days=keep_days)
    rows = (db.query(UNI.symbol, UNI.n_contracts, UNI.active, UNI.last_seen).all())
    keep = [(s, int(n or 0)) for s, n, a, seen in rows if a or (seen is not None and seen >= cutoff)]
    keep.sort(key=lambda x: (-x[1], x[0]))
    return keep


def pass_symbols(db, *, now=None, keep_days: int = INACTIVE_KEEP_DAYS) -> list[str]:
    """The symbols a market pass reads, most contracts first (``pass_symbols_weighted``
    without the counts - the two never drift apart)."""
    return [s for s, _ in pass_symbols_weighted(db, now=now, keep_days=keep_days)]


def universe_info(db) -> dict:
    """``{"n": active symbols, "total": rows, "refreshed": newest last_seen}``."""
    n = db.query(func.count(UNI.id)).filter(UNI.active.is_(True)).scalar() or 0
    total = db.query(func.count(UNI.id)).scalar() or 0
    refreshed = db.query(func.max(UNI.last_seen)).scalar()
    return {"n": int(n), "total": int(total), "refreshed": refreshed}


# ────────────────────────────────── passes ──────────────────────────────────

def start_pass(db, *, kind: str, session, n_symbols: int, now=None) -> int:
    """A new ``scr_pass`` row (``finished`` NULL while it runs). Commits; returns its id."""
    stamp = _now(now)

    def apply() -> int:
        p = P(kind=str(kind or "cycle")[:8], session=_day_str(session), started=stamp,
              n_symbols=int(n_symbols or 0), n_ok=0, n_failed=0, n_contracts=0, requests=0, ms=0)
        db.add(p)
        db.flush()
        return p.id

    return _txn(db, apply)


def finish_pass(db, pass_id, *, n_ok=None, n_failed=None, n_contracts=None, requests=None, ms=None,
                finished: bool = True, now=None, n_symbols=None, partial: bool | None = None) -> dict | None:
    """Record a pass's counts; ``finished=True`` stamps ``finished`` (the web app reloads on
    a newer finished pass), ``False`` leaves it NULL (a pass cut short by a paused Massive
    is not a finished one). ``n_symbols`` updates the symbol count of a pass that grew with a
    streamed universe (v4.137; ``finished=False`` with only it set records the growth of a
    running pass). ``partial`` (when not None) records whether the pass was read on a
    partial list of optionable stocks. Commits; returns the row as a dict (None when
    unknown)."""
    stamp = _now(now)

    def apply():
        p = db.get(P, int(pass_id))
        if p is None:
            return None
        for k, v in (("n_ok", n_ok), ("n_failed", n_failed), ("n_contracts", n_contracts),
                     ("requests", requests), ("ms", ms), ("n_symbols", n_symbols)):
            if v is not None:
                setattr(p, k, int(v))
        if partial is not None:
            p.partial = bool(partial)
        if finished:
            p.finished = stamp
        db.flush()
        return _row_dict(p, P)

    return _txn(db, apply)


def last_pass(db, *, kind=None, finished: bool | None = True, min_contracts: int | None = None,
              partial: bool | None = None) -> dict | None:
    """The newest pass (of ``kind`` when given; only finished ones by default, any with
    ``finished=None``; with ``min_contracts``, only one that stored at least that many
    contracts - a finished pass that read nothing is not "done"; ``partial=False`` only one
    read on a complete list of optionable stocks - NULL counts as complete - and
    ``partial=True`` only one read on a partial list) as a dict, or None."""
    q = db.query(P)
    if kind:
        q = q.filter(P.kind == kind)
    if min_contracts is not None:
        q = q.filter(P.n_contracts >= int(min_contracts))
    if partial is False:
        q = q.filter(or_(P.partial.is_(None), P.partial.is_(False)))
    elif partial is True:
        q = q.filter(P.partial.is_(True))
    if finished is True:
        q = q.filter(P.finished.isnot(None))
    elif finished is False:
        q = q.filter(P.finished.is_(None))
    row = q.order_by(P.id.desc()).first()
    return _row_dict(row, P) if row is not None else None


# ────────────────────────────────── the heartbeat row ──────────────────────────────────

def status(db) -> dict | None:
    """The collector's heartbeat row (``id = 1``) as a dict, or None before its first write."""
    row = db.get(ScrStatus, STATUS_ID)
    return _row_dict(row, ScrStatus) if row is not None else None


def _json_safe(v, depth: int = 0):
    """A value a JSON column stores on any backend: dicts (string keys) and lists of
    numbers / strings / booleans / None; a datetime becomes ISO text (naive = UTC, with
    its ``+00:00``), a date ``YYYY-MM-DD``, a tuple / set a list, NaN / inf None, anything
    else its text. Nested at most 6 deep."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_dt.timezone.utc)
        return v.isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    if depth >= 6:
        return str(v)
    if isinstance(v, dict):
        return {str(k): _json_safe(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        items = sorted(v, key=str) if isinstance(v, (set, frozenset)) else v
        return [_json_safe(x, depth + 1) for x in items]
    f = _f(v)                       # numpy scalars and friends
    if f is not None:
        return f
    return str(v)


def set_status(db, **fields) -> None:
    """Update the heartbeat row (created on first use) with any of its columns; unknown
    keys are ignored with a warning. Datetimes are stored naive UTC (an aware one or ISO
    text is converted), strings cut to the column width, a JSON column (``progress``) made
    JSON-safe (``_json_safe``). ``heartbeat`` is set to now (or the ``now`` keyword) unless
    given. A key left out keeps its stored value; ``None`` clears it. Commits."""
    cols = {c.name: c for c in ScrStatus.__table__.columns if c.name != "id"}
    stamp = _now(fields.pop("now", None))

    def apply() -> None:
        row = db.get(ScrStatus, STATUS_ID)
        if row is None:
            row = ScrStatus(id=STATUS_ID)
            db.add(row)
        for k, v in fields.items():
            col = cols.get(k)
            if col is None:
                log.warning("scr_store.set_status: unknown field %r ignored", k)
                continue
            if v is not None and isinstance(col.type, DateTime):
                v = _naive_utc(v)
            elif v is not None and isinstance(col.type, JSON):
                v = _json_safe(v)
            elif v is not None and isinstance(col.type, String) and col.type.length:
                v = str(v)[:col.type.length]
            setattr(row, k, v)
        if "heartbeat" not in fields:
            row.heartbeat = stamp
        db.flush()

    _txn(db, apply)


# ────────────────────────────────── IV history bookkeeping ──────────────────────────────────

def history_applies(symbol, sec_type=None) -> bool:
    """Can ``symbol`` get an IV history? Not an index (``sec_type`` index, or Massive's
    ``I:`` prefix): Stocks Basic has no index closes to price its options against."""
    if str(sec_type or "").strip().lower() == "index":
        return False
    return not str(symbol or "").strip().upper().startswith("I:")


def history_queue(db, *, now=None, symbols=None, limit: int = 50) -> list[str]:
    """Underlyings still without their IV history whose retry time has come, most option
    volume (call + put) first; ``symbols`` (the pass symbols) limits the list. Index
    underlyings are left out (``history_applies``)."""
    stamp = _now(now)
    keep = set(symbols) if symbols is not None else None
    rows = (db.query(U.symbol, U.call_vol, U.put_vol, U.history_next, U.sec_type)
              .filter(U.history_done.is_(False)).all())
    out = []
    for sym, cv, pv, nxt, st in rows:
        if keep is not None and sym not in keep:
            continue
        if not history_applies(sym, st):
            continue
        if nxt is not None and nxt > stamp:
            continue
        out.append((-((cv or 0) + (pv or 0)), sym))
    out.sort()
    return [s for _, s in out[:max(0, int(limit))]]


def history_backoff_s(tries: int) -> float:
    """The wait after the ``tries``-th failed history: 30 min doubling to 24 h."""
    n = max(1, int(tries))
    return min(HISTORY_RETRY_S * 2 ** (n - 1), HISTORY_RETRY_MAX_S)


def mark_history(db, symbol, *, done: bool, now=None) -> dict:
    """Record one IV-history attempt: done -> ``history_done`` True, tries reset; not done
    -> ``history_tries`` + 1 and ``history_next`` = now + ``history_backoff_s``. Commits;
    returns ``{"done", "tries", "next"}``."""
    sym = _sym(symbol)
    stamp = _now(now)

    def apply() -> dict:
        u = _und(db, sym, create=True, now=stamp)
        if done:
            u.history_done, u.history_tries, u.history_next = True, 0, None
        else:
            u.history_done = False
            u.history_tries = int(u.history_tries or 0) + 1
            u.history_next = stamp + _dt.timedelta(seconds=history_backoff_s(u.history_tries))
        u.updated_at = stamp
        db.flush()
        return {"done": bool(u.history_done), "tries": u.history_tries, "next": u.history_next}

    return _txn(db, apply)


def history_not_done(db, symbols=None) -> list[str]:
    """Underlyings that can have an IV history (``history_applies``) and do not have it
    yet, whatever their retry time - sorted. ``symbols`` limits the list."""
    keep = set(symbols) if symbols is not None else None
    rows = db.query(U.symbol, U.sec_type).filter(U.history_done.is_(False)).all()
    return sorted(s for s, st in rows if (keep is None or s in keep) and history_applies(s, st))


def mark_history_many(db, symbols, *, done: bool = True, now=None, chunk: int = 500) -> int:
    """``mark_history`` for many symbols at once (v4.138: a day-file build completes
    thousands of histories together), one commit per ``chunk``; creates missing rows.
    Returns the symbols marked."""
    stamp = _now(now)
    syms = sorted({s for s in (_sym(x) for x in symbols or ()) if s})
    n = 0
    for part in _chunks(syms, max(1, int(chunk))):

        def apply(part=part) -> int:
            have = {u.symbol: u for u in db.query(U).filter(U.symbol.in_(part))}
            for sym in part:
                u = have.get(sym)
                if u is None:
                    u = U(symbol=sym, history_done=False, history_tries=0, updated_at=stamp)
                    db.add(u)
                if done:
                    u.history_done, u.history_tries, u.history_next = True, 0, None
                else:
                    u.history_done = False
                    u.history_tries = int(u.history_tries or 0) + 1
                    u.history_next = stamp + _dt.timedelta(seconds=history_backoff_s(u.history_tries))
                u.updated_at = stamp
            db.flush()
            return len(part)

        n += _txn(db, apply) or 0
    return n


def history_counts(db, symbols=None) -> tuple[int, int]:
    """``(underlyings with their IV history, underlyings that can have one)`` - over
    ``symbols`` when given. Index underlyings are not counted at all (``history_applies``):
    they never get one, so they must not hold the denominator open."""
    rows = db.query(U.symbol, U.history_done, U.sec_type).all()
    keep = set(symbols) if symbols is not None else None
    sel = [bool(h) for s, h, st in rows
           if (keep is None or s in keep) and history_applies(s, st)]
    return sum(sel), len(sel)


# ────────────────────────────────── retention ──────────────────────────────────

def prune(db, *, now=None, today=None, keep_days: int = INACTIVE_KEEP_DAYS) -> dict:
    """Retention: universe symbols inactive more than ``keep_days`` (and every contract,
    daily and underlying row of theirs), contracts already expired (before ``today``, ET),
    daily rows older than ``DAILY_KEEP_DAYS``, pass rows older than ``PASS_KEEP_DAYS``.
    Commits; returns the counts."""
    stamp = _now(now)
    day = _day_str(today) or clock.et_today(stamp)
    cutoff = stamp - _dt.timedelta(days=keep_days)

    def apply() -> dict:
        dead = [s for s, a, seen in db.query(UNI.symbol, UNI.active, UNI.last_seen)
                if not a and (seen is None or seen < cutoff)]
        out = {"symbols": len(dead), "contracts": 0, "daily": 0, "underlyings": 0,
               "expired": 0, "old_daily": 0, "passes": 0}
        for part in _chunks(dead, 500):
            out["contracts"] += db.query(C).filter(C.symbol.in_(part)).delete(synchronize_session=False)
            out["daily"] += db.query(D).filter(D.symbol.in_(part)).delete(synchronize_session=False)
            out["underlyings"] += db.query(U).filter(U.symbol.in_(part)).delete(synchronize_session=False)
            db.query(UNI).filter(UNI.symbol.in_(part)).delete(synchronize_session=False)
        out["expired"] = db.query(C).filter(C.expiry < day).delete(synchronize_session=False)
        old = (_dt.date.fromisoformat(day) - _dt.timedelta(days=DAILY_KEEP_DAYS)).isoformat()
        out["old_daily"] = db.query(D).filter(D.on < old).delete(synchronize_session=False)
        out["passes"] = (db.query(P).filter(P.started < stamp - _dt.timedelta(days=PASS_KEEP_DAYS))
                           .delete(synchronize_session=False))
        db.flush()
        return out

    return _txn(db, apply)


__all__ = [
    "replace_contracts", "delete_contracts", "update_underlying_pass", "recompute_iv",
    "recompute_iv_many", "recompute_technicals", "recompute_technicals_many", "technicals",
    "classify_trend", "file_iv_day", "iv_day_status", "IV_SRC_LIVE", "IV_SRC_REST", "IV_SRC_FLAT",
    "set_identity", "identity_missing", "set_earnings", "upsert_daily", "file_grouped_day", "stock_day_status",
    "daily_counts", "big_moves", "raw_bars", "last_close", "underlying", "upsert_universe",
    "pass_symbols", "universe_info", "start_pass", "finish_pass", "last_pass", "status",
    "set_status", "history_queue", "history_applies", "history_backoff_s", "mark_history",
    "history_counts", "mark_history_many", "history_not_done", "day_closes_raw", "iv_counts",
    "raw_close_counts", "sec_types",
    "prune", "close_time_utc",
]
