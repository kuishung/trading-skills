"""Options v2 data layer (OPTIONS_V2_DESIGN.md §2): the migration ``3b26d60468a0`` and
``services/opt_store.py``.

Every DB test runs on a fresh SQLite file brought to head by the real migration chain
(conftest), so the migration is what is exercised. Times are fixed naive-UTC datetimes
passed as ``now`` / ``as_of`` so nothing depends on the wall clock.
"""
from __future__ import annotations

import datetime as _dt
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
T0 = _dt.datetime(2026, 10, 8, 15, 0, 0)          # naive UTC
E1, E2, E3 = "2026-10-16", "2026-11-20", "2027-01-15"


@pytest.fixture(autouse=True)
def _clean_state():
    opt_store.reset_state()
    yield
    opt_store.reset_state()


# ───────────────────────────────── builders ─────────────────────────────────

def _row(expiry=E2, right="P", strike=100.0, **kw):
    """A th_ibkr-shaped row priced off Black-Scholes (spot 100, iv 0.40); a bad strike
    or expiry (for the validation cases) is priced as the 100 strike, 30 days out."""
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


# ───────────────────────────────── upsert_quotes: the merge rule ─────────────────────────────────

def test_upsert_inserts_with_provenance_and_one_log_row(db):
    rows = [_row(strike=k) for k in (95.0, 100.0, 105.0)] + [_row(E3, "C", 110.0)]
    res = opt_store.upsert_quotes(db, "lrcx", rows, source="hermes", mdt="delayed", as_of=T0,
                                  kind="cycle", ms=812, now=T0)
    assert res["stored"] == 4 and res["skipped_older"] == 0 and res["skipped_bad"] == 0
    assert res["n_expiries"] == 2 and isinstance(res["log_id"], int)
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.as_of, q.source, q.source_user_id, q.mdt) == (T0, "hermes", None, "delayed")
    assert q.mid == pytest.approx((q.bid + q.ask) / 2.0)          # mid filled from bid / ask
    assert q.iv == 0.40 and q.oi == 1200 and q.und_price == 100.0
    logs = db.query(models.OptRefreshLog).all()
    assert len(logs) == 1
    lg = logs[0]
    assert (lg.symbol, lg.as_of, lg.source, lg.mdt, lg.kind, lg.n_contracts, lg.n_expiries, lg.ms) == \
        ("LRCX", T0, "hermes", "delayed", "cycle", 4, 2, 812)


def test_upsert_newer_wins_older_skipped(db):
    t1, t0, t2 = T0, T0 - _dt.timedelta(minutes=5), T0 + _dt.timedelta(minutes=5)
    opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.0, ask=2.2)], source="hermes", mdt="live", as_of=t1)
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=9.0, ask=9.2)], source="hermes", mdt="live", as_of=t0)
    assert res["stored"] == 0 and res["skipped_older"] == 1
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.as_of) == (2.0, t1)
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=3.0, ask=3.2)], source="hermes", mdt="live", as_of=t2)
    assert res["stored"] == 1
    db.expire_all()
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.as_of) == (3.0, t2)
    assert db.query(models.OptRefreshLog).count() == 3            # one log row per call, even a no-op


def test_upsert_equal_time_live_beats_delayed(db):
    opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.0)], source="hermes", mdt="delayed", as_of=T0)
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.1)], source="hermes", mdt="live", as_of=T0)
    assert res["stored"] == 1                                    # live beats delayed on a tie
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.5)], source="hermes", mdt="delayed", as_of=T0)
    assert res["stored"] == 0 and res["skipped_older"] == 1      # delayed never replaces live on a tie
    res = opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.2)], source="hermes", mdt="live", as_of=T0)
    assert res["stored"] == 1                                    # same type, same time: the write wins (>=)
    db.expire_all()
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.mdt) == (2.2, "live")
    # an integer IBKR code is accepted and stored by name
    opt_store.upsert_quotes(db, "LRCX", [_row(bid=2.3)], source="hermes", mdt=1,
                            as_of=T0 + _dt.timedelta(seconds=1))
    db.expire_all()
    assert _quote(db, "LRCX", E2, "P", 100.0).mdt == "live"


def test_upsert_leaves_contracts_not_in_the_payload_untouched(db):
    rows = [_row(strike=k, bid=1.0) for k in (95.0, 100.0, 105.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, source="hermes", mdt="delayed", as_of=T0)
    later = T0 + _dt.timedelta(minutes=1)
    opt_store.upsert_quotes(db, "LRCX", [_row(strike=100.0, bid=1.5)], source="hermes", mdt="live",
                            as_of=later)
    db.expire_all()
    for k in (95.0, 105.0):
        q = _quote(db, "LRCX", E2, "P", k)
        assert (q.bid, q.as_of, q.mdt) == (1.0, T0, "delayed")
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.bid, q.as_of, q.mdt) == (1.5, later, "live")
    assert db.query(models.OptQuote).count() == 3


def test_upsert_member_as_of_is_the_server_receive_time(db, user):
    now = T0
    # a back-dated client stamp is replaced by server now
    opt_store.upsert_quotes(db, "LRCX", [_row(strike=100.0)], source="member", mdt="live",
                            user_id=user.id, as_of=now - _dt.timedelta(days=2), kind="member", now=now)
    q = _quote(db, "LRCX", E2, "P", 100.0)
    assert (q.as_of, q.source, q.source_user_id, q.mdt) == (now, "member", user.id, "live")
    # a forward-dated stamp too
    opt_store.upsert_quotes(db, "LRCX", [_row(strike=105.0)], source="member", mdt="live",
                            user_id=user.id, as_of=now + _dt.timedelta(hours=1), now=now)
    assert _quote(db, "LRCX", E2, "P", 105.0).as_of == now
    # a receive time captured by the route moments earlier is kept
    recv = now - _dt.timedelta(seconds=3)
    opt_store.upsert_quotes(db, "LRCX", [_row(strike=110.0)], source="member", mdt="live",
                            user_id=user.id, as_of=recv, now=now)
    assert _quote(db, "LRCX", E2, "P", 110.0).as_of == recv
    # hermes never records a user
    opt_store.upsert_quotes(db, "LRCX", [_row(strike=115.0)], source="hermes", mdt="delayed",
                            user_id=user.id, as_of=now)
    assert _quote(db, "LRCX", E2, "P", 115.0).source_user_id is None
    lg = db.query(models.OptRefreshLog).order_by(models.OptRefreshLog.id).first()
    assert (lg.source, lg.source_user_id, lg.kind) == ("member", user.id, "member")


def test_upsert_spot_bad_rows_and_bad_arguments(db):
    rows = [_row(strike=100.0), _row(right="X"), _row(strike=0), {"expiry": "soon"}, "junk",
            _row(strike=101.0, bid=-1.0, iv=float("nan"))]
    res = opt_store.upsert_quotes(db, "LRCX", rows, source="hermes", mdt="frozen", as_of=T0,
                                  spot=101.5)
    assert res["stored"] == 2 and res["skipped_bad"] == 4
    q = _quote(db, "LRCX", E2, "P", 101.0)
    assert q.bid is None and q.iv is None                        # IBKR's -1 / NaN are not data
    und = opt_store.underlying(db, "LRCX")
    assert (und["spot"], und["spot_as_of"], und["spot_source"], und["spot_mdt"]) == \
        (101.5, T0, "hermes", "frozen")
    # an older spot never replaces a newer one
    opt_store.set_spot(db, "LRCX", 99.0, source="hermes", mdt="live", as_of=T0 - _dt.timedelta(hours=1))
    assert opt_store.underlying(db, "LRCX")["spot"] == 101.5
    opt_store.set_spot(db, "LRCX", 102.0, source="hermes", mdt="live", as_of=T0 + _dt.timedelta(hours=1))
    assert opt_store.underlying(db, "LRCX")["spot"] == 102.0
    with pytest.raises(ValueError):
        opt_store.upsert_quotes(db, "LRCX", [], source="cboe", mdt="live")
    with pytest.raises(ValueError):
        opt_store.upsert_quotes(db, "LRCX", [], source="hermes", mdt="realtime")
    with pytest.raises(ValueError):
        opt_store.upsert_quotes(db, "", [], source="hermes", mdt="live")


# ───────────────────────────────── validation ─────────────────────────────────

def _payload(**kw):
    p = {"symbol": "LRCX", "spot": 100.0, "mdt": "live",
         "rows": [_row(strike=k) for k in (90.0, 100.0, 110.0)],
         "connector_version": "2.0", "client_as_of": "2026-10-08T15:00:00Z"}
    p.update(kw)
    return p


def test_validation_accepts_a_good_payload(db, user):
    _basket(db, user, "LRCX")
    clean, err, dropped = opt_store.validate_contribution(db, _payload(symbol="lrcx", mdt=3),
                                                          user_id=user.id, today=TODAY, now=T0)
    assert err is None and dropped == 0
    assert set(clean) == {"symbol", "spot", "mdt", "rows"}
    assert (clean["symbol"], clean["spot"], clean["mdt"]) == ("LRCX", 100.0, "delayed")
    assert len(clean["rows"]) == 3
    r = clean["rows"][1]
    assert r["strike"] == 100.0 and r["mid"] == pytest.approx((r["bid"] + r["ask"]) / 2)
    # the clean dict feeds upsert_quotes as is
    res = opt_store.upsert_quotes(db, clean["symbol"], clean["rows"], source="member",
                                  mdt=clean["mdt"], user_id=user.id, spot=clean["spot"], now=T0)
    assert res["stored"] == 3


def test_validation_rejects_whole_payloads(db, user):
    other = _user(db, "b@local.test", "Bee")
    _basket(db, user, "LRCX")
    _basket(db, other, "OFFX", active=False)          # an inactive row does not count

    def why(payload):
        clean, err, _dropped = opt_store.validate_contribution(db, payload, user_id=user.id,
                                                               today=TODAY, now=T0)
        assert clean is None and err
        return err

    assert "JSON object" in why(["not", "a", "dict"])
    assert "symbol" in why(_payload(symbol=""))
    assert "symbol" in why(_payload(symbol="LR CX;"))
    assert "basket" in why(_payload(symbol="MSFT"))
    assert "basket" in why(_payload(symbol="OFFX"))
    assert "list" in why(_payload(rows={"a": 1}))
    assert "too many" in why(_payload(rows=[_row()] * 4001))
    assert "spot" in why(_payload(spot=None))
    assert "spot" in why(_payload(spot=0))
    assert "spot" in why(_payload(spot=-5))
    assert "market data type" in why(_payload(mdt="realtime"))
    assert "no usable" in why(_payload(rows=[_row(right="X")]))
    assert "no usable" in why(_payload(rows=[]))

    # a Hermes spot one trading day old bounds the new one (no iv30 / hv20: 40% -> the 15% floor)
    opt_store.set_spot(db, "LRCX", 100.0, source="hermes", mdt="live", as_of=T0 - _dt.timedelta(days=1))
    assert "15%" in why(_payload(spot=116.0, rows=[_row(strike=k) for k in (110.0, 115.0)]))
    clean, err, _ = opt_store.validate_contribution(db, _payload(spot=114.0), user_id=user.id,
                                                    today=TODAY, now=T0)
    assert err is None and clean["spot"] == 114.0

    # 4000 contracts is still allowed (strikes on the 0.50 grid, 20 expiries)
    exps = [(_dt.date(2026, 10, 9) + _dt.timedelta(weeks=w)).isoformat() for w in range(20)]
    many = [_row(e, r, 50.0 + 0.5 * i) for e in exps for r in ("C", "P") for i in range(100)]
    clean, err, dropped = opt_store.validate_contribution(db, _payload(rows=many), user_id=user.id,
                                                          today=TODAY, now=T0)
    assert err is None and len(clean["rows"]) == 4000 and dropped == 0


def test_validation_never_raises(user):
    clean, err, dropped = opt_store.validate_contribution(None, _payload(), user_id=user.id)
    assert clean is None and err and dropped == 0


@pytest.mark.parametrize("bad, reason", [
    ("junk", "bad_row"),
    (_row(right="X"), "right"),
    (_row(right=None), "right"),
    (_row(expiry="next week"), "expiry"),
    (_row(expiry="2026-10-07"), "expiry"),               # in the past
    (_row(strike=0), "strike"),
    (_row(strike=-5), "strike"),
    (_row(strike=19.0), "strike"),                       # < 0.2 x spot
    (_row(strike=501.0), "strike"),                      # > 5 x spot
    (_row(bid="abc"), "bad_number"),
    (_row(bid=2.0, ask=1.5), "crossed"),
    (_row(bid=-0.5, ask=1.0), "negative_price"),
    (_row(last=-1.0), "negative_price"),
    (_row(iv=0.01), "iv"),                               # the bounds are exclusive
    (_row(iv=5.0), "iv"),
    (_row(iv=0.005), "iv"),
    (_row(iv=7.5), "iv"),
    (_row(delta=1.2), "delta"),
    (_row(right="P", delta=-1.01), "delta"),
    (_row(oi=-1), "negative_size"),
    (_row(volume=-3), "negative_size"),
    # the review of v4.133: listing grid, price plausibility, delta sign
    (_row(expiry="2026-11-21"), "expiry"),               # a Saturday
    (_row(expiry="2099-06-19"), "expiry"),               # more than 1100 days out
    (_row(strike=100.25), "strike"),                     # off the 0.50 grid
    (_row(strike=100.0001), "strike"),
    (_row(right="C", strike=100.0, bid=105.5, ask=106.0), "price"),   # call above spot x 1.05
    (_row(right="P", strike=50.0, bid=1.0, ask=53.0), "price"),       # put above strike x 1.05
    (_row(right="C", strike=60.0, bid=37.0, ask=37.9), "price"),      # 2.10 under intrinsic 40
    (_row(right="P", strike=150.0, bid=45.0, ask=47.9), "price"),     # 2.10 under intrinsic 50
    (_row(right="C", delta=-0.30), "delta"),             # a call's delta is never negative
    (_row(right="P", delta=0.30), "delta"),              # a put's never positive
])
def test_validation_drop_reasons(db, user, bad, reason):
    _basket(db, user, "LRCX")
    rows = [_row(strike=100.0), bad]
    assert opt_store.drop_reasons(rows, 100.0, today=TODAY) == {reason: 1}
    clean, err, dropped = opt_store.validate_contribution(db, _payload(rows=rows), user_id=user.id,
                                                          today=TODAY, now=T0)
    assert err is None and dropped == 1 and len(clean["rows"]) == 1


def test_validation_keeps_rows_at_the_edges(db, user):
    _basket(db, user, "LRCX")
    rows = [_row(expiry=TODAY, strike=100.0),             # expiring today is not past
            _row(strike=20.0),                           # exactly 0.2 x spot ...
            _row(strike=500.0, bid=400.0, ask=400.4),    # ... and 5 x spot (an American put >= intrinsic)
            _row(right="call", strike=100.0, iv=None, delta=None, bid=None, ask=None),
            _row(strike=101.0, iv=0.0101, delta=-1.0),
            _row(right="C", strike=60.0, bid=37.5, ask=38.0),     # exactly $2 (2% of spot) under intrinsic
            _row(right="C", strike=99.5, bid=104.0, ask=105.0),   # exactly spot x 1.05
            _row(right="P", strike=100.5, delta=0.0),             # a zero delta has no sign
            _row(E2, "P", 102.5, bid=1.0, ask=1.1, mid=500.0)]    # a posted mid is ignored
    clean, err, dropped = opt_store.validate_contribution(db, _payload(rows=rows), user_id=user.id,
                                                          today=TODAY, now=T0)
    assert err is None and dropped == 0 and len(clean["rows"]) == 9
    assert clean["rows"][3]["right"] == "C" and clean["rows"][3]["mid"] is None
    assert clean["rows"][8]["mid"] == pytest.approx(1.05)
    exp_1100 = (_dt.date.fromisoformat(TODAY) + _dt.timedelta(days=1100))
    while exp_1100.weekday() >= 5:
        exp_1100 -= _dt.timedelta(days=1)
    assert opt_store.drop_reasons([_row(exp_1100.isoformat())], 100.0, today=TODAY) == {}
    over = exp_1100 + _dt.timedelta(days=1)
    while over.weekday() >= 5 or (over - _dt.date.fromisoformat(TODAY)).days <= 1100:
        over += _dt.timedelta(days=1)
    assert opt_store.drop_reasons([_row(over.isoformat())], 100.0, today=TODAY) == {"expiry": 1}


# ───────────────────────────────── rate limit ─────────────────────────────────

def test_rate_limit_per_symbol_and_per_minute():
    t = 1_000_000.0
    assert opt_store.check_rate(7, "LRCX", now=t) is None
    assert "LRCX" in opt_store.check_rate(7, "lrcx", now=t + 5)
    assert opt_store.check_rate(7, "MSFT", now=t + 1) is None          # another symbol
    assert opt_store.check_rate(8, "LRCX", now=t + 1) is None          # another member
    assert opt_store.check_rate(7, "LRCX", now=t + 2, bucket="history") is None
    assert opt_store.check_rate(7, "LRCX", now=t + 20.5) is None       # the 20 s passed
    # per minute: member 9 gets 30, the 31st is refused, a minute later it is fine again
    for i in range(30):
        assert opt_store.check_rate(9, "S%02d" % i, now=t + i) is None
    msg = opt_store.check_rate(9, "S99", now=t + 31)
    assert msg and "30" in msg
    assert opt_store.check_rate(9, "S99", now=t + 61) is None
    # datetimes work as the clock too
    assert opt_store.check_rate(10, "LRCX", now=T0) is None
    assert opt_store.check_rate(10, "LRCX", now=T0 + _dt.timedelta(seconds=10))
    assert opt_store.check_rate(10, "LRCX", now=T0 + _dt.timedelta(seconds=21)) is None


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
    opt_store.upsert_quotes(db, "LRCX", rows, source="hermes", mdt="delayed", as_of=T0)
    opt_store.upsert_quotes(db, "LRCX", [_row(E1, "C", 100.0, bid=3.0, ask=3.4)], source="member",
                            mdt="live", user_id=user.id, now=T0 + _dt.timedelta(minutes=1))
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
    assert (c["source"], c["source_user_id"], c["source_name"], c["mdt"]) == \
        ("member", user.id, "Member", "live")
    assert c["as_of"] == T0 + _dt.timedelta(minutes=1)
    p = view["expiries"][0]["puts"][0]
    assert (p["source"], p["source_user_id"], p["source_name"], p["mdt"]) == \
        ("hermes", None, None, "delayed")
    assert p["iv"] == 0.40 and p["delta"] < 0                   # FRACTION, signed
    # no opt_underlying row: the spot falls back to the newest quote's und_price
    assert view["spot"] == 100.0 and view["spot_source"] == "member"
    opt_store.set_spot(db, "LRCX", 104.2, source="hermes", mdt="live", as_of=T0)
    view = opt_store.chain_view(db, "LRCX", today=TODAY)
    assert (view["spot"], view["spot_as_of"], view["spot_source"], view["spot_mdt"]) == \
        (104.2, T0, "hermes", "live")
    empty = opt_store.chain_view(db, "NONE", today=TODAY)
    assert empty["expiries"] == [] and empty["spot"] is None


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
    n = opt_store.upsert_daily(db, "LRCX", bars=bars, iv_series=ivs, source="hermes",
                               today=TODAY, now=T0)
    assert n == 300
    assert db.query(models.OptUnderlyingDaily).count() == 300
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
                                  source="hermes", today=TODAY, now=T0) == 1
    assert opt_store.upsert_daily(db, "LRCX", bars=[{"on": "2026-10-01", "open": 1, "high": 105,
                                                     "low": 99, "close": 101, "volume": 1e6}],
                                  source="member", today=TODAY, now=T0) == 1
    r = db.query(models.OptUnderlyingDaily).filter_by(symbol="LRCX", on="2026-10-01").one()
    assert (r.iv30, r.close, r.high, r.low, r.volume, r.source) == (41.0, 101, 105, 99, 1e6, "member")
    # a later value for the same day replaces the earlier one
    opt_store.upsert_daily(db, "LRCX", bars=[{"on": "2026-10-01", "high": 106, "low": 99,
                                              "close": 102, "volume": 2e6}],
                           iv_series=[("2026-10-01", 42.5)], source="hermes", today=TODAY, now=T0)
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
        source="hermes", today=TODAY, now=T0)
    assert n == 0
    assert db.query(models.OptUnderlyingDaily).count() == 1
    # too little history: the stats are None, not invented
    und = opt_store.recompute_underlying(db, "LRCX", now=T0)
    assert und["atr14"] is None and und["hv20"] is None and und["avg_vol20"] is None
    assert und["iv30"] == 42.5 and und["iv_rank"] is None and und["iv_n"] == 1


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
    t1, t2, t3 = T0, T0 + _dt.timedelta(minutes=10), T0 + _dt.timedelta(minutes=20)
    opt_store.upsert_quotes(db, "AAA", [_row()], source="hermes", mdt="delayed", as_of=t1)
    opt_store.upsert_quotes(db, "AAA", [_row(strike=105.0)], source="member", mdt="live",
                            user_id=user.id, kind="member", now=t2)
    # a later fetch that wrote nothing (an error) does not make the symbol look fresh
    opt_store.upsert_quotes(db, "AAA", [], source="hermes", mdt="delayed", as_of=t3, error="timeout")
    opt_store.upsert_quotes(db, "BBB", [_row(), _row(strike=95.0)], source="hermes", mdt="frozen",
                            as_of=t1, kind="eod")
    fr = opt_store.freshness(db, ["aaa", "BBB", "CCC"], now=t3)
    assert set(fr) == {"AAA", "BBB"}
    assert fr["AAA"] == {"as_of": t2, "source": "member", "source_user_id": user.id,
                         "source_name": "Member", "mdt": "live", "n": 1, "kind": "member",
                         "age_min": 10.0}
    assert fr["BBB"]["source_name"] is None and fr["BBB"]["n"] == 2 and fr["BBB"]["age_min"] == 20.0
    assert fr["BBB"]["kind"] == "eod"
    assert opt_store.freshness(db, []) == {}


def test_universe_counts_members_per_active_symbol(db, user):
    u2 = _user(db, "b@local.test", "Bee")
    u3 = _user(db, "c@local.test", "Cee")
    _basket(db, user, "MSFT", "LRCX", "AAPL")
    _basket(db, u2, "LRCX", "AAPL")
    _basket(db, u3, "LRCX")
    _basket(db, u3, "ZZZ", active=False)
    _basket(db, None, "SPY", owner="system")
    assert opt_store.universe(db) == [("LRCX", 3), ("AAPL", 2), ("MSFT", 1), ("SPY", 0)]


# ───────────────────────────────── next_for_member + leases ─────────────────────────────────

def test_next_for_member_stalest_first_with_leases(db, user):
    _basket(db, user, "BBB", "AAA", "CCC", "DDD")
    opt_store.upsert_quotes(db, "BBB", [_row()], source="hermes", mdt="delayed",
                            as_of=T0 - _dt.timedelta(minutes=30))
    opt_store.upsert_quotes(db, "CCC", [_row()], source="hermes", mdt="delayed",
                            as_of=T0 - _dt.timedelta(minutes=10))
    opt_store.upsert_quotes(db, "DDD", [_row()], source="hermes", mdt="delayed",
                            as_of=T0 - _dt.timedelta(seconds=20))       # too fresh to ask for
    opt_store.set_spot(db, "BBB", 349.2, source="hermes", mdt="live", as_of=T0)
    opt_store.upsert_daily(db, "BBB", iv_series=[{"on": "2026-10-07", "iv": 46.0}], source="hermes",
                           today=TODAY, now=T0)
    opt_store.recompute_underlying(db, "BBB", now=T0)

    got = [opt_store.next_for_member(db, user, now=T0) for _ in range(4)]
    assert [g["symbol"] if g else None for g in got] == ["AAA", "BBB", "CCC", None]
    # never quoted: no expiry list, th_ibkr.plan takes the nearest 6; 25 strikes a side
    assert got[0]["spec"] == {"symbol": "AAA", "spot": None, "iv_hint": None, "expiries": None,
                              "max_expiries": 6, "max_weekly_dte": 63, "max_dte": 1100,
                              "sigma_k": 2.5, "min_side": 6, "max_side": 25}
    assert got[0]["history_done"] is False
    assert got[1]["spec"]["spot"] == 349.2 and got[1]["spec"]["iv_hint"] == pytest.approx(0.46)
    assert got[1]["spec"]["expiries"] == [E2] and "max_expiries" not in got[1]["spec"]
    assert opt_store.is_leased("BBB", now=T0)

    # only the holder (or nobody in particular) releases a lease
    opt_store.release_lease("BBB", user_id=user.id + 999)
    assert opt_store.is_leased("BBB", now=T0)
    opt_store.release_lease("BBB", user_id=user.id)
    assert not opt_store.is_leased("BBB", now=T0)
    assert opt_store.next_for_member(db, user, now=T0)["symbol"] == "BBB"
    opt_store.release_lease("BBB")
    assert not opt_store.is_leased("BBB", now=T0)
    assert opt_store.next_for_member(db, user, now=T0)["symbol"] == "BBB"

    # another member's lease blocks the symbol for everyone
    u2 = _user(db, "b@local.test", "Bee")
    _basket(db, u2, "AAA", "EEE")
    assert opt_store.next_for_member(db, u2, now=T0)["symbol"] == "EEE"
    assert opt_store.next_for_member(db, u2, now=T0) is None

    # leases last 240 s - longer than a chunked read (the connector's 150 s)
    assert opt_store.next_for_member(db, u2, now=T0 + _dt.timedelta(seconds=239)) is None
    later = T0 + _dt.timedelta(seconds=241)
    assert opt_store.next_for_member(db, user, now=later)["symbol"] == "AAA"

    # a symbol refreshed in the last minute is not asked for, unless the floor is off
    u3 = _user(db, "c@local.test", "Cee")
    _basket(db, u3, "DDD")
    assert opt_store.next_for_member(db, u3, now=T0) is None
    assert opt_store.next_for_member(db, u3, now=T0, min_age_s=0)["symbol"] == "DDD"


def test_next_for_member_empty_basket_or_no_user(db, user):
    assert opt_store.next_for_member(db, user, now=T0) is None
    assert opt_store.next_for_member(db, None, now=T0) is None
    _basket(db, user, "AAA", active=False)
    assert opt_store.next_for_member(db, user, now=T0) is None


# ───────────────────────────────── collector status ─────────────────────────────────

def test_collector_status_get_and_set(db):
    assert opt_store.collector_status(db) is None
    aware = _dt.datetime(2026, 10, 8, 23, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    opt_store.set_collector_status(db, state="cycle", gateway="127.0.0.1:4002", gateway_ok=True,
                                   mdt="live", cycle_n=3, cycle_started=aware, symbols_total=30,
                                   symbols_done=18, pid=4242, version="2.0", bogus=1, now=T0)
    st = opt_store.collector_status(db)
    assert st["id"] == 1 and st["state"] == "cycle" and st["gateway_ok"] is True
    assert st["cycle_started"] == _dt.datetime(2026, 10, 8, 15, 0)      # stored naive UTC
    assert st["heartbeat"] == T0 and st["symbols_done"] == 18 and st["version"] == "2.0"
    assert "bogus" not in st
    later = T0 + _dt.timedelta(seconds=15)
    opt_store.set_collector_status(db, state="error", last_error="x" * 50, gateway_ok=False,
                                   phase_detail="gateway down", state_extra=None, now=later)
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
    rows += [_row(E2, "P", 100.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, source="hermes", mdt="frozen", as_of=T0)
    assert opt_store.snapshot_eod(db, "LRCX", TODAY) == 5
    S = models.OptionChainSnapshot
    snap = db.query(S).filter_by(symbol="LRCX", snap_on=TODAY).order_by(S.expiry, S.right, S.strike).all()
    assert len(snap) == 5
    assert {(s.kind, s.source) for s in snap} == {("eod", "ibkr")}
    assert {s.expiry: s.dte for s in snap} == {E1: 8, E2: 43}
    q = _quote(db, "LRCX", E1, "C", 95.0)
    s = next(x for x in snap if (x.expiry, x.right, x.strike) == (E1, "C", 95.0))
    assert (s.bid, s.ask, s.mid, s.iv, s.delta, s.oi, s.volume) == \
        (q.bid, q.ask, q.mid, q.iv, q.delta, q.oi, q.volume)
    assert s.iv == 0.40                                        # still a FRACTION
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
        db.add(L(symbol="LRCX", as_of=T0 - _dt.timedelta(days=days), source="hermes", mdt="live",
                 kind="cycle", n_contracts=1, n_expiries=1))
    db.commit()
    rows = [_row("2026-09-30", "P", 100.0, iv=0.4),            # expired 8 days ago: pruned
            _row("2026-10-01", "P", 100.0),                    # expired 7 days ago: kept (grace)
            _row(E1, "P", 100.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, source="hermes", mdt="live", as_of=T0)
    # quotes nobody refreshed for 7 days (an unlisted / invented key no feed re-reads): pruned
    opt_store.upsert_quotes(db, "LRCX", [_row(E3, "P", 95.0)], source="hermes", mdt="live",
                            as_of=T0 - _dt.timedelta(days=7, minutes=1))
    opt_store.upsert_quotes(db, "LRCX", [_row(E3, "P", 90.0)], source="hermes", mdt="live",
                            as_of=T0 - _dt.timedelta(days=6, hours=23))

    calls = []
    monkeypatch.setattr(option_store, "prune", lambda d, today=None: calls.append(today) or {"snapshots_old": 0})
    out = opt_store.prune_v2(db, TODAY, now=T0)
    assert out["refresh_log"] == 1 and out["quotes_expired"] == 1 and out["quotes_stale"] == 1
    assert out["snapshots"] == {"snapshots_old": 0} and calls == [TODAY]
    assert db.query(L).count() == 5                         # 29 d, 1 d and upsert_quotes' three rows
    assert sorted((q.expiry, q.strike) for q in db.query(models.OptQuote)) == \
        [("2026-10-01", 100.0), (E1, 100.0), (E3, 90.0)]

    monkeypatch.delattr(option_store, "prune")
    out = opt_store.prune_v2(db, TODAY, now=T0)
    assert out["snapshots"] is None and out["refresh_log"] == 0 and out["quotes_expired"] == 0
    assert out["quotes_stale"] == 0


# ─────────────────────── the v4.133 review: the spot band (finding 1) ───────────────────────

def _contrib(db, user, spot, *, now=T0, mdt="live", sym="LRCX", rows=None):
    """Validate + store a member chain read the way the route does; the error or None."""
    rows = rows if rows is not None else [_row(strike=k) for k in (90.0, 100.0, 110.0)]
    clean, err, _dropped = opt_store.validate_contribution(
        db, _payload(symbol=sym, spot=spot, mdt=mdt, rows=rows), user_id=user.id, now=now)
    if clean is None:
        return err
    opt_store.upsert_quotes(db, clean["symbol"], clean["rows"], source="member", mdt=clean["mdt"],
                            user_id=user.id, spot=clean["spot"], kind="member", now=now)
    return None


def test_spot_band_is_anchored_to_hermes_and_cannot_be_ratcheted(db, user):
    _basket(db, user, "LRCX")
    # Hermes's EOD read on Wed 10-07: its spot and its quotes (und_price 100) - Thursday's band
    # is 15% (iv30 / hv20 unknown -> 40% a year -> 4 x 2.5% x sqrt(2) = 14.3% -> the 15% floor)
    opt_store.upsert_quotes(db, "LRCX", [_row(E3, "P", 95.0)], source="hermes", mdt="live",
                            as_of=T0 - _dt.timedelta(days=1), spot=100.0)
    assert opt_store.spot_reference(db, "LRCX")["from"] == "spot"
    assert _contrib(db, user, 114.0) is None
    assert opt_store.underlying(db, "LRCX")["spot_source"] == "member"     # the member's price is stored ...
    ref = opt_store.spot_reference(db, "LRCX")                             # ... but is never the reference
    assert (ref["spot"], ref["on"], ref["from"]) == (100.0, "2026-10-07", "quote")
    # the ratchet of the review (x1.19 every post) stops at the first step past the band
    err = _contrib(db, user, 114.0 * 1.14, now=T0 + _dt.timedelta(seconds=30))
    assert err and "IBKR" in err and "15%" in err
    # an honest member posting the real price is never locked out by an earlier member post
    assert _contrib(db, user, 100.0, now=T0 + _dt.timedelta(seconds=60)) is None

    # the Hermes daily close is the reference when no Hermes spot / quote is newer
    _basket(db, user, "MSFT")
    opt_store.upsert_daily(db, "MSFT", bars=[{"on": "2026-10-07", "high": 101, "low": 99, "close": 100.0}],
                           source="hermes", today=TODAY, now=T0)
    opt_store.upsert_daily(db, "MSFT", bars=[{"on": "2026-10-08", "high": 201, "low": 199, "close": 200.0}],
                           source="member", today=TODAY, now=T0)       # a member's close is no reference
    ref = opt_store.spot_reference(db, "MSFT")
    assert (ref["spot"], ref["on"], ref["from"]) == (100.0, "2026-10-07", "daily")
    assert "IBKR" in (_contrib(db, user, 200.0, sym="MSFT") or "")

    # with no Hermes data at all the read is accepted (a first read) and a member's spot never
    # becomes the reference: a later wild price is accepted too (there is nothing IBKR to hold it to)
    _basket(db, user, "NEWT")
    assert _contrib(db, user, 100.0, sym="NEWT") is None
    assert opt_store.spot_reference(db, "NEWT") is None
    assert _contrib(db, user, 160.0, sym="NEWT", now=T0 + _dt.timedelta(seconds=30)) is None


def test_spot_band_width_is_ticker_relative_and_grows_with_time(db, user):
    _basket(db, user, "LRCX", "MSFT")
    far_otm = [_row(strike=k, bid=0.1, ask=0.2) for k in (40.0, 50.0)]     # valid at any spot below
    opt_store.set_spot(db, "LRCX", 100.0, source="hermes", mdt="live", as_of=T0 - _dt.timedelta(days=1))
    # the earnings gap of the review: -25% overnight. A 40%-IV stock: refused (15% band) ...
    assert "15%" in (_contrib(db, user, 75.0, rows=far_otm) or "")
    u = db.query(models.OptUnderlying).filter_by(symbol="LRCX").one()
    # hv20 stands in for an unknown iv30; the 15% floor holds for a quiet stock
    u.hv20 = 60.0
    db.commit()
    assert opt_store.spot_reference(db, "LRCX")["sigma_daily"] == pytest.approx(0.60 / math.sqrt(252))
    u.hv20 = 10.0
    db.commit()
    assert opt_store.spot_band(opt_store.spot_reference(db, "LRCX"), 1) == 0.15
    # ... the same gap on a stock whose IV30 is 80%: 4 x 0.80 / sqrt(252) x sqrt(2) = 28.5% -> accepted
    u.iv30 = 80.0
    db.commit()
    ref = opt_store.spot_reference(db, "LRCX")
    assert ref["sigma_daily"] == pytest.approx(0.80 / math.sqrt(252))
    assert opt_store.spot_band(ref, 1) == pytest.approx(4 * 0.80 / math.sqrt(252) * math.sqrt(2))
    assert _contrib(db, user, 75.0, rows=far_otm) is None

    # the band widens with the trading days since the reference: a Hermes close of Thu 09-24 is
    # 10 trading days before Thu 10-08 -> 4 x 0.40 / sqrt(252) x sqrt(11) = 33.4%
    opt_store.upsert_daily(db, "MSFT", bars=[{"on": "2026-09-24", "high": 101, "low": 99, "close": 100.0}],
                           source="hermes", today=TODAY, now=T0)
    assert opt_store.spot_band(opt_store.spot_reference(db, "MSFT"), 10) == \
        pytest.approx(4 * 0.40 / math.sqrt(252) * math.sqrt(11))
    assert _contrib(db, user, 133.0, sym="MSFT") is None
    assert "IBKR" in (_contrib(db, user, 135.0, sym="MSFT", now=T0 + _dt.timedelta(seconds=30)) or "")


# ─────────────── the review: delayed member data (findings 2 and 21), the posted mid (3) ───────────────

def test_member_delayed_data_is_back_dated_and_never_beats_a_later_live_quote(db, user):
    u2 = _user(db, "b@local.test", "Bee")
    t = T0
    opt_store.upsert_quotes(db, "NVDA", [_row(bid=2.0, ask=2.2)], source="member", mdt="live",
                            user_id=user.id, kind="member", spot=100.0, now=t)
    # member B (no OPRA -> IBKR delayed) posts 61 s later: data of ~14 min BEFORE A's live read
    later = t + _dt.timedelta(seconds=61)
    res = opt_store.upsert_quotes(db, "NVDA", [_row(bid=1.5, ask=1.7), _row(strike=105.0)],
                                  source="member", mdt="delayed", user_id=u2.id, kind="member",
                                  spot=99.0, now=later)
    assert res["stored"] == 1 and res["skipped_older"] == 1
    q = _quote(db, "NVDA", E2, "P", 100.0)
    assert (q.bid, q.mdt, q.source_user_id, q.as_of) == (2.0, "live", user.id, t)
    q2 = _quote(db, "NVDA", E2, "P", 105.0)
    assert (q2.mdt, q2.source_user_id, q2.as_of) == ("delayed", u2.id, later - _dt.timedelta(minutes=15))
    und = opt_store.underlying(db, "NVDA")
    assert (und["spot"], und["spot_mdt"]) == (100.0, "live")          # the delayed spot is older too
    lg = db.query(models.OptRefreshLog).order_by(models.OptRefreshLog.id.desc()).first()
    assert (lg.mdt, lg.as_of) == ("delayed", later - _dt.timedelta(minutes=15))
    assert opt_store.freshness(db, ["NVDA"], now=later)["NVDA"]["mdt"] == "live"
    # set_spot follows the same rule (IBKR code 4 = delayed_frozen)
    opt_store.set_spot(db, "NVDA", 98.0, source="member", mdt=4, user_id=u2.id,
                       now=t + _dt.timedelta(minutes=10))
    assert opt_store.underlying(db, "NVDA")["spot"] == 100.0
    # 16 min after the live read a delayed read shows a later moment, so it does replace it -
    # and its age reads the 15 min it really is
    much_later = t + _dt.timedelta(minutes=16)
    res = opt_store.upsert_quotes(db, "NVDA", [_row(bid=1.6, ask=1.8)], source="member",
                                  mdt="delayed", user_id=u2.id, kind="member", spot=99.0, now=much_later)
    assert res["stored"] == 1
    db.expire_all()
    assert _quote(db, "NVDA", E2, "P", 100.0).as_of == t + _dt.timedelta(minutes=1)
    fr = opt_store.freshness(db, ["NVDA"], now=much_later)["NVDA"]
    assert (fr["mdt"], fr["age_min"]) == ("delayed", 15.0)
    # Hermes data is never back-dated (its as_of is the collector's own stamp)
    opt_store.upsert_quotes(db, "NVDA", [_row(strike=110.0)], source="hermes", mdt="delayed",
                            as_of=much_later, now=much_later)
    assert _quote(db, "NVDA", E2, "P", 110.0).as_of == much_later


def test_a_posted_mid_is_never_stored(db, user):
    opt_store.upsert_quotes(db, "LRCX", [_row(bid=1.0, ask=1.1, mid=500.0), _row(strike=105.0, bid=None, mid=3.0)],
                            source="hermes", mdt="live", as_of=T0)
    assert _quote(db, "LRCX", E2, "P", 100.0).mid == pytest.approx(1.05)
    assert _quote(db, "LRCX", E2, "P", 105.0).mid is None            # one side only: no mid
    _basket(db, user, "LRCX")
    clean, err, _ = opt_store.validate_contribution(
        db, _payload(rows=[_row(bid=1.0, ask=1.1, mid=500.0)]), user_id=user.id, today=TODAY, now=T0)
    assert err is None and clean["rows"][0]["mid"] == pytest.approx(1.05)
    # a row stored before the rule (a forged mid in the table) is shown at (bid + ask) / 2
    q = _quote(db, "LRCX", E2, "P", 100.0)
    q.mid = 500.0
    db.commit()
    put = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)["expiries"][0]["puts"][0]
    assert put["mid"] == pytest.approx(1.05)


# ─────────────── the review: work sharing - chunks, leases, back-off (finding 17) ───────────────

def test_next_for_member_chunks_a_read_to_the_six_stalest_expiries(db, user):
    _basket(db, user, "LRCX")
    exps = [(_dt.date(2026, 10, 9) + _dt.timedelta(weeks=w)).isoformat() for w in range(8)]
    minutes_ago = [60, 30, 80, 10, 40, 70, 20, 50]
    for e, m in zip(exps, minutes_ago):
        opt_store.upsert_quotes(db, "LRCX", [_row(e, "P", 100.0), _row(e, "C", 100.0)], source="hermes",
                                mdt="live", as_of=T0 - _dt.timedelta(minutes=m))
    # one old row makes its expiry the stalest (its OLDEST row counts)
    opt_store.upsert_quotes(db, "LRCX", [_row(exps[3], "P", 95.0)], source="hermes", mdt="live",
                            as_of=T0 - _dt.timedelta(hours=5))
    # an expired expiry is never asked for
    opt_store.upsert_quotes(db, "LRCX", [_row("2026-10-02", "P", 100.0)], source="hermes", mdt="live",
                            as_of=T0 - _dt.timedelta(days=2))
    opt_store.mark_history_done(db, "LRCX")
    got = opt_store.next_for_member(db, user, now=T0)
    assert got["symbol"] == "LRCX" and got["history_done"] is True
    assert got["spec"]["expiries"] == [exps[3], exps[2], exps[5], exps[0], exps[7], exps[4]]
    assert got["spec"]["max_side"] == 25 and "max_expiries" not in got["spec"]


def test_next_for_member_min_age_counts_a_delayed_read_from_its_receive_time(db, user):
    _basket(db, user, "LRCX")
    opt_store.upsert_quotes(db, "LRCX", [_row()], source="member", mdt="delayed", user_id=user.id,
                            kind="member", now=T0)
    # the read is filed 15 min back, but it ARRIVED 30 s ago: not asked for again yet
    assert opt_store.next_for_member(db, user, now=T0 + _dt.timedelta(seconds=30)) is None
    assert opt_store.next_for_member(db, user, now=T0 + _dt.timedelta(seconds=61))["symbol"] == "LRCX"


def test_report_failure_backs_a_symbol_off_and_a_contribution_clears_it(db, user):
    u2 = _user(db, "b@local.test", "Bee")
    _basket(db, user, "AAA", "BBB")
    _basket(db, u2, "AAA")
    assert opt_store.next_for_member(db, user, now=T0)["symbol"] == "AAA"
    assert opt_store.report_failure("AAA", user.id, "chain2 timed out", now=T0, db=db) == 600
    assert not opt_store.is_leased("AAA", now=T0)                    # the reporter's lease is released
    assert opt_store.backoff_until("AAA", now=T0) == pytest.approx(opt_store._epoch(T0) + 600)
    # backed off for every member's loop
    assert opt_store.next_for_member(db, user, now=T0)["symbol"] == "BBB"
    assert opt_store.next_for_member(db, u2, now=T0) is None
    # another member's report while backed off neither doubles it nor frees this member's lease
    assert opt_store.report_failure("BBB", u2.id, "x", now=T0, db=db) == 600
    assert opt_store.is_leased("BBB", now=T0)
    assert opt_store.report_failure("AAA", u2.id, "again", now=T0 + _dt.timedelta(seconds=100), db=db) == 500
    # consecutive failures double: 10 min, 20, 40, 80, then the 2 h cap
    t = T0 + _dt.timedelta(seconds=601)
    waits = []
    for _ in range(5):
        assert opt_store.next_for_member(db, u2, now=t)["symbol"] == "AAA"
        w = opt_store.report_failure("AAA", u2.id, "no data", now=t, db=db)
        waits.append(w)
        t += _dt.timedelta(seconds=w + 1)
    assert waits == [1200, 2400, 4800, 7200, 7200]
    # failures are logged (kind member, nothing written) and never make the symbol look read
    logs = db.query(models.OptRefreshLog).filter_by(symbol="AAA").order_by(models.OptRefreshLog.id).all()
    assert len(logs) == 7 and {(lg.kind, lg.n_contracts, lg.source) for lg in logs} == {("member", 0, "member")}
    assert logs[0].error == "chain2 timed out" and logs[0].source_user_id == user.id
    assert "AAA" not in opt_store.freshness(db, ["AAA"], now=t)
    # the next member write with usable rows clears the back-off (a trade refresh counts;
    # it does not make the chain look fresh, so the loop asks for AAA at once)
    opt_store.report_failure("AAA", u2.id, "no data", now=t, db=db)
    assert opt_store.backoff_until("AAA", now=t) is not None
    opt_store.upsert_quotes(db, "AAA", [_row()], source="member", mdt="live", user_id=user.id,
                            kind="trade", now=t)
    assert opt_store.backoff_until("AAA", now=t) is None
    assert opt_store.next_for_member(db, u2, now=t)["symbol"] == "AAA"
    # ... and the doubling starts again from 10 min
    assert opt_store.report_failure("AAA", u2.id, "no data", now=t, db=db) == 600
    # without a session the log row is best effort: never raises, the back-off stands
    assert opt_store.report_failure("ZZZ", u2.id, "boom", now=t) == 600
    assert opt_store.report_failure("", u2.id, "boom", now=t) == 0


# ─────────────── the review: freshness (lows) ───────────────

def test_freshness_ignores_trade_refreshes_and_failure_rows(db, user):
    opt_store.upsert_quotes(db, "AAA", [_row()], source="hermes", mdt="live", kind="cycle",
                            as_of=T0 - _dt.timedelta(hours=2))
    opt_store.upsert_quotes(db, "AAA", [_row(strike=105.0)], source="member", mdt="live",
                            user_id=user.id, kind="trade", now=T0)
    opt_store.report_failure("AAA", user.id, "timeout", now=T0, db=db)
    fr = opt_store.freshness(db, ["AAA"], now=T0)["AAA"]
    assert (fr["as_of"], fr["kind"], fr["source"]) == (T0 - _dt.timedelta(hours=2), "cycle", "hermes")
    # a trade refresh alone never makes a symbol look read
    opt_store.upsert_quotes(db, "BBB", [_row()], source="member", mdt="live", user_id=user.id,
                            kind="trade", now=T0)
    assert "BBB" not in opt_store.freshness(db, ["BBB"], now=T0)


def _log_rows(sym, n, newest_min, **kw):
    row = dict(symbol=sym, source="hermes", mdt="live", kind="cycle", n_contracts=10, n_expiries=1)
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
    # good reads, then 50 newer rows that do not count (failures and trade refreshes)
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


# ─────────────── the review: chain_view pre-filters, Core select, cache ───────────────

def test_chain_view_filters_cache_and_copies(db, engine, user):
    rows = [_row(e, r, k) for e in (E1, E2, E3) for r in ("C", "P") for k in (95.0, 100.0)]
    opt_store.upsert_quotes(db, "LRCX", rows, source="hermes", mdt="live",
                            as_of=T0 - _dt.timedelta(hours=30))           # Wed 09:00 UTC
    opt_store.upsert_quotes(db, "LRCX", [_row(E2, "P", 100.0, bid=4.0, ask=4.4)], source="member",
                            mdt="live", user_id=user.id, now=T0)

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
    assert v["expiries"][0]["puts"][0]["source_name"] == "Member"
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
    opt_store.upsert_quotes(db, "LRCX", [_row(E1, "P", 95.0, bid=0.5, ask=0.7)], source="member",
                            mdt="live", user_id=user.id, now=T0 + _dt.timedelta(seconds=5))
    v4 = opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)
    assert v4["expiries"][0]["puts"][0]["bid"] == 0.5
    # the spot is read fresh every call (a set_spot writes no refresh-log row)
    opt_store.set_spot(db, "LRCX", 101.0, source="hermes", mdt="live", as_of=T0 + _dt.timedelta(seconds=9))
    assert opt_store.chain_view(db, "LRCX", today=TODAY, now=T0)["spot"] == 101.0
    # another day is another key (DTE and the expired cut move with it)
    assert [e["dte"] for e in opt_store.chain_view(db, "LRCX", today="2026-10-09", now=T0)["expiries"]] == \
        [7, 42, 98]


# ─────────────── the review: unlisted / far contracts (finding 4) are refused at the door ───────────────

def test_fabricated_contract_keys_are_dropped(db, user):
    _basket(db, user, "NVDA")
    junk = [_row(E2, "P", 100.0 + 0.0001 * i) for i in range(1, 50)]     # the review's strike walk
    junk += [_row("2099-06-19", "P", 100.0), _row("2026-11-21", "C", 100.0)]
    good = [_row(E2, "P", 100.0)]
    clean, err, dropped = opt_store.validate_contribution(
        db, _payload(symbol="NVDA", rows=junk + good), user_id=user.id, today=TODAY, now=T0)
    assert err is None and dropped == len(junk) and len(clean["rows"]) == 1
    assert opt_store.drop_reasons(junk, 100.0, today=TODAY) == {"strike": 49, "expiry": 2}


# ─────────────── validate_history + Hermes replaces member history ───────────────

def _weekday_bars(end: str, n: int, close: float = 100.0) -> list[dict]:
    out, d, i = [], _dt.date.fromisoformat(end), 0
    while len(out) < n:
        if d.weekday() < 5:
            c = round(close * (1 + 0.002 * math.sin(i)), 2)
            out.append({"on": d.isoformat(), "open": c, "high": c + 1, "low": c - 1, "close": c,
                        "volume": 1e6 + i})
            i += 1
        d -= _dt.timedelta(days=1)
    return out[::-1]


def test_validate_history(db, user):
    _basket(db, user, "LRCX")
    bars = _weekday_bars("2026-10-07", 30)
    ivs = [{"on": b["on"], "iv": 35.0} for b in bars] + [{"on": bars[0]["on"], "iv": 0.05},
                                                         ("2026-10-06", 2000.0)]
    clean, err = opt_store.validate_history(db, {"symbol": "lrcx", "bars": bars, "iv_series": ivs},
                                            user_id=user.id, now=T0)
    assert err is None and clean["symbol"] == "LRCX"
    assert len(clean["bars"]) == 30 and len(clean["iv_series"]) == 30   # the two IV points out of range left out
    assert set(clean["bars"][0]) == {"on", "open", "high", "low", "close", "volume"}
    clean2, err = opt_store.validate_history(db, {"symbol": "LRCX", "bars": bars}, user_id=user.id, now=T0)
    assert err is None and clean2["iv_series"] == []                     # iv_series is optional
    assert opt_store.upsert_daily(db, clean["symbol"], clean["bars"], clean["iv_series"], source="member",
                                  today=TODAY, now=T0) == 30

    def why(payload):
        c, e = opt_store.validate_history(db, payload, user_id=user.id, now=T0)
        assert c is None and e
        return e

    def with_bar(**kw):
        b = dict(bars[-1])
        b.update(kw)
        return {"symbol": "LRCX", "bars": bars[:-1] + [b]}

    assert "object" in why(["x"])
    assert "symbol" in why({"symbol": "", "bars": bars})
    assert "basket" in why({"symbol": "MSFT", "bars": bars})
    assert "bars" in why({"symbol": "LRCX"})
    assert "bars" in why({"symbol": "LRCX", "bars": []})
    assert "list" in why({"symbol": "LRCX", "bars": bars, "iv_series": {"a": 1}})
    assert "at most" in why({"symbol": "LRCX", "bars": bars * 27})
    assert "weekend" in why(with_bar(on="2026-10-03"))
    assert "after today" in why(with_bar(on="2026-10-09"))
    assert "date" in why(with_bar(on="someday"))
    assert "close" in why(with_bar(close=0))
    assert "high below low" in why(with_bar(high=90.0, low=95.0))
    assert "number" in why(with_bar(volume="lots"))
    assert "weekend" in why({"symbol": "LRCX", "bars": bars, "iv_series": [{"on": "2026-10-04", "iv": 30}]})
    assert why(None) and opt_store.validate_history(None, {"symbol": "LRCX", "bars": bars},
                                                    user_id=user.id)[1]   # never raises

    # once Hermes has a price, every close must sit in the band around it
    opt_store.set_spot(db, "LRCX", 100.0, source="hermes", mdt="live", as_of=T0 - _dt.timedelta(days=1))
    assert opt_store.validate_history(db, {"symbol": "LRCX", "bars": bars}, user_id=user.id, now=T0)[1] is None
    assert "IBKR" in why(with_bar(close=130.0, high=131.0, low=129.0))
    # the band widens with the trading days between the bar and the reference: a year back,
    # 4 x 2.5% x sqrt(251) = 160% - a doubled close then is history, not a forgery
    old = {"on": "2025-10-08", "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.0, "volume": 1e6}
    assert opt_store.validate_history(db, {"symbol": "LRCX", "bars": [old] + bars}, user_id=user.id,
                                      now=T0)[1] is None


def test_hermes_history_replaces_member_rows_in_its_span(db):
    D = models.OptUnderlyingDaily
    member_bars = [{"on": d, "high": 101, "low": 99, "close": 100.0, "volume": 1e6}
                   for d in ("2026-09-15", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-05")]
    member_ivs = [{"on": d, "iv": 50.0} for d in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-05",
                                                  "2026-10-06")]
    assert opt_store.upsert_daily(db, "LRCX", member_bars, member_ivs, source="member",
                                  today=TODAY, now=T0) == 6
    hermes_bars = [{"on": d, "high": 111, "low": 109, "close": 110.0, "volume": 2e6}
                   for d in ("2026-10-01", "2026-10-02", "2026-10-05")]
    hermes_ivs = [{"on": "2026-10-01", "iv": 40.0}, {"on": "2026-10-05", "iv": 41.0}]
    assert opt_store.upsert_daily(db, "LRCX", hermes_bars, hermes_ivs, source="hermes",
                                  today=TODAY, now=T0) == 3
    db.expire_all()
    got = {r.on: (r.source, r.close, r.iv30) for r in db.query(D).filter_by(symbol="LRCX")}
    assert got == {
        "2026-09-15": ("member", 100.0, None),     # before Hermes's span: untouched
        "2026-10-01": ("hermes", 110.0, 40.0),
        "2026-10-02": ("hermes", 110.0, None),     # inside the IV span with no Hermes IV: the member's cleared
        "2026-10-05": ("hermes", 110.0, 41.0),
        "2026-10-06": ("member", None, 50.0),      # after both spans: untouched
    }                                              # 2026-10-03 (a member's invented Saturday): gone
    # a member post never touches Hermes rows' provenance rule in reverse (member rows only)
    opt_store.upsert_daily(db, "LRCX", [{"on": "2026-09-30", "close": 90.0}], source="member",
                           today=TODAY, now=T0)
    opt_store.upsert_daily(db, "LRCX", hermes_bars, None, source="hermes", today=TODAY, now=T0)
    assert db.query(D).filter_by(symbol="LRCX", on="2026-09-30").one().source == "member"
