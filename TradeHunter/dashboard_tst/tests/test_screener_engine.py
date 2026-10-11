"""The Options Screener engine (OPTIONS_SCREENER_DESIGN.md §5): frame, fields, screens, the
API (screens / spec / run / csv), filters, sorting, paging, views, the DB loader and a
performance smoke test.

The market is synthetic: five underlyings whose chains are priced with Black-Scholes from a
known IV smile, so every derived figure can be checked by hand.
"""
from __future__ import annotations

import csv as _csv
import datetime as dt
import io
import json
import math
import os
import time

import numpy as np
import pytest

from app.services.opt_constants import RISK_FREE
from app.services.screener import engine, fields, frame as frame_mod, screens as screens_mod, strategies
from app.services.screener.frame import Frame

TODAY = dt.date(2026, 10, 12)            # a Monday
AS_OF = dt.datetime(2026, 10, 9, 20, 0)  # naive UTC
MONTHLY = {dt.date(2026, 10, 16), dt.date(2026, 11, 20), dt.date(2026, 12, 18), dt.date(2027, 1, 15),
           dt.date(2027, 2, 19), dt.date(2027, 4, 16)}
EXP_DAYS = (4, 11, 18, 25, 39, 67, 95, 130, 186)

UNDERLYINGS = (
    dict(symbol="AAA", spot=100.0, step=2.5, iv=0.30, sec_type="stock", exchange="NYSE", trend="up",
         hv20=25.0, iv_rank=40.0, iv_pct=45.0, earnings=25),
    dict(symbol="BBB", spot=52.0, step=1.0, iv=0.45, sec_type="stock", exchange="NASDAQ", trend="down",
         hv20=50.0, iv_rank=70.0, iv_pct=80.0, earnings=None),
    dict(symbol="CCC", spot=410.0, step=5.0, iv=0.18, sec_type="etf", exchange="AMEX", trend="sideways",
         hv20=15.0, iv_rank=20.0, iv_pct=15.0, earnings=None),
    dict(symbol="DDD", spot=25.0, step=0.5, iv=0.60, sec_type="stock", exchange="NASDAQ", trend=None,
         hv20=55.0, iv_rank=None, iv_pct=None, earnings=60),
    dict(symbol="IDX", spot=4000.0, step=25.0, iv=0.16, sec_type="index", exchange="INDEX", trend="up",
         hv20=14.0, iv_rank=35.0, iv_pct=30.0, earnings=None),
)


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S, K, T, sig, put):
    """price, delta, gamma, theta (per day), vega (per 1 vol point)."""
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (RISK_FREE + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    disc = math.exp(-RISK_FREE * T)
    if put:
        price = K * disc * ncdf(-d2) - S * ncdf(-d1)
        delta = ncdf(d1) - 1.0
        theta = (-S * pdf * sig / (2 * math.sqrt(T)) + RISK_FREE * K * disc * ncdf(-d2)) / 365.0
    else:
        price = S * ncdf(d1) - K * disc * ncdf(d2)
        delta = ncdf(d1)
        theta = (-S * pdf * sig / (2 * math.sqrt(T)) - RISK_FREE * K * disc * ncdf(d2)) / 365.0
    return price, delta, pdf / (S * sq), theta, S * pdf * math.sqrt(T) / 100.0


def p_above(S, X, sig, T):
    """The engine's P(S_T > X), computed with math.erf."""
    if X <= 0:
        return 1.0
    d2 = (math.log(S / X) + (RISK_FREE - 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return ncdf(d2)


def smile(base, S, K, dte):
    x = math.log(K / S)
    return base + 0.25 * x * x - 0.06 * x + 0.02 * math.exp(-dte / 30.0)


def chain(u: dict, today=TODAY, seed=7, exp_days=EXP_DAYS) -> list[dict]:
    rng = np.random.default_rng(seed + sum(map(ord, u["symbol"])))
    S, step = u["spot"], u["step"]
    lo = math.ceil(S * 0.7 / step) * step
    strikes = []
    k = lo
    while k <= S * 1.3 + 1e-9:
        strikes.append(round(k, 4))
        k += step
    out = []
    for dd in exp_days:
        exp = today + dt.timedelta(days=dd)
        T = max(dd, 0.5) / 365.0
        for put in (False, True):
            for K in strikes:
                sig = smile(u["iv"], S, K, dd)
                price, delta, gamma, theta, vega = bs(S, K, T, sig, put)
                vol = int(rng.integers(0, 3000))
                oi = int(rng.integers(50, 9000))
                out.append(dict(
                    symbol=u["symbol"], expiry=exp.isoformat(), right="P" if put else "C", strike=K,
                    weekly=exp not in MONTHLY, price=round(max(price, 0.0), 2), last=round(max(price, 0.0) * 1.01, 2),
                    chg_pct=float(rng.normal(0, 10)), volume=vol, oi=oi,
                    vol_prev=int(rng.integers(1, 3000)), oi_prev=int(rng.integers(1, 9000)),
                    iv=sig, delta=delta, gamma=gamma, theta=theta, vega=vega,
                    last_trade=AS_OF - dt.timedelta(minutes=int(rng.integers(1, 60 * 30))),
                    session="2026-10-09", as_of=AS_OF))
    return out


def underlying_row(u: dict, today=TODAY) -> dict:
    S = u["spot"]
    return dict(
        symbol=u["symbol"], name=u["symbol"] + " Corp", sec_type=u["sec_type"], exchange=u["exchange"],
        spot=S, prev_close=S / 1.01, chg_pct=1.0, stock_volume=2e6, avg_vol20=1.8e6, avg_vol50=1.6e6,
        sma20=S * 0.98, sma50=S * 0.95, sma200=S * 0.90, rsi14=55.0, atr14=S * 0.02, atr_pct=2.0,
        hv20=u["hv20"], hv60=u["hv20"] * 1.1, hi52=S * 1.2, lo52=S * 0.7, perf5=1.5, perf20=4.0,
        trend=u["trend"], iv30=u["iv"] * 100, iv30_prev=u["iv"] * 100 - 1.0, iv_rank=u["iv_rank"],
        iv_pct=u["iv_pct"], exp_move30=u["iv"] * math.sqrt(30 / 365) * 100,
        earnings_date=(today + dt.timedelta(days=u["earnings"])).isoformat() if u["earnings"] else None,
        history_done=u["iv_rank"] is not None)


def make_market(today=TODAY, unds=UNDERLYINGS, meta=None) -> Frame:
    contracts = [r for u in unds for r in chain(u, today)]
    return Frame.from_records(contracts, [underlying_row(u, today) for u in unds],
                              meta=meta or {"pass_id": 7}, today=today)


@pytest.fixture(scope="module")
def market() -> Frame:
    return make_market()


@pytest.fixture(autouse=True)
def pinned(market):
    frame_mod.set_current(market)
    yield market
    frame_mod.reset()


def _contract(fr: Frame, sym, exp_days, right, strike) -> int:
    c = fr.c
    exp = (TODAY - frame_mod.EPOCH).days + exp_days
    hit = np.flatnonzero((c["sym"] == fr.sym_index[sym]) & (c["exp"] == exp)
                         & (c["is_put"] == (1 if right == "P" else 0)) & np.isclose(c["strike"], strike))
    assert len(hit) == 1
    return int(hit[0])


# ─────────────────────────────────── the frame ───────────────────────────────────

def test_frame_sorted_and_grouped(market):
    c = market.c
    key = np.lexsort((c["strike"], c["exp"], c["is_put"], c["sym"]))
    assert np.array_equal(key, np.arange(market.n))
    assert np.all(np.diff(c["ck"]) > 0)                      # (group, strike) keys strictly ascending
    calls = c["is_put"] == 0
    assert np.all(np.diff(c["sk"][calls]) > 0) and np.all(np.diff(c["sk"][~calls]) > 0)
    assert market.symbols == ("AAA", "BBB", "CCC", "DDD", "IDX")
    assert market.meta["n_underlyings"] == 5 and market.meta["n_contracts"] == market.n
    assert market.meta["iv_history_done"] == 4 and market.meta["pass_id"] == 7
    with pytest.raises(ValueError):
        c["strike"][0] = 1.0                                  # immutable


def test_frame_derived_fields(market):
    c = market.c
    i = _contract(market, "AAA", 39, "C", 105.0)
    S, K, p, sig = 100.0, 105.0, c["price"][i], c["iv"][i]
    T = 39 / 365.0
    assert c["dte"][i] == 39
    assert c["moneyness"][i] == pytest.approx((S - K) / S * 100)
    assert c["intrinsic"][i] == 0 and c["tp"][i] == pytest.approx(p)
    assert c["tp_pct"][i] == pytest.approx(p / S * 100)
    assert c["breakeven"][i] == pytest.approx(K + p)
    assert c["breakeven_pct"][i] == pytest.approx((K + p - S) / S * 100)
    assert c["itm_prob"][i] == pytest.approx(100 * p_above(S, K, sig, T), abs=1e-4)
    assert c["otm_prob"][i] == pytest.approx(100 - c["itm_prob"][i])
    assert c["profit_prob"][i] == pytest.approx(100 * p_above(S, K + p, sig, T), abs=1e-4)
    assert c["vol_oi"][i] == pytest.approx(c["volume"][i] / c["oi"][i])
    assert c["oi_chg"][i] == c["oi"][i] - c["oi_prev"][i]
    assert c["premium"][i] == pytest.approx(p * 100 * c["volume"][i])
    assert c["exp_move"][i] == pytest.approx(S * sig * math.sqrt(T))
    assert c["iv_hv"][i] == pytest.approx(sig * 100 / 25.0)
    assert c["earn_before"][i] == 1.0 and c["exp_before_earn"][i] == 0.0     # earnings in 25 d < 39 d
    j = _contract(market, "AAA", 18, "P", 90.0)
    assert c["moneyness"][j] == pytest.approx((90 - 100) / 100 * 100)
    assert c["breakeven"][j] == pytest.approx(90 - c["price"][j])
    assert c["profit_prob"][j] == pytest.approx(
        100 * (1 - p_above(S, 90 - c["price"][j], c["iv"][j], 18 / 365)), abs=1e-4)
    assert c["earn_before"][j] == 0.0
    assert market.u["pct_sma20"][0] == pytest.approx((1 / 0.98 - 1) * 100)


def test_ncdf_matches_erf():
    xs = np.linspace(-8, 8, 2001)
    exact = np.array([ncdf(x) for x in xs])
    assert np.max(np.abs(frame_mod.ncdf(xs) - exact)) < 2e-7
    assert np.isnan(frame_mod.ncdf(np.array([np.nan]))[0])


def test_expired_contracts_dropped():
    u = UNDERLYINGS[0]
    rows = chain(u, exp_days=(4, 11))
    old = dict(rows[0], expiry=(TODAY - dt.timedelta(days=3)).isoformat())
    fr = Frame.from_records(rows + [old], [underlying_row(u)], today=TODAY)
    assert fr.n == len(rows) and fr.meta["n_expired_dropped"] == 1


def test_empty_frame_run_and_spec():
    frame_mod.set_current(Frame.empty_frame(frame_mod.NO_DATA, TODAY))
    out = engine.run("bull-put-spread", {})
    assert out["total"] == 0 and out["rows"] == [] and out["warnings"]
    assert out["data"]["empty"] is True
    sp = engine.spec("options-screener")
    assert sp["data"]["empty"] is True and sp["filters"]
    assert engine.csv("options-screener", {}).splitlines()[0].startswith("Symbol")


def _untyped_market() -> Frame:
    """The test market before the identity job ran: no underlying has a security type."""
    return make_market(unds=tuple(dict(u, sec_type=None) for u in UNDERLYINGS))


@pytest.mark.parametrize("key", ["options-screener", "bull-put-spread"])
def test_untyped_market_skips_the_default_security_type_filter(key):
    """v4.137 (design §11.17): while NO type is in, the screens' default stock + ETF card is
    skipped with a warning - day one is not an empty table."""
    typed_total = engine.run(key, None)["total"]                 # the pinned, typed market
    fr = _untyped_market()
    assert fr.meta["n_sec_type_unknown"] == fr.meta["n_underlyings"] == 5
    frame_mod.set_current(fr)
    out = engine.run(key, None)
    assert out["total"] > 0 and out["rows"]
    assert engine.SEC_TYPES_LOADING in out["warnings"]
    assert engine.SEC_TYPES_LOADING == ("Security types are still loading - the Security Type filter is "
                                        "skipped until they are in, so index underlyings may appear.")
    assert not any("left out by the Security Type filter" in w for w in out["warnings"])
    assert out["data"]["n_sec_type_unknown"] == 5 and out["data"]["reloading"] is False
    if key == "options-screener":                               # index underlyings may appear
        assert out["total"] > typed_total
        idx = engine.run(key, {"filters": [*screens_mod.SCREENS[key].defaults,
                                           {"f": "symbol", "op": "in", "v": ["IDX"]}]})
        assert idx["total"] > 0 and {r["symbol"] for r in idx["rows"]} == {"IDX"}


def test_untyped_market_never_widens_a_members_own_type_choice():
    frame_mod.set_current(_untyped_market())
    out = engine.run("options-screener", {"filters": [{"f": "sec_type", "op": "in", "v": ["etf"]}]})
    assert out["total"] == 0
    assert engine.SEC_TYPES_LOADING not in out["warnings"]
    assert any("left out by the Security Type filter" in w for w in out["warnings"])


def test_partly_typed_market_keeps_the_default_filter():
    unds = tuple(dict(u, sec_type=None) if u["symbol"] == "DDD" else u for u in UNDERLYINGS)
    frame_mod.set_current(make_market(unds=unds))
    out = engine.run("options-screener", None, per_page=1000)
    assert engine.SEC_TYPES_LOADING not in out["warnings"]
    assert {r["symbol"] for r in out["rows"]} <= {"AAA", "BBB", "CCC"}
    assert any("1 underlyings have no security type yet" in w for w in out["warnings"])
    assert out["data"]["sec_type_active"] is True                # applied: the page may blame it


def test_sec_type_active_says_whether_the_filter_was_applied():
    """The page's "the Security Type filter leaves them out" (T-26) needs the filter to have
    been APPLIED - never when the engine skipped it (T-23) or dropped it as invalid."""
    frame_mod.set_current(_untyped_market())
    defaults = [*screens_mod.SCREENS["options-screener"].defaults]
    nothing = {"f": "symbol", "op": "in", "v": ["NOPE"]}
    out = engine.run("options-screener", {"filters": defaults + [nothing]})
    assert out["total"] == 0 and out["data"]["n_sec_type_unknown"] == 5
    assert engine.SEC_TYPES_LOADING in out["warnings"] and out["data"]["sec_type_active"] is False
    out = engine.run("options-screener", {"filters": [{"f": "sec_type", "op": "in", "v": ["etf"]}]})
    assert out["total"] == 0 and out["data"]["sec_type_active"] is True      # a member's own choice
    out = engine.run("options-screener", {"filters": [{"f": "sec_type", "op": "in", "v": ["nonsense"]}, nothing]})
    assert out["data"]["sec_type_active"] is False                           # dropped as invalid
    assert "sec_type_active" not in frame_mod.current().meta                 # a copy, never the shared meta
    assert engine.run("no-such-screen", {})["data"]["sec_type_active"] is False


def test_the_page_blames_the_security_type_filter_only_when_it_was_applied():
    from pathlib import Path  # noqa: PLC0415

    html = (Path(engine.__file__).resolve().parents[2] / "templates" / "options.html").read_text(encoding="utf-8")
    body = html.split("function secTypeBlocked(R)", 1)[1].split("\n  }", 1)[0]
    assert "R.data.sec_type_active" in body and "S.filters" not in body


def test_data_carries_reloading_as_a_copy(market):
    out = engine.run("long-call", {"filters": []})
    sp = engine.spec("long-call")
    assert out["data"]["reloading"] is False and sp["data"]["reloading"] is False
    assert "reloading" not in market.meta


# ─────────────────────────────────── screens / spec ───────────────────────────────────

ALL_KEYS = list(screens_mod.ORDER)


def test_screens_list():
    fams = engine.screens()
    assert [f["family"] for f in fams] == list(screens_mod.FAMILIES)
    keys = [s["key"] for f in fams for s in f["screens"]]
    assert keys == ALL_KEYS and len(keys) == 33
    assert all(s["description"] and s["label"] for f in fams for s in f["screens"])


@pytest.mark.parametrize("key", ALL_KEYS)
def test_spec_every_screen(key):
    sp = engine.spec(key)
    assert sp["key"] == key and "error" not in sp
    json.dumps(sp, allow_nan=False)
    assert set(sp["views"]) == {"main", "filter", "greeks", "vol"}
    main = [c["key"] for c in sp["views"]["main"]]
    assert main[0] == "symbol" and len(main) == len(screens_mod.SCREENS[key].columns)
    groups = [g["group"] for g in sp["filters"]]
    assert set(groups) <= set(fields.GROUPS)
    keys = {f["key"] for g in sp["filters"] for f in g["fields"]}
    for d in sp["defaults"]["filters"]:
        assert d["f"] in keys, (key, d["f"])
    assert len(sp["cards"]) == len(sp["defaults"]["filters"]) and all(sp["cards"])
    assert not any("bid" in k or "ask" in k for k in keys)     # no bid / ask anywhere (§0)
    assert sp["legs"] and all(leg["action"] in ("buy", "sell") for leg in sp["legs"])
    if screens_mod.SCREENS[key].strategy:
        assert "leg1.volume" in keys and "leg2.oi" in keys
    exp_field = next(f for g in sp["filters"] for f in g["fields"]
                     if f["key"] in ("expiry", "leg1.expiry"))
    assert exp_field["choices"] and exp_field["choices"][0]["v"] == "2026-10-16"


def test_spec_unknown():
    sp = engine.spec("nope")
    assert sp["error"]


# ─────────────────────────────────── run: every screen with its defaults ───────────────────────────────────

@pytest.mark.parametrize("key", ALL_KEYS)
def test_run_defaults_every_screen(key):
    out = engine.run(key, None)
    assert out["warnings"] == [], out["warnings"]
    assert out["total"] > 0, key
    json.dumps(out, allow_nan=False)                           # plain JSON, no NaN / numpy types
    assert out["rows"], key
    cols = [c["key"] for c in out["columns"]]
    assert cols == [k for k, _ in screens_mod.SCREENS[key].columns]
    scr = screens_mod.SCREENS[key]
    for r in out["rows"]:
        assert set(cols) <= set(r) and r["symbol"] in ("AAA", "BBB", "CCC", "DDD", "IDX")
        assert len(r["legs"]) == len(scr.legs) + (1 if scr.stock else 0)
        assert r["id"].startswith(r["symbol"] + "|")
    # the default sort holds (missing values last)
    col, d = scr.sort
    vals = [r[col] for r in out["rows"] if r[col] is not None]
    if col != "symbol":
        assert vals == sorted(vals, reverse=(d == "desc"))
    assert out["sort"] == {"col": col, "dir": d}
    assert out["ms"] >= 0 and out["data"]["n_contracts"] > 0


def test_options_screener_default_filters_hold():
    out = engine.run("options-screener", None, per_page=1000)
    assert out["total"] == len(out["rows"]) > 50
    for r in out["rows"]:
        assert r["volume"] >= 500 and r["oi"] >= 100 and -25 <= r["moneyness"] <= 25
    syms = {r["symbol"] for r in out["rows"]}
    assert "IDX" not in syms                                  # security type stock + ETF only
    assert [r["symbol"] for r in out["rows"]] == sorted(r["symbol"] for r in out["rows"])
    assert all(r["option_type"] in ("Call", "Put") for r in out["rows"])


def test_long_call_only_calls_and_formulas(market):
    out = engine.run("long-call", {"filters": [{"f": "symbol", "op": "in", "v": ["AAA"]},
                                               {"f": "dte", "op": "eq", "v": 39},
                                               {"f": "strike", "op": "eq", "v": 105}]})
    assert out["total"] == 1
    r = out["rows"][0]
    i = _contract(market, "AAA", 39, "C", 105.0)
    p = market.c["price"][i]
    assert r["price"] == pytest.approx(p) and r["breakeven"] == pytest.approx(105 + p, abs=0.01)
    assert r["expiry"] == "2026-11-20" and r["legs"][0]["right"] == "C" and r["legs"][0]["action"] == "buy"
    assert r["last_trade"].endswith("Z")


# ─────────────────────────────────── filter ops ───────────────────────────────────

def _run(key, filters, **kw):
    return engine.run(key, {"filters": filters, **kw}, per_page=1000)


def _symset(filters, key="options-screener"):
    """The underlyings with at least one match (page-independent)."""
    return {u["symbol"] for u in UNDERLYINGS
            if _run(key, filters + [{"f": "symbol", "op": "in", "v": [u["symbol"]]}])["total"]}


def test_filter_ops():
    base = [{"f": "symbol", "op": "in", "v": ["AAA"]}]
    allc = _run("options-screener", base)["total"]
    assert allc == sum(1 for _ in chain(UNDERLYINGS[0]))
    gte = _run("options-screener", base + [{"f": "volume", "op": "gte", "lo": 1000}])
    assert all(r["volume"] >= 1000 for r in gte["rows"]) and 0 < gte["total"] < allc
    lte = _run("options-screener", base + [{"f": "volume", "op": "lte", "hi": 1000}])
    assert gte["total"] + lte["total"] >= allc
    btw = _run("options-screener", base + [{"f": "strike", "op": "between", "lo": 95, "hi": 105}])
    assert {r["strike"] for r in btw["rows"]} == {95.0, 97.5, 100.0, 102.5, 105.0}
    swapped = _run("options-screener", base + [{"f": "strike", "op": "between", "lo": 105, "hi": 95}])
    assert swapped["total"] == btw["total"]
    eq = _run("options-screener", base + [{"f": "strike", "op": "eq", "v": 100}])
    assert eq["total"] == 2 * len(EXP_DAYS)
    puts = _run("options-screener", base + [{"f": "option_type", "op": "in", "v": ["P"]}])
    assert {r["option_type"] for r in puts["rows"]} == {"Put"} and puts["total"] == allc // 2
    by_label = _run("options-screener", base + [{"f": "option_type", "op": "eq", "v": "call"}])
    assert by_label["total"] == allc // 2
    weekly = _run("options-screener", base + [{"f": "expiry_type", "op": "in", "v": ["weekly"]}])
    assert {r["expiry"] for r in weekly["rows"]} == {"2026-10-23", "2026-10-30", "2026-11-06"}
    exps = _run("options-screener", base + [{"f": "expiry", "op": "in", "v": ["2026-11-20", "2026-12-18"]}])
    assert {r["expiry"] for r in exps["rows"]} == {"2026-11-20", "2026-12-18"}
    within = _run("options-screener", base + [{"f": "expiry", "op": "within", "v": 20}])
    assert {r["expiry"] for r in within["rows"]} == {"2026-10-16", "2026-10-23", "2026-10-30"}
    dbtw = _run("options-screener", base + [{"f": "expiry", "op": "between", "lo": "2026-10-20", "hi": "2026-11-10"}])
    assert {r["expiry"] for r in dbtw["rows"]} == {"2026-10-23", "2026-10-30", "2026-11-06"}
    is_true = _run("options-screener", base + [{"f": "earnings_before_exp", "op": "is", "v": True}])
    # earnings on 2026-11-06 (TODAY + 25): an expiry ON the earnings day counts as "before expiration"
    assert {r["expiry"] for r in is_true["rows"]} == {"2026-11-06", "2026-11-20", "2026-12-18", "2027-01-15",
                                                       "2027-02-19", "2027-04-16"}
    is_false = _run("options-screener", base + [{"f": "exp_before_earnings", "op": "is", "v": "true"}])
    assert is_true["total"] + is_false["total"] == allc
    assert _symset([{"f": "earnings_date", "op": "within", "v": 30}]) == {"AAA"}
    assert _symset([{"f": "days_to_earnings", "op": "between", "lo": 50, "hi": 70}]) == {"DDD"}
    hours = _run("options-screener", base + [{"f": "last_trade", "op": "within", "v": 2}])
    assert 0 < hours["total"] < allc
    assert _symset([{"f": "exchange", "op": "in", "v": ["NASDAQ"]}]) == {"BBB", "DDD"}
    assert _symset([{"f": "trend", "op": "in", "v": ["sideways", "down"]}]) == {"BBB", "CCC"}
    assert _symset([{"f": "sec_type", "op": "in", "v": ["etf", "index"]}]) == {"CCC", "IDX"}
    # DDD has no IV rank: NaN never matches a range
    assert _symset([{"f": "iv_rank", "op": "gte", "lo": 30}]) == {"AAA", "BBB", "IDX"}
    assert _symset([{"f": "stock_price", "op": "between", "lo": 50, "hi": 500}]) == {"AAA", "BBB", "CCC"}
    empty_cards = _run("options-screener", base + [{"f": "exchange", "op": "in", "v": []},
                                                   {"f": "strike", "op": "between"}])
    assert empty_cards["total"] == allc and empty_cards["warnings"] == []


def test_filter_warnings_never_raise():
    out = _run("options-screener", [{"f": "bogus", "op": "gte", "lo": 1},
                                    {"f": "volume", "op": "nope", "lo": 1},
                                    {"f": "option_type", "op": "in", "v": ["X"]},
                                    {"f": "net_credit", "op": "gte", "lo": 1},
                                    {"f": "volume", "op": "gte", "lo": "abc"},
                                    "junk",
                                    {"f": "earnings_before_exp", "op": "is", "v": "maybe"}])
    ws = " | ".join(out["warnings"])
    for frag in ("Unknown filter field 'bogus'", "Unknown operator 'nope'", "'X' is not a value",
                 "does not apply", "could not read", "must be an object", "'is' needs true or false"):
        assert frag in ws, frag
    assert out["total"] == engine.run("options-screener", {"filters": []})["total"]
    nosym = _run("options-screener", [{"f": "symbol", "op": "in", "v": ["ZZZZ"]}])
    assert nosym["total"] == 0 and "not in the market data" in nosym["warnings"][0]
    bad = engine.run("options-screener", "not a dict")
    assert bad["warnings"] and bad["total"] > 0
    unk = engine.run("no-such-screen", {})
    assert unk["total"] == 0 and "Unknown screen" in unk["warnings"][0]
    srt = engine.run("options-screener", {"sort": {"col": "nope", "dir": "sideways"}})
    assert any("Unknown sort column" in w for w in srt["warnings"])
    assert any("Unknown sort direction" in w for w in srt["warnings"])
    vw = engine.run("options-screener", {"view": "nope"})
    assert vw["view"] == "main" and any("Unknown view" in w for w in vw["warnings"])


def test_leg_and_strategy_filters_on_strategy_screen():
    out = _run("bull-put-spread", [{"f": "symbol", "op": "in", "v": ["AAA"]},
                                   {"f": "dte", "op": "between", "lo": 30, "hi": 45},
                                   {"f": "leg1.strike", "op": "eq", "v": 95},
                                   {"f": "net_credit", "op": "gte", "lo": 50}])
    assert out["total"] > 0
    for r in out["rows"]:
        assert r["leg1.strike"] == 95 and r["net_credit"] >= 50 and r["expiry"] == "2026-11-20"
    wrong_leg = _run("bull-put-spread", [{"f": "leg3.strike", "op": "eq", "v": 95}])
    assert any("does not apply" in w for w in wrong_leg["warnings"])


# ─────────────────────────────────── sort / paging / truncation ───────────────────────────────────

def test_sort_and_nan_last():
    two = [{"f": "symbol", "op": "in", "v": ["AAA", "DDD"]}]          # DDD has no IV rank
    asc = _run("options-screener", two, sort={"col": "iv_rank", "dir": "asc"})
    ranks = [r["iv_rank"] for r in asc["rows"]]
    nn = [x for x in ranks if x is not None]
    assert nn == sorted(nn) and ranks[-1] is None and ranks.index(None) == len(nn)
    desc = _run("options-screener", two, sort={"col": "iv_rank", "dir": "desc"})
    dr = [r["iv_rank"] for r in desc["rows"]]
    assert [x for x in dr if x is not None] == sorted(nn, reverse=True) and dr[-1] is None
    by_exp = _run("options-screener", [{"f": "symbol", "op": "in", "v": ["AAA"]}],
                  sort={"col": "expiry", "dir": "desc"})
    assert by_exp["rows"][0]["expiry"] == "2027-04-16"
    by_leg = _run("bull-put-spread", [{"f": "symbol", "op": "in", "v": ["AAA"]}],
                  sort={"col": "leg2.strike", "dir": "asc"})
    strikes = [r["leg2.strike"] for r in by_leg["rows"]]
    assert strikes == sorted(strikes)


def test_paging():
    full = engine.run("options-screener", {"filters": []}, per_page=1000)
    p1 = engine.run("options-screener", {"filters": []}, page=1, per_page=50)
    p2 = engine.run("options-screener", {"filters": []}, page=2, per_page=50)
    assert p1["pages"] == math.ceil(full["kept"] / 50) and p1["per_page"] == 50
    assert [r["id"] for r in p1["rows"] + p2["rows"]] == [r["id"] for r in full["rows"][:100]]
    last = engine.run("options-screener", {"filters": []}, page=10_000, per_page=50)
    assert last["page"] == last["pages"] and last["rows"]
    clamp = engine.run("options-screener", {"filters": []}, per_page=10 ** 6)
    assert clamp["per_page"] == engine.PER_PAGE_MAX


def test_truncation(monkeypatch):
    monkeypatch.setattr(engine, "MAX_ROWS", 40)
    out = engine.run("options-screener", {"filters": [], "sort": {"col": "volume", "dir": "desc"}}, per_page=100)
    assert out["truncated"] is True and out["kept"] == 40 and out["total"] > 40
    vols = [r["volume"] for r in out["rows"]]
    assert vols == sorted(vols, reverse=True) and len(vols) == 40
    top = max(r["volume"] for r in engine.run("options-screener", {"filters": []}, per_page=1)["rows"] or [{"volume": 0}])
    assert vols[0] >= top
    strat = engine.run("bull-put-spread", {"filters": [], "sort": {"col": "net_credit", "dir": "desc"}})
    assert strat["truncated"] is True and strat["kept"] == 40


def test_combo_cap_stops_with_warning(monkeypatch):
    monkeypatch.setattr(strategies, "COMBO_CAP", 50)
    monkeypatch.setattr(strategies, "PAIR_BUDGET", 10)     # tiny chunks so the cap trips mid-run
    monkeypatch.setattr(strategies, "MIN_CHUNK", 10)
    out = engine.run("bull-call-spread", {"filters": []})
    assert out["truncated"] is True
    assert any("Stopped after" in w for w in out["warnings"])


# ─────────────────────────────────── views / earnings / csv ───────────────────────────────────

def test_views():
    payload = {"filters": [{"f": "symbol", "op": "in", "v": ["AAA"]}, {"f": "iv", "op": "gte", "lo": 20}]}
    flt = engine.run("options-screener", dict(payload, view="filter"))
    keys = [c["key"] for c in flt["columns"]]
    assert keys == ["symbol", "stock_price", "option_type", "expiry", "strike", "iv"]
    gk = engine.run("short-iron-condor", {"filters": [], "view": "greeks"})
    gkeys = [c["key"] for c in gk["columns"]]
    for k in ("leg1.delta", "leg4.vega", "leg3.iv", "net_delta", "net_theta"):
        assert k in gkeys
    vol = engine.run("long-call-calendar", {"filters": [], "view": "vol"})
    vkeys = [c["key"] for c in vol["columns"]]
    for k in ("leg1.iv", "leg2.iv", "iv_skew", "iv_rank", "iv_pctl", "hv20", "avg_iv_hv", "exp_move30"):
        assert k in vkeys
    sv = engine.run("options-screener", {"filters": [], "view": "vol"})
    assert {"iv", "iv_rank", "hv20", "iv_hv", "exp_move"} <= {c["key"] for c in sv["columns"]}


def test_short_iron_condor_net_delta_sign():
    out = engine.run("short-iron-condor", {"filters": [], "view": "greeks"})
    r = out["rows"][0]
    # legs: buy P K1, sell P K2, sell C K3, buy C K4
    expect = r["leg1.delta"] - r["leg2.delta"] - r["leg3.delta"] + r["leg4.delta"]
    assert r["net_delta"] == pytest.approx(expect, abs=3e-4)


def test_flag_earnings():
    out = engine.run("options-screener", {"filters": [{"f": "symbol", "op": "in", "v": ["AAA", "BBB"]}],
                                          "flag_earnings": True}, per_page=1000)
    assert out["columns"][-1]["key"] == "earnings_date" and out["columns"][-1]["label"] == "Earnings"
    for r in out["rows"]:
        if r["symbol"] == "BBB":
            assert r["earnings_date"] is None and r["earnings_flag"] is False
        else:
            assert r["earnings_date"] == "2026-11-06"
            assert r["earnings_flag"] == (r["expiry"] >= "2026-11-06")


def test_csv():
    text = engine.csv("bull-put-spread", {"filters": [{"f": "symbol", "op": "in", "v": ["AAA"]}],
                                          "flag_earnings": True}, limit=7)
    rows = list(_csv.reader(io.StringIO(text)))
    labels = [lbl for _, lbl in screens_mod.SCREENS["bull-put-spread"].columns]
    assert rows[0] == labels + ["Earnings", "Earnings Before Expiration"]
    assert len(rows) == 8 and all(r[0] == "AAA" for r in rows[1:])
    run = engine.run("bull-put-spread", {"filters": [{"f": "symbol", "op": "in", "v": ["AAA"]}]}, per_page=7)
    assert [float(r[3]) for r in rows[1:]] == [r["leg1.strike"] for r in run["rows"]]


# ─────────────────────────────────── the DB loader ───────────────────────────────────

def _seed_db(url: str, n_pass: int, finished=True, extra_symbol=None):
    from app import screener_db
    from app.screener_models import ScrBase, ScrContract, ScrPass, ScrUnderlying

    screener_db.configure(url)
    from pathlib import Path
    root = Path(screener_db.DASH_ROOT)
    if (root / "alembic_screener").is_dir() and (root / "alembic_screener.ini").is_file():
        screener_db.init_screener_db()                       # the real screener migrations
    else:
        # alembic_screener/ was still being written (Part D) when this test was authored; until
        # it lands the tables are created straight from the models on the temp engine.
        ScrBase.metadata.create_all(screener_db.engine())
    unds = UNDERLYINGS[:2] + ((dict(UNDERLYINGS[2], symbol=extra_symbol),) if extra_symbol else ())
    with screener_db.session() as s:
        s.query(ScrContract).delete()
        s.query(ScrUnderlying).delete()
        for u in unds:
            row = underlying_row(u)
            s.add(ScrUnderlying(**row))
            for c in chain(u, exp_days=(11, 39)):
                s.add(ScrContract(**c))
        s.add(ScrPass(id=n_pass, kind="cycle", session="2026-10-09", started=AS_OF,
                      finished=AS_OF + dt.timedelta(minutes=9) if finished else None))
        s.commit()
    return len(unds)


def test_loader_from_db(tmp_path, monkeypatch):
    from app import screener_db

    url = "sqlite:///" + (tmp_path / "screener_test.db").as_posix()
    monkeypatch.setattr(frame_mod.clock, "et_date", lambda now=None: TODAY)
    try:
        _seed_db(url, 1)
        monkeypatch.setattr(frame_mod, "LOAD_CHUNK", 100)        # several yield_per partitions
        frame_mod.reset()
        fr = frame_mod.current()
        assert fr.meta["source"] == "db" and fr.meta["pass_id"] == 1 and not fr.empty
        assert fr.symbols == ("AAA", "BBB") and fr.meta["finished"].endswith("Z")
        expected = sum(len(chain(u, exp_days=(11, 39))) for u in UNDERLYINGS[:2])
        assert fr.n == expected
        assert fr.meta["as_of"] == "2026-10-09T20:00:00Z"
        out = engine.run("bull-put-spread", None)
        assert out["total"] > 0 and out["data"]["pass_id"] == 1
        # same pass -> no reload; a newer finished pass -> background reload swaps the frame
        monkeypatch.setattr(frame_mod, "RELOAD_CHECK_S", 0.0)
        assert frame_mod.current() is fr
        frame_mod.wait_reload()
        assert frame_mod.current() is fr
        _seed_db(url, 2, extra_symbol="EEE")
        frame_mod.current()
        frame_mod.wait_reload()
        fr2 = frame_mod.current()
        assert fr2 is not fr and fr2.meta["pass_id"] == 2 and "EEE" in fr2.symbols
    finally:
        frame_mod.reset()
        screener_db.configure(None)


def test_loader_missing_or_unmigrated_db(tmp_path):
    from app import screener_db

    try:
        screener_db.configure("sqlite:///" + (tmp_path / "nope" / "missing.db").as_posix())
        frame_mod.reset()
        fr = frame_mod.current()
        assert fr.empty and fr.meta["empty"] and fr.meta["warnings"][0].startswith(frame_mod.NO_DATA)
        assert not (tmp_path / "nope" / "missing.db").exists()
        out = engine.run("options-screener", {})
        assert out["total"] == 0 and out["warnings"]
        # a file that exists but has no tables
        bare = tmp_path / "bare.db"
        bare.write_bytes(b"")
        screener_db.configure("sqlite:///" + bare.as_posix())
        frame_mod.reset()
        fr = frame_mod.current()
        assert fr.empty and "could not be read" in fr.meta["warnings"][0]
    finally:
        frame_mod.reset()
        screener_db.configure(None)


# ─────────────────────────────────── performance smoke ───────────────────────────────────

def synthetic_market(n_contracts: int, per_und: int = 250) -> Frame:
    """~n_contracts over n/per_und underlyings, 5 expiries x 25 strikes x 2 rights each,
    built column-wise (vectorised Black-Scholes) - the fast path a real load also takes."""
    from app.services.screener.frame import ncdf as vn

    n_und = max(1, n_contracts // per_und)
    rng = np.random.default_rng(11)
    spots = np.round(rng.uniform(10, 500, n_und), 2)
    syms = np.array([f"S{i:05d}" for i in range(n_und)])
    exp_days = np.array([7, 21, 35, 63, 91])
    n_k = per_und // (2 * len(exp_days))
    step = np.maximum(np.round(spots * 0.02, 0), 0.5)
    und = np.repeat(np.arange(n_und), len(exp_days) * 2 * n_k)
    e = np.tile(np.repeat(exp_days, 2 * n_k), n_und)
    put = np.tile(np.repeat(np.tile([0, 1], len(exp_days)), n_k), n_und)
    kk = np.tile(np.arange(n_k) - n_k // 2, n_und * len(exp_days) * 2)
    S = spots[und]
    K = np.round(S / step[und]) * step[und] + kk * step[und]
    K = np.maximum(K, step[und])
    T = e / 365.0
    sig = 0.25 + 0.3 * np.log(K / S) ** 2 + rng.uniform(0, 0.2, n_und)[und]
    d1 = (np.log(S / K) + (RISK_FREE + 0.5 * sig ** 2) * T) / (sig * np.sqrt(T))
    d2 = d1 - sig * np.sqrt(T)
    disc = np.exp(-RISK_FREE * T)
    call_p = S * vn(d1) - K * disc * vn(d2)
    put_p = K * disc * vn(-d2) - S * vn(-d1)
    price = np.round(np.where(put == 1, put_p, call_p), 2)
    delta = np.where(put == 1, vn(d1) - 1, vn(d1))
    n = len(S)
    exps = (np.datetime64(TODAY.isoformat()) + e.astype("timedelta64[D]"))
    contracts = {
        "symbol": syms[und], "expiry": exps, "right": np.where(put == 1, "P", "C"), "strike": K,
        "weekly": np.zeros(n, dtype=bool), "price": price, "last": price, "chg_pct": np.zeros(n),
        "volume": rng.integers(0, 5000, n).astype(float), "oi": rng.integers(0, 20000, n).astype(float),
        "vol_prev": np.full(n, 100.0), "oi_prev": np.full(n, 100.0), "iv": sig, "delta": delta,
        "gamma": np.full(n, 0.01), "theta": np.full(n, -0.02), "vega": np.full(n, 0.1),
        "last_trade": np.full(n, np.nan), "as_of": [AS_OF],
    }
    underlyings = {"symbol": list(syms), "sec_type": ["stock"] * n_und, "exchange": ["NYSE"] * n_und,
                   "spot": spots, "hv20": np.full(n_und, 30.0), "iv_rank": rng.uniform(0, 100, n_und),
                   "trend": ["up"] * n_und, "history_done": [True] * n_und}
    for k in frame_mod.U_COLS:
        underlyings.setdefault(k, None)
    return Frame.from_columns(contracts, underlyings, today=TODAY)


def test_performance_smoke():
    n = int(os.environ.get("SCREENER_PERF_CONTRACTS", "200000"))
    t0 = time.perf_counter()
    big = synthetic_market(n)
    build = time.perf_counter() - t0
    frame_mod.set_current(big)
    t0 = time.perf_counter()
    single = engine.run("options-screener", None)
    t_single = time.perf_counter() - t0
    t0 = time.perf_counter()
    bps = engine.run("bull-put-spread", None)
    t_bps = time.perf_counter() - t0
    print(f"\n[perf] {big.n:,} contracts / {big.nu:,} underlyings: build {build:.2f}s, "
          f"options-screener {t_single:.2f}s ({single['total']:,} rows), "
          f"bull-put-spread {t_bps:.2f}s ({bps['total']:,} rows)")
    assert single["total"] > 0 and bps["total"] > 0
    assert t_single < 3.0
    assert t_bps < 8.0
