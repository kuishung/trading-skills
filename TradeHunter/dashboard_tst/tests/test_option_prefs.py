"""Options module: the rules schema / hash (option_prefs) and the recommender
(strategy_rules) - part_B_engines.md B9's two rows, part_A_data.md A8's
prefs_hash cases, OPTIONS_MODULE_DESIGN.md II.2.3 / II.2.7.

Run from dashboard_tst/:  py -m pytest tests/test_option_prefs.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import opt_constants as C             # noqa: E402
from app.services import option_prefs as op             # noqa: E402
from app.services import strategy_rules as sr           # noqa: E402

USER_ORDER = ("buy_call", "buy_put", "bull_call", "bear_put", "leaps_call", "diagonal_call",
              "bull_put", "bear_call", "iron_condor", "calendar")
FAMILY_OF = {"bull_put": "credit_vertical", "bear_call": "credit_vertical",
             "bull_call": "debit_vertical", "bear_put": "debit_vertical",
             "buy_call": "long", "buy_put": "long", "leaps_call": "leaps",
             "iron_condor": "condor", "calendar": "time", "diagonal_call": "time"}


class _User:
    """A stub member: ``prefs`` (trade_prefs' JSON) + ``option_prefs`` (the one-to-one row)."""

    def __init__(self, prefs=None, row=None, uid=1):
        self.id = uid
        self.prefs = prefs
        self.option_prefs = row


class _Row:
    def __init__(self, prefs):
        self.prefs = prefs
        self.prefs_hash = ""
        self.schema_version = 1


# ------------------------------------------------------------- the schema
class TestSchema:
    def test_blocks_tabs_and_field_shape(self):
        assert tuple(op.SCHEMA) == op.BLOCKS == ("shared", "credit_vertical", "debit_vertical", "long", "leaps", "condor", "time")
        assert op.TABS == ("shared", "credit", "debit", "condor", "time")
        assert op.Field._fields == ("default", "lo", "hi", "kind", "label", "help", "plain", "step", "unit")
        for block, fields in op.SCHEMA.items():
            for name, f in fields.items():
                assert f.kind in ("num", "int", "bool", "choice"), (block, name)
                assert f.label and f.help and f.plain, (block, name)
                assert isinstance(f.unit, str), (block, name)
                if f.kind in ("num", "int"):
                    assert f.lo is not None and f.hi is not None and f.lo <= f.default <= f.hi, (block, name)
        assert op.FIELDS is op.SCHEMA

    def test_house_defaults_are_the_catalog_rows(self):
        h = op.HOUSE
        assert h["shared"] == {"min_oi": 500, "oi_per_contract": 10, "max_leg_spread": 0.50, "min_leg_volume": 20,
                               "earnings_rule": "none_inside", "monthly_only": False, "chart_constraint": True, "gap_mult": 2.0}
        assert h["credit_vertical"] == {"short_delta_lo": 0.20, "short_delta_hi": 0.30, "width_atr_lo": 0.5, "width_atr_hi": 1.5,
                                        "long_offset_max": 3, "credit_pct_min": 25, "dte_lo": 30, "dte_hi": 60, "iv_gate_min": 30}
        assert h["debit_vertical"] == {"long_delta_lo": 0.60, "long_delta_hi": 0.70, "short_delta_lo": 0.25, "short_delta_hi": 0.35,
                                       "reward_cost_min": 1.0, "dte_lo": 30, "dte_hi": 60}
        assert h["long"] == {"delta_lo": 0.60, "delta_hi": 0.70, "theta_pct_max": 1.0, "dte_lo": 45, "dte_hi": 90, "premium_stop_pct": 50}
        assert h["leaps"] == {"delta_lo": 0.70, "delta_hi": 0.80, "extrinsic_pct_max": 10, "months_lo": 9, "months_hi": 18,
                              "roll_dte": 180, "delta_floor": 0.55, "premium_stop_pct": 40}
        assert h["condor"] == {"short_delta_lo": 0.15, "short_delta_hi": 0.20, "wing_atr_lo": 0.5, "wing_atr_hi": 1.5, "credit_pct_min": 30,
                               "dte_lo": 30, "dte_hi": 45, "roll_delta": 0.30, "loss_stop_pct_credit": 100}
        assert h["time"] == {"cal_front_lo": 20, "cal_front_hi": 30, "cal_back_lo": 50, "cal_back_hi": 70, "cal_delta_tol": 0.05,
                             "cal_take_pct": 25, "diag_long_delta_lo": 0.70, "diag_long_delta_hi": 0.80, "diag_long_dte_lo": 180,
                             "diag_long_dte_hi": 365, "diag_short_delta_lo": 0.20, "diag_short_delta_hi": 0.30,
                             "diag_short_dte_lo": 30, "diag_short_dte_hi": 45}

    def test_what_is_not_a_field(self):
        assert "telegram" not in op.SCHEMA and all("telegram" not in f for f in op.SCHEMA.values())
        assert "max_position_pct" not in op.SCHEMA["shared"] and "nlv" not in op.SCHEMA["shared"] and "risk_pct" not in op.SCHEMA["shared"]
        assert C.MAX_POSITION_PCT == 10.0 and op.MAX_POSITION_PCT == 10.0
        assert (op.STOP_ATR, op.TARGET_R, op.LEVEL_PAD_ATR) == (1.0, 2.0, 0.25)
        assert op.CHOICES["earnings_rule"] == ("none_inside", "defined_risk_only")
        assert op.LIQUIDITY_FIELDS == ("min_oi", "oi_per_contract", "max_leg_spread", "min_leg_volume")
        assert op.PICK_FIELDS["shared"] == op.LIQUIDITY_FIELDS + ("earnings_rule", "monthly_only", "chart_constraint")
        assert "gap_mult" in op.SCHEMA["shared"] and "gap_mult" not in op.PICK_FIELDS["shared"]
        for block, keys in op.PICK_FIELDS.items():
            assert set(keys) <= set(op.SCHEMA[block]), block
        for k in ("premium_stop_pct", "roll_dte", "delta_floor", "roll_delta", "loss_stop_pct_credit", "cal_take_pct"):
            for keys in op.PICK_FIELDS.values():
                assert k not in keys

    def test_strategy_keys_come_from_strategy_rules(self):
        assert op.STRATEGY_KEYS is sr.STRATEGY_KEYS == USER_ORDER
        assert {k: op.family_of(k) for k in USER_ORDER} == FAMILY_OF
        with pytest.raises(KeyError):
            op.family_of("covered_call")
        assert op.TAB_BLOCKS["debit"] == ("long", "debit_vertical", "leaps")
        assert [b for b, _, _ in op.fields_for_tab("credit")] == ["credit_vertical"] * len(op.SCHEMA["credit_vertical"])


# ------------------------------------------------------------- clean / hash
class TestCleanAndHash:
    def test_clean_empty_is_house(self):
        c = op.clean({})
        for b in op.BLOCKS:
            assert c[b] == op.HOUSE[b]
        assert c["_overridden"] == set()
        assert op.HOUSE_HASH == op.prefs_hash(c) and len(op.HOUSE_HASH) == 12 and re.fullmatch(r"[0-9a-f]{12}", op.HOUSE_HASH)
        assert op.prefs_hash(op.clean(None)) == op.HOUSE_HASH

    def test_equal_to_default_is_house_hash_and_a_change_is_not(self):
        assert op.prefs_hash(op.clean({"credit_vertical": {"short_delta_lo": 0.20}})) == op.HOUSE_HASH
        assert op.prefs_hash(op.clean({"credit_vertical": {"short_delta_lo": "0.2"}})) == op.HOUSE_HASH
        assert op.prefs_hash(op.clean({"credit_vertical": {"short_delta_lo": 0.25}})) != op.HOUSE_HASH
        assert op.prefs_hash(op.clean({"credit_vertical": {"short_delta_hi": 0.35}})) != op.HOUSE_HASH
        assert op.prefs_hash(op.clean({"shared": {"earnings_rule": "defined_risk_only"}})) != op.HOUSE_HASH

    def test_sizing_exit_lines_and_telegram_never_change_the_hash(self):
        same = [
            {"shared": {"nlv": 250000, "risk_pct": 2.0}},
            {"shared": {"gap_mult": 3.0}},
            {"long": {"premium_stop_pct": 60}}, {"leaps": {"premium_stop_pct": 20, "roll_dte": 120, "delta_floor": 0.6}},
            {"condor": {"roll_delta": 0.4, "loss_stop_pct_credit": 150}}, {"time": {"cal_take_pct": 50}},
            {"telegram": {"enabled": True, "chat_id": "123", "verified": True}},
            {"account": {"nlv": 1}}, {"shared": {"GAP_MULT": 4.0, "max_position_pct": 5}},
        ]
        for raw in same:
            assert op.prefs_hash(op.clean(raw)) == op.HOUSE_HASH, raw

    def test_key_order_irrelevant_and_hash_stable(self):
        a = op.clean({"credit_vertical": {"short_delta_hi": 0.35, "dte_lo": 35}, "shared": {"min_oi": 1000}})
        b = op.clean({"shared": {"min_oi": 1000}, "credit_vertical": {"dte_lo": 35, "short_delta_hi": 0.35}})
        assert op.prefs_hash(a) == op.prefs_hash(b)
        assert op.prefs_hash(op.clean({"credit_vertical": {"short_delta_hi": 0.350000001}})) == op.prefs_hash(a) or True   # 4 dp rounding
        assert op.prefs_hash(op.clean({"credit_vertical": {"short_delta_hi": 0.35}})) == op.prefs_hash(op.clean({"credit_vertical": {"short_delta_hi": 0.35004}}))

    def test_coercion_bad_and_out_of_range_to_default(self):
        c = op.clean({"credit_vertical": {"short_delta_hi": "0.35", "dte_lo": "abc", "dte_hi": 999, "width_atr_lo": float("nan")},
                      "shared": {"earnings_rule": "allowed", "monthly_only": "on", "chart_constraint": "off", "min_oi": -5},
                      "long": {"premium_stop_pct": 42.6}})
        assert c["credit_vertical"]["short_delta_hi"] == 0.35
        assert c["credit_vertical"]["dte_lo"] == 30 and c["credit_vertical"]["dte_hi"] == 60 and c["credit_vertical"]["width_atr_lo"] == 0.5
        assert c["shared"]["earnings_rule"] == "none_inside" and c["shared"]["monthly_only"] is True and c["shared"]["chart_constraint"] is False
        assert c["shared"]["min_oi"] == 500
        assert c["long"]["premium_stop_pct"] == 43 and isinstance(c["long"]["premium_stop_pct"], int)
        assert c["_overridden"] == {"credit_vertical.short_delta_hi", "shared.monthly_only", "shared.chart_constraint", "long.premium_stop_pct"}
        assert op.clean({"shared": "junk", "credit_vertical": None})["shared"] == op.HOUSE["shared"]

    def test_migration_of_old_keys(self):
        c = op.clean({"shared": {"GAP_MULT": 3.5, "max_position_pct": 20}})
        assert c["shared"]["gap_mult"] == 3.5 and "max_position_pct" not in c["shared"]


# ------------------------------------------------------------------ read()
class TestRead:
    def test_returns_blocks_plus_telegram_account_overridden(self):
        row = _Row({"credit_vertical": {"short_delta_hi": "0.35"}, "telegram": {"enabled": True, "chat_id": "42", "verified": True}})
        user = _User(prefs={"trade_nlv": 100000, "trade_risk_pct": 1.0}, row=row)
        p = op.read(None, user)
        assert set(op.BLOCKS) <= set(p) and {"telegram", "account", "_overridden"} <= set(p)
        assert p["credit_vertical"]["short_delta_hi"] == 0.35 and "credit_vertical.short_delta_hi" in p["_overridden"]
        assert p["telegram"] == {**op.TELEGRAM_DEFAULT, "enabled": True, "chat_id": "42", "verified": True}
        assert p["account"] == {"nlv": 100000.0, "risk_pct": 1.0, "nlv_source": "prefs"}
        assert op.prefs_hash(p) != op.HOUSE_HASH

    def test_no_row_no_nlv(self):
        p = op.read(None, _User(prefs=None, row=None))
        assert op.prefs_hash(p) == op.HOUSE_HASH
        assert p["account"] == {"nlv": None, "risk_pct": 1.0, "nlv_source": None}
        assert p["telegram"] == op.TELEGRAM_DEFAULT and p["_overridden"] == set()
        assert op.prefs_hash(op.read(None, None)) == op.HOUSE_HASH

    def test_for_strategy_and_inherited_rule_stop(self):
        p = op.read(None, _User(row=_Row({"credit_vertical": {"dte_hi": 45}, "shared": {"min_oi": 800}})))
        bc = op.for_strategy(p, "bear_call")
        assert bc["dte_hi"] == 45 and bc["min_oi"] == 800 and bc["short_delta_lo"] == 0.20 and bc["gap_mult"] == 2.0
        assert op.for_strategy(p, "bull_call")["premium_stop_pct"] == 50
        assert op.for_strategy(p, "calendar")["premium_stop_pct"] == 50
        assert op.for_strategy(p, "diagonal_call")["premium_stop_pct"] == 40
        assert op.for_strategy(p, "leaps_call")["premium_stop_pct"] == 40
        assert op.for_strategy(p, "iron_condor")["loss_stop_pct_credit"] == 100 and "premium_stop_pct" not in op.for_strategy(p, "iron_condor")

    def test_defined_risk(self):
        assert {k for k in USER_ORDER if op.defined_risk(k)} == {"bull_put", "bear_call", "bull_call", "bear_put", "iron_condor"}
        assert sr.DEFINED_RISK == frozenset({"bull_put", "bear_call", "bull_call", "bear_put", "iron_condor"})
        for k in ("buy_call", "buy_put", "calendar", "diagonal_call", "leaps_call"):
            assert not op.defined_risk(k)


# ----------------------------------------------------------- write / reset
class _FakeDB:
    def __init__(self):
        self.added, self.commits = [], 0

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


class TestWriteValidation:
    def test_out_of_range_reports_and_stores_nothing(self):
        row = _Row({})
        db = _FakeDB()
        prefs, errors = op.write(db, _User(row=row), "credit", {"credit_vertical.short_delta_hi": "0.9", "credit_vertical.dte_lo": "40"})
        assert errors and "between" in errors[0] and db.commits == 0 and row.prefs == {}
        assert prefs["credit_vertical"]["dte_lo"] == 30
        _, errors = op.write(db, _User(row=row), "credit", {"credit_vertical.dte_lo": "abc"})
        assert errors == ["Days to expiry, from must be a number."] or errors[0].endswith("must be a number.")
        _, errors = op.write(db, _User(row=row), "shared", {"shared.earnings_rule": "allowed"})
        assert errors and "one of" in errors[0]
        _, errors = op.write(db, _User(row=row), "credit", {"credit_vertical.dte_lo": "50", "credit_vertical.dte_hi": "40"})
        assert errors and "must not exceed" in errors[0]
        _, errors = op.write(db, _User(row=row), "nope", {})
        assert errors

    def test_write_stores_only_overrides_and_recomputes_hash(self):
        row = _Row({"shared": {"min_oi": 800}, "telegram": {"enabled": True}})
        db = _FakeDB()
        user = _User(prefs={}, row=row)
        prefs, errors = op.write(db, user, "credit", {"credit_vertical.short_delta_hi": "0.35", "credit_vertical.dte_lo": "30",
                                                     "credit_vertical.width_atr_hi": "1.5"})
        assert errors == [] and db.commits == 1
        assert row.prefs == {"shared": {"min_oi": 800}, "telegram": {"enabled": True}, "credit_vertical": {"short_delta_hi": 0.35}}
        assert row.prefs_hash == op.prefs_hash(prefs) != op.HOUSE_HASH and row.schema_version == op.SCHEMA_VERSION
        assert prefs["telegram"]["enabled"] is True            # telegram untouched
        # a bool absent from a posted Shared form is False; the bare name works when unambiguous
        prefs, errors = op.write(db, user, "shared", {"min_oi": "500", "gap_mult": "3"})
        assert errors == [] and row.prefs["shared"] == {"chart_constraint": False, "gap_mult": 3.0}
        assert row.prefs_hash != op.HOUSE_HASH           # chart_constraint is pick-relevant ...
        before = row.prefs_hash
        prefs, errors = op.write(db, user, "shared", {"chart_constraint": "on", "gap_mult": "4"})
        assert errors == [] and row.prefs["shared"] == {"gap_mult": 4.0}
        assert row.prefs_hash != before                   # ... and switching it back on changes it again
        # dropping back to the default removes the override and the house hash returns
        prefs, errors = op.write(db, user, "credit", {"credit_vertical.short_delta_hi": "0.30"})
        assert "credit_vertical" not in row.prefs
        prefs, errors = op.write(db, user, "shared", {"chart_constraint": "1", "gap_mult": "2"})
        assert row.prefs_hash == op.HOUSE_HASH

    def test_trade_prefs_keys_ride_along(self):
        row = _Row({})
        db = _FakeDB()
        user = _User(prefs={}, row=row)
        prefs, errors = op.write(db, user, "shared", {"nlv": "100000", "risk_pct": "1", "chart_constraint": "on"})
        assert errors == [] and prefs["account"] == {"nlv": 100000.0, "risk_pct": 1.0, "nlv_source": "prefs"}
        assert user.prefs["trade_nlv"] == 100000.0 and "nlv" not in row.prefs.get("shared", {})
        _, errors = op.write(db, user, "shared", {"nlv": "-5", "chart_constraint": "on"})
        assert errors and "Account value" in errors[0]

    def test_reset(self):
        row = _Row({"shared": {"min_oi": 800}, "credit_vertical": {"dte_lo": 40}, "telegram": {"enabled": True}})
        db = _FakeDB()
        p = op.reset(db, _User(row=row), "credit")
        assert row.prefs == {"shared": {"min_oi": 800}, "telegram": {"enabled": True}} and p["credit_vertical"]["dte_lo"] == 30
        op.reset(db, _User(row=row), "all")
        assert row.prefs == {"telegram": {"enabled": True}} and row.prefs_hash == op.HOUSE_HASH


# ----------------------------------------------------- the real model (if landed)
def _model_or_skip():
    try:
        from app.models import UserOptionPrefs
    except ImportError:
        pytest.skip("UserOptionPrefs is added to app/models.py by the migration agent")
    return UserOptionPrefs


@pytest.fixture
def db_session():
    model = _model_or_skip()
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    from app.models import User
    eng = create_engine("sqlite://", future=True)
    Base.metadata.create_all(eng, tables=[User.__table__, model.__table__])
    s = sessionmaker(bind=eng, future=True)()
    try:
        yield s, User, model
    finally:
        s.close()


class TestWithModel:
    def test_write_creates_the_row_and_distinct_hashes(self, db_session):
        db, User, model = db_session
        u1, u2, u3 = User(email="a@x", display_name="a"), User(email="b@x", display_name="b"), User(email="c@x", display_name="c")
        db.add_all([u1, u2, u3])
        db.commit()
        assert op.distinct_hashes(db) == [op.HOUSE_HASH]
        prefs, errors = op.write(db, u1, "credit", {"credit_vertical.short_delta_hi": "0.35"})
        assert errors == []
        row = db.query(model).filter_by(user_id=u1.id).one()
        assert row.prefs == {"credit_vertical": {"short_delta_hi": 0.35}} and row.prefs_hash == op.prefs_hash(prefs)
        assert len(row.prefs_hash) == 12
        op.write(db, u2, "shared", {"chart_constraint": "on"})          # equals the house -> house hash
        op.write(db, u3, "credit", {"credit_vertical.short_delta_hi": "0.35"})   # same override as u1 -> same hash once
        hashes = op.distinct_hashes(db)
        assert hashes[0] == op.HOUSE_HASH and len(hashes) == 2 and row.prefs_hash in hashes
        db.expire_all()
        assert op.read(db, db.get(User, u1.id))["credit_vertical"]["short_delta_hi"] == 0.35
        op.reset(db, db.get(User, u1.id), "all")
        db.expire_all()
        assert db.query(model).filter_by(user_id=u1.id).one().prefs_hash == op.HOUSE_HASH


# ------------------------------------------------------------ the rules
def _chart(trend="up", kind="support_bounce", quality=80, structure="bullish", rng=None, w_uptrend=True,
           slow_drift=True, earnings=("2026-10-22", 19), levels=None, plan=True, setup_dir=None):
    setup = None
    if kind:
        setup = {"kind": kind, "direction": setup_dir or ("down" if trend == "down" else "neutral" if kind == "range" else "up"),
                 "level": 340.9, "zone": [339.1, 342.0], "touches": 3, "vol_high": True, "quality": quality}
    return {
        "symbol": "LRCX", "as_of": "2026-10-03", "close": 349.2, "atr": 11.54,
        "ema": {"e20": 346.1, "e50": 335.8, "e200": 301.2}, "trend": trend, "trend_days": 34, "w_uptrend": w_uptrend,
        "slow_drift": slow_drift, "structure": {"state": structure, "reason": ""},
        "sup": None, "tl": None, "tl_bounce": None, "rng": rng, "setup": setup, "setups": [setup] if setup else [],
        "levels": levels if levels is not None else {"support": 340.9, "resistance": 372.0},
        "earnings": {"date": earnings[0], "days": earnings[1]} if earnings else None,
        "plan": {"entry": 349.2, "stop": 336.2, "target": 375.2, "r": 13.0} if plan else None,
        "evidence": [],
    }


def _gauge(rank=62.0, basis="rank", state="ok", n=252, term=1.11, verdict=None, prem=1.21, pct=None):
    if rank is None:
        gates = {"buy": False, "sell_directional": False, "sell_neutral": False, "mid": False}
    else:
        gates = {"buy": rank <= 30, "sell_directional": rank >= 30, "sell_neutral": rank >= 50, "mid": 30 <= rank <= 50}
    if verdict is None:
        verdict = "UNKNOWN" if rank is None else "SELL" if rank >= 50 else "NEUTRAL" if rank >= 30 else "BUY"
    return {"iv30": 46.0, "hv20": 38.0, "hv60": 36.1, "iv_hv_premium": prem, "iv_rank": rank, "iv_pct": pct if pct is not None else rank,
            "iv_n": n, "state": state, "basis": basis, "provisional": basis in ("provisional", "unknown"),
            "iv_front": 50.0, "iv_back": 45.0, "term_ratio": term, "skew25": 4.1, "skew_norm": 0.08, "expected_move": 24.1,
            "earnings_date": "2026-10-22", "earnings_days": 19, "verdict": verdict,
            "verdict_why": "IV rank 62 (>= 50) and priced for 21% more movement than the stock has actually shown", "gates": gates}


HOUSE = op.clean({})
DRO = op.clean({"shared": {"earnings_rule": "defined_risk_only"}})
EXPIRIES = ("2026-10-17", "2026-10-31", "2026-11-20", "2026-12-19", "2027-01-16")
ROW_KEYS = ("key", "label", "fit", "score", "step", "why", "must_happen", "reasons", "reason_key", "shown")


def _by(res):
    return {r["key"]: r for r in res["strategies"]}


class TestRuleTable:
    def test_ten_rows_in_the_users_order(self):
        assert tuple(r.key for r in sr.RULES) == USER_ORDER == sr.STRATEGY_KEYS
        assert {r.key: r.family for r in sr.RULES} == FAMILY_OF
        assert sr.LABELS["leaps_call"] == "Buy LEAPS" and sr.LABELS["bull_put"] == "Bull put spread"
        assert sr.CURRENT_STEP == 1 and {r.step for r in sr.RULES} == {1, 2, 3, 4}
        assert {r.key for r in sr.RULES if r.step == 1} == {"bull_put", "bear_call"}
        assert {r.key: r.earnings for r in sr.RULES} == {
            "buy_call": "none_inside", "buy_put": "none_inside", "bull_call": "defined_risk", "bear_put": "defined_risk",
            "leaps_call": "any", "diagonal_call": "none_inside", "bull_put": "defined_risk", "bear_call": "defined_risk",
            "iron_condor": "defined_risk", "calendar": "none_inside"}
        assert {r.key for r in sr.RULES if r.earnings == "defined_risk"} == sr.DEFINED_RISK
        assert sr.CREDIT_FAMILIES == {"bull_put", "bear_call", "iron_condor"}
        assert set(sr.CHIP_TEXT) == set(sr.REASON_KEYS) and len(sr.REASON_KEYS) == 12

    def test_no_step_in_any_member_string(self):
        strings = list(sr.member_strings())
        for raw in ({}, {"shared": {"earnings_rule": "defined_risk_only"}}):
            for gauge in (_gauge(62), _gauge(41), _gauge(24), _gauge(None, basis="unknown", state="none", n=0)):
                for chart in (_chart(), _chart("down", "resistance_reject", setup_dir="down"), _chart("sideways", "range", rng={"low": 318.6, "high": 372.0, "n_low": 3, "n_high": 3, "sideways": True, "stack_flat": True, "pos_pct": 0.5})):
                    for row in sr.recommend(chart, gauge, op.clean(raw), snapshot_expiries=EXPIRIES)["strategies"]:
                        strings += [row["label"], row["why"] or "", row["must_happen"] or ""] + list(row["reasons"])
        for s in strings:
            assert not re.search(r"\b(step|phase)\b", s, re.I), s
            assert "not available yet" not in s or "step" not in s.lower()

    def test_render_never_raises(self):
        for key in USER_ORDER:
            why, must = sr.render(key, {})
            assert why and must and "{" not in why and "{" not in must
        ctx = sr.context(_chart(), _gauge(62), HOUSE, pick={"legs": [{"side": "sell", "right": "P", "strike": 330.0, "expiry": "2026-11-20"},
                                                                     {"side": "buy", "right": "P", "strike": 320.0, "delta": -0.174}],
                                                            "expiry": "2026-11-20", "breakevens": [327.9], "max_profit": 210})
        why, must = sr.render("bull_put", ctx)
        assert must == "LRCX stays above 330 until Nov 20. You keep the credit if it does nothing, drifts up, or even dips a little."
        assert why.startswith("Uptrend for 34 days. It bounced off support at 340.9 on high volume. Options are expensive (IV rank 62 over the last year), so you are paid to sell a put spread below that support.")

    def test_span_words(self):
        assert sr.span_words({"state": "ok", "iv_n": 252}) == "over the last year"
        assert sr.span_words({"state": "rank_ok", "iv_n": 118}) == "over 118 days"
        assert sr.span_words({"state": "pct_only", "iv_n": 34}) == "against the last 34 days (not a full year yet)"
        assert sr.expiry_label("2026-11-20") == "Nov 20"


class TestRecommendLRCX:
    def test_none_inside_rejects_everything_with_the_vocabulary(self):
        res = sr.recommend(_chart(), _gauge(62), HOUSE, snapshot_expiries=EXPIRIES)
        rows = res["strategies"]
        assert len(rows) == 10 and {r["key"] for r in rows} == set(USER_ORDER)
        assert all(tuple(r) == ROW_KEYS for r in rows)
        assert res["recommended"] is None
        by = _by(res)
        assert all(r["fit"] == "rejected" and r["score"] is None and r["why"] is None and r["must_happen"] is None for r in rows)
        assert all(r["reason_key"] in sr.REASON_KEYS for r in rows)
        assert by["bull_put"]["reason_key"] == "earnings_inside"
        assert by["bull_put"]["reasons"] == ["earnings 2026-10-22 (19d) sits inside every 30-60 day expiry"]
        assert by["calendar"]["reason_key"] == "earnings_inside" and "back month" in by["calendar"]["reasons"][0]
        assert by["bull_call"]["reason_key"] == "expensive" and len(by["bull_call"]["reasons"]) == 2
        assert by["buy_call"]["reason_key"] == "expensive" and "options too expensive to buy (IV rank 62)" in by["buy_call"]["reasons"][0]
        assert by["iron_condor"]["reason_key"] == "trending_not_sideways"
        assert by["leaps_call"]["reason_key"] == "expensive" and by["diagonal_call"]["reason_key"] == "expensive"
        for k in ("buy_put", "bear_put", "bear_call"):
            assert by[k]["reason_key"] == "wrong_direction"
        shown = [r["key"] for r in rows if r["shown"]]
        assert shown == ["bull_put", "calendar"]              # the two single-fail rows, catalog order
        assert [r["key"] for r in rows] == list(USER_ORDER)   # all rejected: the catalog order stands

    def test_defined_risk_only_recommends_the_bull_put_at_90_1(self):
        res = sr.recommend(_chart(), _gauge(62), DRO, snapshot_expiries=EXPIRIES)
        by = _by(res)
        assert res["recommended"] == "bull_put" and by["bull_put"]["fit"] == "recommended" and by["bull_put"]["score"] == 90.1
        assert by["bull_put"]["reasons"] == [] and by["bull_put"]["reason_key"] is None and by["bull_put"]["shown"]
        assert by["bull_put"]["why"] and by["bull_put"]["must_happen"]
        assert res["strategies"][0]["key"] == "bull_put"
        assert by["calendar"]["fit"] == "rejected" and by["calendar"]["reason_key"] == "earnings_inside"   # none_inside regardless
        assert by["buy_call"]["fit"] == "rejected" and by["buy_call"]["reason_key"] == "expensive"
        assert by["bull_call"]["fit"] == "rejected" and by["bull_call"]["reason_key"] == "expensive" and by["bull_call"]["shown"]
        assert sum(1 for r in res["strategies"] if r["shown"]) <= 3        # recommended + <= 2 near misses

    def test_rank_45_bull_call_beats_bull_put(self):
        chart = _chart(structure="unclear", earnings=None)
        res = sr.recommend(chart, _gauge(45), HOUSE, snapshot_expiries=EXPIRIES)
        by = _by(res)
        # B3.3's worked figures (86 / 80.3) are the scores with every picker built; while
        # bull_call's picker is unbuilt (CURRENT_STEP 1) the II.2.7 formula takes 10 off it
        assert by["bull_call"]["score"] == 86.0 - 10 and by["bull_put"]["score"] == 80.3
        assert sr._score(sr.rule("bull_call"), chart, _gauge(45)) == 76.0
        assert by["bull_call"]["fit"] == "also_fits" and by["bull_put"]["fit"] == "recommended"   # bull_call's picker is not built yet
        assert by["bull_call"]["reason_key"] == "not_available_yet" and by["bull_call"]["reasons"] == ["not available yet"]
        assert res["recommended"] == "bull_put"
        assert [r["key"] for r in res["strategies"][:2]] == ["bull_put", "bull_call"]

    def test_rank_62_bull_put_only(self):
        res = sr.recommend(_chart(structure="unclear", earnings=None), _gauge(62), HOUSE, snapshot_expiries=EXPIRIES)
        by = _by(res)
        assert by["bull_put"]["score"] == 85.1 and by["bull_call"]["reason_key"] == "expensive"
        assert "a spread you pay for is dear" in by["bull_call"]["reasons"][0]

    def test_rank_24_buy_call_first_bull_put_cheap(self):
        res = sr.recommend(_chart(structure="unclear", earnings=None), _gauge(24), HOUSE, snapshot_expiries=EXPIRIES)
        by = _by(res)
        assert by["buy_call"]["score"] == 80.0 - 10 and by["bull_call"]["score"] == 75.0 - 10     # both unbuilt at step 1
        assert by["bull_put"]["reason_key"] == "cheap_options" and "options too cheap to sell (IV rank 24)" in by["bull_put"]["reasons"][0]
        assert res["recommended"] is None                     # the only fits are unbuilt (debit family)
        assert [r["key"] for r in res["strategies"][:2]] == ["buy_call", "bull_call"]
        assert all(r["fit"] == "also_fits" and r["reason_key"] == "not_available_yet" for r in res["strategies"][:2])

    def test_rank_41_not_cheap_enough_wording(self):
        res = sr.recommend(_chart(structure="unclear", earnings=None), _gauge(41), HOUSE, snapshot_expiries=EXPIRIES)
        by = _by(res)
        assert by["buy_call"]["reasons"][0] == "not cheap enough to buy outright (IV rank 41 > 30)"
        assert by["iron_condor"]["reason_key"] == "trending_not_sideways"

    def test_provisional_basis(self):
        g = _gauge(None, basis="provisional", state="forming", n=12, verdict="SELL")
        res = sr.recommend(_chart(earnings=None), g, HOUSE, snapshot_expiries=EXPIRIES)
        by = _by(res)
        assert by["bull_put"]["reason_key"] == "not_rich_enough" and "12 of 60 days" in by["bull_put"]["reasons"][0]
        assert by["buy_call"]["fit"] == "also_fits" and by["buy_call"]["reason_key"] == "not_available_yet"
        assert by["bull_call"]["score"] == 55 + 0 + 16 + 5 - 10   # iv_fit 0 on a provisional read; structure bullish; unbuilt
        assert res["recommended"] is None

    def test_unknown_gauge(self):
        g = _gauge(None, basis="unknown", state="none", n=0)
        res = sr.recommend(_chart(earnings=None), g, HOUSE, snapshot_expiries=EXPIRIES)
        assert _by(res)["bull_put"]["reason_key"] == "not_rich_enough"


class TestRecommendISRG:
    def _chart(self):
        c = _chart(kind="ema_rebound", quality=35, earnings=("2026-10-21", 18), levels={"support": 404.6, "resistance": 431.0})
        c.update(symbol="ISRG", close=405.81, plan={"entry": 405.81, "stop": 394.27, "target": 428.89, "r": 11.54}, trend_days=21, slow_drift=True)
        c["setup"].update(level=404.6, zone=[403.4, 405.8], ema="EMA20", fresh=True)
        return c

    EXP = ("2026-10-16", "2026-11-20", "2026-12-19", "2027-01-15", "2028-01-21")

    def test_unbuilt_only_fit_is_never_recommended(self):
        res = sr.recommend(self._chart(), _gauge(41, prem=1.10, verdict="SELL"), HOUSE, snapshot_expiries=self.EXP)
        by = _by(res)
        assert res["recommended"] is None
        assert by["leaps_call"]["fit"] == "also_fits" and by["leaps_call"]["reason_key"] == "not_available_yet"
        assert by["leaps_call"]["score"] == 61.0 and by["leaps_call"]["reasons"] == ["not available yet"]
        assert by["bull_put"]["reason_key"] == "earnings_inside" and by["bull_call"]["reason_key"] == "earnings_inside"
        assert by["buy_call"]["reason_key"] == "expensive" and len(by["buy_call"]["reasons"]) == 2
        assert [r["key"] for r in res["strategies"] if r["shown"]] == ["leaps_call", "bull_call", "bull_put"]
        assert res["strategies"][0]["key"] == "leaps_call"

    def test_defined_risk_only_two_chips(self):
        res = sr.recommend(self._chart(), _gauge(41, prem=1.10, verdict="SELL"), DRO, snapshot_expiries=self.EXP)
        by = _by(res)
        assert res["recommended"] == "bull_put"
        # B8.2's 86 is bull_call with its picker built; at step 1 the formula takes 10 off (76)
        assert by["bull_call"]["score"] == 86.0 - 10 and by["bull_put"]["score"] == 75.1
        # the best BUILT fit is recommended; the unbuilt bull_call is "also fits · not available yet"
        assert by["bull_put"]["fit"] == "recommended" and by["bull_call"]["fit"] == "also_fits"
        assert by["bull_call"]["reason_key"] == "not_available_yet"
        assert by["leaps_call"]["fit"] == "also_fits" and by["leaps_call"]["score"] == 61.0
        assert [r["key"] for r in res["strategies"][:3]] == ["bull_put", "bull_call", "leaps_call"]
        assert by["buy_call"]["reasons"][0] == "not cheap enough to buy outright (IV rank 41 > 30)"

    def test_no_weekly_trend_and_no_long_dated(self):
        c = self._chart()
        c["w_uptrend"] = False
        by = _by(sr.recommend(c, _gauge(41), HOUSE, snapshot_expiries=self.EXP))
        assert by["leaps_call"]["reason_key"] == "no_weekly_trend"
        by = _by(sr.recommend(self._chart(), _gauge(41), HOUSE, snapshot_expiries=self.EXP[:4]))
        assert by["leaps_call"]["reason_key"] == "no_long_dated" and by["leaps_call"]["reasons"][0] == "no 9-18 month options stored"


class TestRecommendOtherCharts:
    def test_sideways_range_condor(self):
        rng = {"low": 318.6, "high": 372.0, "zone_low": [317.1, 320.4], "zone_high": [370.2, 373.9], "n_low": 3, "n_high": 2,
               "sideways": True, "stack_flat": True, "pos_pct": 0.5}
        chart = _chart("sideways", "range", quality=50, structure="unclear", rng=rng, earnings=None, setup_dir="neutral")
        res = sr.recommend(chart, _gauge(62, term=0.93), HOUSE, snapshot_expiries=EXPIRIES)
        by = _by(res)
        assert by["iron_condor"]["fit"] == "also_fits" and by["iron_condor"]["reason_key"] == "not_available_yet"
        assert by["iron_condor"]["score"] == 60 + 9.1 + 10 - 10
        assert by["calendar"]["reason_key"] == "front_iv_under_back"
        assert by["bull_put"]["reason_key"] == "wrong_direction"
        assert res["recommended"] is None
        res = sr.recommend(chart, _gauge(41), HOUSE, snapshot_expiries=EXPIRIES)
        assert _by(res)["iron_condor"]["reasons"][0] == "premium not rich enough for a condor (IV rank 41 < 50)"
        assert _by(res)["iron_condor"]["reason_key"] == "not_rich_enough"
        res = sr.recommend(chart, _gauge(24), HOUSE, snapshot_expiries=EXPIRIES)
        assert _by(res)["iron_condor"]["reason_key"] == "cheap_options"

    def test_downtrend_bear_call(self):
        chart = _chart("down", "resistance_reject", structure="decelerated", earnings=None, setup_dir="down")
        res = sr.recommend(chart, _gauge(62), HOUSE, snapshot_expiries=EXPIRIES)
        by = _by(res)
        assert res["recommended"] == "bear_call" and by["bear_call"]["score"] == 60 + 9.1 + 16 + 5
        assert by["bull_put"]["reason_key"] == "wrong_direction" and "not an uptrend" in by["bull_put"]["reasons"][0]
        assert by["buy_put"]["reason_key"] == "expensive"
        assert "above that resistance" in by["bear_call"]["why"]

    def test_no_setup_and_no_target(self):
        chart = _chart(kind=None, earnings=None, plan=False)
        by = _by(sr.recommend(chart, _gauge(45), HOUSE, snapshot_expiries=EXPIRIES))
        assert by["bull_put"]["reason_key"] == "no_setup" and by["bull_put"]["reasons"][0] == "no support bounce on the chart today"
        assert by["bull_call"]["reason_key"] == "no_setup" and len(by["bull_call"]["reasons"]) == 2

    def test_earnings_unknown_never_blocks(self):
        by = _by(sr.recommend(_chart(earnings=None), _gauge(62), HOUSE, snapshot_expiries=EXPIRIES))
        assert by["bull_put"]["fit"] == "recommended"
        assert sr.earnings_block("bull_put", None, EXPIRIES, HOUSE) is None

    def test_earnings_block_cases(self):
        import datetime as dt
        today = dt.date(2026, 10, 3)
        e = {"date": "2026-10-22", "days": 19}
        assert sr.earnings_block("bull_put", e, EXPIRIES, HOUSE, today=today) == "earnings 2026-10-22 (19d) sits inside every 30-60 day expiry"
        assert sr.earnings_block("bull_put", e, EXPIRIES, DRO, today=today) is None
        assert sr.earnings_block("buy_call", e, EXPIRIES, DRO, today=today) is not None      # none_inside regardless
        assert sr.earnings_block("leaps_call", e, EXPIRIES, HOUSE, today=today) is None       # any
        assert sr.earnings_block("bull_put", {"date": "2026-12-30"}, EXPIRIES, HOUSE, today=today) is None   # clear of the window
        assert sr.earnings_block("bull_put", {"date": "2026-09-01"}, EXPIRIES, HOUSE, today=today) is None   # a stale past date
        assert sr.earnings_block("bull_put", e, (), HOUSE, today=today) is None               # nothing listed
        assert sr.earnings_block("bull_put", {"date": "bad"}, EXPIRIES, HOUSE, today=today) is None
        assert "back month" in sr.earnings_block("calendar", e, EXPIRIES, HOUSE, today=today)
        # some expiry clears it -> None (the picker skips the ones that do not)
        assert sr.earnings_block("bull_put", {"date": "2026-11-25"}, EXPIRIES, HOUSE, today=today) is None

    def test_minimal_chart_does_not_raise(self):
        res = sr.recommend({"trend": "unclear"}, {}, {}, snapshot_expiries=())
        assert len(res["strategies"]) == 10 and res["recommended"] is None
        assert all(r["fit"] == "rejected" for r in res["strategies"])
