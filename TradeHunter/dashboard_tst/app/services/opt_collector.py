"""The Hermes options collector (OPTIONS_V2_DESIGN.md §13.4, v4.134): the always-on loop
that keeps the shared Options data (``opt_quote``, ``opt_underlying``,
``opt_underlying_daily``, ``option_chain_snapshot``) filled from Massive (formerly
Polygon.io), so every member sees a chain without reading anything themselves.

Data: Massive Options Starter (the whole-chain snapshot - greeks, IV, open interest, the
day bar; 15 min delayed; no bid/ask) and Stocks Basic (end-of-day daily bars, about 5
requests a minute - the client paces itself). Every read goes through ``opt_massive``;
every write through ``opt_store``.

One ``tick()`` (every 15 s) does a bounded amount of work, so the heartbeat stays timely:

1. heartbeat -> ``opt_collector_status`` (row 1) and ``state/options_collector.json``
   (the Hermes tray reads the file) - at the start and at the end of every tick;
2. the key: no ``TST_MASSIVE_API_KEY`` -> state ``error`` "TST_MASSIVE_API_KEY is not
   set on this PC", looked for again every 5 min (``app/.env`` is re-read, so a key
   added there is picked up without a restart);
3. a session pass that is running or due has the tick (09:30-16:00 ET on trading days):
   every ``TST_OPTIONS_CYCLE_MIN`` minutes (default 15) a pass over
   ``opt_store.universe()`` (most-held first) - ``opt_massive.ingest_symbol(kind="cycle")``,
   a few symbols per tick. The close cuts a running pass short;
4. history, in the gaps between session passes: ONE universe symbol whose
   ``history_done`` is False -> ``opt_massive.backfill_history`` (2 years of daily bars +
   the IV30 series rebuilt from option daily bars), then - when it was never quoted - its
   chain (kind ``history``). Too little data (``history_done`` stays False) or a failure
   -> retried after 30 min, doubling to 6 h. A never-quoted symbol whose history cannot
   run now still gets its chain read (one per tick, at most every 30 min). A history read
   while a pass is in progress leaves the pass's ticker counts alone;
5. the end-of-day pass, once per trading day after 16:20 ET (``last_eod_on``, kept in the
   status row across restarts): per symbol ``ingest_symbol(kind="eod")`` ->
   ``opt_store.snapshot_eod`` -> ``opt_massive.daily_update`` (Stocks Basic bars) -> the
   earnings date (Yahoo, ``prices.fetch_next_earnings``); when the pass ends,
   ``opt_store.prune_v2``. A pass missed in the evening is caught up before the next
   open (on a weekend / holiday: the last trading day's). A symbol's stock bars count as
   done only when ``daily_update`` worked AND its bars include the session it asked for
   (``complete``); otherwise (refused, failed, paused, not yet published) they are owed
   and retried outside the session - the next tick, then after 5 min doubling to 2 h -
   while the day's chain snapshot stays done. The next day's pass drops what is still
   owed (its 10-day lookback covers it);
6. otherwise ``idle``.

Errors
------
One symbol's failure is written to ``opt_refresh_log`` (its error text - never the key)
and the loop moves on. Massive errors that are not about one symbol pause work and put
the collector in state ``error`` with the plain reason (the Options page strip and the
Hermes tray show it):

* ``config`` / ``auth`` (no key, a rejected key) - everything, retried every 5 min;
  ``app/.env`` is re-read then and its ``TST_MASSIVE_API_KEY`` replaces the process's
  (v4.137), so a key added or corrected there is used without a restart (a rejected key
  still unchanged there is tried again at most every 30 min);
* ``network`` (Massive not reachable) - everything, retried after 60 s doubling to 5 min;
* ``plan`` (HTTP 403 - the plan lacks an endpoint) - only the part that needs it
  (chain reads, history reads, or the end-of-day stock bars), retried every 5 min; the
  rest carries on.

The error clears on the next success of what failed.

The status row's ``gateway`` / ``gateway_ok`` columns (named in v4.133) now hold the data
source's host (``api.massive.com``) and whether its last request worked.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import clock, massive, opt_massive, opt_store
from .massive import MassiveError

COLLECTOR_VERSION = "2.0"
SOURCE = opt_massive.SOURCE           # "massive"

TICK_S = 15.0                    # one tick every 15 s
HEARTBEAT_EVERY_S = 15.0         # mid-tick heartbeats at most this often
TICK_BUDGET_S = 20.0             # a tick takes no new symbol after this long ...
BATCH = 5                        # ... nor more than this many (session / EOD pass)
CYCLE_MIN_DEFAULT = 15           # TST_OPTIONS_CYCLE_MIN: a session pass every N minutes
CYCLE_MIN_LO, CYCLE_MIN_HI = 1, 240
KEY_RETRY_S = 300.0              # no key / a rejected key: looked at again every 5 min
KEY_SAME_RETRY_S = 1800.0        # a rejected key still unchanged in app/.env: tried again every 30 min
PLAN_RETRY_S = 300.0             # an endpoint the plan lacks: tried again every 5 min
NETWORK_RETRY_S = 60.0           # Massive unreachable: 60 s ...
NETWORK_RETRY_MAX_S = 300.0      # ... doubling to 5 min
HISTORY_RETRY_S = 1800.0         # a failed / too-short history: 30 min ...
HISTORY_RETRY_MAX_S = 6 * 3600.0 # ... doubling to 6 h
FIRST_READ_RETRY_S = 1800.0      # a never-quoted symbol's chain read while its history waits
BARS_RETRY_S = 300.0             # owed end-of-day stock bars: the next tick, then 5 min ...
BARS_RETRY_MAX_S = 2 * 3600.0    # ... doubling to 2 h
STATE_LOG_EVERY_S = 1800.0       # a persisting error is logged when it begins, then every 30 min
EOD_AFTER = _dt.time(16, 20)     # §13.4: the end-of-day pass starts 16:20 ET

NO_KEY_TEXT = "%s is not set on this PC" % massive.ENV_KEY
ALL = "all"                       # an error scope: every Massive request
OPS = ("chain", "history", "bars")
_OP_WORDS = {"chain": "chain reads", "history": "history reads", "bars": "end-of-day stock bars"}
_FATAL = ("config", "auth", "network")    # MassiveError kinds that pause everything

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "options_collector.json"
APP_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


# ────────────────────────────────── settings ──────────────────────────────────

def env_cycle_min() -> int:
    """``TST_OPTIONS_CYCLE_MIN`` (minutes between session passes): 1-240, else 15 -
    the same rule the Options page reads."""
    raw = (os.environ.get("TST_OPTIONS_CYCLE_MIN") or "").strip()
    try:
        v = int(raw) if raw else CYCLE_MIN_DEFAULT
    except ValueError:
        return CYCLE_MIN_DEFAULT
    return v if CYCLE_MIN_LO <= v <= CYCLE_MIN_HI else CYCLE_MIN_DEFAULT


def _load_env() -> None:
    """Re-read ``app/.env``: every variable not already set is loaded, and the Massive key
    is taken FROM THE FILE whatever the process holds - a key added or corrected there is
    picked up without a restart; a blank ``TST_MASSIVE_API_KEY=`` removes it. A missing
    file (or python-dotenv) leaves the environment as it is."""
    try:
        from dotenv import dotenv_values, load_dotenv  # noqa: PLC0415

        load_dotenv(APP_ENV_PATH, override=False)
        if not Path(APP_ENV_PATH).is_file():
            return
        vals = dotenv_values(APP_ENV_PATH)
        if massive.ENV_KEY in vals:
            v = str(vals.get(massive.ENV_KEY) or "").strip()
            if v:
                os.environ[massive.ENV_KEY] = v
            else:
                os.environ.pop(massive.ENV_KEY, None)
    except Exception:  # noqa: BLE001 - python-dotenv or the file unreadable: the environment as is
        pass


def default_client():
    """A ``massive.Client`` with the key from the environment. ``app/.env`` is re-read
    first (``_load_env``: the key in the file wins), so a key added or corrected there
    while the collector runs is found at the next 5-minute look."""
    _load_env()
    return massive.Client()


# ────────────────────────────────── helpers ──────────────────────────────────

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


def _has_key(client) -> bool:
    return client is not None and bool(getattr(client, "has_key", True))


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# ────────────────────────────────── the collector ──────────────────────────────────

class Collector:
    """The §13.4 loop. ``tick()`` does one bounded step; ``run_forever()`` ticks every
    15 s; ``run_once`` / ``run_history`` / ``run_eod`` are the CLI's one-off runs.

    ``session_factory()`` returns a SQLAlchemy session (``app.db.SessionLocal``).
    ``client`` is a ``massive.Client`` (or a fake with ``chain_snapshot``,
    ``stock_daily``, ``option_daily``, ``has_key``); ``client_factory()`` builds one
    (default ``default_client`` when no ``client`` is given) and is called again while
    the key is missing. ``clock()`` returns now (naive = UTC); ``sleep(s)`` waits between
    ticks. ``earnings(symbol)`` returns ``{"date": ...}`` or None (default
    ``prices.fetch_next_earnings``). ``cycle_min`` (default ``TST_OPTIONS_CYCLE_MIN``),
    ``batch`` and ``tick_budget_s`` bound the work per pass and per tick."""

    def __init__(self, session_factory, *, client=None, client_factory=None, clock=None,
                 sleep=None, log=None, state_path=None, earnings=None, tick_s: float = TICK_S,
                 cycle_min=None, batch: int = BATCH, tick_budget_s: float = TICK_BUDGET_S):
        self._sf = session_factory
        self.client = client
        self._client_factory = client_factory if client_factory is not None else (
            default_client if client is None else None)
        self._clock = clock
        self._sleep = sleep or time.sleep
        self.log = log or logging.getLogger(__name__)
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self._earnings = earnings
        self.tick_s = float(tick_s)
        self.cycle_min = int(cycle_min) if cycle_min else env_cycle_min()
        self.batch = max(1, int(batch))
        self.tick_budget_s = float(tick_budget_s)
        self.version = COLLECTOR_VERSION
        self.pid = os.getpid()
        # status (what the heartbeat carries)
        self.state = "starting"
        self.detail = "starting"
        self.mdt: str | None = None
        self.api_ok: bool | None = None
        self.cycle_n = 0
        self.cycle_started: _dt.datetime | None = None
        self.cycle_finished: _dt.datetime | None = None
        self.symbols_total = 0
        self.symbols_done = 0
        self.last_eod_on: str | None = None
        self.last_error: str | None = None
        self.universe_n = 0
        self.history_pending = 0
        # errors that pause work: scope (ALL | an op) -> (kind, reason) / next try
        self._alerts: dict[str, tuple[str, str]] = {}
        self._retry_at: dict[str, _dt.datetime] = {}
        self._net_fails = 0
        self._auth_tried_at: _dt.datetime | None = None   # a rejected key's last try (same key)
        # work
        self._cycle: list[str] | None = None              # symbols left in the running pass
        self._pass_failed = 0
        self._eod: tuple[str, list[str]] | None = None    # (ET day, symbols left)
        self._bars_day: str | None = None                 # the end-of-day pass the owed bars belong to
        self._bars_owed: dict[str, tuple[int, _dt.datetime]] = {}   # symbol -> (tries, next try)
        self._hist_retry: dict[str, tuple[int, _dt.datetime]] = {}
        self._first_tried: dict[str, _dt.datetime] = {}
        self._state_log: tuple | None = None
        self._restored = False
        self._db = None
        self._last_beat: _dt.datetime | None = None
        self._db_warned = False
        self._file_warned = False

    # ── clock ──

    def _now(self) -> _dt.datetime:
        t = self._clock() if self._clock is not None else _dt.datetime.now(_dt.timezone.utc)
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t

    def _in_session(self, now: _dt.datetime) -> bool:
        return clock.us_session_open(now)

    # ── text safety ──

    def _scrub(self, text) -> str:
        """Text safe for a log line, the refresh log or the status: the key (from the
        environment or the client) masked, capped at 500 characters."""
        s = str(text or "")
        for key in (massive.api_key(), getattr(self.client, "_key", None)):
            if isinstance(key, str) and len(key) >= 4:
                s = s.replace(key, "***")
        return massive._scrub(s)[:500]   # noqa: SLF001 - the client's own masking rules

    def _err_text(self, exc) -> str:
        return self._scrub(str(exc) or type(exc).__name__)

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

    def _api_host(self) -> str | None:
        base = getattr(self.client, "base_url", None) or massive.base_url()
        try:
            return urlsplit(str(base)).netloc or str(base)
        except ValueError:
            return None

    def _alert_scope(self) -> str | None:
        if ALL in self._alerts:
            return ALL
        for op in OPS:
            if op in self._alerts:
                return op
        return None

    def _shown(self) -> tuple[str, str]:
        """(state, detail) as reported: ``error`` with the plain reason while an error
        pauses work (unless stopped), else the work state."""
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

    def error_kind(self) -> str | None:
        scope = self._alert_scope()
        return self._alerts[scope][0] if scope else None

    def _fields(self) -> dict:
        state, detail = self._shown()
        return {"state": state, "phase_detail": detail, "gateway": self._api_host(),
                "gateway_ok": self.api_ok, "mdt": self.mdt, "cycle_n": self.cycle_n,
                "cycle_started": self.cycle_started, "cycle_finished": self.cycle_finished,
                "symbols_total": self.symbols_total, "symbols_done": self.symbols_done,
                "last_eod_on": self.last_eod_on, "last_error": self.last_error,
                "pid": self.pid, "version": self.version}

    def _beat(self, db, *, force: bool = False) -> None:
        """Write the heartbeat (status row + state file) when forced or when the last one
        is ``HEARTBEAT_EVERY_S`` old (mid-tick progress)."""
        now = self._now()
        due = self._last_beat is None or (now - self._last_beat).total_seconds() >= HEARTBEAT_EVERY_S
        if not (force or due):
            return
        self._last_beat = now
        fields = self._fields()
        if db is not None:
            try:
                opt_store.set_collector_status(db, heartbeat=now, **fields)
                self._db_warned = False
            except Exception as exc:  # noqa: BLE001 - the file still carries the heartbeat
                self._rollback(db)
                if not self._db_warned:
                    self.log.warning("could not write the collector status row: %s", exc)
                    self._db_warned = True
        self._write_state(fields, now)

    def _write_state(self, fields: dict, now: _dt.datetime) -> None:
        """``state/options_collector.json`` for the Hermes tray, replaced atomically.
        The first write into a ``state`` folder also drops a ``.gitignore`` there (``*``)
        so the runtime file never shows up in git."""
        doc = {k: (_iso(v) if isinstance(v, _dt.datetime) else v) for k, v in fields.items()
               if k not in ("gateway", "gateway_ok")}
        scope = self._alert_scope()
        nxt = self._retry_at.get(scope) if scope else None
        doc.update(heartbeat=_iso(now), source=SOURCE, api=fields.get("gateway"),
                   api_ok=self.api_ok, error_kind=self.error_kind(),
                   next_try=_iso(nxt) if (nxt is not None and nxt > now) else None,
                   history_pending=int(self.history_pending or 0), universe=self.universe_n,
                   cycle_min=self.cycle_min,
                   written_by="dashboard_tst/app/services/opt_collector.py")
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

    def _log_state(self, key: tuple, level: int, msg: str, *args) -> None:
        """Log an error line when it begins or changes, then at most once per
        ``STATE_LOG_EVERY_S`` while it persists - never one line per retry."""
        now = self._now()
        last = self._state_log
        if last is not None and last[0] == key and (now - last[1]).total_seconds() < STATE_LOG_EVERY_S:
            return
        self._state_log = (key, now)
        self.log.log(level, msg, *args)

    # ── errors that pause work ──

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

    def _allowed(self, op: str, now: _dt.datetime) -> bool:
        """May ``op`` (chain | history | bars) send requests now?"""
        for scope in (ALL, op):
            until = self._retry_at.get(scope)
            if until is not None and now < until:
                return False
        return True

    def _paused_all(self, now: _dt.datetime | None = None) -> bool:
        until = self._retry_at.get(ALL)
        return until is not None and (now or self._now()) < until

    def _ready(self, now: _dt.datetime, *, force: bool = False) -> bool:
        """A client with a key, and every request not paused (``force``: look now). No
        key -> state error with ``NO_KEY_TEXT``, looked at again in 5 min. A rejected key:
        ``app/.env`` is re-read at each look - a different key there gets a fresh client at
        once; the same key is tried again at most every 30 min."""
        if not force and self._paused_all(now):
            return False
        c = self.client
        if self._alerts.get(ALL, ("",))[0] == "auth" and self._client_factory is not None and _has_key(c):
            _load_env()
            if massive.api_key() != getattr(c, "_key", None):
                try:
                    fresh = self._client_factory()
                except Exception as exc:  # noqa: BLE001
                    self._alert(ALL, "config", "could not set up the Massive client: %s"
                                % self._err_text(exc), KEY_RETRY_S, now)
                    return False
                if fresh is not c:
                    self._close_client(c)            # one thread: nothing is in flight on it
                self.client = c = fresh
                self._auth_tried_at = None
                self.log.info("the Massive key in app/.env changed - a new client uses it")
            elif not force and self._auth_tried_at is not None and \
                    (now - self._auth_tried_at).total_seconds() < KEY_SAME_RETRY_S:
                self._retry_at[ALL] = now + _dt.timedelta(seconds=KEY_RETRY_S)
                return False
            else:
                self._auth_tried_at = now
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
            self._clear(ALL)                         # the key is there now
        return True

    def _close_client(self, c) -> None:
        close = getattr(c, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _success(self, op: str) -> None:
        self.api_ok = True
        self._net_fails = 0
        for scope in (ALL, op):
            if scope in self._alerts:
                self._clear(scope)

    def _symbol_failure(self, db, sym: str, kind: str, msg: str) -> None:
        """One symbol's failure: an ``opt_refresh_log`` row (``error``, no contracts),
        ``last_error``, a log line."""
        msg = self._scrub(msg)
        self.last_error = ("%s %s: %s" % (kind, sym, msg))[:500]
        self.log.warning("%s %s failed: %s", kind, sym, msg)
        try:
            opt_store.upsert_quotes(db, sym, [], source=SOURCE, mdt=opt_massive.MDT, kind=kind,
                                    as_of=self._now(), error=msg, now=self._now())
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            self.log.warning("could not log the %s failure of %s: %s", kind, sym, exc)

    def _guard(self, db, op: str, sym: str, kind: str, fn) -> tuple[bool, object]:
        """Run one Massive-backed operation for ``sym``: ``(ok, result)``. A failure is
        logged for the symbol; a Massive error that is not about the symbol pauses work
        (``_FATAL`` kinds: everything; ``plan``: ``op``)."""
        try:
            res = fn()
        except MassiveError as exc:
            self._rollback(db)
            msg = self._err_text(exc)
            self._symbol_failure(db, sym, kind, msg)
            now = self._now()
            if exc.kind in ("config", "auth"):
                self.api_ok = False
                self._alert(ALL, exc.kind, msg, KEY_RETRY_S, now)
            elif exc.kind == "network":
                self.api_ok = False
                self._net_fails += 1
                wait = min(NETWORK_RETRY_S * 2 ** (self._net_fails - 1), NETWORK_RETRY_MAX_S)
                self._alert(ALL, "network", msg, wait, now)
            elif exc.kind == "plan":
                self._alert(op, "plan", msg, PLAN_RETRY_S, now)
            return False, None
        except Exception as exc:  # noqa: BLE001 - one symbol never stops the loop
            self._rollback(db)
            self._symbol_failure(db, sym, kind, self._err_text(exc))
            return False, None
        self._success(op)
        return True, res

    # ── one symbol ──

    def _read(self, db, sym: str, kind: str) -> bool:
        """``opt_massive.ingest_symbol`` -> the chain, the spot, today's IV30. True when
        the read worked (even with no contract stored)."""
        ok, res = self._guard(db, "chain", sym, kind, lambda: opt_massive.ingest_symbol(
            db, self.client, sym, kind=kind, now=self._now()))
        if not ok:
            return False
        res = res or {}
        self.mdt = opt_massive.MDT
        level = logging.DEBUG if kind == "cycle" else logging.INFO
        self.log.log(level, "%s %s: %d of %d contracts stored, %d expiries, spot %s (%s), iv30 %s, "
                     "%d pages, %d ms", kind, sym, res.get("stored", 0), res.get("rows", 0),
                     res.get("expiries", 0), res.get("spot"), res.get("spot_kind"), res.get("iv30"),
                     res.get("pages", 0), res.get("ms", 0))
        return True

    def _backoff(self, sym: str, now: _dt.datetime) -> None:
        n = self._hist_retry.get(sym, (0, now))[0] + 1
        wait = min(HISTORY_RETRY_S * 2 ** (n - 1), HISTORY_RETRY_MAX_S)
        self._hist_retry[sym] = (n, now + _dt.timedelta(seconds=wait))

    def _history_one(self, db, sym: str, *, read_chain: bool | None = None) -> tuple[bool, bool]:
        """The one-time history of ``sym`` (``opt_massive.backfill_history``), then its
        chain (kind ``history``). ``read_chain`` None = only when it was never quoted.
        Returns ``(history done, chain read)``. Too little data or a failure of the
        symbol is retried after a back-off; a paused request is not (it is retried when
        the pause ends)."""
        now = self._now()
        ok, res = self._guard(db, "history", sym, "history", lambda: opt_massive.backfill_history(
            db, self.client, sym, now=self._now()))
        if not ok:
            if self._allowed("history", self._now()):
                self._backoff(sym, now)
            return False, False
        res = res or {}
        if not res.get("history_done"):
            n = opt_massive.HISTORY_MIN_POINTS
            self._symbol_failure(db, sym, "history", (
                "Massive returned %d daily bars and %d IV points - the history needs %d of each "
                "(a young listing, or no options listed); retried later"
                % (res.get("bars", 0), res.get("iv_points", 0), n)))
            self._backoff(sym, now)
            return False, False
        self._hist_retry.pop(sym, None)
        self._first_tried.pop(sym, None)
        self.log.info("history %s: %d daily bars, %d IV points, %d requests, %d ms", sym,
                      res.get("bars", 0), res.get("iv_points", 0), res.get("requests", 0),
                      res.get("ms", 0))
        if read_chain is None:
            read_chain = sym not in opt_store.freshness(db, [sym])
        if not read_chain or not self._allowed("chain", self._now()):
            return True, False
        return True, self._read(db, sym, "history")

    def _refresh_earnings(self, db, sym: str, day: str) -> str | None:
        """The next earnings date (free source - the one figure not from Massive). A
        missing answer keeps the stored date unless that date has passed."""
        try:
            if self._earnings is not None:
                got = self._earnings(sym)
            else:
                from . import prices  # noqa: PLC0415 - imported on use (httpx client)

                got = prices.fetch_next_earnings(sym)
        except Exception as exc:  # noqa: BLE001
            self.log.info("earnings %s: %s", sym, self._err_text(exc))
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

    def _bars_update(self, db, sym: str) -> tuple[bool, dict | None]:
        """``opt_massive.daily_update`` for ``sym`` through ``_guard`` (op ``bars``)."""
        return self._guard(db, "bars", sym, "eod", lambda: opt_massive.daily_update(
            db, self.client, sym, now=self._now()))

    def _bars_settle(self, sym: str, day: str, ok: bool, res) -> bool:
        """File the stock-bar part of ``sym``'s end-of-day ``day``. Done only when
        ``daily_update`` worked AND its bars include the session it asked for
        (``complete``); otherwise ``sym`` is owed - tried again the next tick, then after
        ``BARS_RETRY_S`` doubling to ``BARS_RETRY_MAX_S`` (a pause still holds it back)."""
        now = self._now()
        if self._bars_day != day:
            self._bars_day, self._bars_owed = day, {}
        if ok and (res or {}).get("complete", True):
            self._bars_owed.pop(sym, None)
            return True
        if ok:
            self.log.info("eod %s: the stock bars do not include the session asked for yet - "
                          "retried later", sym)
        n = self._bars_owed.get(sym, (0, now))[0]
        wait = min(BARS_RETRY_S * 2 ** (n - 1), BARS_RETRY_MAX_S) if n else 0.0
        self._bars_owed[sym] = (n + 1, now + _dt.timedelta(seconds=wait))
        return False

    def _eod_one(self, db, sym: str, day: str) -> dict | None:
        """One symbol of the end-of-day pass: the chain (kind ``eod``), the day's record
        (``snapshot_eod``), the Stocks Basic bars (``daily_update``), the earnings date.
        Each part is independent; None = every request got paused part-way (the symbol
        is done again when the pause ends). Bars that are refused, fail, are paused or
        are not complete are owed (``_bars_retry``); ``bars`` is True only when done."""
        out = {"quote": False, "snapshot": 0, "bars": False, "earnings": None}
        if self._allowed("chain", self._now()):
            out["quote"] = self._read(db, sym, "eod")
            if self._paused_all():
                return None
        try:
            out["snapshot"] = opt_store.snapshot_eod(db, sym, day)
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            self.last_error = ("eod %s: snapshot failed: %s" % (sym, self._err_text(exc)))[:500]
            self.log.warning("end-of-day snapshot %s failed: %s", sym, exc)
        if self._allowed("bars", self._now()):
            ok, res = self._bars_update(db, sym)
            if self._paused_all():
                return None
            out["bars"] = self._bars_settle(sym, day, ok, res)
        else:
            self._bars_settle(sym, day, False, None)       # paused: owed, retried when it ends
        out["earnings"] = self._refresh_earnings(db, sym, day)
        return out

    def _bars_retry(self, db, syms: list[str], now: _dt.datetime) -> int:
        """Owed end-of-day stock bars (outside the session): a few per tick, each once its
        back-off ends; a symbol that left every basket is dropped. Returns the reads made."""
        if not self._bars_owed or not self._allowed("bars", now):
            return 0
        held = set(syms)
        day = self._bars_day
        t0 = self._now()
        n = 0
        for sym in [s for s, (_, at) in self._bars_owed.items() if at <= now]:
            if sym not in held:
                self._bars_owed.pop(sym, None)
                continue
            if (n >= self.batch or not self._allowed("bars", self._now())
                    or (self._now() - t0).total_seconds() >= self.tick_budget_s):
                break
            ok, res = self._bars_update(db, sym)
            n += 1
            if self._paused_all():
                self._bars_settle(sym, day, False, None)
                break
            if self._bars_settle(sym, day, ok, res):
                self.log.info("eod %s: stock bars of %s done on a retry", sym, day)
        return n

    def _finish_eod(self, db, day: str) -> None:
        try:
            pruned = opt_store.prune_v2(db, today=clock.et_today(self._now()), now=self._now())
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            pruned = None
            self.log.warning("prune failed: %s", exc)
        self.last_eod_on = day
        self._eod = None
        self.state = "idle"
        self.detail = "end-of-day %s done (%s%s)" % (day, _plural(self.symbols_total, "ticker"),
                                                     self._owed_words(day, "; "))
        self.log.info("end-of-day %s done: %s%s; prune %s", day, _plural(self.symbols_total, "ticker"),
                      self._owed_words(day, "; "), pruned)

    def _owed_words(self, day: str | None, lead: str) -> str:
        """``"<lead>stock bars of N tickers still to come"`` while bars of ``day`` are owed."""
        if not self._bars_owed or day is None or self._bars_day != day:
            return ""
        return "%sstock bars of %s still to come" % (lead, _plural(len(self._bars_owed), "ticker"))

    # ── phases ──

    def _eod_day_for(self, now: _dt.datetime) -> str | None:
        """The trading day whose end-of-day pass may run now: today after 16:20 ET, the
        previous trading day before today's open (a missed evening is caught up), the
        last trading day on a weekend / holiday; None from the open to 16:20 ET."""
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
            return "session", None
        day = self._eod_day_for(now)
        if day and (self.last_eod_on or "") < day:
            return "eod", day
        return "idle", None

    def _backing_off(self, sym: str, now: _dt.datetime) -> bool:
        r = self._hist_retry.get(sym)
        return r is not None and now < r[1]

    def _first_reads(self, db, pending: list[str], now: _dt.datetime) -> list[str]:
        """Symbols without history that were never quoted (and not tried in the last
        30 min): their chain is read while the history cannot run."""
        if not pending:
            return []
        fr = opt_store.freshness(db, pending)
        out = []
        for s in pending:
            if s in fr:
                continue
            t = self._first_tried.get(s)
            if t is not None and (now - t).total_seconds() < FIRST_READ_RETRY_S:
                continue
            out.append(s)
        return out

    def _end_pass(self, now: _dt.datetime, *, cut: bool = False) -> None:
        self.cycle_finished = now
        self._cycle = None
        self.state = "idle"
        self.detail = "pass %d %s at %s (%d/%d tickers%s)" % (
            self.cycle_n, "cut short by the close" if cut else "finished", _et_hm(now),
            self.symbols_done, self.symbols_total,
            ", %d failed" % self._pass_failed if self._pass_failed else "")
        self.log.info(self.detail)

    def _pass_tick(self, db, syms: list[str], now: _dt.datetime) -> None:
        """A few symbols of the session pass; a new pass every ``cycle_min`` minutes."""
        if self._cycle is None:
            if (self.cycle_started is not None
                    and (now - self.cycle_started).total_seconds() < self.cycle_min * 60):
                nxt = self.cycle_started + _dt.timedelta(minutes=self.cycle_min)
                self.state = "idle"
                self.detail = "pass %d done; the next starts %s (every %d min)" % (
                    self.cycle_n, _et_hm(nxt), self.cycle_min)
                return
            if not syms:
                self.state = "idle"
                self.detail = "no ticker in any member's basket"
                return
            if not self._allowed("chain", now):
                return
            self.cycle_n += 1
            self.cycle_started, self.cycle_finished = now, None
            self._cycle = list(syms)
            self._pass_failed = 0
            self.symbols_total, self.symbols_done = len(syms), 0
            self.log.info("pass %d: %s - %s", self.cycle_n, _plural(len(syms), "ticker"), " ".join(syms))
        held = set(syms)
        t0 = self._now()
        n = 0
        while self._cycle and n < self.batch:
            if not self._allowed("chain", self._now()):
                break
            sym = self._cycle[0]
            if sym not in held:                     # left every basket since the pass began
                self._cycle.pop(0)
                self.symbols_done += 1
                continue
            self.state = "cycle"
            self.detail = "pass %d: %s (%d/%d)" % (self.cycle_n, sym, self.symbols_done + 1,
                                                  self.symbols_total)
            self._beat(db)
            ok = self._read(db, sym, "cycle")
            if not ok and not self._allowed("chain", self._now()):
                break                               # paused: this symbol is read when it ends
            self._cycle.pop(0)
            self.symbols_done += 1
            n += 1
            if not ok:
                self._pass_failed += 1
            if (self._now() - t0).total_seconds() >= self.tick_budget_s:
                break
        if not self._cycle:
            self._end_pass(self._now())

    def _eod_tick(self, db, syms: list[str], day: str) -> None:
        """A few symbols of the end-of-day pass; when it is through, the prune."""
        if self._eod is None or self._eod[0] != day:
            self._eod = (day, list(syms))
            self.symbols_total, self.symbols_done = len(syms), 0
            if self._bars_day != day:                    # the new pass's lookback covers older owed bars
                self._bars_day, self._bars_owed = day, {}
            self.log.info("end-of-day %s: %s", day, _plural(len(syms), "ticker"))
        left = self._eod[1]
        held = set(syms)
        t0 = self._now()
        n = 0
        while left and n < self.batch:
            if self._paused_all():
                return
            sym = left[0]
            if sym not in held:
                left.pop(0)
                self.symbols_done += 1
                continue
            self.state = "eod"
            self.detail = "end-of-day %s: %s (%d/%d)" % (day, sym, self.symbols_done + 1,
                                                        self.symbols_total)
            self._beat(db)
            if self._eod_one(db, sym, day) is None:
                return
            left.pop(0)
            self.symbols_done += 1
            n += 1
            if (self._now() - t0).total_seconds() >= self.tick_budget_s:
                break
        if not left:
            self._finish_eod(db, day)

    def _idle(self, now: _dt.datetime) -> None:
        self.state = "idle"
        et = clock.et_now(now)
        d, t = et.date(), et.time()
        if clock.is_trading_day(d) and clock.SESSION_CLOSE <= t < EOD_AFTER:
            self.detail = "market closed; the end-of-day pass starts %02d:%02d ET" % (
                EOD_AFTER.hour, EOD_AFTER.minute)
            return
        nxt = d if (clock.is_trading_day(d) and t < clock.SESSION_OPEN) else clock.next_trading_day(d)
        eod = ("end-of-day %s done%s; " % (self.last_eod_on, self._owed_words(self.last_eod_on, ", "))
               if self.last_eod_on else "")
        self.detail = "%snext session %s 09:30 ET" % (eod, nxt.isoformat())

    def _universe(self, db) -> tuple[list[str], list[str]]:
        """(universe symbols most-held first, those still without history)."""
        syms = [s for s, _ in opt_store.universe(db)]
        self.universe_n = len(syms)
        unds = opt_store.underlyings(db, syms) if syms else {}
        pending = [s for s in syms if not (unds.get(s) or {}).get("history_done")]
        self.history_pending = len(pending)
        return syms, pending

    def _pass_due(self, syms: list[str], now: _dt.datetime) -> bool:
        """Has the session pass work for this tick - running, or due - with chain reads
        not paused? (Histories then wait for the gap after it.)"""
        if not syms or not self._allowed("chain", now):
            return False
        if self._cycle is not None or self.cycle_started is None:
            return True
        return (now - self.cycle_started).total_seconds() >= self.cycle_min * 60

    def _step(self, db, now: _dt.datetime) -> None:
        syms, pending_all = self._universe(db)
        if not self._ready(now):
            return
        phase, day = self._phase(now)
        if phase != "session" and self._cycle is not None:
            self._end_pass(now, cut=True)
        if phase != "eod":
            self._eod = None
        # 1. a session pass that is running or due goes first
        if phase == "session" and self._pass_due(syms, now):
            self._pass_tick(db, syms, now)
            return
        # 2. history in the gaps - one symbol per tick
        pending = [s for s in pending_all if not self._backing_off(s, now)]
        if pending and self._allowed("history", now):
            sym = pending[0]
            n_done = len(syms) - len(pending_all)
            own = self._cycle is None and self._eod is None   # a pass in progress keeps its counts
            self.state = "history"
            if own:
                self.symbols_total, self.symbols_done = len(syms), n_done
            self.detail = "history %d/%d: %s (2 years of daily bars + the IV history)" % (
                n_done + 1, len(syms), sym)
            self._beat(db)
            if self._history_one(db, sym)[0]:
                if own:
                    self.symbols_done = n_done + 1
                self.history_pending = max(0, self.history_pending - 1)
            return
        # 2b. a never-quoted symbol whose history cannot run now still gets its chain
        if pending_all and self._allowed("chain", now):
            first = self._first_reads(db, pending_all, now)
            if first:
                sym = first[0]
                self.state = "history"
                self.detail = "first read %s: the chain now, its history later" % sym
                self._beat(db)
                self._first_tried[sym] = now
                self._read(db, sym, "history")
                return
        # 3. the gap between session passes / the end-of-day pass / idle
        if phase == "session":
            self._pass_tick(db, syms, now)
        elif phase == "eod":
            self._eod_tick(db, syms, day)
        else:
            self._bars_retry(db, syms, now)
            self._idle(now)

    # ── public ──

    @property
    def shown_state(self) -> str:
        return self._shown()[0]

    def tick(self) -> str:
        """One step of the loop; never raises. Returns the state as reported."""
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
                self.last_error = ("tick failed: %s" % self._err_text(exc))[:500]
                self.detail = self.last_error
                self.log.exception("tick failed")
            self._beat(db, force=True)
            return self.shown_state
        finally:
            self._db = None
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def run_forever(self, *, max_ticks: int | None = None) -> None:
        """Tick every ``tick_s`` until Ctrl+C (or ``max_ticks``); a tick that took
        longer than ``tick_s`` is followed by the next after 1 s."""
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

    def _begin_one_off(self, db) -> tuple[list[str], list[str], bool]:
        """A one-off run tries at once: earlier pauses are forgotten, the key looked at now."""
        self._retry_at.clear()
        syms, pending = self._universe(db)
        self._beat(db, force=True)
        return syms, pending, self._ready(self._now(), force=True)

    def _one_off_result(self, out: dict) -> dict:
        """``ok`` False while any Massive error is reported (the CLI exits 2)."""
        scope = self._alert_scope()
        out["ok"] = scope is None
        if scope is not None:
            out["error"] = self._alerts[scope][1]
            out["error_kind"] = self._alerts[scope][0]
        return out

    def run_once(self) -> dict:
        """One full pass now, whatever the clock: the history of every universe symbol
        still missing it (with its chain), then one chain read of every other universe
        symbol. Returns ``{"ok", "symbols", "history", "history_failed", "quoted",
        "failed"}`` (+ ``error`` / ``error_kind``)."""
        out = {"ok": False, "symbols": 0, "history": 0, "history_failed": 0, "quoted": 0, "failed": 0}
        db = self._session()
        try:
            syms, pending, ready = self._begin_one_off(db)
            out["symbols"] = len(syms)
            if not ready:
                return self._one_off_result(out)
            read: set[str] = set()
            for i, sym in enumerate(pending):
                if not self._allowed("history", self._now()):
                    out["history_failed"] += len(pending) - i
                    break
                self.state = "history"
                self.symbols_total, self.symbols_done = len(pending), i
                self.detail = "history (one-off) %d/%d: %s" % (i + 1, len(pending), sym)
                self._beat(db)
                ok, chain = self._history_one(db, sym, read_chain=True)
                out["history" if ok else "history_failed"] += 1
                if chain:
                    read.add(sym)
            order = [s for s in syms if s not in read]
            self.cycle_n += 1
            self.cycle_started, self.cycle_finished = self._now(), None
            self.symbols_total, self.symbols_done = len(order), 0
            self._pass_failed = 0
            for sym in order:
                if not self._allowed("chain", self._now()):
                    out["failed"] += len(order) - self.symbols_done
                    break
                self.state = "cycle"
                self.detail = "pass %d (one-off): %s (%d/%d)" % (
                    self.cycle_n, sym, self.symbols_done + 1, self.symbols_total)
                self._beat(db)
                ok = self._read(db, sym, "cycle")
                out["quoted" if ok else "failed"] += 1
                self._pass_failed += 0 if ok else 1
                self.symbols_done += 1
            self._end_pass(self._now())
            return self._one_off_result(out)
        finally:
            self._beat(db, force=True)
            self._close_session(db)

    def run_history(self, symbols) -> dict:
        """The history (and a chain read) of ``symbols`` now, even when it was done
        before. Returns ``{"ok", "symbols", "done", "failed"}`` (+ ``error``)."""
        syms: list[str] = []
        for s in symbols or ():
            s = str(s or "").strip().upper()
            if s and s not in syms:
                syms.append(s)
        out = {"ok": False, "symbols": len(syms), "done": 0, "failed": 0}
        db = self._session()
        try:
            _, _, ready = self._begin_one_off(db)
            if not ready:
                return self._one_off_result(out)
            self.symbols_total, self.symbols_done = len(syms), 0
            for i, sym in enumerate(syms):
                if not self._allowed("history", self._now()):
                    out["failed"] += len(syms) - i
                    break
                self.state = "history"
                self.detail = "history (one-off) %d/%d: %s" % (i + 1, len(syms), sym)
                self._beat(db)
                out["done" if self._history_one(db, sym, read_chain=True)[0] else "failed"] += 1
                self.symbols_done += 1
            self.state = "idle"
            self.detail = "history (one-off) done: %d ok, %d failed" % (out["done"], out["failed"])
            return self._one_off_result(out)
        finally:
            self._beat(db, force=True)
            self._close_session(db)

    def run_eod(self, day: str | None = None) -> dict:
        """The end-of-day pass now, whatever the clock and ``last_eod_on``, filed under
        ``day`` (default: the day the loop would file now - today after 16:20 ET, the
        previous trading day before the open, the last one on a weekend; today in the
        session). Returns ``{"ok", "day", "symbols", "quoted", "snapshot_rows", "bars",
        "failed"}`` (+ ``error``); a run stopped part-way does not mark the day done."""
        now = self._now()
        day = day or self._eod_day_for(now) or clock.et_today(now)
        out = {"ok": False, "day": day, "symbols": 0, "quoted": 0, "snapshot_rows": 0, "bars": 0,
               "failed": 0}
        db = self._session()
        try:
            syms, _, ready = self._begin_one_off(db)
            out["symbols"] = len(syms)
            if not ready:
                return self._one_off_result(out)
            self._eod = (day, list(syms))
            self.symbols_total, self.symbols_done = len(syms), 0
            left = self._eod[1]
            while left:
                sym = left[0]
                self.state = "eod"
                self.detail = "end-of-day %s (one-off): %s (%d/%d)" % (
                    day, sym, self.symbols_done + 1, self.symbols_total)
                self._beat(db)
                r = self._eod_one(db, sym, day)
                if r is None:
                    break
                left.pop(0)
                self.symbols_done += 1
                out["quoted"] += 1 if r["quote"] else 0
                out["failed"] += 0 if r["quote"] else 1
                out["bars"] += 1 if r["bars"] else 0
                out["snapshot_rows"] += r["snapshot"] or 0
            if not left:
                self._finish_eod(db, day)
            else:
                self._eod = None
                self.state = "idle"
                self.detail = "end-of-day %s (one-off) stopped at %d/%d" % (
                    day, self.symbols_done, self.symbols_total)
            return self._one_off_result(out)
        finally:
            self._beat(db, force=True)
            self._close_session(db)

    def stop(self, reason: str = "stopped") -> None:
        """Heartbeat ``stopped`` and close the client this object made."""
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
            self._write_state(self._fields(), self._now())
        if self._client_factory is not None and self.client is not None:
            self._close_client(self.client)
            self.client = None


__all__ = ["Collector", "default_client", "env_cycle_min", "STATE_PATH", "TICK_S", "BATCH",
           "EOD_AFTER", "COLLECTOR_VERSION", "NO_KEY_TEXT", "KEY_RETRY_S", "PLAN_RETRY_S",
           "NETWORK_RETRY_S", "HISTORY_RETRY_S", "CYCLE_MIN_DEFAULT"]
