"""The Options Screener's data layer (OPTIONS_SCREENER_DESIGN.md §3): the screener DB's own
Alembic environment (``alembic_screener/``) and ``app/services/scr_store.py``.

Every test runs on a fresh SQLite file under ``tmp_path`` brought to head by the real
screener migrations (``screener_db.configure`` + ``init_screener_db``); the module is
pointed back at its default afterwards. No network.
"""
from __future__ import annotations

import ast
import datetime as _dt
import math
import random
import threading
import time

import pytest
from alembic import command
from sqlalchemy import inspect

from app import screener_db
from app.screener_models import (ScrContract, ScrPass, ScrStatus, ScrUnderlying,
                                 ScrUnderlyingDaily, ScrUniverse)
from app.services import scr_store

from .conftest import DASH_ROOT

HEAD = "b4e1f7c9d2a6"                 # scr_pass.partial (v4.137) on top of 8d2f4b6a1c37
STATUS_REV = "8d2f4b6a1c37"           # scr_status progress columns (v4.137) on top of 5c7a1d3e9b20
BASE_REV = "5c7a1d3e9b20"
TABLES = {"scr_contract", "scr_underlying", "scr_underlying_daily", "scr_universe", "scr_pass",
          "scr_status"}
NOW = _dt.datetime(2026, 10, 8, 14, 0)          # Thursday 10:00 ET (naive UTC)
MATP_SRC = DASH_ROOT.parent / "resources" / "MATP" / "scripts" / "daily_bounce_alert.py"


@pytest.fixture
def sdb(tmp_path):
    """A session on a fresh screener DB at the Alembic head."""
    screener_db.configure("sqlite:///" + (tmp_path / "screener.db").as_posix())
    screener_db.init_screener_db()
    s = screener_db.SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        screener_db.configure(None)


def _weekdays(end: _dt.date, n: int) -> list[str]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= _dt.timedelta(days=1)
    return list(reversed(out))


def _bars(days, closes, vol=1_000_000.0, rng=1.0):
    return [{"on": on, "open": c, "high": c + rng, "low": c - rng, "close": c, "volume": vol}
            for on, c in zip(days, closes)]


def _contract(expiry="2026-11-20", right="C", strike=100.0, **kw):
    r = {"expiry": expiry, "right": right, "strike": strike, "weekly": False, "price": 2.5,
         "last": 2.4, "chg_pct": 1.0, "volume": 10, "oi": 100, "iv": 0.3, "delta": 0.5,
         "gamma": 0.02, "theta": -0.03, "vega": 0.12, "last_trade": NOW}
    r.update(kw)
    return r


def _rows(db, sym):
    db.expire_all()
    return {(r.expiry, r.right, r.strike): r for r in db.query(ScrContract).filter(ScrContract.symbol == sym)}


# ───────────────────────────────────────── the migration ─────────────────────────────────────────

def test_migration_creates_the_tables_and_its_own_version_table(sdb, tmp_path):
    eng = screener_db.engine()
    names = set(inspect(eng).get_table_names())
    assert names == TABLES | {"alembic_version_screener"}
    assert "alembic_version" not in names                 # the main history is not touched
    from sqlalchemy import text  # noqa: PLC0415 - reading Alembic's own table in a test only
    with eng.connect() as c:
        assert c.execute(text("select version_num from alembic_version_screener")).scalar() == HEAD
    idx = {i["name"]: i["column_names"] for i in inspect(eng).get_indexes("scr_contract")}
    assert idx["ix_scr_contract_symbol"] == ["symbol"]
    uq = {u["name"]: u["column_names"] for u in inspect(eng).get_unique_constraints("scr_contract")}
    assert uq["uq_scr_contract"] == ["symbol", "expiry", "right", "strike"]
    screener_db.init_screener_db()                        # a second run is a no-op
    # the migration mirrors the models exactly
    from alembic.autogenerate import compare_metadata  # noqa: PLC0415
    from alembic.migration import MigrationContext  # noqa: PLC0415
    from app.screener_models import ScrBase  # noqa: PLC0415
    with eng.connect() as c:
        mc = MigrationContext.configure(c, opts={"compare_type": True,
                                                 "version_table": "alembic_version_screener"})
        assert compare_metadata(mc, ScrBase.metadata) == []
    command.downgrade(screener_db.alembic_config(), "base")
    assert set(inspect(screener_db.engine()).get_table_names()) == {"alembic_version_screener"}


STATUS_NEW = {"error_kind", "next_try", "warn", "universe_done", "earnings_on", "progress"}


def test_status_progress_migration_upgrade_and_downgrade(sdb):
    eng = screener_db.engine()
    cols = {c["name"] for c in inspect(eng).get_columns("scr_status")}
    assert STATUS_NEW <= cols
    # an older collector's row (none of the new fields) survives both ways
    scr_store.set_status(sdb, state="universe", detail="reading", universe_n=0, now=NOW)
    sdb.close()
    command.downgrade(screener_db.alembic_config(), BASE_REV)
    eng = screener_db.engine()
    cols = {c["name"] for c in inspect(eng).get_columns("scr_status")}
    assert not (STATUS_NEW & cols) and {"state", "detail", "heartbeat", "last_error"} <= cols
    from sqlalchemy import text  # noqa: PLC0415 - reading the downgraded table in a test only
    with eng.connect() as c:
        assert c.execute(text("select state, detail from scr_status where id = 1")).one() == ("universe", "reading")
        assert c.execute(text("select version_num from alembic_version_screener")).scalar() == BASE_REV
    screener_db.init_screener_db()                         # back to head: the columns again, empty
    s = screener_db.SessionLocal()
    try:
        st = scr_store.status(s)
        assert (st["state"], st["detail"]) == ("universe", "reading")
        assert all(st[k] is None for k in STATUS_NEW)
    finally:
        s.close()


def test_pass_partial_migration_upgrade_and_downgrade(sdb):
    eng = screener_db.engine()
    assert "partial" in {c["name"] for c in inspect(eng).get_columns("scr_pass")}
    pid = scr_store.start_pass(sdb, kind="eod", session="2026-10-07", n_symbols=3, now=NOW)
    scr_store.finish_pass(sdb, pid, n_ok=3, n_contracts=90, now=NOW)        # an older collector: NULL
    sdb.close()
    command.downgrade(screener_db.alembic_config(), STATUS_REV)
    eng = screener_db.engine()
    assert "partial" not in {c["name"] for c in inspect(eng).get_columns("scr_pass")}
    assert "progress" in {c["name"] for c in inspect(eng).get_columns("scr_status")}   # only its own column
    screener_db.init_screener_db()
    s = screener_db.SessionLocal()
    try:
        row = scr_store.last_pass(s, kind="eod")
        assert row["id"] == pid and row["partial"] is None
    finally:
        s.close()


def test_finish_pass_records_partial_and_last_pass_tells_them_apart(sdb):
    def eod(session, *, partial, n_contracts=50):
        pid = scr_store.start_pass(sdb, kind="eod", session=session, n_symbols=3, now=NOW)
        scr_store.finish_pass(sdb, pid, n_ok=3, n_contracts=n_contracts, now=NOW, partial=partial)
        return pid

    legacy = eod("2026-10-05", partial=None)                 # a v4.136 row: NULL = a complete list
    full = eod("2026-10-06", partial=False)
    part = eod("2026-10-07", partial=True)
    assert scr_store.last_pass(sdb, kind="eod")["partial"] is True
    assert scr_store.last_pass(sdb, kind="eod", partial=False)["id"] == full
    assert scr_store.last_pass(sdb, kind="eod", partial=True)["id"] == part
    eod("2026-10-08", partial=False, n_contracts=0)          # read nothing
    assert scr_store.last_pass(sdb, kind="eod", min_contracts=1, partial=False)["id"] == full
    sdb.query(ScrPass).filter(ScrPass.id == full).delete()
    sdb.commit()
    assert scr_store.last_pass(sdb, kind="eod", min_contracts=1, partial=False)["id"] == legacy
    # a later call without partial leaves the mark alone
    scr_store.finish_pass(sdb, part, n_symbols=4, finished=False)
    assert scr_store.last_pass(sdb, kind="eod", partial=True)["id"] == part


def test_pass_symbols_weighted_follows_pass_symbols(sdb):
    scr_store.upsert_universe(sdb, {"AAA": 3000, "BBB": 1200, "CCC": 500, "DDD": None}, now=NOW)
    pairs = scr_store.pass_symbols_weighted(sdb, now=NOW)
    assert pairs == [("AAA", 3000), ("BBB", 1200), ("CCC", 500), ("DDD", 0)]
    assert scr_store.pass_symbols(sdb, now=NOW) == [s for s, _ in pairs]


def test_newest_session_of_the_stored_chain(sdb):
    assert scr_store.newest_session(sdb, "AAA") is None
    scr_store.replace_contracts(sdb, "AAA", [_contract()], session_day="2026-10-07", as_of=NOW)
    assert scr_store.newest_session(sdb, "aaa") == "2026-10-07"


def test_screener_env_targets_only_the_scr_tables():
    src = (DASH_ROOT / "alembic_screener" / "env.py").read_text(encoding="utf-8")
    assert "ScrBase.metadata" in src and "version_table=VERSION_TABLE" in src
    assert "render_as_batch=True" in src and "fileConfig(" not in src
    assert screener_db.VERSION_TABLE == "alembic_version_screener"
    ini = (DASH_ROOT / "alembic_screener.ini").read_text(encoding="utf-8")
    assert "script_location = alembic_screener" in ini


# ───────────────────────────────────────── contracts ─────────────────────────────────────────

def test_replace_contracts_carries_prev_values_across_sessions(sdb):
    a, b = _contract(strike=100.0, volume=10, oi=100), _contract(strike=105.0, volume=5, oi=50)
    assert scr_store.replace_contracts(sdb, "AAA", [a, b], session_day="2026-10-07", as_of=NOW) == 2
    got = _rows(sdb, "AAA")
    assert got[("2026-11-20", "C", 100.0)].vol_prev is None          # a new contract has none
    # the same session again (a later pass): volumes grow, prev values stay None
    scr_store.replace_contracts(sdb, "AAA", [dict(a, volume=40, oi=100), dict(b, volume=9, oi=50)],
                                session_day="2026-10-07", as_of=NOW)
    got = _rows(sdb, "AAA")
    assert got[("2026-11-20", "C", 100.0)].volume == 40 and got[("2026-11-20", "C", 100.0)].vol_prev is None
    # the next session's first pass: yesterday's final volume / OI become the prev values
    new = _contract(strike=110.0, volume=3, oi=0)
    scr_store.replace_contracts(sdb, "AAA", [dict(a, volume=2, oi=130), new],
                                session_day="2026-10-08", as_of=NOW)
    got = _rows(sdb, "AAA")
    r = got[("2026-11-20", "C", 100.0)]
    assert (r.volume, r.oi, r.vol_prev, r.oi_prev, r.session) == (2, 130, 40, 100, "2026-10-08")
    assert ("2026-11-20", "C", 105.0) not in got                     # not in this read: gone
    assert got[("2026-11-20", "C", 110.0)].vol_prev is None
    # a later pass in the same session keeps the carried values
    scr_store.replace_contracts(sdb, "AAA", [dict(a, volume=25, oi=130)],
                                session_day="2026-10-08", as_of=NOW + _dt.timedelta(minutes=30))
    r = _rows(sdb, "AAA")[("2026-11-20", "C", 100.0)]
    assert (r.volume, r.vol_prev, r.oi_prev) == (25, 40, 100)
    assert r.as_of == NOW + _dt.timedelta(minutes=30)


def test_replace_contracts_drops_bad_rows_dedupes_and_leaves_other_symbols(sdb):
    scr_store.replace_contracts(sdb, "BBB", [_contract()], session_day="2026-10-08", as_of=NOW)
    rows = [_contract(strike=100.0, volume=1), _contract(strike=100.0, volume=7),   # repeated key: last wins
            _contract(right="X"), _contract(strike=-5), _contract(expiry="soon"),
            _contract(strike=95.0, iv=9.5, volume=None, oi=-3, price=-1)]
    assert scr_store.replace_contracts(sdb, "aaa", rows, session_day="2026-10-08", as_of=NOW) == 2
    got = _rows(sdb, "AAA")
    assert got[("2026-11-20", "C", 100.0)].volume == 7
    junk = got[("2026-11-20", "C", 95.0)]
    assert (junk.iv, junk.volume, junk.oi, junk.price) == (None, None, None, None)
    assert len(_rows(sdb, "BBB")) == 1
    with pytest.raises(ValueError):
        scr_store.replace_contracts(sdb, "AAA", [], session_day="not a day", as_of=NOW)
    assert scr_store.replace_contracts(sdb, "AAA", [], session_day="2026-10-08", as_of=NOW) == 0
    assert _rows(sdb, "AAA") == {}


def test_replace_contracts_bulk_chunks(sdb, monkeypatch):
    monkeypatch.setattr(scr_store, "CHUNK", 7)
    rows = [_contract(strike=float(k)) for k in range(50, 80)]
    assert scr_store.replace_contracts(sdb, "AAA", rows, session_day="2026-10-08", as_of=NOW) == 30
    assert len(_rows(sdb, "AAA")) == 30


# ───────────────────────────────────────── daily history ─────────────────────────────────────────

def test_upsert_daily_field_by_field(sdb):
    days = _weekdays(_dt.date(2026, 10, 7), 5)
    scr_store.upsert_daily(sdb, "AAA", bars=_bars(days, [10, 11, 12, 13, 14]))
    scr_store.upsert_daily(sdb, "AAA", raw_bars=[{"on": days[-1], "close": 28.0}],
                           iv_series=[{"on": days[-1], "iv": 33.0}, {"on": days[0], "iv": 5000}])
    sdb.expire_all()
    r = sdb.query(ScrUnderlyingDaily).filter_by(symbol="AAA", on=days[-1]).one()
    assert (r.close, r.close_raw, r.iv30, r.high) == (14.0, 28.0, 33.0, 15.0)
    assert sdb.query(ScrUnderlyingDaily).filter_by(symbol="AAA", on=days[0]).one().iv30 is None
    scr_store.upsert_daily(sdb, "AAA", iv_series=[{"on": days[-1], "iv": 40.0}], overwrite_iv=False)
    sdb.expire_all()
    assert sdb.query(ScrUnderlyingDaily).filter_by(symbol="AAA", on=days[-1]).one().iv30 == 33.0
    n = scr_store.upsert_daily(sdb, "AAA", bars=[{"on": "2026-10-09", "close": 1.0}], today="2026-10-08")
    assert n == 0                                               # after today: skipped
    assert scr_store.last_close(sdb, "AAA") == (14.0, days[-1])
    assert scr_store.last_close(sdb, "ZZZ") == (None, None)


def test_file_grouped_day_files_only_the_universe_and_both_reads_fill_one_row(sdb):
    bars = [{"symbol": "AAA", "on": "2026-10-07", "open": 9.0, "high": 11.0, "low": 8.5, "close": 10.0,
             "volume": 5e6},
            {"symbol": "BBB", "on": "2026-10-07", "open": 50.0, "high": 52.0, "low": 49.0, "close": 51.0,
             "volume": 1e6},
            {"symbol": "ZZZ", "on": "2026-10-07", "close": 3.0},              # not in the universe
            {"symbol": "BAD", "close": None}]
    assert scr_store.file_grouped_day(sdb, "2026-10-07", bars, adjusted=True, symbols={"AAA", "BBB"}) == 2
    raw = [{"symbol": "AAA", "close": 20.0}, {"symbol": "ZZZ", "close": 3.0}]
    assert scr_store.file_grouped_day(sdb, "2026-10-07", raw, adjusted=False, symbols={"AAA", "BBB"}) == 1
    sdb.expire_all()
    a = sdb.query(ScrUnderlyingDaily).filter_by(symbol="AAA").one()
    assert (a.open, a.close, a.close_raw, a.volume) == (9.0, 10.0, 20.0, 5e6)
    assert sdb.query(ScrUnderlyingDaily).filter_by(symbol="ZZZ").count() == 0
    st = scr_store.stock_day_status(sdb, "2026-10-01", "2026-10-08")
    assert st == {"2026-10-07": (2, 1)}
    assert scr_store.daily_counts(sdb) == {"AAA": 1, "BBB": 1}


def test_daily_writers_share_one_lock(sdb):
    """file_grouped_day waits while another thread of this process holds _DAILY_LOCK."""
    assert isinstance(scr_store._DAILY_LOCK, type(threading.RLock()))
    out: list = []

    def other():
        s = screener_db.SessionLocal()
        try:
            out.append(scr_store.file_grouped_day(s, "2026-10-07", [{"symbol": "AAA", "close": 10.0}],
                                                  symbols={"AAA"}))
        finally:
            s.close()

    with scr_store._DAILY_LOCK:
        t = threading.Thread(target=other)
        t.start()
        t.join(0.3)
        assert t.is_alive() and out == []                       # blocked on the lock
    t.join(10)
    assert out == [1]


def test_grouped_day_and_eod_iv_race_no_integrity_error(sdb):
    """The EOD pass's workers (``update_underlying_pass(file_iv=True)``) and the stocks
    lane (``file_grouped_day``) insert the same (symbol, day) rows at once: 8 worker
    threads against the grouped filing, on the WAL file DB, several sessions in a row."""
    syms = ["S%02d" % i for i in range(48)]
    days = ["2026-10-0%d" % d for d in (5, 6, 7, 8)]
    errors: list = []

    def run(fn):
        s = screener_db.SessionLocal()
        try:
            fn(s)
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(repr(exc))
        finally:
            s.close()

    for day in days:
        gate = threading.Barrier(9)

        def worker(part, day=day, gate=gate):
            def fn(s):
                gate.wait(10)
                for sym in part:
                    scr_store.update_underlying_pass(s, sym, session_day=day, spot=100.0, iv30=30.0,
                                                     file_iv=True, now=NOW)
            run(fn)

        def grouped(day=day, gate=gate):
            bars = [{"symbol": s_, "close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1e6}
                    for s_ in syms]

            def fn(s):
                gate.wait(10)
                time.sleep(0.001)
                scr_store.file_grouped_day(s, day, bars, adjusted=True, symbols=set(syms))
            run(fn)

        threads = [threading.Thread(target=worker, args=(syms[i::8],)) for i in range(8)]
        threads.append(threading.Thread(target=grouped))
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads)
    assert errors == []
    sdb.expire_all()
    rows = sdb.query(ScrUnderlyingDaily).filter(ScrUnderlyingDaily.on.in_(days)).all()
    assert len(rows) == len(syms) * len(days)                   # one row per (symbol, day) ...
    assert all(r.close == 100.0 and r.iv30 == 30.0 for r in rows)   # ... carrying both writes


def test_raw_bars_prefers_unadjusted_closes(sdb):
    days = _weekdays(_dt.date(2026, 10, 7), 30)
    scr_store.upsert_daily(sdb, "AAA", bars=_bars(days, [50.0] * 30))
    got = scr_store.raw_bars(sdb, "AAA", 25)
    assert len(got) == 25 and got[0]["close"] == 50.0           # under 20 raw: the adjusted ones
    scr_store.upsert_daily(sdb, "AAA", raw_bars=[{"on": d, "close": 100.0} for d in days])
    got = scr_store.raw_bars(sdb, "AAA", 25)
    assert len(got) == 25 and {b["close"] for b in got} == {100.0} and got[-1]["on"] == days[-1]


def test_big_moves_flags_a_split_jump(sdb):
    d0, d1 = "2026-10-06", "2026-10-07"
    for sym, c0, c1 in (("AAA", 100.0, 50.0), ("BBB", 100.0, 103.0), ("CCC", 10.0, 100.0)):
        scr_store.upsert_daily(sdb, sym, bars=[{"on": d0, "close": c0}, {"on": d1, "close": c1}])
    assert scr_store.big_moves(sdb, d1, lo=0.6, hi=1.6) == ["AAA", "CCC"]
    assert scr_store.big_moves(sdb, d1, lo=0.6, hi=1.6, symbols={"AAA"}) == ["AAA"]


# ───────────────────────────────────────── technicals ─────────────────────────────────────────

def _matp_rule():
    """MATP's own ``classify_trend`` (and its ``ema``) lifted out of
    resources/MATP/scripts/daily_bounce_alert.py - the script imports its env helpers at
    module level and cannot load here."""
    src = MATP_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in ("ema", "classify_trend"))
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "MIN_BARS_FOR_EMA200" for t in n.targets))]
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(MATP_SRC), "exec"), ns)   # noqa: S102
    return ns["classify_trend"]


def test_classify_trend_is_the_matp_rule():
    if not MATP_SRC.exists():
        pytest.skip("resources/MATP not on this PC")
    matp = _matp_rule()
    words = {"Uptrend": "up", "Downtrend": "down", "Sideways": "sideways", "Unknown": None}
    rnd = random.Random(7)
    for i in range(60):
        n = rnd.choice((150, 209, 210, 260, 400))
        drift = rnd.choice((-0.004, -0.001, 0.0, 0.001, 0.004))
        c, closes = 100.0, []
        for _ in range(n):
            c *= math.exp(drift + rnd.gauss(0, 0.012))
            closes.append(c)
        assert scr_store.classify_trend(closes) == words[matp(closes, 20)], i
    up = [100 * 1.003 ** i for i in range(260)]
    assert scr_store.classify_trend(up) == "up"
    assert scr_store.classify_trend(list(reversed(up))) == "down"
    assert scr_store.classify_trend(up[:100]) is None


def test_technicals_from_bars():
    days = _weekdays(_dt.date(2026, 10, 7), 260)
    closes = [100 * 1.002 ** i for i in range(260)]
    t = scr_store.technicals(_bars(days, closes, vol=2e6, rng=1.0))
    assert t["sma20"] == pytest.approx(sum(closes[-20:]) / 20)
    assert t["sma200"] == pytest.approx(sum(closes[-200:]) / 200)
    assert t["rsi14"] == 100.0                                   # only gains
    assert t["atr14"] == pytest.approx(2.0, rel=0.05)           # high - low = 2 every day
    assert t["atr_pct"] == pytest.approx(t["atr14"] / closes[-1] * 100)
    assert t["hv20"] == pytest.approx(0.0, abs=1e-6) and t["hv60"] == pytest.approx(0.0, abs=1e-6)
    assert (t["avg_vol20"], t["avg_vol50"], t["stock_volume"]) == (2e6, 2e6, 2e6)
    assert t["hi52"] == pytest.approx(closes[-1] + 1) and t["lo52"] == pytest.approx(closes[-252] - 1)
    assert t["perf5"] == pytest.approx((1.002 ** 5 - 1) * 100)
    assert t["perf20"] == pytest.approx((1.002 ** 20 - 1) * 100)
    assert t["trend"] == "up"
    short = scr_store.technicals(_bars(days[:10], closes[:10]))
    assert short["sma20"] is None and short["rsi14"] is None and short["trend"] is None
    assert scr_store.technicals([])["hi52"] is None
    zig = [100.0 + (1 if i % 2 else -1) for i in range(30)]
    assert 40 < scr_store.technicals(_bars(days[:30], zig))["rsi14"] < 60


def test_recompute_technicals_writes_the_row_and_the_day_change(sdb):
    days = _weekdays(_dt.date(2026, 10, 7), 230)
    closes = [50 + 0.1 * i for i in range(230)]
    scr_store.upsert_daily(sdb, "AAA", bars=_bars(days, closes))
    row = scr_store.recompute_technicals(sdb, "AAA", now=NOW)
    assert row["trend"] == "up" and row["sma50"] == pytest.approx(sum(closes[-50:]) / 50)
    assert row["prev_close"] == pytest.approx(closes[-1])      # no pass yet: the newest close
    assert row["bars_as_of"] == _dt.datetime(2026, 10, 7, 20, 0)   # 16:00 ET (EDT) as UTC
    # after a pass of session 2026-10-08 at spot 80: prev close = 2026-10-07's, change vs it
    pid = scr_store.start_pass(sdb, kind="cycle", session="2026-10-08", n_symbols=1, now=NOW)
    scr_store.update_underlying_pass(sdb, "AAA", session_day="2026-10-08", pass_id=pid, spot=80.0,
                                     spot_src="parity", spot_as_of=NOW, now=NOW)
    row = scr_store.recompute_technicals(sdb, "AAA", now=NOW)
    assert row["prev_close"] == pytest.approx(closes[-1])
    assert row["chg_pct"] == pytest.approx((80.0 / closes[-1] - 1) * 100)
    assert scr_store.recompute_technicals_many(sdb, ["AAA", "NEW"], now=NOW, chunk=1) == 2
    assert scr_store.underlying(sdb, "NEW")["sma20"] is None


# ───────────────────────────────────────── the underlying ─────────────────────────────────────────

def test_update_underlying_pass_spot_change_and_eod_iv_filing(sdb):
    days = _weekdays(_dt.date(2026, 10, 7), 40)
    scr_store.upsert_daily(sdb, "AAA", bars=_bars(days, [100.0] * 39 + [104.0]),
                           iv_series=[{"on": d, "iv": 20.0 + i} for i, d in enumerate(days)])
    pid = scr_store.start_pass(sdb, kind="cycle", session="2026-10-08", n_symbols=1, now=NOW)
    row = scr_store.update_underlying_pass(
        sdb, "AAA", session_day="2026-10-08", pass_id=pid, spot=106.08, spot_src="parity",
        spot_as_of=NOW, iv30=70.0, call_vol=10, put_vol=5, call_oi=100, put_oi=50, n_contracts=12, now=NOW)
    assert (row["spot"], row["spot_src"], row["prev_close"]) == (106.08, "parity", 104.0)
    assert row["chg_pct"] == pytest.approx(2.0)
    assert row["exp_move30"] == pytest.approx(70.0 * math.sqrt(30 / 365))
    assert (row["call_vol"], row["put_vol"], row["call_oi"], row["put_oi"], row["n_contracts"]) == \
        (10, 5, 100, 50, 12)
    # the IV figures: 40 past readings (20..59) + today's 70 -> the top of the range
    assert (row["iv30"], row["iv30_prev"], row["iv_n"]) == (70.0, 59.0, 41)
    assert row["iv_rank"] is None                                 # a rank needs 60 readings
    assert row["iv_pct"] == pytest.approx(40 / 41 * 100, abs=0.1)
    assert (row["iv_lo"], row["iv_hi"]) == (20.0, 70.0)
    assert row["pass_id"] == pid
    # not an EOD pass: nothing filed for 2026-10-08
    assert sdb.query(ScrUnderlyingDaily).filter_by(symbol="AAA", on="2026-10-08").count() == 0
    # a pass without a spot / IV keeps the stored ones
    row = scr_store.update_underlying_pass(sdb, "AAA", session_day="2026-10-08", pass_id=pid, now=NOW)
    assert (row["spot"], row["iv30"]) == (106.08, 70.0)
    # the EOD pass files the day's IV30
    row = scr_store.update_underlying_pass(sdb, "AAA", session_day="2026-10-08", pass_id=pid,
                                           spot=105.0, iv30=65.0, file_iv=True, now=NOW)
    sdb.expire_all()
    assert sdb.query(ScrUnderlyingDaily).filter_by(symbol="AAA", on="2026-10-08").one().iv30 == 65.0
    assert (row["iv30"], row["iv30_prev"], row["iv_n"]) == (65.0, 59.0, 41)


def test_recompute_iv_without_a_pass_uses_the_newest_reading(sdb):
    days = _weekdays(_dt.date(2026, 10, 7), 25)
    scr_store.upsert_daily(sdb, "AAA", iv_series=[{"on": d, "iv": 30.0 + (i % 5)} for i, d in enumerate(days)])
    row = scr_store.recompute_iv(sdb, "AAA", now=NOW)
    assert row["iv30"] == 30.0 + (24 % 5) and row["iv30_prev"] == 30.0 + (23 % 5)
    assert row["iv_n"] == 25 and row["iv_pct"] is not None and row["iv_rank"] is None   # rank needs 60
    big = _weekdays(_dt.date(2026, 10, 7), 300)
    scr_store.upsert_daily(sdb, "BBB", iv_series=[{"on": d, "iv": 10.0 + i} for i, d in enumerate(big)])
    row = scr_store.recompute_iv(sdb, "BBB", now=NOW)
    assert row["iv_n"] == scr_store.IV_WINDOW and row["iv_lo"] == 10.0 + 300 - 252


# ───────────────────────────────────────── the universe ─────────────────────────────────────────

def test_upsert_universe_new_gone_partial_and_retention(sdb):
    t0 = NOW
    res = scr_store.upsert_universe(sdb, {"AAA": 300, "BBB": 50, "CCC": 900, "DDD": 10}, now=t0)
    assert res == {"n": 4, "new": 4, "inactive": 0, "partial": False}
    assert scr_store.pass_symbols(sdb, now=t0) == ["CCC", "AAA", "BBB", "DDD"]     # most contracts first
    assert sdb.query(ScrUnderlying).count() == 4                   # an underlying row each
    # DDD gone from the list: inactive, but still read for 10 days
    t1 = t0 + _dt.timedelta(days=1)
    res = scr_store.upsert_universe(sdb, {"AAA": 310, "BBB": 50, "CCC": 900}, now=t1)
    assert res["inactive"] == 1 and not res["partial"]
    assert "DDD" in scr_store.pass_symbols(sdb, now=t1)
    assert scr_store.universe_info(sdb)["n"] == 3
    # a short (partial) list deactivates nothing
    res = scr_store.upsert_universe(sdb, {"AAA": 1}, now=t1)
    assert res["partial"] and res["inactive"] == 0
    assert scr_store.upsert_universe(sdb, {}, now=t1)["partial"]
    # 11 days later DDD is out of the passes and pruned with every row of it
    scr_store.replace_contracts(sdb, "DDD", [_contract()], session_day="2026-10-08", as_of=t0)
    scr_store.upsert_daily(sdb, "DDD", bars=[{"on": "2026-10-07", "close": 5.0}])
    scr_store.replace_contracts(sdb, "AAA", [_contract(expiry="2026-10-02"), _contract()],
                                session_day="2026-10-08", as_of=t0)
    t11 = t1 + _dt.timedelta(days=11)
    assert "DDD" not in scr_store.pass_symbols(sdb, now=t11)
    out = scr_store.prune(sdb, now=t11, today="2026-10-08")
    assert out["symbols"] == 1 and out["contracts"] == 1 and out["daily"] == 1 and out["underlyings"] == 1
    assert out["expired"] == 1                                       # AAA's 2026-10-02 contract
    assert sdb.query(ScrUniverse).filter_by(symbol="DDD").count() == 0
    assert set(_rows(sdb, "AAA")) == {("2026-11-20", "C", 100.0)}


def test_upsert_universe_partial_never_deactivates(sdb):
    t0 = NOW
    scr_store.upsert_universe(sdb, {"AAA": 300, "BBB": 50, "CCC": 900, "DDD": 10}, now=t0)
    # a streamed save of a walk in progress: its first pages hold only two symbols - long
    # enough to pass the ratio rule would not matter, deactivate=False never deactivates
    t1 = t0 + _dt.timedelta(minutes=5)
    res = scr_store.upsert_universe(sdb, {"AAA": 120, "EEE": 7}, now=t1, deactivate=False)
    assert res == {"n": 2, "new": 1, "inactive": 0, "partial": True}
    sdb.expire_all()
    act = {u.symbol: u.active for u in sdb.query(ScrUniverse)}
    assert act == {"AAA": True, "BBB": True, "CCC": True, "DDD": True, "EEE": True}
    assert scr_store.underlying(sdb, "EEE") is not None                 # filed at once for the pass
    aaa = sdb.query(ScrUniverse).filter_by(symbol="AAA").one()
    assert (aaa.n_contracts, aaa.last_seen) == (120, t1)
    # even a list as long as the active one stays a partial save with deactivate=False
    res = scr_store.upsert_universe(sdb, {"AAA": 1, "BBB": 1, "CCC": 1, "EEE": 1}, now=t1, deactivate=False)
    assert res["partial"] and res["inactive"] == 0
    # the walk completes: the final save deactivates what is gone, stamped with the
    # COMPLETION time (not the start)
    done = t1 + _dt.timedelta(minutes=20)
    res = scr_store.upsert_universe(sdb, {"AAA": 310, "CCC": 900, "EEE": 7}, now=done)
    assert res == {"n": 3, "new": 0, "inactive": 2, "partial": False}
    sdb.expire_all()
    act = {u.symbol: u.active for u in sdb.query(ScrUniverse)}
    assert act == {"AAA": True, "BBB": False, "CCC": True, "DDD": False, "EEE": True}
    assert scr_store.universe_info(sdb)["refreshed"] == done
    # the gone ones are still read for 10 days (most contracts first)
    assert scr_store.pass_symbols(sdb, now=done) == ["CCC", "AAA", "DDD", "EEE", "BBB"]


# ───────────────────────────────────────── passes, status, history ─────────────────────────────────────────

def test_passes_and_the_status_row(sdb):
    pid = scr_store.start_pass(sdb, kind="cycle", session="2026-10-08", n_symbols=3, now=NOW)
    assert scr_store.last_pass(sdb) is None                          # not finished yet
    assert scr_store.last_pass(sdb, finished=None)["id"] == pid
    scr_store.finish_pass(sdb, pid, n_ok=2, n_failed=1, n_contracts=40, requests=9, ms=1200, finished=False)
    assert scr_store.last_pass(sdb) is None
    row = scr_store.finish_pass(sdb, pid, n_ok=3, n_failed=0, now=NOW + _dt.timedelta(minutes=3))
    assert row["finished"] == NOW + _dt.timedelta(minutes=3) and row["n_contracts"] == 40
    assert scr_store.last_pass(sdb, kind="cycle")["id"] == pid and scr_store.last_pass(sdb, kind="eod") is None
    assert scr_store.finish_pass(sdb, 999) is None

    assert scr_store.status(sdb) is None
    scr_store.set_status(sdb, state="pass", detail="x" * 5000, pid=42, version="1.0" * 20,
                         universe_on="2026-10-08", api_ok=True, bogus=1, now=NOW)
    st = scr_store.status(sdb)
    assert (st["state"], st["pid"], st["heartbeat"], st["api_ok"]) == ("pass", 42, NOW, True)
    assert len(st["version"]) == 16 and len(st["detail"]) == 5000
    assert sdb.query(ScrStatus).count() == 1
    # the v4.137 fields are None until a collector writes them (an older one never does)
    assert all(st[k] is None for k in STATUS_NEW)


def test_set_status_round_trips_progress_and_next_try(sdb):
    myt = _dt.timezone(_dt.timedelta(hours=8))
    started = _dt.datetime(2026, 10, 10, 13, 5)                      # naive UTC
    progress = {"universe_pages": 312, "universe_symbols": 1840, "universe_started": started,
                "pass_kind": "eod", "pass_session": _dt.date(2026, 10, 9), "pass_pct": 41.5,
                "pass_eta_s": float("nan"), "stock_days_pending": (3, 4), "history_left": None,
                "nested": {"ok": True, "days": {"2026-10-08"}}}
    scr_store.set_status(sdb, state="universe", error_kind="network-and-more",
                         next_try=_dt.datetime(2026, 10, 10, 21, 30, tzinfo=myt),   # 13:30 UTC
                         universe_done="2026-10-09T20:31:00+00:00", earnings_on="2026-10-09",
                         warn="Universe refresh failed", progress=progress, now=NOW)
    sdb.expire_all()
    st = scr_store.status(sdb)
    assert st["error_kind"] == "network-an"                          # cut to the column's 10
    assert st["next_try"] == _dt.datetime(2026, 10, 10, 13, 30) and st["next_try"].tzinfo is None
    assert st["universe_done"] == _dt.datetime(2026, 10, 9, 20, 31)
    assert (st["earnings_on"], st["warn"]) == ("2026-10-09", "Universe refresh failed")
    assert st["progress"] == {"universe_pages": 312, "universe_symbols": 1840,
                              "universe_started": "2026-10-10T13:05:00+00:00", "pass_kind": "eod",
                              "pass_session": "2026-10-09", "pass_pct": 41.5, "pass_eta_s": None,
                              "stock_days_pending": [3, 4], "history_left": None,
                              "nested": {"ok": True, "days": ["2026-10-08"]}}
    # a heartbeat that leaves a field out keeps it; None clears it (SQL NULL)
    scr_store.set_status(sdb, state="pass", now=NOW + _dt.timedelta(seconds=15))
    sdb.expire_all()
    st = scr_store.status(sdb)
    assert st["progress"]["universe_pages"] == 312 and st["warn"] == "Universe refresh failed"
    scr_store.set_status(sdb, progress=None, warn=None, next_try=None, error_kind=None, now=NOW)
    sdb.expire_all()
    st = scr_store.status(sdb)
    assert (st["progress"], st["warn"], st["next_try"], st["error_kind"]) == (None, None, None, None)
    from sqlalchemy import text  # noqa: PLC0415 - checking the stored NULL in a test only
    assert sdb.execute(text("select progress is null from scr_status where id = 1")).scalar() == 1


def test_last_pass_min_contracts(sdb):
    good = scr_store.start_pass(sdb, kind="eod", session="2026-10-08", n_symbols=3, now=NOW)
    scr_store.finish_pass(sdb, good, n_ok=3, n_failed=0, n_contracts=40, now=NOW)
    empty = scr_store.start_pass(sdb, kind="eod", session="2026-10-09", n_symbols=3, now=NOW)
    scr_store.finish_pass(sdb, empty, n_ok=0, n_failed=3, n_contracts=0, now=NOW)
    assert scr_store.last_pass(sdb, kind="eod")["id"] == empty          # finished, but read nothing
    assert scr_store.last_pass(sdb, kind="eod", min_contracts=1)["id"] == good
    assert scr_store.last_pass(sdb, min_contracts=41) is None
    running = scr_store.start_pass(sdb, kind="cycle", session="2026-10-09", n_symbols=3, now=NOW)
    assert scr_store.last_pass(sdb, finished=None, min_contracts=1)["id"] == good   # 0 so far
    scr_store.finish_pass(sdb, running, n_contracts=7, finished=False, now=NOW)
    assert scr_store.last_pass(sdb, finished=False, min_contracts=1)["id"] == running


def test_history_queue_order_and_backoff(sdb):
    scr_store.upsert_universe(sdb, {"AAA": 1, "BBB": 1, "CCC": 1}, now=NOW)
    for sym, cv, pv in (("AAA", 10, 5), ("BBB", 900, 100), ("CCC", 0, 0)):
        scr_store.update_underlying_pass(sdb, sym, session_day="2026-10-08", call_vol=cv, put_vol=pv, now=NOW)
    assert scr_store.history_queue(sdb, now=NOW) == ["BBB", "AAA", "CCC"]
    assert scr_store.history_queue(sdb, now=NOW, symbols={"AAA", "CCC"}, limit=1) == ["AAA"]
    waits = []
    for i in range(8):
        got = scr_store.mark_history(sdb, "BBB", done=False, now=NOW)
        waits.append((got["next"] - NOW).total_seconds())
        assert got["tries"] == i + 1
    assert waits[:4] == [1800, 3600, 7200, 14400] and waits[-1] == 24 * 3600
    assert "BBB" not in scr_store.history_queue(sdb, now=NOW)
    assert "BBB" in scr_store.history_queue(sdb, now=NOW + _dt.timedelta(days=1, seconds=1))
    got = scr_store.mark_history(sdb, "BBB", done=True, now=NOW)
    assert got == {"done": True, "tries": 0, "next": None}
    assert scr_store.history_counts(sdb) == (1, 3)
    assert scr_store.history_counts(sdb, {"AAA", "BBB"}) == (1, 2)


def test_history_queue_skips_index(sdb):
    # the contracts list names index options bare (SPX); the ticker list gives I:NDX
    scr_store.upsert_universe(sdb, {"AAA": 10, "SPX": 9000, "I:NDX": 4000, "BBB": 5}, now=NOW)
    scr_store.set_identity(sdb, [{"symbol": "AAA", "sec_type": "stock", "exchange": "NYSE"},
                                 {"symbol": "SPX", "sec_type": "index", "exchange": "INDEX"}],
                           only={"AAA", "SPX", "I:NDX", "BBB"}, now=NOW)
    for sym, cv in (("AAA", 10), ("SPX", 90_000), ("I:NDX", 50_000), ("BBB", 5)):
        scr_store.update_underlying_pass(sdb, sym, session_day="2026-10-08", call_vol=cv, put_vol=0, now=NOW)
    assert scr_store.history_queue(sdb, now=NOW) == ["AAA", "BBB"]     # no index, however busy
    assert scr_store.history_counts(sdb) == (0, 2)                     # nor in the denominator
    assert scr_store.history_counts(sdb, {"AAA", "SPX", "I:NDX"}) == (0, 1)
    scr_store.mark_history(sdb, "AAA", done=True, now=NOW)
    scr_store.mark_history(sdb, "BBB", done=True, now=NOW)
    assert scr_store.history_counts(sdb) == (2, 2)                     # complete: nothing waits on SPX
    assert scr_store.history_applies("SPY") and scr_store.history_applies("SPX", "stock")
    assert not scr_store.history_applies("SPX", "index") and not scr_store.history_applies("i:ndx")


def test_identity_and_earnings(sdb):
    scr_store.upsert_universe(sdb, {"AAA": 1, "BRK-B": 1, "I:SPX": 1}, now=NOW)
    assert scr_store.identity_missing(sdb)
    recs = [{"symbol": "AAA", "name": "Aaa Corp", "sec_type": "stock", "exchange": "NASDAQ"},
            {"symbol": "BRK-B", "name": "Berkshire", "sec_type": "stock", "exchange": "XYZ"},
            {"symbol": "I:SPX", "name": "S&P 500", "sec_type": "index", "exchange": "INDEX"},
            {"symbol": "ZZZ", "name": "not in the universe", "sec_type": "etf", "exchange": "AMEX"}]
    n = scr_store.set_identity(sdb, recs, only={"AAA", "BRK-B", "I:SPX", "NEW"}, now=NOW)
    assert n == 3
    a, b = scr_store.underlying(sdb, "AAA"), scr_store.underlying(sdb, "BRK-B")
    assert (a["name"], a["sec_type"], a["exchange"]) == ("Aaa Corp", "stock", "NASDAQ")
    assert b["exchange"] == "OTHER" and scr_store.underlying(sdb, "ZZZ") is None
    assert not scr_store.identity_missing(sdb)

    scr_store.set_earnings(sdb, {"AAA": "2026-10-20", "BRK-B": "2026-11-02", "OLD": "2026-10-01"},
                           today="2026-10-08", now=NOW)
    assert scr_store.underlying(sdb, "AAA")["earnings_date"] == "2026-10-20"
    assert scr_store.underlying(sdb, "AAA")["earnings_src"] == "nasdaq"
    # a later read without AAA keeps its future date; a date that has passed is cleared
    scr_store.set_earnings(sdb, {"I:SPX": "2026-10-30"}, today="2026-11-03", now=NOW)
    assert scr_store.underlying(sdb, "AAA")["earnings_date"] is None       # 2026-10-20 < 2026-11-03
    assert scr_store.underlying(sdb, "BRK-B")["earnings_date"] is None      # 2026-11-02 passed too
    scr_store.set_earnings(sdb, {"AAA": "2026-12-01"}, today="2026-11-03", now=NOW)
    scr_store.set_earnings(sdb, {}, today="2026-11-04", now=NOW)
    assert scr_store.underlying(sdb, "AAA")["earnings_date"] == "2026-12-01"


def test_only_orm_no_raw_sql_in_scr_store():
    src = (DASH_ROOT / "app" / "services" / "scr_store.py").read_text(encoding="utf-8")
    low = src.lower()
    for word in ("text(", "insert or replace", "\"pragma", "on conflict", "exec_driver_sql", "executescript"):
        assert word not in low, word
    assert ScrPass.__tablename__ == "scr_pass"
