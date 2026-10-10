"""Options v2 rules (OPTIONS_V2_DESIGN.md section 7): the schema, the defaults,
partial writes with form strings, clamping, reset, and the v1 -> v2 migration."""
from __future__ import annotations

import inspect

import pytest

from app import models
from app.services import opt_rules as R
from app.services import payoff


# ------------------------------------------------------------------ schema
def test_catalog_order_labels_families():
    assert R.STRATEGIES == ("buy_call", "buy_put", "bull_call", "bear_put", "leaps_call", "diagonal_call",
                            "bull_put", "bear_call", "iron_condor", "calendar")
    assert set(R.LABELS) == set(R.STRATEGIES) == set(R.FAMILY) == set(R.RIGHT)
    assert R.LABELS["bull_put"] == "Bull put spread" and R.LABELS["leaps_call"] == "Buy LEAPS"
    assert set(R.FAMILY.values()) == set(R.FAMILIES) == {
        "single", "debit_vertical", "credit_vertical", "leaps", "diagonal", "condor", "calendar"}
    assert R.FAMILY["iron_condor"] == "condor" and R.FAMILY["diagonal_call"] == "diagonal"
    assert R.DEFAULT_STRATEGY == "bull_put"


def test_every_field_is_complete_and_in_bounds():
    assert set(R.SCHEMA) == {"shared", *R.STRATEGIES}
    for block, fields in R.SCHEMA.items():
        for name, f in fields.items():
            assert f.kind in R.KINDS, (block, name)
            assert f.label and f.help, (block, name)
            if f.kind in ("int", "num"):
                assert f.lo is not None and f.hi is not None and f.lo <= f.default <= f.hi, (block, name)
                assert R.step(name, f) > 0
            if f.kind == "choice":
                assert f.default in f.choices
            if f.kind == "bool":
                assert isinstance(f.default, bool)
    # no field name is in both shared and a strategy (a bare form key is never ambiguous)
    for s in R.STRATEGIES:
        assert not set(R.SCHEMA["shared"]) & set(R.SCHEMA[s])
        assert R.SCHEMA[s]["iv_rank_min"].unit == "" and "iv_rank_max" in R.SCHEMA[s]


# max_leg_spread (the bid/ask $ cap) is per strategy since 2026-10-09: $0.50 on the three
# premium sellers, 0 = off elsewhere (a flat $0.50 blocked every LEAPS on a stock above ~$110)
DESIGN_DEFAULTS = {
    "shared": {"price_min": 20, "stock_vol_min": 0, "oi_min": 100, "opt_vol_min": 0,
               "max_leg_spread_pct": 25, "monthly_only": False, "max_age_h": 24, "per_ticker": 3},
    "buy_call": {"delta_lo": .60, "delta_hi": .70, "dte_lo": 30, "dte_hi": 60, "theta_pct_max": 1.0,
                 "max_leg_spread": 0, "iv_rank_min": 0, "iv_rank_max": 50, "earnings_rule": "none_inside"},
    # width 1.0-6.0 (was 0.5-2.0, which no 0.60-0.70 / 0.25-0.35 pair could ever meet)
    "bull_call": {"long_delta_lo": .60, "long_delta_hi": .70, "short_delta_lo": .25, "short_delta_hi": .35,
                  "width_atr_lo": 1.0, "width_atr_hi": 6.0, "debit_pct_max": 60, "dte_lo": 30, "dte_hi": 60,
                  "max_leg_spread": 0, "iv_rank_min": 0, "iv_rank_max": 70, "earnings_rule": "none_inside"},
    # time value 25% (was 10%, which no 9-18 month call at delta 0.70-0.85 could ever meet)
    "leaps_call": {"delta_lo": .70, "delta_hi": .85, "months_lo": 9, "months_hi": 18, "extrinsic_pct_max": 25,
                   "max_leg_spread": 0, "iv_rank_min": 0, "iv_rank_max": 50, "earnings_rule": "allow"},
    "diagonal_call": {"long_delta_lo": .70, "long_delta_hi": .80, "long_dte_lo": 180, "long_dte_hi": 365,
                      "short_delta_lo": .20, "short_delta_hi": .30, "short_dte_lo": 30, "short_dte_hi": 45,
                      "debit_pct_spot_max": 25, "max_leg_spread": 0, "iv_rank_min": 0, "iv_rank_max": 60,
                      "earnings_rule": "short_leg"},
    "bull_put": {"short_delta_lo": .20, "short_delta_hi": .30, "width_atr_lo": .5, "width_atr_hi": 1.5,
                 "credit_pct_min": 25, "dte_lo": 30, "dte_hi": 60, "max_leg_spread": 0.50, "iv_rank_min": 30,
                 "iv_rank_max": 100, "earnings_rule": "none_inside"},
    "iron_condor": {"short_delta_lo": .15, "short_delta_hi": .20, "wing_atr_lo": .5, "wing_atr_hi": 1.5,
                    "credit_pct_min": 30, "dte_lo": 30, "dte_hi": 45, "max_leg_spread": 0.50, "iv_rank_min": 50,
                    "iv_rank_max": 100, "earnings_rule": "none_inside"},
    "calendar": {"delta_tol": .05, "front_dte_lo": 20, "front_dte_hi": 30, "back_dte_lo": 50, "back_dte_hi": 70,
                 "front_iv_ge_back": False, "max_leg_spread": 0, "iv_rank_min": 0, "iv_rank_max": 60,
                 "earnings_rule": "short_leg"},
}
DESIGN_DEFAULTS["buy_put"] = DESIGN_DEFAULTS["buy_call"]
DESIGN_DEFAULTS["bear_put"] = DESIGN_DEFAULTS["bull_call"]
DESIGN_DEFAULTS["bear_call"] = DESIGN_DEFAULTS["bull_put"]


def test_defaults_are_the_design_table():
    for block, want in DESIGN_DEFAULTS.items():
        got = {k: f.default for k, f in R.SCHEMA[block].items()}
        assert got == want, block
    flat = R.defaults("bull_put")
    assert flat == {**DESIGN_DEFAULTS["shared"], **DESIGN_DEFAULTS["bull_put"]}
    h = R.house()
    assert h["schema"] == 2 and h["shared"] == DESIGN_DEFAULTS["shared"]
    assert h["calendar"] == DESIGN_DEFAULTS["calendar"]
    with pytest.raises(KeyError):
        R.defaults("straddle")


EARNINGS_DEFAULTS = {"buy_call": "none_inside", "buy_put": "none_inside", "bull_call": "none_inside",
                     "bear_put": "none_inside", "leaps_call": "allow", "diagonal_call": "short_leg",
                     "bull_put": "none_inside", "bear_call": "none_inside", "iron_condor": "none_inside",
                     "calendar": "short_leg"}


def test_earnings_rule_is_per_strategy():
    assert "earnings_rule" not in R.SCHEMA["shared"]
    assert R.EARNINGS_CHOICES == ("none_inside", "short_leg", "allow")
    assert R.CHOICE_LABELS["earnings_rule"] == {"none_inside": "No earnings before the last expiry",
                                                "short_leg": "No earnings before the sold leg's expiry",
                                                "allow": "Earnings allowed"}
    for s in R.STRATEGIES:
        f = R.SCHEMA[s]["earnings_rule"]
        assert f.kind == "choice" and f.choices == R.EARNINGS_CHOICES and f.label == "Earnings"
        assert f.default == EARNINGS_DEFAULTS[s], s
        assert R.defaults(s)["earnings_rule"] == EARNINGS_DEFAULTS[s]
        assert "Earnings allowed" in f.help and "not known" in f.help, s     # every help explains the choices
    # plain words per strategy: the one-expiry ones say short_leg = last expiry, the time spreads say why not
    for s in ("buy_call", "bull_call", "bull_put", "iron_condor", "leaps_call"):
        assert "works the same as 'before the last expiry'" in R.SCHEMA[s]["earnings_rule"].help, s
    for s in ("diagonal_call", "calendar"):
        h = R.SCHEMA[s]["earnings_rule"].help
        assert "near call you sell" in h and "far call" in h and "works the same" not in h
    assert "9 to 18 months" in R.SCHEMA["leaps_call"]["earnings_rule"].help
    assert len({R.SCHEMA[s]["earnings_rule"].help for s in R.STRATEGIES}) == 7     # one per family
    assert R.EARNINGS_FROM_SHARED == ("buy_call", "buy_put", "bull_call", "bear_put", "bull_put", "bear_call",
                                      "iron_condor")


SPREAD_DEFAULTS = {"buy_call": 0, "buy_put": 0, "bull_call": 0, "bear_put": 0, "leaps_call": 0,
                   "diagonal_call": 0, "bull_put": 0.50, "bear_call": 0.50, "iron_condor": 0.50, "calendar": 0}


def test_bid_ask_dollar_cap_is_per_strategy():
    """The $ cap left the shared block: $0.50 for the three premium sellers, off (0) for
    the seven that buy deep / long-dated options; the shared % of price rule stays."""
    assert "max_leg_spread" not in R.SCHEMA["shared"] and "max_leg_spread_pct" in R.SCHEMA["shared"]
    for s in R.STRATEGIES:
        f = R.SCHEMA[s]["max_leg_spread"]
        assert f.default == SPREAD_DEFAULTS[s], s
        assert (f.kind, f.lo, f.unit, f.label) == ("num", 0, "$", "Widest bid/ask per option")
        assert "0 turns this check off" in f.help and "% of price rule" in f.help
        assert R.defaults(s)["max_leg_spread"] == SPREAD_DEFAULTS[s]
        assert R.step("max_leg_spread", f) == 0.05
    assert R.SPREAD_FROM_SHARED == ("bull_put", "bear_call", "iron_condor")
    assert "house band for selling premium" in R.SCHEMA["bull_put"]["max_leg_spread"].help
    assert "Off by default" in R.SCHEMA["leaps_call"]["max_leg_spread"].help
    # 0 is a valid stored value (off), not clamped up to a floor
    assert R.for_strategy({"bull_put": {"max_leg_spread": 0}}, "bull_put")["rules"]["max_leg_spread"] == 0
    assert R.parse("0", R.SCHEMA["bull_put"]["max_leg_spread"]) == (0.0, None)


def test_fields_and_for_strategy_shape():
    rows = R.fields("iron_condor")
    assert rows[0][0] == "shared" and rows[-1][0] == "iron_condor"
    assert [n for b, n, _ in rows if b == "shared"] == list(R.SCHEMA["shared"])
    fs = R.for_strategy(R.house(), "bear_call")
    assert set(fs) == {"strategy", "shared", "rules"} and fs["strategy"] == "bear_call"
    assert fs["rules"] == DESIGN_DEFAULTS["bear_call"]
    # a partial dict: missing fields take their defaults, out-of-bounds stored values are clamped
    fs2 = R.for_strategy({"shared": {"oi_min": 5}, "bear_call": {"credit_pct_min": 9999}}, "bear_call")
    assert fs2["shared"]["oi_min"] == 5 and fs2["shared"]["price_min"] == 20
    assert fs2["rules"]["credit_pct_min"] == 300 and fs2["rules"]["dte_lo"] == 30
    with pytest.raises(KeyError):
        R.for_strategy({}, "nope")


def test_parse_form_strings_per_kind():
    f = R.SCHEMA["bull_put"]["short_delta_lo"]
    assert R.parse("0.25", f) == (0.25, None)
    assert R.parse("", f)[0] is R._SKIP and R.parse("", f)[1] is None
    v, err = R.parse("abc", f)
    assert v is R._SKIP and "must be a number" in err
    v, err = R.parse("1.5", f)
    assert v == 0.99 and "set to 0.99" in err
    b = R.SCHEMA["shared"]["monthly_only"]
    assert R.parse("on", b) == (True, None) and R.parse("true", b) == (True, None)
    assert R.parse("", b) == (False, None) and R.parse("off", b) == (False, None)
    assert R.parse("maybe", b)[0] is R._SKIP
    c = R.SCHEMA["bull_put"]["earnings_rule"]
    assert R.parse("allow", c) == ("allow", None) and R.parse("short_leg", c) == ("short_leg", None)
    v, err = R.parse("defined_risk_only", c, name="earnings_rule")
    assert v is R._SKIP and "No earnings before the last expiry" in err and "Earnings allowed" in err
    i = R.SCHEMA["shared"]["oi_min"]
    assert R.parse("250.4", i) == (250, None) and R.parse("1,500", i) == (1500, None)


# ------------------------------------------------------------------ storage
def _row(db, user):
    return db.query(models.UserOptionPrefs).filter_by(user_id=user.id).one_or_none()


def test_read_without_a_row_is_the_house(db, user):
    assert R.read(db, user) == R.house()
    assert _row(db, user) is None


def test_write_is_partial_and_sparse(db, user):
    prefs, errors = R.write(db, user, "bull_put", {"short_delta_lo": "0.22", "monthly_only": "on",
                                                    "csrf": "x", "strategy": "bull_put"})
    assert errors == []
    assert prefs["bull_put"]["short_delta_lo"] == 0.22 and prefs["shared"]["monthly_only"] is True
    assert prefs["bull_put"]["short_delta_hi"] == 0.30          # not posted, unchanged
    assert prefs["bear_call"]["short_delta_lo"] == 0.20         # another strategy untouched
    row = _row(db, user)
    assert row.schema_version == 2 and len(row.prefs_hash) == 12
    assert row.prefs == {"schema": 2, "shared": {"monthly_only": True}, "bull_put": {"short_delta_lo": 0.22}}
    # a second partial write keeps the first; posting a default drops the override
    prefs, errors = R.write(db, user, "bull_put", {"dte_hi": "45", "short_delta_lo": "0.20"})
    assert errors == [] and prefs["bull_put"]["dte_hi"] == 45 and prefs["bull_put"]["short_delta_lo"] == 0.20
    assert _row(db, user).prefs == {"schema": 2, "shared": {"monthly_only": True}, "bull_put": {"dte_hi": 45}}
    assert R.read(db, user) == prefs
    # an unchecked box posted as "" turns the bool off
    prefs, _ = R.write(db, user, "bull_put", {"monthly_only": ""})
    assert prefs["shared"]["monthly_only"] is False and "shared" not in _row(db, user).prefs


def test_write_prefixed_keys_and_strategy_scope(db, user):
    prefs, errors = R.write(db, user, "calendar", {"shared.oi_min": "300", "calendar__front_iv_ge_back": "true",
                                                   "bull_put.dte_lo": "10", "dte_lo": "10"})
    assert errors == []
    assert prefs["shared"]["oi_min"] == 300 and prefs["calendar"]["front_iv_ge_back"] is True
    assert prefs["bull_put"]["dte_lo"] == 30          # another strategy's key is ignored on a calendar write


def test_write_clamps_and_reports(db, user):
    prefs, errors = R.write(db, user, "iron_condor", {"wing_atr_hi": "50", "credit_pct_min": "-3",
                                                      "dte_lo": "soon"})
    assert prefs["iron_condor"]["wing_atr_hi"] == 10.0 and prefs["iron_condor"]["credit_pct_min"] == 1.0
    assert prefs["iron_condor"]["dte_lo"] == 30        # unreadable: not stored
    assert len(errors) == 3
    assert any("Wing width, up to" in e and "set to 10" in e for e in errors)
    assert any("Credit at least" in e and "set to 1" in e for e in errors)
    assert any("must be a number" in e for e in errors)
    assert _row(db, user).prefs["iron_condor"] == {"wing_atr_hi": 10.0, "credit_pct_min": 1.0}


def test_write_reports_a_from_above_its_to(db, user):
    prefs, errors = R.write(db, user, "buy_call", {"delta_lo": "0.80"})
    assert prefs["buy_call"]["delta_lo"] == 0.80                 # stored as typed
    assert errors and "Delta, from (0.8) is above Delta, up to (0.7)" in errors[0]
    _, errors = R.write(db, user, "buy_call", {"delta_hi": "0.9"})
    assert errors == []


def test_write_unknown_strategy(db, user):
    prefs, errors = R.write(db, user, "straddle", {"dte_lo": "10"})
    assert errors == ["Unknown strategy 'straddle'."] and prefs == R.house()


def test_reset_clears_the_shown_blocks_only(db, user):
    R.write(db, user, "bull_put", {"oi_min": "500", "dte_lo": "21"})
    R.write(db, user, "bear_call", {"dte_lo": "35"})
    prefs = R.reset(db, user, "bull_put")
    assert prefs["bull_put"] == DESIGN_DEFAULTS["bull_put"] and prefs["shared"]["oi_min"] == 100
    assert prefs["bear_call"]["dte_lo"] == 35
    assert _row(db, user).prefs == {"schema": 2, "bear_call": {"dte_lo": 35}}
    R.write(db, user, "bull_put", {"oi_min": "500"})
    prefs = R.reset(db, user, "bear_call", shared=False)
    assert prefs["shared"]["oi_min"] == 500 and prefs["bear_call"]["dte_lo"] == 30
    assert R.reset(db, user, "straddle")["shared"]["oi_min"] == 500      # unknown: nothing cleared
    assert R.reset(db, user, "all") == R.house()


V1_ROW = {
    "shared": {"min_oi": 300, "max_leg_spread": 0.75, "earnings_rule": "defined_risk_only", "monthly_only": True,
               "gap_mult": 3.0, "chart_constraint": False},
    "credit_vertical": {"short_delta_hi": 0.35, "iv_gate_min": 40, "long_offset_max": 4, "dte_hi": 50},
    "long": {"dte_lo": 45, "dte_hi": 90, "premium_stop_pct": 60},
    "leaps": {"extrinsic_pct_max": 15, "months_hi": 24, "roll_dte": 120},
    "condor": {"wing_atr_hi": 2.0},
    "time": {"cal_front_lo": 15, "cal_delta_tol": 0.08, "diag_long_dte_hi": 400, "diag_short_delta_lo": 0.25},
    "telegram": {"enabled": True, "chat_id": "123"},
}


def test_migrate_v1_pure():
    m = R.migrate_v1(V1_ROW)
    assert m["shared"] == {"oi_min": 300, "monthly_only": True}
    # v1 "defined_risk_only" = reports allowed inside the defined-risk spreads only; the
    # shared $0.75 bid/ask cap goes to the three premium sellers only
    assert m["bull_put"] == m["bear_call"] == {"short_delta_hi": 0.35, "iv_rank_min": 40, "dte_hi": 50,
                                               "earnings_rule": "allow", "max_leg_spread": 0.75}
    assert m["bull_call"] == m["bear_put"] == {"earnings_rule": "allow"}
    assert m["buy_call"] == m["buy_put"] == {"dte_lo": 45, "dte_hi": 90}      # none_inside = the default
    assert m["leaps_call"] == {"months_hi": 24}                 # time value % changed meaning: dropped
    assert m["iron_condor"] == {"wing_atr_hi": 2.0, "earnings_rule": "allow", "max_leg_spread": 0.75}
    assert m["calendar"] == {"front_dte_lo": 15, "delta_tol": 0.08}             # keeps its own default
    assert m["diagonal_call"] == {"long_dte_hi": 400, "short_delta_lo": 0.25}
    assert "telegram" not in m
    assert R.migrate_v1(None) == {} and R.migrate_v1({"telegram": {}}) == {}
    # a v1 none_inside is every one-expiry strategy's default: nothing to keep
    assert R.migrate_v1({"shared": {"earnings_rule": "none_inside", "min_oi": 50}}) == {"shared": {"oi_min": 50}}


def test_v1_row_migrates_on_read_and_is_rewritten_on_write(db, user):
    db.add(models.UserOptionPrefs(user_id=user.id, prefs=V1_ROW, prefs_hash="v1hash", schema_version=1))
    db.commit()
    prefs = R.read(db, user)
    assert prefs["schema"] == 2
    assert prefs["shared"]["oi_min"] == 300 and "earnings_rule" not in prefs["shared"]
    assert {s: prefs[s]["earnings_rule"] for s in R.STRATEGIES} == {
        **EARNINGS_DEFAULTS, "bull_call": "allow", "bear_put": "allow", "bull_put": "allow", "bear_call": "allow",
        "iron_condor": "allow"}
    assert prefs["bull_put"]["iv_rank_min"] == 40 and prefs["bear_call"]["short_delta_hi"] == 0.35
    assert prefs["calendar"]["front_dte_lo"] == 15 and prefs["leaps_call"]["extrinsic_pct_max"] == 25
    assert "max_leg_spread" not in prefs["shared"]
    assert {s: prefs[s]["max_leg_spread"] for s in R.STRATEGIES} == {
        **SPREAD_DEFAULTS, "bull_put": 0.75, "bear_call": 0.75, "iron_condor": 0.75}
    assert _row(db, user).prefs == V1_ROW                         # reading never writes
    fs = R.for_strategy(V1_ROW, "bull_put")                       # a raw v1 dict reads the same
    assert fs["rules"]["iv_rank_min"] == 40 and fs["shared"]["oi_min"] == 300
    assert fs["rules"]["earnings_rule"] == "allow" and "earnings_rule" not in fs["shared"]
    prefs2, errors = R.write(db, user, "bull_put", {"dte_lo": "35"})
    assert errors == []
    row = _row(db, user)
    assert row.schema_version == 2 and row.prefs["schema"] == 2 and "telegram" not in row.prefs
    assert "credit_vertical" not in row.prefs and row.prefs["bull_put"]["dte_lo"] == 35
    assert row.prefs["calendar"] == {"front_dte_lo": 15, "delta_tol": 0.08}
    assert "earnings_rule" not in row.prefs["shared"] and row.prefs["iron_condor"]["earnings_rule"] == "allow"
    assert "max_leg_spread" not in row.prefs["shared"] and row.prefs["iron_condor"]["max_leg_spread"] == 0.75
    assert "max_leg_spread" not in (row.prefs.get("leaps_call") or {})
    assert prefs2 == R.read(db, user) and prefs2["bull_put"]["iv_rank_min"] == 40


def test_v2_row_with_a_shared_earnings_rule_is_migrated(db, user):
    """A v2 row saved before the rule moved into each strategy: the shared value goes
    to the seven none_inside-default strategies (a strategy's own value wins), LEAPS /
    diagonal / calendar keep their defaults, and the shared key is dropped on save."""
    stored = {"schema": 2, "shared": {"earnings_rule": "allow", "oi_min": 250},
              "bear_call": {"earnings_rule": "none_inside", "dte_lo": 40}}
    db.add(models.UserOptionPrefs(user_id=user.id, schema_version=2, prefs_hash="old", prefs=stored))
    db.commit()
    prefs = R.read(db, user)
    assert "earnings_rule" not in prefs["shared"] and prefs["shared"]["oi_min"] == 250
    got = {s: prefs[s]["earnings_rule"] for s in R.STRATEGIES}
    assert got == {"buy_call": "allow", "buy_put": "allow", "bull_call": "allow", "bear_put": "allow",
                   "leaps_call": "allow", "diagonal_call": "short_leg", "bull_put": "allow",
                   "bear_call": "none_inside", "iron_condor": "allow", "calendar": "short_leg"}
    assert prefs["bear_call"]["dte_lo"] == 40
    assert _row(db, user).prefs == stored                         # reading never writes
    # the first save rewrites the row without the shared key (schema stays 2)
    prefs2, errors = R.write(db, user, "calendar", {"earnings_rule": "none_inside"})
    assert errors == [] and prefs2["calendar"]["earnings_rule"] == "none_inside"
    row = _row(db, user)
    assert row.prefs["schema"] == 2 and row.schema_version == 2
    assert row.prefs["shared"] == {"oi_min": 250}
    assert row.prefs["bull_put"] == {"earnings_rule": "allow"} and row.prefs["calendar"] == {"earnings_rule": "none_inside"}
    assert row.prefs["bear_call"] == {"dte_lo": 40}               # its own none_inside = the default
    assert R.read(db, user) == prefs2
    # an unknown shared value falls back to each strategy's default
    assert R.clean({"schema": 2, "shared": {"earnings_rule": "sometimes"}})["bull_put"]["earnings_rule"] == "none_inside"


def test_v2_row_with_a_shared_dollar_cap_is_migrated(db, user):
    """A v2 row saved while the bid/ask $ cap was shared: the value goes to the three
    premium-selling strategies only (a strategy's own value wins); the seven buying
    strategies keep the cap off; the shared key is dropped on the first save."""
    stored = {"schema": 2, "shared": {"max_leg_spread": 0.30, "oi_min": 250},
              "bear_call": {"max_leg_spread": 0.80}, "leaps_call": {"months_hi": 24}}
    db.add(models.UserOptionPrefs(user_id=user.id, schema_version=2, prefs_hash="old", prefs=stored))
    db.commit()
    prefs = R.read(db, user)
    assert "max_leg_spread" not in prefs["shared"] and prefs["shared"]["oi_min"] == 250
    assert {s: prefs[s]["max_leg_spread"] for s in R.STRATEGIES} == {
        **SPREAD_DEFAULTS, "bull_put": 0.30, "bear_call": 0.80, "iron_condor": 0.30}
    assert _row(db, user).prefs == stored                         # reading never writes
    # a bare dict (the screener's input) reads the same
    assert R.for_strategy({"shared": {"max_leg_spread": 0.30}}, "iron_condor")["rules"]["max_leg_spread"] == 0.30
    assert R.for_strategy({"shared": {"max_leg_spread": 0.30}}, "leaps_call")["rules"]["max_leg_spread"] == 0
    prefs2, errors = R.write(db, user, "leaps_call", {"months_lo": "10"})
    assert errors == []
    row = _row(db, user)
    assert row.prefs["shared"] == {"oi_min": 250}
    assert row.prefs["bull_put"] == {"max_leg_spread": 0.30} and row.prefs["iron_condor"] == {"max_leg_spread": 0.30}
    assert row.prefs["bear_call"] == {"max_leg_spread": 0.80}
    assert row.prefs["leaps_call"] == {"months_hi": 24, "months_lo": 10}
    assert R.read(db, user) == prefs2
    # a page from before the move posting "shared.max_leg_spread" sets the edited strategy's own cap
    assert R._resolve("shared.max_leg_spread", "leaps_call") == ("leaps_call", "max_leg_spread")
    prefs3, errors = R.write(db, user, "leaps_call", {"shared.max_leg_spread": "1.50"})
    assert errors == [] and prefs3["leaps_call"]["max_leg_spread"] == 1.5 and prefs3["bull_put"]["max_leg_spread"] == 0.30


def test_earnings_rule_writes_and_old_style_keys():
    """for_strategy on a bare dict still honours an old shared earnings rule; a posted
    'shared.earnings_rule' (a page from before the move) lands on the strategy."""
    fs = R.for_strategy({"shared": {"earnings_rule": "allow"}}, "bull_put")
    assert fs["rules"]["earnings_rule"] == "allow" and "earnings_rule" not in fs["shared"]
    assert R.for_strategy({"shared": {"earnings_rule": "allow"}}, "calendar")["rules"]["earnings_rule"] == "short_leg"
    assert R.for_strategy({"shared": {"earnings_rule": "allow"}, "bull_put": {"earnings_rule": "short_leg"}},
                          "bull_put")["rules"]["earnings_rule"] == "short_leg"
    assert R._resolve("shared.earnings_rule", "diagonal_call") == ("diagonal_call", "earnings_rule")
    assert R._resolve("earnings_rule", "bull_put") == ("bull_put", "earnings_rule")
    assert R._resolve("shared.nope", "bull_put") == (None, None)


def test_earnings_rule_write_per_strategy(db, user):
    prefs, errors = R.write(db, user, "diagonal_call", {"shared.earnings_rule": "allow"})
    assert errors == [] and prefs["diagonal_call"]["earnings_rule"] == "allow"
    assert prefs["calendar"]["earnings_rule"] == "short_leg"          # other strategies untouched
    prefs, errors = R.write(db, user, "bull_put", {"bull_put.earnings_rule": "short_leg"})
    assert errors == [] and prefs["bull_put"]["earnings_rule"] == "short_leg"
    _, errors = R.write(db, user, "bull_put", {"earnings_rule": "sometimes"})
    assert len(errors) == 1 and "choose one of" in errors[0]
    assert _row(db, user).prefs == {"schema": 2, "diagonal_call": {"earnings_rule": "allow"},
                                    "bull_put": {"earnings_rule": "short_leg"}}
    assert R.reset(db, user, "bull_put")["bull_put"]["earnings_rule"] == "none_inside"
    assert _row(db, user).prefs == {"schema": 2, "diagonal_call": {"earnings_rule": "allow"}}


def test_v2_row_with_junk_is_cleaned_on_read(db, user):
    db.add(models.UserOptionPrefs(user_id=user.id, schema_version=2, prefs_hash="x",
                                  prefs={"schema": 2, "shared": {"oi_min": "abc", "per_ticker": 99, "zzz": 1},
                                         "bull_put": {"dte_lo": 0}, "nonsense": {"a": 1}}))
    db.commit()
    p = R.read(db, user)
    assert p["shared"]["oi_min"] == 100 and p["shared"]["per_ticker"] == 50 and "zzz" not in p["shared"]
    assert p["bull_put"]["dte_lo"] == 1 and "nonsense" not in p


def test_prefs_hash_tracks_the_rules():
    a = R.prefs_hash(R.house())
    b = R.prefs_hash(R.clean({"schema": 2, "bull_put": {"dte_lo": 31}}))
    assert a != b and a == R.prefs_hash(R.clean({"schema": 2}))


# ------------------------------------------------------------------ payoff reads families from opt_rules
def test_payoff_takes_labels_and_families_from_opt_rules():
    src = inspect.getsource(payoff)
    assert "import option_prefs" not in src and "from .option_prefs" not in src
    assert "from .strategy_rules" not in src and "import strategy_rules" not in src
    assert payoff._family_of("buy_put") == "long" and payoff._family_of("leaps_call") == "leaps"
    assert payoff._family_of("diagonal_call") == "time" and payoff._family_of("calendar") == "time"
    assert payoff._family_of("bull_put") == "credit_vertical" and payoff._family_of("iron_condor") == "condor"
    assert payoff._family_of(None) is None
    with pytest.raises(KeyError):
        payoff._family_of("straddle")
    assert payoff._strategy_label("leaps_call") == "Buy LEAPS"
    assert payoff._house_premium_stop_pct("diagonal_call") == 40.0
    assert payoff._house_premium_stop_pct("bull_call") == 50.0


# ------------------------------------------------------------------ the data plan (Massive, §13.5)
@pytest.mark.parametrize("value, want", [
    (None, False), ("0", False), ("", False), ("no", False), ("off", False), ("false", False), ("junk", False),
    ("1", True), ("true", True), ("Yes", True), (" on ", True),
])
def test_quotes_available_reads_the_env(monkeypatch, value, want):
    if value is None:
        monkeypatch.delenv("TST_MASSIVE_QUOTES", raising=False)
    else:
        monkeypatch.setenv("TST_MASSIVE_QUOTES", value)
    assert R.quotes_available() is want
    assert R.QUOTES_ENV == "TST_MASSIVE_QUOTES"


def test_bid_ask_fields_unused_without_quotes_keep_their_schema():
    assert set(R.UNUSED_WITHOUT_QUOTES) == {"max_leg_spread", "max_leg_spread_pct"}
    assert set(R.UNUSED_WITHOUT_QUOTES.values()) == {R.QUOTES_NOTE}
    assert R.QUOTES_NOTE == "not used - the current data plan (Massive Starter) has no bid/ask"
    # no schema change: the % rule stays shared, the $ cap per strategy, defaults as before
    assert R.SCHEMA_VERSION == 2
    assert R.SCHEMA["shared"]["max_leg_spread_pct"].default == 25
    for s in R.STRATEGIES:
        assert "max_leg_spread" in R.SCHEMA[s] and "max_leg_spread" not in R.SCHEMA["shared"]
        names = {name for _b, name, _f in R.fields(s)}
        assert set(R.UNUSED_WITHOUT_QUOTES) <= names            # the panel can grey each one out
    # the help texts no longer name the old data sources
    for block in R.SCHEMA.values():
        for f in block.values():
            assert "IBKR" not in f.help and "Hermes" not in f.help and "connector" not in f.help
