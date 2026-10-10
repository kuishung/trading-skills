"""The Hermes Options Screener collector (OPTIONS_SCREENER_DESIGN.md §4, v4.136): the
always-on loop that keeps the screener database (``app/screener_db.py``, the ``scr_*``
tables) filled with the whole US options market from Massive (formerly Polygon.io), so
the Options page can screen ~1M contracts without reading anything itself.

Data: Massive Options Starter (the whole-chain snapshot - greeks, IV, open interest, the
day bar; 15 min delayed; NO bid/ask - and option daily bars; the options contracts
reference list) and Stocks Basic (grouped daily bars of every stock, the ticker
reference list; ~5 requests a minute, the client paces itself). Earnings dates come from
Nasdaq's free calendar (``calendars.earnings_for``). Every write goes through
``scr_store``.

The loop
--------
One ``tick()`` every 15 s never blocks for long: the work runs in two executors and the
tick only starts jobs and files their results.

* ``WORKERS`` threads (``TST_SCREENER_WORKERS``, 8) share ONE ``massive.Client``
  (``concurrency=WORKERS``, ``max_rps=TST_SCREENER_MAX_RPS`` 40) - the chain reads of a
  market pass and the IV-history reads; each job opens its own DB session.
* one "stocks lane" thread - the universe, identities, earnings, grouped stock days,
  per-symbol bar fills and the technicals, one job at a time, in that priority.

1. **Universe** - daily from 07:30 ET (and at start when older than 20 h, or empty):
   ``option_underlyings(exp_lte=today+60d)`` -> ``scr_store.upsert_universe`` (a symbol
   absent from the list goes inactive and stays in passes 10 more days) -> ``prune``. A
   failed refresh keeps yesterday's list and is retried in 15 min.
2. **Market passes** - on trading days a ``cycle`` pass every ``TST_SCREENER_CYCLE_MIN``
   (30) minutes from 09:45 to 16:00 ET, then one ``eod`` pass after 16:20 ET (a missed
   evening is caught up before the next open; on a weekend / holiday, the last trading
   day's). Every pass symbol, most contracts first: the whole chain
   (``chain_snapshot(exp today..today+TST_SCREENER_MAX_DTE)``, no strike window) ->
   ``opt_massive.standard_rows`` -> ``estimate_spot`` -> ``contract_rows`` (kept: open
   interest or volume; price = ``opt_massive.model_price`` from the contract's own IV,
   else the last trade; ``as_of`` = read time - 15 min) -> ``scr_store.replace_contracts``
   -> ``update_underlying_pass`` (spot, IV30, volumes / OI, expected move; the EOD pass
   files IV30 as the day's reading). One symbol failing is counted and skipped; a Massive
   error that is not about the symbol (no key, a rejected key, the plan, Massive not
   reachable) pauses the pass - the rest of it is read when the pause ends.
3. **Stock days** - every session of the last 2 years up to the last PUBLISHED one
   (``opt_massive.published_session``: a day's bars are out at 20:00 ET) that is not on
   file: ``grouped_daily`` adjusted (OHLCV) and unadjusted (``close_raw``), newest first,
   filed for the universe symbols only. A filed day that shows a > 40 % jump for a symbol
   (a split after its older bars were filed) re-reads that symbol's adjusted bars; a
   universe symbol with under 20 bars once the days are complete gets its own bars
   (``stock_daily``, at most once a day). Then the technicals (``recompute_technicals``).
   Weekly: the ticker reference lists (stocks + indices) -> name / security type /
   exchange. Daily: the earnings dates of the next 70 days.
4. **IV history** - between passes, once the last 260 sessions' unadjusted closes are on
   file: underlyings without it, most option volume first, up to ``WORKERS`` at a time ->
   ``opt_massive.iv30_history`` -> daily IV30 -> ``recompute_iv``; done at >= 20 points,
   else retried after 30 min doubling to 24 h (``scr_store.mark_history``).
5. **Status** - ``scr_status`` (row 1) and ``state/screener_collector.json`` (the Hermes
   tray reads it) at the start and the end of every tick.

Errors (as ``opt_collector``): ``config`` / ``auth`` -> everything paused, retried every
5 min (``app/.env`` re-read for the key); ``network`` -> everything paused 60 s doubling
to 5 min; ``plan`` -> only the part that needs the endpoint (``universe``, ``chain``,
``history`` or ``stocks``) paused 5 min. The key is never logged or written anywhere.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as _dt
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from . import calendars, clock, massive, opt_massive, option_metrics, scr_store
from .massive import MassiveError

COLLECTOR_VERSION = "1.0"
SOURCE = "massive"

TICK_S = 15.0                    # one tick every 15 s
HEARTBEAT_EVERY_S = 15.0         # one-off runs heartbeat this often while they wait
CYCLE_MIN_DEFAULT, CYCLE_MIN_LO, CYCLE_MIN_HI = 30, 5, 240
WORKERS_DEFAULT, WORKERS_LO, WORKERS_HI = 8, 1, 32
MAX_RPS_DEFAULT, MAX_RPS_LO, MAX_RPS_HI = 40.0, 1.0, 200.0
MAX_DTE_DEFAULT, MAX_DTE_LO, MAX_DTE_HI = 1100, 30, 1500

RTH_START = _dt.time(9, 45)      # the first cycle pass of a session
RTH_END = _dt.time(16, 0)        # no cycle pass starts at or after this
EOD_AFTER = _dt.time(16, 20)     # the end-of-day pass
UNIVERSE_AT = _dt.time(7, 30)    # the daily universe refresh
UNIVERSE_MAX_AGE_H = 20.0        # at start: refresh when older than this
UNIVERSE_EXP_DAYS = 60           # the universe = underlyings with an expiry in the next 60 days
UNIVERSE_RETRY_S = 900.0         # a failed refresh: again in 15 min (yesterday's list holds)
STOCK_YEARS = 2                  # grouped daily bars kept for the last 2 years
STOCK_DAYS_RELOAD_S = 6 * 3600.0  # the on-file days are re-read from the DB this often
EMPTY_DAY_RETRY_S = 6 * 3600.0   # a session Massive has no bars for: asked again in 6 h
STOCKS_RETRY_S = 600.0           # a failed stocks-lane job: again in 10 min
TECH_EVERY_S = 3600.0            # technicals while days are still being filed: at most hourly
IDENTITY_EVERY_S = 7 * 86400.0   # names / types / exchanges weekly
EARNINGS_DAYS = 70               # earnings dates for the next 70 days ...
EARNINGS_RETRY_S = 3600.0        # ... re-read daily; a failed read again in 1 h
FILL_MIN_BARS = 20               # a universe symbol under this many bars gets its own read ...
FILL_RETRY_S = 24 * 3600.0       # ... at most once a day
FILL_EMPTY_RETRY_S = 7 * 86400.0  # a symbol Massive has no stock bars for: once a week
FILL_BATCH = 50                  # symbols queued for a bar fill at a time
SPLIT_LO, SPLIT_HI = 0.6, 1.6    # a day-over-day close ratio outside this re-reads the bars
HISTORY_SESSIONS = 260           # the IV history covers the last 260 sessions
HISTORY_MIN_POINTS = scr_store.HISTORY_MIN_POINTS
KEY_RETRY_S = 300.0              # no key / a rejected key: looked at again every 5 min
PLAN_RETRY_S = 300.0             # an endpoint the plan lacks: tried again every 5 min
NETWORK_RETRY_S = 60.0           # Massive unreachable: 60 s ...
NETWORK_RETRY_MAX_S = 300.0      # ... doubling to 5 min
STATE_LOG_EVERY_S = 1800.0       # a persisting error is logged when it begins, then every 30 min
FAIL_LOG_PER_PASS = 20           # per-symbol failures logged at WARNING per pass (then a summary)

NO_KEY_TEXT = "%s is not set on this PC" % massive.ENV_KEY
ALL = "all"                       # an error scope: every Massive request
OPS = ("universe", "chain", "history", "stocks")
_OP_WORDS = {"universe": "the universe refresh", "chain": "market passes",
             "history": "IV-history reads", "stocks": "stock bars and reference reads"}
_FATAL = ("config", "auth", "network")    # MassiveError kinds that pause everything
_PAUSING = _FATAL + ("plan",)
_BG_STATE = {"universe": "universe", "identity": "stocks", "earnings": "stocks", "stocks": "stocks",
             "fill": "stocks", "technicals": "stocks"}
_BG_OP = {"universe": "universe", "identity": "stocks", "stocks": "stocks", "fill": "stocks"}

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "screener_collector.json"
APP_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

classify_trend = scr_store.classify_trend      # the MATP rule (ported in scr_store)


# ────────────────────────────────── settings ──────────────────────────────────

def _env_num(name: str, default, lo, hi, cast=int):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = cast(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def env_cycle_min() -> int:
    """``TST_SCREENER_CYCLE_MIN``: minutes between cycle passes, 5-240, else 30."""
    return _env_num("TST_SCREENER_CYCLE_MIN", CYCLE_MIN_DEFAULT, CYCLE_MIN_LO, CYCLE_MIN_HI)


def env_workers() -> int:
    """``TST_SCREENER_WORKERS``: reader threads (and the client's concurrency), 1-32, else 8."""
    return _env_num("TST_SCREENER_WORKERS", WORKERS_DEFAULT, WORKERS_LO, WORKERS_HI)


def env_max_rps() -> float:
    """``TST_SCREENER_MAX_RPS``: Massive requests a second at most, 1-200, else 40."""
    return _env_num("TST_SCREENER_MAX_RPS", MAX_RPS_DEFAULT, MAX_RPS_LO, MAX_RPS_HI, cast=float)


def env_max_dte() -> int:
    """``TST_SCREENER_MAX_DTE``: the farthest expiry read, in days, 30-1500, else 1100."""
    return _env_num("TST_SCREENER_MAX_DTE", MAX_DTE_DEFAULT, MAX_DTE_LO, MAX_DTE_HI)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(APP_ENV_PATH, override=False)
    except Exception:  # noqa: BLE001 - python-dotenv or the file missing: the environment as is
        pass


def default_client(workers: int | None = None, max_rps: float | None = None):
    """ONE ``massive.Client`` for the whole collector (``concurrency`` = the worker threads,
    ``max_rps`` the request cap), the key from the environment; ``app/.env`` is re-read
    first so a key added there is found at the next 5-minute look."""
    _load_env()
    w = int(workers or env_workers())
    return massive.Client(max_rps=float(max_rps or env_max_rps()), concurrency=w)


# ────────────────────────────────── pure helpers ──────────────────────────────────

def sec_type_of(code, market: str = "stocks") -> str:
    """Massive's ticker type -> the screener's security type: CS -> stock; ETF / ETN / ETV
    / ETS -> etf; an index (or the indices market) -> index; anything else (ADRC, PFD,
    WARRANT, UNIT, ...) -> other."""
    if str(market or "").lower() == "indices":
        return "index"
    c = str(code or "").strip().upper()
    if c == "CS":
        return "stock"
    if c in ("ETF", "ETN", "ETV", "ETS"):
        return "etf"
    if c == "INDEX":
        return "index"
    return "other"


def exchange_of(mic, market: str = "stocks") -> str:
    """A primary-exchange MIC -> the screener's exchange: XNYS -> NYSE; XNAS -> NASDAQ;
    XASE / ARCX / BATS -> AMEX; an index -> INDEX; anything else -> OTHER."""
    if str(market or "").lower() == "indices":
        return "INDEX"
    return {"XNYS": "NYSE", "XNAS": "NASDAQ", "XASE": "AMEX", "ARCX": "AMEX",
            "BATS": "AMEX"}.get(str(mic or "").strip().upper(), "OTHER")


def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x or "")[:10])
    except ValueError:
        return None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def contract_rows(std_rows, spot, *, day, session) -> tuple[list[dict], dict]:
    """The rows a pass keeps from one chain's standard contracts, and the underlying's
    option-wide figures. Pure.

    Kept (§3): open interest > 0 or volume > 0 in the contract's latest day bar; expiry on
    or after ``day`` (the ET date of the read). Per row: ``weekly`` (not the standard
    monthly - ``opt_massive.is_monthly`` against the chain's expiries), ``price`` = the
    Black-Scholes price from its own IV (``opt_massive.model_price``) when the spot is
    known, else the last trade; ``last`` = the day bar close; ``volume`` / ``chg_pct`` =
    the bar's when the bar is from ``session``, else 0 / None (Massive's ``day`` is the
    contract's most recent bar, which for a contract not traded yet in ``session`` is an
    older one); ``last_trade`` = the bar's time.

    Figures: ``iv30`` (PERCENT, ``option_metrics.atm_iv_by_expiry`` without the quote
    condition + ``iv30_constant_maturity``), ``exp_move30`` (= iv30 x sqrt(30/365)),
    ``call_vol`` / ``put_vol`` / ``call_oi`` / ``put_oi``, ``n_contracts``."""
    d0 = _to_date(day)
    ses = str(session)[:10]
    listed = {d for d in (_to_date(r.get("expiry")) for r in std_rows or ()) if d is not None}
    s = _num(spot) if (_num(spot) or 0) > 0 else None
    out: list[dict] = []
    for r in std_rows or ():
        d = _to_date(r.get("expiry"))
        right = str(r.get("right") or "").upper()[:1]
        if d is None or d < d0 or right not in ("C", "P"):
            continue
        vol, oi = r.get("volume"), r.get("oi")
        if not ((oi or 0) > 0 or (vol or 0) > 0):
            continue
        t = r.get("last_updated")
        bar_ses = clock.et_date(t).isoformat() if isinstance(t, _dt.datetime) else None
        fresh = bar_ses is None or bar_ses >= ses
        last = r.get("day_close")
        price = opt_massive.model_price(s, r.get("strike"), (d - d0).days, r.get("iv"), right) if s else None
        out.append({
            "expiry": d.isoformat(), "right": right, "strike": r.get("strike"),
            "weekly": not opt_massive.is_monthly(d, listed),
            "price": price if price is not None else last, "last": last,
            "chg_pct": r.get("day_change_pct") if fresh else None,
            "volume": (vol or 0) if fresh else 0, "oi": oi,
            "iv": r.get("iv"), "delta": r.get("delta"), "gamma": r.get("gamma"),
            "theta": r.get("theta"), "vega": r.get("vega"), "last_trade": t,
        })
    fig = {"iv30": None, "exp_move30": None, "n_contracts": len(out),
           "call_vol": sum(int(x["volume"] or 0) for x in out if x["right"] == "C"),
           "put_vol": sum(int(x["volume"] or 0) for x in out if x["right"] == "P"),
           "call_oi": sum(int(x["oi"] or 0) for x in out if x["right"] == "C"),
           "put_oi": sum(int(x["oi"] or 0) for x in out if x["right"] == "P")}
    if s:
        day_s = d0.isoformat()
        by_exp = option_metrics.atm_iv_by_expiry(
            [{"expiry": x["expiry"], "strike": x["strike"], "iv": x["iv"],
              "dte": (_to_date(x["expiry"]) - d0).days} for x in out],
            s, day_s, require_quote=False)
        v = option_metrics.iv30_constant_maturity(by_exp, day_s)
        if v is not None and v > 0:
            fig["iv30"] = round(v, 4)
            fig["exp_move30"] = round(v * math.sqrt(30.0 / 365.0), 4)
    return out, fig


def snapshot_session(now: _dt.datetime) -> str:
    """The session a chain read at ``now`` describes: today once the session has opened
    on a trading day; before the open the previous trading day; on a weekend / holiday the
    last trading day."""
    et = clock.et_now(now)
    d = et.date()
    if clock.is_trading_day(d):
        return (d if et.time() >= clock.SESSION_OPEN else clock.prev_trading_day(d)).isoformat()
    return clock.last_trading_day(d).isoformat()


def eod_day_for(now: _dt.datetime) -> str | None:
    """The trading day whose end-of-day pass may run at ``now``: today after 16:20 ET,
    the previous trading day before today's open (a missed evening caught up), the last
    trading day on a weekend / holiday; None from the open to 16:20 ET."""
    et = clock.et_now(now)
    d, t = et.date(), et.time()
    if clock.is_trading_day(d):
        if t >= EOD_AFTER:
            return d.isoformat()
        if t < clock.SESSION_OPEN:
            return clock.prev_trading_day(d).isoformat()
        return None
    return clock.last_trading_day(d).isoformat()


def stock_days(now: _dt.datetime, years: float | None = None) -> list[str]:
    """The trading days of the last ``years`` years (default ``STOCK_YEARS``) up to the
    last published session (``opt_massive.published_session``), oldest first."""
    years = STOCK_YEARS if years is None else years
    end = opt_massive.published_session(clock.et_date(now), now)
    start = end - _dt.timedelta(days=int(years * 365.25) + 1)
    out, d = [], start
    while d <= end:
        if clock.is_trading_day(d):
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def _iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, _dt.datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return t.isoformat()
    return str(t)


def _et_hm(t: _dt.datetime) -> str:
    return clock.et_now(t).strftime("%H:%M") + " ET"


def _has_key(client) -> bool:
    return client is not None and bool(getattr(client, "has_key", True))


def _plural(n: int, word: str) -> str:
    return "%s %s%s" % (format(int(n), ","), word, "" if n == 1 else "s")


class InlineExecutor:
    """A ``concurrent.futures`` executor that runs every job at once in the caller's
    thread - deterministic ticks for tests and by-hand debugging."""

    def submit(self, fn, *args, **kw):
        f: cf.Future = cf.Future()
        try:
            f.set_result(fn(*args, **kw))
        except BaseException as exc:  # noqa: BLE001
            f.set_exception(exc)
        return f

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:  # noqa: ARG002
        return None


def _thread_pool(n: int):
    return cf.ThreadPoolExecutor(max_workers=max(1, int(n)), thread_name_prefix="scr")


# ────────────────────────────────── a market pass ──────────────────────────────────

class _Pass:
    """One market pass in progress: the symbols still to read (``queue``), the jobs in
    flight, the counts, and the halt flag the workers check when Massive pauses it."""

    def __init__(self, pid: int, kind: str, session: str, day: _dt.date, symbols: list[str],
                 started: _dt.datetime):
        self.id, self.kind, self.session, self.day = pid, kind, session, day
        self.total = len(symbols)
        self.queue: list[str] = list(symbols)
        self.futures: dict[cf.Future, str] = {}
        self.ok = self.failed = self.contracts = self.pages = 0
        self.started = started
        self.t0 = time.monotonic()
        self.halt = threading.Event()
        self.logged_fails = 0

    @property
    def done(self) -> int:
        return self.ok + self.failed


# ────────────────────────────────── the collector ──────────────────────────────────

class Collector:
    """The §4 loop. ``tick()`` does one non-blocking step; ``run_forever()`` ticks every
    15 s; ``run_once`` / ``run_universe`` / ``run_eod`` / ``run_history`` are the CLI's
    one-off runs.

    Injectable (tests): ``session_factory()`` -> a session on the screener DB (default
    ``screener_db.SessionLocal``); ``client`` (a ``massive.Client`` or a fake with
    ``chain_snapshot``, ``option_daily``, ``stock_daily``, ``grouped_daily``,
    ``option_underlyings``, ``reference_tickers``, ``has_key``) or ``client_factory()``;
    ``clock()`` -> now (naive = UTC); ``sleep(s)``; ``executor_factory(n)`` -> an executor
    (default a thread pool; ``InlineExecutor`` runs jobs at once); ``earnings_fetch(day)``
    -> Nasdaq-shaped rows (default ``calendars.earnings_for``)."""

    def __init__(self, session_factory=None, *, client=None, client_factory=None, clock=None,
                 sleep=None, log=None, state_path=None, executor_factory=None, earnings_fetch=None,
                 tick_s: float = TICK_S, cycle_min=None, workers=None, max_rps=None, max_dte=None,
                 stock_years: float | None = None):
        if session_factory is None:
            from .. import screener_db  # noqa: PLC0415

            session_factory = screener_db.SessionLocal
        self._sf = session_factory
        self.workers = int(workers or env_workers())
        self.max_rps = float(max_rps or env_max_rps())
        self.max_dte = int(max_dte or env_max_dte())
        self.cycle_min = int(cycle_min or env_cycle_min())
        self.stock_years = float(stock_years or STOCK_YEARS)
        self.client = client
        if client_factory is None and client is None:
            client_factory = lambda: default_client(self.workers, self.max_rps)  # noqa: E731
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep or time.sleep
        self.log = log or logging.getLogger(__name__)
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self._executor_factory = executor_factory or _thread_pool
        self._earnings_fetch = earnings_fetch or calendars.earnings_for
        self.tick_s = float(tick_s)
        self.version = COLLECTOR_VERSION
        self.pid = os.getpid()
        # status
        self.state = "starting"
        self.detail = "starting"
        self.api_ok: bool | None = None
        self.last_error: str | None = None
        self.universe_n = 0
        self.universe_on: str | None = None
        self.history_done_n = 0
        self.history_total = 0
        self.last_pass: dict | None = None            # the newest FINISHED pass
        # errors that pause work: scope (ALL | an op) -> (kind, reason) / next try
        self._alerts: dict[str, tuple[str, str]] = {}
        self._retry_at: dict[str, _dt.datetime] = {}
        self._net_fails = 0
        self._state_log: tuple | None = None
        # work
        self._pool = None
        self._bgpool = None
        self._pass: _Pass | None = None
        self._last_cycle: _dt.datetime | None = None
        self._last_cycle_session: str | None = None
        self._cycle_retry = False
        self._last_eod: str | None = None
        self._bg: tuple[str, cf.Future] | None = None
        self._bg_detail = ""
        self._hist: dict[cf.Future, str] = {}
        self._universe_refreshed: _dt.datetime | None = None
        self._universe_retry_at: _dt.datetime | None = None
        self._universe_checked = False
        self._identity_at: _dt.datetime | None = None
        self._identity_retry_at: _dt.datetime | None = None
        self._earnings_on: str | None = None
        self._earnings_retry_at: _dt.datetime | None = None
        self._days: dict[str, list[bool]] = {}
        self._days_loaded_at: _dt.datetime | None = None
        self._day_retry: dict[str, _dt.datetime] = {}
        self._stocks_retry_at: _dt.datetime | None = None
        self._tech_dirty = True
        self._tech_at: _dt.datetime | None = None
        self._fill: dict[str, str] = {}                 # symbol -> "new" | "split"
        self._fill_tried: dict[str, _dt.datetime] = {}
        self._restored = False
        self._last_beat: _dt.datetime | None = None
        self._db_warned = False
        self._file_warned = False
        self._stopping = threading.Event()

    # ── clock + text ──

    def _now(self) -> _dt.datetime:
        t = self._clock() if self._clock is not None else _dt.datetime.now(_dt.timezone.utc)
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t

    def _scrub(self, text) -> str:
        """Text safe for a log line or the status: the key masked, capped at 500 chars."""
        s = str(text or "")
        for key in (massive.api_key(), getattr(self.client, "_key", None)):
            if isinstance(key, str) and len(key) >= 4:
                s = s.replace(key, "***")
        return massive._scrub(s)[:500]   # noqa: SLF001 - the client's own masking rules

    def _err_text(self, exc) -> str:
        return self._scrub(str(exc) or type(exc).__name__)

    # ── executors ──

    def _workers_pool(self):
        if self._pool is None:
            self._pool = self._executor_factory(self.workers)
        return self._pool

    def _lane(self):
        if self._bgpool is None:
            self._bgpool = self._executor_factory(1)
        return self._bgpool

    # ── errors that pause work ──

    def _alert_scope(self) -> str | None:
        if ALL in self._alerts:
            return ALL
        for op in OPS:
            if op in self._alerts:
                return op
        return None

    def error_kind(self) -> str | None:
        scope = self._alert_scope()
        return self._alerts[scope][0] if scope else None

    def _log_state(self, key: tuple, level: int, msg: str, *args) -> None:
        now = self._now()
        last = self._state_log
        if last is not None and last[0] == key and (now - last[1]).total_seconds() < STATE_LOG_EVERY_S:
            return
        self._state_log = (key, now)
        self.log.log(level, msg, *args)

    def _alert(self, scope: str, kind: str, reason: str, wait_s: float, now: _dt.datetime) -> None:
        self._alerts[scope] = (kind, reason)
        self._retry_at[scope] = now + _dt.timedelta(seconds=wait_s)
        self.last_error = reason[:500]
        what = "every Massive request" if scope == ALL else _OP_WORDS[scope]
        self._log_state(("alert", scope, kind, reason), logging.WARNING,
                        "Massive %s error - %s paused: %s (next try %s)", kind, what, reason,
                        _et_hm(self._retry_at[scope]))

    def _clear(self, scope: str) -> None:
        got = self._alerts.pop(scope, None)
        self._retry_at.pop(scope, None)
        if got is not None:
            self._state_log = None
            self.log.info("Massive works again (%s error cleared: %s)", got[0], got[1])

    def _allowed(self, op: str, now: _dt.datetime | None = None) -> bool:
        now = now or self._now()
        for scope in (ALL, op):
            until = self._retry_at.get(scope)
            if until is not None and now < until:
                return False
        return True

    def _success(self, op: str) -> None:
        self.api_ok = True
        self._net_fails = 0
        for scope in (ALL, op):
            if scope in self._alerts:
                self._clear(scope)

    def _massive_failure(self, op: str, kind: str, msg: str) -> None:
        """A Massive error that pauses work: ``config`` / ``auth`` / ``network`` ->
        everything, ``plan`` -> ``op``."""
        now = self._now()
        if kind in ("config", "auth"):
            self.api_ok = False
            self._alert(ALL, kind, msg, KEY_RETRY_S, now)
        elif kind == "network":
            self.api_ok = False
            until = self._retry_at.get(ALL)
            if until is not None and now < until and self._alerts.get(ALL, ("",))[0] == "network":
                return                     # jobs in flight failing in the same outage: one back-off step
            self._net_fails += 1
            wait = min(NETWORK_RETRY_S * 2 ** (self._net_fails - 1), NETWORK_RETRY_MAX_S)
            self._alert(ALL, "network", msg, wait, now)
        elif kind == "plan":
            self._alert(op, "plan", msg, PLAN_RETRY_S, now)

    def _ready(self, now: _dt.datetime, *, force: bool = False) -> bool:
        """A client with a key, and Massive not paused (``force``: look now). No key ->
        state error ``NO_KEY_TEXT``, looked at again in 5 min."""
        until = self._retry_at.get(ALL)
        if not force and until is not None and now < until:
            return False
        c = self.client
        if not _has_key(c) and self._client_factory is not None:
            try:
                fresh = self._client_factory()
            except Exception as exc:  # noqa: BLE001
                self._alert(ALL, "config", "could not set up the Massive client: %s"
                            % self._err_text(exc), KEY_RETRY_S, now)
                return False
            if c is not None and fresh is not c:
                self._close_client(c)
            self.client = c = fresh
        if not _has_key(c):
            self._alert(ALL, "config", NO_KEY_TEXT, KEY_RETRY_S, now)
            return False
        if self._alerts.get(ALL, ("",))[0] == "config":
            self._clear(ALL)
        return True

    def _close_client(self, c) -> None:
        close = getattr(c, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    # ── status ──

    def _shown(self) -> tuple[str, str]:
        scope = self._alert_scope()
        if scope is None or self.state == "stopped":
            return self.state, self.detail
        kind, reason = self._alerts[scope]
        text = reason
        if scope != ALL:
            text += " - %s paused, the rest carries on" % _OP_WORDS[scope]
        nxt = self._retry_at.get(scope)
        if nxt is not None and nxt > self._now():
            text += "; next try %s" % _et_hm(nxt)
        return "error", text

    @property
    def shown_state(self) -> str:
        return self._shown()[0]

    def _pending_days(self) -> int:
        return sum(1 for v in self._days.values() if not (v[0] and v[1]))

    def _fields(self) -> dict:
        state, detail = self._shown()
        p = self._pass
        if p is not None:
            pass_id, total, done = p.id, p.total, p.done
        else:
            lp = self.last_pass or {}
            pass_id, total, done = lp.get("id"), lp.get("n_symbols"), (lp.get("n_ok") or 0) + (lp.get("n_failed") or 0)
        return {"state": state, "detail": detail, "pid": self.pid, "version": self.version,
                "pass_id": pass_id, "symbols_total": total, "symbols_done": done,
                "universe_n": self.universe_n, "universe_on": self.universe_on,
                "history_done_n": self.history_done_n, "history_total": self.history_total,
                "last_error": self.last_error, "api_ok": self.api_ok}

    def _beat(self, db, *, force: bool = True) -> None:
        now = self._now()
        if not force and self._last_beat is not None and \
                (now - self._last_beat).total_seconds() < HEARTBEAT_EVERY_S:
            return
        self._last_beat = now
        fields = self._fields()
        if db is not None:
            try:
                scr_store.set_status(db, heartbeat=now, **fields)
                self._db_warned = False
            except Exception as exc:  # noqa: BLE001 - the file still carries the heartbeat
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                if not self._db_warned:
                    self.log.warning("could not write the screener status row: %s", exc)
                    self._db_warned = True
        self._write_state(fields, now)

    def _write_state(self, fields: dict, now: _dt.datetime) -> None:
        """``state/screener_collector.json`` for the Hermes tray, replaced atomically (a
        ``.gitignore`` is dropped into a fresh ``state`` folder)."""
        doc = dict(fields)
        p = self._pass
        lp = self.last_pass or {}
        scope = self._alert_scope()
        nxt = self._retry_at.get(scope) if scope else None
        doc.update(
            heartbeat=_iso(now), source=SOURCE, error_kind=self.error_kind(),
            next_try=_iso(nxt) if (nxt is not None and nxt > now) else None,
            pass_kind=p.kind if p is not None else None,
            pass_session=p.session if p is not None else None,
            symbols_failed=p.failed if p is not None else None,
            last_pass_id=lp.get("id"), last_pass_kind=lp.get("kind"),
            last_pass_session=lp.get("session"), last_pass_finished=_iso(lp.get("finished")),
            last_pass_et=_et_hm(lp["finished"]) if lp.get("finished") else None,
            last_pass_contracts=lp.get("n_contracts"), last_eod_session=self._last_eod,
            stock_days_pending=self._pending_days() if self._days else None,
            cycle_min=self.cycle_min, workers=self.workers,
            written_by="dashboard_tst/app/services/scr_collector.py")
        path = self.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.name == "state":
                gi = path.parent / ".gitignore"
                if not gi.exists():
                    gi.write_text("# runtime state (collector heartbeats) - never committed\n*\n",
                                  encoding="utf-8")
            text = json.dumps(doc, indent=1, sort_keys=True, default=str)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                os.replace(tmp, path)
            except PermissionError:          # the tray had it open this instant (Windows)
                path.write_text(text, encoding="utf-8")
                try:
                    tmp.unlink()
                except OSError:
                    pass
            self._file_warned = False
        except Exception as exc:  # noqa: BLE001
            if not self._file_warned:
                self.log.warning("could not write %s: %s", path, exc)
                self._file_warned = True

    def _restore(self, db) -> None:
        """What survives a restart: the last cycle pass's start, the last finished EOD
        session, the last finished pass."""
        if self._restored:
            return
        self._restored = True
        try:
            cyc = scr_store.last_pass(db, kind="cycle", finished=None)
            if cyc:
                self._last_cycle, self._last_cycle_session = cyc.get("started"), cyc.get("session")
            eod = scr_store.last_pass(db, kind="eod")
            if eod:
                self._last_eod = eod.get("session")
            self.last_pass = scr_store.last_pass(db)
            st = scr_store.status(db) or {}
            self.universe_on = st.get("universe_on") or None
        except Exception as exc:  # noqa: BLE001 - e.g. the tables missing before a migration
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            self.log.warning("could not read the screener's last passes: %s", exc)

    def _refresh_counts(self, db, now: _dt.datetime) -> list[str]:
        syms = scr_store.pass_symbols(db, now=now)
        self.universe_n = len(syms)
        self.history_done_n, self.history_total = scr_store.history_counts(db, syms)
        self._universe_refreshed = scr_store.universe_info(db).get("refreshed")
        return syms

    # ── one chain (a worker thread) ──

    def read_chain(self, db, sym: str, *, kind: str, session: str, day: _dt.date,
                   pass_id: int | None = None) -> dict:
        """Read ``sym``'s whole chain and file it (§4.2). Raises ``MassiveError`` when the
        read fails - nothing is written then. Returns ``{"symbol", "contracts", "pages",
        "spot", "spot_src", "iv30"}``."""
        t_read = self._now()
        snap = self.client.chain_snapshot(sym, exp_gte=day, exp_lte=day + _dt.timedelta(days=self.max_dte))
        std = opt_massive.standard_rows(sym, snap.get("rows") or [])
        stored_close, close_on = scr_store.last_close(db, sym)
        spot, src = opt_massive.estimate_spot(dict(snap, rows=std), stored_close=stored_close, today=day)
        rows, fig = contract_rows(std, spot, day=day, session=session)
        as_of = t_read - _dt.timedelta(seconds=opt_massive.DELAY_S)
        n = scr_store.replace_contracts(db, sym, rows, session_day=session, as_of=as_of)
        if not spot:
            spot, src, s_as_of = None, None, None
        elif src == "massive":
            s_as_of = min(snap.get("underlying_as_of") or as_of, t_read)
        elif src == "close":
            s_as_of = scr_store.close_time_utc(close_on) or as_of
        else:
            s_as_of = as_of
        scr_store.update_underlying_pass(
            db, sym, session_day=session, pass_id=pass_id, spot=spot, spot_src=src, spot_as_of=s_as_of,
            iv30=fig["iv30"], exp_move30=fig["exp_move30"], call_vol=fig["call_vol"],
            put_vol=fig["put_vol"], call_oi=fig["call_oi"], put_oi=fig["put_oi"],
            n_contracts=fig["n_contracts"], file_iv=(kind == "eod"), now=t_read)
        return {"symbol": sym, "contracts": n, "pages": int(snap.get("pages") or 0), "spot": spot,
                "spot_src": src, "iv30": fig["iv30"]}

    def _chain_job(self, p: _Pass, sym: str) -> dict:
        if p.halt.is_set() or self._stopping.is_set():
            return {"symbol": sym, "skipped": True}
        db = self._sf()
        try:
            res = self.read_chain(db, sym, kind=p.kind, session=p.session, day=p.day, pass_id=p.id)
            return dict(res, ok=True)
        except MassiveError as exc:
            if exc.kind in _PAUSING:
                p.halt.set()
            return {"symbol": sym, "ok": False, "kind": exc.kind, "error": self._err_text(exc)}
        except Exception as exc:  # noqa: BLE001 - one symbol never stops the pass
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
        finally:
            db.close()

    # ── passes ──

    def _pass_due(self, now: _dt.datetime, syms: list[str]) -> tuple[str, str] | None:
        if not syms or not self._allowed("chain", now):
            return None
        et = clock.et_now(now)
        d, t = et.date(), et.time()
        if clock.is_trading_day(d) and RTH_START <= t < RTH_END:
            today = d.isoformat()
            if (self._cycle_retry or self._last_cycle is None or self._last_cycle_session != today
                    or (now - self._last_cycle).total_seconds() >= self.cycle_min * 60):
                return "cycle", today
            return None
        day = eod_day_for(now)
        if day and (self._last_eod or "") < day:
            return "eod", day
        return None

    def _start_pass(self, db, kind: str, session: str, syms: list[str], now: _dt.datetime) -> _Pass:
        pid = scr_store.start_pass(db, kind=kind, session=session, n_symbols=len(syms), now=now)
        p = _Pass(pid, kind, session, clock.et_date(now), syms, now)
        self._pass = p
        if kind == "cycle":
            self._last_cycle, self._last_cycle_session, self._cycle_retry = now, session, False
        self.log.info("%s pass %d (session %s): %s, %d workers", kind, pid, session,
                      _plural(len(syms), "underlying"), self.workers)
        self._submit_pass(p)
        return p

    def _submit_pass(self, p: _Pass) -> None:
        p.halt.clear()
        pool = self._workers_pool()
        queue, p.queue = p.queue, []
        for sym in queue:
            p.futures[pool.submit(self._chain_job, p, sym)] = sym

    def _collect_pass(self, db) -> None:
        p = self._pass
        if p is None:
            return
        for f in [f for f in p.futures if f.done()]:
            sym = p.futures.pop(f)
            try:
                res = f.result()
            except Exception as exc:  # noqa: BLE001
                res = {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
            if res.get("skipped"):
                p.queue.append(sym)
            elif res.get("ok"):
                p.ok += 1
                p.contracts += int(res.get("contracts") or 0)
                p.pages += int(res.get("pages") or 0)
                self._success("chain")
            elif res.get("kind") in _PAUSING:
                p.queue.append(sym)                    # read again when the pause ends
                self._massive_failure("chain", res["kind"], res.get("error") or "")
            else:
                p.failed += 1
                self.last_error = ("%s %s: %s" % (p.kind, sym, res.get("error")))[:500]
                if p.logged_fails < FAIL_LOG_PER_PASS:
                    self.log.warning("%s pass %d: %s failed: %s", p.kind, p.id, sym, res.get("error"))
                p.logged_fails += 1
        if p.futures:
            return
        if not p.queue:
            self._finish_pass(db, p, finished=True)
            return
        now = self._now()
        et = clock.et_now(now)
        if p.kind == "cycle" and (et.date().isoformat() != p.session or et.time() >= EOD_AFTER):
            self._finish_pass(db, p, finished=False)     # paused past the session: the EOD pass takes over
            return
        if self._allowed("chain", now) and not self._stopping.is_set():
            self.log.info("%s pass %d resumes: %s left", p.kind, p.id, _plural(len(p.queue), "underlying"))
            self._submit_pass(p)

    def _finish_pass(self, db, p: _Pass, *, finished: bool) -> None:
        ms = int((time.monotonic() - p.t0) * 1000)
        failed = p.failed + (0 if finished else len(p.queue))
        try:
            row = scr_store.finish_pass(db, p.id, n_ok=p.ok, n_failed=failed, n_contracts=p.contracts,
                                        requests=p.pages, ms=ms, finished=finished, now=self._now())
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            row = None
            self.log.warning("could not record the end of pass %d: %s", p.id, exc)
        self._pass = None
        if finished:
            self.last_pass = row or self.last_pass
            if p.kind == "eod":
                self._last_eod = p.session
            self.log.info("%s pass %d done: %d ok, %d failed, %s, %d requests, %.0f s", p.kind, p.id,
                          p.ok, p.failed, _plural(p.contracts, "contract"), p.pages, ms / 1000.0)
        else:
            if p.kind == "cycle":
                self._cycle_retry = False
            self.log.warning("%s pass %d stopped unfinished: %d ok, %d failed, %d not read", p.kind,
                             p.id, p.ok, p.failed, len(p.queue))
        if p.logged_fails > FAIL_LOG_PER_PASS:
            self.log.warning("%s pass %d: %d underlyings failed in all (the first %d logged)", p.kind,
                             p.id, p.logged_fails, FAIL_LOG_PER_PASS)

    # ── IV history (worker threads) ──

    def history_one(self, db, sym: str, *, overwrite: bool = False) -> dict:
        """The IV30 history of ``sym`` (§4.4) from its unadjusted closes; files the series,
        recomputes the IV figures, marks the attempt. Raises ``MassiveError`` (nothing
        marked) when a read fails."""
        bars = scr_store.raw_bars(db, sym, HISTORY_SESSIONS)
        points, n_req = [], 0
        if len(bars) >= HISTORY_MIN_POINTS:
            points, n_req = opt_massive.iv30_history(self.client, sym, bars, days_iv=HISTORY_SESSIONS)
            if points:
                scr_store.upsert_daily(db, sym, iv_series=points, overwrite_iv=overwrite)
            scr_store.recompute_iv(db, sym, now=self._now())
        done = len(points) >= HISTORY_MIN_POINTS
        mark = scr_store.mark_history(db, sym, done=done, now=self._now())
        return {"symbol": sym, "bars": len(bars), "points": len(points), "requests": n_req,
                "done": done, "tries": mark.get("tries")}

    def _history_job(self, sym: str, overwrite: bool = False) -> dict:
        if self._stopping.is_set():
            return {"symbol": sym, "skipped": True}
        db = self._sf()
        try:
            return dict(self.history_one(db, sym, overwrite=overwrite), ok=True)
        except MassiveError as exc:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            if exc.kind not in _PAUSING:
                try:
                    scr_store.mark_history(db, sym, done=False, now=self._now())
                except Exception:  # noqa: BLE001
                    db.rollback()
            return {"symbol": sym, "ok": False, "kind": exc.kind, "error": self._err_text(exc)}
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
                scr_store.mark_history(db, sym, done=False, now=self._now())
            except Exception:  # noqa: BLE001
                db.rollback()
            return {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
        finally:
            db.close()

    def _collect_history(self) -> list[dict]:
        out = []
        for f in [f for f in self._hist if f.done()]:
            sym = self._hist.pop(f)
            try:
                res = f.result()
            except Exception as exc:  # noqa: BLE001
                res = {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
            out.append(res)
            if res.get("skipped"):
                continue
            if res.get("ok"):
                if res.get("requests"):
                    self._success("history")
                lvl = logging.INFO if res.get("done") else logging.DEBUG
                self.log.log(lvl, "IV history %s: %d IV points from %d closes, %d requests%s", sym,
                             res.get("points", 0), res.get("bars", 0), res.get("requests", 0),
                             "" if res.get("done") else " - too few, retried later")
            elif res.get("kind") in _PAUSING:
                self._massive_failure("history", res["kind"], res.get("error") or "")
            else:
                self.last_error = ("IV history %s: %s" % (sym, res.get("error")))[:500]
                self.log.warning("IV history %s failed: %s", sym, res.get("error"))
        return out

    def _recent_days_done(self) -> bool:
        if not self._days:
            return False
        recent = sorted(self._days)[-HISTORY_SESSIONS:]
        return all(self._days[d][0] and self._days[d][1] for d in recent)

    def _history_tick(self, db, now: _dt.datetime, syms: list[str]) -> None:
        room = self.workers - len(self._hist)
        if room <= 0 or not self._allowed("history", now) or not self._recent_days_done():
            return
        busy = set(self._hist.values())
        queue = [s for s in scr_store.history_queue(db, now=now, symbols=syms, limit=room + len(busy))
                 if s not in busy][:room]
        pool = self._workers_pool()
        for sym in queue:
            self._hist[pool.submit(self._history_job, sym)] = sym

    # ── the stocks lane (one thread) ──

    def _load_days(self, db, now: _dt.datetime) -> None:
        if self._days_loaded_at is not None and \
                (now - self._days_loaded_at).total_seconds() < STOCK_DAYS_RELOAD_S:
            want = stock_days(now, self.stock_years)
            if want and want[-1] in self._days:
                return
        days = stock_days(now, self.stock_years)
        if not days:
            self._days = {}
            return
        have = scr_store.stock_day_status(db, days[0], days[-1])
        self._days = {d: [have.get(d, (0, 0))[0] > 0, have.get(d, (0, 0))[1] > 0] for d in days}
        self._days_loaded_at = now

    def _next_day(self, now: _dt.datetime) -> tuple[str, bool, bool] | None:
        for d in sorted(self._days, reverse=True):
            adj, raw = self._days[d]
            if adj and raw:
                continue
            at = self._day_retry.get(d)
            if at is not None and now < at:
                continue
            return d, not adj, not raw
        return None

    def _universe_due(self, now: _dt.datetime) -> bool:
        if not self._allowed("universe", now):
            return False
        if self._universe_retry_at is not None and now < self._universe_retry_at:
            return False
        ref = self._universe_refreshed
        first = not self._universe_checked
        self._universe_checked = True
        if ref is None:
            return True
        et = clock.et_now(now)
        at = _dt.datetime.combine(et.date(), UNIVERSE_AT, tzinfo=et.tzinfo).astimezone(
            _dt.timezone.utc).replace(tzinfo=None)
        if now >= at > ref:
            return True
        return first and (now - ref).total_seconds() > UNIVERSE_MAX_AGE_H * 3600

    def _next_bg_job(self, db, now: _dt.datetime):
        if self._universe_due(now):
            return "universe", self.job_universe, "refreshing the universe (Massive's options list)"
        if not self.universe_n:
            return None
        stocks_ok = self._allowed("stocks", now) and not (
            self._stocks_retry_at is not None and now < self._stocks_retry_at)
        if stocks_ok and self._identity_due(db, now):
            return "identity", self.job_identity, "names, security types and exchanges"
        today = clock.et_today(now)
        if self._earnings_on != today and not (self._earnings_retry_at and now < self._earnings_retry_at):
            et = clock.et_now(now)
            if et.time() >= UNIVERSE_AT or self._earnings_on is None:
                return "earnings", self.job_earnings, "earnings dates (next %d days)" % EARNINGS_DAYS
        if stocks_ok:
            self._load_days(db, now)
            nxt = self._next_day(now)
            if nxt is not None:
                d, adj, raw = nxt
                return ("stocks", lambda: self.job_stock_day(d, adjusted=adj, raw=raw),
                        "stock bars of %s (%s to go)" % (d, _plural(self._pending_days(), "session")))
            for sym, why in list(self._fill.items()):
                at = self._fill_tried.get(sym)
                if at is not None and (now - at).total_seconds() < FILL_RETRY_S:
                    self._fill.pop(sym, None)
                    continue
                self._fill.pop(sym, None)
                self._fill_tried[sym] = now
                return ("fill", lambda s=sym, w=why: self.job_fill(s, split=(w == "split")),
                        "own stock bars of %s (%s)" % (sym, "a split" if why == "split" else "new"))
        if self._tech_dirty and (self._pending_days() == 0 or self._tech_at is None
                                 or (now - self._tech_at).total_seconds() >= TECH_EVERY_S):
            return "technicals", self.job_technicals, "technicals of %s" % _plural(self.universe_n, "underlying")
        return None

    def _identity_due(self, db, now: _dt.datetime) -> bool:
        if self._identity_retry_at is not None and now < self._identity_retry_at:
            return False
        if self._identity_at is not None:
            return (now - self._identity_at).total_seconds() >= IDENTITY_EVERY_S
        return scr_store.identity_missing(db)

    def _bg_wrap(self, name: str, fn) -> dict:
        try:
            out = fn() or {}
            return dict(out, ok=out.get("ok", True), job=name)
        except MassiveError as exc:
            return {"job": name, "ok": False, "kind": exc.kind, "error": self._err_text(exc)}
        except Exception as exc:  # noqa: BLE001
            self.log.exception("stocks lane job %s failed", name)
            return {"job": name, "ok": False, "kind": "error", "error": self._err_text(exc)}

    def _bg_tick(self, db, now: _dt.datetime) -> None:
        if self._bg is not None:
            return
        job = self._next_bg_job(db, now)
        if job is None:
            return
        name, fn, detail = job
        self._bg_detail = detail
        self._bg = (name, self._lane().submit(self._bg_wrap, name, fn))

    def _collect_bg(self) -> dict | None:
        if self._bg is None or not self._bg[1].done():
            return None
        name, fut = self._bg
        self._bg = None
        try:
            res = fut.result()
        except Exception as exc:  # noqa: BLE001
            res = {"job": name, "ok": False, "kind": "error", "error": self._err_text(exc)}
        self._bg_result(name, res)
        return res

    def _bg_result(self, name: str, res: dict) -> None:
        now = self._now()
        if not res.get("ok"):
            kind, err = res.get("kind"), res.get("error") or ""
            op = _BG_OP.get(name)
            if op and kind in _PAUSING:
                self._massive_failure(op, kind, err)
            else:
                self.last_error = ("%s: %s" % (name, err))[:500]
                self.log.warning("%s failed: %s", name, err)
            if name == "universe" and kind not in _FATAL:
                self._universe_retry_at = now + _dt.timedelta(seconds=UNIVERSE_RETRY_S)
            elif name == "identity" and kind not in _FATAL:
                self._identity_retry_at = now + _dt.timedelta(seconds=STOCKS_RETRY_S)
            elif name == "earnings":
                self._earnings_retry_at = now + _dt.timedelta(seconds=EARNINGS_RETRY_S)
            elif name in ("stocks", "fill") and kind not in _PAUSING:
                self._stocks_retry_at = now + _dt.timedelta(seconds=STOCKS_RETRY_S)
            return
        if name in _BG_OP:
            self._success(_BG_OP[name])
        if name == "universe":
            self._universe_retry_at = None
            self.universe_on = clock.et_today(now)
            self._universe_refreshed = now
            self._tech_dirty = True
            self.log.info("universe: %s (%d new, %d gone%s); prune %s", _plural(res.get("n", 0), "underlying"),
                          res.get("new", 0), res.get("inactive", 0),
                          ", partial list - nothing deactivated" if res.get("partial") else "",
                          res.get("pruned"))
            self._queue_fill(res.get("fill") or [], "new")
        elif name == "identity":
            self._identity_at, self._identity_retry_at = now, None
            self.log.info("identity: %d underlyings named (%d stock tickers, %d indices)",
                          res.get("named", 0), res.get("stocks", 0), res.get("indices", 0))
        elif name == "earnings":
            self._earnings_on, self._earnings_retry_at = clock.et_today(now), None
            self.log.info("earnings: %d dates from %d calendar rows, %d rows changed",
                          res.get("dates", 0), res.get("rows", 0), res.get("changed", 0))
        elif name == "stocks":
            d = res.get("day")
            if d in self._days:
                if res.get("adj_bars"):
                    self._days[d][0] = True
                if res.get("raw_bars"):
                    self._days[d][1] = True
            if (res.get("adjusted") and not res.get("adj_bars")) or (res.get("raw") and not res.get("raw_bars")):
                self._day_retry[d] = now + _dt.timedelta(seconds=EMPTY_DAY_RETRY_S)
                self.log.info("stock bars of %s: Massive has none yet - asked again in %d h", d,
                              EMPTY_DAY_RETRY_S / 3600)
            if res.get("filed"):
                self._tech_dirty = True
            self._queue_fill(res.get("split") or [], "split")
            if self._pending_days() == 0 and res.get("filed"):
                self.log.info("stock bars: every session of the last %d years is on file", STOCK_YEARS)
        elif name == "fill":
            # job_fill recomputed that symbol's technicals itself. Massive had no bars at all
            # for it (not a stock - an index, a foreign listing): ask again in a week.
            if not res.get("days") and res.get("symbol"):
                self._fill_tried[res["symbol"]] = now + _dt.timedelta(seconds=FILL_EMPTY_RETRY_S - FILL_RETRY_S)
        elif name == "technicals":
            self._tech_dirty = False
            self._tech_at = now
            self._queue_fill(res.get("fill") or [], "new")
            self.log.info("technicals: %s recomputed", _plural(res.get("n", 0), "underlying"))

    def _queue_fill(self, syms, why: str) -> None:
        now = self._now()
        for s in list(syms)[:FILL_BATCH * 4]:
            at = self._fill_tried.get(s)
            if why == "split" or at is None or (now - at).total_seconds() >= FILL_RETRY_S:
                if why == "split":
                    self._fill_tried.pop(s, None)
                if len(self._fill) < FILL_BATCH or why == "split":
                    self._fill[s] = why

    def _fill_candidates(self, db) -> list[str]:
        """Universe symbols with too few bars once every stock day is on file."""
        if not self._days or self._pending_days():
            return []
        counts = scr_store.daily_counts(db)
        return [s for s in scr_store.pass_symbols(db, now=self._now())
                if not s.startswith("I:") and counts.get(s, 0) < FILL_MIN_BARS]

    # ── stocks-lane jobs (each opens its own session) ──

    def job_universe(self) -> dict:
        """Massive's options list -> the universe -> retention."""
        now = self._now()
        today = clock.et_date(now)
        counts = self.client.option_underlyings(exp_lte=today + _dt.timedelta(days=UNIVERSE_EXP_DAYS))
        db = self._sf()
        try:
            res = scr_store.upsert_universe(db, counts, now=now)
            res["pruned"] = scr_store.prune(db, now=now, today=today.isoformat())
            res["fill"] = self._fill_candidates(db)
            return res
        finally:
            db.close()

    def job_identity(self) -> dict:
        """The ticker reference lists -> name / security type / exchange."""
        stocks = self.client.reference_tickers("stocks")
        try:
            indices = self.client.reference_tickers("indices")
        except MassiveError as exc:
            if exc.kind in _FATAL:
                raise
            self.log.info("index names not read (%s): index underlyings keep no name",
                          self._err_text(exc))
            indices = []
        records = [{"symbol": t["symbol"], "name": t.get("name"),
                    "sec_type": sec_type_of(t.get("type")), "exchange": exchange_of(t.get("primary_exchange"))}
                   for t in stocks]
        seen = {r["symbol"] for r in records}
        for t in indices:
            rec = {"symbol": t["symbol"], "name": t.get("name"), "sec_type": "index", "exchange": "INDEX"}
            records.append(rec)
            bare = t["symbol"][2:] if t["symbol"].startswith("I:") else None
            if bare and bare not in seen:
                records.append(dict(rec, symbol=bare))
        db = self._sf()
        try:
            n = scr_store.set_identity(db, records, only=set(scr_store.pass_symbols(db, now=self._now())))
            return {"named": n, "stocks": len(stocks), "indices": len(indices)}
        finally:
            db.close()

    def job_earnings(self) -> dict:
        """The next earnings date per underlying from Nasdaq's calendar (next 70 days)."""
        now = self._now()
        today = clock.et_date(now)
        dates: dict[str, str] = {}
        n_rows = 0
        for i in range(EARNINGS_DAYS + 1):
            d = today + _dt.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            if self._stopping.is_set():
                return {"ok": False, "kind": "error", "error": "stopping"}
            rows = self._earnings_fetch(d) or []
            n_rows += len(rows)
            for r in rows:
                sym = massive.our_symbol((r or {}).get("symbol"))
                if sym and sym not in dates:
                    dates[sym] = d.isoformat()
        if n_rows == 0:
            return {"ok": False, "kind": "error", "error": "Nasdaq's earnings calendar returned nothing"}
        db = self._sf()
        try:
            changed = scr_store.set_earnings(db, dates, today=today.isoformat(), now=now)
        finally:
            db.close()
        return {"dates": len(dates), "rows": n_rows, "changed": changed}

    def job_stock_day(self, day: str, *, adjusted: bool = True, raw: bool = True) -> dict:
        """One session of grouped daily bars (adjusted and / or unadjusted) for the
        universe; a filed adjusted day is checked for splits against the day before."""
        out = {"day": day, "adjusted": adjusted, "raw": raw, "adj_bars": 0, "raw_bars": 0, "filed": 0,
               "split": []}
        db = self._sf()
        try:
            syms = set(scr_store.pass_symbols(db, now=self._now()))
            if adjusted:
                bars = self.client.grouped_daily(day, adjusted=True)
                out["adj_bars"] = len(bars)
                out["filed"] += scr_store.file_grouped_day(db, day, bars, adjusted=True, symbols=syms)
            if raw:
                bars = self.client.grouped_daily(day, adjusted=False)
                out["raw_bars"] = len(bars)
                out["filed"] += scr_store.file_grouped_day(db, day, bars, adjusted=False, symbols=syms)
            if adjusted and out["adj_bars"]:
                prev = clock.prev_trading_day(day).isoformat()
                if self._days.get(prev, [False])[0]:
                    out["split"] = scr_store.big_moves(db, day, lo=SPLIT_LO, hi=SPLIT_HI, symbols=syms)
            return out
        finally:
            db.close()

    def job_fill(self, sym: str, *, split: bool = False) -> dict:
        """One symbol's own 2 years of bars (a new universe member; after a split only the
        adjusted ones), then its technicals."""
        now = self._now()
        end = opt_massive.published_session(clock.et_date(now), now)
        start = end - _dt.timedelta(days=int(self.stock_years * 365.25) + 1)
        adj = self.client.stock_daily(sym, start, end)
        raw = [] if split else self.client.stock_daily(sym, start, end, adjusted=False)
        db = self._sf()
        try:
            n = scr_store.upsert_daily(db, sym, bars=adj, raw_bars=raw, today=end.isoformat())
            scr_store.recompute_technicals(db, sym, now=now)
            return {"symbol": sym, "days": n, "split": split}
        finally:
            db.close()

    def job_technicals(self) -> dict:
        """The technicals of every pass symbol; then the symbols that need their own bars."""
        db = self._sf()
        try:
            syms = scr_store.pass_symbols(db, now=self._now())
            n = scr_store.recompute_technicals_many(db, syms, now=self._now(),
                                                    stop=self._stopping.is_set)
            return {"n": n, "fill": self._fill_candidates(db)}
        finally:
            db.close()

    # ── the tick ──

    def _collect(self, db) -> None:
        self._collect_pass(db)
        self._collect_history()
        self._collect_bg()

    def _set_state(self, now: _dt.datetime) -> None:
        p = self._pass
        if p is not None:
            self.state = "pass"
            self.detail = "%s pass %d: %s/%s underlyings%s" % (
                p.kind, p.id, format(p.done, ","), format(p.total, ","),
                ", %d failed" % p.failed if p.failed else "")
            return
        if self._bg is not None and self._bg[0] == "universe":
            self.state, self.detail = "universe", self._bg_detail
            return
        if self._bg is not None:
            self.state, self.detail = "stocks", self._bg_detail
            return
        if self._hist:
            self.state = "history"
            self.detail = "IV history: %s (%s/%s done)" % (
                ", ".join(sorted(self._hist.values())[:4]) + (" ..." if len(self._hist) > 4 else ""),
                format(self.history_done_n, ","), format(self.history_total, ","))
            return
        self.state = "idle"
        if not self.universe_n:
            self.detail = "waiting for the universe"
            return
        et = clock.et_now(now)
        d, t = et.date(), et.time()
        if clock.is_trading_day(d) and t < RTH_END and t >= clock.SESSION_OPEN:
            if self._last_cycle is not None and self._last_cycle_session == d.isoformat():
                nxt = self._last_cycle + _dt.timedelta(minutes=self.cycle_min)
                self.detail = "next pass %s (every %d min)" % (_et_hm(nxt), self.cycle_min)
            else:
                self.detail = "first pass at %02d:%02d ET" % (RTH_START.hour, RTH_START.minute)
            return
        if clock.is_trading_day(d) and RTH_END <= t < EOD_AFTER:
            self.detail = "market closed; the end-of-day pass starts %02d:%02d ET" % (
                EOD_AFTER.hour, EOD_AFTER.minute)
            return
        nxt = d if (clock.is_trading_day(d) and t < clock.SESSION_OPEN) else clock.next_trading_day(d)
        eod = "end-of-day %s done; " % self._last_eod if self._last_eod else ""
        self.detail = "%snext session %s" % (eod, nxt.isoformat())

    def _step(self, db, now: _dt.datetime) -> None:
        syms = self._refresh_counts(db, now)
        if not self._ready(now):
            return
        if self._pass is None:
            due = self._pass_due(now, syms)
            if due is not None:
                self._start_pass(db, due[0], due[1], syms, now)
        self._bg_tick(db, now)
        if self._pass is None and self._pass_due(now, syms) is None:
            self._history_tick(db, now, syms)

    def tick(self) -> str:
        """One step of the loop; never raises. Returns the state as reported."""
        now = self._now()
        db = self._sf()
        try:
            try:
                self._restore(db)
                self._collect(db)
                self._step(db, now)
                self._collect(db)              # an inline executor finished everything already
                self._set_state(self._now())
            except Exception as exc:  # noqa: BLE001 - a bug or a DB failure must not stop the loop
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                self.state = "error"
                self.last_error = ("tick failed: %s" % self._err_text(exc))[:500]
                self.detail = self.last_error
                self.log.exception("tick failed")
            self._beat(db)
            return self.shown_state
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def run_forever(self, *, max_ticks: int | None = None) -> None:
        """Tick every ``tick_s`` until Ctrl+C (or ``max_ticks``)."""
        n = 0
        try:
            while True:
                t0 = self._now()
                self.tick()
                n += 1
                if max_ticks is not None and n >= max_ticks:
                    break
                spent = (self._now() - t0).total_seconds()
                self._sleep(max(1.0, self.tick_s - spent))
        except KeyboardInterrupt:
            self.log.info("stopping (interrupted)")
        finally:
            self.stop()

    # ── one-off runs (the CLI) ──

    def _begin_one_off(self, db) -> bool:
        self._retry_at.clear()
        self._restore(db)
        self._refresh_counts(db, self._now())
        self._beat(db)
        return self._ready(self._now(), force=True)

    def _one_off_result(self, out: dict) -> dict:
        scope = self._alert_scope()
        out["ok"] = scope is None
        if scope is not None:
            out["error"] = self._alerts[scope][1]
            out["error_kind"] = self._alerts[scope][0]
        return out

    def _wait(self, db, futures, *, collect) -> None:
        """Wait for ``futures`` while heartbeating every ``HEARTBEAT_EVERY_S``."""
        futures = list(futures)
        while futures:
            cf.wait(futures, timeout=HEARTBEAT_EVERY_S, return_when=cf.ALL_COMPLETED)
            collect()
            futures = [f for f in futures if not f.done()]
            self._set_state(self._now())
            self._beat(db)

    def run_universe(self) -> dict:
        """Refresh the universe now. Returns ``{"ok", "n", "new", "inactive"}`` (+ error)."""
        db = self._sf()
        try:
            if not self._begin_one_off(db):
                return self._one_off_result({"n": 0})
            self.state, self.detail = "universe", "refreshing the universe (one-off)"
            self._beat(db)
            res = self._bg_wrap("universe", self.job_universe)
            self._bg_result("universe", res)
            self._refresh_counts(db, self._now())
            self.state, self.detail = "idle", "universe (one-off): %s" % _plural(self.universe_n, "underlying")
            return self._one_off_result({"n": res.get("n", 0), "new": res.get("new", 0),
                                         "inactive": res.get("inactive", 0)})
        finally:
            self._beat(db)
            db.close()

    def run_once(self, kind: str = "manual") -> dict:
        """One full market pass now, whatever the clock (the universe first when there is
        none). ``kind`` ``manual`` files the session the read describes; ``eod`` files
        the end-of-day pass's session and the day's IV30. Returns ``{"ok", "pass_id",
        "session", "symbols", "read", "failed", "contracts"}`` (+ ``error``); a pass
        stopped by a Massive error is not recorded as finished."""
        out = {"pass_id": None, "session": None, "symbols": 0, "read": 0, "failed": 0, "contracts": 0}
        db = self._sf()
        try:
            if not self._begin_one_off(db):
                return self._one_off_result(out)
            now = self._now()
            syms = scr_store.pass_symbols(db, now=now)
            if not syms:
                res = self._bg_wrap("universe", self.job_universe)
                self._bg_result("universe", res)
                syms = self._refresh_counts(db, self._now())
                if not syms:
                    return self._one_off_result(out)
            session = (eod_day_for(now) or clock.et_today(now)) if kind == "eod" else snapshot_session(now)
            p = self._start_pass(db, kind, session, syms, now)
            out.update(pass_id=p.id, session=session, symbols=len(syms))
            while self._pass is p:
                self._wait(db, list(p.futures), collect=lambda: self._collect_pass(db))
                if self._pass is p and not p.futures:
                    # paused (a Massive error): a one-off stops here
                    self._finish_pass(db, p, finished=False)
            out.update(read=p.ok, failed=p.failed + len(p.queue), contracts=p.contracts)
            self.state, self.detail = "idle", "%s pass %d (one-off): %d read, %d failed" % (
                kind, p.id, p.ok, out["failed"])
            return self._one_off_result(out)
        finally:
            self._beat(db)
            db.close()

    def run_eod(self) -> dict:
        """The end-of-day pass now (``run_once(kind="eod")``)."""
        return self.run_once(kind="eod")

    def run_history(self, symbols) -> dict:
        """The IV history of ``symbols`` now, even when done before (overwrites the stored
        series). Returns ``{"ok", "symbols", "done", "failed"}`` (+ ``error``)."""
        syms: list[str] = []
        for s in symbols or ():
            s = str(s or "").strip().upper()
            if s and s not in syms:
                syms.append(s)
        out = {"symbols": len(syms), "done": 0, "failed": 0}
        db = self._sf()
        try:
            if not self._begin_one_off(db):
                return self._one_off_result(out)
            pool = self._workers_pool()
            futs = {pool.submit(self._history_job, s, True): s for s in syms}
            self._hist.update(futs)
            results: list[dict] = []
            self._wait(db, futs, collect=lambda: results.extend(self._collect_history()))
            results.extend(self._collect_history())
            for r in results:
                out["done" if (r.get("ok") and r.get("done")) else "failed"] += 1
            self.state, self.detail = "idle", "IV history (one-off): %d done, %d failed" % (out["done"], out["failed"])
            return self._one_off_result(out)
        finally:
            self._beat(db)
            db.close()

    def wait_idle(self, timeout: float = 60.0) -> None:
        """Wait for every job in flight (tests and one-offs)."""
        futs = list(self._hist)
        if self._pass is not None:
            futs += list(self._pass.futures)
        if self._bg is not None:
            futs.append(self._bg[1])
        if futs:
            cf.wait(futs, timeout=timeout)

    def stop(self, reason: str = "stopped") -> None:
        """Heartbeat ``stopped``, let the jobs in flight end, close the client this object
        made."""
        self._stopping.set()
        if self._pass is not None:
            self._pass.halt.set()
        for pool in (self._pool, self._bgpool):
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:     # an executor without cancel_futures
                    pool.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
        self._pool = self._bgpool = None
        self.state = "stopped"
        self.detail = reason
        try:
            db = self._sf()
            try:
                self._beat(db)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("final heartbeat failed: %s", exc)
            self._write_state(self._fields(), self._now())
        if self._client_factory is not None and self.client is not None:
            self._close_client(self.client)
            self.client = None


__all__ = ["Collector", "InlineExecutor", "default_client", "contract_rows", "snapshot_session",
           "eod_day_for", "stock_days", "sec_type_of", "exchange_of", "classify_trend",
           "env_cycle_min", "env_workers", "env_max_rps", "env_max_dte", "STATE_PATH",
           "COLLECTOR_VERSION", "NO_KEY_TEXT", "TICK_S", "RTH_START", "RTH_END", "EOD_AFTER"]
