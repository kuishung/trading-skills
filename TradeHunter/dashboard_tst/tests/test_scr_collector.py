"""The Hermes Options Screener collector (OPTIONS_SCREENER_DESIGN.md §4, v4.136):
``app/services/scr_collector.py``, ``deploy/screener_collector.py``,
``deploy/setup_screener_task.ps1`` and the "Options screener" line in
``dashboard_intraday/tray_status.py``.

The collector runs the REAL ``scr_store`` / ``opt_massive`` against a FAKE Massive client
(every endpoint it uses, priced by Black-Scholes, recording each call), a fake clock, a fake
Nasdaq earnings calendar and - unless a test says otherwise - an inline executor (every job
runs at once in the tick, so ticks are deterministic), on a fresh screener DB migrated to
its Alembic head. Nothing touches the network.
"""
from __future__ import annotations

import ast
import datetime as _dt
import importlib.util
import json
import logging
import math
import re
import sys
from pathlib import Path

import pytest

from app import screener_db
from app.screener_models import ScrContract, ScrPass, ScrUnderlyingDaily
from app.services import clock, massive, scr_collector, scr_store
from app.services.black_scholes import black_scholes
from app.services.massive import MassiveError
from app.services.opt_constants import RISK_FREE

from .conftest import DASH_ROOT
from .fixtures.options import bs_greeks

# 2026-10-08 is a Thursday (a trading day); New York is UTC-4 (EDT) - naive UTC below.
DAY = "2026-10-08"
RTH = _dt.datetime(2026, 10, 8, 14, 0)          # 10:00 ET
EOD = _dt.datetime(2026, 10, 8, 20, 25)         # 16:25 ET
NEXT_PRE = _dt.datetime(2026, 10, 9, 12, 0)     # Friday 08:00 ET
SAT = _dt.datetime(2026, 10, 10, 16, 0)         # Saturday 12:00 ET
EXPIRIES = ("2026-10-16", "2026-10-23", "2026-11-20")   # monthly, weekly, monthly
MULTS = (0.9, 0.95, 1.0, 1.05, 1.1)
SPOTS = {"AAA": 100.0, "BBB": 50.0, "CCC": 200.0, "ZZZ": 10.0}
UNIVERSE = {"AAA": 3000, "BBB": 1200, "CCC": 500}
KEPT = len(EXPIRIES) * len(MULTS) * 2 + 1       # the standard grid + the stale-bar put
KEY = "unit-test-key-0123456789abcdef"          # not a real key

TRAY = DASH_ROOT.parent / "dashboard_intraday" / "tray_status.py"
CLI = DASH_ROOT / "deploy" / "screener_collector.py"
PS1 = DASH_ROOT / "deploy" / "setup_screener_task.ps1"
MODULE = Path(scr_collector.__file__)


# ───────────────────────────────────────── fakes ─────────────────────────────────────────

class Clock:
    def __init__(self, t: _dt.datetime):
        self.t = t

    def __call__(self) -> _dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += _dt.timedelta(**kw)


def _close(sym: str, d: _dt.date) -> float:
    """The fake stock's close on ``d`` - deterministic, around its spot."""
    return round(SPOTS.get(sym, 20.0) * (1.0 + 0.04 * math.sin(d.toordinal() / 9.0)), 2)


def _weekdays(start, end):
    d = _dt.date.fromisoformat(str(start)[:10])
    e = _dt.date.fromisoformat(str(end)[:10])
    while d <= e:
        if d.weekday() < 5:
            yield d
        d += _dt.timedelta(days=1)


_OPT = re.compile(r"^O:([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


class FakeMassive:
    """A ``massive.Client`` stand-in with every endpoint the screener collector uses. The
    chain: 3 expiries x 5 strikes x C/P at 40 % IV (no bid/ask, no underlying price - Options
    Starter + Stocks Basic), plus a 10-share mini (dropped as not standard), an untraded
    strike with no open interest (not kept) and a put whose day bar is two sessions old
    (kept, no volume today). ``fail[(name, symbol)]`` - or ``(name, None)`` - raises."""

    base_url = "https://api.massive.test"

    def __init__(self, clk: Clock, *, key: bool = True):
        self.clk = clk
        self.has_key = key
        self.calls: list[tuple] = []
        self.fail: dict = {}
        self.universe = dict(UNIVERSE)
        self.bars_end = "2026-10-07"
        self.no_options: set[str] = set()
        self.split_on: str | None = None             # AAA splits 2:1 on this day

    def _rec(self, name, key, **kw):
        self.calls.append((name, key, kw))
        exc = self.fail.get((name, key)) or self.fail.get((name, None))
        if exc is not None:
            raise exc

    def _row(self, sym, e, k, right, now, stamp, **kw):
        S = SPOTS[sym]
        T = max((_dt.date.fromisoformat(e) - clock.et_date(now)).days, 1) / 365.0
        g = bs_greeks(S, k, T, 0.40, right)
        px = round(max(0.05, g["price"]), 2)
        r = {"expiry": e, "right": right, "strike": k, "iv": 0.40, "delta": round(g["delta"], 4),
             "gamma": round(g["gamma"], 5), "theta": round(g["theta"], 4), "vega": round(g["vega"], 4),
             "oi": 900, "volume": 40, "day_close": px, "day_vwap": px, "prev_close": px,
             "day_change_pct": 1.5, "bid": None, "ask": None, "bid_size": None, "ask_size": None,
             "last_updated": stamp, "ticker": massive.option_ticker(sym, e, right, k), "multiplier": 100,
             "und_price": None, "und_as_of": None}
        r.update(kw)
        return r

    def chain_snapshot(self, symbol, *, exp_gte=None, exp_lte=None, strike_gte=None, strike_lte=None):
        self._rec("chain_snapshot", symbol, exp_gte=str(exp_gte), exp_lte=str(exp_lte),
                  strike_gte=strike_gte, strike_lte=strike_lte)
        now = self.clk()
        stamp = now - _dt.timedelta(minutes=15)
        S = SPOTS[symbol]
        rows = [self._row(symbol, e, round(S * m, 2), r, now, stamp)
                for e in EXPIRIES for m in MULTS for r in ("C", "P")]
        rows.append(dict(rows[0], multiplier=10, ticker="O:%s1%s" % (symbol, rows[0]["ticker"][2 + len(symbol):])))
        rows.append(self._row(symbol, EXPIRIES[-1], round(S * 1.5, 2), "C", now, stamp, oi=0, volume=0))
        rows.append(self._row(symbol, EXPIRIES[-1], round(S * 0.8, 2), "P", now,
                              stamp - _dt.timedelta(days=2), oi=300, volume=12))
        return {"symbol": symbol, "rows": rows, "underlying_price": None, "underlying_as_of": None,
                "pages": 2, "as_of": stamp}

    def option_underlyings(self, *, exp_lte=None):
        self._rec("option_underlyings", None, exp_lte=str(exp_lte))
        return dict(self.universe)

    def _bar_close(self, sym, d: _dt.date, adjusted: bool) -> float:
        c = _close(sym, d)
        if sym == "AAA" and self.split_on and (adjusted or d.isoformat() >= self.split_on):
            c = round(c / 2.0, 2)
        return c

    def grouped_daily(self, day, *, adjusted=True):
        self._rec("grouped_daily", str(day)[:10], adjusted=adjusted)
        d = _dt.date.fromisoformat(str(day)[:10])
        if str(day)[:10] > self.bars_end or d.weekday() >= 5:
            return []
        out = []
        for sym in ("AAA", "BBB", "CCC", "ZZZ"):
            c = self._bar_close(sym, d, adjusted)
            out.append({"symbol": sym, "on": d.isoformat(), "open": c - 0.3, "high": c + 1.0,
                        "low": c - 1.0, "close": c, "volume": 2_000_000.0})
        return out

    def stock_daily(self, symbol, start, end, *, adjusted=True):
        self._rec("stock_daily", symbol, start=str(start), end=str(end), adjusted=adjusted)
        out = []
        for d in _weekdays(start, min(str(end), self.bars_end)):
            c = self._bar_close(symbol, d, adjusted)
            out.append({"on": d.isoformat(), "open": c, "high": c + 1.0, "low": c - 1.0, "close": c,
                        "volume": 1_000_000.0})
        return out

    def option_daily(self, option_ticker, start, end):
        m = _OPT.match(option_ticker)
        self._rec("option_daily", m.group(1) if m else option_ticker, ticker=option_ticker)
        if m is None or m.group(1) in self.no_options:
            return []
        exp = _dt.date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
        k = int(m.group(6)) / 1000.0
        kind = "call" if m.group(5) == "C" else "put"
        out = []
        for d in _weekdays(start, min(str(end), self.bars_end)):
            dte = (exp - d).days
            if dte <= 0:
                continue
            p = round(black_scholes(_close(m.group(1), d), k, dte / 365.0, RISK_FREE, 0.30, kind).price, 2)
            if p >= 0.01:
                out.append({"on": d.isoformat(), "open": p, "high": p, "low": p, "close": p, "volume": 10.0})
        return out

    def reference_tickers(self, market="stocks"):
        self._rec("reference_tickers", market)
        if market == "indices":
            return [{"symbol": "I:SPX", "name": "S&P 500 Index", "type": None, "primary_exchange": None}]
        return [{"symbol": "AAA", "name": "Aaa Industries", "type": "CS", "primary_exchange": "XNYS"},
                {"symbol": "BBB", "name": "Bbb Sector ETF", "type": "ETF", "primary_exchange": "ARCX"},
                {"symbol": "CCC", "name": "Ccc Systems", "type": "CS", "primary_exchange": "XNAS"},
                {"symbol": "ZZZ", "name": "Not optionable", "type": "CS", "primary_exchange": "XNAS"}]

    # what the assertions read
    def of(self, name, key=None) -> list[tuple]:
        return [c for c in self.calls if c[0] == name and (key is None or c[1] == key)]

    def read(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "chain_snapshot"]


EARNINGS = {"2026-10-20": [{"symbol": "AAA"}, {"symbol": "ZZZ"}],
            "2026-10-29": [{"symbol": "BBB"}], "2026-11-24": [{"symbol": "AAA"}]}


def fake_earnings(day):
    return [dict(r) for r in EARNINGS.get(day.isoformat(), [])]


# ───────────────────────────────────────── fixtures ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("TST_MASSIVE_API_KEY", "TST_MASSIVE_BASE_URL", "TST_SCREENER_CYCLE_MIN",
                "TST_SCREENER_WORKERS", "TST_SCREENER_MAX_RPS", "TST_SCREENER_MAX_DTE"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def Session(tmp_path):
    """The screener DB on a fresh file at the head of its own migrations."""
    screener_db.configure("sqlite:///" + (tmp_path / "screener.db").as_posix())
    screener_db.init_screener_db()
    yield screener_db.SessionLocal
    screener_db.configure(None)


@pytest.fixture
def db(Session):
    s = Session()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def build(Session, tmp_path):
    """``build(clock, **kw) -> (collector, client)``; inline executors unless
    ``executor_factory`` is given; every collector is stopped at teardown."""
    made = []

    def _build(clk, *, client=None, **kw):
        if "client_factory" not in kw:
            client = client if client is not None else FakeMassive(clk)
            kw["client"] = client
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("state_path", tmp_path / "state" / "screener_collector.json")
        kw.setdefault("log", logging.getLogger("test_scr_collector"))
        kw.setdefault("executor_factory", lambda n: scr_collector.InlineExecutor())
        kw.setdefault("earnings_fetch", fake_earnings)
        kw.setdefault("workers", 2)
        kw.setdefault("stock_years", 0.05)
        col = scr_collector.Collector(Session, clock=clk, **kw)
        made.append(col)
        return col, client

    yield _build
    for col in made:
        try:
            col.stop("test teardown")
        except Exception:  # noqa: BLE001
            pass


def _doc(tmp_path) -> dict:
    return json.loads((tmp_path / "state" / "screener_collector.json").read_text(encoding="utf-8"))


def _status(db) -> dict:
    db.expire_all()
    return scr_store.status(db) or {}


def _und(db, sym) -> dict:
    db.expire_all()
    return scr_store.underlying(db, sym) or {}


def _contracts(db, sym) -> list:
    db.expire_all()
    return db.query(ScrContract).filter(ScrContract.symbol == sym).all()


def _passes(db) -> list:
    db.expire_all()
    return db.query(ScrPass).order_by(ScrPass.id).all()


def _utc(t: _dt.datetime) -> _dt.datetime:
    return t.replace(tzinfo=_dt.timezone.utc)


def _settle(col, clk, *, n=200, step_s=0.0, until=None):
    """Tick until ``until(col)`` (default: the stocks lane and the history have nothing
    left) or ``n`` ticks."""
    for _ in range(n):
        col.tick()
        if step_s:
            clk.advance(seconds=step_s)
        if until is not None:
            if until(col):
                return
        elif col._days and col._pending_days() == 0 and not col._tech_dirty and col._bg is None \
                and not col._hist and col._earnings_on:
            return
    raise AssertionError("did not settle in %d ticks" % n)


# ───────────────────────────────────────── pure helpers ─────────────────────────────────────────

def test_contract_rows_kept_rule_stale_bars_weekly_and_model_price():
    clk = Clock(RTH)
    snap = FakeMassive(clk).chain_snapshot("AAA")
    from app.services import opt_massive  # noqa: PLC0415

    std = opt_massive.standard_rows("AAA", snap["rows"])
    assert len(std) == len(snap["rows"]) - 1                        # the mini is not standard
    day = clock.et_date(RTH)
    rows, fig = scr_collector.contract_rows(std, 100.0, day=day, session=DAY)
    assert len(rows) == KEPT                                         # the untraded strike is not kept
    stale = [r for r in rows if r["strike"] == 80.0]
    assert len(stale) == 1 and stale[0]["volume"] == 0 and stale[0]["chg_pct"] is None
    assert stale[0]["oi"] == 300 and stale[0]["last"] is not None
    fresh = [r for r in rows if r["strike"] == 100.0 and r["expiry"] == "2026-11-20" and r["right"] == "C"][0]
    assert fresh["volume"] == 40 and fresh["chg_pct"] == 1.5
    assert fresh["price"] == opt_massive.model_price(100.0, 100.0, (_dt.date(2026, 11, 20) - day).days, 0.40, "C")
    weekly = {r["expiry"]: r["weekly"] for r in rows}
    assert weekly == {"2026-10-16": False, "2026-10-23": True, "2026-11-20": False}
    assert fig["iv30"] == pytest.approx(40.0, abs=0.01)
    assert fig["exp_move30"] == pytest.approx(40.0 * math.sqrt(30 / 365), abs=0.01)
    assert (fig["call_vol"], fig["put_vol"]) == (15 * 40, 15 * 40)
    assert (fig["call_oi"], fig["put_oi"]) == (15 * 900, 15 * 900 + 300)
    assert fig["n_contracts"] == KEPT
    # no spot: the last trade is the price, no IV30
    rows, fig = scr_collector.contract_rows(std, None, day=day, session=DAY)
    assert all(r["price"] == r["last"] for r in rows) and fig["iv30"] is None
    # an expiry before the read day is dropped
    assert scr_collector.contract_rows(std, 100.0, day=_dt.date(2026, 10, 20), session=DAY)[1]["n_contracts"] \
        == len(MULTS) * 2 * 2 + 1


def test_sessions_types_exchanges_and_settings(monkeypatch):
    assert scr_collector.snapshot_session(RTH) == DAY
    assert scr_collector.snapshot_session(NEXT_PRE) == DAY                    # before the open
    assert scr_collector.snapshot_session(SAT) == "2026-10-09"
    assert scr_collector.eod_day_for(RTH) is None
    assert scr_collector.eod_day_for(EOD) == DAY
    assert scr_collector.eod_day_for(NEXT_PRE) == DAY
    assert scr_collector.eod_day_for(SAT) == "2026-10-09"
    assert [scr_collector.sec_type_of(c) for c in ("CS", "ETF", "ETN", "ETV", "ADRC", "PFD", None)] == \
        ["stock", "etf", "etf", "etf", "other", "other", "other"]
    assert scr_collector.sec_type_of("CS", "indices") == "index"
    assert [scr_collector.exchange_of(m) for m in ("XNYS", "XNAS", "XASE", "ARCX", "BATS", "OTCM", None)] == \
        ["NYSE", "NASDAQ", "AMEX", "AMEX", "AMEX", "OTHER", "OTHER"]
    assert scr_collector.exchange_of(None, "indices") == "INDEX"
    days = scr_collector.stock_days(RTH, 0.05)
    assert days[-1] == "2026-10-07" and all(clock.is_trading_day(d) for d in days)
    assert scr_collector.stock_days(_dt.datetime(2026, 10, 9, 0, 30), 0.05)[-1] == DAY   # 20:30 ET: published
    assert len(scr_collector.stock_days(RTH)) > 490                                   # 2 years
    assert (scr_collector.env_cycle_min(), scr_collector.env_workers(), scr_collector.env_max_rps(),
            scr_collector.env_max_dte()) == (30, 8, 40.0, 1100)
    monkeypatch.setenv("TST_SCREENER_CYCLE_MIN", "15")
    monkeypatch.setenv("TST_SCREENER_WORKERS", "4")
    monkeypatch.setenv("TST_SCREENER_MAX_RPS", "25.5")
    monkeypatch.setenv("TST_SCREENER_MAX_DTE", "nonsense")
    assert (scr_collector.env_cycle_min(), scr_collector.env_workers(), scr_collector.env_max_rps(),
            scr_collector.env_max_dte()) == (15, 4, 25.5, 1100)
    monkeypatch.setenv("TST_SCREENER_WORKERS", "500")
    assert scr_collector.env_workers() == 8
    assert scr_collector.classify_trend is scr_store.classify_trend


def test_default_client_shares_one_client_sized_to_the_workers(monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    monkeypatch.setattr(scr_collector, "APP_ENV_PATH", Path("does-not-exist.env"))
    c = scr_collector.default_client(6, 30.0)
    try:
        assert c.has_key and c._sem._initial_value == 6
        assert c._bucket.interval == pytest.approx(1 / 30.0)
        assert KEY not in repr(c)
    finally:
        c.close()


# ───────────────────────────────────────── a market pass, end to end ─────────────────────────────────────────

def test_universe_then_a_pass_over_three_underlyings(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    assert col.tick() == "idle"                              # tick 1: the universe (no pass without it)
    assert cl.of("option_underlyings")[0][2]["exp_lte"] == "2026-12-07"   # today + 60 days
    assert cl.read() == []
    assert scr_store.pass_symbols(db, now=RTH) == ["AAA", "BBB", "CCC"]
    assert _doc(tmp_path)["universe_on"] == DAY

    col.tick()                                               # tick 2: the cycle pass (+ identities)
    assert cl.read() == ["AAA", "BBB", "CCC"]                 # most contracts first
    snap = cl.of("chain_snapshot", "AAA")[0][2]
    assert snap["exp_gte"] == DAY and snap["exp_lte"] == "2029-10-12"     # today + 1100 days
    assert snap["strike_gte"] is None and snap["strike_lte"] is None     # the whole chain

    for sym in ("AAA", "BBB", "CCC"):
        rows = _contracts(db, sym)
        assert len(rows) == KEPT, sym
        assert {r.session for r in rows} == {DAY}
        assert {r.as_of for r in rows} == {RTH - _dt.timedelta(minutes=15)}
        u = _und(db, sym)
        assert u["spot"] == pytest.approx(SPOTS[sym], abs=0.05) and u["spot_src"] == "parity"
        assert u["iv30"] == pytest.approx(40.0, abs=0.05)
        assert (u["call_vol"], u["put_vol"], u["n_contracts"]) == (600, 600, KEPT)
        assert u["put_oi"] == 15 * 900 + 300
        assert u["exp_move30"] == pytest.approx(40.0 * math.sqrt(30 / 365), abs=0.02)
    a = _und(db, "AAA")
    assert (a["name"], a["sec_type"], a["exchange"]) == ("Aaa Industries", "stock", "NYSE")
    assert (_und(db, "BBB")["sec_type"], _und(db, "BBB")["exchange"]) == ("etf", "AMEX")
    assert _und(db, "ZZZ") == {}                                  # not optionable: not named

    p = _passes(db)
    assert len(p) == 1
    p = p[0]
    assert (p.kind, p.session, p.n_symbols, p.n_ok, p.n_failed) == ("cycle", DAY, 3, 3, 0)
    assert (p.n_contracts, p.requests) == (3 * KEPT, 6) and p.finished is not None
    assert a["pass_id"] == p.id

    st = _status(db)
    assert st["heartbeat"] == RTH and st["pid"] == col.pid and st["version"] == scr_collector.COLLECTOR_VERSION
    assert (st["state"], st["pass_id"], st["symbols_total"], st["symbols_done"]) == ("idle", p.id, 3, 3)
    assert (st["universe_n"], st["universe_on"], st["history_total"], st["history_done_n"]) == (3, DAY, 3, 0)
    assert st["api_ok"] is True
    doc = _doc(tmp_path)
    assert doc["state"] == "idle" and "next pass 10:30 ET" in doc["detail"]
    assert (doc["last_pass_id"], doc["last_pass_kind"], doc["last_pass_et"]) == (p.id, "cycle", "10:00 ET")
    assert doc["last_pass_contracts"] == 3 * KEPT and doc["universe_n"] == 3
    assert doc["written_by"] == "dashboard_tst/app/services/scr_collector.py"
    assert (tmp_path / "state" / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")


def test_cycle_cadence_and_the_next_session_carries_prev_volumes(db, build):
    clk = Clock(RTH)
    col, cl = build(clk, cycle_min=30)
    col.tick()
    col.tick()
    assert len(cl.read()) == 3
    clk.advance(minutes=10)
    col.tick()
    assert len(cl.read()) == 3                                # not due before 30 min
    clk.advance(minutes=20)
    col.tick()
    assert len(cl.read()) == 6 and len(_passes(db)) == 2
    r = [x for x in _contracts(db, "AAA") if x.strike == 100.0 and x.right == "C" and x.expiry == "2026-11-20"][0]
    assert (r.volume, r.vol_prev, r.oi_prev) == (40, None, None)   # the same session: nothing carried
    # the next session: nothing before 09:45 ET ...
    col._last_eod = DAY                                       # (the EOD pass is another test)
    clk.t = _dt.datetime(2026, 10, 9, 13, 40)                 # Friday 09:40 ET
    col.tick()
    assert len(cl.read()) == 6
    # ... then its first pass carries yesterday's final volume / OI
    clk.t = _dt.datetime(2026, 10, 9, 13, 50)                 # Friday 09:50 ET
    col.tick()
    assert len(cl.read()) == 9
    r = [x for x in _contracts(db, "AAA") if x.strike == 100.0 and x.right == "C" and x.expiry == "2026-11-20"][0]
    assert (r.session, r.vol_prev, r.oi_prev) == ("2026-10-09", 40, 900)
    assert _passes(db)[-1].session == "2026-10-09"
    # no cycle pass starts at or after 16:00 ET
    clk.t = _dt.datetime(2026, 10, 9, 20, 5)                  # 16:05 ET
    col.tick()
    assert len(cl.read()) == 9


def test_eod_pass_files_iv30_once_and_a_missed_evening_is_caught_up(db, build, tmp_path):
    clk = Clock(EOD)
    col, cl = build(clk)
    col.tick()
    col.tick()
    p = _passes(db)[-1]
    assert (p.kind, p.session, p.n_ok) == ("eod", DAY, 3) and p.finished is not None
    db.expire_all()
    rows = {r.symbol: r.iv30 for r in db.query(ScrUnderlyingDaily).filter(ScrUnderlyingDaily.on == DAY)}
    assert set(rows) == {"AAA", "BBB", "CCC"} and rows["AAA"] == pytest.approx(40.0, abs=0.05)
    assert _und(db, "AAA")["iv_n"] == 1
    n = len(cl.read())
    clk.advance(minutes=30)
    col.tick()
    assert len(cl.read()) == n                                # once per session
    assert _doc(tmp_path)["last_eod_session"] == DAY

    # a restart remembers it (the pass table)
    col2, cl2 = build(clk, client=cl)
    col2.tick()
    assert len(cl.read()) == n

    # missed evening: the next morning before the open catches it up
    clk2 = Clock(NEXT_PRE)
    col3, cl3 = build(clk2)
    db.query(ScrPass).delete()
    db.commit()
    col3.tick()
    p = _passes(db)[-1]
    assert (p.kind, p.session) == ("eod", DAY)


def test_grouped_daily_filing_technicals_earnings_and_a_split(db, build):
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.12)
    _settle(col, clk)
    days = scr_collector.stock_days(RTH, 0.12)
    assert len(days) >= 28
    adj = [c for c in cl.of("grouped_daily") if c[2]["adjusted"]]
    raw = [c for c in cl.of("grouped_daily") if not c[2]["adjusted"]]
    assert len(adj) == len(raw) == len(days)
    assert adj[0][1] == days[-1]                              # newest session first
    db.expire_all()
    assert db.query(ScrUnderlyingDaily).filter_by(symbol="ZZZ").count() == 0     # universe only
    rows = db.query(ScrUnderlyingDaily).filter_by(symbol="AAA").order_by(ScrUnderlyingDaily.on).all()
    assert [r.on for r in rows] == days
    assert all(r.close == r.close_raw == _close("AAA", _dt.date.fromisoformat(r.on)) for r in rows)
    u = _und(db, "AAA")
    closes = [r.close for r in rows]
    assert u["sma20"] == pytest.approx(sum(closes[-20:]) / 20)
    assert u["hv20"] is not None and u["atr14"] is not None and u["rsi14"] is not None
    assert u["trend"] is None                                  # under 210 sessions
    assert u["stock_volume"] == 2_000_000.0 and u["bars_as_of"] == _dt.datetime(2026, 10, 7, 20, 0)
    assert u["prev_close"] == pytest.approx(closes[-1])        # the pass of 2026-10-08: the 10-07 close
    assert u["earnings_date"] == "2026-10-20" and u["earnings_src"] == "nasdaq"
    assert _und(db, "BBB")["earnings_date"] == "2026-10-29" and _und(db, "CCC")["earnings_date"] is None
    assert col._pending_days() == 0 and cl.of("stock_daily") == []       # no symbol needed its own bars

    # 20:30 ET: today's bars are published - AAA split 2:1 today
    cl.bars_end, cl.split_on = DAY, DAY
    clk.t = _dt.datetime(2026, 10, 9, 0, 30)
    col._last_eod = DAY
    _settle(col, clk)
    assert [c[1] for c in cl.of("grouped_daily")][-2:] == [DAY, DAY]
    fills = cl.of("stock_daily", "AAA")
    assert len(fills) == 1 and fills[0][2]["adjusted"] is True          # adjusted bars only
    db.expire_all()
    d7 = db.query(ScrUnderlyingDaily).filter_by(symbol="AAA", on="2026-10-07").one()
    assert d7.close == pytest.approx(_close("AAA", _dt.date(2026, 10, 7)) / 2, abs=0.01)   # re-adjusted
    assert d7.close_raw == _close("AAA", _dt.date(2026, 10, 7))                            # as traded
    assert cl.of("stock_daily", "BBB") == []


def test_a_new_universe_member_gets_its_own_bars(db, build):
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.05)
    _settle(col, clk)
    cl.universe["DDD"] = 50
    SPOTS["DDD"] = 30.0
    try:
        clk.t = _dt.datetime(2026, 10, 9, 11, 35)            # Friday 07:35 ET: the daily refresh
        col._last_eod = DAY
        _settle(col, clk, until=lambda c: c._bg is None and not c._fill and len(cl.of("stock_daily", "DDD")) == 2)
        reads = cl.of("stock_daily", "DDD")
        assert [c[2]["adjusted"] for c in reads] == [True, False]
        db.expire_all()
        assert db.query(ScrUnderlyingDaily).filter_by(symbol="DDD").count() >= 10
        assert _und(db, "DDD")["bars_as_of"] is not None      # its technicals ran
        assert scr_store.underlying(db, "DDD")["perf5"] is not None
    finally:
        SPOTS.pop("DDD", None)


# ───────────────────────────────────────── IV history ─────────────────────────────────────────

def test_iv_history_most_volume_first_done_and_retry_backoff(db, build):
    clk = Clock(SAT)
    col, cl = build(clk, stock_years=0.3)
    cl.bars_end = "2026-10-09"
    cl.no_options.add("CCC")
    _settle(col, clk, until=lambda c: c._pending_days() == 0 and bool(c._days) and not c._hist
            and c._bg is None and scr_store.history_counts(db)[0] >= 2
            and (_und(db, "CCC").get("history_tries") or 0) >= 1)
    db.expire_all()
    a = _und(db, "AAA")
    assert a["history_done"] is True and a["history_tries"] == 0
    ivs = [r.iv30 for r in db.query(ScrUnderlyingDaily).filter_by(symbol="AAA")
           if r.iv30 is not None and r.on < "2026-10-09"]
    assert len(ivs) >= 20 and all(abs(v - 30.0) < 1.0 for v in ivs)    # the fake prices at 30 %
    assert a["iv_n"] >= 20 and a["iv30_prev"] == pytest.approx(30.0, abs=1.0)
    assert a["iv_pct"] is not None
    order = []
    for c in cl.of("option_daily"):
        if c[1] not in order:
            order.append(c[1])
    assert order[0] == "AAA" or set(order[:2]) == {"AAA", "BBB"}       # most option volume first
    c = _und(db, "CCC")
    assert c["history_done"] is False and c["history_tries"] == 1
    assert abs((c["history_next"] - clk.t).total_seconds() - 1800) < 1      # 30 min
    n = len(cl.of("option_daily", "CCC"))
    col.tick()
    assert len(cl.of("option_daily", "CCC")) == n            # backing off
    clk.advance(minutes=31)
    col.tick()
    assert len(cl.of("option_daily", "CCC")) > n
    assert _und(db, "CCC")["history_tries"] == 2
    assert abs((_und(db, "CCC")["history_next"] - clk.t).total_seconds() - 3600) < 1    # doubling


def test_history_waits_for_the_recent_stock_days(db, build):
    clk = Clock(SAT)
    col, cl = build(clk, stock_years=0.3)
    cl.bars_end = "2026-10-09"
    for _ in range(6):                                        # universe, EOD, earnings, a few days
        col.tick()
    assert col._pending_days() > 0 and cl.of("option_daily") == []


# ───────────────────────────────────────── errors ─────────────────────────────────────────

def test_a_rejected_key_pauses_the_pass_then_it_resumes(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    col.tick()
    n = len(cl.read())
    cl.fail[("chain_snapshot", None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)
    clk.advance(minutes=30)
    assert col.tick() == "error"                              # never raises
    assert len(cl.read()) == n + 1                            # the first failure halts the rest
    p = _passes(db)[-1]
    assert p.finished is None and p.kind == "cycle"           # a paused pass is not finished
    doc = _doc(tmp_path)
    assert doc["state"] == "error" and "Massive rejected the API key" in doc["detail"]
    assert doc["api_ok"] is False and doc["error_kind"] == "auth" and doc["next_try"]
    assert _status(db)["state"] == "error"
    clk.advance(seconds=15)
    col.tick()
    assert len(cl.read()) == n + 1                            # paused: nothing read
    cl.fail.clear()
    clk.advance(minutes=5)
    assert col.tick() != "error"
    assert len(cl.read()) == n + 4                            # the rest of the same pass
    p = _passes(db)[-1]
    assert p.finished is not None and p.n_ok == 3 and len(_passes(db)) == 2
    assert _doc(tmp_path)["api_ok"] is True


def test_a_plan_error_pauses_only_the_passes(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    cl.fail[("chain_snapshot", None)] = MassiveError("plan", "your Massive plan does not include the options "
                                                     "chain snapshot (HTTP 403)", 403)
    col.tick()
    doc = _doc(tmp_path)
    assert doc["state"] == "error" and "market passes paused, the rest carries on" in doc["detail"]
    assert doc["error_kind"] == "plan"
    col.tick()
    assert cl.of("reference_tickers")                         # the stocks lane carries on
    clk.advance(seconds=30)
    col.tick()
    assert cl.of("grouped_daily")


def test_one_symbol_failing_never_stops_the_pass(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    cl.fail[("chain_snapshot", "BBB")] = MassiveError("http", "Massive answered HTTP 500 for the options chain", 500)
    col.tick()
    p = _passes(db)[-1]
    assert (p.n_ok, p.n_failed, p.finished is not None) == (2, 1, True)
    assert _contracts(db, "BBB") == [] and len(_contracts(db, "CCC")) == KEPT
    assert "BBB" in (_doc(tmp_path)["last_error"] or "")
    assert col.shown_state != "error"


def test_massive_unreachable_backs_off_60_s_doubling(db, build):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    for name in ("chain_snapshot", "reference_tickers", "grouped_daily", "option_underlyings",
                 "option_daily", "stock_daily"):
        cl.fail[(name, None)] = MassiveError("network", "could not reach Massive", None)
    col.tick()
    assert col._retry_at["all"] - clk.t == _dt.timedelta(seconds=60)
    assert col.shown_state == "error"
    clk.advance(seconds=61)
    col.tick()
    assert col._retry_at["all"] - clk.t == _dt.timedelta(seconds=120)


def test_a_missing_key_is_an_error_looked_at_again_every_5_min(db, build, tmp_path):
    clk = Clock(RTH)
    keyed = {"on": False}
    clients = []

    def factory():
        c = FakeMassive(clk, key=keyed["on"])
        clients.append(c)
        return c

    col, _ = build(clk, client_factory=factory)
    assert col.tick() == "error"
    doc = _doc(tmp_path)
    assert doc["detail"].startswith(scr_collector.NO_KEY_TEXT) and doc["error_kind"] == "config"
    assert _status(db)["state"] == "error"
    clk.advance(minutes=1)
    col.tick()
    assert len(clients) == 1                                  # not looked at again before 5 min
    keyed["on"] = True
    clk.advance(minutes=5)
    assert col.tick() != "error"
    assert clients[-1].of("option_underlyings")


def test_the_key_never_reaches_the_db_the_state_file_or_the_log(db, build, tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    clk = Clock(RTH)
    col, cl = build(clk)
    cl._key = KEY
    caplog.set_level(logging.DEBUG)
    col.tick()
    cl.fail[("chain_snapshot", "AAA")] = MassiveError("http", "bad request apiKey=%s for %s" % (KEY, KEY), 400)
    col.tick()
    col.stop("done")
    assert KEY not in (tmp_path / "state" / "screener_collector.json").read_text(encoding="utf-8")
    st = _status(db)
    assert KEY not in json.dumps(st, default=str)
    assert KEY not in caplog.text


# ───────────────────────────────────────── threads, one-offs ─────────────────────────────────────────

def test_a_pass_on_real_threads(db, build):
    clk = Clock(RTH)
    col, cl = build(clk, executor_factory=None, workers=4)
    assert col._executor_factory is scr_collector._thread_pool
    col.tick()
    col.wait_idle(30)
    col.tick()                                                 # the universe filed; the pass starts
    for _ in range(20):
        col.wait_idle(30)
        col.tick()
        if col._pass is None and _passes(db) and _passes(db)[-1].finished is not None:
            break
    p = _passes(db)[-1]
    assert p.finished is not None and p.n_ok == 3 and p.n_contracts == 3 * KEPT
    assert sorted(cl.read()[:3]) == ["AAA", "BBB", "CCC"]


def test_one_off_runs(db, build):
    clk = Clock(SAT)
    col, cl = build(clk)
    res = col.run_universe()
    assert res["ok"] and res["n"] == 3
    res = col.run_once()
    assert res["ok"] and (res["read"], res["failed"], res["session"]) == (3, 0, "2026-10-09")
    assert _passes(db)[-1].kind == "manual" and _passes(db)[-1].finished is not None
    res = col.run_eod()
    assert res["ok"] and _passes(db)[-1].kind == "eod" and res["session"] == "2026-10-09"
    db.expire_all()
    assert db.query(ScrUnderlyingDaily).filter_by(on="2026-10-09").count() == 3   # the day's IV30
    # history by hand (bars first)
    cl.bars_end = "2026-10-09"
    for d in scr_collector.stock_days(SAT, 0.3):
        col.job_stock_day(d)
    res = col.run_history(["aaa", "AAA", "CCC"])
    assert res["ok"] and res["symbols"] == 2 and res["done"] >= 1
    assert _und(db, "AAA")["history_done"] is True
    # a paused Massive: ok False, the pass not finished
    cl.fail[("chain_snapshot", None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)
    res = col.run_once()
    assert res["ok"] is False and res["error_kind"] == "auth" and "rejected" in res["error"]
    assert _passes(db)[-1].finished is None


def test_one_offs_report_a_missing_key(db, build):
    clk = Clock(RTH)
    col, _ = build(clk, client_factory=lambda: FakeMassive(clk, key=False))
    for run in (col.run_once, col.run_universe, col.run_eod, lambda: col.run_history(["AAA"])):
        res = run()
        assert res["ok"] is False and res["error"] == scr_collector.NO_KEY_TEXT


def test_run_forever_ticks_sleeps_and_stops(db, build, tmp_path):
    clk = Clock(RTH)
    slept = []
    col, _ = build(clk, sleep=lambda s: slept.append(s))
    col.run_forever(max_ticks=3)
    assert len(slept) == 2 and all(s >= 1.0 for s in slept)
    assert _doc(tmp_path)["state"] == "stopped"


def test_nothing_outside_scr_store_touches_the_tables():
    src = MODULE.read_text(encoding="utf-8")
    for word in ("db.query(", "db.add(", "db.execute(", "db.commit(", "screener_models", "session.query(",
                 "insert(", "delete("):
        assert word not in src, word
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    assert "sqlalchemy" not in " ".join(mods)
    for word in ("ib_insync", "th_ibkr", "eventkit", "py -3.12", "clientid"):
        assert word not in src.lower(), word


# ───────────────────────────────────────── the tray ─────────────────────────────────────────

def _tray_function(state_path):
    """``get_screener_collector_status`` lifted out of tray_status.py with its stale
    threshold - the module itself imports pystray / PIL and cannot load here."""
    tree = ast.parse(TRAY.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "get_screener_collector_status")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "OPTIONS_SCREENER_STALE_SEC" for t in n.targets))]
    assert len(keep) == 2
    ns = {"json": json, "datetime": _dt.datetime, "timezone": _dt.timezone, "Path": Path,
          "OPTIONS_SCREENER_STATE_PATH": state_path}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(TRAY), "exec"), ns)   # noqa: S102
    return ns["get_screener_collector_status"]


TRAY_STATES = {"starting", "pass", "universe", "stocks", "history", "idle", "error", "stopped", "stale",
               "absent"}


def test_state_file_and_the_tray_read_together(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    col.tick()
    status = _tray_function(tmp_path / "state" / "screener_collector.json")
    got = status(now=_utc(RTH) + _dt.timedelta(seconds=20))
    assert got["state"] in TRAY_STATES
    assert (got["state"], got["color"], got["level"], got["tip"]) == ("idle", "green", "ok", "Scr idle")
    line = got["line"]
    assert line.startswith("Options screener: idle")
    for needle in ("last pass 10:00 ET", "universe 3", "IV history 0/3", "hb 20s ago"):
        assert needle in line, (needle, line)
    assert (got["universe_n"], got["history_total"], got["last_pass_et"]) == (3, 3, "10:00 ET")

    got = status(now=_utc(RTH) + _dt.timedelta(minutes=6))      # no heartbeat for 5 min
    assert (got["state"], got["color"]) == ("stale", "amber") and "NO HEARTBEAT" in got["line"]
    assert "TST-Options-Screener" in got["line"] and got["tip"].startswith("Scr stale")

    cl.fail[("chain_snapshot", None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)
    clk.advance(minutes=30)
    col.tick()
    got = status(now=_utc(clk.t))
    assert (got["state"], got["color"], got["tip"]) == ("error", "amber", "Scr ERR")
    assert "Massive rejected the API key" in got["line"] and "Massive failing" in got["line"]

    col.stop("test stop")
    got = status(now=_utc(clk.t))
    assert (got["state"], got["color"], got["tip"]) == ("stopped", "amber", "Scr stopped")


def test_tray_status_function_cases(tmp_path):
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    now = _dt.datetime(2026, 10, 8, 14, 0, tzinfo=_dt.timezone.utc)
    got = status(now=now)
    assert (got["state"], got["level"], got["color"], got["tip"]) == ("absent", "absent", "grey", "Scr -")
    assert "not running on this PC" in got["line"]

    base = {"state": "pass", "pass_id": 12, "pass_kind": "cycle", "symbols_done": 1234, "symbols_total": 4512,
            "last_pass_et": "09:45 ET", "universe_n": 4512, "history_done_n": 1204, "history_total": 4512,
            "api_ok": True, "heartbeat": "2026-10-08T13:59:40+00:00", "detail": "cycle pass 12: 1,234/4,512",
            "last_error": None, "stock_days_pending": 0}
    cases = [
        ({}, "pass", "green", "Scr p12",
         ("cycle pass 12 1,234/4,512", "last pass 09:45 ET", "universe 4,512", "IV history 1,204/4,512",
          "hb 20s ago")),
        ({"state": "idle"}, "idle", "green", "Scr idle", ("idle", "last pass 09:45 ET")),
        ({"state": "history"}, "history", "green", "Scr history", ("history",)),
        ({"state": "stocks", "stock_days_pending": 412}, "stocks", "green", "Scr stocks", ("stock days to go 412",)),
        ({"state": "universe", "last_pass_et": None}, "universe", "green", "Scr universe", ("no pass yet",)),
        ({"state": "starting"}, "starting", "green", "Scr starting", ("starting",)),
        ({"state": "error", "detail": "TST_MASSIVE_API_KEY is not set on this PC; next try 10:05 ET"},
         "error", "amber", "Scr ERR", ("ERROR", "TST_MASSIVE_API_KEY is not set on this PC")),
        ({"state": "error", "api_ok": False, "detail": "Massive rejected the API key (HTTP 401)"},
         "error", "amber", "Scr ERR", ("Massive failing", "rejected")),
        ({"state": "stopped", "detail": "one-off --once run finished"}, "stopped", "amber", "Scr stopped",
         ("STOPPED", "one-off")),
        ({"state": "something-new"}, "idle", "green", "Scr idle", ()),
    ]
    for change, state, color, tip, needles in cases:
        p.write_text(json.dumps(dict(base, **change)), encoding="utf-8")
        got = status(now=now)
        assert got["state"] in TRAY_STATES
        assert (got["state"], got["color"], got["tip"]) == (state, color, tip), change
        for n in needles:
            assert n in got["line"], (change, n, got["line"])
    p.write_text(json.dumps(dict(base, heartbeat="2026-10-08T13:50:00Z")), encoding="utf-8")
    got = status(now=now)
    assert (got["state"], got["color"]) == ("stale", "amber") and "10m" in got["line"]
    p.write_text("{not json", encoding="utf-8")
    got = status(now=now)
    assert (got["state"], got["color"], got["tip"]) == ("error", "amber", "Scr ?")


def test_tray_wires_the_line_into_tooltip_and_window():
    src = TRAY.read_text(encoding="utf-8")
    compile(src, str(TRAY), "exec")
    tree = ast.parse(src)
    callers = set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "get_screener_collector_status"):
                    callers.add(fn.name)
    assert {"_update_loop", "_build_progress_window"} <= callers
    assert '"dashboard_tst" / "state" / "screener_collector.json"' in src
    assert scr_collector.STATE_PATH.relative_to(DASH_ROOT.parent).as_posix() == \
        "dashboard_tst/state/screener_collector.json"
    assert "{scr_str}" in src                                   # the tooltip fragment


# ───────────────────────────────────────── the CLI + the task script ─────────────────────────────────────────

def _load_cli():
    spec = importlib.util.spec_from_file_location("screener_collector_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCollector:
    made: list = []
    result: dict = {"ok": True}

    def __init__(self, session_factory, log=None, **kw):
        self.ran: list[str] = []
        self.stopped = None
        self.workers, self.max_rps, self.cycle_min = 8, 40.0, 30
        self.state_path = Path("state/screener_collector.json")
        _FakeCollector.made.append(self)

    def run_forever(self):
        self.ran.append("forever")

    def run_once(self):
        self.ran.append("once")
        return dict(self.result)

    def run_universe(self):
        self.ran.append("universe")
        return dict(self.result)

    def run_eod(self):
        self.ran.append("eod")
        return dict(self.result)

    def run_history(self, symbols):
        self.ran.append("history:" + ",".join(symbols))
        return dict(self.result)

    def stop(self, reason="stopped"):
        self.stopped = reason


@pytest.fixture
def logger_levels():
    names = ("screener_collector",) + tuple(_load_cli().APP_LOGGERS) + ("httpx", "httpcore")
    saved = {n: (logging.getLogger(n).level, logging.getLogger(n).disabled) for n in names}
    yield
    for n, (lvl, dis) in saved.items():
        logging.getLogger(n).setLevel(lvl)
        logging.getLogger(n).disabled = dis


def test_cli_arguments_modes_and_exit_codes(monkeypatch, logger_levels):
    cli = _load_cli()
    a = cli.parse_args([])
    assert not (a.once or a.history or a.eod_now or a.forever or a.universe_now) and a.log_file is None
    assert cli.parse_args(["--history", "NVDA", "SPY"]).history == ["NVDA", "SPY"]
    assert cli.parse_args(["--universe-now", "-v"]).verbose is True
    for bad in (["--once", "--eod-now"], ["--universe-now", "--once"], ["--port", "4002"]):
        with pytest.raises(SystemExit):
            cli.parse_args(bad)
    assert (cli.EXIT_OK, cli.EXIT_SETUP, cli.EXIT_SOURCE) == (0, 1, 2)
    monkeypatch.setattr(scr_collector, "Collector", _FakeCollector)
    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    try:
        _FakeCollector.made.clear()
        _FakeCollector.result = {"ok": True}
        assert cli.main(["--no-init"]) == 0
        assert cli.main(["--no-init", "--once"]) == 0
        assert cli.main(["--no-init", "--universe-now"]) == 0
        assert cli.main(["--no-init", "--history", "NVDA", "SPY"]) == 0
        _FakeCollector.result = {"ok": False, "error": scr_collector.NO_KEY_TEXT}
        assert cli.main(["--no-init", "--eod-now"]) == 2
        assert [c.ran for c in _FakeCollector.made] == [["forever"], ["once"], ["universe"],
                                                        ["history:NVDA,SPY"], ["eod"]]
        stops = [c.stopped for c in _FakeCollector.made]
        assert stops[0] is None and stops[1] == "one-off --once run finished"
    finally:
        root.setLevel(level)
        for h in list(root.handlers):
            for x in [x for x in h.filters if isinstance(x, cli.KeyScrub)]:
                h.removeFilter(x)
            if h not in before:
                root.removeHandler(h)


def test_cli_runs_the_screener_migrations_first(monkeypatch, tmp_path, logger_levels):
    cli = _load_cli()
    url = "sqlite:///" + (tmp_path / "cli.db").as_posix()
    monkeypatch.setenv("TST_SCREENER_DATABASE_URL", url)
    screener_db.configure(None)
    monkeypatch.setattr(scr_collector, "Collector", _FakeCollector)
    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    try:
        _FakeCollector.result = {"ok": True}
        assert cli.main(["--once"]) == 0
        from sqlalchemy import create_engine, inspect  # noqa: PLC0415
        eng = create_engine(url)
        try:
            assert "scr_contract" in inspect(eng).get_table_names()
        finally:
            eng.dispose()
        assert cli._db_label("postgresql://u:secret@h/db") == "postgresql://u:***@h/db"
    finally:
        screener_db.configure(None)
        root.setLevel(level)
        for h in list(root.handlers):
            for x in [x for x in h.filters if isinstance(x, cli.KeyScrub)]:
                h.removeFilter(x)
            if h not in before:
                root.removeHandler(h)


def test_cli_masks_the_key_and_rotates_its_log(monkeypatch, tmp_path, logger_levels):
    cli = _load_cli()
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    f = cli.KeyScrub()
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed with %s", (KEY,), None)
    assert f.filter(rec) and KEY not in rec.getMessage()
    assert (cli.LOG_MAX_BYTES, cli.LOG_BACKUPS) == (5 * 1024 * 1024, 5)
    path = tmp_path / "logs" / "screener_collector.log"
    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    try:
        cli._logging(logging.INFO, str(path))
        cli._logging(logging.INFO, str(path))
        files = [h for h in root.handlers if isinstance(h, cli.RotatingFileHandler)]
        assert len(files) == 1 and files[0].maxBytes == cli.LOG_MAX_BYTES
        logging.getLogger("screener_collector").info("collector line %s", KEY)
        files[0].flush()
        text = path.read_text(encoding="utf-8")
        assert "collector line ***" in text and KEY not in text
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        logging.captureWarnings(False)
        root.setLevel(level)
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        for h in before:
            for x in [x for x in h.filters if isinstance(x, cli.KeyScrub)]:
                h.removeFilter(x)


def test_task_script_is_ascii_and_registers_the_always_on_task():
    raw = PS1.read_bytes()
    src = raw.decode("ascii")                    # PS 5.1 reads a BOM-less file as ANSI
    for needle in ('"TST-Options-Screener"', "screener_collector.py", "--forever",
                   "screener_collector.log", "-AtStartup", "-Daily -At $At", '"07:00"',
                   "-RestartCount 3", "-RestartInterval (New-TimeSpan -Minutes 5)",
                   "-MultipleInstances IgnoreNew", "-ExecutionTimeLimit ([TimeSpan]::Zero)",
                   "[switch] $StartNow", "Start-ScheduledTask", ".venv\\Scripts\\python.exe",
                   "--log-file", "TST_MASSIVE_API_KEY", "-Quiet", "schtasks.exe /End",
                   "screener_collector\\.py"):
        assert needle in src, needle
    assert "&&" not in src and " ? " not in src
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert ">>" not in code and "cmd.exe" not in code
    assert "options_collector\\.py" not in code               # never kills the basket collector
    low = src.lower()
    for word in ("py -3.12", "ib_insync", "clientid", "gateway", "4002"):
        assert word not in low, word
    assert CLI.read_bytes().decode("ascii")                   # the CLI is ASCII too
    assert sys.version_info >= (3, 10)
