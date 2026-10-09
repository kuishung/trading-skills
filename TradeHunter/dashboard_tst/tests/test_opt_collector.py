"""The Hermes options collector (OPTIONS_V2_DESIGN.md §4): ``app/services/opt_collector.py``,
``deploy/options_collector.py``, ``deploy/setup_options_collector_task.ps1`` and the tray
line in ``dashboard_intraday/tray_status.py``.

The collector runs against a FAKE fetch module (th_ibkr's function names, async,
recording every call), a fake IB connection and a fake clock, on a fresh SQLite file
migrated to the Alembic head (conftest). Nothing touches IBKR or the network: the
earnings lookup is stubbed for every test.
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import opt_collector, opt_store

from .conftest import DASH_ROOT
from .fixtures.options import bs_greeks

# 2026-10-08 is a Thursday (a trading day); New York is UTC-4 (EDT).
DAY = "2026-10-08"
PRE = _dt.datetime(2026, 10, 8, 12, 0)        # 08:00 ET
RTH = _dt.datetime(2026, 10, 8, 14, 0)        # 10:00 ET
AFTER_CLOSE = _dt.datetime(2026, 10, 8, 20, 5)  # 16:05 ET
EOD = _dt.datetime(2026, 10, 8, 20, 20)       # 16:20 ET
# The ingest supervisor's Gateway window (Hermes): OFF Mon-Fri 08:00-20:10 ET, opened at 20:10 ET
THU_2015 = _dt.datetime(2026, 10, 9, 0, 15)    # Thu 20:15 ET - the supervisor is starting the Gateway
THU_2030 = _dt.datetime(2026, 10, 9, 0, 30)    # Thu 20:30 ET - the top-up is running
THU_2040 = _dt.datetime(2026, 10, 9, 0, 40)    # Thu 20:40 ET
THU_2100 = _dt.datetime(2026, 10, 9, 1, 0)     # Thu 21:00 ET
THU_2200 = _dt.datetime(2026, 10, 9, 2, 0)     # Thu 22:00 ET
FRI_0801 = _dt.datetime(2026, 10, 9, 12, 1)    # Fri 08:01 ET - the blackout has begun
SAT_0005 = _dt.datetime(2026, 10, 10, 4, 5)    # Sat 00:05 ET - weekend seeding starts
SAT_1200 = _dt.datetime(2026, 10, 10, 16, 0)   # Sat 12:00 ET
EXPIRIES = ("2026-10-16", "2026-11-20")
STRIKES = (90.0, 95.0, 100.0, 105.0, 110.0)
N_ROWS = len(EXPIRIES) * len(STRIKES) * 2

TRAY = DASH_ROOT.parent / "dashboard_intraday" / "tray_status.py"
CLI = DASH_ROOT / "deploy" / "options_collector.py"
PS1 = DASH_ROOT / "deploy" / "setup_options_collector_task.ps1"


# ───────────────────────────────────────── fakes ─────────────────────────────────────────

class Clock:
    def __init__(self, t: _dt.datetime):
        self.t = t

    def __call__(self) -> _dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += _dt.timedelta(**kw)


class FakeIB:
    def __init__(self):
        self.connected = True

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False


class FakeConnect:
    """``connect()`` for the collector: async, ``(ib, "host:port")``, or raises."""

    def __init__(self):
        self.calls = 0
        self.down = False
        self.ibs: list[FakeIB] = []

    async def __call__(self):
        self.calls += 1
        if self.down:
            raise ConnectionError("IB Gateway / TWS not reachable on 127.0.0.1 - tried 4002 (refused)")
        ib = FakeIB()
        self.ibs.append(ib)
        return ib, "127.0.0.1:4002"


def _days(end: str, n: int) -> list[str]:
    d = _dt.date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= _dt.timedelta(days=1)
    return out[::-1]


def _rows(spot: float = 100.0, today: str = DAY) -> list[dict]:
    """th_ibkr-shaped rows priced by Black-Scholes (iv a FRACTION)."""
    out = []
    for e in EXPIRIES:
        T = max((_dt.date.fromisoformat(e) - _dt.date.fromisoformat(today)).days, 1) / 365.0
        for k in STRIKES:
            for r in ("C", "P"):
                g = bs_greeks(spot, k, T, 0.40, r)
                mid = round(max(0.05, g["price"]), 2)
                out.append({"expiry": e, "right": r, "strike": k, "bid": round(mid - 0.05, 2),
                            "ask": round(mid + 0.05, 2), "mid": mid, "last": mid, "bid_size": 10,
                            "ask_size": 12, "volume": 40, "oi": 900, "iv": 0.40,
                            "delta": round(g["delta"], 4), "gamma": round(g["gamma"], 5),
                            "theta": round(g["theta"], 4), "vega": round(g["vega"], 4),
                            "und_price": spot})
    return out


class FakeFetch:
    """th_ibkr's functions (async where th_ibkr's are), recording every call as
    ``(name, symbol, kwargs)``. ``fail[(name, symbol)]`` makes that call raise."""

    VERSION = "2.0"

    def __init__(self, mdt: str = "live"):
        self.calls: list[tuple] = []
        self.fail: dict = {}
        self.mdt = mdt
        self.bars_end = "2026-10-07"
        self.progress_seen = 0

    def _rec(self, name, sym, **kw):
        self.calls.append((name, sym, kw))
        exc = self.fail.get((name, sym))
        if exc is not None:
            raise exc

    def reset_mdt(self):
        self.calls.append(("reset_mdt", None, {}))

    async def chain_defs(self, ib, symbol):
        self._rec("chain_defs", symbol)
        return {"symbol": symbol, "con_id": 1, "exchange": "SMART", "expiries": list(EXPIRIES),
                "strikes": list(STRIKES), "multiplier": 100}

    def plan(self, defs, *, spot, iv_hint=None, today=None, **kw):
        self._rec("plan", defs["symbol"], spot=spot, iv_hint=iv_hint, today=today)
        return [{"expiry": e, "dte": 10, "strikes": list(defs["strikes"])} for e in defs["expiries"]]

    async def spot(self, ib, symbol, *, mdt_pref=(1, 2, 3, 4)):
        self._rec("spot", symbol, mdt_pref=tuple(mdt_pref))
        return {"spot": 100.0, "bid": 99.95, "ask": 100.05, "last": 100.0, "close": 99.0,
                "mdt": self.mdt}

    async def quote(self, ib, symbol, window, *, max_lines=60, wait=4.0, mdt_pref=(1, 2, 3, 4),
                    progress=None, spot=None):
        self._rec("quote", symbol, mdt_pref=tuple(mdt_pref), max_lines=max_lines, spot=spot)
        rows = _rows()
        if progress is not None:
            progress(len(rows) // 2, len(rows))
            progress(len(rows), len(rows))
            self.progress_seen += 2
        return {"symbol": symbol, "spot": spot, "mdt": self.mdt, "rows": rows,
                "requested": len(rows), "filled": len(rows), "ms": 1234}

    async def daily_bars(self, ib, symbol, duration="2 Y"):
        self._rec("daily_bars", symbol, duration=duration)
        n = {"2 Y": 504, "1 Y": 252, "5 D": 5}.get(duration, 30)
        out = []
        for i, d in enumerate(_days(self.bars_end, n)):
            c = 100.0 + 5.0 * ((i % 40) - 20) / 20.0 + i * 0.01
            out.append({"on": d, "open": c - 0.3, "high": c + 1.2, "low": c - 1.1, "close": c,
                        "volume": 2_000_000 + 1000 * (i % 7)})
        return out

    async def iv_history(self, ib, symbol, duration="1 Y"):
        self._rec("iv_history", symbol, duration=duration)
        n = {"1 Y": 252, "1 M": 21}.get(duration, 30)
        return [{"on": d, "iv": round(25.0 + (i % 30) * 0.5, 2)}
                for i, d in enumerate(_days(self.bars_end, n))]

    # what the assertions read
    def names(self, sym=None) -> list[str]:
        return [c[0] for c in self.calls if sym is None or c[1] == sym]

    def of(self, name, sym=None) -> list[tuple]:
        return [c for c in self.calls if c[0] == name and (sym is None or c[1] == sym)]

    def quoted(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "quote"]


# ───────────────────────────────────────── fixtures ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No test reaches Yahoo; no in-process lease / rate state leaks between tests."""
    from app.services import prices

    calls: list[str] = []

    def fake_earnings(symbol):
        calls.append(symbol)
        return {"date": "2026-10-28", "days": 20}

    monkeypatch.setattr(prices, "fetch_next_earnings", fake_earnings)
    for var in ("TST_IBKR_PORT", "TST_OPTIONS_COLLECTOR_CLIENT_ID", "TST_OPTIONS_MAX_LINES"):
        monkeypatch.delenv(var, raising=False)
    opt_store.reset_state()
    yield calls
    opt_store.reset_state()


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def sup_dir(tmp_path):
    """Where the tests' ingest supervisor 'publishes' its state + heartbeat files. Empty
    by default = no supervisor on this PC (history never deferred)."""
    return tmp_path / "supervisor"


def _sup_files(sup_dir, *, last=None, hb=None, action="IDLE"):
    """Write what scripts/ingest_supervisor.py writes: the state file (``last_success_session``,
    an ET date) and, when ``hb`` (naive UTC) is given, the per-tick heartbeat."""
    sup_dir.mkdir(parents=True, exist_ok=True)
    (sup_dir / "ingest_supervisor_state.json").write_text(
        json.dumps({"last_success_session": last} if last else {}), encoding="utf-8")
    if hb is not None:
        (sup_dir / "supervisor_heartbeat.json").write_text(json.dumps(
            {"ts": hb.replace(tzinfo=_dt.timezone.utc).isoformat(), "action": action,
             "et": "Thu 20:30 ET"}), encoding="utf-8")


@pytest.fixture
def build(Session, tmp_path, sup_dir):
    """``build(clock, **kw) -> (collector, fetch, connect)``; every collector is stopped
    at teardown (closes its private event loop)."""
    made = []

    def _build(clk, *, fetch=None, connect=None, **kw):
        fetch = fetch or FakeFetch()
        connect = connect or FakeConnect()
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("state_path", tmp_path / "state" / "options_collector.json")
        kw.setdefault("supervisor_dir", sup_dir)
        kw.setdefault("log", logging.getLogger("test_opt_collector"))
        col = opt_collector.Collector(Session, fetch=fetch, connect=connect, clock=clk, **kw)
        made.append(col)
        return col, fetch, connect

    yield _build
    for col in made:
        try:
            col.stop("test teardown")
        except Exception:  # noqa: BLE001
            pass


def _member(db, email, name):
    u = models.User(email=email, display_name=name, role=models.ROLE_MEMBER, status=models.APPROVED)
    db.add(u)
    db.commit()
    return u


def _basket(db, user, *symbols):
    for i, s in enumerate(symbols):
        db.add(models.OptionBasket(user_id=user.id, owner_key="u%d" % user.id, symbol=s,
                                   active=True, added_on=DAY, pos=i))
    db.commit()


def _history_done(db, *symbols):
    for s in symbols:
        opt_store.mark_history_done(db, s)


def _status(db) -> dict:
    db.expire_all()
    return opt_store.collector_status(db) or {}


def _logs(db, sym=None) -> list:
    db.expire_all()
    q = db.query(models.OptRefreshLog)
    if sym:
        q = q.filter(models.OptRefreshLog.symbol == sym)
    return q.order_by(models.OptRefreshLog.id).all()


def _n_quotes(db, sym) -> int:
    db.expire_all()
    return db.query(models.OptQuote).filter(models.OptQuote.symbol == sym).count()


# ───────────────────────────────────────── history ─────────────────────────────────────────

def test_history_first_for_a_new_symbol(db, user, build):
    _basket(db, user, "AAA")
    clk = Clock(RTH)
    col, fetch, conn = build(clk)

    assert col.tick() == "history"
    assert fetch.names("AAA") == ["daily_bars", "iv_history", "chain_defs", "spot", "plan", "quote"]
    assert fetch.of("daily_bars")[0][2] == {"duration": "2 Y"}
    assert fetch.of("iv_history")[0][2] == {"duration": "1 Y"}
    assert fetch.of("quote")[0][2]["mdt_pref"] == (1, 2, 3, 4)       # in the session: live first
    assert fetch.of("quote")[0][2]["max_lines"] == 60

    db.expire_all()
    und = opt_store.underlying(db, "AAA")
    assert und["history_done"] is True
    assert und["atr14"] and und["hv20"] and und["hv60"] and und["avg_vol20"]
    assert und["iv30"] == pytest.approx(25.0 + (251 % 30) * 0.5)     # the last IBKR value
    assert und["iv_n"] == 252 and und["iv_rank"] is not None
    assert und["spot"] == 100.0 and und["spot_source"] == "hermes" and und["spot_mdt"] == "live"
    assert db.query(models.OptUnderlyingDaily).filter_by(symbol="AAA").count() == 504

    assert _n_quotes(db, "AAA") == N_ROWS
    q = db.query(models.OptQuote).filter_by(symbol="AAA").first()
    assert (q.source, q.mdt, q.source_user_id, q.as_of) == ("hermes", "live", None, RTH)
    (lg,) = _logs(db, "AAA")
    assert (lg.kind, lg.n_contracts, lg.n_expiries, lg.error) == ("history", N_ROWS, 2, None)

    st = _status(db)
    assert st["state"] == "history" and st["gateway"] == "127.0.0.1:4002" and st["gateway_ok"] is True
    assert (st["symbols_done"], st["symbols_total"]) == (1, 1)

    # the next tick does not pull history again, and the cycle skips what the
    # first-time read just filled (no duplicate read)
    clk.advance(seconds=15)
    col.tick()
    assert len(fetch.of("daily_bars")) == 1
    assert fetch.quoted() == ["AAA"]
    assert col.cycle_n == 1 and col.state == "idle"


def test_history_failure_backs_off_and_is_logged(db, user, build):
    _basket(db, user, "NEW")
    clk = Clock(RTH)
    col, fetch, conn = build(clk)
    fetch.fail[("daily_bars", "NEW")] = RuntimeError("HMDS query returned no data")

    col.tick()
    (lg,) = _logs(db, "NEW")
    assert lg.kind == "history" and lg.n_contracts == 0 and "HMDS" in lg.error
    assert not (opt_store.underlying(db, "NEW") or {}).get("history_done")
    assert "history NEW" in col.last_error

    clk.advance(seconds=15)                    # backing off: not retried yet
    col.tick()
    assert len(fetch.of("daily_bars")) == 1

    del fetch.fail[("daily_bars", "NEW")]
    clk.advance(minutes=31)                    # 30 min later it is
    col.tick()
    assert len(fetch.of("daily_bars")) == 2
    db.expire_all()
    assert opt_store.underlying(db, "NEW")["history_done"] is True


class ShortFetch(FakeFetch):
    """IBKR's history service answering with FEWER points than asked - or none at all,
    which is how ib_insync reports a failed historical request (pacing, HMDS down, its
    own 60 s timeout): an empty list, no exception. ``None`` = the full answer."""

    def __init__(self, bars_n=None, iv_n=None):
        super().__init__()
        self.bars_n, self.iv_n = bars_n, iv_n

    @staticmethod
    def _cut(out, n):
        return out if n is None else (out[len(out) - n:] if n > 0 else [])

    async def daily_bars(self, ib, symbol, duration="2 Y"):
        return self._cut(await super().daily_bars(ib, symbol, duration), self.bars_n)

    async def iv_history(self, ib, symbol, duration="1 Y"):
        return self._cut(await super().iv_history(ib, symbol, duration), self.iv_n)


@pytest.mark.parametrize("bars_n, iv_n", [(0, 0), (None, 0)])
def test_empty_history_is_a_failure_not_done(db, user, build, bars_n, iv_n):
    """#18: an empty answer (e.g. HMDS down over IBKR's weekend reset) used to be filed
    as the symbol's history - history_done True, IV rank / ATR None for good, nothing
    ever retried it. It is a failure now, retried with the back-off."""
    _basket(db, user, "CRWD")
    clk = Clock(SAT_0005 + _dt.timedelta(minutes=20))       # Sat 00:25 ET, Gateway up
    fetch = ShortFetch(bars_n=bars_n, iv_n=iv_n)
    col, _, _ = build(clk, fetch=fetch)

    col.tick()
    db.expire_all()
    assert not (opt_store.underlying(db, "CRWD") or {}).get("history_done")
    (lg,) = [lg for lg in _logs(db, "CRWD") if lg.kind == "history"]
    assert lg.n_contracts == 0 and "no history" in lg.error and "history CRWD" in col.last_error
    if bars_n == 0:
        assert fetch.of("iv_history") == []                  # no bars: the IV request is spared
    assert fetch.of("quote") == []

    clk.advance(minutes=31)                                  # the back-off ends; data now there
    fetch.bars_n = fetch.iv_n = None
    col.tick()
    db.expire_all()
    und = opt_store.underlying(db, "CRWD")
    assert und["history_done"] is True and und["iv_rank"] is not None and und["atr14"]


def test_a_young_listing_is_filed_when_a_later_day_confirms_it(db, user, build):
    _basket(db, user, "IPO")
    clk = Clock(SAT_1200)
    fetch = ShortFetch(bars_n=12, iv_n=9)                    # listed a few weeks ago
    col, _, _ = build(clk, fetch=fetch)

    col.tick()
    db.expire_all()
    assert not (opt_store.underlying(db, "IPO") or {}).get("history_done")
    assert db.query(models.OptUnderlyingDaily).filter_by(symbol="IPO").count() == 12   # still filed
    assert "young listing" in col.last_error

    clk.advance(minutes=31)                                  # the same day again: not yet
    col.tick()
    assert len(fetch.of("daily_bars", "IPO")) == 2
    db.expire_all()
    assert not opt_store.underlying(db, "IPO")["history_done"]

    clk.t = SAT_1200 + _dt.timedelta(days=1)                 # Sunday: as much again -> history
    col.tick()
    assert len(fetch.of("daily_bars", "IPO")) == 3
    db.expire_all()
    assert opt_store.underlying(db, "IPO")["history_done"] is True
    assert fetch.quoted() == ["IPO"]                         # then its first chain read


def test_an_empty_eod_increment_is_retried(db, user, build):
    """#18 (increments): an empty 5 D answer was taken as "nothing new" for the day."""
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    opt_store.upsert_daily(db, "AAA", _bars("2026-10-07", 30), None, source="hermes")
    clk = Clock(EOD)
    fetch = ShortFetch(bars_n=0)
    col, _, _ = build(clk, fetch=fetch)

    col.tick()                                               # the EOD pass: chain yes, bars none
    assert col.last_eod_on == DAY and fetch.quoted() == ["AAA"]
    (lg,) = [lg for lg in _logs(db, "AAA") if lg.kind == "eod" and lg.error]
    assert "no daily bars" in lg.error

    clk.advance(seconds=15)
    n = len(fetch.of("daily_bars"))
    assert col.tick() == "idle" and len(fetch.of("daily_bars")) == n   # not before 30 min

    fetch.bars_n, fetch.bars_end = None, DAY
    clk.advance(minutes=31)
    assert col.tick() == "history" and "increments" in col.detail
    db.expire_all()
    assert db.query(models.OptUnderlyingDaily).filter_by(symbol="AAA", on=DAY).count() == 1


# ───────────────────────────────────────── RTH cycles ─────────────────────────────────────────

def test_rth_cycle_priority_order_and_member_fresh_skip(db, user, build):
    u2 = _member(db, "two@local.test", "Kui")
    _basket(db, user, "A", "B", "D")
    _basket(db, u2, "A", "C")
    _history_done(db, "A", "B", "C", "D")
    rows = _rows()
    for sym, mins in (("A", 50), ("B", 40), ("D", 60)):
        opt_store.upsert_quotes(db, sym, rows, source="hermes", mdt="live", kind="cycle",
                                as_of=RTH - _dt.timedelta(minutes=mins))
    # a member's connector refreshed B five minutes ago
    opt_store.upsert_quotes(db, "B", rows, source="member", user_id=user.id, mdt="live",
                            kind="member", now=RTH - _dt.timedelta(minutes=5))

    clk = Clock(RTH)
    col, fetch, conn = build(clk)
    states = []
    for _ in range(4):
        states.append(col.tick())
        clk.advance(seconds=15)

    # never quoted first (C), then most held (A, 2 members), then the oldest data (D);
    # B is skipped - a member refreshed it under 10 minutes ago
    assert fetch.quoted() == ["C", "A", "D"]
    assert all(c[2]["mdt_pref"] == (1, 2, 3, 4) for c in fetch.of("quote"))
    assert states == ["cycle", "cycle", "cycle", "idle"]
    assert col.cycle_n == 1 and col.cycle_finished is not None
    assert (col.symbols_done, col.symbols_total) == (4, 4)
    assert [lg.kind for lg in _logs(db, "C")] == ["cycle"]
    st = _status(db)
    assert st["cycle_n"] == 1 and st["state"] == "idle" and "finished" in st["phase_detail"]

    # within 10 min of the cycle's start no new cycle begins
    col.tick()
    assert fetch.quoted() == ["C", "A", "D"]
    assert "next starts" in col.detail

    # then cycle 2: everything has data now, so the most-held symbol goes first
    clk.t = RTH + _dt.timedelta(minutes=10, seconds=1)
    assert col.tick() == "cycle"
    assert col.cycle_n == 2 and fetch.quoted()[-1] == "A"


def test_symbol_failure_is_logged_and_the_loop_continues(db, user, build):
    _basket(db, user, "AAA", "BAD")
    _history_done(db, "AAA", "BAD")
    clk = Clock(RTH)
    col, fetch, conn = build(clk)
    fetch.fail[("quote", "BAD")] = RuntimeError("IBKR does not recognise the symbol BAD.")

    assert col.tick() == "cycle"                # AAA
    clk.advance(seconds=15)
    assert col.tick() == "idle"                 # BAD fails, the cycle still ends cleanly
    assert fetch.quoted() == ["AAA", "BAD"]
    (lg,) = _logs(db, "BAD")
    assert lg.kind == "cycle" and lg.n_contracts == 0 and "does not recognise" in lg.error
    assert lg.source == "hermes"
    assert "BAD" in col.last_error and _status(db)["last_error"] == col.last_error
    assert _n_quotes(db, "AAA") == N_ROWS and _n_quotes(db, "BAD") == 0
    assert conn.ibs[0].connected                # a symbol failure is not a connection failure


def test_cycle_is_cut_at_the_close(db, user, build):
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    clk = Clock(_dt.datetime(2026, 10, 8, 19, 59, 50))      # 15:59:50 ET
    col, fetch, conn = build(clk)
    col.tick()
    assert fetch.quoted() == ["AAA"] and col._cycle == ["BBB"]
    clk.t = AFTER_CLOSE                                      # 16:05 ET: no cycle, no EOD yet
    assert col.tick() == "idle"
    assert fetch.quoted() == ["AAA"]
    assert "16:15" in col.detail and col._cycle is None


# ───────────────────────────────────────── EOD ─────────────────────────────────────────

def test_eod_pass_runs_once_per_trading_day(db, user, build, _clean):
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    expired = dict(_rows()[0], expiry="2026-09-25")
    opt_store.upsert_quotes(db, "AAA", [expired], source="hermes", mdt="live",
                            as_of=EOD - _dt.timedelta(days=13))

    clk = Clock(EOD)
    col, fetch, conn = build(clk)
    assert col.tick() == "eod"                  # one symbol per tick
    assert fetch.quoted() == ["AAA"]
    assert col.last_eod_on is None
    assert db.query(models.OptQuote).filter_by(expiry="2026-09-25").count() == 1   # prune at the end

    clk.advance(seconds=15)
    assert col.tick() == "idle"
    assert fetch.quoted() == ["AAA", "BBB"]
    assert col.last_eod_on == DAY and _status(db)["last_eod_on"] == DAY

    # frozen first after the close, history increments, the snapshot, earnings, prune
    assert all(c[2]["mdt_pref"] == (2, 1, 4, 3) for c in fetch.of("quote") + fetch.of("spot"))
    assert [c[2]["duration"] for c in fetch.of("daily_bars")] == ["5 D", "5 D"]
    assert [c[2]["duration"] for c in fetch.of("iv_history")] == ["1 M", "1 M"]
    db.expire_all()
    S = models.OptionChainSnapshot
    for sym in ("AAA", "BBB"):
        snap = db.query(S).filter_by(symbol=sym, snap_on=DAY, kind="eod").all()
        assert len(snap) == N_ROWS and {s.source for s in snap} == {"ibkr"}
        assert opt_store.underlying(db, sym)["earnings_date"] == "2026-10-28"
        assert opt_store.underlying(db, sym)["earnings_src"] == "yahoo"
        assert [lg.kind for lg in _logs(db, sym) if lg.kind == "eod"] == ["eod"]
    assert _clean == ["AAA", "BBB"]
    assert db.query(models.OptQuote).filter_by(expiry="2026-09-25").count() == 0

    # later the same evening: nothing more
    n_calls = len(fetch.calls)
    clk.advance(minutes=20)
    assert col.tick() == "idle"
    assert len(fetch.calls) == n_calls

    # the next trading day: once again
    clk.t = EOD + _dt.timedelta(days=1)
    col.tick()
    clk.advance(seconds=15)
    col.tick()
    clk.advance(seconds=15)
    col.tick()
    assert fetch.quoted() == ["AAA", "BBB", "AAA", "BBB"]
    assert col.last_eod_on == "2026-10-09"


def test_missed_eod_is_caught_up_before_the_next_open(db, user, build):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(_dt.datetime(2026, 10, 9, 12, 0))          # Friday 08:00 ET, Thursday's EOD missed
    col, fetch, conn = build(clk)
    col.tick()
    assert col.last_eod_on == DAY
    db.expire_all()
    assert db.query(models.OptionChainSnapshot).filter_by(symbol="AAA", snap_on=DAY).count() == N_ROWS
    clk.advance(seconds=15)
    assert col.tick() == "idle"                            # Friday's own EOD waits for 16:15
    assert "next session 2026-10-09" in col.detail


def test_eod_restored_from_the_status_row_after_a_restart(db, user, build):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    opt_store.set_collector_status(db, last_eod_on=DAY, cycle_n=7)
    col, fetch, conn = build(Clock(EOD + _dt.timedelta(minutes=30)))
    assert col.tick() == "idle"
    assert fetch.quoted() == [] and col.cycle_n == 7


# ───────────────────────────────────────── gateway ─────────────────────────────────────────

def test_gateway_down_heartbeats_error_and_retries(db, user, build, tmp_path):
    _basket(db, user, "AAA")
    # Thursday 21:00 ET: outside the blackout and past the supervisor's 15-min start
    # grace, no supervisor state on this PC -> an unreachable Gateway is an error
    t0 = THU_2100
    clk = Clock(t0)
    conn = FakeConnect()
    conn.down = True
    col, fetch, _ = build(clk, connect=conn)

    assert col.tick() == "error"
    st = _status(db)
    assert st["state"] == "error" and st["gateway_ok"] is False
    assert st["heartbeat"] == t0 and "not reachable" in st["last_error"]
    assert "retry in 60 s" in st["phase_detail"]
    doc = json.loads((tmp_path / "state" / "options_collector.json").read_text(encoding="utf-8"))
    assert doc["state"] == "error" and doc["gateway_ok"] is False
    assert doc["down_since"].startswith("2026-10-09T01:00:00")
    assert doc["wait_reason"] is None and doc["wait_until"] is None
    assert fetch.calls == [] and conn.calls == 1

    clk.advance(seconds=30)                     # still waiting: no attempt, but a heartbeat
    assert col.tick() == "error"
    assert conn.calls == 1 and _status(db)["heartbeat"] == t0 + _dt.timedelta(seconds=30)

    clk.advance(seconds=31)                     # 61 s after the failure: one more try
    col.tick()
    assert conn.calls == 2 and "retry in 120 s" in col.detail     # backing off

    conn.down = False
    clk.advance(seconds=121)
    assert col.tick() == "history"              # connected; work resumes
    assert conn.calls == 3 and col.gateway_ok and _status(db)["gateway_ok"] is True
    assert ("reset_mdt", None, {}) in fetch.calls

    # the gateway drops the connection: the next tick reconnects at once
    conn.ibs[-1].connected = False
    clk.advance(seconds=15)
    col.tick()
    assert conn.calls == 4 and col.gateway_ok


def _quiet_log(caplog):
    """A logger caplog sees: the migrations (Alembic's fileConfig) disable every logger
    that existed before them, so the test takes its own and re-enables it."""
    lg = logging.getLogger("test_opt_collector.state_lines")
    lg.disabled = False
    lg.propagate = True
    caplog.set_level(logging.INFO, logger=lg.name)
    return lg


def _lines(caplog, needle):
    return [r for r in caplog.records
            if r.name == "test_opt_collector.state_lines" and needle in r.getMessage()]


def test_a_persisting_waiting_state_logs_once_per_30_min(db, user, build, caplog):
    """Review (log growth): the Gateway is down by design most of every weekday and
    tried every minute - the state is logged when it begins, then once per 30 min."""
    lg = _quiet_log(caplog)
    _basket(db, user, "AAA")
    clk = Clock(RTH)                                         # Thu 10:00 ET: the blackout
    col, fetch, conn = _down(build, clk, log=lg)
    for _ in range(40):                                      # 40 min, a try every minute
        col.tick()
        clk.advance(seconds=61)
    assert conn.calls == 40 and col.state == "waiting"
    assert len(_lines(caplog, "down by design")) == 2        # at 10:00 and ~10:30


def test_a_persisting_gateway_error_logs_once_per_30_min(db, user, build, caplog):
    lg = _quiet_log(caplog)
    _basket(db, user, "AAA")
    clk = Clock(THU_2100)                                    # outside every designed window
    col, fetch, conn = _down(build, clk, log=lg)
    for _ in range(40):
        col.tick()
        clk.advance(seconds=60)
    assert conn.calls >= 8 and col.state == "error"
    assert len(_lines(caplog, "IB connect failed")) == 2
    # after a good connection, the next outage is logged at once
    conn.down = False
    clk.advance(minutes=6)
    col.tick()
    assert col.gateway_ok
    conn.ibs[-1].connected = False
    conn.down = True
    clk.advance(seconds=15)
    col.tick()
    assert len(_lines(caplog, "IB connection lost")) == 1
    assert len(_lines(caplog, "IB connect failed")) == 3


# ─────────────────────────── living with the ingest supervisor (blackout + top-up) ───────────────────────────

def _down(build, clk, **kw):
    conn = FakeConnect()
    conn.down = True
    col, fetch, _ = build(clk, connect=conn, **kw)
    return col, fetch, conn


def test_supervisor_window_is_imported_and_matches_the_built_in_rules(monkeypatch):
    """The schedule comes from scripts/ingest_supervisor.py (loaded without leaving its
    sys.path edits behind); the built-in fallback agrees with it all week."""
    monkeypatch.setattr(opt_collector, "_SUP_MODULE", {})
    before = list(sys.path)
    mod = opt_collector.load_supervisor()
    assert sys.path == before
    assert mod is not None and mod.RUN_START == _dt.time(20, 10) and mod.RUN_END == _dt.time(8, 0)

    loaded = opt_collector.SupervisorWindow()
    builtin = opt_collector.SupervisorWindow(supervisor=None)
    assert loaded.source == "scripts/ingest_supervisor.py" and builtin.source == "built-in"
    assert (loaded.run_start, loaded.run_end, loaded.margin_min) == \
        (builtin.run_start, builtin.run_end, builtin.margin_min) == (_dt.time(20, 10), _dt.time(8, 0), 3)

    from app.services import clock

    t = _dt.datetime(2026, 10, 5, 4, 0)            # Mon 00:00 ET .. the next Mon, every 10 min
    while t < _dt.datetime(2026, 10, 12, 16, 0):
        et = clock.et_now(t)
        for fn in ("blackout", "seeding", "session", "session_due"):
            assert getattr(loaded, fn)(et) == getattr(builtin, fn)(et), (fn, et)
        t += _dt.timedelta(minutes=10)

    def et(y, mo, d, h, mi):
        return clock.et_now(_dt.datetime(y, mo, d, h, mi) + _dt.timedelta(hours=4))   # EDT

    w = loaded
    assert w.blackout(et(2026, 10, 8, 10, 0)) is True              # Thu 10:00
    assert w.blackout(et(2026, 10, 8, 20, 9)) is True              # Thu 20:09
    assert w.blackout(et(2026, 10, 8, 20, 10)) is False            # Thu 20:10 - the run window
    assert w.blackout(et(2026, 10, 9, 7, 59)) is False             # Fri 07:59
    assert w.blackout(et(2026, 10, 10, 12, 0)) is False            # Sat: no blackout
    assert w.blackout(et(2026, 10, 12, 7, 59)) is False            # Mon 07:59 - still seeding
    assert w.blackout(et(2026, 10, 12, 8, 0)) is True              # Mon 08:00
    assert w.session(et(2026, 10, 9, 2, 0)) == _dt.date(2026, 10, 8)      # Fri 02:00 -> Thu
    assert w.session_due(et(2026, 10, 12, 10, 0)) == _dt.date(2026, 10, 9)  # Mon blackout -> Fri
    assert w.session_due(et(2026, 10, 12, 2, 0)) is None                  # Mon 02:00 = Sun session


def test_blackout_gateway_down_is_waiting_not_an_error(db, user, build, tmp_path):
    _basket(db, user, "AAA")
    clk = Clock(RTH)                               # Thursday 10:00 ET
    col, fetch, conn = _down(build, clk)

    assert col.tick() == "waiting"
    st = _status(db)
    assert st["state"] == "waiting" and st["gateway_ok"] is False
    assert st["last_error"] is None and col.last_error is None       # no error recorded
    assert st["heartbeat"] == RTH
    assert st["phase_detail"] == ("Gateway off for the manual-trading blackout until 20:10 ET - "
                                  "members' IBKR connectors carry the session")
    doc = json.loads((tmp_path / "state" / "options_collector.json").read_text(encoding="utf-8"))
    assert (doc["state"], doc["wait_reason"], doc["wait_until"]) == ("waiting", "blackout", "20:10 ET")
    assert doc["down_since"] is None and doc["last_error"] is None
    assert fetch.calls == [] and conn.calls == 1

    clk.advance(seconds=30)                        # no attempt yet, the usual heartbeat
    assert col.tick() == "waiting"
    assert conn.calls == 1 and _status(db)["heartbeat"] == RTH + _dt.timedelta(seconds=30)
    clk.advance(seconds=31)                        # 61 s: one more try
    assert col.tick() == "waiting" and conn.calls == 2
    clk.advance(seconds=61)                        # every 60 s - no doubling back-off
    assert col.tick() == "waiting" and conn.calls == 3
    assert _status(db)["last_error"] is None

    # the tray: neutral (grey), not amber
    got = _tray_function(tmp_path / "state" / "options_collector.json")(
        now=clk.t.replace(tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=10))
    assert got["line"] == "Options collector: waiting (Gateway blackout until 20:10 ET)"
    assert (got["color"], got["level"], got["tip"]) == ("grey", "waiting", "Opt wait")


def test_supervisor_start_grace_then_an_error(db, user, build):
    _basket(db, user, "AAA")
    clk = Clock(THU_2015)                          # 20:15 ET: the supervisor is starting the Gateway
    col, fetch, conn = _down(build, clk)
    assert col.tick() == "waiting"
    assert col.wait_reason == "starting" and col.wait_until == "20:25 ET"
    assert "opens the Gateway at 20:10 ET" in col.detail and col.last_error is None

    clk.t = THU_2040                               # 20:40 ET: past the 15-min grace -> error at once
    assert col.tick() == "error"
    assert conn.calls == 2 and "not reachable" in col.last_error
    assert "retry in 60 s" in col.detail and _status(db)["state"] == "error"

    col2, _, _ = _down(build, Clock(THU_2040))     # a collector starting at 20:40 too
    assert col2.tick() == "error"


def test_weekend_gateway_down_is_an_error(db, user, build):
    _basket(db, user, "AAA")
    col, fetch, conn = _down(build, Clock(SAT_1200))   # weekend: the supervisor keeps it UP
    assert col.tick() == "error"
    assert "not reachable" in col.last_error and col.wait_reason is None

    clk = Clock(SAT_0005)                          # Sat 00:00 ET it starts it for seeding: a grace
    col2, _, _ = _down(build, clk)
    assert col2.tick() == "waiting" and col2.wait_reason == "starting" and col2.wait_until == "00:15 ET"
    clk.advance(minutes=15)
    clk.advance(seconds=1)
    assert col2.tick() == "error"


@pytest.mark.parametrize("utc, last, reason", [
    (_dt.datetime(2026, 10, 8, 14, 0), None, "blackout"),            # Thu 10:00 ET
    (_dt.datetime(2026, 10, 9, 0, 9), None, "blackout"),             # Thu 20:09 ET
    (_dt.datetime(2026, 10, 9, 0, 10), None, "starting"),            # Thu 20:10 ET
    (_dt.datetime(2026, 10, 9, 0, 24), None, "starting"),            # Thu 20:24 ET
    (_dt.datetime(2026, 10, 9, 0, 25), None, None),                  # Thu 20:25 ET: grace over
    (_dt.datetime(2026, 10, 9, 2, 0), "2026-10-08", "closed"),       # Thu 22:00 ET, top-up done
    (_dt.datetime(2026, 10, 9, 2, 0), "2026-10-07", None),           # ... top-up not done: error
    (_dt.datetime(2026, 10, 9, 6, 0), "2026-10-08", "closed"),       # Fri 02:00 ET (Thu session)
    (_dt.datetime(2026, 10, 9, 11, 58), None, "blackout"),           # Fri 07:58 ET: being shut
    (_dt.datetime(2026, 10, 10, 2, 0), "2026-10-09", "closed"),      # Fri 22:00 ET -> Sat 00:00
    (_dt.datetime(2026, 10, 10, 6, 0), "2026-10-09", None),          # Sat 02:00 ET: seeding, up
    (_dt.datetime(2026, 10, 10, 16, 0), None, None),                 # Sat 12:00 ET
    (_dt.datetime(2026, 10, 12, 6, 0), None, None),                  # Mon 02:00 ET: seeding
    (_dt.datetime(2026, 10, 12, 12, 0), None, "blackout"),           # Mon 08:00 ET
])
def test_gateway_off_by_design_table(build, sup_dir, utc, last, reason):
    if last:
        _sup_files(sup_dir, last=last, hb=utc - _dt.timedelta(minutes=1))
    col, _, _ = build(Clock(utc))
    got = col._gateway_off_by_design(utc)
    assert (got[0] if got else None) == reason


def test_after_the_nightly_top_up_gateway_down_is_waiting(db, user, build, sup_dir):
    _basket(db, user, "AAA")
    _sup_files(sup_dir, last="2026-10-08", hb=THU_2200 - _dt.timedelta(minutes=1))
    col, _, _ = _down(build, Clock(THU_2200))
    assert col.tick() == "waiting"
    assert (col.wait_reason, col.wait_until) == ("closed", "20:10 ET")
    assert "closed by the ingest supervisor after tonight's top-up" in col.detail
    assert col.last_error is None

    fri = _dt.datetime(2026, 10, 10, 2, 0)         # Fri 22:00 ET after Friday's top-up
    _sup_files(sup_dir, last="2026-10-09", hb=fri - _dt.timedelta(minutes=1))
    col2, _, _ = _down(build, Clock(fri))
    assert col2.tick() == "waiting" and col2.wait_until == "00:00 ET"
    assert col2.detail.endswith("it opens again at 00:00 ET Saturday")

    # the top-up of that session is NOT done: the Gateway should be up -> error ...
    _sup_files(sup_dir, last="2026-10-07", hb=THU_2200 - _dt.timedelta(minutes=1))
    clk = Clock(THU_2200)
    col3, _, _ = _down(build, clk)
    assert col3.tick() == "error" and "not reachable" in col3.last_error
    # ... until the supervisor writes the session done (it closes the Gateway first):
    # the outage is explained, the error is cleared
    _sup_files(sup_dir, last="2026-10-08", hb=THU_2200)
    clk.advance(seconds=61)
    assert col3.tick() == "waiting" and col3.wait_reason == "closed"
    assert col3.last_error is None and _status(db)["last_error"] is None


def test_connection_closed_by_the_blackout_is_waiting(db, user, build):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(THU_2100)
    col, fetch, conn = build(clk)
    col.tick()                                     # connected; Thursday's EOD pass (one symbol)
    assert col.gateway_ok and col.last_eod_on == DAY
    clk.t = FRI_0801                               # the supervisor shuts the Gateway at 08:00 ET
    conn.ibs[-1].connected = False
    conn.down = True
    assert col.tick() == "waiting"
    assert col.wait_reason == "blackout" and col.last_error is None
    assert _status(db)["last_error"] is None and _status(db)["state"] == "waiting"


def test_history_gate_follows_the_supervisor_session(build, sup_dir):
    col, _, _ = build(Clock(THU_2030))

    def gate(utc, last, hb_age=_dt.timedelta(minutes=1)):
        _sup_files(sup_dir, last=last, hb=utc - hb_age)
        return col._history_gate(utc)

    assert gate(THU_2030, "2026-10-08") == (True, None)                     # tonight's top-up done
    assert gate(THU_2030, "2026-10-07") == (False, opt_collector.HISTORY_WAIT_TEXT)   # running
    assert gate(_dt.datetime(2026, 10, 9, 6, 0), "2026-10-07")[0] is False  # Fri 02:00 (Thu session)
    assert gate(_dt.datetime(2026, 10, 9, 14, 0), "2026-10-08")[0] is True  # Fri 10:00: Thu done
    assert gate(_dt.datetime(2026, 10, 9, 14, 0), "2026-10-07")[0] is False  # ... Thu's failed
    assert gate(_dt.datetime(2026, 10, 10, 6, 0), "2026-10-08")[0] is False  # Sat 02:00: Fri runs
    assert gate(_dt.datetime(2026, 10, 10, 6, 0), "2026-10-09")[0] is True   # ... Fri done
    assert gate(SAT_1200, "2026-10-01")[0] is True                         # weekend daytime
    assert gate(_dt.datetime(2026, 10, 11, 1, 0), "2026-10-01")[0] is True  # Sat 21:00 (Sat session)
    assert gate(_dt.datetime(2026, 10, 12, 6, 0), "2026-10-01")[0] is True  # Mon 02:00 (Sun session)
    assert gate(_dt.datetime(2026, 10, 12, 14, 0), "2026-10-08")[0] is False  # Mon 10:00: Fri due
    assert gate(THU_2030, None)[0] is False                                 # state file, no success yet
    # the supervisor has not ticked for over a day: it does not run here any more
    assert gate(THU_2030, "2026-10-01", hb_age=_dt.timedelta(hours=25))[0] is True
    # a state file caught mid-write keeps the last good read
    _sup_files(sup_dir, last="2026-10-08", hb=THU_2030)
    assert col._history_gate(THU_2030)[0] is True
    (sup_dir / "ingest_supervisor_state.json").write_text("{", encoding="utf-8")
    assert col._history_gate(THU_2030)[0] is True
    # no supervisor on this PC (laptop): never deferred
    for p in sup_dir.iterdir():
        p.unlink()
    assert col._history_gate(THU_2030) == (True, None)


def _bars(end: str, n: int) -> list[dict]:
    return [{"on": d, "open": 99.5, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_500_000}
            for d in _days(end, n)]


def test_history_waits_for_the_top_up_chain_reads_do_not(db, user, build, sup_dir, tmp_path):
    _basket(db, user, "NEW", "OLD")
    _history_done(db, "OLD")
    opt_store.upsert_daily(db, "OLD", _bars("2026-10-07", 30), None, source="hermes")
    _sup_files(sup_dir, last="2026-10-07", hb=THU_2030 - _dt.timedelta(minutes=1))   # top-up running
    clk = Clock(THU_2030)
    col, fetch, conn = build(clk)

    # a new symbol gets its chain read at once; its history waits
    assert col.tick() == "history"
    assert fetch.names("NEW") == ["chain_defs", "spot", "plan", "quote"]
    assert fetch.of("daily_bars") == [] and fetch.of("iv_history") == []
    assert opt_collector.HISTORY_WAIT_TEXT in col.detail
    assert _n_quotes(db, "NEW") == N_ROWS
    assert not (opt_store.underlying(db, "NEW") or {}).get("history_done")
    assert [lg.kind for lg in _logs(db, "NEW")] == ["history"]

    # the EOD pass reads chains, its history increments wait
    clk.advance(seconds=15)
    assert col.tick() == "eod" and opt_collector.HISTORY_WAIT_TEXT in col.detail
    clk.advance(seconds=15)
    assert col.tick() == "idle" and col.last_eod_on == DAY
    assert fetch.quoted() == ["NEW", "NEW", "OLD"]
    assert fetch.of("daily_bars") == [] and fetch.of("iv_history") == []

    clk.advance(seconds=15)                        # nothing else may run: NEW's history + OLD's increments wait
    assert col.tick() == "idle"
    assert col.detail.endswith("history waits for tonight's ingest top-up (2 symbols)")
    assert fetch.of("daily_bars") == [] and len(fetch.quoted()) == 3
    doc = json.loads((tmp_path / "state" / "options_collector.json").read_text(encoding="utf-8"))
    assert doc["history_waiting"] == 2
    got = _tray_function(tmp_path / "state" / "options_collector.json")(
        now=clk.t.replace(tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=5))
    assert got["color"] == "green" and "history waits (2)" in got["line"]

    # the supervisor's state shows tonight's top-up done -> history runs
    _sup_files(sup_dir, last="2026-10-08", hb=clk.t)
    fetch.bars_end = DAY
    clk.advance(seconds=15)
    assert col.tick() == "history"
    assert [c[2]["duration"] for c in fetch.of("daily_bars")] == ["2 Y"]
    assert fetch.of("daily_bars")[0][1] == "NEW" and len(fetch.quoted()) == 3   # chain not re-read
    db.expire_all()
    assert opt_store.underlying(db, "NEW")["history_done"] is True

    clk.advance(seconds=15)                        # OLD's deferred increments are caught up
    assert col.tick() == "history" and "increments" in col.detail
    assert [(c[1], c[2]["duration"]) for c in fetch.of("daily_bars")][-1] == ("OLD", "5 D")
    assert [(c[1], c[2]["duration"]) for c in fetch.of("iv_history")][-1] == ("OLD", "1 M")
    db.expire_all()
    assert db.query(models.OptUnderlyingDaily).filter_by(symbol="OLD", on=DAY).count() == 1

    clk.advance(seconds=15)
    n = len(fetch.calls)
    assert col.tick() == "idle" and "history waits" not in col.detail
    assert len(fetch.calls) == n and col.history_waiting == 0


def test_increment_durations_cover_the_gap(build):
    col, _, _ = build(Clock(RTH))
    d = col._increment_durations
    assert d(None, DAY) == ("5 D", "1 M")
    assert d("2026-10-07", DAY) == ("5 D", "1 M")
    assert d("2026-10-02", DAY) == ("5 D", "1 M")          # 6 days
    assert d("2026-10-01", DAY) == ("1 M", "1 M")          # a week behind (deferred all week)
    assert d("2026-07-01", DAY) == ("6 M", "6 M")
    assert d("2025-01-02", DAY) == ("2 Y", "1 Y")


def test_one_off_runs_respect_the_top_up(db, user, build, sup_dir):
    _basket(db, user, "NEW", "OLD")
    _history_done(db, "OLD")
    _sup_files(sup_dir, last="2026-10-07", hb=THU_2030 - _dt.timedelta(minutes=1))
    col, fetch, conn = build(Clock(THU_2030))

    out = col.run_history(["NEW"])                 # nothing pulled, not even a connection
    assert out == {"connected": False, "symbols": 1, "done": 0, "failed": 0, "deferred": 1,
                   "why": opt_collector.HISTORY_WAIT_TEXT}
    assert conn.calls == 0 and fetch.calls == []

    out = col.run_once()                           # chains yes, history no
    assert out["connected"] and out["history_deferred"] == 1 and out["history"] == 0
    assert sorted(fetch.quoted()) == ["NEW", "OLD"] and fetch.of("daily_bars") == []

    out = col.run_eod()
    assert out["connected"] and out["history_deferred"] == 2 and fetch.of("daily_bars") == []

    out = col.run_history(["NEW"], ignore_ingest=True)     # the explicit override
    assert out == {"connected": True, "symbols": 1, "done": 1, "failed": 0}
    assert [c[2]["duration"] for c in fetch.of("daily_bars", "NEW")] == ["2 Y"]


# ───────────────────────────────────────── state file + tray ─────────────────────────────────────────

def _tray_function(state_path):
    """``get_options_collector_status`` lifted out of tray_status.py with its stale
    threshold - the module itself imports pystray / PIL and cannot load here."""
    tree = ast.parse(TRAY.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "get_options_collector_status")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "OPTIONS_COLLECTOR_STALE_SEC" for t in n.targets))]
    assert len(keep) == 2
    ns = {"json": json, "datetime": _dt.datetime, "timezone": _dt.timezone, "Path": Path,
          "OPTIONS_COLLECTOR_STATE_PATH": state_path}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(TRAY), "exec"), ns)   # noqa: S102
    return ns["get_options_collector_status"]


def test_state_file_content_and_the_tray_reads_it(db, user, build, tmp_path):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(RTH)
    col, fetch, conn = build(clk)
    col.tick()
    path = tmp_path / "state" / "options_collector.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert {"state", "phase_detail", "gateway", "gateway_ok", "mdt", "cycle_n", "cycle_started",
            "cycle_finished", "symbols_total", "symbols_done", "last_eod_on", "last_error",
            "heartbeat", "pid", "version", "universe", "down_since", "next_connect",
            "wait_reason", "wait_until", "history_waiting"} <= set(doc)
    assert _dt.datetime.fromisoformat(doc["heartbeat"]) == RTH.replace(tzinfo=_dt.timezone.utc)
    assert doc["gateway"] == "127.0.0.1:4002" and doc["gateway_ok"] is True
    assert doc["mdt"] == "live" and doc["universe"] == 1 and doc["cycle_n"] == 1
    assert doc["version"] == "1.1+th2.0" and isinstance(doc["pid"], int)
    assert doc["history_waiting"] == 0 and doc["wait_reason"] is None
    assert (path.parent / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    assert not path.with_name(path.name + ".tmp").exists()

    status = _tray_function(path)
    now = RTH.replace(tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=20)
    got = status(now=now)
    assert got["color"] == "green" and got["level"] == "ok"
    assert "cycle 1" in got["line"] and "GW ok" in got["line"] and "hb 20s ago" in got["line"]
    assert got["gateway_ok"] is True and got["cycle_n"] == 1

    # heartbeat older than 5 min -> amber
    got = status(now=now + _dt.timedelta(minutes=6))
    assert got["color"] == "amber" and "NO HEARTBEAT" in got["line"] and got["tip"].startswith("Opt stale")

    # the collector stops -> amber "stopped"
    col.stop("test stop")
    got = status(now=RTH.replace(tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=30))
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "stopped"
    assert got["color"] == "amber" and got["tip"] == "Opt stopped"


def test_tray_status_function_cases(tmp_path):
    p = tmp_path / "options_collector.json"
    status = _tray_function(p)
    now = _dt.datetime(2026, 10, 8, 14, 0, tzinfo=_dt.timezone.utc)

    got = status(now=now)                                    # no file: grey
    assert (got["level"], got["color"], got["tip"]) == ("absent", "grey", "Opt -")

    doc = {"state": "cycle", "cycle_n": 12, "symbols_done": 18, "symbols_total": 30, "mdt": "live",
           "gateway_ok": True, "last_eod_on": "2026-10-07", "heartbeat": "2026-10-08T13:59:40+00:00",
           "phase_detail": "cycle 12: LRCX (18/30)", "last_error": None}
    p.write_text(json.dumps(doc), encoding="utf-8")
    got = status(now=now)
    assert got["color"] == "green" and got["tip"] == "Opt c12"
    assert "cycle 12" in got["line"] and "18/30" in got["line"] and "EOD 2026-10-07" in got["line"]

    doc.update(state="error", gateway_ok=False, phase_detail="gateway down since 09:40 ET; retry in 60 s")
    p.write_text(json.dumps(doc), encoding="utf-8")
    got = status(now=now)
    assert got["color"] == "amber" and got["tip"] == "Opt ERR"
    assert "ERROR" in got["line"] and "GW down" in got["line"] and "gateway down since" in got["line"]

    doc.update(state="idle", gateway_ok=True, heartbeat="2026-10-08T13:50:00Z")   # 10 min old
    p.write_text(json.dumps(doc), encoding="utf-8")
    got = status(now=now)
    assert got["color"] == "amber" and "10m" in got["line"]

    p.write_text("{not json", encoding="utf-8")
    got = status(now=now)
    assert got["color"] == "amber" and got["tip"] == "Opt ?"

    # waiting = the supervisor keeps the Gateway down by design: neutral grey, never amber
    base = dict(doc, state="waiting", gateway_ok=False, heartbeat="2026-10-08T13:59:50+00:00")
    for reason, until, line in (
            ("blackout", "20:10 ET", "Options collector: waiting (Gateway blackout until 20:10 ET)"),
            ("starting", "20:25 ET", "Options collector: waiting (supervisor starting the Gateway until 20:25 ET)"),
            ("closed", "00:00 ET", "Options collector: waiting (Gateway closed after the nightly top-up until 00:00 ET)")):
        p.write_text(json.dumps(dict(base, wait_reason=reason, wait_until=until)), encoding="utf-8")
        got = status(now=now)
        assert (got["line"], got["color"], got["level"], got["tip"]) == (line, "grey", "waiting", "Opt wait")
    p.write_text(json.dumps(dict(base, phase_detail="something new")), encoding="utf-8")
    assert status(now=now)["line"] == "Options collector: waiting - something new"
    p.write_text(json.dumps(dict(base, wait_reason="blackout", heartbeat="2026-10-08T13:50:00Z")),
                 encoding="utf-8")
    got = status(now=now)                                    # a dead collector still goes amber
    assert got["color"] == "amber" and "NO HEARTBEAT" in got["line"]

    # history waiting for the ingest top-up shows on a running line
    p.write_text(json.dumps(dict(doc, state="idle", heartbeat="2026-10-08T13:59:50+00:00",
                                 history_waiting=3)), encoding="utf-8")
    got = status(now=now)
    assert got["color"] == "green" and "history waits (3)" in got["line"]


def test_tray_wires_the_line_into_tooltip_and_window():
    src = TRAY.read_text(encoding="utf-8")
    compile(src, str(TRAY), "exec")
    tree = ast.parse(src)
    callers = set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "get_options_collector_status"):
                    callers.add(fn.name)
    assert {"_update_loop", "_build_progress_window"} <= callers
    # the tray reads the file the collector writes
    assert '"dashboard_tst" / "state" / "options_collector.json"' in src
    assert opt_collector.STATE_PATH.relative_to(DASH_ROOT.parent).as_posix() == \
        "dashboard_tst/state/options_collector.json"


# ───────────────────────────────────────── only opt_store writes ─────────────────────────────────────────

OPT_STORE_WRITERS = ("upsert_quotes", "set_spot", "upsert_daily", "recompute_underlying",
                     "set_earnings", "mark_history_done", "set_collector_status", "snapshot_eod",
                     "prune_v2")
ALLOWED_TABLES = {"opt_quote", "opt_underlying", "opt_underlying_daily", "opt_refresh_log",
                  "opt_collector_status", "option_chain_snapshot"}


def test_nothing_outside_opt_store_is_written(db, user, build, Session, monkeypatch):
    _basket(db, user, "NEW", "OLD")
    _history_done(db, "OLD")
    n_basket = db.query(models.OptionBasket).count()

    inside = [0]
    violations: list = []
    tables: set = set()

    def wrap(fn):
        def wrapped(*a, **k):
            inside[0] += 1
            try:
                return fn(*a, **k)
            finally:
                inside[0] -= 1
        return wrapped

    for name in OPT_STORE_WRITERS:
        monkeypatch.setattr(opt_store, name, wrap(getattr(opt_store, name)))

    def before_flush(session, ctx, instances):
        objs = list(session.new) + list(session.dirty) + list(session.deleted)
        for o in objs:
            tables.add(o.__table__.name)
        if objs and inside[0] == 0:
            violations.append(sorted({type(o).__name__ for o in objs}))

    def on_execute(state):
        if (state.is_insert or state.is_update or state.is_delete) and inside[0] == 0:
            violations.append(str(state.statement))

    event.listen(Session, "before_flush", before_flush)
    event.listen(Session, "do_orm_execute", on_execute)
    try:
        clk = Clock(RTH)
        col, fetch, conn = build(clk)
        col.tick()                                  # history NEW (+ its chain)
        clk.advance(seconds=15)
        col.tick()                                  # cycle: OLD (NEW was just read)
        clk.t = EOD
        col.tick()                                  # EOD NEW
        clk.advance(seconds=15)
        col.tick()                                  # EOD OLD + prune
        col.stop("done")
    finally:
        event.remove(Session, "before_flush", before_flush)
        event.remove(Session, "do_orm_execute", on_execute)

    assert fetch.quoted() == ["NEW", "OLD", "NEW", "OLD"]
    assert col.last_eod_on == DAY
    assert violations == []
    assert tables <= ALLOWED_TABLES
    assert {"opt_quote", "opt_underlying", "opt_underlying_daily", "opt_refresh_log",
            "opt_collector_status", "option_chain_snapshot"} <= tables
    db.expire_all()
    assert db.query(models.OptionBasket).count() == n_basket


# ───────────────────────────────────────── run modes ─────────────────────────────────────────

def test_run_forever_ticks_sleeps_and_stops(db, user, build, tmp_path):
    slept: list[float] = []
    clk = Clock(RTH)
    col, fetch, conn = build(clk, sleep=slept.append)
    col.run_forever(max_ticks=3)
    assert slept == [15.0, 15.0]
    assert col.state == "stopped" and _status(db)["state"] == "stopped"
    assert conn.ibs and not conn.ibs[0].connected
    doc = json.loads((tmp_path / "state" / "options_collector.json").read_text(encoding="utf-8"))
    assert doc["state"] == "stopped"


def test_run_once_history_and_eod_now(db, user, build):
    _basket(db, user, "NEW", "OLD")
    _history_done(db, "OLD")
    clk = Clock(_dt.datetime(2026, 10, 10, 15, 0))          # a Saturday: one-off runs ignore the clock
    col, fetch, conn = build(clk)

    out = col.run_once()
    assert out["connected"] and (out["history"], out["quoted"], out["failed"]) == (1, 1, 0)
    assert fetch.quoted() == ["NEW", "OLD"]                 # NEW is not read twice
    assert all(c[2]["mdt_pref"] == (2, 1, 4, 3) for c in fetch.of("quote"))   # out of hours
    assert col.cycle_n == 1

    out = col.run_history(["old"])
    assert out == {"connected": True, "symbols": 1, "done": 1, "failed": 0}
    assert [c[2]["duration"] for c in fetch.of("daily_bars", "OLD")] == ["2 Y"]

    out = col.run_eod()
    assert out["connected"] and out["day"] == "2026-10-09" and out["snapshot_rows"] == 2 * N_ROWS
    assert col.last_eod_on == "2026-10-09" and _status(db)["last_eod_on"] == "2026-10-09"


def test_one_off_runs_report_an_unreachable_gateway(db, user, build):
    _basket(db, user, "AAA")
    conn = FakeConnect()
    conn.down = True
    col, fetch, _ = build(Clock(RTH), connect=conn)
    for out in (col.run_once(), col.run_history(["AAA"]), col.run_eod()):
        assert out["connected"] is False and "not reachable" in out["error"]
        assert "manual-trading blackout" in out["error"]       # 10:00 ET: says why it is down
    assert fetch.calls == [] and conn.calls == 3       # one-off runs try at once, every time
    assert col.state == "waiting" and col.last_error is None


# ───────────────────────────────────────── settings + connect ─────────────────────────────────────────

def test_environment_settings(monkeypatch):
    assert opt_collector.ib_ports() == (4002, 4001, 7497, 7496)
    monkeypatch.setenv("TST_IBKR_PORT", "4001")
    assert opt_collector.ib_ports() == (4001,)
    monkeypatch.setenv("TST_IBKR_PORT", "nope")
    assert opt_collector.ib_ports() == (4002, 4001, 7497, 7496)
    assert opt_collector.env_client_id() == 89
    monkeypatch.setenv("TST_OPTIONS_COLLECTOR_CLIENT_ID", "91")
    assert opt_collector.env_client_id() == 91
    assert opt_collector.env_max_lines() == 60
    monkeypatch.setenv("TST_OPTIONS_MAX_LINES", "500")
    assert opt_collector.env_max_lines() == 100
    monkeypatch.setenv("TST_OPTIONS_MAX_LINES", "x")
    assert opt_collector.env_max_lines() == 60


def test_ib_connector_tries_ports_in_order_with_a_fake_ib_insync(monkeypatch):
    open_ports: set[int] = {7497}
    made: list = []

    class IB:
        def __init__(self):
            self.connected = False
            self.RequestTimeout = None
            made.append(self)

        async def connectAsync(self, host, port, clientId, readonly, timeout):
            self.args = (host, port, clientId, readonly)
            if port not in open_ports:
                raise ConnectionRefusedError("refused")
            self.connected = True

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    fake = types.ModuleType("ib_insync")
    fake.IB = IB
    monkeypatch.setitem(sys.modules, "ib_insync", fake)

    connect = opt_collector.ib_connector(ports=(4002, 4001, 7497, 7496), client_id=89)
    ib, label = asyncio.run(connect())
    assert label == "127.0.0.1:7497" and ib.args == ("127.0.0.1", 7497, 89, True)
    assert [m.args[1] for m in made] == [4002, 4001, 7497]
    assert ib.RequestTimeout == 60.0

    made.clear()
    asyncio.run(connect())                       # the port that worked is tried first
    assert [m.args[1] for m in made] == [7497]

    open_ports.clear()
    with pytest.raises(ConnectionError) as ei:
        asyncio.run(connect())
    msg = str(ei.value)
    assert all(str(p) in msg for p in (4002, 4001, 7497, 7496)) and "clientId 89" in msg


# ───────────────────────────────────────── CLI + task script ─────────────────────────────────────────

def _load_cli():
    spec = importlib.util.spec_from_file_location("options_collector_cli_under_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_arguments_and_exit_code_without_ib_insync(monkeypatch):
    cli = _load_cli()
    a = cli.parse_args([])
    assert not (a.once or a.history or a.eod_now) and a.port is None and a.client_id is None
    assert a.ignore_ingest is False
    assert cli.parse_args(["--history", "NVDA", "LRCX"]).history == ["NVDA", "LRCX"]
    assert cli.parse_args(["--history", "NVDA", "--ignore-ingest"]).ignore_ingest is True
    assert cli.EXIT_DEFERRED == 3
    assert cli.parse_args(["--once", "-v", "--port", "4002"]).port == 4002
    with pytest.raises(SystemExit):
        cli.parse_args(["--once", "--eod-now"])

    root = logging.getLogger()
    level = root.level
    monkeypatch.setitem(sys.modules, "ib_insync", None)      # import ib_insync -> ImportError
    try:
        assert cli.main(["--once"]) == cli.EXIT_SETUP == 1
    finally:
        root.setLevel(level)
    assert (cli.EXIT_OK, cli.EXIT_GATEWAY) == (0, 2)


def test_cli_quiets_ib_insync_connection_and_unknown_contract_noise():
    """Review (log growth): every connect attempt on a closed port made ib_insync log
    three lines (two of them ERROR), and every union strike an expiry does not list two
    more - in an unrotated file, burying the real errors."""
    cli = _load_cli()
    f = cli.IbLogNoise()

    def rec(name, msg):
        return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, None, None)

    dropped = [("ib_insync.client", "Connecting to 127.0.0.1:4002 with clientId 89..."),
               ("ib_insync.client", "API connection failed: ConnectionRefusedError(10061, 'refused')"),
               ("ib_insync.client", "Make sure API port on TWS/IBG is open"),
               ("ib_insync.client", "Disconnecting"),
               ("ib_insync.ib", "Unknown contract: Option(symbol='NVDA', strike=137.0)"),
               ("ib_insync.wrapper", "Error 200, reqId 41: No security definition has been found "
                                     "for the request, contract: Option(symbol='NVDA')")]
    kept = [("ib_insync.client", "Peer closed connection. clientId 89 already in use?"),
            ("ib_insync.wrapper", "Error 162, reqId 7: Historical Market Data Service error "
                                  "message:HMDS query returned no data"),
            ("ib_insync.wrapper", "Warning 2104, reqId -1: Market data farm connection is OK:usfarm"),
            ("options_collector", "API connection failed - our own lines are never filtered"),
            ("app.services.opt_collector", "Connecting to the gateway")]
    assert [n for n, m in dropped if f.filter(rec(n, m))] == []
    assert all(f.filter(rec(n, m)) for n, m in kept)

    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    try:
        cli._logging(logging.INFO)
        cli._logging(logging.INFO)                 # called again after init_db: one filter each
        assert root.handlers
        for h in root.handlers:
            assert sum(isinstance(x, cli.IbLogNoise) for x in h.filters) == 1
    finally:
        root.setLevel(level)
        for h in list(root.handlers):
            for x in [x for x in h.filters if isinstance(x, cli.IbLogNoise)]:
                h.removeFilter(x)
            if h not in before:
                root.removeHandler(h)


def test_cli_log_file_rotates_and_survives_the_alembic_reset(tmp_path, monkeypatch):
    """Review (log growth): the task appended stdout to logs\\options_collector.log with
    '>>', so the file grew forever. --log-file writes it through a RotatingFileHandler
    (5 MB x 5); the second _logging call (after init_db's fileConfig) puts the handler
    back once, and drops the stderr console Alembic adds."""
    cli = _load_cli()
    assert cli.parse_args([]).log_file is None
    path = tmp_path / "logs" / "options_collector.log"
    assert cli.parse_args(["--forever", "--log-file", str(path)]).log_file == str(path)
    assert (cli.LOG_MAX_BYTES, cli.LOG_BACKUPS) == (5 * 1024 * 1024, 5)

    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    alembic_console = logging.StreamHandler(sys.stderr)       # what fileConfig leaves on the root
    try:
        cli._logging(logging.INFO, str(path))
        root.addHandler(alembic_console)
        cli._logging(logging.INFO, str(path))
        files = [h for h in root.handlers if isinstance(h, cli.RotatingFileHandler)]
        assert len(files) == 1 and alembic_console not in root.handlers
        fh = files[0]
        assert (fh.maxBytes, fh.backupCount) == (cli.LOG_MAX_BYTES, cli.LOG_BACKUPS)
        assert sum(isinstance(x, cli.IbLogNoise) for x in fh.filters) == 1
        logging.getLogger("options_collector").info("collector line")
        fh.flush()
        assert "collector line" in path.read_text(encoding="utf-8")
    finally:
        logging.captureWarnings(False)
        root.setLevel(level)
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        for h in before:
            if h not in root.handlers:
                root.addHandler(h)
            for x in [x for x in h.filters if isinstance(x, cli.IbLogNoise)]:
                h.removeFilter(x)

    # Windows will not rename a log another process holds open (a Get-Content -Wait
    # tail): the rotator then copies the file and empties it
    src, dst = tmp_path / "a.log", tmp_path / "a.log.1"
    src.write_text("old lines\n", encoding="utf-8")

    def locked(a, b):
        raise PermissionError(13, "in use by another process")
    monkeypatch.setattr(cli.os, "replace", locked)
    cli._rotate(str(src), str(dst))
    assert dst.read_text(encoding="utf-8") == "old lines\n" and src.read_bytes() == b""
    cli._rotate(str(tmp_path / "missing.log"), str(tmp_path / "missing.log.1"))   # nothing to rotate
    assert not (tmp_path / "missing.log.1").exists()


def test_task_script_is_ascii_and_registers_the_always_on_task():
    raw = PS1.read_bytes()
    raw.decode("ascii")                          # PS 5.1 reads a BOM-less file as ANSI
    src = raw.decode("ascii")
    for needle in ('"TST-Options-Collector"', "options_collector.py", "--forever",
                   "options_collector.log", "-AtStartup", "-Daily -At $At", '"07:00"',
                   "-RestartCount 3", "-RestartInterval (New-TimeSpan -Minutes 5)",
                   "-MultipleInstances IgnoreNew", "-ExecutionTimeLimit ([TimeSpan]::Zero)",
                   "[switch] $StartNow", "Start-ScheduledTask", ".venv\\Scripts\\python.exe",
                   "--log-file"):
        assert needle in src, needle
    assert "&&" not in src
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert ">>" not in code and "cmd.exe" not in code     # the collector rotates its own log
