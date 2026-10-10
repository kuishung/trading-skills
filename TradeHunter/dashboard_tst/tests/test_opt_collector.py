"""The Hermes options collector on Massive (OPTIONS_V2_DESIGN.md §13.4, v4.134):
``app/services/opt_collector.py``, ``deploy/options_collector.py``,
``deploy/setup_options_collector_task.ps1`` and the collector line in
``dashboard_intraday/tray_status.py``.

The collector runs the REAL ``opt_massive`` / ``opt_store`` against a FAKE Massive client
(the three endpoints, priced by Black-Scholes, recording every call) and a fake clock, on
a fresh SQLite file migrated to the Alembic head (conftest). Nothing touches the network:
no httpx client is built, and the earnings lookup is stubbed for every test.
"""
from __future__ import annotations

import ast
import datetime as _dt
import importlib.util
import json
import logging
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import clock, opt_collector, opt_massive, opt_store
from app.services.black_scholes import black_scholes
from app.services.massive import MassiveError
from app.services.opt_constants import RISK_FREE

from .conftest import DASH_ROOT
from .fixtures.options import bs_greeks

# 2026-10-08 is a Thursday (a trading day); New York is UTC-4 (EDT) - naive UTC below.
DAY = "2026-10-08"
PRE = _dt.datetime(2026, 10, 8, 12, 0)          # 08:00 ET
RTH = _dt.datetime(2026, 10, 8, 14, 0)          # 10:00 ET
AFTER_CLOSE = _dt.datetime(2026, 10, 8, 20, 5)  # 16:05 ET
EOD = _dt.datetime(2026, 10, 8, 20, 20)         # 16:20 ET
SAT = _dt.datetime(2026, 10, 10, 16, 0)         # Saturday 12:00 ET
EXPIRIES = ("2026-10-16", "2026-11-20")         # a weekly-range monthly and the next monthly
STRIKES = (90.0, 95.0, 100.0, 105.0, 110.0)
N_ROWS = len(EXPIRIES) * len(STRIKES) * 2
KEY = "unit-test-key-0123456789abcdef"          # not a real key
BASE = "https://api.massive.test"

TRAY = DASH_ROOT.parent / "dashboard_intraday" / "tray_status.py"
CLI = DASH_ROOT / "deploy" / "options_collector.py"
PS1 = DASH_ROOT / "deploy" / "setup_options_collector_task.ps1"
MODULE = Path(opt_collector.__file__)


# ───────────────────────────────────────── fakes ─────────────────────────────────────────

class Clock:
    def __init__(self, t: _dt.datetime):
        self.t = t

    def __call__(self) -> _dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += _dt.timedelta(**kw)


def _close(d: _dt.date) -> float:
    """The fake stock's close on ``d`` - deterministic, around 100."""
    return round(100.0 + 4.0 * math.sin(d.toordinal() / 9.0), 2)


def _weekdays(start, end):
    d = _dt.date.fromisoformat(str(start)[:10])
    e = _dt.date.fromisoformat(str(end)[:10])
    while d <= e:
        if d.weekday() < 5:
            yield d
        d += _dt.timedelta(days=1)


_OPT = re.compile(r"^O:([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


class FakeMassive:
    """A ``massive.Client`` stand-in: ``chain_snapshot`` (Options Starter shape: greeks, IV,
    OI, the day bar, NO bid/ask, stamped 15 min before the clock), ``stock_daily`` (Stocks
    Basic bars up to ``bars_end``) and ``option_daily`` (closes priced by Black-Scholes at
    30% vol on the stock's close, so the IV history solves back to 30). Every call is
    recorded as ``(name, symbol, kwargs)``; ``fail[(name, symbol)]`` - or ``(name, None)``
    for every symbol - raises. ``step_s`` advances the clock per snapshot (a slow read)."""

    base_url = BASE

    def __init__(self, clk: Clock, *, key: bool = True):
        self.clk = clk
        self.has_key = key
        self.calls: list[tuple] = []
        self.fail: dict = {}
        self.spot = 100.0
        self.bars_end = "2026-10-07"
        self.no_options: set[str] = set()
        self.step_s = 0.0

    def _rec(self, name, sym, **kw):
        self.calls.append((name, sym, kw))
        exc = self.fail.get((name, sym)) or self.fail.get((name, None))
        if exc is not None:
            raise exc

    def chain_snapshot(self, symbol, *, exp_gte=None, exp_lte=None, strike_gte=None, strike_lte=None):
        self._rec("chain_snapshot", symbol, exp_gte=str(exp_gte), exp_lte=str(exp_lte))
        if self.step_s:
            self.clk.advance(seconds=self.step_s)
        now = self.clk()
        stamp = now - _dt.timedelta(minutes=15)
        today = clock.et_date(now)
        rows = []
        for e in EXPIRIES:
            T = max((_dt.date.fromisoformat(e) - today).days, 1) / 365.0
            for k in STRIKES:
                for r in ("C", "P"):
                    g = bs_greeks(self.spot, k, T, 0.40, r)
                    px = round(max(0.05, g["price"]), 2)
                    rows.append({"expiry": e, "right": r, "strike": k, "iv": 0.40,
                                 "delta": round(g["delta"], 4), "gamma": round(g["gamma"], 5),
                                 "theta": round(g["theta"], 4), "vega": round(g["vega"], 4),
                                 "oi": 900, "volume": 40, "day_close": px, "day_vwap": px,
                                 "prev_close": px, "day_change_pct": 0.0, "bid": None, "ask": None,
                                 "bid_size": None, "ask_size": None, "last_updated": stamp,
                                 "und_price": self.spot, "und_as_of": stamp})
        return {"symbol": symbol, "rows": rows, "underlying_price": self.spot,
                "underlying_as_of": stamp, "pages": 1, "as_of": stamp}

    def stock_daily(self, symbol, start, end, *, adjusted=True):
        self._rec("stock_daily", symbol, start=str(start), end=str(end), adjusted=adjusted)
        last = min(str(end), self.bars_end)
        out = []
        for d in _weekdays(start, last):
            c = _close(d)
            out.append({"on": d.isoformat(), "open": c - 0.3, "high": c + 1.2, "low": c - 1.1,
                        "close": c, "volume": 2_000_000.0})
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
            p = round(black_scholes(_close(d), k, dte / 365.0, RISK_FREE, 0.30, kind).price, 2)
            if p >= 0.01:
                out.append({"on": d.isoformat(), "open": p, "high": p, "low": p, "close": p,
                            "volume": 10.0})
        return out

    # what the assertions read
    def names(self, sym=None) -> list[str]:
        return [c[0] for c in self.calls if sym is None or c[1] == sym]

    def of(self, name, sym=None) -> list[tuple]:
        return [c for c in self.calls if c[0] == name and (sym is None or c[1] == sym)]

    def read(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "chain_snapshot"]


def _bar_reads(cl, sym=None) -> list[tuple]:
    """The split-adjusted ``stock_daily`` reads (a history's extra unadjusted read for the
    IV series is left out): one per history, one per ``daily_update``."""
    return [c for c in cl.of("stock_daily", sym) if c[2].get("adjusted", True)]


# ───────────────────────────────────────── fixtures ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No test reaches Yahoo or Massive; no environment or cache leaks between tests."""
    from app.services import prices

    calls: list[str] = []

    def fake_earnings(symbol):
        calls.append(symbol)
        return {"date": "2026-10-28", "days": 20}

    monkeypatch.setattr(prices, "fetch_next_earnings", fake_earnings)
    for var in ("TST_MASSIVE_API_KEY", "TST_MASSIVE_BASE_URL", "TST_OPTIONS_CYCLE_MIN"):
        monkeypatch.delenv(var, raising=False)
    opt_store.reset_state()
    yield calls
    opt_store.reset_state()


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def build(Session, tmp_path):
    """``build(clock, **kw) -> (collector, client)``; every collector is stopped at
    teardown. ``client_factory=`` replaces the fake client (then ``client`` is None)."""
    made = []

    def _build(clk, *, client=None, **kw):
        if "client_factory" not in kw:
            client = client if client is not None else FakeMassive(clk)
            kw["client"] = client
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("state_path", tmp_path / "state" / "options_collector.json")
        kw.setdefault("log", logging.getLogger("test_opt_collector"))
        col = opt_collector.Collector(Session, clock=clk, **kw)
        made.append(col)
        return col, client

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


def _doc(tmp_path) -> dict:
    return json.loads((tmp_path / "state" / "options_collector.json").read_text(encoding="utf-8"))


def _utc(t: _dt.datetime) -> _dt.datetime:
    return t.replace(tzinfo=_dt.timezone.utc)


# ───────────────────────────────────────── no IBKR left ─────────────────────────────────────────

IBKR_WORDS = ("ib_insync", "th_ibkr", "eventkit", "ingest_supervisor", "clientid", "client_id",
              "blackout", "py -3.12", "4002")


def _imports(src: str) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def test_no_ibkr_import_or_gateway_logic_anywhere():
    for path in (MODULE, CLI):
        src = path.read_text(encoding="utf-8")
        assert not _imports(src) & {"ib_insync", "th_ibkr", "eventkit", "asyncio", "ingest_supervisor"}, path
        low = src.lower()
        for word in IBKR_WORDS:
            assert word not in low, (path.name, word)
    # importing the collector (and the CLI module) pulls no IBKR library in
    code = ("import sys, importlib.util; import app.services.opt_collector as m; "
            "spec = importlib.util.spec_from_file_location('cli_probe', r'%s'); "
            "c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c); "
            "print(sorted(n for n in ('ib_insync', 'th_ibkr', 'eventkit') if n in sys.modules))" % CLI)
    env = dict(os.environ)
    env.pop("TST_MASSIVE_API_KEY", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=str(DASH_ROOT), env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"


# ───────────────────────────────────────── heartbeat + key ─────────────────────────────────────────

def test_heartbeat_and_state_file_every_tick(db, user, build, tmp_path):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    opt_store.set_collector_status(db, last_eod_on="2026-10-09", cycle_n=7)
    clk = Clock(SAT)
    col, cl = build(clk)
    for i in range(3):
        assert col.tick() == "idle"
        doc = _doc(tmp_path)
        assert _dt.datetime.fromisoformat(doc["heartbeat"]) == _utc(clk.t)
        assert _status(db)["heartbeat"] == clk.t
        clk.advance(seconds=15)
    assert cl.calls == []                                     # nothing due on a Saturday
    assert {"state", "phase_detail", "mdt", "cycle_n", "cycle_started", "cycle_finished",
            "symbols_total", "symbols_done", "last_eod_on", "last_error", "heartbeat", "pid",
            "version", "source", "api", "api_ok", "error_kind", "next_try", "history_pending",
            "universe", "cycle_min", "written_by"} <= set(doc)
    assert "gateway" not in doc and "gateway_ok" not in doc
    assert (doc["version"], doc["source"], doc["api"]) == ("2.0", "massive", "api.massive.test")
    assert doc["last_eod_on"] == "2026-10-09" and doc["cycle_n"] == 7          # restored
    assert doc["phase_detail"] == "end-of-day 2026-10-09 done; next session 2026-10-12 09:30 ET"
    assert (doc["universe"], doc["history_pending"], doc["cycle_min"]) == (1, 0, 15)
    assert doc["error_kind"] is None and doc["next_try"] is None
    st = _status(db)
    assert st["gateway"] == "api.massive.test" and st["version"] == "2.0" and st["state"] == "idle"
    path = tmp_path / "state" / "options_collector.json"
    assert (path.parent / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    assert not path.with_name(path.name + ".tmp").exists()


def test_missing_key_is_an_error_looked_at_again_every_5_min(db, user, build, tmp_path):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(RTH)
    keys = [False, False, True]
    made: list[FakeMassive] = []

    def factory():
        c = FakeMassive(clk, key=keys[len(made)])
        made.append(c)
        return c

    col, _ = build(clk, client_factory=factory)
    assert col.tick() == "error"
    st = _status(db)
    assert st["last_error"] == opt_collector.NO_KEY_TEXT == "TST_MASSIVE_API_KEY is not set on this PC"
    assert st["phase_detail"] == "TST_MASSIVE_API_KEY is not set on this PC; next try 10:05 ET"
    doc = _doc(tmp_path)
    assert doc["state"] == "error" and doc["error_kind"] == "config"
    assert _dt.datetime.fromisoformat(doc["next_try"]) == _utc(RTH + _dt.timedelta(minutes=5))

    clk.advance(seconds=60)
    assert col.tick() == "error" and len(made) == 1          # not looked at again yet
    assert _status(db)["heartbeat"] == clk.t                 # ... but the heartbeat goes on

    clk.t = RTH + _dt.timedelta(minutes=5)
    assert col.tick() == "error" and len(made) == 2          # looked again: still none
    assert "next try 10:10 ET" in _status(db)["phase_detail"]

    clk.t = RTH + _dt.timedelta(minutes=10)
    assert col.tick() == "idle" and len(made) == 3           # the key is there: the pass ran
    assert made[2].read() == ["AAA"] and made[0].calls == made[1].calls == []
    doc = _doc(tmp_path)
    assert doc["state"] == "idle" and doc["error_kind"] is None and doc["api_ok"] is True
    assert [lg.error for lg in _logs(db)] == [None]          # no request was made without a key


def test_default_client_reads_the_key_from_app_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(opt_collector, "APP_ENV_PATH", env_file)
    monkeypatch.setenv("TST_MASSIVE_API_KEY", "x")
    monkeypatch.delenv("TST_MASSIVE_API_KEY")                 # absent now; absent again after the test
    c = opt_collector.default_client()
    try:
        assert c.has_key is False
    finally:
        c.close()
    env_file.write_text("TST_MASSIVE_API_KEY=%s\n" % KEY, encoding="utf-8")
    c = opt_collector.default_client()                       # a key added to app/.env is found
    try:
        assert c.has_key is True and KEY not in repr(c)
    finally:
        c.close()
        os.environ.pop("TST_MASSIVE_API_KEY", None)


# ───────────────────────────────────────── history ─────────────────────────────────────────

def test_history_one_symbol_per_tick_in_the_gap_after_the_pass(db, user, build):
    _basket(db, user, "AAA", "BBB")
    clk = Clock(RTH)
    col, cl = build(clk)

    assert col.tick() == "idle"                               # the due session pass goes first
    assert cl.read() == ["AAA", "BBB"] and cl.of("stock_daily") == []
    clk.advance(seconds=15)
    assert col.tick() == "history"                            # the gap: one history per tick
    names = cl.names("AAA")
    assert names[:2] == ["chain_snapshot", "stock_daily"]
    assert "chain_snapshot" not in names[1:]                  # the pass quoted it: no second chain read
    assert set(names[1:]) == {"stock_daily", "option_daily"} and 2 * 10 < names.count("option_daily") <= 300
    (bars,) = _bar_reads(cl, "AAA")
    assert bars[2]["end"] == "2026-10-07"                     # the last published session at 10:00 ET
    assert cl.names("BBB") == ["chain_snapshot"]              # one history per tick

    db.expire_all()
    und = opt_store.underlying(db, "AAA")
    assert und["history_done"] is True
    assert und["atr14"] and und["hv20"] and und["hv60"] and und["avg_vol20"]
    assert und["iv_n"] >= 200 and und["iv_rank"] is not None
    yesterday = (db.query(models.OptUnderlyingDaily)
                   .filter_by(symbol="AAA", on="2026-10-07").one())
    assert yesterday.iv30 == pytest.approx(30.0, abs=1.0)    # solved back from the option closes
    assert und["iv30"] == pytest.approx(40.0, abs=1.0)       # today's, from the chain the pass read
    assert und["spot"] == 100.0 and und["spot_source"] == "massive"
    assert _n_quotes(db, "AAA") == N_ROWS
    q = db.query(models.OptQuote).filter_by(symbol="AAA").first()
    assert (q.source, q.mdt, q.bid, q.ask) == ("massive", "delayed", None, None)
    assert q.as_of == RTH - _dt.timedelta(minutes=15) and q.mid > 0     # the feed's stamp, a model mid
    (lg,) = _logs(db, "AAA")
    assert (lg.kind, lg.source, lg.n_contracts, lg.error) == ("cycle", "massive", N_ROWS, None)
    st = _status(db)
    assert st["state"] == "history" and (st["symbols_done"], st["symbols_total"]) == (1, 2)
    assert st["mdt"] == "delayed" and st["gateway_ok"] is True

    clk.advance(seconds=15)
    assert col.tick() == "history"
    assert [c[1] for c in _bar_reads(cl)] == ["AAA", "BBB"]
    assert (_status(db)["symbols_done"], _status(db)["symbols_total"]) == (2, 2)

    clk.advance(seconds=15)
    assert col.tick() == "idle"                               # histories done, the next pass not due
    assert len(cl.read()) == 2 and len(_bar_reads(cl)) == 2
    clk.t = RTH + _dt.timedelta(minutes=15)
    col.tick()
    assert cl.read() == ["AAA", "BBB", "AAA", "BBB"] and col.cycle_n == 2
    assert [lg.kind for lg in _logs(db, "BBB")] == ["cycle", "cycle"]


def test_a_due_session_pass_goes_before_pending_histories(db, user, build):
    """A history is a few hundred requests; it runs in the gaps between session passes,
    one per tick. A pass that is running or due always has the tick, so a member importing
    a watchlist never holds back every other basket's 15-minute reads."""
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    clk = Clock(RTH)
    col, cl = build(clk, batch=1)
    assert col.tick() == "cycle" and cl.read() == ["AAA"]
    _basket(db, _member(db, "two@local.test", "Two"), "NWA", "NWB", "NWC")   # imported mid-pass

    clk.advance(seconds=15)
    assert col.tick() == "idle"                               # the running pass goes on and ends ...
    assert cl.read() == ["AAA", "BBB"] and cl.of("stock_daily") == []      # ... before any history
    assert _status(db)["phase_detail"] == "pass 1 finished at 10:00 ET (2/2 tickers)"
    for want in (["NWA"], ["NWA", "NWB"]):                   # the gap: one history per tick
        clk.advance(seconds=15)
        assert col.tick() == "history"
        assert [c[1] for c in _bar_reads(cl)] == want
    assert cl.read() == ["AAA", "BBB", "NWA", "NWB"]          # each never-quoted one: its chain after it

    clk.t = RTH + _dt.timedelta(minutes=15)                   # the next pass is due: it goes first,
    for i, sym in enumerate(["AAA", "BBB", "NWA", "NWB", "NWC"]):   # though NWC's history still waits
        assert col.tick() in ("cycle", "idle")
        assert cl.read()[-1] == sym and len(_bar_reads(cl)) == 2
        st = _status(db)
        assert (st["cycle_n"], st["symbols_done"], st["symbols_total"]) == (2, i + 1, 5)
        clk.advance(seconds=15)
    assert col.cycle_finished is not None and col.tick() == "history"     # the gap again: NWC
    assert [c[1] for c in _bar_reads(cl)] == ["NWA", "NWB", "NWC"]
    clk.advance(seconds=15)
    assert col.tick() == "idle" and col.history_pending == 0


def test_a_history_during_a_session_pass_leaves_its_counts_alone(db, user, build):
    """A pass held up by a chain pause lets a history run in the gap; the pass's
    ticker counts (heartbeat, strip, tray, the end-of-pass line) are not overwritten."""
    _basket(db, user, "AAA", "BBB", "CCC")
    _history_done(db, "AAA", "BBB", "CCC")
    clk = Clock(RTH)
    col, cl = build(clk, batch=1)
    assert col.tick() == "cycle" and (col.symbols_done, col.symbols_total) == (1, 3)
    cl.fail[("chain_snapshot", None)] = MassiveError(
        "plan", "your Massive plan does not include the options chain snapshot (HTTP 403)", 403)
    clk.advance(seconds=15)
    assert col.tick() == "error" and cl.read() == ["AAA", "BBB"]     # BBB refused: chain reads paused
    _basket(db, _member(db, "two@local.test", "Two"), "NEW")
    clk.advance(seconds=15)
    col.tick()                                                # NEW's history in the gap
    assert [c[1] for c in _bar_reads(cl)] == ["NEW"] and col.history_pending == 0
    st = _status(db)
    assert (st["state"], st["symbols_done"], st["symbols_total"]) == ("error", 1, 3)
    assert (col.symbols_done, col.symbols_total) == (1, 3)

    del cl.fail[("chain_snapshot", None)]
    clk.t = RTH + _dt.timedelta(minutes=5, seconds=30)        # the pause is over: the pass resumes
    col.tick()
    assert (_status(db)["symbols_done"], _status(db)["symbols_total"]) == (2, 3)
    clk.advance(seconds=15)
    assert col.tick() == "idle"
    assert _status(db)["phase_detail"] == "pass 1 finished at 10:05 ET (3/3 tickers)"


def test_a_history_during_the_eod_pass_leaves_its_counts_alone(db, user, build):
    _basket(db, user, "AAA", "BBB", "CCC")
    _history_done(db, "AAA", "BBB", "CCC")
    clk = Clock(EOD)
    col, cl = build(clk, batch=1)
    seen = []

    def counts():
        st = _status(db)
        seen.append((st["symbols_done"], st["symbols_total"]))
        return seen[-1]

    assert col.tick() == "eod" and counts() == (1, 3)
    _basket(db, _member(db, "two@local.test", "Two"), "NEW")
    clk.advance(seconds=15)
    assert col.tick() == "history" and counts() == (1, 3)    # NEW's history: the pass keeps its counts
    assert [c[1] for c in _bar_reads(cl)] == ["AAA", "NEW"]
    clk.advance(seconds=15)
    assert col.tick() == "eod" and counts() == (2, 3)
    clk.advance(seconds=15)
    assert col.tick() == "idle" and counts() == (3, 3)
    assert _status(db)["phase_detail"] == "end-of-day 2026-10-08 done (3 tickers)"
    assert all(d <= t for d, t in seen)


def test_history_failure_backs_off_and_is_logged(db, user, build):
    _basket(db, user, "NEW")
    clk = Clock(RTH)
    col, cl = build(clk, cycle_min=240)                       # one pass: the history back-off alone
    cl.fail[("stock_daily", "NEW")] = MassiveError("http", "Massive answered HTTP 500 for stock daily bars", 500)

    col.tick()                                                # the pass reads its chain first
    assert cl.read() == ["NEW"] and cl.of("stock_daily") == []
    clk.advance(seconds=15)
    col.tick()                                                # then its history - which fails
    assert [x.kind for x in _logs(db, "NEW")] == ["cycle", "history"]
    lg = _logs(db, "NEW")[-1]
    assert (lg.kind, lg.n_contracts) == ("history", 0) and "HTTP 500" in lg.error
    assert col.last_error.startswith("history NEW: Massive answered HTTP 500")
    assert col.shown_state != "error"                         # one symbol's failure is not an outage
    assert _doc_state(col) == "history"
    t_fail = clk.t

    clk.advance(seconds=15)
    assert col.tick() == "idle"                               # backing off (and quoted: no first read)
    assert len(cl.of("stock_daily")) == 1 and cl.read() == ["NEW"]

    clk.t = t_fail + _dt.timedelta(minutes=30, seconds=1)
    col.tick()                                                # retried after 30 min - fails again
    assert len(cl.of("stock_daily")) == 2
    t_fail = clk.t
    del cl.fail[("stock_daily", "NEW")]
    clk.t = t_fail + _dt.timedelta(minutes=59)
    col.tick()                                                # 60 min back-off now: not yet
    assert len(cl.of("stock_daily")) == 2
    clk.t = t_fail + _dt.timedelta(minutes=60, seconds=1)
    col.tick()
    assert len(_bar_reads(cl)) == 3
    db.expire_all()
    assert opt_store.underlying(db, "NEW")["history_done"] is True


def _doc_state(col) -> str:
    return json.loads(col.state_path.read_text(encoding="utf-8"))["state"]


def test_too_little_history_is_not_done_and_retried(db, user, build):
    _basket(db, user, "YNG")
    clk = Clock(PRE)
    col, cl = build(clk)
    cl.no_options.add("YNG")                                  # bars, but no option bars at all

    col.tick()
    db.expire_all()
    u = opt_store.underlying(db, "YNG")
    assert u["history_done"] is False and u["hv20"] is not None     # the bars still count
    (lg,) = _logs(db, "YNG")
    assert lg.kind == "history" and "0 IV points" in lg.error and "needs 20 of each" in lg.error
    assert _doc(col.state_path.parent.parent)["history_pending"] == 1

    clk.advance(seconds=15)
    col.tick()                                                # backing off; the first chain read instead
    assert len(_bar_reads(cl)) == 1 and cl.read() == ["YNG"]
    clk.advance(minutes=31)
    cl.no_options.clear()
    col.tick()
    db.expire_all()
    assert len(_bar_reads(cl)) == 2 and opt_store.underlying(db, "YNG")["history_done"] is True


# ───────────────────────────────────────── session passes ─────────────────────────────────────────

def test_session_pass_cadence_most_held_first(db, user, build):
    _basket(db, user, "AAA", "BBB", "CCC")
    other = _member(db, "two@local.test", "Two")
    _basket(db, other, "CCC")
    _history_done(db, "AAA", "BBB", "CCC")
    clk = Clock(RTH)
    col, cl = build(clk, batch=2)

    assert col.tick() == "cycle"
    assert cl.read() == ["CCC", "AAA"]                        # most held first, a few per tick
    st = _status(db)
    assert (st["state"], st["cycle_n"], st["symbols_done"], st["symbols_total"]) == ("cycle", 1, 2, 3)
    assert st["cycle_started"] == RTH and st["cycle_finished"] is None

    clk.advance(seconds=15)
    assert col.tick() == "idle"
    assert cl.read() == ["CCC", "AAA", "BBB"]
    st = _status(db)
    assert st["phase_detail"] == "pass 1 finished at 10:00 ET (3/3 tickers)"
    assert st["cycle_finished"] == clk.t
    assert {lg.kind for lg in _logs(db)} == {"cycle"}

    while clk.t < RTH + _dt.timedelta(minutes=14, seconds=30):
        clk.advance(seconds=15)
        col.tick()
    assert len(cl.read()) == 3                                # nothing until 15 min after the start
    assert _status(db)["phase_detail"] == "pass 1 done; the next starts 10:15 ET (every 15 min)"

    clk.t = RTH + _dt.timedelta(minutes=15)
    assert col.tick() == "cycle" and col.cycle_n == 2
    assert cl.read()[3:] == ["CCC", "AAA"]


def test_cycle_interval_from_the_environment(db, user, build, monkeypatch):
    assert opt_collector.env_cycle_min() == 15
    for raw, want in (("5", 5), ("240", 240), ("0", 15), ("241", 15), ("x", 15), ("", 15)):
        monkeypatch.setenv("TST_OPTIONS_CYCLE_MIN", raw)
        assert opt_collector.env_cycle_min() == want, raw
    monkeypatch.setenv("TST_OPTIONS_CYCLE_MIN", "5")
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(RTH)
    col, cl = build(clk)
    assert col.cycle_min == 5
    col.tick()
    clk.advance(minutes=4, seconds=59)
    col.tick()
    assert cl.read() == ["AAA"]
    clk.advance(seconds=1)
    col.tick()
    assert cl.read() == ["AAA", "AAA"] and col.cycle_n == 2


def test_tick_budget_keeps_the_heartbeat_timely(db, user, build, monkeypatch):
    syms = ["S%02d" % i for i in range(8)]
    _basket(db, user, *syms)
    _history_done(db, *syms)
    beats: list[_dt.datetime] = []
    real = opt_store.set_collector_status

    def spy(db_, **fields):
        beats.append(fields.get("heartbeat"))
        return real(db_, **fields)

    monkeypatch.setattr(opt_store, "set_collector_status", spy)
    clk = Clock(RTH)
    col, cl = build(clk)
    cl.step_s = 8.0                                           # every chain read takes 8 s
    col.tick()
    assert len(cl.read()) == 3                                # 24 s >= the 20 s budget: stop taking more
    assert len(beats) >= 3                                    # start, mid-pass (>= 15 s), end
    gaps = [(b - a).total_seconds() for a, b in zip(beats, beats[1:])]
    assert max(gaps) <= 16.0


def test_the_close_cuts_a_running_pass_short(db, user, build):
    _basket(db, user, "AAA", "BBB", "CCC")
    _history_done(db, "AAA", "BBB", "CCC")
    clk = Clock(_dt.datetime(2026, 10, 8, 19, 59, 50))       # 15:59:50 ET
    col, cl = build(clk, batch=1)
    assert col.tick() == "cycle" and cl.read() == ["AAA"]
    clk.t = AFTER_CLOSE
    assert col.tick() == "idle"
    assert cl.read() == ["AAA"] and col._cycle is None        # noqa: SLF001
    assert _status(db)["cycle_finished"] == AFTER_CLOSE
    assert _status(db)["phase_detail"] == "market closed; the end-of-day pass starts 16:20 ET"


def test_empty_universe_is_idle(db, build):
    clk = Clock(RTH)
    col, cl = build(clk)
    assert col.tick() == "idle" and col.detail == "no ticker in any member's basket"
    assert cl.calls == []


# ───────────────────────────────────────── end of day ─────────────────────────────────────────

def test_eod_pass_runs_exactly_once_per_trading_day(db, user, build, _clean):
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    clk = Clock(AFTER_CLOSE)
    col, cl = build(clk)

    assert col.tick() == "idle" and cl.calls == []            # 16:05 ET: not yet
    clk.t = EOD
    assert col.tick() == "idle"                               # the whole pass (2 <= batch), then the prune
    assert cl.read() == ["AAA", "BBB"]
    assert [c[1] for c in cl.of("stock_daily")] == ["AAA", "BBB"]
    assert all(c[2]["end"] == "2026-10-07" for c in cl.of("stock_daily"))   # last published at 16:20 ET
    assert _clean == ["AAA", "BBB"]                           # earnings (Yahoo) per ticker
    assert col.last_eod_on == DAY and _status(db)["last_eod_on"] == DAY
    assert _status(db)["phase_detail"] == "end-of-day 2026-10-08 done (2 tickers)"
    snaps = db.query(models.OptionChainSnapshot).filter_by(snap_on=DAY, kind="eod").all()
    assert len(snaps) == 2 * N_ROWS and {s.source for s in snaps} == {"massive"}
    assert {lg.kind for lg in _logs(db)} == {"eod"}
    db.expire_all()
    assert opt_store.underlying(db, "AAA")["earnings_date"] == "2026-10-28"

    for t in (_dt.datetime(2026, 10, 8, 20, 35), _dt.datetime(2026, 10, 9, 0, 0),
              _dt.datetime(2026, 10, 9, 12, 0)):                   # 16:35, 20:00, Fri 08:00 ET
        clk.t = t
        assert col.tick() == "idle"
    assert len(cl.read()) == 2 and len(cl.of("stock_daily")) == 2   # exactly once

    clk.t = _dt.datetime(2026, 10, 9, 20, 20)                # Friday 16:20 ET: the next day's
    col.tick()
    assert col.last_eod_on == "2026-10-09" and len(cl.of("stock_daily")) == 4


def test_eod_pass_a_few_symbols_per_tick(db, user, build):
    _basket(db, user, "AAA", "BBB", "CCC")
    _history_done(db, "AAA", "BBB", "CCC")
    clk = Clock(EOD)
    col, cl = build(clk, batch=1)
    assert col.tick() == "eod" and cl.read() == ["AAA"] and col.last_eod_on is None
    st = _status(db)
    assert (st["state"], st["symbols_done"], st["symbols_total"]) == ("eod", 1, 3)
    clk.advance(seconds=15)
    col.tick()
    assert col.last_eod_on is None
    clk.advance(seconds=15)
    assert col.tick() == "idle" and col.last_eod_on == DAY
    assert cl.read() == ["AAA", "BBB", "CCC"]


def test_missed_eod_is_caught_up_before_the_next_open(db, user, build):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    opt_store.set_collector_status(db, last_eod_on="2026-10-06")
    clk = Clock(PRE)                                          # Thu 08:00 ET, Wed's pass missed
    col, cl = build(clk)
    col.tick()
    assert col.last_eod_on == "2026-10-07" and cl.read() == ["AAA"]
    assert db.query(models.OptionChainSnapshot).filter_by(snap_on="2026-10-07").count() == N_ROWS

    # a weekend: Friday's pass
    clk.t = SAT
    col.last_eod_on = DAY
    col.tick()
    assert col.last_eod_on == "2026-10-09"


def test_eod_survives_a_restart(db, user, build):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(EOD)
    col, cl = build(clk)
    col.tick()
    assert col.last_eod_on == DAY
    col.stop("restart")
    clk.t = _dt.datetime(2026, 10, 8, 21, 0)
    col2, cl2 = build(clk)
    assert col2.tick() == "idle" and col2.last_eod_on == DAY and cl2.calls == []


def test_eod_waits_while_every_request_is_paused(db, user, build):
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    clk = Clock(EOD)
    col, cl = build(clk)
    cl.fail[("chain_snapshot", None)] = MassiveError("network", "could not reach Massive for the "
                                                     "options chain snapshot (ConnectError: down)")
    assert col.tick() == "error"
    assert cl.read() == ["AAA"] and cl.of("stock_daily") == [] and col.last_eod_on is None
    del cl.fail[("chain_snapshot", None)]
    clk.advance(seconds=61)
    assert col.tick() == "idle" and col.last_eod_on == DAY
    assert cl.read() == ["AAA", "AAA", "BBB"]                 # AAA again from the start


def test_eod_stock_bars_count_only_when_complete_and_are_retried(db, user, build, monkeypatch):
    """A symbol's end-of-day stock bars are done only when ``daily_update`` worked AND its
    bars include the session it asked for (``complete``). Bars not published yet, a
    failure, a refusal (HTTP 403 plan) or a pause leave them owed: retried outside the
    session - the next tick, then after 5 min doubling - while the day's chain snapshot
    stays done (never read again) and the day counts as done."""
    _basket(db, user, "AAA", "BBB", "CCC")
    _history_done(db, "AAA", "BBB", "CCC")
    plan = "your Massive plan does not include stock daily bars (HTTP 403)"
    script = {"AAA": ["late", "late", "ok"], "BBB": ["http", "plan", "ok"], "CCC": ["ok"]}
    calls: list[str] = []

    def daily_update(db_, client, symbol, *, today=None, now=None):
        calls.append(symbol)
        what = script[symbol].pop(0) if len(script[symbol]) > 1 else script[symbol][0]
        if what == "http":
            raise MassiveError("http", "Massive answered HTTP 502 for stock daily bars", 502)
        if what == "plan":
            raise MassiveError("plan", plan, 403)
        return {"symbol": symbol, "bars": 7, "ms": 1, "complete": what == "ok"}

    monkeypatch.setattr(opt_massive, "daily_update", daily_update)
    clk = Clock(EOD)
    col, cl = build(clk)

    assert col.tick() == "idle"                               # the pass: AAA not out yet, BBB failed
    assert calls == ["AAA", "BBB", "CCC"] and col.last_eod_on == DAY
    st = _status(db)
    assert st["last_eod_on"] == DAY
    assert st["phase_detail"] == "end-of-day 2026-10-08 done (3 tickers; stock bars of 2 tickers still to come)"
    assert db.query(models.OptionChainSnapshot).filter_by(snap_on=DAY, kind="eod").count() == 3 * N_ROWS

    clk.advance(seconds=15)
    assert col.tick() == "error"                              # the next tick: both retried, BBB refused
    assert calls[3:] == ["AAA", "BBB"]
    st = _status(db)
    assert st["phase_detail"] == (plan + " - end-of-day stock bars paused, the rest carries on; "
                                  "next try 16:25 ET")
    clk.advance(seconds=15)
    col.tick()                                                # AAA's 5 min back-off, the pause: nothing
    assert len(calls) == 5

    clk.t = EOD + _dt.timedelta(minutes=5, seconds=30)
    assert col.tick() == "idle"
    assert calls[5:] == ["AAA", "BBB"]                        # both in now
    assert _status(db)["phase_detail"] == "end-of-day 2026-10-08 done; next session 2026-10-09 09:30 ET"
    for t in (EOD + _dt.timedelta(hours=1), _dt.datetime(2026, 10, 9, 12, 0)):
        clk.t = t
        col.tick()
    assert len(calls) == 7 and cl.read() == ["AAA", "BBB", "CCC"]    # the chains never read again

    # a refusal in the pass pauses the bars of the rest: they are owed, not skipped for the day
    script.update(AAA=["plan", "ok"], BBB=["ok"], CCC=["ok"])
    clk.t = _dt.datetime(2026, 10, 9, 20, 20)                 # Friday 16:20 ET
    assert col.tick() == "error" and col.last_eod_on == "2026-10-09"
    assert calls[7:] == ["AAA"]
    assert "stock bars of 3 tickers still to come" in col.detail
    clk.t = _dt.datetime(2026, 10, 9, 20, 25, 30)
    assert col.tick() == "idle" and calls[8:] == ["AAA", "BBB", "CCC"]
    assert len(cl.read()) == 6 and col._bars_owed == {}       # noqa: SLF001


# ───────────────────────────────────────── errors ─────────────────────────────────────────

def test_symbol_errors_are_logged_and_the_pass_goes_on(db, user, build):
    _basket(db, user, "AAA", "BAD", "CCC")
    _history_done(db, "AAA", "BAD", "CCC")
    clk = Clock(RTH)
    col, cl = build(clk)
    cl.fail[("chain_snapshot", "BAD")] = MassiveError(
        "http", "Massive answered HTTP 404 for the options chain snapshot: not found", 404)
    cl.fail[("chain_snapshot", "CCC")] = RuntimeError("a bug in a parser")

    assert col.tick() == "idle"
    assert cl.read() == ["AAA", "BAD", "CCC"]
    assert _n_quotes(db, "AAA") == N_ROWS
    (bad,) = _logs(db, "BAD")
    assert (bad.kind, bad.n_contracts, bad.source) == ("cycle", 0, "massive") and "HTTP 404" in bad.error
    (ccc,) = _logs(db, "CCC")
    assert ccc.error == "a bug in a parser"
    st = _status(db)
    assert st["state"] == "idle" and st["phase_detail"] == "pass 1 finished at 10:00 ET (3/3 tickers, 2 failed)"
    assert st["last_error"] == "cycle CCC: a bug in a parser"


def test_a_rejected_key_pauses_everything_then_clears(db, user, build, tmp_path):
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    clk = Clock(RTH)
    col, cl = build(clk)
    cl.fail[("chain_snapshot", None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)

    assert col.tick() == "error"
    assert cl.read() == ["AAA"]                               # BBB not tried: every request would fail
    st = _status(db)
    assert st["last_error"] == "Massive rejected the API key (HTTP 401)"
    assert st["phase_detail"] == "Massive rejected the API key (HTTP 401); next try 10:05 ET"
    assert st["gateway_ok"] is False
    doc = _doc(tmp_path)
    assert (doc["error_kind"], doc["api_ok"]) == ("auth", False)
    (lg,) = _logs(db)
    assert lg.symbol == "AAA" and lg.error == "Massive rejected the API key (HTTP 401)"

    clk.advance(seconds=60)
    assert col.tick() == "error" and len(cl.read()) == 1

    del cl.fail[("chain_snapshot", None)]
    clk.t = RTH + _dt.timedelta(minutes=5)
    assert col.tick() == "idle"
    assert cl.read() == ["AAA", "AAA", "BBB"]                 # the paused symbol first, then the rest
    doc = _doc(tmp_path)
    assert (doc["state"], doc["error_kind"], doc["api_ok"]) == ("idle", None, True)


def test_a_plan_error_pauses_only_that_part(db, user, build, tmp_path):
    _basket(db, user, "NEW", "OLD")
    _history_done(db, "OLD")
    clk = Clock(RTH)
    col, cl = build(clk, cycle_min=5)
    reason = "your Massive plan does not include stock daily bars (HTTP 403)"
    cl.fail[("stock_daily", None)] = MassiveError("plan", reason, 403)

    assert col.tick() == "idle" and cl.read() == ["NEW", "OLD"]     # the due pass first
    clk.advance(seconds=15)
    assert col.tick() == "error"                              # then NEW's history: refused
    st = _status(db)
    assert st["last_error"] == reason
    assert st["phase_detail"] == reason + " - history reads paused, the rest carries on; next try 10:05 ET"
    assert _doc(tmp_path)["error_kind"] == "plan"

    clk.t = RTH + _dt.timedelta(minutes=5)                    # history still paused (10:05:15) ...
    assert col.tick() == "error"
    assert cl.read() == ["NEW", "OLD", "NEW", "OLD"]          # ... the session pass reads the chains
    assert len(cl.of("stock_daily")) == 1                     # history not retried before 5 min

    del cl.fail[("stock_daily", None)]
    clk.t = RTH + _dt.timedelta(minutes=5, seconds=15)
    assert col.tick() == "history"                            # retried, worked: the error is gone
    assert _doc(tmp_path)["error_kind"] is None
    db.expire_all()
    assert opt_store.underlying(db, "NEW")["history_done"] is True


def test_massive_unreachable_backs_off_60_s_doubling(db, user, build):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(RTH)
    col, cl = build(clk)
    cl.fail[("chain_snapshot", None)] = MassiveError("network", "could not reach Massive for the "
                                                     "options chain snapshot (ConnectError: refused)")
    tried = []
    for _ in range(int(8 * 60 / 15)):                         # 8 minutes of ticks
        col.tick()
        tried.append(len(cl.read()))
        clk.advance(seconds=15)
    # tries at 0 s, 60 s, 180 s (+120), 420 s (+240)
    assert [i * 15 for i in range(1, len(tried)) if tried[i] > tried[i - 1]] == [60, 180, 420]
    assert col.shown_state == "error" and col.error_kind() == "network"


def test_the_key_never_reaches_the_db_the_state_file_or_the_log(db, user, build, tmp_path, caplog,
                                                                monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    clk = Clock(RTH)
    col, cl = build(clk)
    cl.fail[("chain_snapshot", "AAA")] = RuntimeError(
        "GET https://api.massive.test/v3/snapshot/options/AAA?apiKey=%s failed" % KEY)
    cl.fail[("chain_snapshot", "BBB")] = MassiveError("http", "Massive answered HTTP 500: token %s" % KEY, 500)
    caplog.set_level(logging.DEBUG)
    col.tick()
    col.stop("done")
    texts = [lg.error or "" for lg in _logs(db)]
    assert len(texts) == 2 and all("***" in t for t in texts)
    assert all(KEY not in t for t in texts)
    st = _status(db)
    assert KEY not in (st["last_error"] or "") and KEY not in (st["phase_detail"] or "")
    assert KEY not in (tmp_path / "state" / "options_collector.json").read_text(encoding="utf-8")
    assert KEY not in caplog.text


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


TRAY_STATES = {"running", "idle", "history", "eod", "error", "stopped", "stale", "absent"}
NOT_IN_TRAY = ("gateway", "gw ", "blackout", "waiting", "supervisor", "ibkr")


def _tray_clean(got):
    assert got["state"] in TRAY_STATES, got
    low = (got["line"] + " " + got["tip"]).lower()
    assert not any(w in low for w in NOT_IN_TRAY), got["line"]


def test_state_file_and_the_tray_read_together(db, user, build, tmp_path):
    _basket(db, user, "AAA", "BBB")
    _history_done(db, "AAA", "BBB")
    clk = Clock(RTH)
    col, cl = build(clk, batch=1)
    col.tick()
    path = tmp_path / "state" / "options_collector.json"
    status = _tray_function(path)
    got = status(now=_utc(RTH) + _dt.timedelta(seconds=20))
    _tray_clean(got)
    assert (got["state"], got["color"], got["level"], got["tip"]) == ("running", "green", "ok", "Opt p1")
    assert "pass 1" in got["line"] and "1/2" in got["line"] and "Massive delayed" in got["line"]
    assert "hb 20s ago" in got["line"] and got["api_ok"] is True

    got = status(now=_utc(RTH) + _dt.timedelta(minutes=6))  # heartbeat older than 5 min
    _tray_clean(got)
    assert (got["state"], got["color"]) == ("stale", "amber") and "NO HEARTBEAT" in got["line"]
    assert got["tip"].startswith("Opt stale")

    cl.fail[("chain_snapshot", None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)
    clk.advance(seconds=15)
    col.tick()
    got = status(now=_utc(clk.t))
    _tray_clean(got)
    assert (got["state"], got["color"], got["tip"]) == ("error", "amber", "Opt ERR")
    assert "Massive rejected the API key" in got["line"] and "Massive failing" in got["line"]

    col.stop("test stop")
    got = status(now=_utc(clk.t))
    _tray_clean(got)
    assert (got["state"], got["color"], got["tip"]) == ("stopped", "amber", "Opt stopped")


def test_tray_status_function_cases(tmp_path):
    p = tmp_path / "options_collector.json"
    status = _tray_function(p)
    now = _dt.datetime(2026, 10, 8, 14, 0, tzinfo=_dt.timezone.utc)

    got = status(now=now)                                    # no file: grey
    _tray_clean(got)
    assert (got["state"], got["level"], got["color"], got["tip"]) == ("absent", "absent", "grey", "Opt -")

    base = {"state": "cycle", "cycle_n": 12, "symbols_done": 18, "symbols_total": 30, "mdt": "delayed",
            "api_ok": True, "last_eod_on": "2026-10-07", "heartbeat": "2026-10-08T13:59:40+00:00",
            "phase_detail": "pass 12: LRCX (18/30)", "last_error": None, "history_pending": 0}
    cases = [
        ({}, "running", "green", "Opt p12", ("pass 12", "18/30", "Massive delayed", "EOD 2026-10-07", "hb 20s ago")),
        ({"state": "idle"}, "idle", "green", "Opt idle", ("idle", "pass 12")),
        ({"state": "history", "symbols_done": 3}, "history", "green", "Opt history", ("history", "3/30")),
        ({"state": "eod", "symbols_done": 5}, "eod", "green", "Opt eod", ("eod", "5/30")),
        ({"state": "starting", "cycle_n": 0}, "running", "green", "Opt running", ("running",)),
        ({"state": "error", "phase_detail": "TST_MASSIVE_API_KEY is not set on this PC; next try 10:05 ET"},
         "error", "amber", "Opt ERR", ("ERROR", "TST_MASSIVE_API_KEY is not set on this PC")),
        ({"state": "error", "api_ok": False, "phase_detail": "Massive rejected the API key (HTTP 401)"},
         "error", "amber", "Opt ERR", ("Massive failing", "rejected")),
        ({"state": "stopped", "phase_detail": "one-off --once run finished"}, "stopped", "amber",
         "Opt stopped", ("STOPPED", "one-off")),
        ({"state": "idle", "history_pending": 3}, "idle", "green", "Opt idle", ("history pending (3)",)),
        ({"state": "waiting"}, "idle", "green", "Opt idle", ()),      # an old file's word: just alive
    ]
    for change, state, color, tip, needles in cases:
        p.write_text(json.dumps(dict(base, **change)), encoding="utf-8")
        got = status(now=now)
        _tray_clean(got)
        assert (got["state"], got["color"], got["tip"]) == (state, color, tip), change
        for n in needles:
            assert n in got["line"], (change, n, got["line"])

    p.write_text(json.dumps(dict(base, heartbeat="2026-10-08T13:50:00Z")), encoding="utf-8")
    got = status(now=now)                                    # 10 min old
    _tray_clean(got)
    assert (got["state"], got["color"]) == ("stale", "amber") and "10m" in got["line"]

    p.write_text("{not json", encoding="utf-8")
    got = status(now=now)
    _tray_clean(got)
    assert (got["state"], got["color"], got["tip"]) == ("error", "amber", "Opt ?")


def test_tray_wires_the_line_into_tooltip_and_window():
    src = TRAY.read_text(encoding="utf-8")
    compile(src, str(TRAY), "exec")
    tree = ast.parse(src)
    callers = set()
    fn_src = ""
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            if fn.name == "get_options_collector_status":
                fn_src = ast.get_source_segment(src, fn) or ""
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "get_options_collector_status"):
                    callers.add(fn.name)
    assert {"_update_loop", "_build_progress_window"} <= callers
    code = "\n".join(ln for ln in fn_src.splitlines() if not ln.lstrip().startswith("#"))
    for word in ("Gateway", "GW ", "blackout", "supervisor", "wait_reason"):
        assert word not in code, word
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
        col, cl = build(clk)
        col.tick()                                  # the pass: NEW, OLD
        clk.advance(seconds=15)
        col.tick()                                  # history NEW (quoted already: no chain)
        clk.t = EOD
        col.tick()                                  # end of day: NEW, OLD + prune
        col.stop("done")
    finally:
        event.remove(Session, "before_flush", before_flush)
        event.remove(Session, "do_orm_execute", on_execute)

    assert cl.read() == ["NEW", "OLD", "NEW", "OLD"] and len(_bar_reads(cl)) == 3
    assert col.last_eod_on == DAY
    assert violations == []
    assert tables <= ALLOWED_TABLES
    assert ALLOWED_TABLES <= tables
    db.expire_all()
    assert db.query(models.OptionBasket).count() == n_basket


# ───────────────────────────────────────── run modes ─────────────────────────────────────────

def test_run_forever_ticks_sleeps_and_stops(db, user, build, tmp_path):
    slept: list[float] = []
    clk = Clock(RTH)
    col, cl = build(clk, sleep=slept.append)
    col.run_forever(max_ticks=3)
    assert slept == [15.0, 15.0]
    assert col.state == "stopped" and _status(db)["state"] == "stopped"
    assert _doc(tmp_path)["state"] == "stopped"


def test_run_once_history_and_eod_now(db, user, build):
    _basket(db, user, "NEW", "OLD")
    _history_done(db, "OLD")
    clk = Clock(SAT)                                          # one-off runs ignore the clock
    col, cl = build(clk)
    cl.bars_end = "2026-10-09"                                # Friday's bar is out by Saturday

    out = col.run_once()
    assert out == {"ok": True, "symbols": 2, "history": 1, "history_failed": 0, "quoted": 1, "failed": 0}
    assert cl.read() == ["NEW", "OLD"]                        # NEW is not read twice
    assert col.cycle_n == 1

    out = col.run_history(["old", "OLD"])
    assert out == {"ok": True, "symbols": 1, "done": 1, "failed": 0}
    assert [c[1] for c in _bar_reads(cl)] == ["NEW", "OLD"]

    out = col.run_eod()
    assert out == {"ok": True, "day": "2026-10-09", "symbols": 2, "quoted": 2,
                   "snapshot_rows": 2 * N_ROWS, "bars": 2, "failed": 0}
    assert col.last_eod_on == "2026-10-09" and _status(db)["last_eod_on"] == "2026-10-09"


def test_one_off_runs_report_a_missing_or_rejected_key(db, user, build):
    _basket(db, user, "AAA")
    _history_done(db, "AAA")
    clk = Clock(RTH)
    made: list[FakeMassive] = []

    def factory():
        made.append(FakeMassive(clk, key=False))
        return made[-1]

    col, _ = build(clk, client_factory=factory)
    for out in (col.run_once(), col.run_history(["AAA"]), col.run_eod()):
        assert out["ok"] is False and out["error"] == opt_collector.NO_KEY_TEXT
        assert out["error_kind"] == "config"
    assert all(c.calls == [] for c in made) and len(made) == 3   # each run looks for the key at once
    assert col.last_eod_on is None

    col2, cl = build(clk)
    cl.fail[("chain_snapshot", None)] = MassiveError("auth", "Massive rejected the API key (HTTP 401)", 401)
    out = col2.run_once()
    assert out["ok"] is False and out["error_kind"] == "auth" and len(cl.read()) == 1
    out = col2.run_eod()
    assert out["ok"] is False and col2.last_eod_on is None    # a stopped pass does not count as done


# ───────────────────────────────────────── CLI + task script ─────────────────────────────────────────

def _load_cli():
    spec = importlib.util.spec_from_file_location("options_collector_cli_under_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def logger_levels():
    """The CLI's _logging sets levels on named loggers; put them back afterwards."""
    names = ("options_collector", "app.services.opt_collector", "app.services.opt_massive",
             "app.services.massive", "app.services.opt_store", "app.services.option_store",
             "app.services.prices", "httpx", "httpcore", "urllib3", "hpack")
    saved = {n: (logging.getLogger(n).level, logging.getLogger(n).disabled) for n in names}
    yield
    for n, (lvl, dis) in saved.items():
        logging.getLogger(n).setLevel(lvl)
        logging.getLogger(n).disabled = dis


class _FakeCollector:
    result: dict = {"ok": True}
    made: list = []

    def __init__(self, session_factory, **kw):
        self.kw = kw
        self.cycle_min = 15
        self.state_path = Path("state/options_collector.json")
        self.ran: list[str] = []
        self.stopped = None
        _FakeCollector.made.append(self)

    def run_forever(self):
        self.ran.append("forever")

    def run_once(self):
        self.ran.append("once")
        return dict(self.result)

    def run_history(self, syms):
        self.ran.append("history:" + ",".join(syms))
        return dict(self.result)

    def run_eod(self):
        self.ran.append("eod")
        return dict(self.result)

    def stop(self, reason="stopped"):
        self.stopped = reason


def test_cli_arguments_modes_and_exit_codes(monkeypatch, logger_levels):
    cli = _load_cli()
    a = cli.parse_args([])
    assert not (a.once or a.history or a.eod_now or a.forever) and a.log_file is None
    assert cli.parse_args(["--history", "NVDA", "LRCX"]).history == ["NVDA", "LRCX"]
    assert cli.parse_args(["--once", "-v"]).verbose is True
    for bad in (["--once", "--eod-now"], ["--port", "4002"], ["--client-id", "89"], ["--ignore-ingest"]):
        with pytest.raises(SystemExit):
            cli.parse_args(bad)
    assert (cli.EXIT_OK, cli.EXIT_SETUP, cli.EXIT_SOURCE) == (0, 1, 2)

    monkeypatch.setitem(sys.modules, "ib_insync", None)       # importing it would fail: never needed
    monkeypatch.setattr(opt_collector, "Collector", _FakeCollector)
    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    try:
        _FakeCollector.made.clear()
        _FakeCollector.result = {"ok": True}
        assert cli.main(["--no-init"]) == 0
        assert cli.main(["--no-init", "--once"]) == 0
        assert cli.main(["--no-init", "--history", "NVDA", "LRCX"]) == 0
        _FakeCollector.result = {"ok": False, "error": opt_collector.NO_KEY_TEXT}
        assert cli.main(["--no-init", "--eod-now"]) == 2
        ran = [c.ran for c in _FakeCollector.made]
        assert ran == [["forever"], ["once"], ["history:NVDA,LRCX"], ["eod"]]
        stops = [c.stopped for c in _FakeCollector.made]
        assert stops[0] is None and stops[1] == "one-off --once run finished"
    finally:
        root.setLevel(level)
        for h in list(root.handlers):
            for x in [x for x in h.filters if isinstance(x, cli.KeyScrub)]:
                h.removeFilter(x)
            if h not in before:
                root.removeHandler(h)


def test_cli_masks_the_key_and_quiets_per_request_http_lines(monkeypatch, logger_levels):
    cli = _load_cli()
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    f = cli.KeyScrub()
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed with %s", (KEY,), None)
    assert f.filter(rec) and KEY not in rec.getMessage() and "***" in rec.getMessage()
    try:
        raise RuntimeError("boom " + KEY)
    except RuntimeError:
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "crash", None, sys.exc_info())
    f.filter(rec)
    assert KEY not in logging.Formatter().format(rec) and "boom ***" in rec.exc_text

    root = logging.getLogger()
    level, before = root.level, list(root.handlers)
    try:
        cli._logging(logging.DEBUG)
        cli._logging(logging.DEBUG)                 # called again after init_db: one filter each
        for h in root.handlers:
            assert sum(isinstance(x, cli.KeyScrub) for x in h.filters) == 1
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert logging.getLogger("app.services.opt_collector").level == logging.DEBUG
    finally:
        root.setLevel(level)
        for h in list(root.handlers):
            for x in [x for x in h.filters if isinstance(x, cli.KeyScrub)]:
                h.removeFilter(x)
            if h not in before:
                root.removeHandler(h)


def test_cli_log_file_rotates_and_survives_the_alembic_reset(tmp_path, monkeypatch, logger_levels):
    """--log-file writes through a RotatingFileHandler (5 MB x 5); the second _logging
    call (after init_db's fileConfig) puts the handler back once, and drops the stderr
    console Alembic adds."""
    cli = _load_cli()
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
        assert sum(isinstance(x, cli.KeyScrub) for x in fh.filters) == 1
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
            for x in [x for x in h.filters if isinstance(x, cli.KeyScrub)]:
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
    src = raw.decode("ascii")                    # PS 5.1 reads a BOM-less file as ANSI
    for needle in ('"TST-Options-Collector"', "options_collector.py", "--forever",
                   "options_collector.log", "-AtStartup", "-Daily -At $At", '"07:00"',
                   "-RestartCount 3", "-RestartInterval (New-TimeSpan -Minutes 5)",
                   "-MultipleInstances IgnoreNew", "-ExecutionTimeLimit ([TimeSpan]::Zero)",
                   "[switch] $StartNow", "Start-ScheduledTask", ".venv\\Scripts\\python.exe",
                   "--log-file", "TST_MASSIVE_API_KEY", "-Quiet"):
        assert needle in src, needle
    assert "&&" not in src
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert ">>" not in code and "cmd.exe" not in code     # the collector rotates its own log
    low = src.lower()
    for word in ("py -3.12", "ib_insync", "clientid", "client id", "gateway", "blackout", "4002"):
        assert word not in low, word
