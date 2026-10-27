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
import gzip
import importlib.util
import json
import logging
import math
import re
import sys
import threading
from pathlib import Path

import pytest

from app import screener_db
from app.screener_models import ScrContract, ScrPass, ScrUnderlyingDaily
from app.services import clock, flat_files, massive, scr_collector, scr_flativ, scr_store
from app.services.black_scholes import black_scholes
from app.services.massive import MassiveError
from app.services.opt_constants import RISK_FREE

from .conftest import DASH_ROOT
from .fixtures.options import bs_greeks
from .test_flat_files import ClientError, EndpointConnectionError, FakeS3

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
        # the contracts list (option_underlyings): ``uni_page`` symbols a page (None: one page);
        # ``uni_fail[page]`` raises on that page (once), carrying the counts / cursor as the real
        # client does; ``uni_gate = (page, release, arrived)`` holds the walk after that page;
        # ``uni_hook(page)`` runs before each page; ``uni_urls`` = the pages asked for.
        self.uni_page: int | None = None
        self.uni_fail: dict = {}
        self.uni_gate = None
        self.uni_hook = None
        self.uni_urls: list[str] = []
        self.chain_rows: dict = {}                   # symbol -> rows to return instead of the grid

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
        if symbol in self.chain_rows:
            return {"symbol": symbol, "rows": list(self.chain_rows[symbol]), "underlying_price": None,
                    "underlying_as_of": None, "pages": 1, "as_of": stamp}
        S = SPOTS[symbol]
        rows = [self._row(symbol, e, round(S * m, 2), r, now, stamp)
                for e in EXPIRIES for m in MULTS for r in ("C", "P")]
        rows.append(dict(rows[0], multiplier=10, ticker="O:%s1%s" % (symbol, rows[0]["ticker"][2 + len(symbol):])))
        rows.append(self._row(symbol, EXPIRIES[-1], round(S * 1.5, 2), "C", now, stamp, oi=0, volume=0))
        rows.append(self._row(symbol, EXPIRIES[-1], round(S * 0.8, 2), "P", now,
                              stamp - _dt.timedelta(days=2), oi=300, volume=12))
        return {"symbol": symbol, "rows": rows, "underlying_price": None, "underlying_as_of": None,
                "pages": 2, "as_of": stamp}

    def _uni_url(self, page: int) -> str:
        return "%s/v3/reference/options/contracts?cursor=%d" % (self.base_url, page)

    def option_underlyings(self, exp_lte=None, *, contract_type="call", start_url=None, counts=None,
                           on_page=None, **kw):
        self._rec("option_underlyings", None, exp_lte=str(exp_lte), contract_type=contract_type,
                  start_url=start_url, counts=dict(counts or {}))
        items = list(self.universe.items())
        size = self.uni_page or max(1, len(items))
        pages = [dict(items[i:i + size]) for i in range(0, len(items), size)] or [{}]
        first = int(start_url.rsplit("=", 1)[1]) if start_url else 1
        got = dict(counts or {})
        n = 0
        for page in range(first, len(pages) + 1):
            url = self._uni_url(page)
            self.uni_urls.append(url)
            if self.uni_hook is not None:
                self.uni_hook(page)
            exc = self.uni_fail.pop(page, None)
            if exc is not None:
                exc.counts, exc.pages, exc.resume_url = dict(got), n, url
                raise exc
            for sym, c in pages[page - 1].items():
                got[sym] = got.get(sym, 0) + c
            n += 1
            if on_page is not None:
                try:
                    on_page(n, dict(got), self._uni_url(page + 1) if page < len(pages) else None)
                except Exception:  # noqa: BLE001 - as the real client: a hook never stops the walk
                    pass
            if self.uni_gate is not None and self.uni_gate[0] == page:
                self.uni_gate[2].set()
                assert self.uni_gate[1].wait(30), "the test never released the walk"
        return got

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
                "TST_SCREENER_WORKERS", "TST_SCREENER_MAX_RPS", "TST_SCREENER_MAX_DTE",
                "TST_MASSIVE_S3_KEY_ID", "TST_MASSIVE_S3_SECRET", "TST_MASSIVE_S3_ENDPOINT", "TST_MASSIVE_S3_BUCKET"):
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
        if "flat_store" not in kw:
            kw.setdefault("flat_factory", lambda: None)     # no Flat Files keys (the day-file lane off)
        if "history_store" not in kw:
            kw.setdefault("history_factory", lambda: None)  # the parquet lane off (v4.139)
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
    assert doc["state"] == "idle"
    assert doc["detail"] == "Up to date with the Thu Oct 8 10:00 ET read; next market pass Thu Oct 8 10:30 ET."
    assert cl.of("option_underlyings")[0][2]["contract_type"] == "call"
    assert doc["universe_done"] == _utc(RTH).isoformat() and st["universe_done"] == RTH
    assert doc["error_kind"] is None and doc["next_try"] is None and doc["warn"] is None
    assert set(doc["progress"]) == set(scr_collector.PROGRESS_KEYS)
    # v4.149: the surface tab's countdown - the 10:00 read, a 30-min cycle -> the next pass at 10:30
    assert doc["progress"]["cycle_min"] == 30 and 1700 <= doc["progress"]["next_pass_in_s"] <= 1800
    assert doc["progress"]["next_pass"].endswith("10:30:00-04:00")
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


# ───────────────────────────────────────── v4.137: the first-run fix ─────────────────────────────────────────

FRI_0725 = _dt.datetime(2026, 10, 9, 11, 25)      # Friday 07:25 ET
MON_RTH = _dt.datetime(2026, 10, 12, 13, 50)      # Monday 09:50 ET


def _e403(what="the options chain snapshot", *, window=False):
    exc = MassiveError("plan", "your Massive plan does not include %s (HTTP 403)" % what, 403)
    exc.window = window
    return exc


def _http(what="the options contracts list", status=502):
    return MassiveError("http", "Massive answered HTTP %d for %s" % (status, what), status)


def _gated(clk, build, page=1, **kw):
    """A collector whose universe walk runs on its own REAL thread and is held after
    ``page`` (one symbol a page: AAA, BBB, CCC) until the test releases it; every other
    job runs inline."""
    cl = FakeMassive(clk)
    cl.uni_page = 1
    release, arrived = threading.Event(), threading.Event()
    cl.uni_gate = (page, release, arrived)
    col, _ = build(clk, client=cl, universe_executor_factory=scr_collector._thread_pool, **kw)
    return col, cl, release, arrived


# -- the universe and the first pass --

def test_streamed_universe_starts_the_pass_before_the_walk_ends(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl, release, arrived = _gated(clk, build)
    try:
        col.tick()                                           # the walk starts on its own thread
        assert arrived.wait(30)
        assert scr_store.pass_symbols(db, now=RTH) == ["AAA"]          # page 1 is filed at once
        assert col.tick() == "pass"                          # the first pass starts on it
        assert cl.read() == ["AAA"] and len(_contracts(db, "AAA")) == KEPT
        doc = _doc(tmp_path)
        assert doc["state"] == "pass" and doc["universe_done"] is None and doc["error_kind"] is None
        assert doc["detail"] == (
            "Reading the option market (live, 15-min delayed): all 1 underlyings listed so far are read - "
            "waiting for the rest of the list." + scr_collector.DETAIL_JOIN +
            "Step 1 of 2: reading Massive's list of optionable stocks - page 1 (1 stocks so far, 0 min). "
            "Results start appearing as soon as the first stocks are read.")
        pr = doc["progress"]
        assert (pr["universe_pages"], pr["universe_symbols"], pr["universe_started"]) == (1, 1, _utc(RTH).isoformat())
        assert (pr["pass_kind"], pr["pass_pct"]) == ("cycle", 100.0)
        assert _passes(db)[-1].finished is None              # it waits for the rest of the list
        assert _passes(db)[-1].n_symbols == 1
        assert _und(db, "AAA")["sec_type"] == "stock"        # identities run on the partial list
    finally:
        release.set()
    col._uni_future.result(timeout=30)
    col.tick()                                               # the list is complete: the pass grows and ends
    assert cl.read() == ["AAA", "BBB", "CCC"]
    p = _passes(db)[-1]
    assert p.finished is not None and (p.n_ok, p.n_failed, p.n_contracts) == (3, 0, 3 * KEPT)
    assert p.n_symbols == 3                                  # the pass row follows the growth (frame T-21)
    assert _und(db, "BBB")["sec_type"] == "etf"              # named from the cached identity records
    assert len(cl.of("reference_tickers", "stocks")) == 1
    doc = _doc(tmp_path)
    assert doc["universe_done"] == _utc(RTH).isoformat() and doc["state"] == "idle"
    assert doc["last_pass_contracts"] == 3 * KEPT


def test_growing_pass_reads_symbols_filed_later_before_last_eod_is_set(db, build, tmp_path):
    clk = Clock(EOD)
    col, cl, release, arrived = _gated(clk, build, page=2)
    try:
        col.tick()
        assert arrived.wait(30)
        assert scr_store.pass_symbols(db, now=EOD) == ["AAA"]   # page 2 waits for the 25-page / 30 s save
        col.tick()
        assert cl.read() == ["AAA"] and _passes(db)[-1].kind == "eod"
        clk.advance(minutes=1)
        col.tick()
        assert col._pass is not None and col._last_eod is None
        assert _doc(tmp_path)["last_eod_session"] is None
    finally:
        release.set()
    col._uni_future.result(timeout=30)
    col.tick()
    assert cl.read() == ["AAA", "BBB", "CCC"] and col._last_eod == DAY
    p = _passes(db)[-1]
    assert (p.kind, p.n_ok) == ("eod", 3) and p.finished is not None
    db.expire_all()
    assert db.query(ScrUnderlyingDaily).filter(ScrUnderlyingDaily.on == DAY,
                                               ScrUnderlyingDaily.iv30.isnot(None)).count() == 3


def test_universe_walk_resumes_at_failed_page_not_page_1(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    cl.uni_page = 1
    cl.uni_fail[2] = _http()
    assert col.tick() == "error"
    doc = _doc(tmp_path)
    assert doc["error_kind"] == "http" and doc["universe_done"] is None
    assert doc["detail"] == ("universe: Massive answered HTTP 502 for the options contracts list"
                             + scr_collector.NO_UNIVERSE_SUFFIX + "; next try 10:01 ET")
    assert _dt.datetime.fromisoformat(doc["next_try"]) == _utc(RTH + _dt.timedelta(seconds=60))
    assert (doc["progress"]["universe_pages"], doc["progress"]["universe_symbols"]) == (1, 1)
    assert scr_store.pass_symbols(db, now=RTH) == ["AAA"]            # page 1 is kept
    clk.advance(seconds=15)
    col.tick()
    assert len(cl.of("option_underlyings")) == 1                       # not before 60 s
    clk.advance(seconds=50)
    col.tick()
    calls = cl.of("option_underlyings")
    assert len(calls) == 2 and calls[1][2]["start_url"] == cl._uni_url(2)
    assert calls[1][2]["counts"] == {"AAA": 3000}
    assert cl.uni_urls == [cl._uni_url(1), cl._uni_url(2), cl._uni_url(2), cl._uni_url(3)]   # page 1 read once
    assert scr_store.pass_symbols(db, now=clk.t) == ["AAA", "BBB", "CCC"]
    doc = _doc(tmp_path)
    assert doc["error_kind"] is None and doc["universe_done"] is not None and doc["last_error"] is None


def test_universe_cursor_refused_starts_again_from_page_1(db, build):
    clk = Clock(RTH)
    col, cl = build(clk)
    cl.uni_page = 1
    cl.uni_fail[2] = MassiveError("network", "could not reach Massive for the options contracts list (ReadTimeout: )")
    col.tick()
    assert col._retry_at["all"] - clk.t == _dt.timedelta(seconds=60) and "universe" not in col._alerts
    cl.uni_fail[2] = _http(status=400)                       # the saved cursor is refused
    clk.advance(seconds=61)
    col.tick()
    assert [c[2]["start_url"] for c in cl.of("option_underlyings")] == [None, cl._uni_url(2), None]
    assert scr_store.pass_symbols(db, now=clk.t) == ["AAA", "BBB", "CCC"]
    assert col._universe_done is not None and col._uni_resume is None


def test_partial_universe_never_deactivates_and_does_not_count_as_refreshed(db, build, tmp_path):
    clk = Clock(RTH)
    scr_store.upsert_universe(db, {"DDD": 10, "AAA": 5}, now=RTH - _dt.timedelta(days=1))   # an older list, no stamp
    col, cl = build(clk)
    cl.chain_rows["DDD"] = []
    cl.uni_page = 1
    cl.uni_fail[2] = _http()
    col.tick()
    db.expire_all()
    assert scr_store.universe_info(db)["n"] == 2              # AAA updated, DDD still active
    assert col._universe_done is None and _status(db)["universe_done"] is None
    assert _status(db)["universe_on"] is None
    clk.advance(seconds=61)
    col.tick()                                               # resumed and complete: only now is DDD gone
    db.expire_all()
    info = scr_store.universe_info(db)
    assert (info["n"], info["total"]) == (3, 4) and col._universe_done == clk.t
    assert _status(db)["universe_done"] == clk.t


def test_day2_refresh_failure_keeps_yesterdays_list_warn_not_error(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()                                               # Thursday: a complete list
    col._last_eod = DAY
    clk.t = _dt.datetime(2026, 10, 9, 11, 35)                # Friday 07:35 ET: the daily refresh fails
    cl.fail[("option_underlyings", None)] = _http()
    assert col.tick() != "error"
    doc = _doc(tmp_path)
    assert doc["error_kind"] is None and doc["state"] != "error"
    assert doc["warn"] == ("Universe refresh failed 07:35 ET (Massive answered HTTP 502 for the options "
                           "contracts list); next try 07:50 ET - yesterday's list in use.")
    assert _dt.datetime.fromisoformat(doc["next_try"]) == _utc(clk.t + _dt.timedelta(minutes=15))
    assert doc["last_error"].startswith("universe: Massive answered HTTP 502")
    assert scr_store.pass_symbols(db, now=clk.t) == ["AAA", "BBB", "CCC"] and doc["universe_n"] == 3
    assert _status(db)["warn"] == doc["warn"]
    cl.fail.clear()
    clk.advance(minutes=15)
    col.tick()
    doc = _doc(tmp_path)
    assert doc["warn"] is None and doc["last_error"] is None and len(cl.of("option_underlyings")) == 3
    assert doc["universe_done"] == _utc(clk.t).isoformat()


@pytest.mark.parametrize("exc", [
    _http(),
    MassiveError("rate", "Massive kept answering 'too many requests' for the options contracts list (HTTP 429)", 429),
], ids=["http", "rate"])
def test_universe_http_rate_error_on_empty_db_is_error_with_next_try(db, build, tmp_path, exc):
    clk = Clock(SAT)
    col, cl = build(clk)
    cl.fail[("option_underlyings", None)] = exc
    assert col.tick() == "error"
    doc = _doc(tmp_path)
    assert (doc["state"], doc["error_kind"], doc["universe_n"]) == ("error", exc.kind, 0)
    assert doc["detail"] == "universe: %s%s; next try 12:15 ET" % (exc, scr_collector.NO_UNIVERSE_SUFFIX)
    assert _dt.datetime.fromisoformat(doc["next_try"]) == _utc(SAT + _dt.timedelta(minutes=15))
    st = _status(db)
    assert (st["state"], st["error_kind"], st["next_try"]) == ("error", exc.kind, SAT + _dt.timedelta(minutes=15))
    clk.advance(minutes=5)
    col.tick()
    assert len(cl.of("option_underlyings")) == 1
    cl.fail.clear()
    clk.advance(minutes=10)
    assert col.tick() != "error"                             # the recovery clears it
    doc = _doc(tmp_path)
    assert doc["error_kind"] is None and doc["last_error"] is None and doc["next_try"] is None
    assert scr_store.pass_symbols(db, now=clk.t) == ["AAA", "BBB", "CCC"]


def test_network_blip_still_retries_after_60s(db, build):
    clk = Clock(SAT)
    col, cl = build(clk)
    cl.fail[("option_underlyings", None)] = MassiveError("network", "could not reach Massive for the options "
                                                                    "contracts list (ConnectError: )")
    assert col.tick() == "error"
    assert col._retry_at["all"] - clk.t == _dt.timedelta(seconds=60)
    assert "universe" not in col._alerts and col._universe_retry_at is None    # no 15-min universe alert on top
    cl.fail.clear()
    clk.advance(seconds=61)
    col.tick()
    assert len(cl.of("option_underlyings")) == 2 and col.error_kind() is None
    assert scr_store.pass_symbols(db, now=clk.t) == ["AAA", "BBB", "CCC"]


def test_empty_universe_is_error_and_not_retried_each_tick(db, build, tmp_path):
    clk = Clock(SAT)
    col, cl = build(clk)
    cl.universe = {}
    assert col.tick() == "error"
    doc = _doc(tmp_path)
    assert doc["error_kind"] == "empty" and doc["universe_on"] is None and doc["universe_done"] is None
    assert doc["detail"].startswith(scr_collector.UNIVERSE_EMPTY_TEXT + scr_collector.NO_UNIVERSE_SUFFIX)
    assert doc["last_error"] == scr_collector.UNIVERSE_EMPTY_TEXT
    for _ in range(4):
        clk.advance(seconds=15)
        col.tick()
    assert len(cl.of("option_underlyings")) == 1                       # not asked again every tick
    cl.universe = dict(UNIVERSE)
    clk.t = SAT + _dt.timedelta(minutes=15, seconds=1)
    col.tick()
    assert len(cl.of("option_underlyings")) == 2 and col.error_kind() is None


def test_universe_straddling_0730_runs_once(db, build):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()                                               # Thursday 10:00 ET: the list
    clk2 = Clock(FRI_0725)                                   # a restart at 07:25 ET Friday: the list is 21 h old
    col2, _ = build(clk2, client=cl)
    cl.uni_hook = lambda page: clk2.advance(minutes=10)      # the walk takes 10 min: it ends at 07:35
    col2.tick()
    assert len(cl.of("option_underlyings")) == 2
    assert col2._universe_done == _dt.datetime(2026, 10, 9, 11, 35)     # its completion, not its start
    cl.uni_hook = None
    for minutes in (1, 10, 30):
        clk2.t = _dt.datetime(2026, 10, 9, 11, 35) + _dt.timedelta(minutes=minutes)
        col2.tick()
    assert len(cl.of("option_underlyings")) == 2             # not read again for 07:30


def _partial_first_eod(build):
    """First start at 16:25 ET: page 1 (AAA) is filed, page 2 fails, and the EOD pass reads
    AAA and ends 'finished' on that PARTIAL list (the walk waits 60 s for its resume)."""
    clk = Clock(EOD)
    col, cl = build(clk)
    cl.uni_page = 1
    cl.uni_fail[2] = _http()
    col.tick()                                               # the walk (inline): AAA filed, page 2 fails
    clk.advance(seconds=15)
    col.tick()                                               # the EOD pass on [AAA]
    assert col._partial_eod == DAY and col._last_eod is None
    return clk, col, cl


def test_restart_after_a_partial_eod_reads_the_rest_once_the_list_is_complete(db, build):
    clk, col, cl = _partial_first_eod(build)
    p = _passes(db)[-1]
    assert (p.kind, p.session, p.n_symbols, p.partial) == ("eod", DAY, 1, True) and p.finished is not None
    col.stop("restart")                                      # before the walk completes
    clk2 = Clock(EOD + _dt.timedelta(minutes=2))
    col2, _ = build(clk2, client=cl)
    col2.tick()                                              # restored as partial: no EOD on [AAA] again ...
    assert col2._partial_eod == DAY and col2._last_eod is None
    for _ in range(4):
        clk2.advance(seconds=15)
        col2.tick()                                          # ... the walk completes, then the full EOD pass
    assert col2._universe_done is not None and col2._last_eod == DAY
    eods = [x for x in _passes(db) if x.kind == "eod" and x.finished is not None]
    assert [(x.n_symbols, x.partial) for x in eods] == [(1, True), (3, False)]
    assert _contracts(db, "BBB") and _contracts(db, "CCC")
    assert sorted(cl.read()) == ["AAA", "AAA", "BBB", "CCC"]


def test_restart_during_the_full_eod_after_a_partial_one_reads_the_rest(db, build):
    clk, col, cl = _partial_first_eod(build)
    for sym in UNIVERSE:                                     # the full pass will pause at once
        cl.fail[("chain_snapshot", sym)] = MassiveError("network", "could not reach Massive (ConnectError: )")
    for _ in range(4):
        clk.advance(seconds=61)
        col.tick()                                           # the walk resumes and completes; the full pass starts
    assert col._universe_done is not None
    assert any(x.kind == "eod" and x.finished is None and x.n_symbols == 3 for x in _passes(db))
    col.stop("restart")
    cl.fail.clear()
    n = len(cl.read())
    clk2 = Clock(clk.t + _dt.timedelta(minutes=2))
    col2, _ = build(clk2, client=cl)
    for _ in range(3):
        col2.tick()
        clk2.advance(seconds=15)
    assert sorted(cl.read()[n:]) == ["AAA", "BBB", "CCC"] and col2._last_eod == DAY
    assert _contracts(db, "BBB") and _contracts(db, "CCC")


@pytest.mark.parametrize("partial", [False, None], ids=["complete", "legacy-null"])
def test_restart_after_a_complete_eod_reads_nothing_again(db, build, partial):
    clk = Clock(SAT)
    col, cl = build(clk)
    assert col.run_universe()["ok"]
    pid = scr_store.start_pass(db, kind="eod", session="2026-10-09", n_symbols=3, now=SAT)
    scr_store.finish_pass(db, pid, n_ok=3, n_failed=0, n_contracts=90, finished=True, now=SAT, partial=partial)
    col2, _ = build(clk, client=cl)
    col2.tick()
    assert col2._last_eod == "2026-10-09" and col2._partial_eod is None
    assert cl.read() == [] and len(_passes(db)) == 1


def test_resumed_walk_shows_its_progress_not_the_error(db, build, tmp_path):
    clk = Clock(RTH)
    cl = FakeMassive(clk)
    cl.uni_page = 1
    cl.universe["DDD"] = 100                                 # a 4th page
    cl.chain_rows["DDD"] = []
    release, arrived = threading.Event(), threading.Event()
    cl.uni_gate = (3, release, arrived)
    col, _ = build(clk, client=cl, universe_executor_factory=scr_collector._thread_pool)
    cl.uni_fail[2] = _http()
    try:
        col.tick()                                           # the walk on its thread: AAA filed, page 2 fails
        col._uni_future.result(timeout=30)
        assert col.tick() == "error" and col.error_kind() == "http"
        doc = _doc(tmp_path)
        # AAA is filed and read: "nothing can be screened" (T-09) would not be true now
        assert scr_collector.NO_UNIVERSE_SUFFIX not in doc["detail"]
        assert doc["detail"].startswith("universe: Massive answered HTTP 502 for the options contracts list - "
                                        "the universe refresh paused, the rest carries on; next try 10:01 ET")
        clk.advance(seconds=61)
        col.tick()                                           # the resumed walk starts at page 2
        assert arrived.wait(30)                              # pages 2 and 3 are back; held after 3
        clk.advance(seconds=15)
        assert col.tick() in ("universe", "pass")            # the walk works again: its progress, not an error
        doc = _doc(tmp_path)
        assert doc["error_kind"] is None and doc["state"] in ("universe", "pass")
        assert "Step 1 of 2: reading Massive's list of optionable stocks - page 3 (" in doc["detail"]
        assert doc["last_error"].startswith("universe: Massive answered HTTP 502")    # kept until it completes
        cl.uni_fail[4] = _http()                             # ... and the resumed walk fails again
    finally:
        release.set()
    col._uni_future.result(timeout=30)
    assert col.tick() == "error" and col.error_kind() == "http"     # the alert is back
    clk.advance(seconds=61)
    col.tick()                                               # resumed at page 4 and complete
    col._uni_future.result(timeout=30)
    col.tick()
    assert col.error_kind() is None and col.last_error is None and col._universe_done is not None
    assert cl.uni_urls.count(cl._uni_url(1)) == 1             # page 1 was never read again


def test_pass_weights_come_from_the_stored_universe_after_a_restart(db, build, tmp_path):
    clk = Clock(EOD)
    col, cl = build(clk)
    assert col.run_universe()["ok"]
    col.stop("restart")
    col2, _ = build(clk, client=cl)                          # no walk is due: nothing in _uni_counts
    cl.fail[("chain_snapshot", "BBB")] = MassiveError("network", "could not reach Massive (ConnectError: )")
    col2.tick()                                              # AAA read; BBB pauses the pass; CCC not read
    p = col2._pass
    assert col2._uni_counts == {} and p is not None and p.w == UNIVERSE
    assert col2._pass_pct(p) == pytest.approx(3000 / 4700)   # by contracts, not 1 of 3 symbols
    assert _doc(tmp_path)["progress"]["pass_pct"] == 63.8


# -- no single symbol or failed read stalls a pass --

def test_single_symbol_403_counts_failed_and_pass_finishes(db, build, caplog):
    caplog.set_level(logging.WARNING, logger="test_scr_collector")
    for sym, t0 in (("AAA", RTH), ("CCC", RTH + _dt.timedelta(days=1))):    # sorted first, then last
        clk = Clock(t0)
        col, cl = build(clk)
        cl.fail[("chain_snapshot", sym)] = _e403()
        col.tick()
        col.tick()
        p = _passes(db)[-1]
        assert (p.n_ok, p.n_failed, p.finished is not None) == (2, 1, True), sym
        assert col.shown_state != "error" and col.error_kind() is None
        assert sym in col._refused
        assert "Massive refused %s's chain (HTTP 403) - skipped this pass" % sym in caplog.text
        n = len(cl.of("chain_snapshot", sym))
        clk.advance(minutes=30)
        col.tick()
        assert len(cl.of("chain_snapshot", sym)) == n        # left out of passes for 24 h
        assert _passes(db)[-1].n_symbols == 2 and _passes(db)[-1].finished is not None


def test_all_symbols_403_pauses_passes(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    cl.fail[("chain_snapshot", None)] = _e403()
    col.tick()
    doc = _doc(tmp_path)
    assert doc["state"] == "error" and doc["error_kind"] == "plan"
    assert "market passes paused, the rest carries on" in doc["detail"]
    p = _passes(db)[-1]
    assert p.finished is None and sorted(col._pass.queue) == ["AAA", "BBB", "CCC"] and col._pass.failed == 0
    assert col._refused == {}
    for _ in range(2):                                       # refused at each 5-min look (the pause halts the rest)
        clk.advance(minutes=5, seconds=1)
        col.tick()
    p = col._pass                                            # AAA was requeued twice: the third refusal counts
    assert p is not None and (p.failed, p.ok, p.requeues["AAA"]) == (1, 0, 2)
    assert sorted(p.queue) == ["BBB", "CCC"] and col.error_kind() == "plan" and col.shown_state == "error"
    assert _passes(db)[-1].finished is None and col._refused == {}


def test_eod_pass_open_at_monday_rth_closes_unfinished(db, build):
    clk = Clock(SAT)
    col, cl = build(clk)
    col.tick()                                               # the universe
    cl.fail[("chain_snapshot", None)] = MassiveError("network", "could not reach Massive for the options chain "
                                                                "snapshot (ConnectError: )")
    col.tick()                                               # Friday's EOD pass: paused at once
    p = col._pass
    assert p is not None and (p.kind, p.session) == ("eod", "2026-10-09") and p.queue
    cl.fail.clear()
    clk.t = MON_RTH                                          # nothing ticked since: Monday 09:50 ET
    col.tick()
    ps = _passes(db)
    assert (ps[-2].kind, ps[-2].session, ps[-2].finished) == ("eod", "2026-10-09", None)
    assert (ps[-1].kind, ps[-1].session, ps[-1].n_ok) == ("cycle", "2026-10-12", 3)
    assert ps[-1].finished is not None and col._last_eod is None
    res = col.run_eod()                                      # a by-hand EOD pass in the session is not closed
    assert res["ok"] and res["read"] == 3 and _passes(db)[-1].finished is not None


def test_a_pass_over_12_h_old_closes_unfinished(db, build):
    clk = Clock(SAT)
    col, cl = build(clk)
    col.tick()
    cl.fail[("chain_snapshot", None)] = MassiveError("network", "could not reach Massive for the options chain "
                                                                "snapshot (ConnectError: )")
    col.tick()
    first = col._pass.id
    cl.fail.clear()
    clk.advance(hours=12, minutes=1)                         # Sunday 00:01 ET: the same EOD day, but 12 h on
    col.tick()
    ps = _passes(db)
    assert ps[-2].id == first and ps[-2].finished is None
    assert (ps[-1].kind, ps[-1].session, ps[-1].n_ok) == ("eod", "2026-10-09", 3) and ps[-1].finished is not None


def test_all_chains_fail_pass_not_finished_backoff_then_retry(db, build, tmp_path):
    clk = Clock(EOD)
    col, cl = build(clk)
    col.tick()                                               # the universe
    cl.fail[("chain_snapshot", None)] = _http("the options chain snapshot", 500)
    assert col.tick() == "error"
    p = _passes(db)[-1]
    assert (p.kind, p.finished, p.n_ok, p.n_failed) == ("eod", None, 0, 3)
    doc = _doc(tmp_path)
    assert doc["error_kind"] == "empty" and doc["last_eod_session"] is None
    assert doc["last_error"] == ("eod pass %d read no option data: Massive answered HTTP 500 for the options chain "
                                 "snapshot (3 of 3 underlyings)" % p.id)
    assert col._retry_at["chain"] - clk.t == _dt.timedelta(minutes=10)
    clk.advance(minutes=5)
    col.tick()
    assert len(_passes(db)) == 1                             # the passes wait
    clk.advance(minutes=5, seconds=1)
    col.tick()                                               # read again: fails again -> 20 min
    assert len(_passes(db)) == 2 and col._retry_at["chain"] - clk.t == _dt.timedelta(minutes=20)
    cl.fail.clear()
    clk.advance(minutes=20, seconds=1)
    col.tick()
    p = _passes(db)[-1]
    assert p.finished is not None and p.n_ok == 3 and col._last_eod == DAY
    assert col.error_kind() is None and col._empty_fails == 0


def test_mostly_failed_eod_pass_is_finished_but_read_again(db, build):
    clk = Clock(EOD)
    col, cl = build(clk)
    col.tick()
    cl.fail[("chain_snapshot", "BBB")] = _http("the options chain snapshot", 500)
    cl.fail[("chain_snapshot", "CCC")] = _http("the options chain snapshot", 500)
    col.tick()                                               # 1 of 3 read: the failed two get one more round
    assert col._pass is not None and sorted(col._pass.queue) == ["BBB", "CCC"]
    assert col._pass.retry_at == clk.t + _dt.timedelta(minutes=5)
    clk.advance(minutes=5, seconds=1)
    col.tick()                                               # still failing: finished, but not "done"
    p = _passes(db)[-1]
    assert p.finished is not None and (p.n_ok, p.n_failed) == (1, 2) and col._last_eod is None
    assert col.error_kind() == "http" and col._retry_at["chain"] - clk.t == _dt.timedelta(minutes=10)
    cl.fail.clear()
    clk.advance(minutes=10, seconds=1)
    col.tick()
    assert col._last_eod == DAY and _passes(db)[-1].n_ok == 3 and col.error_kind() is None


def test_zero_contract_pass_not_finished(db, build, caplog):
    caplog.set_level(logging.WARNING, logger="test_scr_collector")
    clk = Clock(EOD)
    col, cl = build(clk)
    col.tick()
    stamp = clk.t - _dt.timedelta(minutes=15)
    for sym in UNIVERSE:
        cl.chain_rows[sym] = [cl._row(sym, EXPIRIES[0], SPOTS[sym], "C", clk.t, stamp, oi=0, volume=0)]
    col.tick()
    p = _passes(db)[-1]
    assert (p.finished, p.n_ok, p.n_contracts) == (None, 3, 0)
    assert col._last_eod is None and col.error_kind() == "empty"
    assert col.last_error == ("eod pass %d read no option data: Massive returned no contracts (3 of 3 underlyings)"
                              % p.id)
    assert "3 underlyings listed by Massive returned no contracts (first: AAA, BBB, CCC)" in caplog.text
    assert _status(db)["last_error"] == col.last_error


def test_restore_ignores_finished_zero_contract_eod(db, build):
    clk = Clock(SAT)
    col, cl = build(clk)
    assert col.run_universe()["ok"]
    for n_ok, n_failed, n_contracts in ((3, 0, 0), (1, 2, 500)):    # read nothing; mostly failed
        pid = scr_store.start_pass(db, kind="eod", session="2026-10-09", n_symbols=3, now=SAT)
        scr_store.finish_pass(db, pid, n_ok=n_ok, n_failed=n_failed, n_contracts=n_contracts, finished=True,
                              now=SAT)
        n = len(cl.read())
        col2, _ = build(clk, client=cl)
        col2.tick()                                          # a restart: that EOD pass does not count as done
        assert len(cl.read()) == n + 3, (n_ok, n_contracts)
        p = _passes(db)[-1]
        assert (p.kind, p.session, p.n_ok) == ("eod", "2026-10-09", 3) and p.finished is not None
        assert col2._last_eod == "2026-10-09"


def test_empty_read_does_not_wipe_stored_rows(db, build):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    col.tick()
    assert len(_contracts(db, "AAA")) == KEPT
    stamp = clk.t - _dt.timedelta(minutes=15)
    for rows in ([], [cl._row("AAA", EXPIRIES[0], 100.0, "C", clk.t, stamp, oi=0, volume=0)]):
        cl.chain_rows["AAA"] = rows                          # an empty answer; one that keeps nothing
        clk.advance(minutes=30)
        col.tick()
        p = _passes(db)[-1]
        assert p.finished is not None and p.n_ok == 3
        assert len(_contracts(db, "AAA")) == KEPT and _und(db, "AAA")["n_contracts"] == KEPT


def test_all_adjusted_chain_clears_stored_rows(db, build):
    """A cash merger / delisting: the chain lists only ADJUSTED series (root AAA1). That
    answer is final - the old standard rows go, the underlying counts 0 contracts."""
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    col.tick()
    assert len(_contracts(db, "AAA")) == KEPT
    grid = cl.chain_snapshot("AAA")["rows"]
    cl.chain_rows["AAA"] = [dict(r, ticker=r["ticker"].replace("O:AAA", "O:AAA1", 1)) for r in grid]
    clk.advance(minutes=30)
    col.tick()
    p = _passes(db)[-1]
    assert p.finished is not None and (p.n_ok, p.n_failed) == (3, 0)
    assert _contracts(db, "AAA") == [] and _und(db, "AAA")["n_contracts"] == 0
    assert len(_contracts(db, "BBB")) == KEPT


def test_an_empty_answer_keeps_only_recent_rows(db, build):
    clk = Clock(RTH)                                         # Thursday: AAA's rows describe 2026-10-08
    col, cl = build(clk)
    col.tick()
    col.tick()
    cl.chain_rows["AAA"] = []
    clk.t = _dt.datetime(2026, 10, 12, 14, 0)                # Monday: 2 sessions on - still kept
    col.tick()
    assert len(_contracts(db, "AAA")) == KEPT
    clk.t = _dt.datetime(2026, 10, 13, 14, 0)                # Tuesday: older than the last 2 sessions
    col.tick()
    assert _contracts(db, "AAA") == [] and _und(db, "AAA")["n_contracts"] == 0
    assert len(_contracts(db, "BBB")) == KEPT


def test_index_chain_read_as_I_prefix_when_the_bare_symbol_is_empty(db, build):
    SPOTS["SPX"] = 5000.0
    try:
        clk = Clock(RTH)
        col, cl = build(clk)
        cl.universe["SPX"] = 9000                            # the contracts list names it bare
        cl.chain_rows["SPX"] = []
        col.tick()
        col.tick()                                           # SPX not typed yet: empty, nothing stored
        assert _contracts(db, "SPX") == [] and _und(db, "SPX")["sec_type"] == "index"
        stamp = clk.t - _dt.timedelta(minutes=15)
        cl.chain_rows["I:SPX"] = [cl._row("SPX", e, round(5000.0 * m, 2), r, clk.t, stamp)
                                  for e in EXPIRIES for m in MULTS for r in ("C", "P")]
        clk.advance(minutes=30)
        col.tick()
        assert len(_contracts(db, "SPX")) == len(EXPIRIES) * len(MULTS) * 2
        assert col._spelling == {"SPX": "I:SPX"}
        clk.advance(minutes=30)
        col.tick()
        assert len(cl.of("chain_snapshot", "SPX")) == 2 and len(cl.of("chain_snapshot", "I:SPX")) == 2
    finally:
        SPOTS.pop("SPX", None)


# -- the lanes and the one-off runs --

def test_history_403_one_symbol_marks_and_does_not_pause(db, build):
    clk = Clock(SAT)
    col, cl = build(clk, stock_years=0.3)
    cl.bars_end = "2026-10-09"
    cl.fail[("option_daily", "CCC")] = _e403("the option daily bars")
    _settle(col, clk, until=lambda c: (_und(db, "CCC").get("history_tries") or 0) >= 1 and not c._hist)
    assert _und(db, "AAA")["history_done"] is True
    c = _und(db, "CCC")
    assert c["history_done"] is False and c["history_tries"] == 1
    assert abs((c["history_next"] - clk.t).total_seconds() - 1800) < 1
    assert "history" not in col._alerts and col.shown_state != "error"


def test_history_403_before_any_read_worked_pauses_history(db, build):
    clk = Clock(SAT)
    col, cl = build(clk, stock_years=0.3)
    cl.bars_end = "2026-10-09"
    cl.fail[("option_daily", None)] = _e403("the option daily bars")
    _settle(col, clk, until=lambda c: "history" in c._alerts)
    assert col.error_kind() == "plan" and "IV-history reads paused" in col._shown()[1]
    assert _und(db, "AAA")["history_tries"] == 0              # nothing marked: the plan, not the symbol


def test_stock_window_never_requests_outside_plan_and_pending_reaches_zero(db, build, monkeypatch):
    assert scr_collector.stock_days(RTH)[0] >= massive.stocks_plan_start(clock.et_date(RTH)).isoformat()
    monkeypatch.setattr(massive, "STOCKS_PLAN_DAYS", 40)      # a narrow plan window, so 0.3 years crosses it
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.3)
    start = scr_collector.plan_start(RTH)
    assert start == clock.et_date(RTH) - _dt.timedelta(days=40)
    days = scr_collector.stock_days(RTH, 0.3)
    assert days[0] >= start.isoformat()
    _settle(col, clk)
    asked = {c[1] for c in cl.of("grouped_daily")}
    assert asked == set(days) and min(asked) >= start.isoformat()
    assert col._pending_days() == 0 and set(col._days) == set(days)
    col.job_fill("AAA")
    assert cl.of("stock_daily") and all(c[2]["start"] >= start.isoformat() for c in cl.of("stock_daily"))


def test_dated_403_retires_the_day(db, build, monkeypatch):
    monkeypatch.setattr(massive, "STOCKS_PLAN_DAYS", 40)
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.3)
    days = scr_collector.stock_days(RTH, 0.3)
    old, recent = days[0], days[-3]
    edge = (scr_collector.plan_start(RTH) + _dt.timedelta(days=7)).isoformat()
    assert old < edge <= recent
    for d in (old, recent):
        cl.fail[("grouped_daily", d)] = _e403("the grouped daily bars - Your plan doesn't include this data "
                                              "timeframe", window=True)
    _settle(col, clk, until=lambda c: bool(c._days) and c._bg is None and c._next_day(clk.t) is None)
    assert old in col._refused_days and old not in col._days
    assert len(cl.of("grouped_daily", old)) == 1                       # asked once, then retired
    assert col._day_retry[recent] - clk.t == _dt.timedelta(hours=1)
    assert "stocks" not in col._alerts and col.shown_state != "error" and col._pending_days() == 1
    cl.fail.clear()
    clk.advance(hours=1, seconds=1)
    _settle(col, clk)
    assert col._pending_days() == 0 and len(cl.of("grouped_daily", old)) == 1


def test_stock_403_not_about_the_date_before_any_day_pauses_the_lane(db, build):
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.05)
    cl.fail[("grouped_daily", None)] = _e403("the grouped daily bars")
    _settle(col, clk, until=lambda c: "stocks" in c._alerts)
    assert col.error_kind() == "plan" and "stock bars and reference reads paused" in col._shown()[1]
    assert not col._refused_days and not col._day_retry


def test_technicals_hourly_once_260_sessions_filed(db, build, monkeypatch):
    monkeypatch.setattr(scr_collector, "HISTORY_SESSIONS", 10)    # "the newest 260 sessions", scaled down
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.12)
    seen = []
    real = col.job_technicals

    def spy():
        seen.append(col._pending_days())
        return real()

    col.job_technicals = spy
    _settle(col, clk, until=lambda c: len(seen) == 1)
    assert seen[0] > 0 and col._recent_days_done()                 # while older days still file
    col.tick()
    assert len(seen) == 1 and col._tech_dirty                      # not again within the hour
    clk.advance(minutes=61)
    _settle(col, clk, until=lambda c: len(seen) == 2)
    assert seen[1] > 0                                             # an hour on, days still filing
    _settle(col, clk)
    assert seen[-1] == 0 and col._pending_days() == 0              # and once every day is on file


def test_technicals_failure_backs_off(db, build, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="test_scr_collector")
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.05)
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(scr_store, "recompute_technicals_many", boom)
    _settle(col, clk, until=lambda c: c._tech_retry_at is not None)
    assert len(calls) == 1 and col._tech_retry_at - clk.t == _dt.timedelta(minutes=10)
    for _ in range(5):
        col.tick()
    assert len(calls) == 1                                   # not every tick
    clk.advance(minutes=10, seconds=1)
    col.tick()
    assert len(calls) == 2
    tracebacks = [r for r in caplog.records if r.getMessage() == "lane job technicals failed"]
    assert len(tracebacks) == 1 and tracebacks[0].exc_info   # the traceback once
    assert "lane job technicals failed again (2 in a row): disk I/O error" in caplog.text


def test_restart_same_day_skips_earnings(db, build):
    clk = Clock(RTH)
    asked1, asked2 = [], []
    col, cl = build(clk, earnings_fetch=lambda d: asked1.append(d) or fake_earnings(d))
    _settle(col, clk)
    assert asked1 and col._earnings_on == DAY and _status(db)["earnings_on"] == DAY
    col2, _ = build(clk, client=cl, earnings_fetch=lambda d: asked2.append(d) or fake_earnings(d))
    for _ in range(5):
        col2.tick()
    # the day's read of the next 70 days is not repeated; only the past year's backfill (v4.143) may go on
    assert all(d.isoformat() < DAY for d in asked2) and col2._earnings_on == DAY


def test_rejected_key_fixed_in_env_file_is_used_after_5_min(db, build, monkeypatch, tmp_path):
    env_file = tmp_path / "app.env"
    monkeypatch.setattr(scr_collector, "APP_ENV_PATH", env_file)
    monkeypatch.setenv("TST_MASSIVE_API_KEY", "stale-process-key-9999")    # the file's key wins over it
    bad = "rejected-key-0000000000"
    env_file.write_text("TST_MASSIVE_API_KEY=%s\n" % bad, encoding="utf-8")
    clk = Clock(RTH)
    made: list[FakeMassive] = []

    def factory():                          # what default_client does, on a fake
        scr_collector._load_env()
        key = massive.api_key()
        c = FakeMassive(clk, key=bool(key))
        c._key = key
        if key and key != KEY:
            for name in ("option_underlyings", "chain_snapshot", "reference_tickers", "grouped_daily"):
                c.fail[(name, None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)
        made.append(c)
        return c

    col, _ = build(clk, client_factory=factory)
    assert col.tick() == "error" and col.error_kind() == "auth"
    assert len(made) == 1 and made[0]._key == bad
    clk.advance(minutes=5)
    col.tick()                                               # the same key: tried once more
    assert len(made) == 1 and len(made[0].of("option_underlyings")) == 2 and col.error_kind() == "auth"
    clk.advance(minutes=5)
    col.tick()                                               # still the same, within 30 min: held
    assert len(made[0].of("option_underlyings")) == 2 and col.error_kind() == "auth"
    env_file.write_text("TST_MASSIVE_API_KEY=%s\n" % KEY, encoding="utf-8")
    clk.advance(minutes=5)
    assert col.tick() != "error"                             # corrected: a fresh client, no restart
    assert len(made) == 2 and made[1]._key == KEY and made[1].of("option_underlyings")
    assert col.error_kind() is None and col._retired == []

    env_file.write_text("TST_MASSIVE_API_KEY=\n", encoding="utf-8")   # a blank placeholder ...
    col2, _ = build(clk, client_factory=factory)
    assert col2.tick() == "error" and col2.error_kind() == "config"
    assert col2.last_error == scr_collector.NO_KEY_TEXT and massive.api_key() is None
    env_file.write_text("TST_MASSIVE_API_KEY=%s\n" % KEY, encoding="utf-8")   # ... filled in later
    clk.advance(minutes=5)
    col2.tick()
    assert col2.error_kind() is None and made[-1]._key == KEY


def test_last_error_points_at_active_alert_after_clear(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk)
    col.tick()
    cl.fail[("chain_snapshot", None)] = _e403()
    col.tick()                                               # every chain refused: passes paused (plan)
    plan = col._alerts["chain"][1]
    assert col.last_error == plan
    col._alert("stocks", "plan", "stocks refused", 300, clk.t)
    assert col.last_error == "stocks refused"
    col._clear("stocks")
    assert col.last_error == plan                            # the alert still active
    col._bg_result("earnings", {"ok": False, "kind": "error", "error": "Nasdaq's earnings calendar returned nothing"})
    assert col.last_error == plan and col.last_failure == "earnings: Nasdaq's earnings calendar returned nothing"
    col.tick()
    assert _doc(tmp_path)["last_error"] == plan
    col._clear("chain")
    assert col.last_error is None                            # it was this alert's reason
    col._alert("history", "plan", "history refused", 300, clk.t)
    col.last_error = "a later failure"
    col._clear("history")
    assert col.last_error == "a later failure"               # not this alert's reason: kept


def test_progress_fields_in_state_file(db, build, tmp_path):
    clk = Clock(RTH)
    col, cl = build(clk, stock_years=0.05)
    cl.uni_page = 1
    cl.uni_fail[3] = _http()
    col.tick()                                               # the walk stops at page 3: 2 pages kept
    pr = _doc(tmp_path)["progress"]
    assert set(pr) == set(scr_collector.PROGRESS_KEYS)
    assert (pr["universe_pages"], pr["universe_symbols"], pr["universe_started"]) == (2, 2, _utc(RTH).isoformat())
    assert pr["pass_kind"] is None and pr["stock_days_pending"] is None
    clk.advance(seconds=61)
    col.tick()                                               # resumed and complete
    cl.fail[("chain_snapshot", "BBB")] = MassiveError("network", "could not reach Massive for the options chain "
                                                                 "snapshot (ConnectError: )")
    clk.advance(minutes=30)
    col.tick()                                               # AAA read, BBB failed (paused), CCC not read
    doc = _doc(tmp_path)
    pr = doc["progress"]
    assert (pr["pass_kind"], pr["pass_session"]) == ("cycle", DAY)
    assert pr["pass_pct"] == round(3000 / 4700 * 100, 1)      # weighted by the contracts of each underlying
    assert pr["pass_paused_at"] == _utc(clk.t).isoformat() and pr["pass_eta_s"] == 0
    assert col.detail.startswith("Reading the option market (live, 15-min delayed): 1 of 3 underlyings, 63% of "
                                 "contracts")
    assert (pr["universe_pages"], pr["universe_symbols"], pr["universe_started"]) == (None, None, None)
    pend = col._pending_days()
    assert pr["stock_days_pending"] == pend > 0 and pr["stock_eta_s"] == int(2 * pend * 60.5 / 5)
    assert (pr["history_left"], pr["history_waiting"], pr["history_eta_s"]) == (3, 0, None)
    st = _status(db)
    assert st["progress"] == pr and st["error_kind"] == doc["error_kind"] == "network"
    assert st["next_try"] == clk.t + _dt.timedelta(seconds=60)
    assert doc["universe_done"] is not None and st["universe_done"] is not None


def test_run_universe_failure_exit_2(db, build):
    clk = Clock(SAT)
    col, cl = build(clk)
    cl.fail[("option_underlyings", None)] = _http()
    res = col.run_universe()
    assert res["ok"] is False and res["error_kind"] == "http" and "HTTP 502" in res["error"]
    cl.fail.clear()
    assert col.run_universe()["ok"] is True
    cl.fail[("option_underlyings", None)] = _http()
    res = col.run_universe()                                 # a list on file: only a warning - still exit 2
    assert res["ok"] is False and "HTTP 502" in res["error"] and col.error_kind() is None
    cl.fail.clear()
    cl.universe = {}
    res = col.run_universe()
    assert res["ok"] is False and res["error_kind"] == "empty"
    cl.universe = dict(UNIVERSE)
    for sym in UNIVERSE:
        cl.chain_rows[sym] = []
    res = col.run_once()                                     # a pass that read no option data
    assert res["ok"] is False and res["contracts"] == 0 and res["read"] == 3


def test_run_history_skips_no_bars_exit_3(db, build, caplog):
    caplog.set_level(logging.WARNING, logger="test_scr_collector")
    clk = Clock(SAT)
    col, cl = build(clk, stock_years=0.3)
    assert col.run_universe()["ok"]
    res = col.run_history(["AAA", "BBB"])
    assert (res["ok"], res["nothing"], res["skipped"], res["done"], res["failed"]) == (True, True, 2, 0, 0)
    assert cl.of("option_daily") == [] and _und(db, "AAA")["history_tries"] == 0     # nothing read, nothing marked
    assert "IV history AAA skipped: 0 stored closes, need 20 - the stock bars are not loaded yet (" in caplog.text


# ───────────────────────────────────────── v4.138: IV history from Massive's day files ─────────────────────────────────────────

FLAT_IV = 0.35                                   # the fake day files price every option at 35 % (REST: 30, live: 40)
FLAT_N = 25                                      # the day-file window in these tests (252 on Hermes)
FRI = "2026-10-09"
MON = "2026-10-12"
S3_KEY_ID = "UNITTESTFLATKEYID0123"              # made-up Flat Files keys (not real)
S3_SECRET = "unit-test-flat-secret-Zq9vX2/abc+0123"


def _day_file(day: str, iv: float = FLAT_IV, syms=("AAA", "BBB", "CCC", "ZZZ")) -> bytes:
    """One options day file (Massive's ``day_aggs_v1`` CSV, gzip) priced by Black-Scholes
    at ``iv`` on the fake stocks' closes of that day: the 15-45 DTE monthlies, 5 strikes,
    calls and puts - plus noise the builder must ignore (a weekly expiry priced at 50 and
    an adjusted ``AAA1`` series)."""
    d = _dt.date.fromisoformat(day)
    lines = ["ticker,volume,open,close,high,low,window_start,transactions"]
    for e, dte in scr_flativ.monthly_window(d).values():
        for sym in syms:
            S = _close(sym, d)
            for m in MULTS:
                k = float(round(S * m))
                for right in ("C", "P"):
                    kind = "call" if right == "C" else "put"
                    p = round(black_scholes(S, k, dte / 365.0, RISK_FREE, iv, kind).price, 2)
                    if p < 0.01:
                        continue
                    t = massive.option_ticker(sym, e, right, k)
                    lines.append("%s,12,%s,%s,%s,%s,1760000000000000000,3" % (t, p, p, p, p))
                    if sym == "AAA" and m == 1.0:
                        lines.append("O:AAA1%s,5,9.99,9.99,9.99,9.99,1760000000000000000,1" % t[5:])
                        wk = massive.option_ticker(sym, e - _dt.timedelta(days=7), right, k)
                        lines.append("%s,40,50,50,50,50,1760000000000000000,9" % wk)
    return gzip.compress(("\n".join(lines) + "\n").encode("ascii"), mtime=0)


def _flat(tmp_path, clk, days, *, page=50, **kw):
    """A ``FlatFiles`` store on a fake S3 holding the day files of ``days`` (files kept
    under tmp_path/flatfiles; back-off sleeps skipped)."""
    s3 = FakeS3(page=page)
    for d in days:
        s3.put(flat_files.key_for(d), _day_file(d, **kw))
    return flat_files.FlatFiles(client=s3, root=tmp_path / "flatfiles", now=clk, sleep=lambda s: None), s3


def _ivs(db, sym, src=None) -> dict:
    db.expire_all()
    return {r.on: r.iv30 for r in db.query(ScrUnderlyingDaily).filter_by(symbol=sym)
            if r.iv30 is not None and (src is None or r.iv30_src == src)}


def _iv_days(window) -> list[str]:
    """The sessions an IV30 can come out of: a standard monthly expiry 15-45 days out
    (on 2026-10-02 and 10-05 Oct 16 is too near and Nov 20 too far - no IV, the REST way too)."""
    return [d for d in window if scr_flativ.monthly_window(d)]


def _quiet(c) -> bool:
    """Every stock day filed and no job left in any lane (the day-file one included)."""
    return (bool(c._days) and c._pending_days() == 0 and c._bg is None and not c._hist
            and c._flat_future is None and c._earnings_on is not None)


def _flat_built(c) -> bool:
    return _quiet(c) and c._flat_state == "ready"


def test_flat_window_and_when_a_day_file_is_due():
    # a day's file is out ~11:00 ET the next (calendar) day: asked for from 11:15 ET
    sat_10 = _dt.datetime(2026, 10, 10, 14, 0)                   # Saturday 10:00 ET
    assert scr_collector.flat_newest_day(sat_10).isoformat() == "2026-10-08"
    assert scr_collector.flat_newest_day(SAT).isoformat() == FRI     # Saturday 12:00 ET
    assert scr_collector.flat_newest_day(_dt.datetime(2026, 10, 12, 14, 0)).isoformat() == FRI   # Mon 10:00
    assert scr_collector.flat_newest_day(_dt.datetime(2026, 10, 13, 15, 14)).isoformat() == FRI  # Tue 11:14
    assert scr_collector.flat_newest_day(_dt.datetime(2026, 10, 13, 15, 15)).isoformat() == MON  # Tue 11:15
    w = scr_collector.flat_days(SAT, FLAT_N)
    assert len(w) == FLAT_N and w[-1] == FRI and w == sorted(w)
    assert all(clock.is_trading_day(d) for d in w)
    # the default window is every session Massive still serves: ~2 years back to plan_start (v4.138)
    full = scr_collector.flat_days(SAT)
    assert scr_collector.FLAT_SESSIONS is None and "flat_state" in scr_collector.PROGRESS_KEYS
    assert full[-1] == FRI and full == sorted(full) and all(clock.is_trading_day(d) for d in full)
    assert full[0] >= scr_collector.plan_start(SAT).isoformat()
    assert clock.prev_trading_day(_dt.date.fromisoformat(full[0])) < scr_collector.plan_start(SAT)
    assert 480 <= len(full) <= 510
    for k in ("flat_days_done", "flat_days_total", "flat_eta_s", "flat_state", "flat_error", "flat_kind"):
        assert k in scr_collector.PROGRESS_KEYS


def test_flat_lane_builds_the_iv_history_from_the_day_files(db, build, tmp_path, monkeypatch):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    recomputes = []
    real_many = scr_store.recompute_iv_many
    monkeypatch.setattr(scr_store, "recompute_iv_many",
                        lambda db_, syms, **kw: recomputes.append(len(syms)) or real_many(db_, syms, **kw))
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=_flat_built)
    # the stocks lane hands the days over one at a time: the IV figures are not redone after each
    assert len(recomputes) <= 3 and recomputes[-1] == 3, recomputes
    ivd = _iv_days(window)
    assert len(ivd) == FLAT_N - 2 and FRI in ivd
    for sym in ("AAA", "BBB", "CCC"):
        flat = _ivs(db, sym, "flat")
        assert sorted(flat) == [d for d in ivd if d != FRI], sym     # every session but Friday ...
        assert all(abs(v - 35.0) < 0.5 for v in flat.values()), (sym, flat)
        live = _ivs(db, sym, "live")
        assert list(live) == [FRI] and live[FRI] == pytest.approx(40.0, abs=0.05)   # ... the EOD reading wins
        u = _und(db, sym)
        assert u["history_done"] is True and u["iv_n"] == len(ivd) and u["iv_pct"] is not None   # (rank: 60+)
    assert _ivs(db, "ZZZ") == {}                                 # not in the universe: never filed
    assert cl.of("option_daily") == []                           # the REST history lane read nothing
    gets = s3.ops("get")
    assert len(gets) == FLAT_N and gets[0][1] == flat_files.key_for(FRI)    # each file once, newest first
    assert all((tmp_path / "flatfiles").joinpath(*flat_files.key_for(d).split("/")).is_file() for d in window)
    doc = _doc(tmp_path)
    pr = doc["progress"]
    assert (pr["flat_state"], pr["flat_days_done"], pr["flat_days_total"]) == ("ready", FLAT_N, FLAT_N)
    assert pr["flat_error"] is None and pr["flat_eta_s"] is None and doc["warn"] is None
    assert col.flat_status()["mapped"] == 3 and col._flat_check_at is not None
    # nothing to do: an hour on, the lane only looks (no download, no new filing)
    n = len(s3.calls)
    clk.advance(minutes=61)
    col.tick()
    assert len(s3.ops("get")) == FLAT_N and len(s3.calls) == n


def test_flat_detail_and_progress_while_a_job_runs(db, build, tmp_path):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, _ = _flat(tmp_path, clk, window)
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: len(c._flat_days) >= 5)
    col._flat_future = scr_collector.cf.Future()                   # a job in flight, 5 of 6 days filed
    col._flat_prog = {"phase": "build", "t0": scr_collector.time.monotonic() - 50.0, "n": 5, "left": 1, "todo": 6,
                      "rescan": 0, "days": 6, "i": 5}
    try:
        col._set_state(clk.t)
        done, total = col._flat_counts(col._flat_window)
        assert col.detail.endswith("Building IV history from Massive's daily files: %d of %d days, about 1 min left."
                                   % (done, total))
        pr = col._progress(clk.t)
        assert pr["flat_days_done"] == done and 5 <= pr["flat_eta_s"] <= 15
        col._flat_prog = dict(col._flat_prog, todo=0, rescan=12, days=24, i=7)
        assert col._flat_text(clk.t) == ("Reading Massive's stored daily files for 12 underlyings still without "
                                         "an IV history: day 7 of 24.")
        col._flat_prog = {"phase": "check"}
        assert col._flat_text(clk.t) == "Checking Massive's daily option files for re-published days."
    finally:
        col._flat_future.set_result({"ok": True})
        col._flat_future = None


def test_flat_lane_on_real_threads(db, build, tmp_path):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    col, cl = build(clk, executor_factory=None, workers=3, stock_years=0.12, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    for _ in range(300):
        col.wait_idle(30)
        col.tick()
        if _flat_built(col) and all(_und(db, s).get("history_done") for s in ("AAA", "BBB", "CCC")):
            break
    else:
        raise AssertionError("the day-file lane did not finish on real threads: %s" % col.flat_status())
    assert col._flatpool is not None and col._flat_future is None
    assert sorted(_ivs(db, "BBB", "flat")) == [d for d in _iv_days(window) if d != FRI]
    assert cl.of("option_daily") == [] and len(s3.ops("get")) == FLAT_N


def test_rest_lane_takes_only_what_the_day_files_could_not_fill(db, build, tmp_path):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, _ = _flat(tmp_path, clk, window, syms=("AAA", "BBB", "ZZZ"))      # CCC has no options in the files
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: _flat_built(c) and bool(_und(db, "CCC").get("history_done")))
    assert {c[1] for c in cl.of("option_daily")} == {"CCC"}        # the REST lane read CCC only
    assert _und(db, "AAA")["history_done"] and _und(db, "BBB")["history_done"]
    rest = _ivs(db, "CCC", "rest")
    assert len(rest) >= 20 and all(abs(v - 30.0) < 1.0 for v in rest.values())
    assert _ivs(db, "CCC", "flat") == {} and "CCC" in col._flat_tried


def test_rest_lane_waits_while_the_day_files_are_being_filed(db, build, tmp_path):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, _ = _flat(tmp_path, clk, window, syms=("AAA", "BBB"))
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: c._flat_mapped is not None)
    assert col._flat_working() and not col._rest_may("CCC")       # mapped, files not read for it yet
    col._flat_unmapped.add("XYZ1")
    assert col._rest_may("XYZ1") and not col._rest_may("NEWSYM")  # no option root / newer than the map
    col._flat_state = "error"
    assert not col._flat_working()                                 # refused: the REST lane takes everything


def test_flat_daily_top_up_after_1115_et_and_a_missing_file_is_not_yet(db, build, tmp_path):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=_flat_built)
    # Tuesday 10:00 ET: Monday's closes are filed, but its option file is not due before 11:15 ET
    cl.bars_end = MON
    clk.t = _dt.datetime(2026, 10, 13, 14, 0)
    _settle(col, clk, until=lambda c: c._days.get(MON, [False, False])[1] and c._bg is None
            and c._flat_future is None)
    col.tick()
    assert col._flat_window[-1] == FRI and MON not in col._flat_days
    assert not [c for c in s3.calls if MON in str(c[1:])]
    # 11:30 ET: Monday is asked for - Massive has not published it yet: "not yet", again in 1 h
    clk.t = _dt.datetime(2026, 10, 13, 15, 30)
    col.tick()
    assert col._flat_window[-1] == MON and col._flat_window[0] == window[1]      # the window moved on a day
    assert MON not in col._flat_days and col._flat_wait[MON] == clk.t + _dt.timedelta(hours=1)
    assert col.flat_status()["state"] == "ready" and _doc(tmp_path)["progress"]["flat_error"] is None
    s3.put(flat_files.key_for(MON), _day_file(MON))
    clk.advance(minutes=30)
    col.tick()
    assert MON not in col._flat_days                                # not asked for again before the hour
    clk.advance(minutes=31)
    col.tick()
    assert MON in col._flat_days
    assert _ivs(db, "AAA", "flat")[MON] == pytest.approx(35.0, abs=0.5)
    pr = _doc(tmp_path)["progress"]
    assert pr["flat_days_done"] == pr["flat_days_total"] == FLAT_N


def test_a_republished_day_file_is_filed_again(db, build, tmp_path):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=_flat_built)
    day, other = _iv_days(window)[-4], _iv_days(window)[-5]
    s3.put(flat_files.key_for(day), _day_file(day, iv=0.45))       # Massive re-published that day
    clk.advance(days=3)
    col.tick()
    assert _ivs(db, "AAA", "flat")[day] == pytest.approx(35.0, abs=0.5)      # not checked before a week
    clk.advance(days=4, minutes=1)
    col.tick()
    assert _ivs(db, "AAA", "flat")[day] == pytest.approx(45.0, abs=0.5)      # the weekly check re-filed it
    assert _ivs(db, "AAA", "flat")[other] == pytest.approx(35.0, abs=0.5)
    assert len([c for c in s3.ops("get") if c[1] == flat_files.key_for(day)]) == 2
    assert col._flat_check_at == clk.t and day in col._flat_days


@pytest.mark.parametrize("code,kind", [("InvalidAccessKeyId", "auth"), ("AccessDenied", "plan")])
def test_refused_day_files_warn_and_the_rest_lane_builds_the_history(db, build, tmp_path, monkeypatch, caplog,
                                                                     code, kind):
    monkeypatch.setenv("TST_MASSIVE_S3_KEY_ID", S3_KEY_ID)
    monkeypatch.setenv("TST_MASSIVE_S3_SECRET", S3_SECRET)
    caplog.set_level(logging.DEBUG)
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    s3.fail["list_objects_v2"] = [ClientError(code, 403, "key %s / %s refused" % (S3_KEY_ID, S3_SECRET))
                                  for _ in range(200)]
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: _quiet(c) and bool(_und(db, "BBB").get("history_done"))
            and bool(_und(db, "AAA").get("history_done")))
    assert (col._flat_state, col._flat_kind) == ("error", kind) and not col._flat_working()
    assert col._flat_retry_at == col._flat_job_at + _dt.timedelta(minutes=30)
    assert {"AAA", "BBB"} <= {c[1] for c in cl.of("option_daily")}           # the REST lane took over
    assert _ivs(db, "AAA", "flat") == {} and len(_ivs(db, "AAA", "rest")) >= 20
    doc = _doc(tmp_path)
    pr = doc["progress"]
    assert pr["flat_state"] == "error" and "Flat Files" in pr["flat_error"]
    assert doc["state"] != "error"                                  # a warning, never the collector's error
    text = (tmp_path / "state" / "screener_collector.json").read_text(encoding="utf-8") + caplog.text
    assert S3_SECRET not in text and S3_KEY_ID not in text and S3_SECRET not in json.dumps(_status(db), default=str)
    got = _tray_function(tmp_path / "state" / "screener_collector.json")(now=_utc(clk.t))
    assert (got["color"], got["level"], got["tip"]) == ("amber", "warn", "Scr WARN")
    assert got["line"].startswith("Options screener: WARN - IV history from Massive's daily files is paused: ")
    # 30 min later it is tried again; once the files answer, the lane builds and the warning goes
    s3.fail.clear()
    clk.t = col._flat_retry_at
    _settle(col, clk, until=_flat_built)
    assert col._flat_error is None and len(_ivs(db, "AAA", "flat")) >= 1
    assert _doc(tmp_path)["progress"]["flat_state"] == "ready"


def _flat_page(tmp_path, clk, admin: bool = False) -> dict:
    """The /options status (``options_page._status_view``) for the state file as written."""
    from app.routes import options_page  # noqa: PLC0415

    doc = _doc(tmp_path)
    doc["heartbeat"] = _utc(clk.t).isoformat()
    return options_page._status_view({"available": True, "status": doc, "source": "file"}, None,
                                     now=_utc(clk.t), is_admin=admin)


@pytest.mark.parametrize("err,needle", [
    (ClientError("NoSuchBucket", 404, "The specified bucket does not exist"),
     "Massive Flat Files has no bucket 'flatfiles' - check TST_MASSIVE_S3_BUCKET in app\\.env, default flatfiles"),
    (ClientError("404", 404, "Not Found"),                       # e.g. the endpoint set to the API host
     "Massive Flat Files has no file list at https://files.massive.com, bucket 'flatfiles' - check "
     "TST_MASSIVE_S3_ENDPOINT and TST_MASSIVE_S3_BUCKET in app\\.env"),
], ids=["no-bucket", "list-404"])
def test_flat_setup_error_turns_the_lane_off_with_a_warning(db, build, tmp_path, monkeypatch, caplog, err, needle):
    """Keys set but the lane cannot run on this PC (config kind - here the bucket / the
    endpoint): the lane is off WITH the reason, the REST lane builds the history, the page
    and the tray turn amber with the reason (the fix first), and the reason is KEPT across
    the 5-minute looks until a job works - no green flicker in between."""
    for var in ("TST_MASSIVE_S3_ENDPOINT", "TST_MASSIVE_S3_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    caplog.set_level(logging.INFO, logger="test_scr_collector")
    from app.routes import options_page  # noqa: PLC0415

    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    _, s3 = _flat(tmp_path, clk, window)
    s3.fail["list_objects_v2"] = [err for _ in range(10000)]
    looks = []

    def factory():                                   # a store the collector owns (dropped when it fails)
        looks.append(1)
        return flat_files.FlatFiles(client=s3, root=tmp_path / "flatfiles", now=clk, sleep=lambda s: None)

    col, cl = build(clk, stock_years=0.3, flat_factory=factory, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: _quiet(c) and bool(_und(db, "AAA").get("history_done")))
    assert (col._flat_state, col._flat_kind) == ("off", "config") and needle in col._flat_error
    assert not col._flat_working() and col._flat is None
    assert cl.of("option_daily")                                     # the REST lane as before
    doc = _doc(tmp_path)
    pr = doc["progress"]
    assert (pr["flat_state"], pr["flat_kind"]) == ("off", "config") and needle in pr["flat_error"]
    assert doc["warn"] is None                                       # (the lane's warning, not the collector's)
    got = _tray_function(tmp_path / "state" / "screener_collector.json")(now=_utc(clk.t))
    assert (got["color"], got["level"], got["tip"]) == ("amber", "warn", "Scr WARN")
    assert got["line"].startswith("Options screener: WARN - IV history from Massive's daily files is not running: "
                                  + needle)
    m, a = _flat_page(tmp_path, clk), _flat_page(tmp_path, clk, admin=True)
    assert m["dot"] == a["dot"] == "amber" and options_page._FLAT_SETUP in m["label"]
    assert "Admin" not in m["label"] and "TST_MASSIVE_S3" not in m["label"]
    assert options_page._FLAT_SETUP_ADMIN + " Details: " + needle in a["label"]
    # the next look builds the store again: the lane stays off with its reason until a job works
    assert col._flat_get(clk.t) is not None and len(looks) == 2
    assert (col._flat_state, col._flat_kind) == ("off", "config") and needle in col._flat_error
    pr = col._progress(clk.t)
    assert pr["flat_state"] == "off" and needle in pr["flat_error"]
    assert caplog.text.count("Massive's daily option files are set up") == 1     # (again: only with --verbose)
    # fixed on Hermes: the next try works, the warning goes
    s3.fail.clear()
    clk.advance(minutes=5, seconds=1)
    _settle(col, clk, until=_flat_built)
    assert col._flat_error is None and col._flat_kind is None and col._flat_working()
    assert "Massive's daily files work again" in caplog.text
    got = _tray_function(tmp_path / "state" / "screener_collector.json")(now=_utc(clk.t))
    assert got["color"] == "green" and _flat_page(tmp_path, clk, admin=True)["dot"] == "green"


@pytest.mark.parametrize("exc,kind", [(ClientError("Conflict", 409, "odd"), "http"),
                                      (RuntimeError("boom in the day-file lane"), "error"),
                                      (EndpointConnectionError("Could not connect to the endpoint URL"), "network")],
                         ids=["http", "bug", "network"])
def test_a_lasting_flat_failure_hands_over_to_the_rest_lane_and_warns(db, build, tmp_path, exc, kind):
    """A failure that is not about the keys, the plan or the setup (an HTTP error, a bug,
    Massive unreachable) is retried - the REST lane waits and the page / tray stay green -
    but after FLAT_FAIL_GIVEUP failed jobs in a row the lane is in "error": the REST lane
    takes every symbol, the page and the tray turn amber with words that fit the cause;
    once a job works the counters reset and the lane takes over again."""
    from app.routes import options_page  # noqa: PLC0415

    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    s3.fail["list_objects_v2"] = [exc for _ in range(100000)]
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: c._flat_state == "retry")
    assert (col._flat_kind, col._flat_fails) == (kind, 1) and col._flat_working()
    assert not {"AAA", "BBB"} & {c[1] for c in cl.of("option_daily")}           # a short retry: the REST lane waits
    assert _flat_page(tmp_path, clk, admin=True)["dot"] == "green"
    assert "(retrying)" in _tray_function(tmp_path / "state" / "screener_collector.json")(now=_utc(clk.t))["line"]
    for n in range(2, scr_collector.FLAT_FAIL_GIVEUP + 1):
        clk.t = col._flat_retry_at
        _settle(col, clk, until=lambda c, n=n: c._flat_fails == n)
        assert col._flat_working() is (n < scr_collector.FLAT_FAIL_GIVEUP)
        assert col._flat_state == ("error" if n == scr_collector.FLAT_FAIL_GIVEUP else "retry")
    wait = col._flat_retry_at - clk.t
    assert wait == (_dt.timedelta(seconds=960) if kind == "network" else _dt.timedelta(minutes=30))
    _settle(col, clk, until=lambda c: _quiet(c) and all(_und(db, s).get("history_done") for s in ("AAA", "BBB")))
    assert {"AAA", "BBB"} <= {c[1] for c in cl.of("option_daily")}            # the REST lane took over
    pr = _doc(tmp_path)["progress"]
    assert (pr["flat_state"], pr["flat_kind"]) == ("error", kind) and pr["flat_error"]
    m, a = _flat_page(tmp_path, clk), _flat_page(tmp_path, clk, admin=True)
    assert m["dot"] == a["dot"] == "amber" and options_page._FLAT_FAILING in m["label"]
    assert options_page._FLAT_PAUSED not in m["label"] and "Admin" not in m["label"]
    hint = options_page._FLAT_BUG_ADMIN if kind == "error" else options_page._FLAT_FAIL_ADMIN
    assert hint + " Details: " in a["label"]
    assert "building from daily files" not in m["line"]                        # the REST progress shows instead
    got = _tray_function(tmp_path / "state" / "screener_collector.json")(now=_utc(clk.t))
    assert (got["color"], got["tip"]) == ("amber", "Scr WARN")
    assert got["line"].startswith("Options screener: WARN - IV history from Massive's daily files keeps failing ")
    # the files answer again: the lane builds, the counters reset, green again
    s3.fail.clear()
    clk.t = col._flat_retry_at
    _settle(col, clk, until=_flat_built)
    assert (col._flat_fails, col._flat_net_fails, col._flat_error) == (0, 0, None) and col._flat_working()
    assert _flat_page(tmp_path, clk)["dot"] == "green"


def test_a_lasting_flat_failure_is_logged_at_most_every_30_min(db, build, tmp_path, caplog):
    """The job's exception (ERROR, with its traceback) and the job's result (WARNING) each
    keep their own throttle: one failure lasting an hour writes a handful of lines, not a
    pair every 5 minutes."""
    caplog.set_level(logging.INFO, logger="test_scr_collector")
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    s3.fail["list_objects_v2"] = [RuntimeError("boom in the day-file lane") for _ in range(100000)]
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: c._flat_state == "retry")
    t_end = clk.t + _dt.timedelta(minutes=65)
    while clk.t < t_end:
        clk.advance(minutes=1)
        col.tick()
    recs = [r for r in caplog.records if r.name == "test_scr_collector"]
    job = [r for r in recs if "IV history (day files): the job failed" in r.getMessage()]
    res = [r for r in recs if "the daily-file job failed" in r.getMessage() or "keep failing" in r.getMessage()]
    assert 1 <= len(job) <= 2 and all(r.levelno == logging.ERROR and r.exc_info for r in job)
    assert 2 <= len(res) <= 3, [r.getMessage() for r in res]        # (a retry, then the hand-over)
    assert col._flat_state == "error" and col._flat_fails >= scr_collector.FLAT_FAIL_GIVEUP


@pytest.mark.parametrize("where", ["bad-data", "bad-crc", "massive"])
def test_a_damaged_day_file_fails_only_that_day_and_is_downloaded_again(db, build, tmp_path, caplog, where):
    """A day file that cannot be read (damaged on this PC with its size unchanged - bad
    gzip data or a wrong CRC - or sent damaged by Massive) fails that day only: the days
    after it are still filed, a damaged local copy is set aside as .bad (never deleted),
    and an hour later the day is downloaded again and filed. The lane never stalls."""
    caplog.set_level(logging.INFO, logger="test_scr_collector")
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    bad_day = _iv_days(window)[-4]
    good = s3.objects[flat_files.key_for(bad_day)]["data"]
    if where == "massive":
        broken = bytearray(good)
        broken[20] ^= 0xFF
        s3.put(flat_files.key_for(bad_day), bytes(broken))
    else:
        p = store.ensure(bad_day)
        broken = bytearray(p.read_bytes())
        broken[20 if where == "bad-data" else -6] ^= 0xFF
        p.write_bytes(bytes(broken))
        with pytest.raises(__import__("zlib").error if where == "bad-data" else gzip.BadGzipFile):
            list(scr_flativ.read_day(p))
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: _quiet(c) and len(c._flat_days) == FLAT_N - 1 and bad_day in c._flat_wait)
    assert bad_day not in col._flat_days and col._flat_fails == 0 and col._flat_state not in ("error", "retry")
    assert all(d in col._flat_days for d in window if d != bad_day)          # the older days too
    assert "the daily file of %s failed" % bad_day in caplog.text
    path = store.local_path(bad_day)
    if where != "massive":
        assert path.with_name(path.name + ".bad").read_bytes() == bytes(broken)
        assert not path.exists() and flat_files.key_for(bad_day) not in store.manifest()
    else:
        assert not path.exists() and "damaged" in caplog.text
        s3.put(flat_files.key_for(bad_day), good)                            # Massive fixed its copy
    clk.advance(hours=1, minutes=1)
    _settle(col, clk, until=lambda c: _quiet(c) and bad_day in c._flat_days)
    assert path.read_bytes() == good and _ivs(db, "AAA", "flat")[bad_day] == pytest.approx(35.0, abs=0.5)
    pr = _doc(tmp_path)["progress"]
    assert pr["flat_days_done"] == pr["flat_days_total"] == FLAT_N and pr["flat_state"] == "ready"


def test_a_running_job_past_its_file_list_shows_as_building(db, build, tmp_path):
    """A lane marked off / refused / retrying whose running job got past its file list:
    the files answer again, so the page and the tray see "building" (no stale warning for
    the length of a long build); before the list answers the warning stays. The state
    itself is settled by the job's result."""
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, _ = _flat(tmp_path, clk, window)
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: len(c._flat_days) >= 5)
    col._flat_future = scr_collector.cf.Future()
    try:
        for state, kind in (("off", "config"), ("error", "auth"), ("retry", "network")):
            col._flat_state, col._flat_kind, col._flat_error = state, kind, "why it failed"
            col._flat_prog = {"phase": "start", "t0": scr_collector.time.monotonic(), "n": 0, "left": 4, "todo": 4,
                              "rescan": 0}
            pr = col._progress(clk.t)
            assert (pr["flat_state"], pr["flat_kind"], pr["flat_error"]) == (state, kind, "why it failed")
            col._flat_prog = dict(col._flat_prog, phase="build", days=4, i=1, n=1, left=3)
            pr = col._progress(clk.t)
            assert (pr["flat_state"], pr["flat_kind"], pr["flat_error"]) == ("building", None, None)
            assert col._flat_state == state
    finally:
        col._flat_future.set_result({"ok": True})
        col._flat_future = None
        col._flat_prog = None


def test_run_flat_iv_reports_building_while_it_files(db, build, tmp_path, monkeypatch):
    """The --flat-iv one-off sets the lane's state like the loop does: a heartbeat in the
    middle of the run says "building" - never "waiting for stock prices"."""
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, _ = _flat(tmp_path, clk, window)
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    assert col.run_universe()["ok"]
    cl.bars_end = FRI
    for d in window:
        col.job_stock_day(d)
    seen = []
    real = scr_store.file_iv_day

    def hook(db_, d, vals, **kw):
        n = real(db_, d, vals, **kw)
        if len(seen) < 6:
            col._beat(None)                                          # what a heartbeat writes now
            tray = _tray_function(tmp_path / "state" / "screener_collector.json")(now=_utc(clk.t))
            seen.append((_doc(tmp_path)["progress"]["flat_state"], tray["line"]))
        return n

    monkeypatch.setattr(scr_store, "file_iv_day", hook)
    res = col.run_flat_iv()
    assert res["ok"] and res["filed"] == FLAT_N and seen
    for state, line in seen:
        assert state == "building" and "IV files " in line and "(waiting for stock prices)" not in line, line


def test_no_keys_the_lane_is_off_silently_and_looked_at_every_5_min(db, build, tmp_path, monkeypatch):
    env_file = tmp_path / "app.env"
    env_file.write_text("TST_MASSIVE_API_KEY=%s\n" % KEY, encoding="utf-8")
    monkeypatch.setattr(scr_collector, "APP_ENV_PATH", env_file)
    for var in ("TST_MASSIVE_S3_KEY_ID", "TST_MASSIVE_S3_SECRET"):
        monkeypatch.setenv(var, "")                                  # (restored after: _load_env writes os.environ)
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    looks = []

    def factory():
        looks.append(1)
        return scr_collector.default_flat_files()

    clk = Clock(SAT)
    col, cl = build(clk, stock_years=0.3, flat_factory=factory, flat_sessions=FLAT_N)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: _quiet(c) and bool(_und(db, "AAA").get("history_done")))
    assert (col._flat_state, col._flat_error, col._flat) == ("off", None, None)
    assert cl.of("option_daily") and len(looks) == 1                 # the REST lane as before; one look
    doc = _doc(tmp_path)
    assert doc["progress"]["flat_state"] == "off" and doc["progress"]["flat_days_total"] is None
    got = _tray_function(tmp_path / "state" / "screener_collector.json")(now=_utc(clk.t))
    assert got["color"] == "green" and "IV files" not in got["line"]
    # the keys are added to app/.env: picked up at the next 5-minute look, no restart
    env_file.write_text("TST_MASSIVE_API_KEY=%s\nTST_MASSIVE_S3_KEY_ID=%s\nTST_MASSIVE_S3_SECRET=%s\n"
                        % (KEY, S3_KEY_ID, S3_SECRET), encoding="utf-8")
    clk.advance(minutes=4)
    col.tick()
    assert len(looks) == 1
    real = flat_files.FlatFiles
    s3 = FakeS3()
    monkeypatch.setattr(flat_files, "FlatFiles", lambda log=None: real(client=s3, root=tmp_path / "ff", now=clk,
                                                                       sleep=lambda s: None))
    clk.advance(minutes=1, seconds=1)
    col.tick()
    assert len(looks) == 2 and col._flat is not None and col._flat_own
    assert flat_files.configured() and col._flat_state in ("waiting", "building", "ready")
    assert S3_SECRET not in repr(real(client=s3, root=tmp_path / "ff"))


def test_flat_network_errors_back_off_and_hand_over_after_5(db, build, tmp_path):
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, 5)
    store, s3 = _flat(tmp_path, clk, window)
    s3.fail["list_objects_v2"] = [EndpointConnectionError("Could not connect to the endpoint URL")
                                  for _ in range(1000)]
    col, cl = build(clk, stock_years=0.05, flat_store=store, flat_sessions=5)
    cl.bars_end = FRI
    _settle(col, clk, until=lambda c: c._flat_state == "retry")
    assert col._flat_retry_at - clk.t == _dt.timedelta(seconds=60) and col._flat_working()
    assert "could not reach Massive Flat Files" in _doc(tmp_path)["progress"]["flat_error"]
    for n, wait in ((2, 120), (3, 240), (4, 480), (5, 960)):
        clk.t = col._flat_retry_at
        _settle(col, clk, until=lambda c, n=n: c._flat_net_fails == n)
        assert col._flat_retry_at - clk.t == _dt.timedelta(seconds=wait)
        assert col._flat_working() is (n < scr_collector.FLAT_NET_GIVEUP)
    s3.fail.clear()
    clk.t = col._flat_retry_at
    _settle(col, clk, until=lambda c: c._flat_state == "ready" and c._flat_future is None)
    assert col._flat_net_fails == 0 and col._flat_working() and col._flat_error is None


def test_a_new_underlying_gets_its_history_from_the_stored_files(db, build, tmp_path):
    SPOTS["DDD"] = 30.0
    try:
        clk = Clock(SAT)
        window = scr_collector.flat_days(SAT, FLAT_N)
        store, s3 = _flat(tmp_path, clk, window, syms=("AAA", "BBB", "CCC", "DDD"))   # DDD trades, not listed yet
        col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
        cl.bars_end = FRI
        _settle(col, clk, until=_flat_built)
        assert _ivs(db, "DDD") == {} and len(s3.ops("get")) == FLAT_N
        cl.universe["DDD"] = 50                                    # Sunday 07:35 ET: the daily refresh lists it
        clk.t = _dt.datetime(2026, 10, 11, 11, 35)
        _settle(col, clk, until=lambda c: len(cl.of("stock_daily", "DDD")) == 2 and c._bg is None
                and bool(_und(db, "DDD").get("history_done")), step_s=60)
        flat = _ivs(db, "DDD", "flat")
        assert sorted(flat) == _iv_days(window) and all(abs(v - 35.0) < 0.5 for v in flat.values())
        assert cl.of("option_daily", "DDD") == []                 # no REST reads: the stored files had it
        assert len(s3.ops("get")) == FLAT_N                        # and nothing was downloaded again
    finally:
        SPOTS.pop("DDD", None)


def test_run_flat_iv_one_off(db, build, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="test_scr_collector")
    clk = Clock(SAT)
    window = scr_collector.flat_days(SAT, FLAT_N)
    store, s3 = _flat(tmp_path, clk, window)
    col, cl = build(clk, stock_years=0.3, flat_store=store, flat_sessions=FLAT_N)
    assert col.run_universe()["ok"]
    res = col.run_flat_iv()                                      # no stock prices yet: nothing to file
    assert res["ok"] and res["nothing"] and res["days_done"] == 0 and res["waiting"] == FLAT_N
    cl.bars_end = FRI
    for d in window:
        col.job_stock_day(d)
    res = col.run_flat_iv()
    assert res["ok"] and not res["nothing"] and (res["days_done"], res["days_total"], res["filed"]) == \
        (FLAT_N, FLAT_N, FLAT_N)
    assert res["marked"] == 3 and res["readings"] == 3 * len(_iv_days(window)) and res["waiting"] == 0
    assert _und(db, "AAA")["history_done"] and cl.of("option_daily") == []
    assert "IV history: %s filed from Massive's daily file" % FRI in caplog.text           # progress lines
    assert "IV history: %s filed from Massive's daily file" % window[-10] in caplog.text
    assert "of %d days filed" % FLAT_N in caplog.text
    again = col.run_flat_iv()
    assert again["ok"] and again["filed"] == 0 and again["days_done"] == FLAT_N and not again["nothing"]
    assert len(s3.ops("get")) == FLAT_N
    # refused: exit 2 (ok False with the kind)
    s3.fail["list_objects_v2"] = [ClientError("SignatureDoesNotMatch", 403, "bad") for _ in range(50)]
    res = col.run_flat_iv()                                      # (the weekly check lists the months first)
    assert res["ok"] is False and res["error_kind"] == "auth" and "Flat Files keys" in res["error"]


def test_run_flat_iv_without_keys(db, build):
    clk = Clock(SAT)
    col, cl = build(clk)
    res = col.run_flat_iv()
    assert res["ok"] is False and res["error_kind"] == "config" and res["nothing"]
    assert "TST_MASSIVE_S3_KEY_ID" in res["error"] and "TST_MASSIVE_S3_SECRET" in res["error"]
    assert cl.calls == []                                       # no Massive REST request either


# ───────────────────────────────────────── the tray ─────────────────────────────────────────

_TRAY_FUNCS = ("get_screener_collector_status", "_screener_log_error", "screener_toast",
               "get_option_calc_status", "option_calc_toast", "_option_calc_et")
_TRAY_CONSTS = ("OPTIONS_SCREENER_STALE_SEC", "OPTIONS_SCREENER_DETAIL_MAX", "OPTIONS_SCREENER_LOG_TAIL",
                "OPTION_CALC_STALE_SEC", "OPTION_CALC_DETAIL_MAX")


def _tray_ns(state_path, log_path=None) -> dict:
    """The screener and option-calculator parts of tray_status.py - their functions and
    thresholds - lifted out (the
    module itself imports pystray / PIL and cannot load here). The log defaults to a path
    that does not exist."""
    tree = ast.parse(TRAY.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _TRAY_FUNCS)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in _TRAY_CONSTS for t in n.targets))]
    assert len(keep) == len(_TRAY_FUNCS) + len(_TRAY_CONSTS)
    state_path = Path(state_path)
    ns = {"json": json, "re": re, "datetime": _dt.datetime, "timezone": _dt.timezone, "Path": Path,
          "OPTIONS_SCREENER_STATE_PATH": state_path,
          "OPTION_CALC_STATE_PATH": state_path.parent / "option_calc_worker.json",
          "OPTIONS_SCREENER_LOG_PATH": Path(log_path) if log_path else state_path.parent / "no-logs" / "x.log"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(TRAY), "exec"), ns)   # noqa: S102
    return ns


def _tray_function(state_path, log_path=None):
    """``get_screener_collector_status`` lifted out of tray_status.py."""
    return _tray_ns(state_path, log_path)["get_screener_collector_status"]


TRAY_STATES = {"starting", "pass", "universe", "stocks", "history", "idle", "error", "stopped", "stale",
               "absent"}
TRAY_NOW = _dt.datetime(2026, 10, 8, 14, 0, tzinfo=_dt.timezone.utc)
T01 = ("Step 1 of 2: reading Massive's list of optionable stocks - page 312 (1,840 stocks so far, 4 min). "
       "Results start appearing as soon as the first stocks are read.")
T08 = ("Universe refresh failed 07:31 ET (Massive HTTP 502); next try 07:46 ET - yesterday's list in use.")


def _write(p: Path, doc: dict) -> None:
    p.write_text(json.dumps(doc), encoding="utf-8")


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
    assert got["last_pass_id"] == 1 and got["last_pass_contracts"] > 0 and got["last_pass_n"] == 3
    assert got["detail"].startswith("Up to date with the Thu Oct 8 10:00 ET read; next market pass ")   # T-17
    assert got["detail"] == _doc(tmp_path)["detail"]
    assert got["warn"] is None and got["error_kind"] is None

    got = status(now=_utc(RTH) + _dt.timedelta(minutes=6))      # no heartbeat for 5 min
    assert (got["state"], got["color"]) == ("stale", "amber") and "NO HEARTBEAT" in got["line"]
    assert "TST-Options-Screener" in got["line"] and got["tip"].startswith("Scr stale")

    cl.fail[("chain_snapshot", None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)
    clk.advance(minutes=30)
    col.tick()
    err = status(now=_utc(clk.t))
    assert (err["state"], err["color"], err["tip"]) == ("error", "amber", "Scr ERR")
    assert "Massive rejected the API key" in err["line"] and "Massive failing" in err["line"]
    assert err["error_kind"] == "auth" and "Massive rejected the API key" in err["reason"]
    toast = _tray_ns(tmp_path / "state" / "screener_collector.json")["screener_toast"]
    assert toast(got, err) is None                                 # stale -> error: already a bad state
    assert toast(dict(got, state="idle"), err).startswith("Options screener: ERROR - Massive rejected the API key")

    stale = status(now=_utc(clk.t) + _dt.timedelta(minutes=10))  # it stopped reporting while in error
    assert stale["state"] == "stale" and stale["line"].startswith("Options screener: NOT RUNNING - ")
    assert "Massive rejected the API key" in stale["line"]

    col.stop("test stop")
    got = status(now=_utc(clk.t))
    assert (got["state"], got["color"], got["tip"]) == ("stopped", "amber", "Scr stopped")


def test_tray_status_function_cases(tmp_path):
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    now = TRAY_NOW
    got = status(now=now)
    assert (got["state"], got["level"], got["color"], got["tip"]) == ("absent", "absent", "grey", "Scr -")
    assert "not running on this PC" in got["line"]

    base = {"state": "pass", "pass_id": 12, "pass_kind": "cycle", "symbols_done": 1234, "symbols_total": 4512,
            "last_pass_et": "09:45 ET", "last_pass_id": 11, "last_pass_contracts": 1043221,
            "universe_n": 4512, "universe_done": "2026-10-08T11:40:00+00:00", "history_done_n": 1204,
            "history_total": 4512, "api_ok": True, "heartbeat": "2026-10-08T13:59:40+00:00",
            "detail": "Reading the option market (live, 15-min delayed): 1,234 of 4,512 underlyings",
            "last_error": None, "stock_days_pending": 0}
    cases = [
        ({}, "pass", "green", "Scr p12",
         ("cycle pass 12 1,234/4,512", "last pass 09:45 ET", "universe 4,512", "IV history 1,204/4,512",
          "hb 20s ago")),
        ({"state": "idle"}, "idle", "green", "Scr idle", ("idle", "last pass 09:45 ET")),
        ({"state": "history"}, "history", "green", "Scr history", ("history",)),
        ({"state": "stocks", "stock_days_pending": 412}, "stocks", "green", "Scr stocks", ("stock days to go 412",)),
        ({"state": "universe", "last_pass_et": None}, "universe", "green", "Scr universe", ("universe",)),
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
        _write(p, dict(base, **change))
        got = status(now=now)
        assert got["state"] in TRAY_STATES
        assert (got["state"], got["color"], got["tip"]) == (state, color, tip), change
        for n in needles:
            assert n in got["line"], (change, n, got["line"])
    _write(p, dict(base, heartbeat="2026-10-08T13:50:00Z"))
    got = status(now=now)
    assert (got["state"], got["color"]) == ("stale", "amber") and "10m" in got["line"]
    assert got["line"].startswith("Options screener: NO HEARTBEAT for 10m (last: pass)")
    p.write_text("{not json", encoding="utf-8")
    got = status(now=now)
    assert (got["state"], got["color"], got["tip"]) == ("error", "amber", "Scr ?")


def test_tray_universe_walk_line_detail_and_progress(tmp_path):
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    walk = {"state": "universe", "heartbeat": "2026-10-08T13:59:50+00:00", "pass_id": None, "universe_n": 1840,
            "universe_done": None, "last_pass_et": None, "last_pass_id": None, "history_total": 0,
            "stock_days_pending": None, "detail": T01, "last_error": None, "api_ok": True,
            "progress": {"universe_pages": 312, "universe_symbols": 1840,
                         "universe_started": "2026-10-08T13:55:00+00:00"}}
    _write(p, walk)
    got = status(now=TRAY_NOW)
    assert got["line"] == "Options screener: universe · page 312 (1,840 so far) · hb 10s ago"     # T-70
    assert (got["state"], got["color"], got["tip"], got["detail"]) == ("universe", "green", "Scr universe", T01)
    long = T01 + " · " + T01                                   # jobs at once: joined, then cut at 220
    _write(p, dict(walk, detail=long))
    assert status(now=TRAY_NOW)["detail"] == long[:220]
    # the first walk legitimately has no list yet: never amber during the walk or at start
    for change in ({"warn": T08}, {"universe_n": 0, "last_error": "universe: Massive HTTP 502"},
                   {"state": "starting", "universe_n": 0, "last_error": "universe: Massive HTTP 502"},
                   {"last_pass_contracts": 0, "last_pass_id": 1, "last_pass_et": "16:40 ET"}):
        _write(p, dict(walk, **change))
        got = status(now=TRAY_NOW)
        assert (got["color"], got["level"]) == ("green", "ok"), change
    # a day-2 refresh: the list on file is shown, the walk's page too
    _write(p, dict(walk, universe_done="2026-10-07T11:40:00+00:00", universe_n=4512, last_pass_et="16:40 ET",
                   last_pass_id=7))
    assert status(now=TRAY_NOW)["line"] == ("Options screener: universe · page 312 (1,840 so far) · "
                                           "last pass 16:40 ET · universe 4,512 · hb 10s ago")

    first = {"state": "pass", "pass_id": 1, "pass_kind": "eod", "symbols_done": 1240, "symbols_total": 4512,
             "heartbeat": "2026-10-08T13:59:40+00:00", "last_pass_et": None, "last_pass_id": None,
             "universe_n": 4512, "universe_done": "2026-10-08T13:58:00+00:00", "detail": "Reading ...",
             "progress": {"pass_kind": "eod", "pass_session": "2026-10-07", "pass_pct": 27.34, "pass_eta_s": 840,
                          "pass_paused_at": None, "stock_days_pending": 412, "stock_eta_s": 9888}}
    _write(p, first)
    got = status(now=TRAY_NOW)
    assert got["line"] == ("Options screener: pass · eod pass 1 1,240/4,512 27.3% · about 14 min left · "
                           "first pass running · universe 4,512 · stock days to go 412 (at least 2.7 h) · "
                           "hb 20s ago")
    assert (got["tip"], got["color"], got["last_pass_n"]) == ("Scr p1 27%", "green", None)
    _write(p, dict(first, progress=dict(first["progress"], pass_paused_at="2026-10-08T13:58:00+00:00")))
    line = status(now=TRAY_NOW)["line"]
    assert "1,240/4,512 27.3% · paused · first pass running" in line and "about 14 min left" not in line

    hist = {"state": "history", "heartbeat": "2026-10-08T13:59:40+00:00", "last_pass_et": "16:40 ET",
            "last_pass_id": 3, "universe_n": 4512, "universe_done": "2026-10-08T11:40:00+00:00",
            "history_done_n": 1204, "history_total": 4512, "detail": "Building IV history ...",
            "progress": {"history_left": 3300, "history_waiting": 8, "history_eta_s": 7200,
                         "stock_days_pending": 0, "stock_eta_s": 0}}
    _write(p, hist)
    got = status(now=TRAY_NOW)
    assert "IV history 1,204/4,512 (3,300 left, about 2.0 h)" in got["line"]
    assert "stock days" not in got["line"]
    _write(p, dict(hist, progress={"history_left": 3300, "history_eta_s": None}))
    assert "IV history 1,204/4,512 (3,300 left) · hb" in status(now=TRAY_NOW)["line"]


def test_tray_warnings_are_amber(tmp_path):
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    idle = {"state": "idle", "heartbeat": "2026-10-08T13:59:40+00:00", "last_pass_et": "16:40 ET",
            "last_pass_id": 4, "last_pass_kind": "eod", "last_pass_session": "2026-10-07",
            "last_pass_contracts": 980000, "universe_n": 4512, "universe_done": "2026-10-07T11:40:00+00:00",
            "detail": "Up to date with the Wed Oct 7 close; next market pass Thu Oct 8 09:45 ET.",
            "last_error": None, "warn": None}
    _write(p, idle)
    assert status(now=TRAY_NOW)["color"] == "green"

    cases = [
        ({"warn": T08}, T08),                                                   # a failed day-2 refresh
        ({"state": "pass", "pass_id": 5, "symbols_total": 10, "symbols_done": 2, "warn": T08}, T08),
        ({"last_pass_contracts": 0}, "the last market pass (eod 2026-10-07) stored no contracts"),
        ({"last_pass_contracts": 0, "last_error": "eod pass 4 read no option data: x"},
         "eod pass 4 read no option data: x"),
        ({"universe_n": 0, "last_error": "universe: Massive HTTP 502"}, "universe: Massive HTTP 502"),
    ]
    for change, why in cases:
        _write(p, dict(idle, **change))
        got = status(now=TRAY_NOW)
        assert got["state"] in TRAY_STATES and got["state"] == change.get("state", "idle")
        assert (got["color"], got["level"], got["tip"]) == ("amber", "warn", "Scr WARN"), change
        assert got["line"] == "Options screener: WARN - " + why                 # T-71
        assert got["reason"] == why and got["detail"] == idle["detail"]
    _write(p, dict(idle, warn="x" * 400))
    assert status(now=TRAY_NOW)["line"] == "Options screener: WARN - " + "x" * 220
    # no list and no error is not a warning (e.g. a fresh install before the first walk)
    _write(p, dict(idle, universe_n=0, last_pass_et=None, last_pass_id=None, last_pass_contracts=None))
    got = status(now=TRAY_NOW)
    assert got["color"] == "green" and "no pass yet" in got["line"]


def test_tray_shows_the_iv_files_progress_and_its_warning(tmp_path):
    """v4.138 (tray-sync rule): the IV history from Massive's daily option files on the
    screener line - its days, time left, waiting / retrying / off - and Massive refusing the
    files as an amber WARN; an older collector without the keys changes nothing."""
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    det = "Building IV history from Massive's daily files: 187 of 252 days, about 6 min left."
    prog = {"flat_state": "building", "flat_days_done": 187, "flat_days_total": 252, "flat_eta_s": 360,
            "flat_error": None}
    base = {"state": "history", "heartbeat": "2026-10-08T13:59:40+00:00", "last_pass_et": "16:40 ET",
            "last_pass_id": 3, "last_pass_contracts": 980000, "universe_n": 4512,
            "universe_done": "2026-10-08T11:40:00+00:00", "history_done_n": 1204, "history_total": 4512,
            "detail": det, "warn": None, "last_error": None, "progress": prog}
    _write(p, base)
    got = status(now=TRAY_NOW)
    assert (got["color"], got["level"], got["tip"], got["detail"]) == ("green", "ok", "Scr history", det)
    assert got["line"] == ("Options screener: history · last pass 16:40 ET · universe 4,512 · IV history 1,204/4,512 · "
                           "IV files 187/252 days, about 6 min · hb 20s ago")
    boto = "boto3 is not installed on this PC - run: pip install -r app\\requirements.txt"
    for change, needle in (({"flat_state": "waiting", "flat_eta_s": None}, "IV files 187/252 days (waiting for stock prices) ·"),
                           ({"flat_state": "ready", "flat_days_done": 252}, "IV files 252/252 days · hb"),
                           ({"flat_state": "retry", "flat_error": "could not reach Massive Flat Files"},
                            "IV files 187/252 days (retrying) ·"),
                           ({"flat_state": "retry", "flat_days_total": None}, "· IV files retrying ·")):
        _write(p, dict(base, progress=dict(prog, **change)))
        got = status(now=TRAY_NOW)
        assert (got["color"], got["level"]) == ("green", "ok") and needle in got["line"], (change, got["line"])
    for quiet in ({"flat_state": "off", "flat_error": None, "flat_days_total": None}, {}, None):   # no keys / older
        _write(p, dict(base, progress=quiet))
        got = status(now=TRAY_NOW)
        assert got["color"] == "green" and "IV files" not in got["line"], quiet
    # the keys are set but the lane cannot run on this PC: amber WARN with the whole reason (the fix included)
    off = dict(base, progress=dict(prog, flat_state="off", flat_days_total=None, flat_error=boto, flat_kind="config"))
    _write(p, off)
    got = status(now=TRAY_NOW)
    assert (got["color"], got["level"], got["tip"]) == ("amber", "warn", "Scr WARN")
    assert got["line"] == "Options screener: WARN - IV history from Massive's daily files is not running: " + boto
    assert got["reason"].endswith("pip install -r app\\requirements.txt")
    _write(p, dict(off, state="error", detail="Massive rejected the API key (HTTP 401)"))     # an error wins
    got = status(now=TRAY_NOW)
    assert got["tip"] == "Scr ERR" and "· IV files off ·" in got["line"]
    # the files keep failing for another reason (network / http / a bug): the REST reads took over
    fail = ("could not reach Massive Flat Files for the file list for 2026-10 after 5 tries "
            "(EndpointConnectionError: Could not connect)")
    for k in ("network", "http", "error"):
        _write(p, dict(base, progress=dict(prog, flat_state="error", flat_kind=k, flat_error=fail)))
        got = status(now=TRAY_NOW)
        assert (got["color"], got["level"], got["tip"]) == ("amber", "warn", "Scr WARN"), k
        assert got["line"] == ("Options screener: WARN - IV history from Massive's daily files keeps failing (the REST "
                               "reads build it meanwhile): " + fail)
    # Massive refused the files (the keys or the plan): amber WARN with the reason, the detail kept
    err = ("Massive did not accept the Flat Files keys for the file list for 2026-10 (HTTP 403, InvalidAccessKeyId) - "
           "copy the Access Key ID and the Secret Access Key again from massive.com/dashboard/keys into app\\.env")
    bad = dict(base, state="idle", detail="Up to date with the Wed Oct 7 close.",
               progress={"flat_state": "error", "flat_error": err, "flat_days_done": 40, "flat_days_total": 252})
    _write(p, bad)
    got = status(now=TRAY_NOW)
    why = "IV history from Massive's daily files is paused: " + err
    assert (got["color"], got["level"], got["tip"]) == ("amber", "warn", "Scr WARN")
    assert got["line"] == "Options screener: WARN - " + why[:220] and got["reason"] == why[:220]
    assert got["detail"] == "Up to date with the Wed Oct 7 close."
    _write(p, dict(bad, state="universe"))                       # still a warning during a day-2 list refresh
    assert status(now=TRAY_NOW)["tip"] == "Scr WARN"
    _write(p, dict(bad, warn=T08))                               # the list refresh's own warning first
    assert status(now=TRAY_NOW)["line"] == "Options screener: WARN - " + T08
    _write(p, dict(bad, state="error", detail="Massive rejected the API key (HTTP 401)"))     # an error wins
    got = status(now=TRAY_NOW)
    assert got["tip"] == "Scr ERR" and "IV history from Massive's daily files" not in got["line"]


def test_tray_not_running_and_never_checked_in(tmp_path):
    p = tmp_path / "state" / "screener_collector.json"
    log_file = tmp_path / "logs" / "screener_collector.log"
    status = _tray_function(p, log_file)
    crash = ("The collector could not start on the server: the app modules could not be loaded "
             "(ModuleNotFoundError: No module named 'numpy'). - see logs\\screener_collector.log")
    p.parent.mkdir(parents=True)
    _write(p, {"state": "error", "error_kind": "startup", "detail": crash, "last_error": "x",
               "heartbeat": "2026-10-08T12:00:00+00:00", "written_by": "dashboard_tst/deploy/screener_collector.py"})
    got = status(now=TRAY_NOW)
    assert (got["state"], got["color"], got["level"]) == ("stale", "amber", "warn")
    assert got["line"] == ("Options screener: NOT RUNNING - " + crash[:160] + " (2.0h ago) - "
                           "is TST-Options-Screener running?")                     # T-72
    _write(p, {"state": "error", "detail": crash, "heartbeat": "2026-10-08T13:59:00+00:00"})
    got = status(now=TRAY_NOW)                                     # a fresh crash state: an error
    assert (got["state"], got["tip"]) == ("error", "Scr ERR") and "could not start on the server" in got["line"]
    assert got["detail"] == crash and got["reason"] == crash      # the whole reason on the second line
    p.unlink()

    assert status(now=TRAY_NOW)["state"] == "absent"               # no file, no log: not on this PC
    log_file.parent.mkdir()
    log_file.write_text(
        "2026-10-08 09:00:00 INFO    screener collector starting\n"
        "2026-10-08 09:00:01 ERROR   could not load the app modules\n"
        "Traceback (most recent call last):\n"
        '  File "deploy/screener_collector.py", line 180, in _load_app\n'
        "    importlib.import_module('app.services.scr_collector')\n"
        "ValueError: an older problem\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "app/services/screener/engine.py", line 3, in <module>\n'
        "    import numpy as np\n"
        "ModuleNotFoundError: No module named 'numpy'\n"
        "2026-10-08 09:00:01 WARNING --forever: could not start; trying again in 5 min\n", encoding="utf-8")
    got = status(now=TRAY_NOW)
    assert (got["state"], got["color"], got["level"], got["tip"]) == ("stale", "amber", "warn", "Scr NEVER")
    assert got["line"] == ("Options screener: NEVER CHECKED IN - last log error: "
                           "ModuleNotFoundError: No module named 'numpy'")             # T-73
    # only the tail is read: an error further back than 64 KB is not found
    log_file.write_text("2026-10-08 09:00:01 ERROR   an old error line\n" + "x" * 70000 + "\n"
                        "2026-10-08 10:00:01 ERROR   init_screener_db (the Alembic upgrade) failed\n",
                        encoding="utf-8")
    assert status(now=TRAY_NOW)["line"].endswith("last log error: init_screener_db (the Alembic upgrade) failed")
    log_file.write_text("2026-10-08 09:00:01 ERROR   " + "y" * 300 + "\n" + "z" * 70000 + "\n", encoding="utf-8")
    assert status(now=TRAY_NOW)["line"].endswith("last log error: none found")
    # the log path defaults to logs\screener_collector.log beside the state folder
    assert _tray_ns(p)["get_screener_collector_status"](p, now=TRAY_NOW)["tip"] == "Scr NEVER"


def test_tray_toasts_on_the_edges(tmp_path):
    p = tmp_path / "screener_collector.json"
    ns = _tray_ns(p)
    status, toast = ns["get_screener_collector_status"], ns["screener_toast"]

    def read(doc):
        if doc is None:
            if p.exists():
                p.unlink()
        else:
            _write(p, doc)
        return status(now=TRAY_NOW)

    hb = "2026-10-08T13:59:40+00:00"
    first = read({"state": "pass", "pass_id": 1, "pass_kind": "eod", "symbols_done": 9, "symbols_total": 4512,
                  "universe_n": 4512, "last_pass_et": None, "last_pass_id": None, "heartbeat": hb})
    done = read({"state": "idle", "pass_id": 1, "symbols_done": 4512, "symbols_total": 4512, "universe_n": 4512,
                 "last_pass_et": "16:58 ET", "last_pass_id": 1, "last_pass_contracts": 1043221, "heartbeat": hb})
    err = read({"state": "error", "detail": "Massive rejected the API key (HTTP 401); next try 17:05 ET",
                "last_pass_et": "16:58 ET", "last_pass_id": 1, "heartbeat": hb})
    stale = read({"state": "idle", "last_pass_et": "16:58 ET", "last_pass_id": 1,
                  "heartbeat": "2026-10-08T13:40:00+00:00"})
    absent = read(None)

    assert toast(first, done) == "Options screener: first market pass done - 4,512 underlyings, 1,043,221 contracts"
    assert toast(done, done) is None and toast(first, first) is None           # edge-triggered: once
    assert toast(done, err) == "Options screener: ERROR - Massive rejected the API key (HTTP 401); next try 17:05 ET"
    assert toast(err, err) is None
    assert toast(done, stale) == "Options screener: no heartbeat for 5 min"
    assert toast(err, stale) is None and toast(stale, err) is None             # still not working: no repeat
    assert toast(stale, done) is None and toast(err, done) is None             # recovering is quiet
    assert toast(None, err) is None and toast(None, done) is None              # the tray's first poll
    assert toast(absent, done) is None                                         # the file just appeared
    p.write_text("{not json", encoding="utf-8")
    broken = status(now=TRAY_NOW)
    assert toast(done, broken) == "Options screener: ERROR - see the tray status"
    assert toast(broken, done) is None                                         # no heartbeat read before
    p.unlink()
    log_file = tmp_path / "x.log"
    log_file.write_text("2026-10-08 09:00:01 ERROR   init_screener_db failed\n", encoding="utf-8")
    never = status(now=TRAY_NOW, log_path=log_file)
    assert never["tip"] == "Scr NEVER"
    assert toast(absent, never) == "Options screener: ERROR - init_screener_db failed"


def test_tray_wires_the_line_into_tooltip_and_window():
    src = TRAY.read_text(encoding="utf-8")
    compile(src, str(TRAY), "exec")
    tree = ast.parse(src)
    callers: dict[str, set] = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    callers.setdefault(node.func.id, set()).add(fn.name)
    assert {"_update_loop", "_build_progress_window"} <= callers["get_screener_collector_status"]
    assert callers["screener_toast"] == {"_update_loop"}            # the toasts
    loop = ast.get_source_segment(src, next(f for f in tree.body
                                             if isinstance(f, ast.FunctionDef) and f.name == "_update_loop"))
    assert "icon.notify(" in loop and 'title="Options screener"' in loop
    assert "current_alert = (dc.get(\"status\") == \"issues\")" in loop   # the red ring stays the deep check's
    assert '"dashboard_tst" / "state" / "screener_collector.json"' in src
    assert '"dashboard_tst" / "logs" / "screener_collector.log"' in src
    assert scr_collector.STATE_PATH.relative_to(DASH_ROOT.parent).as_posix() == \
        "dashboard_tst/state/screener_collector.json"
    assert "{scr_str}" in src                                   # the tooltip fragment
    assert "scr_detail_var.set(sc_detail)" in src               # the collector's sentence in the window


# ─────────────────────────────── the tray: the option calculator lane ───────────────────────────────

CALC_NOW = _dt.datetime(2026, 10, 8, 18, 33, tzinfo=_dt.timezone.utc)      # 14:33 ET (EDT)
CALC_BASE = {"version": "1.0", "pid": 4242, "heartbeat": "2026-10-08T18:32:40+00:00", "state": "idle",
             "detail": "up to date with pass 812 · 7 active setups", "last_look": "2026-10-08T18:32:40+00:00",
             "next_look": "2026-10-08T18:33:40+00:00", "last_pass_id": 812, "last_pass_session": "2026-10-08",
             "last_pass_finished": "2026-10-08T18:31:00+00:00", "requests_active": 7, "requests_due": 0,
             "done": 20, "failed": 0, "last_run_ms": 120, "last_error": None,
             "computed_at": "2026-10-08T18:32:05+00:00", "written_by": "option_calc_worker"}


def _calc_status(tmp_path, change=None, *, now=CALC_NOW):
    p = tmp_path / "option_calc_worker.json"
    if change is not None:
        _write(p, dict(CALC_BASE, **change))
    return _tray_ns(tmp_path / "screener_collector.json")["get_option_calc_status"](p, now=now)


def test_tray_option_calc_reads_every_key_the_lane_writes():
    from app.services import calc_worker

    assert set(CALC_BASE) == set(calc_worker.STATE_KEYS)
    assert calc_worker.STATE_PATH.relative_to(DASH_ROOT.parent).as_posix() == \
        "dashboard_tst/state/option_calc_worker.json"


def test_tray_option_calc_idle_and_computing(tmp_path):
    got = _calc_status(tmp_path, {})
    assert (got["state"], got["level"], got["color"], got["tip"]) == ("idle", "ok", "green", "Calc idle")
    assert got["line"] == "Option calculator: idle · 7 setups · last computed 14:32 ET on pass 812 · hb 20s ago"
    assert got["detail"] == "up to date with pass 812 · 7 active setups" and got["reason"] is None
    assert (got["last_pass_id"], got["requests_active"], got["done"], got["failed"]) == (812, 7, 20, 0)
    assert got["computed_at"] == "2026-10-08T18:32:05+00:00"

    got = _calc_status(tmp_path, {"state": "computing", "requests_due": 3,
                                  "detail": "3 setups to compute on pass 812"})
    assert (got["state"], got["color"], got["tip"]) == ("computing", "green", "Calc 3 due")
    assert got["line"] == ("Option calculator: computing 3 setups (of 7) · last computed 14:32 ET on pass 812 "
                           "· hb 20s ago")
    assert got["detail"] == "3 setups to compute on pass 812"

    got = _calc_status(tmp_path, {"computed_at": "2026-10-07T18:32:05+00:00", "requests_active": 1})
    assert got["line"] == "Option calculator: idle · 1 setup · last computed Wed 14:32 ET on pass 812 · hb 20s ago"
    got = _calc_status(tmp_path, {"computed_at": None, "last_pass_id": None, "requests_active": 0,
                                  "detail": "waiting for the screener collector's first market pass"})
    assert got["line"] == "Option calculator: idle · no setups · waiting for the first market pass · hb 20s ago"
    got = _calc_status(tmp_path, {"state": "starting", "computed_at": None})
    assert (got["color"], got["tip"]) == ("green", "Calc starting")
    assert got["line"] == "Option calculator: starting · 7 setups · nothing computed yet · pass 812 · hb 20s ago"


def test_tray_option_calc_setups_that_could_not_be_computed(tmp_path):
    got = _calc_status(tmp_path, {"done": 26, "failed": 3, "last_error": "AAPL #4: the 2026-11-20 185 call is no longer listed",
                                  "detail": "pass 812: 6 computed, 1 not (see the setups) · 7 active setups · 120 ms"})
    assert (got["state"], got["color"], got["tip"]) == ("idle", "green", "Calc idle")
    assert got["line"] == ("Option calculator: idle · 7 setups · last computed 14:32 ET on pass 812 "
                           "· 1 setup could not be computed · hb 20s ago")
    assert got["reason"] == "AAPL #4: the 2026-11-20 185 call is no longer listed"
    got = _calc_status(tmp_path, {"failed": 3, "last_error": None})            # an earlier run's: not shown
    assert "could not be computed" not in got["line"]
    got = _calc_status(tmp_path, {"failed": 2, "last_error": "MSFT #9: no IV", "detail": "something else"})
    assert "· 2 setups could not be computed ·" in got["line"]


def test_tray_option_calc_stale_error_stopped_absent(tmp_path):
    p = tmp_path / "option_calc_worker.json"
    got = _calc_status(tmp_path)                                               # no file
    assert (got["state"], got["level"], got["color"], got["tip"]) == ("absent", "absent", "grey", "Calc -")
    assert got["line"] == "Option calculator: not running on this PC (no state file)"

    got = _calc_status(tmp_path, {"heartbeat": "2026-10-08T18:26:00+00:00"})
    assert (got["state"], got["level"], got["color"], got["tip"]) == ("stale", "warn", "amber", "Calc stale 7m")
    assert got["line"] == "Option calculator: NO HEARTBEAT for 7m (last: idle) - is TST-Option-Calc running?"

    err = {"state": "error", "last_error": "OperationalError: database is locked",
           "detail": "the look failed; again in 60 s"}
    got = _calc_status(tmp_path, err)
    assert (got["state"], got["level"], got["color"], got["tip"]) == ("error", "warn", "amber", "Calc ERR")
    assert got["line"] == ("Option calculator: ERROR · 7 setups · last computed 14:32 ET on pass 812 · hb 20s ago"
                           " - OperationalError: database is locked")
    assert got["reason"] == "OperationalError: database is locked"
    assert got["detail"] == "the look failed; again in 60 s"

    got = _calc_status(tmp_path, dict(err, heartbeat="2026-10-08T18:20:00+00:00"))
    assert (got["state"], got["color"], got["tip"]) == ("stale", "amber", "Calc stale 13m")
    assert got["line"] == ("Option calculator: NOT RUNNING - OperationalError: database is locked (13m ago) "
                           "- is TST-Option-Calc running?")

    got = _calc_status(tmp_path, {"state": "stopped", "detail": "stopped"})
    assert (got["state"], got["color"], got["tip"]) == ("stopped", "amber", "Calc stopped")
    assert got["line"] == "Option calculator: STOPPED · 7 setups · last computed 14:32 ET on pass 812 · hb 20s ago"
    assert got["detail"] is None

    p.write_text("{not json", encoding="utf-8")
    got = _calc_status(tmp_path)
    assert (got["state"], got["color"], got["tip"]) == ("error", "amber", "Calc ?")
    assert got["line"].startswith("Option calculator: state file unreadable (")


def test_tray_option_calc_toasts_on_the_edges(tmp_path):
    toast = _tray_ns(tmp_path / "screener_collector.json")["option_calc_toast"]
    idle = _calc_status(tmp_path, {})
    err = _calc_status(tmp_path, {"state": "error", "last_error": "OperationalError: database is locked"})
    stale = _calc_status(tmp_path, {"heartbeat": "2026-10-08T18:20:00+00:00"})
    gone = _calc_status(tmp_path, {"state": "error", "last_error": "OperationalError: database is locked",
                                   "heartbeat": "2026-10-08T18:20:00+00:00"})
    assert toast(idle, err) == "Option calculator: ERROR - OperationalError: database is locked"
    assert toast(idle, stale) == "Option calculator: no heartbeat for 5 min"
    assert toast(idle, gone) == "Option calculator: ERROR - OperationalError: database is locked"
    assert toast(err, err) is None and toast(err, stale) is None and toast(stale, err) is None
    assert toast(err, idle) is None and toast(stale, idle) is None             # recovering is quiet
    assert toast(None, err) is None                                            # the tray's first poll


def test_tray_wires_the_option_calc_line_into_tooltip_and_window():
    src = TRAY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    callers: dict[str, set] = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    callers.setdefault(node.func.id, set()).add(fn.name)
    assert {"_update_loop", "_build_progress_window"} <= callers["get_option_calc_status"]
    assert callers["option_calc_toast"] == {"_update_loop"}
    loop = ast.get_source_segment(src, next(f for f in tree.body
                                             if isinstance(f, ast.FunctionDef) and f.name == "_update_loop"))
    assert 'title="Option calculator"' in loop and "{calc_str}" in loop
    assert '"dashboard_tst" / "state" / "option_calc_worker.json"' in src
    assert "calc_detail_var.set(oc_detail)" in src


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


def test_cli_arguments_modes_and_exit_codes(monkeypatch, logger_levels, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state" / "screener_collector.json")   # its lock too
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
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state" / "screener_collector.json")   # its lock too
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


# ───────────────────────────── the contract history: day files -> Parquet (v4.139) ─────────────────────────────

def _pq_flat(root: Path, days) -> Path:
    """A flat-file store with one tiny options day file per day in ``days`` + its manifest."""
    from app.services import opt_history  # noqa: F401 - the lane's module

    man = {}
    for d in days:
        key = flat_files.key_for(d)
        path = root.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
            f.write("ticker,volume,open,close,high,low,window_start,transactions\n"
                    "O:AAA261120C00100000,5,1.0,1.1,1.2,0.9,1,2\nO:AAA261120P00100000,4,2.0,2.1,2.2,1.9,1,2\n")
        man[key] = {"day": d, "size": path.stat().st_size, "etag": "e-" + d, "downloaded_at": "2026-10-08T15:20:00Z"}
    (root / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return root


def test_the_parquet_lane_converts_the_day_files_and_reports_it(db, build, tmp_path):
    pytest.importorskip("pyarrow")
    from app.services import opt_history

    flat = _pq_flat(tmp_path / "flat", ["2026-10-05", "2026-10-06"])
    clk = Clock(RTH)
    store = opt_history.Store(root=tmp_path / "pq", flat_root=flat, now=lambda: clk.t)
    col, cl = build(clk, history_store=store)
    col.tick()                                         # inline: the job runs and is collected in the tick
    assert col.pq_status() == {"state": "ready", "error": None, "days_done": 2, "days_total": 2, "running": False}
    assert store.days() == ["2026-10-05", "2026-10-06"]
    doc = json.loads((tmp_path / "state" / "screener_collector.json").read_text(encoding="utf-8"))
    pr = doc["progress"]
    assert (pr["pq_state"], pr["pq_days_done"], pr["pq_days_total"], pr["pq_error"]) == ("ready", 2, 2, None)
    assert set(scr_collector.PROGRESS_KEYS) <= set(pr)
    # right after a job the lane looks again at once (more may have landed meanwhile) ...
    _pq_flat(flat, ["2026-10-05", "2026-10-06", "2026-10-07"])
    clk.t += _dt.timedelta(seconds=20)
    col.tick()
    assert col.pq_status()["days_done"] == 3 and store.days()[-1] == "2026-10-07"
    clk.t += _dt.timedelta(seconds=20)
    col.tick()                                         # ... finds nothing: from now on at most every minute
    _pq_flat(flat, ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"])
    clk.t += _dt.timedelta(seconds=20)
    col.tick()
    assert col.pq_status()["days_done"] == 3 and col.pq_status()["days_total"] == 3
    clk.t += _dt.timedelta(seconds=scr_collector.PQ_LOOK_S)
    col.tick()
    assert col.pq_status()["days_done"] == 4 and store.days()[-1] == "2026-10-08"
    assert cl.calls == [] or all("flatfiles" not in str(c) for c in cl.calls)     # no Massive request for it


def test_the_parquet_lane_reports_a_broken_folder_and_a_missing_pyarrow(db, build, tmp_path, monkeypatch):
    pytest.importorskip("pyarrow")
    from app.services import opt_history

    flat = _pq_flat(tmp_path / "flat", ["2026-10-06"])
    clk = Clock(RTH)

    class Broken(opt_history.Store):
        def run(self, days=None, *, stop=None, on_day=None):
            raise opt_history.HistoryError("config", "could not write 2026-10-06.parquet (disk full)")

    store = Broken(root=tmp_path / "pq", flat_root=flat)
    col, cl = build(clk, history_store=store)
    col.tick()
    st = col.pq_status()
    assert st["state"] == "error" and "disk full" in st["error"]
    pr = json.loads((tmp_path / "state" / "screener_collector.json").read_text(encoding="utf-8"))["progress"]
    assert pr["pq_state"] == "error" and "disk full" in pr["pq_error"]
    # pyarrow missing on this PC: the lane is off with the fix in its reason
    monkeypatch.setattr(opt_history, "available", lambda: "pyarrow is not installed on this PC (ImportError) - "
                                                           "run: pip install -r app\\requirements.txt")
    col2, _ = build(clk, history_factory=lambda: opt_history.Store(root=tmp_path / "pq2", flat_root=flat))
    col2.tick()
    st2 = col2.pq_status()
    assert st2["state"] == "off" and "pip install" in st2["error"]


def test_day_files_that_keep_failing_are_an_error_not_ready(db, build, tmp_path):
    """The review's finding: failed days wait an hour before the next try, so nothing is pending -
    the lane must still say error (with the newest failure's reason), never ready."""
    pytest.importorskip("pyarrow")
    from app.services import opt_history

    flat = _pq_flat(tmp_path / "flat", ["2026-10-05", "2026-10-06"])
    for d in ("2026-10-05", "2026-10-06"):                      # damaged copies, sizes as the manifest says
        p = flat.joinpath(*flat_files.key_for(d).split("/"))
        p.write_bytes(b"\x1f\x8b" + b"\x00" * (p.stat().st_size - 2))
    clk = Clock(RTH)
    store = opt_history.Store(root=tmp_path / "pq", flat_root=flat, now=lambda: clk.t)
    col, cl = build(clk, history_store=store)
    col.tick()
    clk.t += _dt.timedelta(seconds=20)
    col.tick()                                                  # the look after the job: nothing pending
    st = col.pq_status()
    assert st["state"] == "error" and st["days_done"] == 0 and st["days_total"] == 2
    assert st["error"].startswith("2 day files could not be converted (tried again later) - the newest, 2026-10-06:")
    pr = json.loads((tmp_path / "state" / "screener_collector.json").read_text(encoding="utf-8"))["progress"]
    assert pr["pq_state"] == "error" and pr["pq_failed"] == ["2026-10-06", "2026-10-05"]
    _pq_flat(flat, ["2026-10-05", "2026-10-06"])               # good copies downloaded again
    clk.t += _dt.timedelta(seconds=scr_collector.PQ_LOOK_S)
    col.tick()
    assert col.pq_status() == {"state": "ready", "error": None, "days_done": 2, "days_total": 2, "running": False}


def test_tray_shows_the_parquet_conversion_and_its_warning(tmp_path):
    """v4.139 (tray-sync rule): the contract history's conversion on the screener line - its
    days and time left, done - and a failing conversion as an amber WARN."""
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    prog = {"flat_state": "ready", "flat_days_done": 497, "flat_days_total": 497,
            "pq_state": "building", "pq_days_done": 120, "pq_days_total": 497, "pq_eta_s": 360, "pq_error": None}
    base = {"state": "history", "heartbeat": "2026-10-08T13:59:40+00:00", "last_pass_et": "16:40 ET",
            "last_pass_id": 3, "last_pass_contracts": 980000, "universe_n": 4512,
            "universe_done": "2026-10-08T11:40:00+00:00", "history_done_n": 4512, "history_total": 4512,
            "detail": "Contract history: converting the daily option files to Parquet, 120 of 497 days.",
            "warn": None, "last_error": None, "progress": prog}
    _write(p, base)
    got = status(now=TRAY_NOW)
    assert (got["color"], got["level"]) == ("green", "ok")
    assert "· IV files 497/497 days · Parquet 120/497 days, about 6 min · hb 20s ago" in got["line"], got["line"]
    _write(p, dict(base, progress=dict(prog, pq_state="ready", pq_days_done=497, pq_eta_s=None)))
    assert "· Parquet 497/497 days · hb" in status(now=TRAY_NOW)["line"]
    for quiet in ({"pq_state": "off", "pq_error": None, "pq_days_total": None}, {"pq_state": None}):
        _write(p, dict(base, progress=dict(prog, **quiet)))
        got = status(now=TRAY_NOW)
        assert got["color"] == "green" and "Parquet" not in got["line"], quiet
    err = "could not write 2026-10-06.parquet (disk full) - check the free disk space and the folder C:\\x"
    _write(p, dict(base, progress=dict(prog, pq_state="error", pq_error=err)))
    got = status(now=TRAY_NOW)
    assert (got["color"], got["level"], got["tip"]) == ("amber", "warn", "Scr WARN")
    assert got["line"] == "Options screener: WARN - Contract history (Parquet) is not being updated: " + err
    off = "pyarrow is not installed on this PC (ImportError) - run: pip install -r app\\requirements.txt"
    _write(p, dict(base, progress=dict(prog, pq_state="off", pq_error=off, pq_days_total=None)))
    got = status(now=TRAY_NOW)
    assert got["tip"] == "Scr WARN" and got["line"].endswith("Contract history (Parquet) is not running: " + off)


# ───────────────────────── the earnings history (v4.143): kept daily, the past year backfilled ─────────────────────────

def test_the_daily_earnings_read_keeps_its_dates_and_the_backfill_reads_the_past_year(db, build, tmp_path):
    past = clock.prev_trading_day(clock.prev_trading_day(clock.et_date(_utc(RTH)), 5)).isoformat()
    asked: list[str] = []

    def fetch(day):
        asked.append(day.isoformat())
        if day.isoformat() == past:
            return [{"symbol": "AAA", "when": "amc"}, {"symbol": "ZZZ", "when": "bmo"}]
        if day < clock.et_date(_utc(RTH)):
            return [{"symbol": "ZZZ", "when": "pre"}]       # a real calendar day has names on it
        return fake_earnings(day)

    clk = Clock(RTH)
    col, cl = build(clk, earnings_fetch=fetch)
    _settle(col, clk, n=30, until=lambda c: c._earnings_on is not None)
    # the daily read of the next 70 days keeps only TODAY's list as history (the rest are Nasdaq's
    # projections, which move) - the next date per stock stays the underlying's, re-read daily
    assert scr_store.earnings_dates(db, "AAA") == [] and scr_store.earnings_dates(db, "BBB") == []
    assert scr_store.underlying(db, "AAA")["earnings_date"] == "2026-10-20"
    assert scr_store.earnings_days_fetched(db, ["2026-10-08", "2026-10-20"]) == {"2026-10-08"}
    # the backfill: the past 260 weekdays, 10 a job, one job a tick, newest first
    _settle(col, clk, n=40, until=lambda c: c._earn_hist_days == [])
    assert col._earn_hist_days == []
    weekdays, d = [], clock.et_date(_utc(RTH))
    while len(weekdays) < scr_collector.EARNINGS_HISTORY_DAYS:
        d -= _dt.timedelta(days=1)
        if d.weekday() < 5:
            weekdays.append(d.isoformat())
    assert scr_store.earnings_days_fetched(db, weekdays) == set(weekdays)
    assert asked[-1] == weekdays[-1] and past in asked
    assert [e["on"] for e in scr_store.earnings_dates(db, "AAA")] == [past]
    assert scr_store.earnings_dates(db, "AAA", None, past)[0]["when"] == "amc"
    # a failed request (the strict fetch raises) leaves the batch for the next try; nothing is filed as empty
    boom_day = weekdays[3]
    col3, _ = build(clk, earnings_fetch=lambda d: (_ for _ in ()).throw(RuntimeError("Nasdaq down"))
                    if d.isoformat() == boom_day else fetch(d))
    col3._earn_hist_days = [boom_day, weekdays[4]]
    col3._earnings_on = clock.et_today(_utc(RTH))
    col3.universe_n = 3
    col3.tick()
    col3.tick()
    assert col3._earn_hist_days == [boom_day, weekdays[4]] and col3._earn_hist_retry_at is not None
    assert "earnings_history_left" in scr_collector.PROGRESS_KEYS
    assert col._progress(clk.t)["earnings_history_left"] == 0
    # a restart finds nothing left to read
    col2, _ = build(clk, earnings_fetch=fetch)
    n_before = len(asked)
    _settle(col2, clk, n=5, until=lambda c: c._earn_hist_days is not None)
    assert col2._earn_hist_days == [] and len(asked) - n_before <= scr_collector.EARNINGS_DAYS + 1


def test_tray_shows_the_earnings_history_backfill(tmp_path):
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    base = {"state": "idle", "heartbeat": "2026-10-08T13:59:40+00:00", "last_pass_et": "16:40 ET",
            "last_pass_id": 3, "last_pass_contracts": 980000, "universe_n": 4512,
            "universe_done": "2026-10-08T11:40:00+00:00", "history_done_n": 4512, "history_total": 4512,
            "detail": "Loading past earnings dates: 120 days to go.", "warn": None, "last_error": None,
            "progress": {"earnings_history_left": 120}}
    _write(p, base)
    got = status(now=TRAY_NOW)
    assert got["color"] == "green" and "· earnings history 120 days to go ·" in got["line"]
    _write(p, dict(base, progress={"earnings_history_left": 0}))
    assert "earnings history" not in status(now=TRAY_NOW)["line"]


def test_the_next_pass_countdown_is_counted_in_utc_across_a_dst_change(build):
    """v4.149: Fri Oct 30 2026 17:00 EDT, the day's EOD pass done -> the next pass is Mon Nov 2
    09:45 EST. Two wall clocks differ by 64h45m; the real wait is an hour longer."""
    clk = Clock(_dt.datetime(2026, 10, 30, 21, 0))        # 17:00 EDT
    col, _cl = build(clk)
    col._last_eod, col._last_cycle, col.universe_n = "2026-10-30", None, 3
    pr = col._progress(clk())
    assert pr["next_pass"] == "2026-11-02T09:45:00-05:00"
    assert pr["next_pass_in_s"] == 236700                 # 65 h 45 min, not 64 h 45 min


def test_a_pass_that_is_due_but_held_publishes_no_next_pass_time(build):
    """v4.149: after the close on a first start, the EOD read was done on the partial list and the
    full pass waits for the complete list - "due now" with nothing to count down to."""
    clk = Clock(_dt.datetime(2026, 10, 8, 21, 0))         # Thu 17:00 EDT
    col, _cl = build(clk)
    col._last_eod, col._partial_eod, col._universe_done, col.universe_n = "2026-10-07", "2026-10-08", None, 3
    pr = col._progress(clk())
    assert pr["next_pass"] is None and pr["next_pass_in_s"] is None and pr["cycle_min"] == col.cycle_min
    col._universe_done = clk()                             # the list is complete: the EOD pass is due now
    pr = col._progress(clk())
    assert pr["next_pass_in_s"] == 0 and pr["next_pass"] is not None


# ───────────────────────────── the closing IV snapshot (v4.225) ─────────────────────────────

def test_the_eod_pass_is_saved_as_the_closing_snapshot(db, build, tmp_path):
    """v4.225: after the EOD pass on the full list the collector keeps that pass's contracts -
    Massive's quote-based IV at the close - as Parquet (once: a restart does not write it again)."""
    pytest.importorskip("pyarrow")
    from app.services import opt_history, opt_snapshot

    (tmp_path / "flat").mkdir()
    clk = Clock(EOD)
    store = opt_history.Store(root=tmp_path / "pq", flat_root=tmp_path / "flat", now=lambda: clk.t)
    col, cl = build(clk, history_store=store)
    for _ in range(3):
        col.tick()
        clk.advance(seconds=15)
    assert col._last_eod == DAY
    root = tmp_path / "pq"
    assert opt_snapshot.days(root) == [DAY]
    tbl = opt_snapshot.chain("AAA", DAY, root=root)
    assert tbl.num_rows == KEPT and all(r["iv"] for r in tbl.to_pylist() if r["right"] in "CP")
    pr = json.loads((tmp_path / "state" / "screener_collector.json").read_text(encoding="utf-8"))["progress"]
    assert (pr["snap_state"], pr["snap_session"], pr["snap_error"]) == ("saved", DAY, None)
    assert pr["snap_rows"] == db.query(ScrContract).filter(ScrContract.session == DAY).count()
    mtime = opt_snapshot.day_path(DAY, root).stat().st_mtime
    col2, _ = build(clk, client=cl, history_store=opt_history.Store(root=root, flat_root=tmp_path / "flat"))
    col2.tick()
    col2.tick()
    assert opt_snapshot.day_path(DAY, root).stat().st_mtime == mtime and col2._snap_due is None


def test_tray_shows_the_closing_snapshot_and_its_warning(tmp_path):
    """v4.225 (tray-sync rule): the closing IV snapshot on the screener line, and one that could not
    be written as an amber WARN."""
    p = tmp_path / "screener_collector.json"
    status = _tray_function(p)
    prog = {"pq_state": "ready", "pq_days_done": 497, "pq_days_total": 497, "snap_state": "saved",
            "snap_session": "2026-10-27", "snap_error": None}
    base = {"state": "idle", "heartbeat": "2026-10-08T13:59:40+00:00", "last_pass_et": "16:40 ET",
            "last_pass_id": 3, "last_pass_contracts": 980000, "universe_n": 4512,
            "universe_done": "2026-10-08T11:40:00+00:00", "detail": "Up to date.", "warn": None,
            "last_error": None, "progress": prog}
    _write(p, base)
    got = status(now=TRAY_NOW)
    assert got["color"] == "green" and "· close IV 2026-10-27 saved ·" in got["line"], got["line"]
    _write(p, dict(base, progress=dict(prog, snap_state="saving")))
    assert "· close IV saving ·" in status(now=TRAY_NOW)["line"]
    err = "the closing snapshot of 2026-10-27 could not be written (OSError: disk full)"
    _write(p, dict(base, progress=dict(prog, snap_state="error", snap_error=err)))
    got = status(now=TRAY_NOW)
    assert (got["color"], got["tip"]) == ("amber", "Scr WARN")
    assert got["line"] == "Options screener: WARN - Closing IV snapshot not saved (tried again every 10 min): " + err
