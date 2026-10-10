"""Options data layer (OPTIONS_V2_DESIGN.md §2, §13): the migration ``3b26d60468a0`` and
``services/opt_store.py`` - since v4.134 written by ONE vendor (Massive) through the
collector; the v4.133 member machinery (contribution validation, rate limits, leases,
back-off, the IBKR spot band) is gone, and the IBKR build's rows are read only.

Every DB test runs on a fresh SQLite file brought to head by the real migration chain
(conftest), so the migration is what is exercised. Times are fixed naive-UTC datetimes
passed as ``now`` / ``as_of`` so nothing depends on the wall clock. No network.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import math

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

from app import models
from app.services import opt_store, option_metrics, support_bounce

from .conftest import DASH_ROOT, downgrade, make_engine, table_names, upgrade
from .fixtures.options import bars_synth, bs_greeks

REV = "3b26d60468a0"
PREV = "f4a5b6c7d8e9"
FIVE = ["opt_quote", "opt_underlying", "opt_underlying_daily", "opt_refresh_log",
        "opt_collector_status"]

TODAY = "2026-10-08"
T0 = _dt.datetime(2026, 10, 8, 15, 0, 0)          # naive UTC (Thu 11:00 ET, session open)
E1, E2, E3 = "2026-10-16", "2026-11-20", "2027-01-15"
MIN = _dt.timedelta(minutes=1)


@pytest.fixture(autouse=True)
def _clean_state():
    opt_store.reset_state()
    yield
    opt_store.reset_state()


# ───────────────────────────────── builders ─────────────────────────────────

def _row(expiry=E2, right="P", strike=100.0, **kw):
    """A quoted row priced off Black-Scholes (spot 100, iv 0.40) - a plan WITH quotes;
    a bad strike or expiry (for the sanity cases) is priced as the 100 strike, 30 days out."""
    try:
        T = max((_dt.date.fromisoformat(expiry) - _dt.date.fromisoformat(TODAY)).days, 1) / 365.0
    except (TypeError, ValueError):
        T = 30 / 365.0
    k = strike if isinstance(strike, (int, float)) and strike > 0 else 100.0
    g = bs_greeks(100.0, k, T, 0.40, str(right or "P"))
    mid = round(max(0.05, g["price"]), 2)
    row = {"expiry": expiry, "right": right, "strike": strike,
           "bid": round(mid - 0.05, 2), "ask": round(mid + 0.05, 2), "mid": None, "last": mid,
           "bid_size": 10, "ask_size": 12, "volume": 50, "oi": 1200, "iv": 0.40,
           "delta": round(g["delta"], 4), "gamma": round(g["gamma"], 5),
           "theta": round(g["theta"], 4), "vega": round(g["vega"], 4), "und_price": 100.0}
    row.update(kw)
    return row


def _mrow(expiry=E2, right="P", strike=100.0, **kw):
    """A Massive Options Starter row as ``opt_massive`` hands it over: no bid / ask, ``mid``
    = the model price from the contract's IV, ``last`` = the day's close."""
    row = _row(expiry, right, strike)
    row.update(bid=None, ask=None, bid_size=None, ask_size=None, mid=row["last"])
    row.update(kw)
    return row


def _basket(db, user, *symbols, active=True, owner=None):
    for i, s in enumerate(symbols):
        db.add(models.OptionBasket(user_id=(user.id if user is not None else None),
                                   owner_key=owner or ("u%d" % user.id if user is not None else "system"),
                                   symbol=s, active=active, added_on=TODAY, pos=i))
    db.commit()


def _user(db, email, name):
    u = models.User(email=email, display_name=name, role=models.ROLE_MEMBER,
                    status=models.APPROVED)
    db.add(u)
    db.commit()
    return u


def _quote(db, sym, expiry, right, strike):
    return (db.query(models.OptQuote)
              .filter_by(symbol=sym, expiry=expiry, right=right, strike=strike)
              .one())


def _legacy_quote(db, sym, expiry, right, strike, *, source="hermes", uid=None, mdt="live", as_of=T0, **kw):
    """A row the IBKR build (v4.133) left behind - written straight to the table."""
    q = models.OptQuote(symbol=sym, expiry=expiry, right=right, strike=strike, source=source,
                        source_user_id=uid, mdt=mdt, as_of=as_of, updated_at=as_of, **kw)
    db.add(q)
    db.commit()
    return q


# ───────────────────────────────── the migration ─────────────────────────────────

def test_migration_is_the_single_head_off_the_options_module():
    script = ScriptDirectory(str(DASH_ROOT / "alembic"))
    assert len(script.get_heads()) == 1
    rev = script.get_revision(REV)
    assert rev is not None and rev.down_revision == PREV


def test_migration_upgrade_downgrade_upgrade(db_url):
    upgrade(db_url, REV)
    have = table_names(db_url)
    assert set(FIVE) <= have
    eng = make_engine(db_url)
    try:
        insp = sa.inspect(eng)
        for name in FIVE:          # the migration and the models agree column for column
            cols = {c["name"] for c in insp.get_columns(name)}
            model_cols = {c.name for c in models.Base.metadata.tables[name].columns}
            assert cols == model_cols, (name, cols ^ model_cols)
        uniques = {u["name"]: u["column_names"] for u in insp.get_unique_constraints("opt_quote")}
        assert uniques["uq_opt_quote_contract"] == ["symbol", "expiry", "right", "strike"]
        assert {u["name"] for u in insp.get_unique_constraints("opt_underlying_daily")} == {"uq_opt_und_daily"}
        assert {"ix_opt_quote_symbol_expiry", "ix_opt_quote_as_of"} <= {
            i["name"] for i in insp.get_indexes("opt_quote")}
        assert "ix_opt_refresh_log_symbol_as_of" in {i["name"] for i in insp.get_indexes("opt_refresh_log")}
        fks = insp.get_foreign_keys("opt_quote")
        assert fks and fks[0]["referred_table"] == "users"
        assert fks[0]["options"].get("ondelete") == "SET NULL"
    finally:
        eng.dispose()

    downgrade(db_url, PREV)
    have = table_names(db_url)
    assert not (set(FIVE) & have)
    assert {"option_basket", "option_chain_snapshot", "users"} <= have      # nothing else touched

    upgrade(db_url, REV)
    assert set(FIVE) <= table_names(db_url)


def test_migration_guard_when_create_all_made_a_table_first(db_url):
    upgrade(db_url, PREV)
    eng = make_engine(db_url)
    try:
        models.OptQuote.__table__.create(eng)              # the legacy create_all situation
    finally:
        eng.dispose()
    upgrade(db_url, REV)                                   # must not raise
    assert set(FIVE) <= table_names(db_url)


def test_purge_migration_keeps_only_massive_data(db_url):
    """v4.135 (7c1e5a9d2b40): non-Massive option rows go, the daily history is emptied for
    a Massive rebuild, the derived stock figures are cleared, the basket stays."""
    upgrade(db_url, REV)
    T = models.Base.metadata.tables
    eng = make_engine(db_url)
    q = {"symbol": "SPY", "expiry": E2, "right": "P", "as_of": T0, "mdt": "delayed"}
    snap = {"symbol": "SPY", "snap_on": TODAY, "kind": "eod", "expiry": E2, "dte": 43, "right": "P"}
    try:
        with eng.begin() as c:
            c.execute(T["opt_quote"].insert(), [dict(q, strike=95.0, source="massive"),
                                                dict(q, strike=96.0, source="hermes"),
                                                dict(q, strike=97.0, source="member")])
            c.execute(T["opt_refresh_log"].insert(), [{"symbol": "SPY", "as_of": T0, "source": s}
                                                      for s in ("massive", "hermes", "member")])
            c.execute(T["option_chain_snapshot"].insert(), [dict(snap, strike=95.0, source="massive"),
                                                            dict(snap, strike=96.0, source="ibkr"),
                                                            dict(snap, strike=97.0, source="cboe")])
            c.execute(T["opt_underlying_daily"].insert(), [
                {"symbol": "SPY", "on": "2026-10-06", "close": 100.0, "iv30": 20.0, "source": "massive"},
                {"symbol": "SPY", "on": "2026-10-07", "close": 101.0, "iv30": 21.0, "source": "hermes"}])
            c.execute(T["opt_underlying"].insert(),
                      {"symbol": "SPY", "spot": 101.0, "spot_source": "massive", "spot_mdt": "delayed",
                       "atr14": 2.0, "hv20": 15.0, "iv30": 21.0, "iv_rank": 40.0, "iv_n": 200,
                       "earnings_date": "2027-01-20", "earnings_src": "yahoo", "history_done": True})
            c.execute(T["opt_underlying"].insert(),
                      {"symbol": "QQQ", "spot": 400.0, "spot_source": "hermes", "spot_user_id": 3,
                       "spot_mdt": "live", "iv30": 25.0, "history_done": True})
            c.execute(T["iv_daily"].insert(), [{"symbol": "SPY", "on": TODAY, "source": "ibkr"}])
            c.execute(T["option_signal"].insert(), [{"symbol": "SPY", "snap_on": TODAY, "kind": "eod",
                                                     "prefs_hash": "abc", "engine_version": "1",
                                                     "status": "ok"}])
            c.execute(T["option_basket"].insert(), [{"owner_key": "system", "symbol": "SPY", "added_on": TODAY}])
    finally:
        eng.dispose()

    upgrade(db_url, "head")
    eng = make_engine(db_url)
    try:
        with eng.connect() as c:
            def rows(name, *cols):
                return [tuple(r) for r in c.execute(sa.select(*(T[name].c[k] for k in cols))
                                                    .order_by(T[name].c.id))]
            assert rows("opt_quote", "strike", "source") == [(95.0, "massive")]
            assert rows("opt_refresh_log", "source") == [("massive",)]
            assert rows("option_chain_snapshot", "strike", "source") == [(95.0, "massive")]
            assert rows("opt_underlying_daily", "on") == []          # rebuilt from Massive by the collector
            assert rows("iv_daily", "symbol") == [] and rows("option_signal", "symbol") == []
            assert rows("option_basket", "symbol") == [("SPY",)]
            und = rows("opt_underlying", "symbol", "spot", "spot_source", "spot_user_id", "atr14", "hv20",
                       "iv30", "iv_rank", "iv_n", "earnings_date", "history_done")
            assert und == [("SPY", 101.0, "massive", None, None, None, None, None, None, "2027-01-20", False),
                           ("QQQ", None, None, None, None, None, None, None, None, None, False)]
    finally:
        eng.dispose()

    downgrade(db_url, REV)                                 # the no-op downgrade does not raise
    assert set(FIVE) <= table_names(db_url)


# ───────────────────────────────── the member machinery is gone (§13.1) ─────────────────────────────────

def test_the_member_machinery_is_gone_and_massive_is_the_writer():
    for name in ("validate_contribution", "drop_reasons", "check_rate", "next_for_member", "release_lease",
                 "is_leased", "report_failure", "backoff_until", "validate_history", "spot_reference",
                 "spot_band", "DELAY_S", "MEMBER_ASOF_SLACK_S", "LEASE_S", "RATE_SYMBOL_S", "MAX_CONTRACTS"):
        assert not hasattr(opt_store, name), name
    assert opt_store.SOURCES == ("massive",)
    assert set(opt_store.LEGACY_SOURCES) == {"hermes", "member"}
    assert opt_store.MDT == ("live", "delayed", "eod")
    for fn in (opt_store.upsert_quotes, opt_store.set_spot, opt_store.upsert_daily):
        params = inspect.signature(fn).parameters
        assert "user_id" not in params and params["source"].default == "massive", fn.__name__
    assert inspect.signature(opt_store.upsert_quotes).parameters["mdt"].default == "delayed"
    assert inspect.signature(opt_store.set_spot).parameters["mdt"].default == "delayed"


# ───────────────────────────────── upsert_quotes: the merge rule ─────────────────────────────────

def test_upsert_inserts_with_provenance_and_one_log_row(db):
    rows = [_row(strike=k) for k in (95.0, 100.0, 105.0)] + [_row(E3, "C", 110.0)]
    res = opt_store.upsert_quotes(db, "lrcx", rows, source="massive", mdt="delayed", as_of=T0,
                                  kind="cycle", ms=812, now=T0)
    assert res["stored"] == 4 and res["skipped_older"] == 0 and res["skipped_bad"] == 0
    assert res["n_expiries"] == 2 and isinstance(res["log_id"], int)
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.as_of, q.source, q.source_user_id, q.mdt, q.updated_at) == (T0, "massive", None, "delayed", T0)
    assert q.mid == pytest.approx((q.bid + q.ask) / 2.0)          # mid filled from bid / ask
    assert q.iv == 0.40 and q.oi == 1200 and q.und_price == 100.0
    logs = db.query(models.OptRefreshLog).all()
    assert len(logs) == 1
    lg = logs[0]
    assert (lg.symbol, lg.as_of, lg.source, lg.source_user_id, lg.mdt, lg.kind, lg.n_contracts,
            lg.n_expiries, lg.ms) == ("LRCX", T0, "massive", None, "delayed", "cycle", 4, 2, 812)
    # the defaults are Massive's: source massive, data type delayed (Options Starter)
    opt_store.upsert_quotes(db, "MSFT", [_mrow()], now=T0)
    q = _quote(db, "MSFT", E2, "P", 100.0)
    assert (q.source, q.mdt, q.as_of, q.bid, q.ask) == ("massive", "delayed", T0, None, None)


def test_upsert_newer_wins_older_skipped(db):
    t1, t0, t2 = T0, T0 - 5 * MIN, T0 + 5 * MIN
    opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.0, ask=2.2)], mdt="live", as_of=t1)
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=9.0, ask=9.2)], mdt="live", as_of=t0)
    assert res["stored"] == 0 and res["skipped_older"] == 1
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.as_of) == (2.0, t1)
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=3.0, ask=3.2)], mdt="live", as_of=t2)
    assert res["stored"] == 1
    db.expire_all()
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.as_of) == (3.0, t2)
    assert db.query(models.OptRefreshLog).count() == 3            # one log row per call, even a no-op


def test_upsert_equal_time_the_better_type_wins(db):
    opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.0)], mdt="delayed", as_of=T0)
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.1)], mdt="live", as_of=T0)
    assert res["stored"] == 1                                    # live beats delayed on a tie
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.5)], mdt="delayed", as_of=T0)
    assert res["stored"] == 0 and res["skipped_older"] == 1      # delayed never replaces live on a tie
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.2)], mdt="LIVE", as_of=T0)
    assert res["stored"] == 1                                    # same type, same time: the write wins (>=)
    db.expire_all()
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.mdt) == (2.2, "live")
    # a closing value (eod) ranks under a delayed read on a tie, and wins when newer
    opt_store.upsert_quotes(db, "LRCX", [_row(strike=105.0, bid=1.0)], mdt="delayed", as_of=T0)
    assert opt_store.upsert_quotes(db, "LRCX", [_row(strike=105.0, bid=1.1)], mdt="eod",
                                   as_of=T0)["stored"] == 0
    assert opt_store.upsert_quotes(db, "LRCX", [_row(strike=105.0, bid=1.2)], mdt="eod",
                                   as_of=T0 + MIN)["stored"] == 1
    # IBKR's codes and frozen types are not written any more
    for bad in (1, 3, "frozen", "delayed_frozen", "realtime", None):
        with pytest.raises(ValueError):
            opt_store.upsert_quotes(db, "LRCX", [_row()], mdt=bad, as_of=T0)


def test_upsert_leaves_contracts_not_in_the_payload_untouched(db):
    rows = [_row(strike=k, bid=1.0) for k in (95.0, 100.0, 105.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, mdt="delayed", as_of=T0)
    later = T0 + MIN
    opt_store.upsert_quotes(db, "LRCX", [_row(strike=100.0, bid=1.5)], mdt="live", as_of=later)
    db.expire_all()
    for k in (95.0, 105.0):
        q = _quote(db, "LRCX", E2, "P", k)
        assert (q.bid, q.as_of, q.mdt) == (1.0, T0, "delayed")
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.as_of, q.mdt) == (1.5, later, "live")
    assert db.query(models.OptQuote).count() == 3


def test_upsert_stamps_each_row_with_the_feeds_own_time(db):
    """§13.1: a row's as_of is the contract's own last_updated, so the market-time age is
    the data's age; the log row (and the spot) take the newest stamp of the read."""
    t1, t2 = T0 - 20 * MIN, T0 - 16 * MIN
    rows = [_mrow(strike=95.0, as_of=t1),
            _mrow(strike=100.0, last_updated=(t2.isoformat() + "Z")),                    # the client's key name
            _mrow(strike=105.0, as_of=t1.replace(tzinfo=_dt.timezone.utc).astimezone(
                _dt.timezone(_dt.timedelta(hours=-4))))]                                  # aware: converted
    res = opt_store.upsert_quotes(db, "SPY", rows, kind="cycle", spot=101.5, now=T0)
    assert res["stored"] == 3
    assert [_quote(db, "SPY", E2, "P", k).as_of for k in (95.0, 100.0, 105.0)] == [t1, t2, t1]
    assert all(_quote(db, "SPY", E2, "P", k).updated_at == T0 for k in (95.0, 100.0, 105.0))
    lg = db.query(models.OptRefreshLog).one()
    assert lg.as_of == t2                                                # the newest row's stamp
    und = opt_store.underlying(db, "SPY")
    assert (und["spot"], und["spot_as_of"], und["spot_source"], und["spot_mdt"]) == (101.5, t2, "massive", "delayed")
    fr = opt_store.freshness(db, ["SPY"], now=T0)["SPY"]
    assert (fr["as_of"], fr["age_min"], fr["source"]) == (t2, 16.0, "massive")   # the feed's delay shows
    # newer wins per ROW: an older stamp for one contract is skipped while a newer one is written
    res = opt_store.upsert_quotes(db, "SPY", [_mrow(strike=95.0, as_of=t1 - MIN, mid=9.0),
                                              _mrow(strike=100.0, as_of=t2 + MIN, mid=8.0)], now=T0)
    assert res["stored"] == 1 and res["skipped_older"] == 1
    db.expire_all()
    assert _quote(db, "SPY", E2, "P", 95.0).mid != 9.0 and _quote(db, "SPY", E2, "P", 100.0).mid == 8.0
    # a row without a stamp of its own takes the call's as_of (which also stamps the log) ...
    call = T0 - 30 * MIN
    opt_store.upsert_quotes(db, "SPY", [_mrow(strike=110.0), _mrow(strike=111.0, as_of=T0 - MIN)],
                            as_of=call, now=T0)
    assert _quote(db, "SPY", E2, "P", 110.0).as_of == call and _quote(db, "SPY", E2, "P", 111.0).as_of == T0 - MIN
    lg = db.query(models.OptRefreshLog).order_by(models.OptRefreshLog.id.desc()).first()
    assert lg.as_of == call
    # ... else server now
    opt_store.upsert_quotes(db, "SPY", [_mrow(strike=112.0)], now=T0)
    assert _quote(db, "SPY", E2, "P", 112.0).as_of == T0


def test_upsert_row_sanity_and_bad_arguments(db):
    good = _row(strike=100.0)
    drops = [_row(right="X"), _row(right=None), _row(strike=0), _row(strike=-5.0), "junk",
             {"expiry": "soon", "right": "P", "strike": 100.0},
             _row(strike=101.0, bid=-1.0),                                # a negative price
             _mrow(strike=102.0, mid=-0.5),
             _row(strike=103.0, last=-1.0),
             _row(strike=104.0, und_price=-5.0),
             _row(strike=106.0, iv=0.01), _row(strike=107.0, iv=5.0),     # iv outside (0.01, 5)
             _row(strike=108.0, iv=0.005), _row(strike=109.0, iv=7.5),
             _row(strike=111.0, delta=1.2), _row(strike=112.0, delta=-1.01)]
    keeps = [_row(right="call", strike=100.0, iv=None, delta=None),
             _row(right="PUT", strike=113.0),
             _row(strike=114.0, iv=float("nan"), oi=-1, volume="lots"),   # junk numbers read as absent
             _row(strike=115.0, iv=0.0101, delta=-1.0),                   # the edges are kept
             _row(strike=116.0, iv=4.99)]
    res = opt_store.upsert_quotes(db, "LRCX", [good] + drops + keeps, as_of=T0, spot=101.5)
    assert res["stored"] == 1 + len(keeps) and res["skipped_bad"] == len(drops)
    assert _quote(db, "LRCX", E2, "C", 100.0).iv is None
    assert _quote(db, "LRCX", E2, "P", 113.0).right == "P"
    q = _quote(db, "LRCX", E2, "P", 114.0)
    assert (q.iv, q.oi, q.volume) == (None, None, None)
    assert db.query(models.OptQuote).count() == 1 + len(keeps)
    und = opt_store.underlying(db, "LRCX")
    assert (und["spot"], und["spot_as_of"], und["spot_source"]) == (101.5, T0, "massive")
    # an older spot never replaces a newer one
    assert opt_store.set_spot(db, "LRCX", 99.0, mdt="live", as_of=T0 - _dt.timedelta(hours=1)) is False
    assert opt_store.underlying(db, "LRCX")["spot"] == 101.5
    assert opt_store.set_spot(db, "LRCX", 102.0, mdt="eod", as_of=T0 + _dt.timedelta(hours=1)) is True
    und = opt_store.underlying(db, "LRCX")
    assert (und["spot"], und["spot_mdt"], und["spot_user_id"]) == (102.0, "eod", None)
    for kw in ({"source": "hermes"}, {"source": "member"}, {"source": "cboe"}, {"mdt": "realtime"}):
        with pytest.raises(ValueError):
            opt_store.upsert_quotes(db, "LRCX", [], **kw)
        with pytest.raises(ValueError):
            opt_store.set_spot(db, "LRCX", 100.0, **kw)
    with pytest.raises(ValueError):
        opt_store.upsert_quotes(db, "", [])
    with pytest.raises(ValueError):
        opt_store.set_spot(db, "LRCX", 0)


def test_mid_is_the_midpoint_with_quotes_else_the_model_price(db):
    rows = [_row(bid=1.0, ask=1.1, mid=500.0),               # quoted: the midpoint, a posted mid ignored
            _mrow(strike=105.0, mid=3.25),                   # no quotes (Starter): the model price posted
            _row(strike=110.0, bid=None, mid=2.0)]           # one side only: the posted price too
    opt_store.upsert_quotes(db, "LRCX", rows, as_of=T0, now=T0)
    assert _quote(db, "LRCX", E2, "P", 100.0).mid == pytest.approx(1.05)
    q = _quote(db, "LRCX", E2, "P", 105.0)
    assert (q.bid, q.ask, q.mid) == (None, None, 3.25)
    assert _quote(db, "LRCX", E2, "P", 110.0).mid == 2.0
    puts = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)["expiries"][0]["puts"]
    assert [(p["strike"], p["mid"]) for p in puts] == [(100.0, pytest.approx(1.05)), (105.0, 3.25), (110.0, 2.0)]
    # a quoted row whose stored mid disagrees (written before the rule) is shown at the midpoint
    q = _quote(db, "LRCX", E2, "P", 100.0)
    q.mid = 500.0
    db.commit()
    opt_store.reset_state()
    put = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)["expiries"][0]["puts"][0]
    assert put["mid"] == pytest.approx(1.05)


# ───────────────────────────────── the IBKR build's rows: read, then replaced ─────────────────────────────────

def test_legacy_rows_are_read_and_always_replaced_by_a_massive_read(db, user):
    later = T0 + _dt.timedelta(hours=1)                         # newer than the Massive read below
    _legacy_quote(db, "LRCX", E2, "P", 100.0, source="hermes", mdt="live", as_of=later, bid=9.0, ask=9.2,
                  mid=9.1, iv=0.5, und_price=120.0)
    _legacy_quote(db, "LRCX", E2, "P", 105.0, source="member", uid=user.id, mdt="delayed_frozen", as_of=later,
                  bid=4.0, ask=4.4, mid=4.2, und_price=120.0)
    db.add(models.OptUnderlying(symbol="LRCX", spot=120.0, spot_as_of=later, spot_source="member",
                                spot_user_id=user.id, spot_mdt="live", first_seen=T0, history_done=False))
    db.commit()
    view = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)
    p100, p105 = view["expiries"][0]["puts"]
    assert (p100["source"], p100["mdt"], p100["mid"]) == ("hermes", "live", pytest.approx(9.1))
    assert (p105["source"], p105["source_user_id"], p105["source_name"], p105["mdt"]) == \
        ("member", user.id, "Member", "delayed_frozen")
    assert (view["spot"], view["spot_source"]) == (120.0, "member")
    # a Massive read replaces both although its feed stamps are older
    res = opt_store.upsert_quotes(db, "LRCX", [_mrow(strike=100.0, as_of=T0), _mrow(strike=105.0, as_of=T0)],
                                  spot=101.0, now=T0)
    assert res["stored"] == 2 and res["skipped_older"] == 0
    db.expire_all()
    for k in (100.0, 105.0):
        q = _quote(db, "LRCX", E2, "P", k)
        assert (q.source, q.source_user_id, q.mdt, q.as_of, q.bid) == ("massive", None, "delayed", T0, None)
    und = opt_store.underlying(db, "LRCX")
    assert (und["spot"], und["spot_source"], und["spot_user_id"], und["spot_mdt"]) == \
        (101.0, "massive", None, "delayed")
    # from then on newer-wins holds between Massive reads
    assert opt_store.upsert_quotes(db, "LRCX", [_mrow(strike=100.0, as_of=T0 - MIN)],
                                   now=T0)["skipped_older"] == 1
    # set_spot replaces a legacy spot too
    db.add(models.OptUnderlying(symbol="MSFT", spot=300.0, spot_as_of=later, spot_source="hermes",
                                spot_mdt="live", first_seen=T0, history_done=False))
    db.commit()
    assert opt_store.set_spot(db, "MSFT", 310.0, mdt="eod", as_of=T0) is True
    und = opt_store.underlying(db, "MSFT")
    assert (und["spot"], und["spot_source"], und["spot_mdt"], und["spot_as_of"]) == (310.0, "massive", "eod", T0)
    assert opt_store.set_spot(db, "MSFT", 305.0, as_of=T0 - MIN) is False
    # the EOD record labels a legacy row "ibkr", a Massive one "massive"
    _legacy_quote(db, "LRCX", E2, "P", 110.0, source="hermes", as_of=later, bid=12.0, ask=12.4)
    assert opt_store.snapshot_eod(db, "LRCX", TODAY) == 3
    S = models.OptionChainSnapshot
    assert {s.strike: s.source for s in db.query(S).filter_by(symbol="LRCX", snap_on=TODAY)} == \
        {100.0: "massive", 105.0: "massive", 110.0: "ibkr"}


# ───────────────────────────────── chain_view ─────────────────────────────────

ROW_KEYS = {"strike", "bid", "ask", "mid", "last", "bid_size", "ask_size", "volume", "oi", "iv",
            "delta", "gamma", "theta", "vega", "und_price", "as_of", "source", "source_user_id",
            "source_name", "mdt"}


def test_chain_view_shape_order_and_expired_hidden(db, user):
    rows = []
    for exp in (E3, "2026-10-02", E1, E2):                      # 2026-10-02 is already expired
        for k in (110.0, 90.0, 100.0):
            rows += [_row(exp, "C", k), _row(exp, "P", k)]
    rows[-1]["mid"] = None
    opt_store.upsert_quotes(db, "LRCX", rows, mdt="delayed", as_of=T0)
    # one row as the IBKR build left it: a member's (read only)
    q = _quote(db, "LRCX", E1, "C", 100.0)
    q.source, q.source_user_id, q.mdt, q.as_of = "member", user.id, "live", T0 + MIN
    db.commit()
    view = opt_store.chain_view(db, "lrcx", today=TODAY)
    assert set(view) == {"symbol", "spot", "spot_as_of", "spot_source", "spot_mdt", "expiries"}
    assert view["symbol"] == "LRCX"
    assert [e["expiry"] for e in view["expiries"]] == [E1, E2, E3]
    assert [e["dte"] for e in view["expiries"]] == [8, 43, 99]
    for e in view["expiries"]:
        assert set(e) == {"expiry", "dte", "calls", "puts"}
        for side in ("calls", "puts"):
            assert [r["strike"] for r in e[side]] == [90.0, 100.0, 110.0]
            for r in e[side]:
                assert set(r) == ROW_KEYS
                assert isinstance(r["as_of"], _dt.datetime) and r["as_of"].tzinfo is None
                assert r["mid"] is not None
    c = view["expiries"][0]["calls"][1]
    assert (c["source"], c["source_user_id"], c["source_name"], c["mdt"]) == ("member", user.id, "Member", "live")
    assert c["as_of"] == T0 + MIN
    p = view["expiries"][0]["puts"][0]
    assert (p["source"], p["source_user_id"], p["source_name"], p["mdt"]) == ("massive", None, None, "delayed")
    assert p["iv"] == 0.40 and p["delta"] < 0                   # FRACTION, signed
    # no opt_underlying row: the spot falls back to the newest quote's und_price
    assert view["spot"] == 100.0 and view["spot_source"] == "member"
    opt_store.set_spot(db, "LRCX", 104.2, mdt="live", as_of=T0)
    view = opt_store.chain_view(db, "LRCX", today=TODAY)
    assert (view["spot"], view["spot_as_of"], view["spot_source"], view["spot_mdt"]) == \
        (104.2, T0, "massive", "live")
    empty = opt_store.chain_view(db, "NONE", today=TODAY)
    assert empty["expiries"] == [] and empty["spot"] is None


def test_massive_rows_through_chain_view(db):
    """Starter-shaped rows (no bid / ask, the model price as mid, the feed's 15-min-old
    stamp) come out of chain_view with their source, feed and stamp intact."""
    stamp = T0 - 16 * MIN
    T = (_dt.date.fromisoformat(E2) - _dt.date.fromisoformat(TODAY)).days / 365.0
    rows = []
    for k in range(80, 101):
        g = bs_greeks(100.0, float(k), T, 0.30, "P")
        rows.append({"expiry": E2, "right": "put", "strike": float(k), "bid": None, "ask": None,
                     "mid": round(g["price"], 2), "last": round(g["price"], 2), "volume": 40, "oi": 900,
                     "iv": 0.30, "delta": round(g["delta"], 4), "gamma": round(g["gamma"], 5),
                     "theta": round(g["theta"], 4), "vega": round(g["vega"], 4), "as_of": stamp})
    res = opt_store.upsert_quotes(db, "spy", rows, source="massive", mdt="delayed", kind="cycle",
                                  spot=100.0, now=T0)
    assert res["stored"] == 21 and res["skipped_bad"] == 0
    view = opt_store.chain_view(db, "SPY", today=TODAY, now=T0, dte_min=30, dte_max=60, max_age_h=24 + 96)
    puts = view["expiries"][0]["puts"]
    assert len(puts) == 21 and view["expiries"][0]["calls"] == []
    assert all(p["bid"] is None and p["ask"] is None and p["mid"] > 0 for p in puts)
    assert {(p["source"], p["mdt"], p["source_user_id"], p["source_name"], p["as_of"]) for p in puts} == \
        {("massive", "delayed", None, None, stamp)}
    assert (view["spot"], view["spot_source"], view["spot_mdt"], view["spot_as_of"]) == \
        (100.0, "massive", "delayed", stamp)


def test_chain_view_filters_cache_and_copies(db, engine):
    rows = [_row(e, r, k) for e in (E1, E2, E3) for r in ("C", "P") for k in (95.0, 100.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, mdt="live", as_of=T0 - _dt.timedelta(hours=30))   # Wed 09:00 UTC
    opt_store.upsert_quotes(db, "LRCX", [_row(E2, "P", 100.0, bid=4.0, ask=4.4)], mdt="live", as_of=T0, now=T0)

    def exps(**kw):
        return [e["expiry"] for e in opt_store.chain_view(db, "LRCX", today=TODAY, now=T0, **kw)["expiries"]]

    assert exps() == [E1, E2, E3]                                       # DTE 8, 43, 99
    assert exps(dte_min=10, dte_max=60) == [E2]
    assert exps(dte_min=8, dte_max=99) == [E1, E2, E3]                  # the bounds are inclusive
    assert exps(dte_min=44) == [E3] and exps(dte_max=7) == [] and exps(dte_min=50, dte_max=40) == []
    # max_age_h: wall clock, the cutoff floored to the hour (a little more, never less)
    v = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0, max_age_h=24)
    assert [(e["expiry"], [p["strike"] for p in e["puts"]], e["calls"]) for e in v["expiries"]] == \
        [(E2, [100.0], [])]
    assert v["expiries"][0]["puts"][0]["source"] == "massive" and v["expiries"][0]["puts"][0]["bid"] == 4.0
    assert exps(max_age_h=29.5) == [E1, E2, E3]                         # 09:30 -> 09:00: the 09:00 rows stay
    assert exps(max_age_h=29.0) == [E2]

    # the cache: an identical second call reads no quote rows; a write changes the key
    opt_store.reset_state()                                             # start cold
    seen = []

    def hook(_conn, _cursor, statement, _params, _context, _many):
        seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", hook)
    try:
        v1 = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)
        n_first = sum("FROM opt_quote" in s for s in seen)
        seen.clear()
        v2 = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)
        assert n_first == 1 and sum("FROM opt_quote" in s for s in seen) == 0
    finally:
        sa.event.remove(engine, "before_cursor_execute", hook)
    assert v1 == v2
    v1["expiries"][0]["puts"][0]["bid"] = -1.0                           # the caller's own copy
    v1["expiries"][0]["puts"].clear()
    v3 = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)
    assert v3 == v2
    opt_store.upsert_quotes(db, "LRCX", [_row(E1, "P", 95.0, bid=0.5, ask=0.7)], mdt="live",
                            as_of=T0 + 5 * _dt.timedelta(seconds=1), now=T0)
    v4 = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)
    assert v4["expiries"][0]["puts"][0]["bid"] == 0.5
    # the spot is read fresh every call (a set_spot writes no refresh-log row)
    opt_store.set_spot(db, "LRCX", 101.0, mdt="live", as_of=T0 + _dt.timedelta(seconds=9))
    assert opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)["spot"] == 101.0
    # another day is another key (DTE and the expired cut move with it)
    assert [e["dte"] for e in opt_store.chain_view(db, "LRCX", today="2026-10-09", now=T0)["expiries"]] == \
        [7, 42, 98]


# ───────────────────────────────── daily history + stats ─────────────────────────────────

def _bars(n=300, kind="uptrend_bounce"):
    out = []
    for b in bars_synth(kind, n=n, end="2026-10-02"):
        out.append({"on": b["time"], "open": b["open"], "high": b["high"], "low": b["low"],
                    "close": b["close"], "volume": b["volume"]})
    return out


def _iv_series(bars):
    return [{"on": b["on"], "iv": round(30.0 + 10.0 * math.sin(i / 17.0) + 0.01 * i, 3)}
            for i, b in enumerate(bars)]


def test_upsert_daily_and_recompute_underlying_numbers(db):
    bars = _bars()
    ivs = _iv_series(bars)
    n = opt_store.upsert_daily(db, "LRCX", bars=bars, iv_series=ivs, today=TODAY, now=T0)
    assert n == 300
    assert db.query(models.OptUnderlyingDaily).count() == 300
    assert {r.source for r in db.query(models.OptUnderlyingDaily)} == {"massive"}
    und = opt_store.recompute_underlying(db, "LRCX", now=T0)

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    assert und["atr14"] == pytest.approx(support_bounce.atr_series(highs, lows, closes)[-1])
    assert und["hv20"] == pytest.approx(option_metrics.hv(closes, 20))
    assert und["hv60"] == pytest.approx(option_metrics.hv(closes, 60))
    assert und["avg_vol20"] == pytest.approx(sum(b["volume"] for b in bars[-20:]) / 20)
    series = [p["iv"] for p in ivs]
    rk = option_metrics.iv_rank_pct(series[-252:], series[-1])
    assert und["iv30"] == series[-1]
    assert (und["iv_rank"], und["iv_pct"], und["iv_n"], und["iv_lo"], und["iv_hi"]) == \
        (rk["iv_rank"], rk["iv_pct"], 252, rk["lo"], rk["hi"])
    assert und["iv_lo"] == min(series[-252:])                   # the window is the last 252 only
    assert und["bars_as_of"] == T0 and und["iv_as_of"] == T0
    assert und["history_done"] is False
    opt_store.mark_history_done(db, "LRCX")
    assert opt_store.underlying(db, "LRCX")["history_done"] is True


def test_wilder_atr_by_hand_and_short_history():
    # every true range is 2.0 -> ATR 2.0 exactly
    closes = [100.0 + i for i in range(20)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    assert opt_store._wilder_atr(highs, lows, closes) == pytest.approx(2.0)
    # Wilder smoothing: 14 TRs of 1.0 (SMA 1.0), then one TR of 15.0 -> (1*13 + 15) / 14 = 2.0
    c = [100.0] * 16
    h = [100.5] * 15 + [107.0]
    lo = [99.5] * 15 + [92.0]
    assert opt_store._wilder_atr(h, lo, c) == pytest.approx((1.0 * 13 + 15.0) / 14)
    assert opt_store._wilder_atr(h[:14], lo[:14], c[:14]) is None


def test_upsert_daily_field_by_field_and_bounds(db):
    # the IV series first, the bars later: neither erases the other
    assert opt_store.upsert_daily(db, "LRCX", iv_series=[{"on": "2026-10-01", "iv": 41.0}],
                                  source="massive", today=TODAY, now=T0) == 1
    assert opt_store.upsert_daily(db, "LRCX", bars=[{"on": "2026-10-01", "open": 1, "high": 105,
                                                     "low": 99, "close": 101, "volume": 1e6}],
                                  today=TODAY, now=T0) == 1
    r = db.query(models.OptUnderlyingDaily).filter_by(symbol="LRCX", on="2026-10-01").one()
    assert (r.iv30, r.close, r.high, r.low, r.volume, r.source) == (41.0, 101, 105, 99, 1e6, "massive")
    # a later value for the same day replaces the earlier one
    opt_store.upsert_daily(db, "LRCX", bars=[{"on": "2026-10-01", "high": 106, "low": 99,
                                              "close": 102, "volume": 2e6}],
                           iv_series=[("2026-10-01", 42.5)], today=TODAY, now=T0)
    db.expire_all()
    r = db.query(models.OptUnderlyingDaily).filter_by(symbol="LRCX", on="2026-10-01").one()
    assert (r.iv30, r.close, r.high) == (42.5, 102, 106)
    # skipped: future day, close <= 0, high < low, iv out of bounds, unreadable date
    n = opt_store.upsert_daily(
        db, "LRCX",
        bars=[{"on": "2026-10-09", "high": 1, "low": 1, "close": 1},
              {"on": "2026-09-30", "high": 1, "low": 1, "close": 0},
              {"on": "2026-09-29", "high": 1, "low": 2, "close": 1.5},
              {"on": "someday", "high": 1, "low": 1, "close": 1}],
        iv_series=[{"on": "2026-09-28", "iv": 0.05}, {"on": "2026-09-27", "iv": 2000}],
        today=TODAY, now=T0)
    assert n == 0
    assert db.query(models.OptUnderlyingDaily).count() == 1
    # too little history: the stats are None, not invented
    und = opt_store.recompute_underlying(db, "LRCX", now=T0)
    assert und["atr14"] is None and und["hv20"] is None and und["avg_vol20"] is None
    assert und["iv30"] == 42.5 and und["iv_rank"] is None and und["iv_n"] == 1
    # only Massive writes; a legacy IBKR day is updated in place and becomes Massive's
    for bad in ("hermes", "member"):
        with pytest.raises(ValueError):
            opt_store.upsert_daily(db, "LRCX", iv_series=[{"on": "2026-10-01", "iv": 40.0}], source=bad,
                                   today=TODAY, now=T0)
    db.add(models.OptUnderlyingDaily(symbol="LRCX", on="2026-09-25", close=90.0, high=91.0, low=89.0,
                                     iv30=55.0, source="hermes", as_of=T0))
    db.commit()
    opt_store.upsert_daily(db, "LRCX", bars=[{"on": "2026-09-25", "high": 96, "low": 94, "close": 95.0}],
                           today=TODAY, now=T0)
    r = db.query(models.OptUnderlyingDaily).filter_by(symbol="LRCX", on="2026-09-25").one()
    assert (r.close, r.high, r.iv30, r.source) == (95.0, 96, 55.0, "massive")


def test_earnings_and_underlyings(db):
    opt_store.set_earnings(db, "LRCX", "2026-10-22", now=T0)
    opt_store.set_earnings(db, "MSFT", {"date": _dt.date(2026, 10, 28)}, now=T0)
    opt_store.set_earnings(db, "NVDA", None, now=T0)
    got = opt_store.underlyings(db, ["lrcx", "MSFT", "NVDA", "NONE"])
    assert set(got) == {"LRCX", "MSFT", "NVDA"}
    assert (got["LRCX"]["earnings_date"], got["LRCX"]["earnings_src"], got["LRCX"]["earnings_as_of"]) == \
        ("2026-10-22", "yahoo", T0)
    assert got["MSFT"]["earnings_date"] == "2026-10-28"
    assert got["NVDA"]["earnings_date"] is None and got["NVDA"]["earnings_as_of"] == T0
    assert set(got["LRCX"]) == {c.name for c in models.OptUnderlying.__table__.columns}
    assert opt_store.underlying(db, "NONE") is None
    assert opt_store.underlyings(db, []) == {}


# ───────────────────────────────── freshness + universe ─────────────────────────────────

def test_freshness_reads_the_newest_log_per_symbol(db, user):
    t1, t2, t3 = T0, T0 + 10 * MIN, T0 + 20 * MIN
    opt_store.upsert_quotes(db, "AAA", [_mrow()], as_of=t1)
    opt_store.upsert_quotes(db, "AAA", [_mrow(strike=105.0)], as_of=t2, kind="manual")
    # a later read that wrote nothing (an error) does not make the symbol look fresh
    opt_store.upsert_quotes(db, "AAA", [], as_of=t3, error="timeout")
    opt_store.upsert_quotes(db, "BBB", [_mrow(), _mrow(strike=95.0)], mdt="eod", as_of=t1, kind="eod")
    fr = opt_store.freshness(db, ["aaa", "BBB", "CCC"], now=t3)
    assert set(fr) == {"AAA", "BBB"}
    assert fr["AAA"] == {"as_of": t2, "source": "massive", "source_user_id": None, "source_name": None,
                         "mdt": "delayed", "n": 1, "kind": "manual", "age_min": 10.0}
    assert fr["BBB"]["n"] == 2 and fr["BBB"]["age_min"] == 20.0 and fr["BBB"]["kind"] == "eod"
    assert fr["BBB"]["mdt"] == "eod"
    assert opt_store.freshness(db, []) == {}
    # a legacy member read is still reported, with the member's name
    db.add(models.OptRefreshLog(symbol="CCC", as_of=t2, source="member", source_user_id=user.id, mdt="live",
                                kind="member", n_contracts=3, n_expiries=1))
    db.commit()
    fr = opt_store.freshness(db, ["CCC"], now=t3)["CCC"]
    assert (fr["source"], fr["source_user_id"], fr["source_name"], fr["kind"]) == ("member", user.id, "Member",
                                                                                   "member")


def test_freshness_ignores_legacy_trade_refreshes_and_failure_rows(db, user):
    opt_store.upsert_quotes(db, "AAA", [_mrow()], kind="cycle", as_of=T0 - _dt.timedelta(hours=2))
    L = models.OptRefreshLog
    db.add(L(symbol="AAA", as_of=T0, source="member", source_user_id=user.id, mdt="live", kind="trade",
             n_contracts=4, n_expiries=1))
    db.add(L(symbol="AAA", as_of=T0, source="member", source_user_id=user.id, mdt=None, kind="member",
             n_contracts=0, n_expiries=0, error="timeout"))
    db.add(L(symbol="BBB", as_of=T0, source="member", source_user_id=user.id, mdt="live", kind="trade",
             n_contracts=2, n_expiries=1))
    db.commit()
    fr = opt_store.freshness(db, ["AAA", "BBB"], now=T0)
    assert (fr["AAA"]["as_of"], fr["AAA"]["kind"], fr["AAA"]["source"]) == \
        (T0 - _dt.timedelta(hours=2), "cycle", "massive")
    assert "BBB" not in fr                                          # a trade refresh alone never counts


def test_universe_counts_members_per_active_symbol(db, user):
    u2 = _user(db, "b@local.test", "Bee")
    u3 = _user(db, "c@local.test", "Cee")
    _basket(db, user, "MSFT", "LRCX", "AAPL")
    _basket(db, u2, "LRCX", "AAPL")
    _basket(db, u3, "LRCX")
    _basket(db, u3, "ZZZ", active=False)
    _basket(db, None, "SPY", owner="system")
    assert opt_store.universe(db) == [("LRCX", 3), ("AAPL", 2), ("MSFT", 1), ("SPY", 0)]


def _log_rows(sym, n, newest_min, **kw):
    row = dict(symbol=sym, source="massive", mdt="delayed", kind="cycle", n_contracts=10, n_expiries=1)
    row.update(kw)
    return [dict(row, as_of=T0 - _dt.timedelta(minutes=newest_min + n - i)) for i in range(n)]


def _vm_steps(db, fn) -> int:
    """SQLite virtual-machine steps (in thousands) ``fn`` costs on ``db``'s connection."""
    raw = db.connection().connection.dbapi_connection
    n = [0]

    def tick():
        n[0] += 1
        return 0

    raw.set_progress_handler(tick, 1000)
    try:
        fn()
    finally:
        raw.set_progress_handler(None, 1000)
    return n[0]


def test_freshness_is_one_bounded_query(db, engine):
    L = models.OptRefreshLog
    # good reads, then 50 newer rows that do not count (failed reads and legacy trade refreshes)
    rows = _log_rows("AAA", 400, 600)
    rows += [dict(r, source="member", kind="trade" if i % 2 else "member", n_contracts=4 if i % 2 else 0)
             for i, r in enumerate(_log_rows("AAA", 50, 0))]
    rows += _log_rows("BBB", 400, 0)
    db.execute(L.__table__.insert(), rows)
    db.commit()
    seen = []

    def hook(_conn, _cursor, statement, params, _context, _many):
        seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", hook)
    try:
        fr = opt_store.freshness(db, ["AAA", "BBB", "CCC"], now=T0)
    finally:
        sa.event.remove(engine, "before_cursor_execute", hook)
    assert fr["AAA"]["as_of"] == T0 - _dt.timedelta(minutes=601) and fr["AAA"]["kind"] == "cycle"
    assert fr["BBB"]["as_of"] == T0 - _dt.timedelta(minutes=1) and "CCC" not in fr
    assert len(seen) == 1                                            # one statement

    # bounded: ten times more retained (older) log rows cost the query about nothing - each
    # symbol's pick walks its index from the newest row and stops at the first that counts
    syms = ["AAA", "BBB"]
    before = _vm_steps(db, lambda: opt_store.freshness(db, syms, now=T0))
    db.execute(L.__table__.insert(), _log_rows("AAA", 4000, 1100) + _log_rows("BBB", 4000, 500))
    db.commit()
    after = _vm_steps(db, lambda: opt_store.freshness(db, syms, now=T0))
    assert opt_store.freshness(db, syms, now=T0)["AAA"]["as_of"] == T0 - _dt.timedelta(minutes=601)
    assert after <= before * 1.5 + 2, (before, after)


# ───────────────────────────────── collector status ─────────────────────────────────

def test_collector_status_get_and_set(db):
    assert opt_store.collector_status(db) is None
    aware = _dt.datetime(2026, 10, 8, 23, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    opt_store.set_collector_status(db, state="cycle", gateway="api.massive.com", gateway_ok=True,
                                   mdt="delayed", cycle_n=3, cycle_started=aware, symbols_total=30,
                                   symbols_done=18, pid=4242, version="3.0", bogus=1, now=T0)
    st = opt_store.collector_status(db)
    assert st["id"] == 1 and st["state"] == "cycle" and st["gateway_ok"] is True
    assert st["cycle_started"] == _dt.datetime(2026, 10, 8, 15, 0)      # stored naive UTC
    assert st["heartbeat"] == T0 and st["symbols_done"] == 18 and st["version"] == "3.0"
    assert "bogus" not in st
    later = T0 + _dt.timedelta(seconds=15)
    opt_store.set_collector_status(db, state="error", last_error="x" * 50, gateway_ok=False,
                                   phase_detail="Massive rejected the API key", state_extra=None, now=later)
    st = opt_store.collector_status(db)
    assert st["state"] == "error" and st["gateway_ok"] is False
    assert st["cycle_n"] == 3 and st["pid"] == 4242                   # untouched fields stay
    assert st["heartbeat"] == later
    opt_store.set_collector_status(db, state="a-very-long-state-name", heartbeat=T0)
    st = opt_store.collector_status(db)
    assert st["state"] == "a-very-long-"[:12] and st["heartbeat"] == T0
    assert db.query(models.OptCollectorStatus).count() == 1


# ───────────────────────────────── EOD snapshot + prune ─────────────────────────────────

def test_snapshot_eod_copies_and_replaces(db):
    rows = [_row("2026-10-02", "P", 100.0)]                    # expired before the day: not copied
    rows += [_row(E1, r, k) for r in ("C", "P") for k in (95.0, 100.0)]
    rows += [_mrow(E2, "P", 100.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, mdt="delayed", as_of=T0)
    assert opt_store.snapshot_eod(db, "LRCX", TODAY) == 5
    S = models.OptionChainSnapshot
    snap = db.query(S).filter_by(symbol="LRCX", snap_on=TODAY).order_by(S.expiry, S.right, S.strike).all()
    assert len(snap) == 5
    assert {(s.kind, s.source) for s in snap} == {("eod", "massive")}
    assert {s.expiry: s.dte for s in snap} == {E1: 8, E2: 43}
    q = _quote(db, "LRCX", E1, "C", 95.0)
    s = next(x for x in snap if (x.expiry, x.right, x.strike) == (E1, "C", 95.0))
    assert (s.bid, s.ask, s.mid, s.iv, s.delta, s.oi, s.volume) == \
        (q.bid, q.ask, q.mid, q.iv, q.delta, q.oi, q.volume)
    assert s.iv == 0.40                                        # still a FRACTION
    m = next(x for x in snap if x.expiry == E2)
    assert m.bid is None and m.mid == _quote(db, "LRCX", E2, "P", 100.0).mid   # the model price is kept
    # a second run replaces, never duplicates; another day's rows are untouched
    db.add(S(symbol="LRCX", snap_on="2026-10-07", kind="eod", source="cboe", expiry=E1, dte=9,
             right="P", strike=90.0))
    db.commit()
    assert opt_store.snapshot_eod(db, "LRCX", TODAY) == 5
    assert db.query(S).filter_by(symbol="LRCX", snap_on=TODAY).count() == 5
    assert db.query(S).filter_by(symbol="LRCX", snap_on="2026-10-07").count() == 1
    # no quotes: nothing written, nothing replaced
    assert opt_store.snapshot_eod(db, "NONE", TODAY) == 0


def test_prune_v2(db, monkeypatch):
    from app.services import option_store

    L = models.OptRefreshLog
    for days in (31, 29, 1):
        db.add(L(symbol="LRCX", as_of=T0 - _dt.timedelta(days=days), source="massive", mdt="delayed",
                 kind="cycle", n_contracts=1, n_expiries=1))
    db.commit()
    rows = [_row("2026-09-30", "P", 100.0, iv=0.4),            # expired 8 days ago: pruned
            _row("2026-10-01", "P", 100.0),                    # expired 7 days ago: kept (grace)
            _row(E1, "P", 100.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, as_of=T0, now=T0)
    # quotes nobody WROTE for 7 days (outside every read's window, or no longer listed): pruned
    old = T0 - _dt.timedelta(days=7, minutes=1)
    opt_store.upsert_quotes(db, "LRCX", [_row(E3, "P", 95.0)], as_of=old, now=old)
    recent = T0 - _dt.timedelta(days=6, hours=23)
    opt_store.upsert_quotes(db, "LRCX", [_row(E3, "P", 90.0)], as_of=recent, now=recent)
    # a contract that has not TRADED for 10 days but was re-read at T0 stays: the write time counts
    opt_store.upsert_quotes(db, "LRCX", [_mrow(E3, "P", 85.0, as_of=T0 - _dt.timedelta(days=10))], now=T0)
    # a legacy IBKR row nobody replaced for a week ages out the same way
    _legacy_quote(db, "LRCX", E3, "P", 80.0, as_of=T0 - _dt.timedelta(days=8))

    calls = []
    monkeypatch.setattr(option_store, "prune", lambda d, today=None: calls.append(today) or {"snapshots_old": 0})
    out = opt_store.prune_v2(db, TODAY, now=T0)
    assert out["refresh_log"] == 1 and out["quotes_expired"] == 1 and out["quotes_stale"] == 2
    assert out["snapshots"] == {"snapshots_old": 0} and calls == [TODAY]
    assert db.query(L).count() == 6                         # 29 d, 1 d and upsert_quotes' four rows
    assert sorted((q.expiry, q.strike) for q in db.query(models.OptQuote)) == \
        [("2026-10-01", 100.0), (E1, 100.0), (E3, 85.0), (E3, 90.0)]

    monkeypatch.delattr(option_store, "prune")
    out = opt_store.prune_v2(db, TODAY, now=T0)
    assert out["snapshots"] is None and out["refresh_log"] == 0 and out["quotes_expired"] == 0
    assert out["quotes_stale"] == 0
