"""The persistence foundations of the v1 Options module that survive Options v2: the
migration ``f4a5b6c7d8e9`` (its nine tables stay in the DB, unused), what is left of
``services/option_store.py`` (``prune``, ``basket_universe``, ``upsert_iv_daily`` and the
``option_signal`` helpers the Telegram push still reads), ``job_runs.py``, ``clock.py``.

Every DB test runs against a fresh SQLite file brought to head by the REAL migration
chain (conftest), never ``create_all`` - so the migration is what is tested.
"""
from __future__ import annotations

import datetime as _dt

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import clock, job_runs, option_store

from .conftest import HEAD, PREVIOUS_HEAD, downgrade, make_engine, table_names, upgrade

NINE = ["option_basket", "option_chain_snapshot", "iv_daily", "option_signal",
        "user_option_prefs", "option_jobs", "option_trades", "option_trade_checks",
        "option_idea_push"]

HOUSE = "0123456789ab"       # a stand-in house hash (option_prefs owns the real one)
MINE = "fedcba987654"        # a member's own hash


# ───────────────────────────────── fixtures / builders ─────────────────────────────────

def _session(url):
    eng = make_engine(url)
    Session = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    return eng, Session()


def _chain(symbol="LRCX", snap_on="2026-10-02", kind="eod", source="cboe", n=6,
           spot=349.2, iv30=46.0, as_of=None):
    """A small Chain-shaped dict: n put rows across two expiries, deltas spread from
    deep OTM (thinned) to ATM, plus one call."""
    rows = []
    for i in range(n):
        strike = 300.0 + 10 * i
        delta = -0.02 - 0.09 * i           # -0.02 (thinned), ..., -0.47
        rows.append({"expiry": "2026-11-20", "right": "P", "strike": strike,
                     "bid": 1.0 + i, "ask": 1.1 + i, "mid": None, "last": None,
                     "bid_size": 5, "ask_size": 7, "iv": 0.40 + 0.01 * i, "delta": delta,
                     "gamma": 0.01, "theta": -0.05, "vega": 0.3, "rho": 0.0, "theo": None,
                     "oi": 1000 + i, "volume": None, "prev_close": None})
    rows.append({"expiry": "2026-12-19", "right": "C", "strike": 360.0, "bid": 9.0,
                 "ask": 9.4, "iv": 0.45, "delta": 0.40, "oi": 500, "volume": 12})
    return {"symbol": symbol, "snap_on": snap_on, "kind": kind, "source": source,
            "as_of": as_of or _dt.datetime(2026, 10, 2, 19, 59, 59), "spot": spot,
            "iv30": iv30, "rows": rows, "partial": False}


# ───────────────────────────────── the migration ─────────────────────────────────

def test_migration_empty_db_creates_nine_tables(db_url):
    upgrade(db_url, "head")
    have = table_names(db_url)
    assert set(NINE) <= have
    eng = make_engine(db_url)
    try:
        with eng.connect() as c:
            assert c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == HEAD
            # the migration and the models agree: every model column exists in its table
            insp = sa.inspect(c)
            for name in NINE:
                cols = {col["name"] for col in insp.get_columns(name)}
                model_cols = {col.name for col in models.Base.metadata.tables[name].columns}
                assert model_cols <= cols, (name, model_cols - cols)
            assert "meta" in {col["name"] for col in insp.get_columns("option_trades")}
            assert "pushed" in {col["name"] for col in insp.get_columns("option_jobs")}
    finally:
        eng.dispose()


def test_migration_guard_when_create_all_pre_created_a_table(db_url):
    upgrade(db_url, PREVIOUS_HEAD)
    eng = make_engine(db_url)
    try:
        models.OptionBasket.__table__.create(eng)        # the Hermes create_all situation
    finally:
        eng.dispose()
    upgrade(db_url, "head")                               # must not raise
    assert set(NINE) <= table_names(db_url)


def _seed_spreads(url):
    eng, s = _session(url)
    try:
        u = models.User(email="legacy@local.test", display_name="Legacy",
                        role=models.ROLE_MEMBER, status=models.APPROVED)
        s.add(u)
        s.flush()
        rows = []
        for i in range(5):
            rows.append(models.OptionSpread(
                user_id=u.id, symbol="MSFT", strategy="bull_put", expiry="2026-11-20",
                short_strike=460.0 - i, long_strike=450.0 - i, credit=2.0 + 0.1 * i,
                contracts=2, entry_delta=0.25, status="open" if i < 3 else "closed",
                note="spread %d" % i, roll_delta=0.30, loss_stop_pct=20.0,
                profit_target_pct=50.0, dte_floor=21, short_price=4.04, long_price=2.04,
                short_entry_delta=0.25, long_entry_delta=0.17, entry_iv=0.43,
                opened_at=_dt.datetime(2026, 9, 1, 14, 0, 0)))
        s.add_all(rows)
        s.commit()
    finally:
        s.close()
        eng.dispose()


def _spread_rows(url):
    eng = make_engine(url)
    try:
        with eng.connect() as c:
            return c.execute(sa.text("SELECT * FROM option_spreads ORDER BY id")).fetchall()
    finally:
        eng.dispose()


def _trade_rows(url):
    eng, s = _session(url)
    try:
        return [(t.id, t.user_id, t.symbol, t.strategy, t.family, t.legs, t.front_expiry,
                 t.net_entry, t.contracts, t.max_loss, t.roll_delta, t.dte_floor, t.meta,
                 t.status, t.note)
                for t in s.query(models.OptionTrade).order_by(models.OptionTrade.id).all()]
    finally:
        s.close()
        eng.dispose()


def test_migration_copies_open_spreads_once_and_downgrades_clean(db_url):
    upgrade(db_url, PREVIOUS_HEAD)
    _seed_spreads(db_url)
    before = _spread_rows(db_url)
    assert len(before) == 5

    upgrade(db_url, "head")
    trades = _trade_rows(db_url)
    assert len(trades) == 3
    for (_id, user_id, sym, strategy, family, legs, front, net, n, max_loss,
         roll_delta, dte_floor, meta, status, note) in trades:
        assert (sym, strategy, family, front, n, status) == ("MSFT", "bull_put", "credit_vertical",
                                                             "2026-11-20", 2, "open")
        assert len(legs) == 2
        short, long_ = legs
        assert short["side"] == "sell" and long_["side"] == "buy"
        assert short["right"] == "P" and long_["right"] == "P"
        assert short["qty"] == 1 and long_["qty"] == 1
        assert short["strike"] - long_["strike"] == pytest.approx(10.0)
        assert short["entry_price"] == 4.04 and long_["entry_price"] == 2.04
        assert short["entry_delta"] == pytest.approx(-0.25)      # stored absolute, kept signed
        assert long_["entry_delta"] == pytest.approx(-0.17)
        assert short["entry_iv"] == 0.43 and short["iv"] == 0.43
        assert net < 0 and net == pytest.approx(-(2.0 + 0.1 * (_id - 1)))
        assert max_loss == pytest.approx((10.0 - (2.0 + 0.1 * (_id - 1))) * 100.0)
        assert roll_delta == 0.30 and dte_floor == 21
        assert meta == {}
        assert note.startswith("migrated from option_spreads #")
    assert _spread_rows(db_url) == before                   # option_spreads untouched

    upgrade(db_url, "head")                                 # re-run: no duplicates
    assert len(_trade_rows(db_url)) == 3
    assert _spread_rows(db_url) == before

    # explicitly to the revision before f4a5b6c7d8e9: "-1" from head would now only undo
    # the Options v2 migration (3b26d60468a0) and leave the nine tables in place
    downgrade(db_url, PREVIOUS_HEAD)
    have = table_names(db_url)
    assert not (set(NINE) & have)
    assert "option_spreads" in have and _spread_rows(db_url) == before

    upgrade(db_url, "head")                                 # and back up: the copy runs again, once
    assert len(_trade_rows(db_url)) == 3


# ───────────────────────────────── snapshot rows + iv_daily ─────────────────────────────────

def _snap(db, chain) -> int:
    """Write ``chain``'s rows to ``option_chain_snapshot`` the way the v1 writer did -
    replaced per ``(symbol, snap_on, kind)``, and an intraday write drops the symbol's
    older intraday rows - through the ORM, so ``prune`` has rows to prune. (The v2
    collector's EOD copy is ``opt_store.snapshot_eod``, tested in test_opt_store.)"""
    S = models.OptionChainSnapshot
    q = db.query(S).filter(S.symbol == chain["symbol"], S.kind == chain["kind"])
    if chain["kind"] != "intraday":
        q = q.filter(S.snap_on == chain["snap_on"])
    q.delete(synchronize_session=False)
    on = _dt.date.fromisoformat(chain["snap_on"])
    rows = [S(symbol=chain["symbol"], snap_on=chain["snap_on"], kind=chain["kind"],
              source=chain["source"], expiry=r["expiry"], right=r["right"], strike=r["strike"],
              dte=(_dt.date.fromisoformat(r["expiry"]) - on).days,
              bid=r.get("bid"), ask=r.get("ask"), iv=r.get("iv"), delta=r.get("delta"),
              oi=r.get("oi"), volume=r.get("volume"))
            for r in chain["rows"]]
    db.add_all(rows)
    db.flush()
    return len(rows)


# ───────────────────────────────── prune ─────────────────────────────────

def test_prune(db, user, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "options_snapshot_days", 90)
    monkeypatch.setattr(settings, "options_full_days", 7)
    today = _dt.date(2026, 10, 2)
    for i in range(100):                                    # 100 EOD days, 7 rows each
        day = (today - _dt.timedelta(days=i)).isoformat()
        _snap(db, _chain(snap_on=day, kind="eod"))
        db.add(models.OptionSignal(symbol="LRCX", snap_on=day, kind="eod", prefs_hash=HOUSE,
                                   engine_version="t", status="ok"))
    _snap(db, _chain(snap_on=(today - _dt.timedelta(days=1)).isoformat(), kind="intraday"))
    # an expired contract 8 days back, and one 6 days back (kept)
    expired = _chain(snap_on="2026-09-20")
    expired["rows"] = [dict(expired["rows"][3], expiry="2026-09-24"),
                       dict(expired["rows"][4], expiry="2026-09-26")]
    _snap(db, expired)
    t = models.OptionTrade(user_id=user.id, symbol="LRCX", strategy="bull_put",
                           family="credit_vertical", legs=[], front_expiry="2026-11-20",
                           net_entry=-2.1, contracts=1)
    db.add(t)
    db.flush()
    for i in (1, 50, 95):
        db.add(models.OptionTradeCheck(trade_id=t.id,
                                       checked_on=(today - _dt.timedelta(days=i)).isoformat()))
    for i in range(200):
        db.add(models.OptionJob(job="nightly", run_on=today.isoformat()))
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    db.add(models.OptionIdeaPush(user_id=user.id, symbol="LRCX", idea_key="LRCX|bull_put|2026-11-20",
                                 sent_at=now - _dt.timedelta(days=50)))
    db.add(models.OptionIdeaPush(user_id=user.id, symbol="MSFT", idea_key="MSFT|bull_put|2026-11-20",
                                 sent_at=now - _dt.timedelta(days=10)))
    db.commit()

    out = option_store.prune(db, today)
    S = models.OptionChainSnapshot
    days = sorted({d for (d,) in db.query(S.snap_on).filter_by(kind="eod").distinct()})
    assert len(days) == 90 and days[0] == (today - _dt.timedelta(days=89)).isoformat()
    day10 = (today - _dt.timedelta(days=10)).isoformat()
    day3 = (today - _dt.timedelta(days=3)).isoformat()
    deltas10 = [abs(d) for (d,) in db.query(S.delta).filter_by(snap_on=day10, kind="eod")]
    assert all(0.03 <= d <= 0.97 for d in deltas10) and len(deltas10) == 6   # only the -0.02 put is thinned
    assert db.query(S).filter_by(snap_on=day3, kind="eod").count() == 7     # full width inside 7 days
    assert db.query(S).filter_by(kind="intraday").count() == 0              # yesterday's intraday gone
    exp = {e for (e,) in db.query(S.expiry).filter_by(snap_on="2026-09-20").distinct()}
    assert exp == {"2026-09-26"}                                             # expired > 7 days gone
    assert db.query(models.OptionSignal).count() == 90
    checks = sorted(c.checked_on for c in db.query(models.OptionTradeCheck).all())
    assert len(checks) == 2 and db.query(models.OptionTrade).count() == 1
    assert db.query(models.OptionJob).count() == 180
    assert [p.symbol for p in db.query(models.OptionIdeaPush).all()] == ["MSFT"]
    assert out["jobs"] == 20 and out["idea_push"] == 1 and out["trade_checks"] == 1



def test_basket_universe_includes_open_trades(db, user):
    db.add(models.OptionBasket(user_id=user.id, owner_key="u%d" % user.id, symbol="KO",
                               source="typed", added_on="2026-10-01"))
    db.add(models.OptionTrade(user_id=user.id, symbol="LRCX", strategy="bull_put",
                              family="credit_vertical", legs=[], front_expiry="2026-11-20",
                              net_entry=-2.1))
    db.add(models.OptionTrade(user_id=user.id, symbol="MSFT", strategy="bull_put",
                              family="credit_vertical", legs=[], front_expiry="2026-11-20",
                              net_entry=-2.1, status="closed"))
    db.commit()
    assert option_store.basket_universe(db) == ["KO", "LRCX"]


def test_user_option_prefs_one_to_one(db, user):
    db.add(models.UserOptionPrefs(user_id=user.id, prefs={"credit_vertical": {"short_delta_hi": 0.25}},
                                  prefs_hash=MINE))
    db.commit()
    db.refresh(user)
    assert user.option_prefs.prefs_hash == MINE
    assert user.option_prefs.user is user
    db.delete(user)
    db.commit()
    assert db.query(models.UserOptionPrefs).count() == 0


# ───────────────────────────────── job_runs ─────────────────────────────────

def test_job_runs_start_finish_latest_missed(db):
    assert job_runs.latest(db, "nightly") is None
    run = job_runs.start(db, "nightly", "2026-10-02", source="cboe")
    assert run.id and run.started_at is not None and run.finished_at is None
    assert job_runs.latest(db, "nightly") is None                       # not finished yet
    assert job_runs.running(db, "nightly") is True
    job_runs.finish(db, run, ok=4, errors=1, rows=12000, pushed=3,
                    detail={"KO": {"status": "error", "err": "HTTP 403"}}, note="one failed")
    latest = job_runs.latest(db, "nightly")
    assert latest.id == run.id and latest.symbols == 5 and latest.pushed == 3
    assert latest.detail["KO"]["err"] == "HTTP 403" and latest.finished_at is not None
    assert job_runs.running(db, "nightly") is False
    with pytest.raises(ValueError):
        job_runs.start(db, "bogus", "2026-10-02")

    # Friday 2026-10-02's run is due Saturday 08:00 MYT (= Sat 00:00 UTC)
    sat_0700_myt = _dt.datetime(2026, 10, 2, 23, 0, tzinfo=_dt.timezone.utc)
    sat_0900_myt = _dt.datetime(2026, 10, 3, 1, 0, tzinfo=_dt.timezone.utc)
    assert job_runs.due_day(sat_0700_myt) == _dt.date(2026, 10, 1)
    assert job_runs.due_day(sat_0900_myt) == _dt.date(2026, 10, 2)
    assert job_runs.missed(db, "nightly", now=sat_0900_myt) is False    # Friday's run is on file
    mon_2130_myt = _dt.datetime(2026, 10, 5, 13, 30, tzinfo=_dt.timezone.utc)
    assert job_runs.due_day(mon_2130_myt) == _dt.date(2026, 10, 2)      # Monday's run is not due yet
    assert job_runs.missed(db, "nightly", now=mon_2130_myt) is False
    tue_0900_myt = _dt.datetime(2026, 10, 6, 1, 0, tzinfo=_dt.timezone.utc)
    assert job_runs.due_day(tue_0900_myt) == _dt.date(2026, 10, 5)
    assert job_runs.missed(db, "nightly", now=tue_0900_myt) is True     # Monday's run never happened
    assert job_runs.missed(db, "refresh", now=tue_0900_myt) is True     # no refresh ever


# ───────────────────────────────── clock ─────────────────────────────────

def test_clock_session_and_holidays():
    assert clock.is_trading_day(_dt.date(2026, 10, 2))                  # a Friday
    assert not clock.is_trading_day(_dt.date(2026, 10, 3))              # Saturday
    assert not clock.is_trading_day(_dt.date(2026, 11, 26))             # Thanksgiving 2026
    assert not clock.is_trading_day(_dt.date(2026, 7, 3))               # Independence Day observed (Jul 4 is a Saturday)
    assert not clock.is_trading_day(_dt.date(2026, 4, 3))               # Good Friday 2026
    assert not clock.is_trading_day(_dt.date(2026, 6, 19))              # Juneteenth
    assert clock.is_trading_day(_dt.date(2026, 12, 31))
    assert clock.last_trading_day(_dt.date(2026, 11, 26)) == _dt.date(2026, 11, 25)
    assert clock.prev_trading_day(_dt.date(2026, 10, 5)) == _dt.date(2026, 10, 2)
    assert clock.next_trading_day(_dt.date(2026, 10, 2)) == _dt.date(2026, 10, 5)

    # 2026-10-02 (EDT, UTC-4): 09:30 ET = 13:30 UTC
    assert clock._us_session_open(_dt.datetime(2026, 10, 2, 13, 29, tzinfo=_dt.timezone.utc)) is False
    assert clock._us_session_open(_dt.datetime(2026, 10, 2, 13, 30, tzinfo=_dt.timezone.utc)) is True
    assert clock._us_session_open(_dt.datetime(2026, 10, 2, 19, 59, tzinfo=_dt.timezone.utc)) is True
    assert clock._us_session_open(_dt.datetime(2026, 10, 2, 20, 0, tzinfo=_dt.timezone.utc)) is False
    assert clock._us_session_open(_dt.datetime(2026, 10, 3, 15, 0, tzinfo=_dt.timezone.utc)) is False
    assert clock._us_session_open(_dt.datetime(2026, 11, 26, 16, 0, tzinfo=_dt.timezone.utc)) is False
    # a naive stamp is read as UTC, like every other naive stamp here
    assert clock._us_session_open(_dt.datetime(2026, 10, 2, 15, 0)) is True

    assert clock.et_today(_dt.datetime(2026, 10, 3, 2, 0, tzinfo=_dt.timezone.utc)) == "2026-10-02"
    close = clock.last_session_close(_dt.datetime(2026, 10, 3, 2, 0, tzinfo=_dt.timezone.utc))
    assert close.astimezone(_dt.timezone.utc) == _dt.datetime(2026, 10, 2, 20, 0, tzinfo=_dt.timezone.utc)
    during = clock.last_session_close(_dt.datetime(2026, 10, 2, 15, 0, tzinfo=_dt.timezone.utc))
    assert during.astimezone(_dt.timezone.utc) == _dt.datetime(2026, 10, 1, 20, 0, tzinfo=_dt.timezone.utc)
    assert clock.older_than_last_close(_dt.datetime(2026, 10, 1, 19, 59, 59),
                                       _dt.datetime(2026, 10, 2, 15, 0, tzinfo=_dt.timezone.utc)) is True
    assert clock.older_than_last_close(_dt.datetime(2026, 10, 1, 20, 0, 1),
                                       _dt.datetime(2026, 10, 2, 15, 0, tzinfo=_dt.timezone.utc)) is False
    assert clock.myt_now(_dt.datetime(2026, 10, 2, 13, 30, tzinfo=_dt.timezone.utc)).hour == 21
