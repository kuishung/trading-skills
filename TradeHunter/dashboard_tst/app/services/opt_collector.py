"""The Hermes options collector (OPTIONS_V2_DESIGN.md §4): the always-on loop that keeps
the shared Options v2 data (``opt_quote``, ``opt_underlying``, ``opt_underlying_daily``)
filled from IB Gateway, so every member sees a chain even with no connector running.

One ``tick()`` (every 15 s) does ONE step, so the heartbeat stays timely:

1. heartbeat -> ``opt_collector_status`` (row 1) and ``state/options_collector.json``
   (the Hermes tray reads the file);
2. connect when not connected (ports ``TST_IBKR_PORT`` or 4002, 4001, 7497, 7496,
   clientId 89, read-only). When the Gateway is down BY DESIGN (see "Living with the
   ingest supervisor" below) the state is ``waiting`` - no error, a retry every 60 s.
   Otherwise state ``error`` and a retry after 60 s, doubling to 5 min;
3. history first: one universe symbol whose ``history_done`` is False gets 2 years of
   daily bars + 1 year of IBKR's 30-day IV, the stats, ``history_done``, then a chain
   read (kind ``history``) - a new basket ticker has data within a minute or two.
   ``history_done`` only when IBKR really returned history (HISTORY_MIN_POINTS bars
   AND IV points, or a young listing confirmed on a later day); an empty answer is a
   failure, retried with the back-off.
   While historical requests are deferred (below) a never-quoted symbol still gets
   its chain read at once; its history follows when allowed;
4. RTH (09:30-16:00 ET on trading days): the next symbol of the current cycle, walked in
   the §4.2 priority (never quoted first, then most held, then stalest), skipping a
   symbol a member (or its first-time read) refreshed in the last 10 min;
5. after 16:15 ET: the next symbol of the day's EOD pass (frozen data first, history
   increments, stats, the EOD snapshot, the earnings date); when the pass ends, the
   retention prune - once per trading day (``last_eod_on``). A pass missed in the
   evening is caught up before the next open;
6. otherwise: history increments that were deferred (a symbol whose newest daily bar
   is older than the last closed session) are caught up, one symbol per tick, when
   historical requests are allowed; else idle.

Living with the ingest supervisor (``scripts/ingest_supervisor.py``, Hermes)
---------------------------------------------------------------------------
The supervisor OWNS the Hermes IB Gateway (paper login, port 4002): it forces it OFF
Mon-Fri 08:00-20:10 ET so it never competes with the user's manual trading on the live
login (IBKR shares market data between the two, but not at the same time), opens it at
20:10 ET for the nightly top-up (historical OHLCV on clientId 84, same login), closes
it again when the top-up is done, and keeps it up across the weekend (Sat 00:00 - Mon
08:00 ET) for seeding. This collector NEVER starts, stops or keeps up the Gateway; it
only reads what the supervisor publishes:

* the schedule - ``RUN_START`` / ``RUN_END`` / ``is_blackout`` / ``session_date`` /
  ``is_weekend_seeding`` imported from the supervisor when it loads, else the same
  rules as constants (``SupervisorWindow``);
* ``state/ingest_supervisor_state.json`` (``last_success_session``, an ET date) and
  ``state/supervisor_heartbeat.json`` (``{ts, action, et}``).

Gateway unreachable while it is down by design = state ``waiting``: inside the
blackout ("members' IBKR connectors carry the session"), the first 15 min after 20:10
ET on a weekday (and after Sat 00:00 ET) while the supervisor starts it, and on a
weekday night once the supervisor's state shows that session's top-up done (it closes
the Gateway then). Anywhere else it is an ``error``.

Historical requests (``daily_bars`` / ``iv_history``: first-time history AND the EOD
increments) share IBKR's per-login pacing (60 per 10 min) with the top-up, so they run
only when no top-up can be running: no supervisor state file on this PC (laptop),
the supervisor's heartbeat older than a day (not running), a weekend session or
weekend daytime, or ``last_success_session`` >= the session that is due (the
supervisor's ``session_date``; in the weekday blackout, the previous weekday's).
Otherwise they wait ("history waits for tonight's ingest top-up"). Chain quotes are
market data, not historical, and are never deferred.

IB access goes through ``fetch`` (``bridge/th_ibkr.py`` in production, a fake in the
tests) and ``connect`` (``ib_connector`` below). ``ib_insync`` is never imported at
module level: it cannot be imported on Python 3.14, where the web app and the tests
run. Every database write goes through ``opt_store``; one symbol's failure is written
to ``opt_refresh_log`` (``error``) and the loop moves on.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import importlib.util
import inspect
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

from . import clock, opt_store

COLLECTOR_VERSION = "1.1"

TICK_S = 15.0                    # §4: one step every 15 s
HEARTBEAT_EVERY_S = 15.0         # an unchanged status is re-written at most this often
CONNECT_RETRY_S = 60.0           # §4 step 2: retry the gateway after 60 s ...
CONNECT_RETRY_MAX_S = 300.0      # ... doubling to at most 5 min
WAIT_RETRY_S = 60.0              # Gateway down by design (state waiting): a try every 60 s, no back-off
GATEWAY_START_GRACE_S = 900.0    # the first 15 min after the supervisor opens the Gateway = waiting
MEMBER_FRESH_S = 600.0           # §4.2: a member refresh within 10 min skips the symbol this cycle
MIN_CYCLE_S = 600.0              # a new cycle starts at most every 10 min (a small basket
                                 # would otherwise re-read nonstop and hold market-data lines)
HISTORY_RETRY_S = 1800.0         # a failed history pull is retried after 30 min ...
HISTORY_RETRY_MAX_S = 6 * 3600.0 # ... doubling to 6 h
HISTORY_MIN_POINTS = 20          # a first-time pull is the symbol's history only with at least
                                 # this many daily bars AND IV points: ib_insync answers a failed
                                 # historical request (pacing, HMDS down, its own timeout) with
                                 # an EMPTY list, not an error. Fewer but not zero = a young
                                 # listing, filed once a pull on a LATER day returns as much again.
STATE_LOG_EVERY_S = 1800.0       # a persisting waiting / error state is logged when it begins,
                                 # then at most once per 30 min (the Gateway is down by design
                                 # most of every weekday and is tried every minute)
EOD_AFTER = _dt.time(16, 15)     # §4 step 5
RTH_PREF = (1, 2, 3, 4)          # live first in the session
OFF_HOURS_PREF = (2, 1, 4, 3)    # frozen first after the close (§4 step 5)
HISTORY_BARS, HISTORY_IV = "2 Y", "1 Y"
EOD_BARS, EOD_IV = "5 D", "1 M"

IB_HOST = "127.0.0.1"
IB_PORT_CANDIDATES = (4002, 4001, 7497, 7496)   # Gateway paper / live, TWS paper / live
DEFAULT_CLIENT_ID = 89                          # CLAUDE.md clientId table
DEFAULT_MAX_LINES = 60

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "options_collector.json"

# The ingest supervisor (TradeHunter/scripts/ingest_supervisor.py) and what it publishes
# in TradeHunter/state/ (per-PC, gitignored). Only read here, never written.
TRADEHUNTER_ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR_SCRIPT = TRADEHUNTER_ROOT / "scripts" / "ingest_supervisor.py"
SUPERVISOR_STATE_DIR = TRADEHUNTER_ROOT / "state"
SUPERVISOR_STATE_FILE = "ingest_supervisor_state.json"    # {"last_success_session": "YYYY-MM-DD", ...}
SUPERVISOR_HEARTBEAT_FILE = "supervisor_heartbeat.json"   # {"ts": UTC ISO, "action", "et"} each tick
SUPERVISOR_STALE_S = 24 * 3600.0  # no supervisor tick for a day = it is not running on this PC.
                                  # (Its heartbeat stops during a blocking top-up + deep check,
                                  # which end by 08:00 ET - at most ~13 h.)
HISTORY_WAIT_TEXT = "history waits for tonight's ingest top-up"
BLACKOUT_TEXT = ("Gateway off for the manual-trading blackout until %s ET - "
                 "members' IBKR connectors carry the session")

# The supervisor's schedule when its module cannot be loaded (same values, ET wall clock)
_RUN_START_DEFAULT = _dt.time(20, 10)    # 10 min after the 20:00 ET extended close
_RUN_END_DEFAULT = _dt.time(8, 0)        # 90 min before the 09:30 ET open
_MARGIN_MIN_DEFAULT = 3                  # it closes the Gateway this long before RUN_END

# Ceilings (s) per IB call: a stalled request must never pin the loop. th_ibkr has its
# own shorter waits; these only catch a hang.
T_CONNECT = 120.0
T_DEFS = 60.0
T_SPOT = 30.0
T_HIST = 180.0
T_QUOTE = 1800.0

_MDT_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}


# ────────────────────────────────── settings ──────────────────────────────────

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def ib_ports() -> tuple[int, ...]:
    """``TST_IBKR_PORT`` alone when set and numeric, else the probe order."""
    raw = os.environ.get("TST_IBKR_PORT", "").strip()
    try:
        port = int(raw) if raw else 0
    except ValueError:
        port = 0
    return (port,) if port > 0 else IB_PORT_CANDIDATES


def env_client_id() -> int:
    """``TST_OPTIONS_COLLECTOR_CLIENT_ID``, default 89."""
    return _env_int("TST_OPTIONS_COLLECTOR_CLIENT_ID", DEFAULT_CLIENT_ID, 0, 2 ** 31 - 1)


def env_max_lines() -> int:
    """``TST_OPTIONS_MAX_LINES`` (concurrent market-data lines per wave), default 60,
    kept within 1-100 (a login's allowance is 100, shared by every client)."""
    return _env_int("TST_OPTIONS_MAX_LINES", DEFAULT_MAX_LINES, 1, 100)


def ib_connector(*, host: str = IB_HOST, ports=None, client_id=None, timeout: float = 10.0,
                 request_timeout: float = 60.0, log=None):
    """A ``connect`` for ``Collector``: an async function that tries each port in turn
    with ``ib_insync.IB().connectAsync`` (read-only), the port that last worked first,
    and returns ``(ib, "host:port")``. Raises ConnectionError naming every port tried.

    Connecting IS the probe (no bare socket probe: a socket opened and closed on the
    API port makes the Gateway log "client disconnected before version was sent").
    A refused port fails at once. ``ib_insync`` is imported on the first call."""
    order = tuple(ports or ib_ports())
    cid = env_client_id() if client_id is None else int(client_id)
    lg = log or logging.getLogger(__name__)
    last = {"port": None}

    async def connect():
        from ib_insync import IB  # noqa: PLC0415 - Python 3.12 only (see the module docstring)

        tried = []
        for port in sorted(order, key=lambda p: p != last["port"]):
            ib = IB()
            try:
                ib.RequestTimeout = request_timeout
            except Exception:  # noqa: BLE001
                pass
            try:
                await ib.connectAsync(host, port, clientId=cid, readonly=True, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - refused, timed out, clientId in use
                tried.append("%d (%s)" % (port, str(exc) or type(exc).__name__))
                try:
                    ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                continue
            if ib.isConnected():
                last["port"] = port
                lg.info("IB API connected on %s:%d (clientId %d)", host, port, cid)
                return ib, "%s:%d" % (host, port)
            tried.append("%d (not connected)" % port)
        raise ConnectionError("IB Gateway / TWS not reachable on %s with clientId %d - tried %s"
                              % (host, cid, ", ".join(tried) or "no port"))

    return connect


# ────────────────────────────────── helpers ──────────────────────────────────

def _pos(v) -> float | None:
    """A finite positive float, else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if (math.isfinite(f) and f > 0) else None


def _mdt_name(v) -> str | None:
    """A market data type as its name (names and IBKR codes 1-4 accepted)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return _MDT_NAMES.get(v)
    s = str(v or "").strip().lower().replace("-", "_")
    if s.isdigit():
        return _MDT_NAMES.get(int(s))
    return s if s in opt_store.MDT else None


def _iso(t) -> str | None:
    """A naive-UTC datetime as an ISO string with its offset (what the tray parses)."""
    if t is None:
        return None
    if isinstance(t, _dt.datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return t.isoformat()
    return str(t)


def _et_hm(t: _dt.datetime) -> str:
    return clock.et_now(t).strftime("%H:%M") + " ET"


def _err_text(exc) -> str:
    return (str(exc) or type(exc).__name__)[:500]


def _default_earnings(symbol: str):
    """The earnings date from the free source (§2.1: the one non-IBKR figure)."""
    from . import prices  # noqa: PLC0415 - imported on use (httpx client)

    return prices.fetch_next_earnings(symbol)


def _hm(t: _dt.time) -> str:
    return "%02d:%02d" % (t.hour, t.minute)


def _secs(t: _dt.time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _prev_weekday(d: _dt.date) -> _dt.date:
    d -= _dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= _dt.timedelta(days=1)
    return d


def _parse_utc(v) -> _dt.datetime | None:
    """An ISO timestamp as naive UTC (an offset-less one is read as UTC), else None."""
    try:
        t = _dt.datetime.fromisoformat(str(v or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t


# ────────────────────────────────── the ingest supervisor's schedule ──────────────────────────────────

_SUP_MODULE: dict = {}


def load_supervisor():
    """``scripts/ingest_supervisor.py`` as a module (loaded once per process), or None
    when it is missing or does not load (e.g. no zone database). The module is loaded
    from its file under a private name with the TradeHunter root on ``sys.path`` for
    the duration of the import only - its own bootstrap prepends three folders to
    ``sys.path``, which are taken off again so nothing in the app can be shadowed."""
    if "mod" in _SUP_MODULE:
        return _SUP_MODULE["mod"]
    mod = None
    for name in ("scripts.ingest_supervisor", "ingest_supervisor"):
        m = sys.modules.get(name)
        if m is not None and callable(getattr(m, "is_blackout", None)):
            mod = m
            break
    if mod is None and SUPERVISOR_SCRIPT.is_file():
        saved = list(sys.path)
        try:
            if str(TRADEHUNTER_ROOT) not in sys.path:
                sys.path.insert(0, str(TRADEHUNTER_ROOT))
            spec = importlib.util.spec_from_file_location("_th_ingest_supervisor", SUPERVISOR_SCRIPT)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            mod = m
        except Exception as exc:  # noqa: BLE001 - fall back to the built-in constants
            logging.getLogger(__name__).info("ingest_supervisor not loadable (%s) - using the "
                                             "built-in 08:00-20:10 ET window", _err_text(exc))
            mod = None
        finally:
            sys.path[:] = saved
    _SUP_MODULE["mod"] = mod
    return mod


class SupervisorWindow:
    """The ingest supervisor's Gateway schedule in ET wall-clock terms, from its module
    (``RUN_START`` / ``RUN_END`` / ``DEADLINE_MARGIN_MIN`` / ``is_blackout`` /
    ``session_date`` / ``is_weekend_seeding``) when it loads, else the same rules as
    constants: run window 20:10-08:00 ET, blackout 08:00-20:10 ET Mon-Fri only (the
    weekend - Sat 00:00 to Mon 08:00 ET - is seeding, Gateway up, no blackout).

    ``supervisor``: a module-like object, None for the built-in rules, or omitted to
    load ``scripts/ingest_supervisor.py``. Every method takes an AWARE ET datetime."""

    _UNSET = object()

    def __init__(self, supervisor=_UNSET):
        sup = load_supervisor() if supervisor is SupervisorWindow._UNSET else supervisor
        self.source = "scripts/ingest_supervisor.py" if sup is not None else "built-in"
        rs, re_ = getattr(sup, "RUN_START", None), getattr(sup, "RUN_END", None)
        self.run_start = rs if isinstance(rs, _dt.time) else _RUN_START_DEFAULT
        self.run_end = re_ if isinstance(re_, _dt.time) else _RUN_END_DEFAULT
        m = getattr(sup, "DEADLINE_MARGIN_MIN", None)
        self.margin_min = m if (isinstance(m, int) and not isinstance(m, bool) and 0 <= m < 60) \
            else _MARGIN_MIN_DEFAULT
        self._is_blackout = getattr(sup, "is_blackout", None) if sup is not None else None
        self._session_date = getattr(sup, "session_date", None) if sup is not None else None
        self._seeding = getattr(sup, "is_weekend_seeding", None) if sup is not None else None

    def blackout_time(self, t: _dt.time) -> bool:
        """The supervisor's ``is_blackout(t)``: 08:00 <= t < 20:10 (any day)."""
        if callable(self._is_blackout):
            try:
                return bool(self._is_blackout(t))
            except Exception:  # noqa: BLE001
                pass
        return not (t >= self.run_start or t < self.run_end)

    def seeding(self, et: _dt.datetime) -> bool:
        """The weekend span Sat 00:00 - Mon 08:00 ET (Gateway kept up, no blackout)."""
        if callable(self._seeding):
            try:
                return bool(self._seeding(et))
            except Exception:  # noqa: BLE001
                pass
        wd = et.weekday()
        return wd >= 5 or (wd == 0 and et.time() < self.run_end)

    def blackout(self, et: _dt.datetime) -> bool:
        """The weekday manual-trading blackout (Mon-Fri 08:00-20:10 ET): Gateway OFF."""
        return (not self.seeding(et)) and self.blackout_time(et.time())

    def session(self, et: _dt.datetime) -> _dt.date | None:
        """The supervisor's ``session_date``: the ET date the evening run belongs to
        (>= 20:10 today, < 08:00 yesterday), None in 08:00-20:10."""
        if callable(self._session_date):
            try:
                got = self._session_date(et)
                if got is None or isinstance(got, _dt.date):
                    return got
            except Exception:  # noqa: BLE001
                pass
        t = et.time()
        if t >= self.run_start:
            return et.date()
        if t < self.run_end:
            return et.date() - _dt.timedelta(days=1)
        return None

    def session_due(self, et: _dt.datetime) -> _dt.date | None:
        """The session whose top-up must be done before historical requests may run,
        or None when no top-up is due (a weekend session, weekend daytime): the
        supervisor's session; in the weekday blackout, the previous weekday's (the
        last top-up, which ended by 08:00 ET)."""
        sess = self.session(et)
        if sess is None:
            if et.weekday() >= 5:
                return None
            sess = _prev_weekday(et.date())
        return sess if sess.weekday() < 5 else None


# ────────────────────────────────── the collector ──────────────────────────────────

class Collector:
    """The §4 loop. ``tick()`` does one step; ``run_forever()`` ticks every 15 s.

    ``session_factory()`` returns a SQLAlchemy session (``app.db.SessionLocal``);
    ``fetch`` has th_ibkr's functions (``chain_defs``, ``plan``, ``spot``, ``quote``,
    ``daily_bars``, ``iv_history``; async or plain); ``connect()`` returns ``(ib,
    "host:port")`` or ``ib`` (async or plain) and raises when the gateway is not
    reachable. ``clock()`` returns now (naive = UTC); ``sleep(s)`` waits between ticks
    (default: runs the event loop for ``s`` so ib_insync keeps reading the socket).
    ``loop`` is the event loop ib_insync was connected on (the CLI makes one); the
    collector makes its own when none is given. ``earnings(symbol)`` returns
    ``{"date": ...}`` or None (default ``prices.fetch_next_earnings``).
    ``window`` is the supervisor's schedule (default ``SupervisorWindow()``);
    ``supervisor_dir`` the folder holding its state + heartbeat files (default
    ``TradeHunter/state``)."""

    def __init__(self, session_factory, *, fetch, connect, clock=None, sleep=None, log=None,
                 state_path=None, loop=None, earnings=None, max_lines=None,
                 tick_s: float = TICK_S, min_cycle_s: float = MIN_CYCLE_S,
                 window=None, supervisor_dir=None):
        self._sf = session_factory
        self.fetch = fetch
        self._connect = connect
        self._clock = clock
        self._sleep = sleep
        self.log = log or logging.getLogger(__name__)
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self._loop = loop
        self._own_loop = False
        self._earnings = earnings
        self.max_lines = int(max_lines) if max_lines else env_max_lines()
        self.tick_s = float(tick_s)
        self.min_cycle_s = float(min_cycle_s)
        th_version = getattr(fetch, "VERSION", None)
        self.version = (COLLECTOR_VERSION + ("+th%s" % th_version if th_version else ""))[:16]
        self.pid = os.getpid()
        # the ingest supervisor (read only)
        self.window = window if window is not None else SupervisorWindow()
        self.supervisor_dir = Path(supervisor_dir) if supervisor_dir else SUPERVISOR_STATE_DIR
        self._sup_last_good: dict | None = None
        # connection
        self.ib = None
        self.gateway: str | None = None
        self.gateway_ok = False
        self._next_connect: _dt.datetime | None = None
        self._connect_fails = 0
        self._down_since: _dt.datetime | None = None
        self._last_connect_error: str | None = None
        self.wait_reason: str | None = None      # blackout | starting | closed (state waiting)
        self.wait_until: str | None = None       # "20:10 ET" - when the Gateway is due back
        # status (what the heartbeat carries)
        self.state = "starting"
        self.detail = "starting"
        self.mdt: str | None = None
        self.cycle_n = 0
        self.cycle_started: _dt.datetime | None = None
        self.cycle_finished: _dt.datetime | None = None
        self.symbols_total = 0
        self.symbols_done = 0
        self.last_eod_on: str | None = None
        self.last_error: str | None = None
        self.universe_n = 0
        # work
        self._cycle: list[str] | None = None        # symbols left in the running cycle
        self._eod: tuple[str, list[str]] | None = None   # (ET day, symbols left) of the EOD pass
        self._hist_retry: dict[str, tuple[int, _dt.datetime]] = {}
        self._hist_short: dict[str, tuple[str, int, int]] = {}  # symbol -> (ET day, bars, IV points)
        self._state_log: tuple | None = None              # (state key, when) of the last state line
        self._first_tried: dict[str, _dt.datetime] = {}  # chain-only first reads (history deferred)
        self._inc_done: dict[str, tuple[str, _dt.datetime | None]] = {}  # symbol -> (day, retry at)
        self._inc_run: tuple[str, int] | None = None      # (day, symbols) of the increments catch-up
        self.history_waiting = 0                          # symbols whose history waits for the top-up
        self._defs: dict[str, tuple[str, dict]] = {}     # symbol -> (ET day, chain_defs)
        self._restored = False
        self._db = None
        self._last_beat: _dt.datetime | None = None
        self._last_sig = None
        self._db_warned = False
        self._file_warned = False

    # ── clock + event loop ──

    def _now(self) -> _dt.datetime:
        t = self._clock() if self._clock is not None else _dt.datetime.now(_dt.timezone.utc)
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t

    def _et_day(self, now: _dt.datetime) -> str:
        return clock.et_today(now)

    def _in_session(self, now: _dt.datetime) -> bool:
        return clock.us_session_open(now)

    def _pref(self, now: _dt.datetime) -> tuple:
        return RTH_PREF if self._in_session(now) else OFF_HOURS_PREF

    def _get_loop(self):
        """The loop IB calls run on: the one given (ib_insync was connected on it), else
        a private one - not installed as the thread's current loop."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            self._own_loop = True
        return self._loop

    def _call(self, fn, *args, timeout: float | None = None, **kw):
        """Call ``fn``; an awaitable result is run on the event loop (with a ceiling)."""
        res = fn(*args, **kw)
        if inspect.isawaitable(res):
            aw = asyncio.wait_for(res, timeout) if timeout else res
            return self._get_loop().run_until_complete(aw)
        return res

    def _do_sleep(self, seconds: float) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
            return
        loop = self._loop
        if loop is not None and not loop.is_closed() and not loop.is_running():
            # run the loop meanwhile so ib_insync keeps reading the socket (disconnects,
            # error messages) - what ib.sleep() does
            loop.run_until_complete(asyncio.sleep(seconds))
        else:
            time.sleep(seconds)

    # ── database + status ──

    def _rollback(self, db) -> None:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass

    def _restore(self, db) -> None:
        """``last_eod_on`` and ``cycle_n`` survive a restart (the status row)."""
        if self._restored:
            return
        self._restored = True
        try:
            st = opt_store.collector_status(db)
        except Exception as exc:  # noqa: BLE001 - e.g. the table is missing before a migration
            self._rollback(db)
            self.log.warning("could not read the collector status row: %s", exc)
            return
        if st:
            self.last_eod_on = st.get("last_eod_on") or None
            try:
                self.cycle_n = int(st.get("cycle_n") or 0)
            except (TypeError, ValueError):
                self.cycle_n = 0

    def _fields(self) -> dict:
        return {"state": self.state, "phase_detail": self.detail, "gateway": self.gateway,
                "gateway_ok": bool(self.gateway_ok), "mdt": self.mdt, "cycle_n": self.cycle_n,
                "cycle_started": self.cycle_started, "cycle_finished": self.cycle_finished,
                "symbols_total": self.symbols_total, "symbols_done": self.symbols_done,
                "last_eod_on": self.last_eod_on, "last_error": self.last_error,
                "pid": self.pid, "version": self.version}

    def _extras(self) -> dict:
        """State-file-only fields (no status-row column): why the collector waits and
        until when, and how many symbols' history waits for the ingest top-up."""
        return {"wait_reason": self.wait_reason if self.state == "waiting" else None,
                "wait_until": self.wait_until if self.state == "waiting" else None,
                "history_waiting": int(self.history_waiting or 0)}

    def _beat(self, db, *, force: bool = False, throttle_only: bool = False) -> None:
        """Write the heartbeat (status row + state file) when forced, when the status
        changed (unless ``throttle_only``) or when the last one is 15 s old."""
        now = self._now()
        fields = self._fields()
        sig = tuple((k, str(v)) for k, v in sorted(dict(fields, **self._extras()).items()))
        due = self._last_beat is None or (now - self._last_beat).total_seconds() >= HEARTBEAT_EVERY_S
        if not (force or due or (not throttle_only and sig != self._last_sig)):
            return
        self._last_beat, self._last_sig = now, sig
        if db is not None:
            try:
                opt_store.set_collector_status(db, heartbeat=now, **fields)
                self._db_warned = False
            except Exception as exc:  # noqa: BLE001 - the file still carries the heartbeat
                self._rollback(db)
                if not self._db_warned:
                    self.log.warning("could not write the collector status row: %s", exc)
                    self._db_warned = True
        self._write_state(dict(fields, heartbeat=now))

    def _write_state(self, fields: dict) -> None:
        """``state/options_collector.json`` for the Hermes tray, replaced atomically.
        The first write into a ``state`` folder also drops a ``.gitignore`` there
        (``*``) so the runtime file never shows up in git."""
        doc = {k: (_iso(v) if isinstance(v, _dt.datetime) else v) for k, v in fields.items()}
        doc.update(self._extras())
        doc["universe"] = self.universe_n
        doc["down_since"] = _iso(self._down_since)
        doc["next_connect"] = _iso(self._next_connect)
        doc["written_by"] = "dashboard_tst/app/services/opt_collector.py"
        p = self.state_path
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.parent.name == "state":
                gi = p.parent / ".gitignore"
                if not gi.exists():
                    gi.write_text("# runtime state (options collector heartbeat) - never committed\n*\n",
                                  encoding="utf-8")
            text = json.dumps(doc, indent=1, sort_keys=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                os.replace(tmp, p)
            except PermissionError:          # the tray had it open this instant (Windows)
                p.write_text(text, encoding="utf-8")
                try:
                    tmp.unlink()
                except OSError:
                    pass
            self._file_warned = False
        except Exception as exc:  # noqa: BLE001
            if not self._file_warned:
                self.log.warning("could not write %s: %s", p, exc)
                self._file_warned = True

    # ── the ingest supervisor ──

    def _supervisor_state(self, now: _dt.datetime) -> dict | None:
        """What the ingest supervisor publishes on this PC, or None when it does not
        run here: no ``ingest_supervisor_state.json`` (laptop / another PC), or its
        heartbeat file says it has not ticked for a day. Returns
        ``{"last_success_session": date | None, "heartbeat": naive UTC | None,
        "action": str | None}``. A state file caught mid-write keeps the last good read."""
        d = self.supervisor_dir
        p = d / SUPERVISOR_STATE_FILE
        if not p.is_file():
            return None
        hb, action = None, None
        try:
            h = json.loads((d / SUPERVISOR_HEARTBEAT_FILE).read_text(encoding="utf-8"))
            if isinstance(h, dict):
                hb, action = _parse_utc(h.get("ts")), h.get("action")
        except (OSError, ValueError):
            pass
        if hb is not None and (now - hb).total_seconds() > SUPERVISOR_STALE_S:
            return None
        last = None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            v = raw.get("last_success_session")
            last = _dt.date.fromisoformat(str(v)[:10]) if v else None
            self._sup_last_good = {"last_success_session": last}
        except (OSError, ValueError):
            last = (self._sup_last_good or {}).get("last_success_session")
        return {"last_success_session": last, "heartbeat": hb, "action": action}

    def _history_gate(self, now: _dt.datetime) -> tuple[bool, str | None]:
        """(may historical requests run now, why not). They share IBKR's per-login
        pacing with the supervisor's nightly top-up, so they wait until no top-up can
        be running: allowed with no supervisor on this PC, when no top-up is due
        (weekend), or when ``last_success_session`` >= the session that is due."""
        sup = self._supervisor_state(now)
        if sup is None:
            return True, None
        due = self.window.session_due(clock.et_now(now))
        if due is None:
            return True, None
        last = sup.get("last_success_session")
        if last is not None and last >= due:
            return True, None
        return False, HISTORY_WAIT_TEXT

    def _gateway_off_by_design(self, now: _dt.datetime) -> tuple[str, str, str] | None:
        """``(reason, until, phase_detail)`` when the supervisor keeps the Gateway down
        right now, else None (then an unreachable Gateway is an error):

        * ``blackout`` - Mon-Fri 08:00-20:10 ET (and its last minutes before 08:00, when
          the supervisor already shuts it);
        * ``starting`` - the first 15 min after it opens the Gateway (20:10 ET on a
          weekday evening, Sat 00:00 ET for weekend seeding);
        * ``closed`` - a weekday night after that session's top-up is done (the
          supervisor closes the Gateway until the next 20:10 ET, Sat 00:00 after a Friday)."""
        w = self.window
        et = clock.et_now(now)
        t, wd = et.time(), et.weekday()
        rs = _hm(w.run_start)
        if w.blackout(et):
            return "blackout", rs + " ET", BLACKOUT_TEXT % rs
        seeding = w.seeding(et)
        if not seeding and t < w.run_end and _secs(w.run_end) - _secs(t) <= w.margin_min * 60:
            return "blackout", rs + " ET", BLACKOUT_TEXT % rs       # it is already being shut
        grace_end = _hm((_dt.datetime.min + _dt.timedelta(
            seconds=_secs(w.run_start) + GATEWAY_START_GRACE_S)).time())
        since_open = _secs(t) - _secs(w.run_start)
        if not seeding and wd < 5 and 0 <= since_open < GATEWAY_START_GRACE_S:
            return ("starting", grace_end + " ET",
                    "the ingest supervisor opens the Gateway at %s ET - waiting for it until %s ET"
                    % (rs, grace_end))
        if seeding and wd == 5 and _secs(t) < GATEWAY_START_GRACE_S:
            until = _hm((_dt.datetime.min + _dt.timedelta(seconds=GATEWAY_START_GRACE_S)).time())
            return ("starting", until + " ET",
                    "the ingest supervisor opens the Gateway for weekend seeding at 00:00 ET - "
                    "waiting for it until %s ET" % until)
        if not seeding:
            sess = w.session(et)
            if sess is not None and sess.weekday() < 5:
                last = (self._supervisor_state(now) or {}).get("last_success_session")
                if last is not None and last >= sess:
                    back = "00:00 ET" if sess.weekday() == 4 else rs + " ET"   # Friday -> weekend seeding
                    return ("closed", back,
                            "Gateway closed by the ingest supervisor after tonight's top-up - "
                            "it opens again at %s%s" % (back, " Saturday" if sess.weekday() == 4 else ""))
        return None

    def _log_state(self, key: tuple, level: int, msg: str, *args) -> None:
        """Log a waiting / error state line when the state (``key``: state + reason)
        begins or changes, then at most once per STATE_LOG_EVERY_S while it persists -
        never one line per retry."""
        now = self._now()
        last = self._state_log
        if (last is not None and last[0] == key
                and (now - last[1]).total_seconds() < STATE_LOG_EVERY_S):
            return
        self._state_log = (key, now)
        self.log.log(level, msg, *args)

    def _set_waiting(self, why: tuple[str, str, str]) -> None:
        """State ``waiting``: the Gateway is down by design. No error is recorded."""
        reason, until, text = why
        self._log_state(("waiting", reason), logging.INFO,
                        "IB Gateway down by design (%s) - %s", reason, text)
        if (self._down_since is not None and self.last_error
                and self.last_error.startswith(("gateway:", "IB connection lost"))):
            # the outage it described is now explained (e.g. the supervisor closed the
            # Gateway after the top-up a minute before writing its state file)
            self.last_error = None
        self.state = "waiting"
        self.gateway_ok = False
        self.wait_reason, self.wait_until = reason, until
        self.detail = text
        self._down_since = None
        self._connect_fails = 0

    # ── connection ──

    def _ib_alive(self) -> bool:
        if self.ib is None:
            return False
        try:
            return bool(self.ib.isConnected())
        except Exception:  # noqa: BLE001
            return False

    def _down_text(self) -> str:
        if self._down_since is None:
            return "gateway not reachable"
        return "gateway down since %s" % _et_hm(self._down_since)

    def _drop(self, reason: str) -> None:
        ib, self.ib = self.ib, None
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.gateway_ok = False
        now = self._now()
        why = self._gateway_off_by_design(now)
        if why is not None:            # e.g. the supervisor shut it at 08:00 ET: expected
            self.log.info("IB connection closed (%s)", reason)
            self._set_waiting(why)
            return
        self._down_since = self._down_since or now
        self.state = "error"
        self.detail = "IB connection lost: %s" % reason
        self.last_error = self.detail[:500]
        self._log_state(("error", "lost"), logging.WARNING, "IB connection lost (%s)", reason)

    def _ensure_connected(self, now: _dt.datetime, *, force: bool = False) -> bool:
        """Connected, or one attempt when the retry time has come (``force``: now).
        Not connected while the Gateway is down by design = state ``waiting`` (a try
        every 60 s, nothing recorded as an error); otherwise state ``error``."""
        if self._ib_alive():
            return True
        if self.ib is not None:
            self._drop("the gateway closed the connection")
        why = self._gateway_off_by_design(now)
        was_waiting = self.state == "waiting"
        if (not force and self._next_connect is not None and now < self._next_connect
                and not (was_waiting and why is None)):     # a waiting window that ended: try now
            if why is not None:
                self._set_waiting(why)
            else:
                self.state = "error"
                self.gateway_ok = False
                self.detail = "%s; next try %s" % (self._down_text(), _et_hm(self._next_connect))
            return False
        try:
            got = self._call(self._connect, timeout=T_CONNECT)
        except Exception as exc:  # noqa: BLE001
            self._last_connect_error = ("gateway: " + _err_text(exc))[:500]
            why = self._gateway_off_by_design(now)
            if why is not None:
                self._next_connect = now + _dt.timedelta(seconds=WAIT_RETRY_S)
                self._set_waiting(why)
                return False
            self._connect_fails += 1
            wait = min(CONNECT_RETRY_S * 2 ** (self._connect_fails - 1), CONNECT_RETRY_MAX_S)
            self._next_connect = now + _dt.timedelta(seconds=wait)
            self._down_since = self._down_since or now
            self.gateway_ok = False
            self.state = "error"
            self.last_error = ("gateway: " + _err_text(exc))[:500]
            self.detail = "%s; retry in %d s" % (self._down_text(), int(wait))
            self._log_state(("error", "connect"), logging.WARNING,
                            "IB connect failed (%d in a row): %s - retry in %d s",
                            self._connect_fails, _err_text(exc), int(wait))
            return False
        ib, label = got if isinstance(got, tuple) else (got, getattr(got, "th_gateway", None))
        self.ib = ib
        self.gateway = label or self.gateway
        self.gateway_ok = True
        self._connect_fails = 0
        self._next_connect = None
        self._down_since = None
        self._last_connect_error = None
        self.wait_reason = self.wait_until = None
        self._state_log = None                 # the next waiting / error state logs at once
        reset = getattr(self.fetch, "reset_mdt", None)
        if callable(reset):       # a new session may have another entitlement
            try:
                reset()
            except Exception:  # noqa: BLE001
                pass
        self.log.info("connected to IB at %s", self.gateway or "?")
        return True

    # ── one symbol ──

    def _fail(self, db, sym: str, kind: str, exc, pref: tuple) -> None:
        """Record one symbol's failure: ``opt_refresh_log`` (``error``, no contracts),
        ``last_error``, the log. Drops the connection when it died under the call."""
        msg = _err_text(exc)
        dead = self.ib is not None and not self._ib_alive()
        if dead and self._gateway_off_by_design(self._now()) is not None:
            # the supervisor shut the Gateway under the call (e.g. 08:00 ET): expected
            self.log.info("%s %s cut off - the Gateway closed by design: %s", kind, sym, msg)
        else:
            self.last_error = ("%s %s: %s" % (kind, sym, msg))[:500]
            self.log.warning("%s %s failed: %s", kind, sym, msg)
        self._rollback(db)
        try:
            opt_store.upsert_quotes(db, sym, [], source="hermes", mdt=_MDT_NAMES[pref[0]],
                                    kind=kind, as_of=self._now(), error=msg)
        except Exception as e2:  # noqa: BLE001
            self._rollback(db)
            self.log.warning("could not log the %s failure of %s: %s", kind, sym, e2)
        if self.ib is not None and not self._ib_alive():
            self._drop(msg)

    def _chain_defs(self, sym: str, day: str) -> dict:
        """``chain_defs`` once per symbol per ET day (the listed expiries / strikes)."""
        hit = self._defs.get(sym)
        if hit is not None and hit[0] == day:
            return hit[1]
        defs = self._call(self.fetch.chain_defs, self.ib, sym, timeout=T_DEFS)
        for k in [k for k, v in self._defs.items() if v[0] != day]:
            self._defs.pop(k, None)
        self._defs[sym] = (day, defs)
        return defs

    def _progress(self, base: str):
        """A ``quote`` progress callback: keeps the heartbeat fresh during a long read."""
        def cb(done, total):
            self.detail = "%s - %s/%s contracts" % (base, done, total)
            if self._db is not None:
                try:
                    self._beat(self._db, throttle_only=True)
                except Exception:  # noqa: BLE001
                    pass
        return cb

    def _read_chain(self, db, sym: str, *, kind: str, pref: tuple) -> bool:
        """``chain_defs`` -> ``spot`` -> ``plan`` -> ``quote`` -> ``upsert_quotes``
        (source hermes) -> the stock price. True when quotes were stored."""
        t0 = self._now()
        base = self.detail
        und: dict = {}
        try:
            day = self._et_day(t0)
            und = opt_store.underlying(db, sym) or {}
            defs = self._chain_defs(sym, day)
            try:
                s = self._call(self.fetch.spot, self.ib, sym, mdt_pref=pref, timeout=T_SPOT) or {}
            except Exception as exc:  # noqa: BLE001 - plan from the stored price instead
                if self.ib is not None and not self._ib_alive():
                    raise
                self.log.info("%s: no IBKR stock price (%s) - planning from the stored one",
                              sym, _err_text(exc))
                s = {}
            px = _pos(s.get("spot"))
            fresh_spot = px is not None
            if px is None:
                px = _pos(und.get("spot"))
            if px is None:
                raise RuntimeError("no stock price from IBKR and none stored")
            iv30 = _pos(und.get("iv30"))
            window = self._call(self.fetch.plan, defs, spot=px,
                                iv_hint=(round(iv30 / 100.0, 4) if iv30 else None), today=day)
            if not window:
                raise RuntimeError("no listed expiry inside the fetch window")
            res = self._call(self.fetch.quote, self.ib, sym, window, max_lines=self.max_lines,
                             mdt_pref=pref, progress=self._progress(base), spot=px,
                             timeout=T_QUOTE) or {}
        except Exception as exc:  # noqa: BLE001 - one symbol never stops the loop
            self.detail = base
            self._fail(db, sym, kind, exc, pref)
            return False
        self.detail = base
        rows = list(res.get("rows") or [])
        mdt = _mdt_name(res.get("mdt")) or _MDT_NAMES[pref[0]]
        stamp = self._now()
        err = None if rows else "IBKR returned no quotes (%s contracts requested)" % res.get("requested", "?")
        out = opt_store.upsert_quotes(db, sym, rows, source="hermes", mdt=mdt, kind=kind,
                                      ms=res.get("ms"), as_of=stamp, error=err)
        if fresh_spot:
            opt_store.set_spot(db, sym, px, source="hermes",
                               mdt=_mdt_name(s.get("mdt")) or mdt, as_of=stamp)
        self.mdt = mdt
        if err:
            self.last_error = ("%s %s: %s" % (kind, sym, err))[:500]
        self.log.info("%s %s: %d stored, %d older kept, %d expiries, %s, %.0f s",
                      kind, sym, out.get("stored", 0), out.get("skipped_older", 0),
                      out.get("n_expiries", 0), mdt, (stamp - t0).total_seconds())
        return bool(rows)

    def _history_verdict(self, sym: str, n_bars: int, n_iv: int, day: str) -> str | None:
        """None when a first-time pull counts as the symbol's history, else why not
        (the pull is then a failure, retried with the history back-off):

        * at least HISTORY_MIN_POINTS daily bars AND IV points - history;
        * no bars or no IV points at all - IBKR's answer to a failed request (pacing,
          HMDS busy or down over the weekend reset, ib_insync's own timeout), never
          history: marking it done would leave the symbol with no IV rank / ATR for good;
        * fewer but not zero - a young listing, or a cut-short answer. Filed once a pull
          on a LATER ET day returns at least as much again (a young listing only grows)."""
        if n_bars >= HISTORY_MIN_POINTS and n_iv >= HISTORY_MIN_POINTS:
            self._hist_short.pop(sym, None)
            return None
        if n_bars <= 0 or n_iv <= 0:
            return ("IBKR returned no history (%d daily bars, %d IV points) - its history "
                    "service was busy, down or pacing; retried later" % (n_bars, n_iv))
        prev = self._hist_short.get(sym)
        if prev is not None and prev[0] < day and n_bars >= prev[1] and n_iv >= prev[2]:
            self._hist_short.pop(sym, None)
            self.log.info("history %s: a young listing - %d daily bars, %d IV points again on %s "
                          "(first seen %s); filed as its history", sym, n_bars, n_iv, day, prev[0])
            return None
        if prev is None or prev[0] != day:
            self._hist_short[sym] = (day, n_bars, n_iv)
        first = self._hist_short[sym][0]
        return ("IBKR returned only %d daily bars and %d IV points (first seen %s) - filed as a "
                "young listing when a later day's pull returns as much again"
                % (n_bars, n_iv, first))

    def _history_one(self, db, sym: str, *, read_chain: bool | None = None) -> tuple[bool, bool]:
        """The first-time history of one symbol (§4 step 3), then its chain read.
        ``read_chain`` None = only when the symbol was never quoted (a chain read while
        its history was deferred already filled it). Returns ``(history ok, chain
        read)``; a failed history is retried after a back-off. ``history_done`` is set
        only when IBKR really returned history (``_history_verdict``); what a short pull
        did return is still filed (and the stats recomputed from it)."""
        now = self._now()
        try:
            bars = list(self._call(self.fetch.daily_bars, self.ib, sym, duration=HISTORY_BARS,
                                   timeout=T_HIST) or [])
            ivs = []
            if bars:                    # no bars = the history service failed: spare the IV request
                ivs = list(self._call(self.fetch.iv_history, self.ib, sym, duration=HISTORY_IV,
                                      timeout=T_HIST) or [])
            n_days = opt_store.upsert_daily(db, sym, bars, ivs, source="hermes") if bars else 0
            short = self._history_verdict(sym, len(bars), len(ivs), self._et_day(now))
            if short is not None:
                if n_days:
                    opt_store.recompute_underlying(db, sym)
                raise RuntimeError(short)
            und = opt_store.recompute_underlying(db, sym)
            opt_store.mark_history_done(db, sym)
        except Exception as exc:  # noqa: BLE001
            self._fail(db, sym, "history", exc, self._pref(now))
            n = self._hist_retry.get(sym, (0, now))[0] + 1
            wait = min(HISTORY_RETRY_S * 2 ** (n - 1), HISTORY_RETRY_MAX_S)
            self._hist_retry[sym] = (n, now + _dt.timedelta(seconds=wait))
            return False, False
        self._hist_retry.pop(sym, None)
        self._first_tried.pop(sym, None)
        self.log.info("history %s: %d days filed (%d bars, %d IV points), IV rank %s",
                      sym, n_days, len(bars), len(ivs), und.get("iv_rank"))
        if read_chain is None:
            read_chain = sym not in opt_store.freshness(db, [sym])
        if not read_chain:
            return True, False
        self._read_chain(db, sym, kind="history", pref=self._pref(self._now()))
        return True, True

    def _first_reads(self, db, pending: list[str], now: _dt.datetime) -> list[str]:
        """Symbols waiting for their history that were never quoted (and not tried in
        the last 30 min): while history is deferred they still get a chain read."""
        if not pending:
            return []
        fr = opt_store.freshness(db, pending)
        out = []
        for s in pending:
            if s in fr:
                continue
            t = self._first_tried.get(s)
            if t is not None and (now - t).total_seconds() < HISTORY_RETRY_S:
                continue
            out.append(s)
        return out

    # ── history increments (the EOD pass, or caught up when deferred) ──

    def _last_closed_day(self, now: _dt.datetime) -> str:
        """The newest session whose daily bar should be on file: today after 16:15 ET
        on a trading day, else the trading day before."""
        et = clock.et_now(now)
        d, t = et.date(), et.time()
        if clock.is_trading_day(d):
            return (d if t >= EOD_AFTER else clock.prev_trading_day(d)).isoformat()
        return clock.last_trading_day(d).isoformat()

    def _last_bar_days(self, db, syms: list[str]) -> dict[str, str]:
        """``{symbol: newest opt_underlying_daily day with a close}`` (one read)."""
        if not syms:
            return {}
        from sqlalchemy import func  # noqa: PLC0415

        from ..models import OptUnderlyingDaily as D  # noqa: PLC0415 - a read; writes stay in opt_store

        rows = (db.query(D.symbol, func.max(D.on))
                  .filter(D.symbol.in_(list(syms)), D.close.isnot(None))
                  .group_by(D.symbol)
                  .all())
        return {s: str(on) for s, on in rows if s and on}

    def _increment_durations(self, last_on: str | None, day: str) -> tuple[str, str]:
        """(bars, IV) durations that cover the gap from the newest bar on file to
        ``day``: the EOD's 5 D / 1 M for a normal day, longer after a deferral."""
        if not last_on:
            return EOD_BARS, EOD_IV
        try:
            gap = (_dt.date.fromisoformat(day[:10]) - _dt.date.fromisoformat(last_on[:10])).days
        except ValueError:
            return EOD_BARS, EOD_IV
        if gap <= 6:
            return EOD_BARS, EOD_IV
        if gap <= 28:
            return "1 M", "1 M"
        if gap <= 170:
            return "6 M", "6 M"
        return HISTORY_BARS, HISTORY_IV

    def _increments_one(self, db, sym: str, day: str, *, kind: str) -> bool:
        """Daily bars + IBKR's 30-day IV since the newest bar on file, then the stats.
        Remembered per symbol for ``day`` (a failure is retried after 30 min). An EMPTY
        answer is a failure, not "nothing new": ib_insync returns [] for a failed
        historical request, and a ``5 D`` / ``1 M`` window always holds sessions."""
        now = self._now()
        bars_dur, iv_dur = self._increment_durations(self._last_bar_days(db, [sym]).get(sym), day)
        try:
            bars = list(self._call(self.fetch.daily_bars, self.ib, sym, duration=bars_dur,
                                   timeout=T_HIST) or [])
            if not bars:
                raise RuntimeError("IBKR returned no daily bars (%s) - its history service was "
                                   "busy or down; retried in 30 min" % bars_dur)
            ivs = list(self._call(self.fetch.iv_history, self.ib, sym, duration=iv_dur,
                                  timeout=T_HIST) or [])
            opt_store.upsert_daily(db, sym, bars, ivs, source="hermes")
            opt_store.recompute_underlying(db, sym)
            if not ivs:
                raise RuntimeError("IBKR returned no IV points (%s) - retried in 30 min" % iv_dur)
        except Exception as exc:  # noqa: BLE001
            self._fail(db, sym, kind, exc, OFF_HOURS_PREF)
            self._inc_done[sym] = (day, now + _dt.timedelta(seconds=HISTORY_RETRY_S))
            return False
        self._inc_done[sym] = (day, None)
        return True

    def _owed_increments(self, db, syms: list[str], now: _dt.datetime) -> list[str]:
        """Symbols with history whose newest daily bar is older than the last closed
        session and not already brought up to it (or tried) for that session - the EOD
        increments a deferral left behind."""
        if not syms:
            return []
        day = self._last_closed_day(now)
        unds = opt_store.underlyings(db, syms)
        have = [s for s in syms if (unds.get(s) or {}).get("history_done")]
        last = self._last_bar_days(db, have)
        out = []
        for s in have:
            lo = last.get(s)
            if lo is None or lo >= day:
                continue
            rec = self._inc_done.get(s)
            if rec is not None and rec[0] >= day and (rec[1] is None or now < rec[1]):
                continue
            out.append(s)
        return out

    def _increments_tick(self, db, owed: list[str], now: _dt.datetime) -> None:
        """One symbol of the deferred-increments catch-up."""
        day = self._last_closed_day(now)
        if self._inc_run is None or self._inc_run[0] != day or self._inc_run[1] < len(owed):
            self._inc_run = (day, len(owed))
        total = self._inc_run[1]
        sym = owed[0]
        self.state = "history"
        self.symbols_total, self.symbols_done = total, total - len(owed)
        self.detail = "history increments %d/%d: %s (daily bars + IV up to %s)" % (
            total - len(owed) + 1, total, sym, day)
        self._beat(db)
        if self._increments_one(db, sym, day, kind="history"):
            self.symbols_done += 1
            self.log.info("history increments %s up to %s", sym, day)

    def _refresh_earnings(self, db, sym: str, day: str) -> str | None:
        """The next earnings date (free source). A missing answer keeps the stored date
        unless that date has passed."""
        try:
            got = (self._earnings or _default_earnings)(sym)
        except Exception as exc:  # noqa: BLE001
            self.log.info("earnings %s: %s", sym, _err_text(exc))
            return None
        date = got.get("date") if isinstance(got, dict) else got
        try:
            if date:
                opt_store.set_earnings(db, sym, str(date)[:10], src="yahoo")
                return str(date)[:10]
            old = (opt_store.underlying(db, sym) or {}).get("earnings_date")
            if old and str(old) < day:
                opt_store.set_earnings(db, sym, None, src="yahoo")
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            self.log.warning("earnings %s not stored: %s", sym, exc)
        return None

    def _eod_one(self, db, sym: str, day: str, *, increments: bool = True) -> dict:
        """One symbol of the EOD pass (§4 step 5); each part is independent.
        ``increments`` False = the historical requests wait for the ingest top-up
        (``out["history"]`` None); the catch-up step brings them later."""
        out = {"quote": False, "history": False, "snapshot": 0, "earnings": None}
        out["quote"] = self._read_chain(db, sym, kind="eod", pref=OFF_HOURS_PREF)
        if not increments:
            out["history"] = None
        elif self._ib_alive():
            out["history"] = self._increments_one(db, sym, day, kind="eod")
        try:
            out["snapshot"] = opt_store.snapshot_eod(db, sym, day)
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            self.last_error = ("eod %s: snapshot failed: %s" % (sym, _err_text(exc)))[:500]
            self.log.warning("EOD snapshot %s failed: %s", sym, exc)
        out["earnings"] = self._refresh_earnings(db, sym, day)
        return out

    def _finish_eod(self, db, day: str) -> None:
        try:
            pruned = opt_store.prune_v2(db, today=self._et_day(self._now()))
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            pruned = None
            self.log.warning("prune failed: %s", exc)
        self.last_eod_on = day
        self._eod = None
        self.state = "idle"
        self.detail = "EOD %s done (%d symbols)" % (day, self.symbols_total)
        self.log.info("EOD %s done: %d symbols; prune %s", day, self.symbols_total, pruned)

    # ── phases ──

    def _eod_day_for(self, now: _dt.datetime) -> str | None:
        """The trading day whose EOD pass may run now: today after 16:15 ET, the previous
        trading day before today's open (a missed evening is caught up), the last trading
        day on a weekend / holiday; None in the session and 16:00-16:15."""
        et = clock.et_now(now)
        d, t = et.date(), et.time()
        if clock.is_trading_day(d):
            if t >= EOD_AFTER:
                return d.isoformat()
            if t < clock.SESSION_OPEN:
                return clock.prev_trading_day(d).isoformat()
            return None
        return clock.last_trading_day(d).isoformat()

    def _phase(self, now: _dt.datetime) -> tuple[str, str | None]:
        if self._in_session(now):
            return "rth", None
        day = self._eod_day_for(now)
        if day and (self.last_eod_on or "") < day:
            return "eod", day
        return "idle", None

    def _history_pending(self, db, syms: list[str], now: _dt.datetime, *,
                         ignore_backoff: bool = False) -> tuple[list[str], int]:
        """(universe symbols still without history and not backing off, number done)."""
        if not syms:
            return [], 0
        unds = opt_store.underlyings(db, syms)
        pending, done = [], 0
        for s in syms:
            if (unds.get(s) or {}).get("history_done"):
                done += 1
                continue
            r = self._hist_retry.get(s)
            if r is not None and not ignore_backoff and now < r[1]:
                continue
            pending.append(s)
        return pending, done

    def _priority(self, db, uni: list[tuple[str, int]]) -> list[str]:
        """§4.2: never quoted first, then most members holding it, then oldest data."""
        syms = [s for s, _ in uni]
        held = dict(uni)
        fr = opt_store.freshness(db, syms)

        def key(s):
            f = fr.get(s)
            return (f is not None, -held.get(s, 0), (f or {}).get("as_of") or _dt.datetime.min, s)

        return sorted(syms, key=key)

    def _fresh_skip(self, db, sym: str, now: _dt.datetime) -> str | None:
        """Why ``sym`` is skipped this cycle, or None. §4.2: its newest data is a
        member's refresh under 10 min old. The first-time read (kind ``history``) under
        10 min old counts the same - the cycle would only re-read what was just read."""
        f = opt_store.freshness(db, [sym]).get(sym)
        if not f or f.get("as_of") is None:
            return None
        if f.get("source") != "member" and f.get("kind") != "history":
            return None
        age = (now - f["as_of"]).total_seconds()
        if age >= MEMBER_FRESH_S:
            return None
        who = "a member" if f.get("source") == "member" else "the first-time read"
        return "%s refreshed it %.0f min ago" % (who, max(0.0, age) / 60.0)

    def _end_cycle(self, now: _dt.datetime, *, cut: bool = False) -> None:
        self.cycle_finished = now
        self._cycle = None
        self.state = "idle"
        self.detail = "cycle %d %s at %s (%d/%d symbols)" % (
            self.cycle_n, "cut short by the close" if cut else "finished", _et_hm(now),
            self.symbols_done, self.symbols_total)
        self.log.info(self.detail)

    def _cycle_tick(self, db, uni, now: _dt.datetime) -> None:
        if self._cycle is None:
            if (self.cycle_started is not None
                    and (now - self.cycle_started).total_seconds() < self.min_cycle_s):
                nxt = self.cycle_started + _dt.timedelta(seconds=self.min_cycle_s)
                self.state = "idle"
                self.detail = "cycle %d finished; the next starts %s" % (self.cycle_n, _et_hm(nxt))
                return
            order = self._priority(db, uni)
            if not order:
                self.state = "idle"
                self.detail = "no symbol in any member's basket"
                return
            self.cycle_n += 1
            self.cycle_started, self.cycle_finished = now, None
            self._cycle = order
            self.symbols_total, self.symbols_done = len(order), 0
            self.log.info("cycle %d: %d symbols - %s", self.cycle_n, len(order), " ".join(order))
        held = {s for s, _ in uni}
        while self._cycle:
            sym = self._cycle.pop(0)
            if sym not in held:                     # left every basket since the cycle began
                self.symbols_done += 1
                continue
            why = self._fresh_skip(db, sym, now)
            if why is not None:
                self.symbols_done += 1
                self.log.info("cycle %d: %s skipped - %s", self.cycle_n, sym, why)
                continue
            self.state = "cycle"
            self.detail = "cycle %d: %s (%d/%d)" % (self.cycle_n, sym, self.symbols_done + 1,
                                                   self.symbols_total)
            self._beat(db)
            self._read_chain(db, sym, kind="cycle", pref=RTH_PREF)
            self.symbols_done += 1
            break
        if not self._cycle:
            self._end_cycle(self._now())

    def _eod_tick(self, db, uni, day: str, *, hist_ok: bool = True, hist_why: str | None = None) -> None:
        if self._eod is None or self._eod[0] != day:
            order = [s for s, _ in uni]
            self._eod = (day, order)
            self.symbols_total, self.symbols_done = len(order), 0
            self.log.info("EOD %s: %d symbols%s", day, len(order),
                          "" if hist_ok else " (%s)" % hist_why)
        left = self._eod[1]
        if left:
            sym = left.pop(0)
            self.state = "eod"
            self.detail = "EOD %s: %s (%d/%d)%s" % (day, sym, self.symbols_done + 1, self.symbols_total,
                                                   "" if hist_ok else " - %s" % hist_why)
            self._beat(db)
            self._eod_one(db, sym, day, increments=hist_ok)
            self.symbols_done += 1
        if not left:
            self._finish_eod(db, day)
            if not hist_ok:
                self.detail = "%s; %s" % (self.detail, hist_why)

    def _idle(self, now: _dt.datetime) -> None:
        self.state = "idle"
        et = clock.et_now(now)
        d, t = et.date(), et.time()
        if clock.is_trading_day(d) and clock.SESSION_CLOSE <= t < EOD_AFTER:
            self.detail = "market closed; the EOD pass starts 16:15 ET"
            return
        nxt = d if (clock.is_trading_day(d) and t < clock.SESSION_OPEN) else clock.next_trading_day(d)
        eod = ("EOD %s done; " % self.last_eod_on) if self.last_eod_on else ""
        self.detail = "%snext session %s 09:30 ET" % (eod, nxt.isoformat())

    def _step(self, db, now: _dt.datetime) -> None:
        uni = opt_store.universe(db)
        self.universe_n = len(uni)
        self.history_waiting = 0
        if not self._ensure_connected(now):
            return
        syms = [s for s, _ in uni]
        hist_ok, hist_why = self._history_gate(now)
        pending, n_done = self._history_pending(db, syms, now)
        if pending and hist_ok:
            sym = pending[0]
            self.state = "history"
            self.symbols_total, self.symbols_done = len(syms), n_done
            self.detail = "history %d/%d: %s" % (n_done + 1, len(syms), sym)
            self._beat(db)
            if self._history_one(db, sym)[0]:
                self.symbols_done = n_done + 1
            return
        if pending:
            # history deferred for the ingest top-up; the chain (market data) is not
            self.history_waiting = len(pending)
            first = self._first_reads(db, pending, now)
            if first:
                sym = first[0]
                self.state = "history"
                self.symbols_total, self.symbols_done = len(syms), n_done
                self.detail = "first read %s: the chain now - %s" % (sym, hist_why)
                self._beat(db)
                self._first_tried[sym] = now
                self._read_chain(db, sym, kind="history", pref=self._pref(now))
                return
        phase, day = self._phase(now)
        if phase != "rth" and self._cycle is not None:
            self._end_cycle(now, cut=True)
        if phase != "eod":
            self._eod = None
        if phase == "rth":
            self._cycle_tick(db, uni, now)
        elif phase == "eod":
            self._eod_tick(db, uni, day, hist_ok=hist_ok, hist_why=hist_why)
        else:
            owed = self._owed_increments(db, syms, now)
            if owed and hist_ok:
                self._increments_tick(db, owed, now)
                return
            self._idle(now)
            if not hist_ok and (pending or owed):
                self.history_waiting = len(pending) + len(owed)
                n = self.history_waiting
                self.detail = "%s; %s (%d symbol%s)" % (self.detail, hist_why, n, "" if n == 1 else "s")

    # ── public ──

    def tick(self) -> str:
        """One step of the loop; never raises. Returns the state after the step."""
        now = self._now()
        db = self._sf()
        self._db = db
        try:
            self._restore(db)
            self._beat(db)
            try:
                self._step(db, now)
            except Exception as exc:  # noqa: BLE001 - a bug or a DB failure must not stop the loop
                self._rollback(db)
                self.state = "error"
                self.last_error = ("tick failed: %s" % _err_text(exc))[:500]
                self.detail = self.last_error
                self.log.exception("tick failed")
            self._beat(db)
            return self.state
        finally:
            self._db = None
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def run_forever(self, *, max_ticks: int | None = None) -> None:
        """Tick every ``tick_s`` until Ctrl+C (or ``max_ticks``); a tick that took
        longer than ``tick_s`` (a chain read) is followed by the next at once."""
        n = 0
        try:
            while True:
                t0 = self._now()
                self.tick()
                n += 1
                if max_ticks is not None and n >= max_ticks:
                    break
                spent = (self._now() - t0).total_seconds()
                self._do_sleep(max(1.0, self.tick_s - spent))
        except KeyboardInterrupt:
            self.log.info("stopping (interrupted)")
        finally:
            self.stop()

    def _session(self):
        db = self._sf()
        self._db = db
        self._restore(db)
        return db

    def _close_session(self, db) -> None:
        self._db = None
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    def _connect_error(self) -> str | None:
        """What a one-off run reports when the Gateway is not reachable."""
        err = self._last_connect_error or self.last_error
        if self.state == "waiting":
            err = "%s (%s)" % (err or "gateway not reachable", self.detail)
        return err

    def run_once(self, *, ignore_ingest: bool = False) -> dict:
        """One full pass now, whatever the clock: history for every universe symbol
        still missing it, then one chain read of every other universe symbol in the
        §4.2 order (no member-fresh skip - a manual run reads everything). While the
        ingest top-up is pending the history pulls are skipped (``history_deferred``)
        unless ``ignore_ingest``; those symbols still get their chain read."""
        out = {"connected": False, "symbols": 0, "history": 0, "history_failed": 0,
               "quoted": 0, "failed": 0}
        db = self._session()
        try:
            now = self._now()
            uni = opt_store.universe(db)
            self.universe_n = out["symbols"] = len(uni)
            self._beat(db, force=True)
            if not self._ensure_connected(now, force=True):
                out["error"] = self._connect_error()
                return out
            out["connected"] = True
            syms = [s for s, _ in uni]
            pending, n_done = self._history_pending(db, syms, now, ignore_backoff=True)
            hist_ok, hist_why = self._history_gate(now)
            if pending and not (hist_ok or ignore_ingest):
                out["history_deferred"] = len(pending)
                self.log.info("history of %d symbol(s) skipped: %s", len(pending), hist_why)
                pending = []
            read: set[str] = set()
            for sym in pending:
                if not self._ensure_connected(self._now(), force=True):
                    out["history_failed"] += 1
                    continue
                self.state = "history"
                self.symbols_total, self.symbols_done = len(syms), n_done
                self.detail = "history %d/%d: %s" % (n_done + 1, len(syms), sym)
                self._beat(db)
                ok, chain = self._history_one(db, sym)
                if ok:
                    out["history"] += 1
                    n_done += 1
                    if chain:
                        read.add(sym)
                else:
                    out["history_failed"] += 1
            order = [s for s in self._priority(db, opt_store.universe(db)) if s not in read]
            self.cycle_n += 1
            self.cycle_started, self.cycle_finished = self._now(), None
            self.symbols_total, self.symbols_done = len(order), 0
            for sym in order:
                if not self._ensure_connected(self._now(), force=True):
                    out["failed"] += 1
                    self.symbols_done += 1
                    continue
                self.state = "cycle"
                self.detail = "cycle %d (one-off): %s (%d/%d)" % (
                    self.cycle_n, sym, self.symbols_done + 1, self.symbols_total)
                self._beat(db)
                ok = self._read_chain(db, sym, kind="cycle", pref=self._pref(self._now()))
                out["quoted" if ok else "failed"] += 1
                self.symbols_done += 1
            self._end_cycle(self._now())
            return out
        finally:
            self._beat(db, force=True)
            self._close_session(db)

    def run_eod(self, day: str | None = None, *, ignore_ingest: bool = False) -> dict:
        """The EOD pass now, whatever the clock and ``last_eod_on``, filed under ``day``
        (default: today's ET date when it is a trading day, else the last one). While
        the ingest top-up is pending the history increments are left to the catch-up
        (``history_deferred``) unless ``ignore_ingest``."""
        now = self._now()
        day = day or clock.last_trading_day(clock.et_date(now)).isoformat()
        out = {"connected": False, "day": day, "symbols": 0, "quoted": 0, "snapshot_rows": 0}
        db = self._session()
        try:
            uni = opt_store.universe(db)
            self.universe_n = out["symbols"] = len(uni)
            self._beat(db, force=True)
            if not self._ensure_connected(now, force=True):
                out["error"] = self._connect_error()
                return out
            out["connected"] = True
            hist_ok, hist_why = self._history_gate(now)
            increments = hist_ok or ignore_ingest
            if not increments:
                out["history_deferred"] = len(uni)
                self.log.info("EOD history increments skipped: %s", hist_why)
            self._eod = (day, [s for s, _ in uni])
            self.symbols_total, self.symbols_done = len(uni), 0
            while self._eod is not None and self._eod[1]:
                sym = self._eod[1].pop(0)
                self._ensure_connected(self._now(), force=True)
                self.state = "eod"
                self.detail = "EOD %s (one-off): %s (%d/%d)" % (day, sym, self.symbols_done + 1,
                                                               self.symbols_total)
                self._beat(db)
                r = self._eod_one(db, sym, day, increments=increments)
                out["quoted"] += 1 if r["quote"] else 0
                out["snapshot_rows"] += r["snapshot"] or 0
                self.symbols_done += 1
            self._finish_eod(db, day)
            return out
        finally:
            self._beat(db, force=True)
            self._close_session(db)

    def run_history(self, symbols, *, ignore_ingest: bool = False) -> dict:
        """The first-time history pull (and a chain read) for ``symbols`` now, even
        when it was done before. While the ingest top-up is pending nothing is pulled
        (``deferred`` = the number of symbols, ``why``) unless ``ignore_ingest``."""
        syms = []
        for s in symbols or ():
            s = str(s or "").strip().upper()
            if s and s not in syms:
                syms.append(s)
        out = {"connected": False, "symbols": len(syms), "done": 0, "failed": 0}
        db = self._session()
        try:
            self._beat(db, force=True)
            hist_ok, hist_why = self._history_gate(self._now())
            if not (hist_ok or ignore_ingest):
                out.update(deferred=len(syms), why=hist_why)
                self.state = "idle"
                self.detail = "history (one-off) not run: %s" % hist_why
                return out
            if not self._ensure_connected(self._now(), force=True):
                out["error"] = self._connect_error()
                return out
            out["connected"] = True
            self.symbols_total, self.symbols_done = len(syms), 0
            for sym in syms:
                if not self._ensure_connected(self._now(), force=True):
                    out["failed"] += 1
                    continue
                self.state = "history"
                self.detail = "history (one-off) %d/%d: %s" % (self.symbols_done + 1, len(syms), sym)
                self._beat(db)
                out["done" if self._history_one(db, sym, read_chain=True)[0] else "failed"] += 1
                self.symbols_done += 1
            self.state = "idle"
            self.detail = "history (one-off) done: %d ok, %d failed" % (out["done"], out["failed"])
            return out
        finally:
            self._beat(db, force=True)
            self._close_session(db)

    def stop(self, reason: str = "stopped") -> None:
        """Heartbeat ``stopped``, disconnect, close the event loop this object made."""
        self._cycle = None
        self._eod = None
        self.state = "stopped"
        self.detail = reason
        try:
            db = self._sf()
            try:
                self._beat(db, force=True)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("final heartbeat failed: %s", exc)
            self._write_state(dict(self._fields(), heartbeat=self._now()))
        ib, self.ib = self.ib, None
        self.gateway_ok = False
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self._own_loop and self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.close()
            finally:
                self._loop = None
                self._own_loop = False


__all__ = ["Collector", "ib_connector", "ib_ports", "env_client_id", "env_max_lines", "STATE_PATH",
           "TICK_S", "MEMBER_FRESH_S", "MIN_CYCLE_S", "EOD_AFTER", "RTH_PREF", "OFF_HOURS_PREF",
           "COLLECTOR_VERSION", "DEFAULT_CLIENT_ID", "SupervisorWindow", "load_supervisor",
           "SUPERVISOR_STATE_DIR", "HISTORY_WAIT_TEXT", "WAIT_RETRY_S", "GATEWAY_START_GRACE_S"]
