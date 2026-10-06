"""ORM reads and writes for the Options module's data tables (OPTIONS_MODULE_DESIGN.md
Part II; Part A §A2.4, A4.6, A5.3, A6.1).

What lives here
---------------
* the chain snapshot (``option_chain_snapshot``): ``replace_snapshot``, ``latest_chain``,
  ``latest_snap_on``;
* the per-day header + vol statistics (``iv_daily``): ``upsert_iv_daily``, ``iv_series``,
  ``bootstrap_iv`` (the member's TWS year of IV), ``backfill_from_iv_history``;
* the card cache (``option_signal``): ``upsert_signal``, ``signal`` and the ONLY two read
  paths the page may use, ``card_for`` and ``basket_rows_for``;
* ``basket_universe`` and ``prune`` (step 5 of every nightly run).

Rules this module enforces so nothing else has to
--------------------------------------------------
* A ``live`` (bridge) chain is never written to the snapshot table.
* Per-contract ``iv`` is a FRACTION; per-day statistics are PERCENT. The IBKR series is
  already percent and is stored AS-IS, bounded ``0.1 <= iv <= 1000``.
* A day the server read itself (``cboe`` / ``alpaca``) is never overwritten by a broker
  series or by the ``iv_history`` copy: both only INSERT missing days.
* Position sizing runs at READ time (``card_for``), never when a row is written: the
  stored ``picks[*].sizing`` is always null, and an account-value edit changes the
  figure on the next read without invalidating a single cached pick.
* ``basket_rows_for`` is the one place that decides ``pick_state``; the route never
  recomputes it.
* ``stale`` is "the snapshot is older than the previous ET trading day", never a
  wall-clock age (weekends and NYSE holidays through ``clock``).

Portable ORM only (the platform data-handling rule): query-then-write upserts, plain
``delete(synchronize_session=False)`` prunes, ``func.abs`` for the thinning rule; nothing
SQLite-specific. The write functions do NOT commit unless they say so - the nightly job
commits once per symbol; ``prune``, ``backfill_from_iv_history`` and ``bootstrap_iv`` are
whole units of work and commit themselves.
"""
from __future__ import annotations

import copy
import datetime as _dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..models import (IVDaily, IVHistory, OptionBasket, OptionChainSnapshot,
                      OptionIdeaPush, OptionJob, OptionSignal, OptionTrade,
                      OptionTradeCheck, _utcnow)
from . import clock

log = logging.getLogger(__name__)

SNAPSHOT_KINDS = ("eod", "intraday")          # a "live" chain is graded in-request only
SIGNAL_STATUSES = ("ok", "no_setup", "no_chain", "no_iv", "stale_iv", "error")
TRENDS = ("up", "down", "sideways", "unclear")

# Retention (II.2.1 / A2.4). The snapshot and full-width windows come from settings.
THIN_DELTA_LO = 0.03         # rows with |delta| below this are thinned after options_full_days
THIN_DELTA_HI = 0.97         # ... and above this
EXPIRED_GRACE_DAYS = 7       # an expired contract keeps its rows for a week (the monitor settles)
CHECKS_KEEP_DAYS = 90        # option_trade_checks
JOBS_KEEP = 180              # option_jobs: the newest N rows
PUSH_KEEP_DAYS = 45          # option_idea_push

IV_SERIES_N = 252            # the rank window, trading days

# The IBKR "Live" bootstrap bounds (A4.6): the series is percent, <= 400 points,
# dates within the last 400 calendar days and not in the future.
BOOTSTRAP_MAX_POINTS = 400
BOOTSTRAP_MAX_AGE_DAYS = 400
BOOTSTRAP_IV_LO = 0.1
BOOTSTRAP_IV_HI = 1000.0

# The contract-row fields a snapshot keeps (ContractRow minus the key / dte).
_ROW_FIELDS = ("bid", "ask", "mid", "last", "bid_size", "ask_size", "iv", "delta",
               "gamma", "theta", "vega", "rho", "theo", "oi", "volume", "prev_close")

# iv_daily statistic columns the engine writes, plus the aliases ``signal.iv`` uses
# for the same figures (A2.1: "column names match the keys of signal.iv").
_IV_FIELDS = ("iv30", "iv30_src", "atm_iv30", "hv20", "hv60", "iv_hv_premium", "iv_rank",
              "iv_pct", "iv_n", "iv_state", "iv_lo", "iv_hi", "iv_by_expiry", "iv_front",
              "iv_back", "term_ratio", "skew25", "skew_norm", "expected_move",
              "earnings_date", "earnings_days", "n_contracts", "n_expiries", "partial")
_IV_ALIASES = {"lo": "iv_lo", "hi": "iv_hi", "n": "iv_n", "state": "iv_state"}


# ────────────────────────────────── small helpers ──────────────────────────────────

def _get(obj, key: str, default=None):
    """Read ``key`` off a dict or an attribute off an object (a Chain, a ContractRow)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _day(d) -> str:
    """An ET date as ``YYYY-MM-DD`` whatever the caller passed."""
    if d is None:
        return clock.et_today()
    if isinstance(d, _dt.datetime):
        return d.date().isoformat()
    if isinstance(d, _dt.date):
        return d.isoformat()
    return str(d)[:10]


def _minus(day: str, days: int) -> str:
    return (_dt.date.fromisoformat(day) - _dt.timedelta(days=days)).isoformat()


def _dte(expiry: str, on: str) -> int:
    return (_dt.date.fromisoformat(expiry) - _dt.date.fromisoformat(on)).days


def _naive_utc(ts) -> _dt.datetime | None:
    """A feed stamp as naive UTC (the ``as_of`` convention): aware input is converted,
    naive input is taken as UTC already, strings are parsed."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return ts


def _age_h(ts) -> float | None:
    """Hours since ``ts``. The column is naive UTC on SQLite and aware on Postgres,
    and ``_utcnow()`` is aware - so both sides are made naive UTC (ivscan's rule)."""
    ts = _naive_utc(ts)
    if ts is None:
        return None
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    return round((now - ts).total_seconds() / 3600.0, 2)


def _stale(day: str | None, now: _dt.datetime | None = None) -> bool:
    """``snap_on`` older than the PREVIOUS ET trading day: at least two sessions behind
    the ET session date (weekends and NYSE holidays stepped over), never a wall-clock
    age. A snapshot of the previous trading day is NOT stale; one session older is."""
    if not day:
        return True
    prev = clock.prev_trading_day(clock.last_trading_day(clock.et_date(now)))
    return str(day)[:10] < prev.isoformat()


def _int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _engine_version() -> str | None:
    """``option_engine.ENGINE_VERSION`` when the engine is importable; None means
    "do not filter by version" (the engine has not landed on this checkout yet)."""
    try:
        from .option_engine import ENGINE_VERSION      # noqa: PLC0415 - lazy by design

        return str(ENGINE_VERSION)
    except Exception:  # noqa: BLE001
        return None


# ────────────────────────────────── the stored chain ──────────────────────────────────

@dataclass(frozen=True)
class StoredRow:
    """A snapshot row read back with the ``option_data.ContractRow`` attribute set, so
    the engines read it the same way whichever object they get."""
    expiry: str
    right: str
    strike: float
    bid: float | None
    ask: float | None
    mid: float | None
    last: float | None
    bid_size: int | None
    ask_size: int | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None
    theo: float | None
    oi: int | None
    volume: int | None
    prev_close: float | None
    dte: int


@dataclass
class StoredChain:
    """The ``option_data.Chain`` attribute set over rows re-read from the DB. Built only
    when ``option_data`` is not importable; otherwise ``latest_chain`` returns a real
    ``Chain`` so every method the engines call exists."""
    symbol: str
    source: str
    kind: str
    as_of: _dt.datetime | None
    snap_on: str
    spot: float | None
    iv30: float | None
    rows: list
    delayed_minutes: int = 15
    header: dict = field(default_factory=dict)
    partial: bool = False
    note: str = ""

    def legs(self) -> dict[tuple, dict]:
        """The legacy ``{(expiry, right, strike): {...}}`` dict ``option_quotes.fetch_chain``
        returns (the key ``open_interest`` survives here and only here)."""
        out = {}
        for r in self.rows:
            out[(r.expiry, r.right, r.strike)] = {
                "bid": r.bid, "ask": r.ask, "mid": r.mid, "last": r.last, "iv": r.iv,
                "delta": r.delta, "gamma": r.gamma, "theta": r.theta, "vega": r.vega,
                "rho": r.rho, "theo": r.theo, "open_interest": r.oi, "volume": r.volume,
            }
        return out


def _row_kwargs(r: OptionChainSnapshot) -> dict:
    return {
        "expiry": r.expiry, "right": r.right, "strike": r.strike, "bid": r.bid, "ask": r.ask,
        "mid": r.mid, "last": r.last, "bid_size": r.bid_size, "ask_size": r.ask_size,
        "iv": r.iv, "delta": r.delta, "gamma": r.gamma, "theta": r.theta, "vega": r.vega,
        "rho": r.rho, "theo": r.theo, "oi": r.oi, "volume": r.volume,
        "prev_close": r.prev_close, "dte": r.dte,
    }


def _build_chain(symbol: str, snap_on: str, kind: str, hdr: IVDaily | None,
                 rows: list[OptionChainSnapshot]):
    source = (hdr.source if hdr is not None else None) or (rows[0].source if rows else "cboe")
    chain_kw = {
        "symbol": symbol, "source": source, "kind": kind, "snap_on": snap_on,
        "as_of": hdr.as_of if hdr is not None else None,
        "spot": hdr.spot if hdr is not None else None,
        "iv30": hdr.iv30 if (hdr is not None and hdr.iv30_src in (None, "cboe")) else None,
        "delayed_minutes": 15, "header": {},
        "partial": bool(hdr.partial) if hdr is not None else False,
        "note": "stored snapshot",
    }
    try:
        from .option_data import Chain, ContractRow      # noqa: PLC0415 - lazy by design

        return Chain(rows=[ContractRow(**_row_kwargs(r)) for r in rows], **chain_kw)
    except Exception:  # noqa: BLE001 - not landed yet, or a different constructor
        return StoredChain(rows=[StoredRow(**_row_kwargs(r)) for r in rows], **chain_kw)


# ────────────────────────────────── snapshot ──────────────────────────────────

def replace_snapshot(db, chain) -> int:
    """Store every contract row of ``chain`` (a ``Chain``, or a dict with the same keys)
    under ``(symbol, snap_on, kind)``, replacing what was there.

    ``kind`` must be ``eod`` or ``intraday``: a live chain is refused. An intraday write
    first drops the symbol's OLDER intraday rows too ("only the latest intraday"). Rows
    without an expiry / right / positive strike are skipped; ``dte`` is computed from
    ``snap_on`` when the row carries none; ``mid`` from bid and ask when absent. Does
    not commit. Returns the number of rows written.
    """
    symbol = str(_get(chain, "symbol") or "").strip().upper()
    snap_on = _day(_get(chain, "snap_on"))
    kind = str(_get(chain, "kind") or "eod")
    source = str(_get(chain, "source") or "cboe")
    if not symbol:
        raise ValueError("replace_snapshot: the chain has no symbol")
    if kind not in SNAPSHOT_KINDS:
        raise ValueError("replace_snapshot: a %r chain is never written to the snapshot table"
                         % kind)

    q = db.query(OptionChainSnapshot).filter(OptionChainSnapshot.symbol == symbol)
    if kind == "intraday":
        q = q.filter(OptionChainSnapshot.kind == "intraday")
    else:
        q = q.filter(OptionChainSnapshot.snap_on == snap_on, OptionChainSnapshot.kind == kind)
    q.delete(synchronize_session=False)

    out: list[OptionChainSnapshot] = []
    for r in _get(chain, "rows") or []:
        expiry = _get(r, "expiry")
        right = str(_get(r, "right") or "").strip().upper()[:1]
        strike = _float(_get(r, "strike"))
        if not expiry or right not in ("C", "P") or strike is None or strike <= 0:
            continue
        expiry = str(expiry)[:10]
        dte = _int(_get(r, "dte"))
        if dte is None:
            dte = _dte(expiry, snap_on)
        bid, ask, mid = _float(_get(r, "bid")), _float(_get(r, "ask")), _float(_get(r, "mid"))
        if mid is None and bid is not None and ask is not None:
            mid = round((bid + ask) / 2.0, 4)
        out.append(OptionChainSnapshot(
            symbol=symbol, snap_on=snap_on, kind=kind, source=source,
            expiry=expiry, dte=dte, right=right, strike=strike,
            bid=bid, ask=ask, mid=mid, last=_float(_get(r, "last")),
            bid_size=_int(_get(r, "bid_size")), ask_size=_int(_get(r, "ask_size")),
            iv=_float(_get(r, "iv")), delta=_float(_get(r, "delta")),
            gamma=_float(_get(r, "gamma")), theta=_float(_get(r, "theta")),
            vega=_float(_get(r, "vega")), rho=_float(_get(r, "rho")),
            theo=_float(_get(r, "theo")), oi=_int(_get(r, "oi")),
            volume=_int(_get(r, "volume")), prev_close=_float(_get(r, "prev_close")),
        ))
    db.add_all(out)
    db.flush()
    return len(out)


def latest_snap_on(db, symbol: str) -> tuple[str, str, _dt.datetime | None] | None:
    """``(snap_on, kind, as_of)`` of the newest eod / intraday snapshot on file for
    ``symbol`` - from the ``iv_daily`` header, else from the snapshot rows themselves
    (``as_of`` None then). None when nothing is stored."""
    symbol = symbol.strip().upper()
    hdr = (db.query(IVDaily)
             .filter(IVDaily.symbol == symbol, IVDaily.kind.in_(SNAPSHOT_KINDS))
             .order_by(IVDaily.on.desc())
             .first())
    if hdr is not None:
        return hdr.on, hdr.kind, hdr.as_of
    row = (db.query(OptionChainSnapshot.snap_on, OptionChainSnapshot.kind)
             .filter(OptionChainSnapshot.symbol == symbol)
             .order_by(OptionChainSnapshot.snap_on.desc(), OptionChainSnapshot.kind.asc())
             .first())
    if row is None:
        return None
    return row[0], row[1], None


def _header(db, symbol: str, on: str) -> IVDaily | None:
    return (db.query(IVDaily)
              .filter(IVDaily.symbol == symbol, IVDaily.on == on)
              .one_or_none())


def latest_chain(db, symbol: str, *, snap_on: str | None = None, kind: str | None = None):
    """The stored chain for ``symbol`` (the newest day by default) as an
    ``option_data.Chain`` - or, until that module lands, a ``StoredChain`` with the same
    attributes. ~3k rows, < 50 ms, no market call. None when nothing is stored.

    When the header says ``eod`` but only ``intraday`` rows exist for that day (or the
    reverse) the rows that DO exist are returned and ``chain.kind`` says which."""
    symbol = symbol.strip().upper()
    if snap_on is None:
        hdr = latest_snap_on(db, symbol)
        if hdr is None:
            return None
        snap_on, kind, _as_of = hdr
    snap_on = _day(snap_on)

    def _rows(k):
        q = (db.query(OptionChainSnapshot)
               .filter(OptionChainSnapshot.symbol == symbol,
                       OptionChainSnapshot.snap_on == snap_on))
        if k:
            q = q.filter(OptionChainSnapshot.kind == k)
        return (q.order_by(OptionChainSnapshot.expiry, OptionChainSnapshot.right,
                           OptionChainSnapshot.strike)
                 .all())

    rows = _rows(kind)
    if not rows and kind:
        rows = _rows(None)
    if not rows:
        return None
    kind = rows[0].kind if (not kind or rows[0].kind != kind) else kind
    return _build_chain(symbol, snap_on, kind, _header(db, symbol, snap_on), rows)


# ────────────────────────────────── iv_daily ──────────────────────────────────

def upsert_iv_daily(db, chain, metrics: dict | None = None) -> IVDaily:
    """One row per (symbol, ET day): the chain header from ``chain`` (a ``Chain`` or a
    dict: symbol, snap_on, kind, source, as_of, spot, iv30, partial, rows) plus every
    statistic in ``metrics`` whose key is an ``iv_daily`` column (``option_metrics.all_for``
    returns them under the column names; the ``signal.iv`` aliases ``lo / hi / n / state``
    are accepted too). Query-then-write; the newest write wins, so an EOD run after an
    intraday refresh leaves the day row ``kind='eod'``. Does not commit."""
    metrics = dict(metrics or {})
    symbol = str(_get(chain, "symbol") or "").strip().upper()
    on = _day(_get(chain, "snap_on") or metrics.get("on"))
    if not symbol:
        raise ValueError("upsert_iv_daily: the chain has no symbol")
    row = _header(db, symbol, on)
    if row is None:
        row = IVDaily(symbol=symbol, on=on)
        db.add(row)
    row.kind = str(_get(chain, "kind") or "eod")
    row.source = str(_get(chain, "source") or "cboe")
    row.as_of = _naive_utc(_get(chain, "as_of"))
    spot = _float(_get(chain, "spot"))
    if spot is not None:
        row.spot = spot
    row.partial = bool(_get(chain, "partial") or False)
    rows = _get(chain, "rows")
    if rows is not None:
        row.n_contracts = len(rows)
        row.n_expiries = len({_get(r, "expiry") for r in rows})

    # The source's own iv30 (Cboe) is the default series value; the engine may override
    # it (and say so in iv30_src) when it computed an ATM figure instead.
    feed_iv30 = _float(_get(chain, "iv30"))
    if feed_iv30 is not None and "iv30" not in metrics:
        row.iv30 = feed_iv30
        row.iv30_src = metrics.get("iv30_src") or ("cboe" if row.source == "cboe" else row.source[:8])
    for k, v in metrics.items():
        col = _IV_ALIASES.get(k, k)
        if col in _IV_FIELDS:
            setattr(row, col, v)
    if row.iv30 is not None and row.iv30_src is None:
        row.iv30_src = "atm" if metrics.get("atm_iv30") == row.iv30 else row.source[:8]
    row.updated_at = _utcnow()
    db.flush()
    return row


def iv_series_rows(db, symbol: str, n: int = IV_SERIES_N, *,
                   until: str | None = None) -> list[tuple[str, float]]:
    """The newest ``n`` ``(on, iv30)`` pairs for ``symbol`` with ``on <= until`` (today
    included when ``until`` is today), OLDEST FIRST, percent, rows without iv30 skipped."""
    symbol = symbol.strip().upper()
    q = (db.query(IVDaily.on, IVDaily.iv30)
           .filter(IVDaily.symbol == symbol, IVDaily.iv30.isnot(None)))
    if until:
        q = q.filter(IVDaily.on <= _day(until))
    rows = q.order_by(IVDaily.on.desc()).limit(int(n)).all()
    return [(on, float(v)) for on, v in reversed(rows)]


def iv_series(db, symbol: str, n: int = IV_SERIES_N, *, until: str | None = None) -> list[float]:
    """The iv30 series (percent, oldest first, up to ``n`` values) the rank is computed
    on - the ``series`` argument of ``option_metrics.iv_rank_pct``. Use
    ``iv_series_rows`` when the dates are needed too."""
    return [v for _on, v in iv_series_rows(db, symbol, n, until=until)]


def backfill_from_iv_history(db, *, symbols=None) -> int:
    """Copy ``iv_history`` into ``iv_daily`` for every (symbol, day) not yet there:
    ``kind='history'``, ``source='iv_history'``, ``iv30`` as stored (both percent),
    ``iv30_src`` ``cboe`` / ``ibkr`` by the old row's source, ``spot`` carried. Idempotent -
    a second run inserts nothing. One query per symbol; commits. Returns the count."""
    q = db.query(IVHistory)
    if symbols:
        q = q.filter(IVHistory.symbol.in_([s.strip().upper() for s in symbols]))
    hist = q.order_by(IVHistory.symbol, IVHistory.on).all()
    if not hist:
        return 0
    inserted = 0
    by_symbol: dict[str, list] = {}
    for h in hist:
        by_symbol.setdefault(h.symbol, []).append(h)
    # One commit PER SYMBOL: the copy now also runs from a web thread and from the
    # nightly (option_backfill), so two writers can race on the same days; a unique-
    # day collision then costs that one symbol's batch, never every other symbol's.
    for sym, rows in by_symbol.items():
        existing = {on for (on,) in db.query(IVDaily.on).filter(IVDaily.symbol == sym)}
        n = 0
        for h in rows:
            if h.on in existing or h.iv30 is None:
                continue
            existing.add(h.on)
            db.add(IVDaily(symbol=h.symbol, on=h.on, kind="history", source="iv_history",
                           iv30=float(h.iv30), iv30_src=("cboe" if h.source == "cboe" else "ibkr"),
                           spot=h.spot))
            n += 1
        if not n:
            continue
        try:
            db.commit()
        except IntegrityError:            # another writer copied the same days meanwhile: theirs stand
            db.rollback()
            log.warning("backfill_from_iv_history %s: a concurrent copy won; %d row(s) skipped", sym, n)
            continue
        inserted += n
    return inserted


def _recompute_rank(db, symbol: str, today: str) -> dict | None:
    """Today's ``iv_rank / iv_pct / iv_n / iv_state / iv_lo / iv_hi`` from the now-full
    window, through ``option_metrics.iv_rank_pct`` (the ONE home of that formula).
    None when today has no iv30 row, or the metrics module has not landed yet."""
    row = _header(db, symbol, today)
    if row is None or row.iv30 is None:
        return None
    try:
        from .option_metrics import iv_rank_pct      # noqa: PLC0415 - lazy by design
    except Exception:  # noqa: BLE001
        log.info("bootstrap_iv: option_metrics.iv_rank_pct not available; rank not recomputed")
        return None
    series = iv_series(db, symbol, IV_SERIES_N, until=today)
    out = iv_rank_pct(series, float(row.iv30))
    if not isinstance(out, dict):
        return None
    row.iv_rank = out.get("iv_rank")
    row.iv_pct = out.get("iv_pct")
    row.iv_n = out.get("n", out.get("iv_n"))
    row.iv_state = out.get("state", out.get("iv_state"))
    if out.get("lo") is not None:
        row.iv_lo = out.get("lo")
    if out.get("hi") is not None:
        row.iv_hi = out.get("hi")
    row.updated_at = _utcnow()
    return out


def rank_after_history(db, symbol: str, today=None) -> dict:
    """After history rows landed for ``symbol`` from ANY source (the Live bootstrap, the
    screener copy, the IB Gateway seed - services/option_backfill): today's rank from
    the now-full window, and the symbol's signal rows on the latest snapshot marked
    ``stale_iv`` so the next card read recomputes the gauge. Flushes, does not commit.
    Returns ``{rank: dict|None, stale_marked: int}``."""
    symbol = str(symbol or "").strip().upper()
    today = _day(today)
    rank = None
    try:
        rank = _recompute_rank(db, symbol, today)
    except Exception as e:  # noqa: BLE001 - the rows are the point; the rank can wait
        log.warning("rank_after_history %s: rank recompute failed: %s", symbol, e)
    stale_marked = 0
    latest = latest_snap_on(db, symbol)
    if latest is not None:
        stale_marked = (db.query(OptionSignal)
                          .filter(OptionSignal.symbol == symbol,
                                  OptionSignal.snap_on == latest[0])
                          .update({OptionSignal.status: "stale_iv"},
                                  synchronize_session=False))
    return {"rank": rank, "stale_marked": int(stale_marked or 0)}


def bootstrap_iv(db, symbol: str, series, *, source: str = "ibkr",
                 today: str | None = None) -> dict:
    """File a broker's daily IV series (percent, ``[{"on": "YYYY-MM-DD", "iv": 31.2}, ...]``
    or ``(on, iv)`` pairs) into ``iv_daily`` as ``kind='history'`` rows - the member's TWS
    year of history behind the IV rank (A4.6).

    Bounded input: more than 400 points is refused outright (``ValueError``); a point is
    rejected (counted, not stored) when its date is in the future or more than 400 days
    old, or its value is outside ``0.1 <= iv <= 1000`` (the series is already percent and
    is stored AS-IS). A day that already has a row - one the server read itself, or an
    earlier bootstrap - is never overwritten. Afterwards today's rank is recomputed from
    the full window, every ``option_signal`` row for the symbol on the latest snapshot day
    is marked ``stale_iv`` (the next card read recomputes the gauge), and an
    ``option_jobs(job='bootstrap')`` row is written. Commits.

    Returns ``{inserted, skipped, rejected, n_total, iv_rank, iv_pct, iv_n, state,
    stale_marked}``.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("bootstrap_iv: no symbol")
    if series is None or isinstance(series, (str, bytes, dict)):
        raise ValueError("bootstrap_iv: series must be a list of points")
    series = list(series)
    if len(series) > BOOTSTRAP_MAX_POINTS:
        raise ValueError("bootstrap_iv: %d points, the limit is %d"
                         % (len(series), BOOTSTRAP_MAX_POINTS))
    today = _day(today)
    today_d = _dt.date.fromisoformat(today)
    floor_d = today_d - _dt.timedelta(days=BOOTSTRAP_MAX_AGE_DAYS)

    existing = {on for (on,) in db.query(IVDaily.on).filter(IVDaily.symbol == symbol)}
    inserted = skipped = rejected = 0
    seen: set[str] = set()
    for p in series:
        if isinstance(p, dict):
            on, iv = p.get("on", p.get("date")), p.get("iv", p.get("iv30"))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            on, iv = p[0], p[1]
        else:
            rejected += 1
            continue
        try:
            d = _dt.date.fromisoformat(str(on)[:10])
            v = float(iv)
        except (TypeError, ValueError):
            rejected += 1
            continue
        if v != v or not (BOOTSTRAP_IV_LO <= v <= BOOTSTRAP_IV_HI) or not (floor_d <= d <= today_d):
            rejected += 1
            continue
        key = d.isoformat()
        if key in existing or key in seen:
            skipped += 1
            continue
        seen.add(key)
        db.add(IVDaily(symbol=symbol, on=key, kind="history", source=source[:12],
                       iv30=v, iv30_src=("ibkr" if source == "ibkr" else source[:8])))
        inserted += 1
    db.flush()

    after = rank_after_history(db, symbol, today)
    rank, stale_marked = after["rank"], after["stale_marked"]

    from . import job_runs      # noqa: PLC0415 - sibling import kept local (it imports models)

    run = job_runs.start(db, "bootstrap", today, source=source)       # commits the rows above too
    detail = {symbol: {"status": "ok", "inserted": inserted, "skipped": skipped,
                       "rejected": rejected, "stale_marked": stale_marked}}
    job_runs.finish(db, run, ok=1, rows=inserted, symbols=1, detail=detail,
                    note="%s: %d new days, %d already on file, %d rejected"
                         % (symbol, inserted, skipped, rejected))
    return {
        "inserted": inserted, "skipped": skipped, "rejected": rejected,
        "n_total": len(existing) + inserted,
        "iv_rank": rank.get("iv_rank") if rank else None,
        "iv_pct": rank.get("iv_pct") if rank else None,
        "iv_n": (rank.get("n", rank.get("iv_n")) if rank else None),
        "state": (rank.get("state", rank.get("iv_state")) if rank else None),
        "stale_marked": stale_marked,
    }


# ────────────────────────────────── option_signal ──────────────────────────────────

def _trend_of(sig: dict) -> str:
    """The card's trend STRING: ``sideways`` iff ``setup.rng.sideways``; otherwise the
    engine's own ``trend`` / the setup's, else by the setup direction, else ``unclear``."""
    setup = sig.get("setup") if isinstance(sig.get("setup"), dict) else {}
    rng = setup.get("rng") if isinstance(setup.get("rng"), dict) else {}
    if rng.get("sideways"):
        return "sideways"
    for cand in (sig.get("trend"), setup.get("trend")):
        if cand in TRENDS:
            return cand
    direction = setup.get("direction")
    if direction in ("long", "up"):
        return "up"
    if direction in ("short", "down"):
        return "down"
    return "unclear"


def upsert_signal(db, sig: dict, *, prefs_hash: str, symbol: str | None = None,
                  snap_on: str | None = None, kind: str | None = None,
                  as_of=None, user_id: int | None = None) -> OptionSignal:
    """Write one card row for ``(symbol, snap_on, kind, prefs_hash)`` from
    ``option_engine.compute``'s dict (``status, headline, setup, iv, strategies, picks,
    computed_ms, engine_version``). The key fields come from the keyword arguments, or
    from ``sig`` itself when the caller put ``symbol / snap_on / kind / as_of`` on it.
    Query-then-write; does not commit. ``picks[*].sizing`` is stored as it comes (null)."""
    symbol = str(symbol or sig.get("symbol") or "").strip().upper()
    snap_on = _day(snap_on or sig.get("snap_on"))
    kind = str(kind or sig.get("kind") or "eod")
    if not symbol:
        raise ValueError("upsert_signal: no symbol")
    if not prefs_hash:
        raise ValueError("upsert_signal: no prefs_hash")
    row = (db.query(OptionSignal)
             .filter(OptionSignal.symbol == symbol, OptionSignal.snap_on == snap_on,
                     OptionSignal.kind == kind, OptionSignal.prefs_hash == prefs_hash)
             .one_or_none())
    if row is None:
        row = OptionSignal(symbol=symbol, snap_on=snap_on, kind=kind, prefs_hash=prefs_hash)
        db.add(row)
    status = sig.get("status") or "ok"
    row.status = status if status in SIGNAL_STATUSES else "error"
    row.engine_version = str(sig.get("engine_version") or _engine_version() or "0")[:12]
    row.as_of = _naive_utc(as_of if as_of is not None else sig.get("as_of"))
    row.user_id = user_id
    row.trend = _trend_of(sig)
    row.headline = sig.get("headline")
    row.setup = sig.get("setup")
    row.iv = sig.get("iv")
    row.strategies = sig.get("strategies")
    row.picks = sig.get("picks")
    row.error = sig.get("error")
    row.computed_ms = _int(sig.get("computed_ms"))
    row.created_at = _utcnow()
    db.flush()
    return row


def signal(db, symbol: str, snap_on: str, prefs_hash: str, *, kind: str | None = None,
           engine_version: str | None = None) -> OptionSignal | None:
    """The stored row for ``(symbol, snap_on, [kind,] prefs_hash)``; with
    ``engine_version`` given, a row of another version counts as missing. Newest first
    when both kinds exist for the day."""
    q = (db.query(OptionSignal)
           .filter(OptionSignal.symbol == symbol.strip().upper(),
                   OptionSignal.snap_on == _day(snap_on),
                   OptionSignal.prefs_hash == prefs_hash))
    if kind:
        q = q.filter(OptionSignal.kind == kind)
    if engine_version:
        q = q.filter(OptionSignal.engine_version == engine_version)
    return q.order_by(OptionSignal.as_of.desc(), OptionSignal.id.desc()).first()


# ────────────────────────────────── universe + prune ──────────────────────────────────

def basket_universe(db) -> list[str]:
    """Distinct ACTIVE basket symbols over all owners, plus every symbol with an OPEN
    ``option_trades`` row (a tracked position's chain is always fresh). Sorted."""
    syms = {s for (s,) in db.query(OptionBasket.symbol)
                             .filter(OptionBasket.active.is_(True)).distinct()}
    syms |= {s for (s,) in db.query(OptionTrade.symbol)
                             .filter(OptionTrade.status == "open").distinct()}
    return sorted(s for s in syms if s)


def prune(db, today=None) -> dict:
    """Step 5 of every nightly run (II.2.1 / A2.4). Deletes, in this order:
    snapshots older than ``options_snapshot_days``; rows older than ``options_full_days``
    with ``delta`` None / |delta| < 0.03 / > 0.97 (expiries are never thinned); intraday
    rows of past days; contracts expired more than 7 days; ``option_signal`` rows older
    than the snapshot window; ``option_trade_checks`` older than 90 days (the trade row
    itself is never pruned); all but the newest 180 ``option_jobs``; ``option_idea_push``
    older than 45 days. ``iv_daily`` is never pruned. Commits; returns the counts."""
    today = _day(today)
    snap_days = int(settings.options_snapshot_days or 90)
    full_days = int(settings.options_full_days or 7)
    out: dict[str, int] = {}

    # "90 days" means the newest 90 snapshot days INCLUDING today, so the cut sits
    # at today - 89: 100 synthetic days prune to exactly 90 (A8).
    oldest_kept = _minus(today, max(snap_days - 1, 0))
    S = OptionChainSnapshot
    out["snapshots_old"] = (db.query(S).filter(S.snap_on < oldest_kept)
                              .delete(synchronize_session=False))
    out["snapshots_thinned"] = (
        db.query(S)
          .filter(S.snap_on < _minus(today, full_days),
                  or_(S.delta.is_(None),
                      func.abs(S.delta) < THIN_DELTA_LO,
                      func.abs(S.delta) > THIN_DELTA_HI))
          .delete(synchronize_session=False))
    out["snapshots_intraday"] = (db.query(S).filter(S.kind == "intraday", S.snap_on < today)
                                   .delete(synchronize_session=False))
    out["snapshots_expired"] = (db.query(S).filter(S.expiry < _minus(today, EXPIRED_GRACE_DAYS))
                                  .delete(synchronize_session=False))
    out["signals"] = (db.query(OptionSignal)
                        .filter(OptionSignal.snap_on < oldest_kept)
                        .delete(synchronize_session=False))
    out["trade_checks"] = (db.query(OptionTradeCheck)
                             .filter(OptionTradeCheck.checked_on < _minus(today, CHECKS_KEEP_DAYS))
                             .delete(synchronize_session=False))
    old_jobs = [i for (i,) in db.query(OptionJob.id).order_by(OptionJob.id.desc())
                                .offset(JOBS_KEEP).all()]
    out["jobs"] = 0
    for i in range(0, len(old_jobs), 500):
        out["jobs"] += (db.query(OptionJob).filter(OptionJob.id.in_(old_jobs[i:i + 500]))
                          .delete(synchronize_session=False))
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=PUSH_KEEP_DAYS)).replace(tzinfo=None)
    out["idea_push"] = (db.query(OptionIdeaPush).filter(OptionIdeaPush.sent_at < cutoff)
                          .delete(synchronize_session=False))
    db.commit()
    return out


# ────────────────────────────────── the two read paths ──────────────────────────────────

def _prefs_for(db, user, prefs, prefs_hash, house_hash) -> tuple[dict, str, str | None]:
    """The member's merged prefs and the two hashes. Callers may pass them (the tests,
    a route that already read them); otherwise ``option_prefs`` supplies them."""
    if prefs is None or prefs_hash is None or house_hash is None:
        try:
            from . import option_prefs      # noqa: PLC0415 - lazy by design
        except ImportError as e:
            raise ImportError("option_store needs app.services.option_prefs for the member's "
                              "rules (read / prefs_hash / HOUSE_HASH): %s" % e) from e
        if prefs is None:
            prefs = option_prefs.read(db, user)
        if prefs_hash is None:
            prefs_hash = option_prefs.prefs_hash(prefs)
        if house_hash is None:
            house_hash = getattr(option_prefs, "HOUSE_HASH", None)
    return prefs, prefs_hash, house_hash


def _account_for(prefs: dict, user) -> dict:
    """``{nlv, risk_pct, nlv_source}``: ``option_prefs.read()``'s own ``account`` key when
    present, else the stored ``trade_prefs`` figures (``nlv_source='prefs'`` when > 0)."""
    acct = prefs.get("account") if isinstance(prefs, dict) else None
    if isinstance(acct, dict) and "nlv" in acct:
        return dict(acct)
    try:
        from . import trade_prefs      # noqa: PLC0415

        tp = trade_prefs.read(user) if user is not None else {}
    except Exception:  # noqa: BLE001
        tp = {}
    nlv = _float(tp.get("nlv")) or 0.0
    return {"nlv": nlv if nlv > 0 else None,
            "risk_pct": _float(tp.get("risk_pct")) or 1.0,
            "nlv_source": "prefs" if nlv > 0 else None}


def _fill_sizing(card: dict, prefs: dict, user) -> None:
    """Read-time sizing: ``option_sizing.size(pick, nlv, prefs)`` on every real pick
    (stubs with ``status`` nearest / none keep ``sizing`` null). The module is imported
    lazily; when it is not there the card still renders, with ``sizing_error`` saying so."""
    account = _account_for(prefs, user)
    prefs2 = dict(prefs) if isinstance(prefs, dict) else {}
    prefs2.setdefault("account", account)
    nlv = account.get("nlv")
    nlv = float(nlv) if nlv else None
    try:
        from . import option_sizing      # noqa: PLC0415 - lazy by design
    except ImportError as e:
        msg = ("Position sizing is not available: app.services.option_sizing could not be "
               "imported (%s)." % e)
        log.warning(msg)
        card["sizing_error"] = msg
        card["account"] = account
        return
    card["account"] = account
    for lst in (card.get("picks") or {}).values():
        if not isinstance(lst, list):
            continue
        for p in lst:
            if not isinstance(p, dict) or p.get("status", "ok") != "ok":
                continue
            try:
                p["sizing"] = option_sizing.size(p, nlv, prefs2)
            except Exception as e:  # noqa: BLE001 - one bad pick must not blank the card
                log.warning("option_sizing.size failed on %s: %s", p.get("strategy"), e)
                p["sizing"] = None
                card["sizing_error"] = "Position sizing failed on one pick: %s" % e


def _row_dict(row: OptionSignal) -> dict:
    """The row as a dict, with the JSON blobs COPIED so read-time fills never mark the
    ORM instance dirty (the stored row keeps ``sizing`` null)."""
    return {
        "id": row.id, "symbol": row.symbol, "snap_on": row.snap_on, "kind": row.kind,
        "as_of": row.as_of, "prefs_hash": row.prefs_hash, "engine_version": row.engine_version,
        "status": row.status, "trend": row.trend, "headline": row.headline,
        "setup": copy.deepcopy(row.setup), "iv": copy.deepcopy(row.iv),
        "strategies": copy.deepcopy(row.strategies), "picks": copy.deepcopy(row.picks),
        "error": row.error, "computed_ms": row.computed_ms,
    }


def _recommended(strategies) -> str | None:
    for s in strategies or []:
        if isinstance(s, dict) and s.get("fit") == "recommended":
            return s.get("key")
    return None


def _has_real_picks(picks, key: str | None) -> bool:
    """A pick list counts only when it holds a REAL pick: the stub the picker stores when
    nothing passes (``status`` nearest / none) is not one."""
    if not key or not isinstance(picks, dict):
        return False
    lst = picks.get(key) or []
    return any(isinstance(p, dict) and p.get("status", "ok") == "ok" for p in lst)


def _lazy_compute(db, symbol: str, day: str, kind: str, as_of, prefs: dict,
                  prefs_hash: str, user) -> OptionSignal | None:
    """A5.3's hash-miss / ``stale_iv`` path: the engines over the STORED chain (no option
    market call). Every module it needs is imported lazily; when one is missing, or the
    compute fails, None is returned and the caller serves what it has."""
    try:
        from . import chart_state, ema_setup, option_engine, option_metrics, prices  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 - not landed on this checkout
        log.info("card_for %s: lazy compute unavailable (%s)", symbol, e)
        return None
    try:
        chain = latest_chain(db, symbol, snap_on=day, kind=kind)
        if chain is None:
            return None
        long_bars = prices.fetch_daily_ohlc(symbol, rng=getattr(ema_setup, "DEEP_RANGE", "10y"))
        bars = ema_setup._last_two_years(long_bars) if long_bars else []
        earnings = prices.fetch_next_earnings(symbol)
        metrics = option_metrics.all_for(chain, bars, earnings,
                                         iv_series=iv_series(db, symbol, IV_SERIES_N, until=day))
        expiries = sorted({r.expiry for r in chain.rows})
        state = chart_state.read(symbol, bars=bars, long_bars=long_bars, today=day,
                                 expiries=expiries)
        sig = option_engine.compute(chain, metrics, state, prefs)
        row = upsert_signal(db, sig, prefs_hash=prefs_hash, symbol=symbol, snap_on=day,
                            kind=kind, as_of=as_of, user_id=getattr(user, "id", None))
        db.commit()
        return row
    except Exception as e:  # noqa: BLE001 - a failed recompute must not blank the page
        log.warning("card_for %s: lazy compute failed: %s", symbol, e, exc_info=True)
        db.rollback()
        return None


def card_for(db, symbol: str, user, *, prefs: dict | None = None,
             prefs_hash: str | None = None, house_hash: str | None = None) -> dict | None:
    """THE card read (A5.3): the latest row for the member's hash on the newest snapshot
    day - lazily computed from the stored chain on a hash miss or ``status='stale_iv'``
    (no market call) - else the house row standing in. None when no signal row exists
    for the symbol at all.

    Adds ``stale`` (snapshot older than the previous ET trading day), ``age_h`` (hours
    since the feed stamp), ``kind``, ``as_of``, ``source``, ``own_rules`` (the row is the
    member's hash, not the house stand-in) and fills ``picks[*].sizing`` through
    ``option_sizing.size`` at read time - 0 contracts is a valid answer there; nothing
    is written. ``prefs`` / ``prefs_hash`` / ``house_hash`` may be supplied by a caller
    that already read them; otherwise ``option_prefs`` is asked."""
    symbol = symbol.strip().upper()
    hdr = latest_snap_on(db, symbol)
    if hdr is None:
        return None
    day, kind, as_of = hdr
    prefs, phash, hhash = _prefs_for(db, user, prefs, prefs_hash, house_hash)
    ev = _engine_version()

    row = signal(db, symbol, day, phash, kind=kind, engine_version=ev)
    if row is None or row.status == "stale_iv":
        fresh = _lazy_compute(db, symbol, day, kind, as_of, prefs, phash, user)
        if fresh is not None:
            row = fresh
    if row is None and hhash and hhash != phash:
        row = signal(db, symbol, day, hhash, kind=kind, engine_version=ev)
    if row is None:
        return None

    header = _header(db, symbol, day)
    card = _row_dict(row)
    card["stale"] = _stale(day)
    card["age_h"] = _age_h(as_of or row.as_of)
    card["kind"] = kind
    card["as_of"] = as_of or row.as_of
    card["source"] = header.source if header is not None else None
    card["own_rules"] = (row.prefs_hash == phash)
    card["recommended"] = _recommended(card.get("strategies"))
    _fill_sizing(card, prefs, user)
    return card


def basket_rows_for(db, user, *, prefs: dict | None = None, prefs_hash: str | None = None,
                    house_hash: str | None = None) -> dict[str, dict]:
    """The basket column's data in ONE batched query, keyed by symbol (A5.3): for every
    ACTIVE symbol in the member's basket, the row on the newest ``snap_on`` for the
    member's hash, else the house hash. Per symbol::

        {symbol, trend, iv: {iv_rank, basis, iv_n}, idea, pick_state, stale, age_h,
         snap_on, status, headline}

    THIS function decides ``pick_state``: ``has_picks`` (a row for the member's hash on
    the latest day with a real pick under the recommended strategy), ``no_strike_passes``
    (that row exists but holds no real pick), ``not_checked`` (no row for the member's
    hash on the latest day - rules saved after the nightly run; ``trend`` / ``iv`` come
    from the house row). A symbol with no row at all is ``not_checked`` with ``stale``
    True. The route never recomputes any of this."""
    if user is None:
        return {}
    basket = (db.query(OptionBasket.symbol)
                .filter(OptionBasket.owner_key == "u%d" % user.id,
                        OptionBasket.active.is_(True))
                .order_by(OptionBasket.pos, OptionBasket.id)
                .all())
    symbols = []
    for (s,) in basket:
        if s and s not in symbols:
            symbols.append(s)
    if not symbols:
        return {}
    prefs, phash, hhash = _prefs_for(db, user, prefs, prefs_hash, house_hash)
    hashes = [h for h in {phash, hhash} if h]
    ev = _engine_version()

    S = OptionSignal
    conds = [S.symbol.in_(symbols), S.prefs_hash.in_(hashes)]
    if ev:
        conds.append(S.engine_version == ev)
    newest = (select(S.symbol.label("symbol"), func.max(S.snap_on).label("day"))
              .where(and_(*conds)).group_by(S.symbol).subquery())
    stmt = (select(S)
            .join(newest, and_(S.symbol == newest.c.symbol, S.snap_on == newest.c.day))
            .where(and_(*conds)))
    by_sym: dict[str, dict[str, OptionSignal]] = {}
    for r in db.execute(stmt).scalars().all():
        slot = by_sym.setdefault(r.symbol, {})
        prev = slot.get(r.prefs_hash)
        if prev is None or (r.as_of or _dt.datetime.min, r.id) > (prev.as_of or _dt.datetime.min, prev.id):
            slot[r.prefs_hash] = r

    out: dict[str, dict] = {}
    for sym in symbols:
        cands = by_sym.get(sym, {})
        mine = cands.get(phash)
        house = cands.get(hhash) if hhash else None
        base = mine or house
        if base is None:
            out[sym] = {"symbol": sym, "trend": None,
                        "iv": {"iv_rank": None, "basis": None, "iv_n": None},
                        "idea": None, "pick_state": "not_checked", "stale": True,
                        "age_h": None, "snap_on": None, "status": None, "headline": None}
            continue
        idea = _recommended(base.strategies)
        if mine is None:
            pick_state = "not_checked"
        elif _has_real_picks(mine.picks, idea):
            pick_state = "has_picks"
        else:
            pick_state = "no_strike_passes"
        iv = base.iv if isinstance(base.iv, dict) else {}
        out[sym] = {
            "symbol": sym, "trend": base.trend,
            "iv": {"iv_rank": iv.get("iv_rank"), "basis": iv.get("basis"), "iv_n": iv.get("iv_n")},
            "idea": idea, "pick_state": pick_state,
            "stale": _stale(base.snap_on), "age_h": _age_h(base.as_of),
            "snap_on": base.snap_on, "status": base.status, "headline": base.headline,
        }
    return out
