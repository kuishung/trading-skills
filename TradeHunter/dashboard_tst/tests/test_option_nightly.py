"""The nightly job (``services/option_nightly.py``) on a fake ``ChainSource`` and
stubbed engines, the on-demand Refresh, and bridge 1.6's ``/iv?series=1`` on
stubbed IBKR bars.

No network: the chain comes from ``chain_bs`` through ``option_data.chain_from_legacy``
(so the real parser, metrics and store run), Yahoo is replaced on ``prices``, the
engines (``chart_state`` / ``option_engine`` / ``option_exits``) are stub modules
dropped into ``sys.modules`` because they land with another step, and the Telegram
push is a recording stub. Every DB test runs on a fresh SQLite file at the Alembic
head (conftest).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from app import models
from app import services as services_pkg
from app.services import (clock, job_runs, option_data, option_nightly, option_prefs,
                          option_store, prices, telegram_push)
from app.services.option_quotes import ChainError

from .fixtures.options import bars_synth, chain_bs

RUN_ON = "2026-10-02"                      # a Friday; the chain's own date too
EXPIRIES = ("2026-10-30", "2026-11-20", "2026-12-18")
STRIKES = tuple(range(80, 125, 5))
HOUSE = option_prefs.HOUSE_HASH
TODAY = clock.et_today()                   # run_on without --on: the job row's date


# ───────────────────────────────────── doubles ─────────────────────────────────────

class FakeSource:
    """A ``ChainSource`` that prices a Black-Scholes chain per symbol and raises
    ``ChainError`` for the names in ``bad`` (Cboe's 403 on a typo)."""

    name = "fake"
    capabilities = option_data.Capabilities(has_iv30=True, has_oi=True, has_greeks=True,
                                            has_rho=True, delayed_minutes=15, all_expiries=True,
                                            server_side=True, pacing_seconds=0.0)

    def __init__(self, bad=("ZZZZ",), spot=100.0):
        self.calls: list[tuple] = []
        self.bad = set(bad)
        self.spot = spot

    def fetch_chain(self, symbol, *, fresh=False, retries=0):
        self.calls.append((symbol, fresh, retries))
        if symbol in self.bad:
            raise ChainError(f"{symbol}: no chain (403)")
        raw = chain_bs(self.spot, 0.40, EXPIRIES, STRIKES, today=RUN_ON, symbol=symbol, iv30=40.0)
        return option_data.chain_from_legacy(raw, source="cboe", kind="eod")


def _sig(symbol: str, metrics: dict) -> dict:
    return {
        "status": "ok", "headline": f"{symbol} test headline",
        "setup": {"kind": "support_bounce", "direction": "long", "atr": 2.5,
                  "plan": {"stop": 95.0, "target": 110.0}, "stop": 95.0, "rng": {"sideways": False}},
        "iv": {"iv30": metrics.get("iv30"), "basis": metrics.get("iv_basis"),
               "earnings_date": metrics.get("earnings_date")},
        "strategies": [{"key": "bull_put", "label": "Bull put spread", "fit": "recommended",
                        "score": 90.1, "step": 1, "reasons": [], "reason_key": None, "shown": True}],
        "picks": {"bull_put": [{"symbol": symbol, "strategy": "bull_put", "family": "credit_vertical",
                                "status": "ok", "legs": [], "sizing": None}]},
        "computed_ms": 3, "engine_version": "t-1",
    }


@pytest.fixture
def calls():
    return []


@pytest.fixture
def engines(monkeypatch, calls):
    """Stub ``chart_state`` / ``option_engine`` / ``option_exits`` into the package;
    ``BOOM`` makes the engine raise so the per-hash error path is exercised."""
    cs = types.ModuleType("app.services.chart_state")

    def read(symbol, *, bars, long_bars, today, expiries):
        calls.append(("chart", symbol))
        return {"symbol": symbol, "today": today, "expiries": list(expiries),
                "n_bars": len(bars), "n_long": len(long_bars)}

    cs.read = read
    oe = types.ModuleType("app.services.option_engine")
    oe.ENGINE_VERSION = "t-1"

    def compute(chain, metrics, state, prefs):
        calls.append(("compute", chain.symbol, state["symbol"]))
        assert isinstance(prefs, dict) and "credit_vertical" in prefs
        if chain.symbol == "BOOM":
            raise RuntimeError("picker exploded")
        return _sig(chain.symbol, metrics)

    oe.compute = compute
    ox = types.ModuleType("app.services.option_exits")

    def sweep(db):
        calls.append(("sweep",))
        return {"checked": 0}

    ox.sweep = sweep
    for short, mod in (("chart_state", cs), ("option_engine", oe), ("option_exits", ox)):
        monkeypatch.setitem(sys.modules, "app.services." + short, mod)
        monkeypatch.setattr(services_pkg, short, mod, raising=False)
    monkeypatch.setattr(option_nightly, "_engines_warned", False)
    return calls


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(prices, "fetch_daily_ohlc",
                        lambda sym, *, rng="2y": bars_synth("uptrend_bounce", n=400, start=100.0, end=RUN_ON))
    monkeypatch.setattr(prices, "fetch_next_earnings", lambda sym: {"date": "2026-10-22", "days": 20})


@pytest.fixture
def recorder(monkeypatch, calls):
    """Wrap the store / ledger / push entry points so the step order is observable."""
    real_snapshot, real_iv, real_prune, real_finish = (option_store.replace_snapshot,
                                                       option_store.upsert_iv_daily,
                                                       option_store.prune, job_runs.finish)

    def snapshot(db, chain):
        calls.append(("snapshot", chain.symbol))
        return real_snapshot(db, chain)

    def iv_daily(db, chain, metrics=None):
        calls.append(("iv_daily", chain.symbol))
        return real_iv(db, chain, metrics)

    def prune(db, today=None):
        calls.append(("prune", today))
        return real_prune(db, today)

    def finish(db, run, **kw):
        calls.append(("finish", run.job))
        return real_finish(db, run, **kw)

    pushes = []

    def push(db, *, as_of, dry_run=False):
        calls.append(("push", as_of, dry_run))
        pushes.append((as_of, dry_run))
        return {"members": 1, "ideas": 2, "sent": 0 if dry_run or len(pushes) > 1 else 2,
                "failed": 0, "skipped": {}}

    monkeypatch.setattr(option_store, "replace_snapshot", snapshot)
    monkeypatch.setattr(option_store, "upsert_iv_daily", iv_daily)
    monkeypatch.setattr(option_store, "prune", prune)
    monkeypatch.setattr(job_runs, "finish", finish)
    monkeypatch.setattr(telegram_push, "run", push)
    return calls


def _no_sleep(_s):
    pass


def _run(db, src, **kw):
    kw.setdefault("sleep", _no_sleep)
    return option_nightly.run_nightly(db, source=src, **kw)


def _member_with_rules(db, user):
    """A saved rule set that differs from the house defaults -> a second hash."""
    prefs, errors = option_prefs.write(db, user, "credit", {"credit_vertical.short_delta_hi": "0.35"})
    assert errors == []
    h = option_prefs.prefs_hash(prefs)
    assert h != HOUSE
    return h


# ───────────────────────────────── the seven-step order ─────────────────────────────────

def test_seven_step_order_with_a_hash_per_rule_set(db, user, engines, no_network, recorder):
    mine = _member_with_rules(db, user)
    src = FakeSource()
    res = _run(db, src, symbols=["AAA", "BBB"])

    assert res["ok"] == 2 and res["errors"] == 0 and res["symbols"] == 2
    assert res["engines"] == "on" and res["signals"] == 4 and res["engine_errors"] == 0
    assert res["pushed"] == 2 and res["telegram"]["ideas"] == 2
    assert [c for c in src.calls] == [("AAA", True, 3), ("BBB", True, 3)]

    # per symbol: snapshot -> iv_daily -> ONE chart read -> compute per hash
    per_sym = [c for c in recorder if c[0] in ("snapshot", "iv_daily", "chart", "compute")]
    assert per_sym == [("snapshot", "AAA"), ("iv_daily", "AAA"), ("chart", "AAA"),
                       ("compute", "AAA", "AAA"), ("compute", "AAA", "AAA"),
                       ("snapshot", "BBB"), ("iv_daily", "BBB"), ("chart", "BBB"),
                       ("compute", "BBB", "BBB"), ("compute", "BBB", "BBB")]
    # then 4 sweep -> 5 prune -> 6 push -> 7 finish, in that order, each once
    tail = [c[0] for c in recorder if c[0] in ("sweep", "prune", "push", "finish")]
    assert tail == ["sweep", "prune", "push", "finish"]
    names = [c[0] for c in recorder]
    assert names.index("sweep") > names.index("compute") and names.index("push") > names.index("prune")
    assert [c for c in recorder if c[0] == "push"] == [("push", TODAY, False)]
    assert [c for c in recorder if c[0] == "prune"] == [("prune", TODAY)]

    # the rows: one snapshot per (symbol, day, eod), one iv_daily row, one signal per hash
    S = models.OptionChainSnapshot
    assert db.query(S).filter(S.symbol == "AAA", S.snap_on == RUN_ON, S.kind == "eod").count() == len(EXPIRIES) * len(STRIKES) * 2
    hdr = db.query(models.IVDaily).filter(models.IVDaily.symbol == "AAA").one()
    assert hdr.kind == "eod" and hdr.iv30 == 40.0 and hdr.iv30_src == "cboe" and hdr.hv20 is not None
    assert hdr.earnings_date == "2026-10-22"
    sigs = {(r.symbol, r.prefs_hash): r for r in db.query(models.OptionSignal).all()}
    assert set(sigs) == {("AAA", HOUSE), ("AAA", mine), ("BBB", HOUSE), ("BBB", mine)}
    assert all(r.status == "ok" and r.kind == "eod" and r.engine_version == "t-1" for r in sigs.values())
    assert sigs[("AAA", HOUSE)].headline == "AAA test headline"
    assert sigs[("AAA", HOUSE)].trend == "up"

    # the job row
    job = job_runs.latest(db, "nightly")
    assert job is not None and job.id == res["job_id"] and job.finished_at is not None
    assert (job.run_on, job.source, job.symbols, job.ok, job.errors, job.pushed) == (TODAY, "fake", 2, 2, 0, 2)
    assert job.rows == res["rows"] > 0
    assert job.detail["AAA"]["status"] == "ok" and job.detail["AAA"]["signals"] == 2
    assert job.detail["_engines"] == {"mode": "on", "hashes": [HOUSE, mine]}
    assert job.detail["_sweep"] == {"checked": 0} and "snapshots_old" in job.detail["_prune"]
    assert job.detail["_telegram"]["sent"] == 2
    assert "2/2 ok" in job.note


def test_rerun_same_day_is_idempotent(db, user, engines, no_network, recorder):
    src = FakeSource()
    first = _run(db, src, symbols=["AAA"])
    second = _run(db, src, symbols=["AAA"])
    S = models.OptionChainSnapshot
    assert db.query(S).filter(S.symbol == "AAA").count() == first["rows"] == second["rows"]
    assert db.query(models.IVDaily).filter(models.IVDaily.symbol == "AAA").count() == 1
    assert db.query(models.OptionSignal).filter(models.OptionSignal.symbol == "AAA").count() == 1
    jobs = db.query(models.OptionJob).filter(models.OptionJob.job == "nightly").all()
    assert len(jobs) == 2 and all(j.finished_at is not None for j in jobs)
    assert second["pushed"] == 0                         # the push's dedupe found nothing new


def test_one_bad_ticker_is_isolated(db, user, engines, no_network, recorder):
    src = FakeSource(bad=("ZZZZ",))
    res = _run(db, src, symbols=["AAA", "ZZZZ", "BBB"])
    assert (res["ok"], res["errors"], res["symbols"]) == (2, 1, 3)
    assert [c[0] for c in src.calls] == ["AAA", "ZZZZ", "BBB"]        # the loop went on
    d = res["detail"]["ZZZZ"]
    assert d["status"] == "error" and "403" in d["err"] and d["n"] == 0
    assert res["detail"]["BBB"]["status"] == "ok"
    assert db.query(models.OptionSignal).filter(models.OptionSignal.symbol == "ZZZZ").count() == 0
    job = job_runs.latest(db, "nightly")
    assert (job.ok, job.errors, job.symbols) == (2, 1, 3)
    assert [c[0] for c in recorder if c[0] in ("sweep", "prune", "push", "finish")] == ["sweep", "prune", "push", "finish"]


def test_engine_failure_writes_an_error_row_and_keeps_the_data(db, user, engines, no_network, recorder):
    res = _run(db, FakeSource(), symbols=["BOOM"])
    assert res["ok"] == 1 and res["errors"] == 0            # the DATA was stored
    assert res["engine_errors"] == 1 and res["signals"] == 1
    assert db.query(models.OptionChainSnapshot).filter(models.OptionChainSnapshot.symbol == "BOOM").count() > 0
    row = db.query(models.OptionSignal).filter(models.OptionSignal.symbol == "BOOM").one()
    assert row.status == "error" and "picker exploded" in row.error and row.picks is None


def test_engines_unavailable_degrades_to_snapshot_and_metrics(db, user, no_network, recorder, monkeypatch):
    for short in ("chart_state", "option_engine", "option_exits"):
        monkeypatch.setitem(sys.modules, "app.services." + short, None)     # import -> ImportError
        monkeypatch.delattr(services_pkg, short, raising=False)
    monkeypatch.setattr(option_nightly, "_engines_warned", False)
    res = _run(db, FakeSource(), symbols=["AAA"])
    assert res["engines"] == "unavailable" and res["signals"] == 0 and res["ok"] == 1
    assert db.query(models.OptionSignal).count() == 0
    assert db.query(models.IVDaily).filter(models.IVDaily.symbol == "AAA").count() == 1
    assert "unavailable" in res["swept"]["skipped"]
    assert [c[0] for c in recorder if c[0] in ("prune", "push", "finish")] == ["prune", "push", "finish"]


def test_no_engines_flag_and_no_push(db, user, engines, no_network, recorder):
    res = _run(db, FakeSource(), symbols=["AAA"], engines=False, push=False)
    assert res["engines"] == "off" and res["signals"] == 0
    assert not [c for c in recorder if c[0] in ("chart", "compute", "push")]
    assert res["telegram"] == {"skipped": {"no-push": 1}, "sent": 0} and res["pushed"] == 0
    assert [c[0] for c in recorder if c[0] in ("sweep", "prune", "finish")] == ["sweep", "prune", "finish"]


def test_telegram_dry_run_is_passed_through(db, user, engines, no_network, recorder):
    res = _run(db, FakeSource(), symbols=["AAA"], telegram_dry_run=True)
    assert [c for c in recorder if c[0] == "push"] == [("push", TODAY, True)]
    assert res["pushed"] == 0 and res["telegram"]["ideas"] == 2


def test_push_failure_is_soft(db, user, engines, no_network, recorder, monkeypatch):
    def boom(db, *, as_of, dry_run=False):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram_push, "run", boom)
    res = _run(db, FakeSource(), symbols=["AAA"])
    assert res["pushed"] == 0 and "telegram down" in res["telegram"]["error"]
    assert job_runs.latest(db, "nightly").finished_at is not None


def test_bad_source_name_raises_before_any_job_row(db):
    with pytest.raises(ChainError):
        option_nightly.run_nightly(db, source="nope", sleep=_no_sleep)
    assert db.query(models.OptionJob).count() == 0


def test_on_refiles_under_that_date(db, user, engines, no_network, recorder):
    res = _run(db, FakeSource(), symbols=["AAA"], on="2026-09-30")
    assert res["run_on"] == "2026-09-30"
    S = models.OptionChainSnapshot
    assert db.query(S).filter(S.symbol == "AAA", S.snap_on == "2026-09-30").count() == res["rows"]
    assert db.query(models.OptionSignal).one().snap_on == "2026-09-30"
    assert job_runs.latest(db, "nightly").run_on == "2026-09-30"


def test_universe_is_the_basket_plus_open_trades(db, user, engines, no_network, recorder):
    db.add(models.OptionBasket(user_id=user.id, owner_key=f"u{user.id}", symbol="AAA", source="typed",
                               active=True, added_on=RUN_ON, pos=0))
    db.add(models.OptionBasket(user_id=user.id, owner_key=f"u{user.id}", symbol="OLD", source="typed",
                               active=False, added_on=RUN_ON, pos=1))
    db.add(models.OptionTrade(user_id=user.id, symbol="BBB", strategy="bull_put", family="credit_vertical",
                              legs=[], front_expiry="2026-11-20", net_entry=-2.1, contracts=1,
                              max_loss=790.0, status="open", meta={}))
    db.commit()
    res = _run(db, FakeSource(), symbols=None)
    assert sorted(res["detail"][s]["status"] for s in ("AAA", "BBB")) == ["ok", "ok"]
    assert "OLD" not in res["detail"] and res["symbols"] == 2


def test_pacing_sleeps_between_fetches_not_before_the_first(db, user, engines, no_network, recorder):
    class Paced(FakeSource):
        capabilities = option_data.Capabilities(has_iv30=True, has_oi=True, has_greeks=True,
                                                has_rho=True, delayed_minutes=15, all_expiries=True,
                                                server_side=True, pacing_seconds=1.5)

    naps = []
    _run(db, Paced(), symbols=["AAA", "BBB", "CCC"], sleep=naps.append)
    assert naps == [1.5, 1.5]


def test_prefs_by_hash_house_first_then_saved(db, user):
    assert option_nightly.prefs_by_hash(db) == [(HOUSE, option_prefs.clean({}))]
    mine = _member_with_rules(db, user)
    got = option_nightly.prefs_by_hash(db)
    assert [h for h, _ in got] == [HOUSE, mine]
    assert got[1][1]["credit_vertical"]["short_delta_hi"] == 0.35


# ───────────────────────────────────── refresh ─────────────────────────────────────

def test_refresh_symbol_intraday_house_and_member_hash(db, user, engines, no_network):
    mine = _member_with_rules(db, user)
    d = option_nightly.refresh_symbol(db, "aaa", user, source=FakeSource())
    assert d["status"] == "ok" and d["symbol"] == "AAA" and d["hashes"] == [HOUSE, mine]
    S = models.OptionChainSnapshot
    assert db.query(S).filter(S.symbol == "AAA", S.kind == "intraday").count() == d["n"] > 0
    assert db.query(S).filter(S.symbol == "AAA", S.kind == "eod").count() == 0
    hdr = db.query(models.IVDaily).filter(models.IVDaily.symbol == "AAA").one()
    assert hdr.kind == "intraday"
    rows = db.query(models.OptionSignal).filter(models.OptionSignal.symbol == "AAA").all()
    assert {(r.prefs_hash, r.kind) for r in rows} == {(HOUSE, "intraday"), (mine, "intraday")}
    job = job_runs.latest(db, "refresh")
    assert job is not None and job.id == d["job_id"] and (job.ok, job.errors, job.symbols, job.rows) == (1, 0, 1, d["n"])
    assert job.detail["_hashes"] == [HOUSE, mine] and job.detail["_user"] == user.id
    # retries 0: a page request never waits through a backoff
    assert [c for c in engines if c[0] == "chart"] == [("chart", "AAA")]


def test_refresh_missing_chain_is_an_error_row_not_a_raise(db, user, engines, no_network):
    d = option_nightly.refresh_symbol(db, "ZZZZ", user, source=FakeSource())
    assert d["status"] == "error" and "403" in d["err"]
    job = job_runs.latest(db, "refresh")
    assert (job.ok, job.errors) == (0, 1) and "403" in job.note


def test_refresh_cooldown(monkeypatch):
    monkeypatch.setattr(option_nightly, "_last_refresh", {})
    u = types.SimpleNamespace(id=7)
    assert option_nightly.refresh_allowed(u, "lrcx") == (True, 0)
    ok, left = option_nightly.refresh_allowed(u, "LRCX")
    assert not ok and 0 < left <= 60
    assert option_nightly.refresh_allowed(u, "LRCX", bypass=True) == (True, 0)   # the ticket's press
    assert option_nightly.refresh_allowed(u, "MSFT") == (True, 0)                # another ticker
    assert option_nightly.refresh_allowed(types.SimpleNamespace(id=8), "LRCX") == (True, 0)


# ────────────────────────────────── bridge 1.6 /iv series ──────────────────────────────────

def _bridge(monkeypatch):
    path = Path(__file__).resolve().parent.parent / "bridge" / "ibkr_bridge.py"
    spec = importlib.util.spec_from_file_location("ibkr_bridge_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setitem(sys.modules, "ib_insync", types.SimpleNamespace(Stock=lambda *a, **k: ("stock",) + a))
    return mod


class _Bar:
    def __init__(self, date, close):
        self.date, self.close = date, close


def _stub_ib(monkeypatch, mod, bars):
    class IB:
        async def qualifyContractsAsync(self, c):
            return [c]

        async def reqHistoricalDataAsync(self, *a, **kw):
            assert kw["whatToShow"] == "OPTION_IMPLIED_VOLATILITY" and kw["formatDate"] == 1
            return bars

    async def connect(ib):
        return None

    monkeypatch.setattr(mod, "worker", lambda: types.SimpleNamespace(ib=IB()))
    monkeypatch.setattr(mod, "_connect", connect)


def test_bridge_version_is_1_6(monkeypatch):
    mod = _bridge(monkeypatch)
    assert mod.Handler.server_version == "TradeHunterIBKRBridge/1.6"
    assert mod.IV_SERIES_MAX == 400


def test_bridge_iv_series_percent_oldest_first_capped(monkeypatch):
    mod = _bridge(monkeypatch)
    start = _dt.date(2025, 1, 1)
    bars = [_Bar(start + _dt.timedelta(days=i), 0.20 + i / 1000.0) for i in range(450)]
    bars.insert(5, _Bar(start + _dt.timedelta(days=5), 0.0))          # a dead bar is dropped
    _stub_ib(monkeypatch, mod, bars)

    plain = asyncio.run(mod._iv("LRCX"))
    assert "series" not in plain and plain["ok"] and plain["n"] == 450          # unchanged reply
    assert plain["iv_current"] == round(bars[-1].close * 100, 1)

    with_series = asyncio.run(mod._iv("LRCX", series=True))
    s = with_series["series"]
    assert len(s) == 400 and s[0]["on"] < s[-1]["on"]                           # oldest first, capped
    assert s[-1] == {"on": bars[-1].date.isoformat(), "iv": round(bars[-1].close * 100, 1)}
    assert all(0.1 <= p["iv"] <= 1000 for p in s) and s[-1]["iv"] == with_series["iv_current"]
    assert {k: v for k, v in with_series.items() if k != "series"} == plain      # additive only


def test_bridge_iv_series_on_the_short_history_reply_and_string_dates(monkeypatch):
    mod = _bridge(monkeypatch)
    bars = [_Bar(f"202601{d:02d}", 0.30 + d / 100.0) for d in range(1, 11)]       # raw YYYYMMDD
    _stub_ib(monkeypatch, mod, bars)
    out = asyncio.run(mod._iv("KO", series=True))
    assert out["iv_rank"] is None and "Not enough" in out["note"]
    assert out["series"][0] == {"on": "2026-01-01", "iv": 31.0} and len(out["series"]) == 10
