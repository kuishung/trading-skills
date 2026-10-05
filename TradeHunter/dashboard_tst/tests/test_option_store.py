"""The persistence foundations of the Options module (step 1): the migration
``f4a5b6c7d8e9`` and ``services/option_store.py``, ``job_runs.py``, ``clock.py``.

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


def _sig(picks_ok=True, recommended="bull_put"):
    strategies = [{"key": recommended, "label": "Bull put spread", "fit": "recommended",
                   "score": 90.1, "step": 1, "reasons": [], "reason_key": None, "shown": True},
                  {"key": "buy_call", "label": "Buy call", "fit": "rejected", "score": None,
                   "reasons": ["options too expensive to buy (IV rank 62)"],
                   "reason_key": "expensive", "shown": True}]
    if picks_ok:
        picks = {recommended: [{"symbol": "LRCX", "strategy": recommended,
                                "family": "credit_vertical", "status": "ok",
                                "chart_stop_pl": -120.7, "max_loss": 790.0,
                                "legs": [], "sizing": None}]}
    else:
        picks = {recommended: [{"status": "nearest", "degenerate": {"reason_key": "no_band"}}]}
    return {"status": "ok", "headline": "Uptrend for 34 days.",
            "setup": {"kind": "support_bounce", "direction": "long", "rng": {"sideways": False}},
            "iv": {"iv30": 46.0, "iv_rank": 62.0, "basis": "rank", "iv_n": 252},
            "strategies": strategies, "picks": picks, "computed_ms": 120,
            "engine_version": "test"}


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

    downgrade(db_url, "-1")
    have = table_names(db_url)
    assert not (set(NINE) & have)
    assert "option_spreads" in have and _spread_rows(db_url) == before

    upgrade(db_url, "head")                                 # and back up: the copy runs again, once
    assert len(_trade_rows(db_url)) == 3


# ───────────────────────────────── snapshot + iv_daily ─────────────────────────────────

def test_replace_snapshot_round_trip_eod_over_intraday(db):
    intraday = _chain(kind="intraday", as_of=_dt.datetime(2026, 10, 2, 15, 0, 0))
    n1 = option_store.replace_snapshot(db, intraday)
    option_store.upsert_iv_daily(db, intraday, {"hv20": 38.0})
    db.commit()
    assert n1 == 7
    assert option_store.latest_snap_on(db, "LRCX")[:2] == ("2026-10-02", "intraday")

    eod = _chain(kind="eod")
    n2 = option_store.replace_snapshot(db, eod)
    option_store.upsert_iv_daily(db, eod, {"hv20": 38.5, "iv_rank": 62.0, "n": 252, "state": "ok"})
    db.commit()
    assert n2 == 7
    # both kinds coexist in the snapshot table; the day header says eod
    kinds = {k for (k,) in db.query(models.OptionChainSnapshot.kind).distinct()}
    assert kinds == {"eod", "intraday"}
    day, kind, as_of = option_store.latest_snap_on(db, "LRCX")
    assert (day, kind) == ("2026-10-02", "eod")
    assert as_of == _dt.datetime(2026, 10, 2, 19, 59, 59)
    hdr = db.query(models.IVDaily).filter_by(symbol="LRCX", on="2026-10-02").one()
    assert hdr.kind == "eod" and hdr.iv30 == 46.0 and hdr.iv30_src == "cboe"
    assert hdr.hv20 == 38.5 and hdr.iv_n == 252 and hdr.iv_state == "ok"
    assert hdr.n_contracts == 7 and hdr.n_expiries == 2 and hdr.spot == 349.2

    # idempotent: the same chain twice -> identical counts
    option_store.replace_snapshot(db, eod)
    db.commit()
    assert db.query(models.OptionChainSnapshot).filter_by(kind="eod").count() == 7

    chain = option_store.latest_chain(db, "LRCX")
    assert chain.kind == "eod" and chain.spot == 349.2 and chain.iv30 == 46.0
    assert len(chain.rows) == 7
    r = [x for x in chain.rows if x.right == "P" and x.strike == 300.0][0]
    assert r.mid == pytest.approx(1.05) and r.dte == 49 and r.oi == 1000 and r.volume is None
    assert r.iv == pytest.approx(0.40)
    legs = chain.legs()
    assert legs[("2026-11-20", "P", 300.0)]["open_interest"] == 1000

    with pytest.raises(ValueError):
        option_store.replace_snapshot(db, _chain(kind="live"))      # live is never stored


def test_intraday_refresh_replaces_older_intraday_rows(db):
    option_store.replace_snapshot(db, _chain(snap_on="2026-10-01", kind="intraday"))
    option_store.replace_snapshot(db, _chain(snap_on="2026-10-02", kind="intraday"))
    db.commit()
    days = {d for (d,) in db.query(models.OptionChainSnapshot.snap_on)
                            .filter_by(kind="intraday").distinct()}
    assert days == {"2026-10-02"}


def test_bootstrap_iv_never_overwrites_a_cboe_day_and_rejects_out_of_bounds(db):
    today = "2026-10-02"
    eod = _chain(snap_on=today)
    option_store.replace_snapshot(db, eod)
    option_store.upsert_iv_daily(db, eod, {})
    db.commit()
    series = [{"on": "2026-09-30", "iv": 31.2}, {"on": "2026-10-01", "iv": 33.0},
              {"on": today, "iv": 99.0},                    # the server read this day: kept at 46.0
              {"on": "2026-10-03", "iv": 30.0},             # future -> rejected
              {"on": "2026-09-29", "iv": 1200.0},           # above 1000 -> rejected
              {"on": "2026-09-28", "iv": 0.05},             # below 0.1 -> rejected
              {"on": "2024-01-01", "iv": 30.0},             # older than 400 days -> rejected
              ("2026-09-25", 28.4)]                         # a pair works too
    out = option_store.bootstrap_iv(db, "lrcx", series, source="ibkr", today=today)
    assert (out["inserted"], out["skipped"], out["rejected"]) == (3, 1, 4)
    assert out["n_total"] == 4
    rows = {r.on: r for r in db.query(models.IVDaily).filter_by(symbol="LRCX").all()}
    assert rows[today].iv30 == 46.0 and rows[today].source == "cboe" and rows[today].kind == "eod"
    assert rows["2026-09-30"].iv30 == 31.2                  # percent, stored as-is
    assert rows["2026-09-30"].kind == "history" and rows["2026-09-30"].source == "ibkr"
    assert rows["2026-09-30"].iv30_src == "ibkr"
    assert option_store.iv_series(db, "LRCX", 252) == [28.4, 31.2, 33.0, 46.0]
    assert option_store.iv_series(db, "LRCX", 2, until="2026-10-01") == [31.2, 33.0]

    job = job_runs.latest(db, "bootstrap")
    assert job is not None and job.rows == 3 and job.symbols == 1 and job.run_on == today

    # a second bootstrap of the same series inserts nothing
    again = option_store.bootstrap_iv(db, "LRCX", series[:3], today=today)
    assert again["inserted"] == 0 and again["skipped"] == 3

    with pytest.raises(ValueError):
        option_store.bootstrap_iv(db, "LRCX", [{"on": "2026-09-01", "iv": 30.0}] * 401, today=today)


def test_bootstrap_iv_marks_latest_signal_rows_stale(db):
    eod = _chain()
    option_store.replace_snapshot(db, eod)
    option_store.upsert_iv_daily(db, eod, {})
    option_store.upsert_signal(db, _sig(), prefs_hash=HOUSE, symbol="LRCX",
                               snap_on="2026-10-02", kind="eod")
    option_store.upsert_signal(db, _sig(), prefs_hash=HOUSE, symbol="LRCX",
                               snap_on="2026-10-01", kind="eod")
    db.commit()
    out = option_store.bootstrap_iv(db, "LRCX", [{"on": "2026-09-30", "iv": 31.2}],
                                    today="2026-10-02")
    assert out["stale_marked"] == 1
    st = {r.snap_on: r.status for r in db.query(models.OptionSignal).all()}
    assert st == {"2026-10-02": "stale_iv", "2026-10-01": "ok"}


def test_backfill_from_iv_history(db):
    for i in range(300):
        on = (_dt.date(2025, 1, 1) + _dt.timedelta(days=i)).isoformat()
        db.add(models.IVHistory(symbol="MSFT", on=on, iv30=20.0 + i % 10, spot=400.0,
                                source="cboe" if i % 2 else "ibkr"))
    for i in range(50):                                     # 50 already present
        on = (_dt.date(2025, 1, 1) + _dt.timedelta(days=i)).isoformat()
        db.add(models.IVDaily(symbol="MSFT", on=on, kind="eod", source="cboe", iv30=99.0))
    db.commit()
    assert option_store.backfill_from_iv_history(db) == 250
    assert option_store.backfill_from_iv_history(db) == 0
    rows = {r.on: r for r in db.query(models.IVDaily).filter_by(symbol="MSFT").all()}
    assert len(rows) == 300
    assert rows["2025-01-01"].iv30 == 99.0                  # the server-read day untouched
    r = rows["2025-03-01"]
    assert r.kind == "history" and r.source == "iv_history" and r.spot == 400.0
    assert r.iv30_src in ("cboe", "ibkr")


# ───────────────────────────────── prune ─────────────────────────────────

def test_prune(db, user, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "options_snapshot_days", 90)
    monkeypatch.setattr(settings, "options_full_days", 7)
    today = _dt.date(2026, 10, 2)
    for i in range(100):                                    # 100 EOD days, 7 rows each
        day = (today - _dt.timedelta(days=i)).isoformat()
        option_store.replace_snapshot(db, _chain(snap_on=day, kind="eod"))
        option_store.upsert_signal(db, _sig(), prefs_hash=HOUSE, symbol="LRCX",
                                   snap_on=day, kind="eod")
    option_store.replace_snapshot(db, _chain(snap_on=(today - _dt.timedelta(days=1)).isoformat(),
                                             kind="intraday"))
    # an expired contract 8 days back, and one 6 days back (kept)
    expired = _chain(snap_on="2026-09-20")
    expired["rows"] = [dict(expired["rows"][3], expiry="2026-09-24"),
                       dict(expired["rows"][4], expiry="2026-09-26")]
    option_store.replace_snapshot(db, expired)
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


# ───────────────────────────────── the read paths ─────────────────────────────────

def test_card_for_none_when_no_row(db, user, monkeypatch):
    # the read path only: a hash miss normally recomputes from the stored chain
    # (the engines' own tests cover that); here the compute is stubbed to "could
    # not" so the None path is what is exercised
    monkeypatch.setattr(option_store, "_lazy_compute", lambda *a, **k: None)
    assert option_store.card_for(db, "LRCX", user, prefs={}, prefs_hash=MINE, house_hash=HOUSE) is None
    # a snapshot without any signal row (and no compute possible) is still None
    option_store.replace_snapshot(db, _chain())
    option_store.upsert_iv_daily(db, _chain(), {})
    db.commit()
    assert option_store.card_for(db, "LRCX", user, prefs={}, prefs_hash=MINE, house_hash=HOUSE) is None


def test_card_for_serves_house_row_and_sizes_at_read_time(db, user, monkeypatch):
    eod = _chain()
    option_store.replace_snapshot(db, eod)
    option_store.upsert_iv_daily(db, eod, {})
    option_store.upsert_signal(db, _sig(), prefs_hash=HOUSE, symbol="LRCX",
                               snap_on="2026-10-02", kind="eod", as_of=eod["as_of"])
    db.commit()
    monkeypatch.setattr(option_store, "_engine_version", lambda: "test")
    monkeypatch.setattr(option_store.clock, "et_date", lambda now=None: _dt.date(2026, 10, 2))
    # the member's hash has no row and the recompute is stubbed to "could not":
    # the house row stands in (own_rules False)
    monkeypatch.setattr(option_store, "_lazy_compute", lambda *a, **k: None)

    card = option_store.card_for(db, "LRCX", user, prefs={}, prefs_hash=MINE, house_hash=HOUSE)
    assert card is not None
    assert card["prefs_hash"] == HOUSE and card["own_rules"] is False
    assert card["kind"] == "eod" and card["snap_on"] == "2026-10-02" and card["source"] == "cboe"
    assert card["stale"] is False and card["age_h"] is not None
    assert card["recommended"] == "bull_put" and card["trend"] == "up"
    assert "account" in card
    pick = card["picks"]["bull_put"][0]
    try:
        import app.services.option_sizing  # noqa: F401
    except ImportError:
        assert pick["sizing"] is None and "sizing_error" in card      # tolerated, and said so
    else:
        assert pick["sizing"] is not None or "sizing_error" in card
    # read-time only: the stored row still carries sizing = null
    row = db.query(models.OptionSignal).one()
    assert row.picks["bull_put"][0]["sizing"] is None

    # an engine-version mismatch treats the row as missing
    monkeypatch.setattr(option_store, "_engine_version", lambda: "newer")
    assert option_store.card_for(db, "LRCX", user, prefs={}, prefs_hash=MINE, house_hash=HOUSE) is None


def test_basket_rows_for_pick_state(db, user, monkeypatch):
    monkeypatch.setattr(option_store, "_engine_version", lambda: "test")
    monkeypatch.setattr(option_store.clock, "et_date", lambda now=None: _dt.date(2026, 10, 2))
    for i, sym in enumerate(["LRCX", "MSFT", "KO", "ZZZZ", "NVDA"]):
        db.add(models.OptionBasket(user_id=user.id, owner_key="u%d" % user.id, symbol=sym,
                                   source="typed", added_on="2026-10-01", pos=i))
    db.add(models.OptionBasket(user_id=user.id, owner_key="u%d" % user.id, symbol="AAPL",
                               source="typed", added_on="2026-10-01", pos=9, active=False))
    db.add(models.OptionBasket(user_id=None, owner_key="system", symbol="SPY",
                               source="system", added_on="2026-10-01"))
    day = "2026-10-02"
    # LRCX: house row + member row with real picks
    option_store.upsert_signal(db, _sig(), prefs_hash=HOUSE, symbol="LRCX", snap_on=day)
    option_store.upsert_signal(db, _sig(), prefs_hash=MINE, symbol="LRCX", snap_on=day)
    # MSFT: member row exists, picks hold only the "nearest" stub
    option_store.upsert_signal(db, _sig(), prefs_hash=HOUSE, symbol="MSFT", snap_on=day)
    option_store.upsert_signal(db, _sig(picks_ok=False), prefs_hash=MINE, symbol="MSFT", snap_on=day)
    # KO: only the house row on the latest day; the member's row is a day old
    option_store.upsert_signal(db, _sig(), prefs_hash=HOUSE, symbol="KO", snap_on=day)
    option_store.upsert_signal(db, _sig(), prefs_hash=MINE, symbol="KO", snap_on="2026-10-01")
    # NVDA: an old row only, two sessions back -> stale
    option_store.upsert_signal(db, _sig(), prefs_hash=MINE, symbol="NVDA", snap_on="2026-09-29")
    # a row of another engine version must be ignored
    old = _sig()
    old["engine_version"] = "older"
    option_store.upsert_signal(db, old, prefs_hash=MINE, symbol="ZZZZ", snap_on=day)
    db.commit()

    rows = option_store.basket_rows_for(db, user, prefs={}, prefs_hash=MINE, house_hash=HOUSE)
    assert list(rows) == ["LRCX", "MSFT", "KO", "ZZZZ", "NVDA"]        # basket order, inactive left out
    assert rows["LRCX"]["pick_state"] == "has_picks" and rows["LRCX"]["idea"] == "bull_put"
    assert rows["LRCX"]["trend"] == "up" and rows["LRCX"]["iv"] == {"iv_rank": 62.0, "basis": "rank", "iv_n": 252}
    assert rows["LRCX"]["stale"] is False
    assert rows["MSFT"]["pick_state"] == "no_strike_passes"
    assert rows["KO"]["pick_state"] == "not_checked" and rows["KO"]["trend"] == "up"
    assert rows["KO"]["snap_on"] == day
    assert rows["ZZZZ"]["pick_state"] == "not_checked" and rows["ZZZZ"]["trend"] is None
    assert rows["ZZZZ"]["stale"] is True
    assert rows["NVDA"]["pick_state"] == "has_picks" and rows["NVDA"]["stale"] is True

    assert option_store.basket_universe(db) == ["AAPL", "KO", "LRCX", "MSFT", "NVDA", "SPY", "ZZZZ"] or \
        option_store.basket_universe(db) == ["KO", "LRCX", "MSFT", "NVDA", "SPY", "ZZZZ"]
    assert "AAPL" not in option_store.basket_universe(db)             # inactive rows are not fetched


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


def test_stale_is_session_based_not_wall_clock():
    fri = _dt.datetime(2026, 10, 2, 23, 0, tzinfo=_dt.timezone.utc)     # Friday 19:00 ET
    assert option_store._stale("2026-10-02", fri) is False
    assert option_store._stale("2026-10-01", fri) is False              # the previous session
    assert option_store._stale("2026-09-30", fri) is True               # one session older
    sun = _dt.datetime(2026, 10, 4, 12, 0, tzinfo=_dt.timezone.utc)
    assert option_store._stale("2026-10-01", sun) is False              # Thu is still "previous" on a Sunday
    assert option_store._stale("2026-09-30", sun) is True
    assert option_store._stale(None, sun) is True


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
