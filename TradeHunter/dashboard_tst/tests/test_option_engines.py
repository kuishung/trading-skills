"""The decision engines of step 1 (design/options/part_B_engines.md B9, OPTIONS_MODULE_DESIGN.md
II.4 step 1): premium_gauge, mirror_setups, chart_state, strike_picker (credit_vertical),
option_sizing, order_ticket, option_exits, option_engine.compute - on synthetic chains and
bars (``tests/fixtures/options``) with TWS off and Cboe down, and on the two GOLDEN fixtures
(``lrcx.json`` / ``isrg.json``) against the values ``regen.py`` printed into ``expected.json``
(II.6: the golden numbers are generated, never typed).

Run from dashboard_tst: ``py -m pytest tests/test_option_engines.py -q``.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DASH = Path(__file__).resolve().parent.parent
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

from app.services import chart_state, mirror_setups, opt_legs, option_data, option_engine  # noqa: E402
from app.services import option_exits, option_metrics, option_prefs, option_sizing, option_words  # noqa: E402
from app.services import order_ticket, premium_gauge, strategy_rules, strike_picker  # noqa: E402
from app.services import support_bounce as sb  # noqa: E402
from app.services.opt_constants import LEVEL_PAD_ATR, LOSS_STOP_FRACTION, STOP_IV_BUMP  # noqa: E402
from tests.fixtures.options import bars_synth, chain_bs, regen  # noqa: E402

TODAY = "2026-10-03"
EXPIRIES = ["2026-10-17", "2026-10-31", "2026-11-20", "2026-12-19", "2027-01-15"]
IV_BY_EXP = {"2026-10-17": 0.52, "2026-10-31": 0.50, "2026-11-20": 0.46, "2026-12-19": 0.45, "2027-01-15": 0.44}
STRIKES = list(range(280, 421, 5))
SPOT = 349.2
ATR = 11.54
EARNINGS = {"date": "2026-10-22", "days": 19}


# ────────────────────────────────────────── fixtures ──────────────────────────────────────────

def _days(end: str, n: int) -> list[_dt.date]:
    d = _dt.date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= _dt.timedelta(days=1)
    return out[::-1]


def bounce_bars(level: float = 340.0, end: str = TODAY, n: int = 300, rng: float = 10.0) -> list[dict]:
    """A deterministic support bounce the shipped detector accepts: 200 bars climbing
    250 -> 349, an oscillation with three dips whose lows sit ON ``level`` and a mild
    drift (EMA20 over EMA50), a shallow slide, then a hammer on the last bar with
    its low just under the level on 2.5x volume."""
    closes = [250.0 + 0.5 * i for i in range(200)]
    seg: list[float] = []
    for _dip in range(3):
        seg += [level + 15 - 2 * k for k in range(6)]
        seg += [level + 3, level + 2.5]
        seg += [level + 5 + 2.2 * k for k in range(8)]
        seg += [level + 17, level + 18, level + 17.5, level + 16]
    while len(closes) + len(seg) < n - 11:
        seg.append(level + 16 + (len(seg) % 3))
    seg = [v + 0.18 * k for k, v in enumerate(seg)]
    closes += seg[: n - 11 - len(closes)]
    top = closes[-1]
    closes += [top - 0.5 * k for k in range(10)]
    days = _days(end, n)
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev if i else c
        h, l = max(o, c) + rng / 2, min(o, c) - rng / 2
        bars.append({"time": days[i].isoformat(), "open": round(o, 2), "high": round(h, 2), "low": round(l, 2),
                     "close": round(c, 2), "volume": 1_000_000 + (i * 7919) % 300_000})
        prev = c
    for b in bars:
        if b["close"] in (round(level + 3, 2), round(level + 2.5, 2)):
            b["low"] = round(level, 2)
    bars.append({"time": days[-1].isoformat(), "open": level + 4.0, "high": level + 5.0, "low": level - 1.5,
                 "close": level + 4.6, "volume": 2_500_000})
    return bars


def fast_bars(n: int = 300) -> list[dict]:
    days = _days(TODAY, n)
    out, c = [], 100.0
    for i in range(n):
        o, c = c, c * 1.02
        out.append({"time": days[i].isoformat(), "open": round(o, 2), "high": round(c * 1.003, 2),
                    "low": round(o * 0.997, 2), "close": round(c, 2), "volume": 1_000_000})
    return out


def quiet_bars(n: int = 300) -> list[dict]:
    days = _days(TODAY, n)
    return [{"time": days[i].isoformat(), "open": 100.0 + (i % 2), "high": 102.0, "low": 99.0,
             "close": 100.0 + ((i + 1) % 2), "volume": 1_000_000} for i in range(n)]


def lrcx_chain(strikes=STRIKES, **kw):
    raw = chain_bs(SPOT, IV_BY_EXP, EXPIRIES, strikes, today=TODAY, skew=0.3, spread=0.10,
                   symbol="LRCX", iv30=46.0, **kw)
    return option_data.chain_from_legacy(raw)


def lrcx_chart(**over) -> dict:
    """The LRCX worked example's chart as a ChartState: uptrend 34 days, support bounce
    at 340.9 (zone 339.1-342.0, 3 touches, high volume, quality 80), ATR 11.54, close
    349.20, resistance 372, earnings Oct 22."""
    setup = {"kind": "support_bounce", "direction": "up", "level": 340.9, "zone": [339.1, 342.0], "touches": 3,
             "quality": 80, "vol_high": True, "vol_ratio": 1.7,
             "candle": {"time": "2026-10-01", "kind": "pin", "low": 339.4}, "summary": "bounce off 340.9 (3 touches)"}
    plan = chart_state.plan_for("up", SPOT, ATR, setup["zone"], resistance=372.0)
    st = {"symbol": "LRCX", "as_of": TODAY, "close": SPOT, "atr": ATR,
          "ema": {"e20": 346.1, "e50": 335.8, "e200": 301.2}, "w_ema": None,
          "trend": "up", "trend_days": 34, "w_uptrend": True, "slow_drift": False,
          "structure": {"state": "bullish", "reason": "higher high and higher low - bullish trend"},
          "sup": None, "tl": None, "tl_bounce": None, "rng": None,
          "setup": setup, "setups": [setup],
          "levels": {"support": 340.9, "resistance": 372.0, "target_up": 372.0, "target_dn": 340.9},
          "earnings": dict(EARNINGS), "plan": plan,
          "evidence": ["EMA20 > EMA50 > EMA200 for 34 sessions", "bounce candle 2026-10-01 on 1.7x volume"],
          "expiries": list(EXPIRIES)}
    st.update(over)
    return st


def bear_chart() -> dict:
    """The mirror: downtrend, resistance rejection at 360 (zone 359.1-360.9)."""
    setup = {"kind": "resistance_reject", "direction": "down", "level": 360.0, "zone": [359.1, 360.9], "touches": 3,
             "quality": 80, "vol_high": True, "candle": {"time": "2026-10-01", "kind": "pin", "high": 361.0}}
    plan = chart_state.plan_for("down", SPOT, ATR, setup["zone"], support=320.0)
    return lrcx_chart(symbol="XYZ", trend="down", structure={"state": "decelerated", "reason": "lower high"},
                      setup=setup, setups=[setup], w_uptrend=False,
                      levels={"support": 320.0, "resistance": 360.0, "target_up": 360.0, "target_dn": 320.0}, plan=plan)


def gauge62() -> dict:
    series = [26.0 + (58.2581 - 26.0) * ((i * 37) % 251) / 250 for i in range(251)] + [46.0]   # rank exactly 62.0
    return premium_gauge.gauge(iv30=46.0, iv_series=series, hv20=38.0, hv60=36.1, iv_front=50.0, iv_back=45.0,
                               front_dte=28, back_dte=77, earnings_date=EARNINGS["date"], earnings_days=19)


def prefs(defined_risk_only: bool = True, nlv: float | None = 100000.0, **over) -> dict:
    raw: dict = {"shared": {"earnings_rule": "defined_risk_only"}} if defined_risk_only else {}
    for k, v in over.items():
        block, field = k.split(".")
        raw.setdefault(block, {})[field] = v
    p = option_prefs.clean(raw)
    p["account"] = {"nlv": nlv, "risk_pct": 1.0, "nlv_source": "prefs" if nlv else None}
    return p


def golden_pick() -> dict:
    """A Pick with the ruled II.2.19 figures, for the sizing / ticket arithmetic: Nov 20
    330/320 at 2.10, max loss 790, chart stop 336.2 -> -120.7, rule stop -158."""
    legs = [{"expiry": "2026-11-20", "right": "P", "strike": 330.0, "side": "sell", "qty": 1, "price": 5.70, "bid": 5.65,
             "ask": 5.75, "iv": 0.46, "delta": -0.250, "oi": 2140, "volume": 412},
            {"expiry": "2026-11-20", "right": "P", "strike": 320.0, "side": "buy", "qty": 1, "price": 3.60, "bid": 3.55,
             "ask": 3.65, "iv": 0.47, "delta": -0.174, "oi": 1630, "volume": 230}]
    return {"symbol": "LRCX", "strategy": "bull_put", "family": "credit_vertical", "legs": legs,
            "expiry": "2026-11-20", "dte": 48, "net": -2.10, "width": 10.0, "max_profit": 210.0, "max_loss": 790.0,
            "breakevens": [327.90], "pop": 0.75, "pop_kind": "keep", "pop_model": 0.73,
            "greeks": {"delta": 0.076, "theta": 0.021, "vega": -0.048, "gamma": -0.010},
            "liquidity": {"tier": "clean", "widest": 0.10, "min_oi": 1630, "vol_ok": True, "worst_fill": -2.00, "notes": []},
            "constraint": {"ok": True, "detail": "330 sits under 336.2"},
            "chart_stop": 336.2, "chart_stop_pl": -120.7, "rule_stop_pl": -158.0,
            "checks": [{"name": "open interest >= 500", "ok": True},
                       {"name": "earnings inside expiry", "ok": False, "blocking": False, "detail": "Oct 22 is inside Nov 20"}],
            "score": 0.328, "why": ["best fit to your rules"], "words": {}, "sizing": None, "status": "ok",
            "rules_line": "delta 0.20-0.30 · 30-60 days", "considered": 23, "degenerate": None, "as_of": "2026-10-02T15:59:59"}


def trade(**over) -> SimpleNamespace:
    p = golden_pick()
    t = SimpleNamespace(id=1, user_id=1, symbol="LRCX", strategy="bull_put", family="credit_vertical",
                        legs=[dict(l, entry_price=l["price"], entry_delta=l["delta"], entry_iv=l["iv"]) for l in p["legs"]],
                        front_expiry="2026-11-20", back_expiry=None, net_entry=-2.10, contracts=2, max_loss=790.0,
                        chart_stop=336.2, chart_target=None, roll_dte=None, paper=False, signal_id=None,
                        earnings_date_at_entry="2026-10-22", roll_delta=None, loss_stop_pct=None,
                        profit_target_pct=None, dte_floor=None, meta={}, status="open", note=None)
    for k, v in over.items():
        setattr(t, k, v)
    return t


def snap(**over) -> dict:
    s = {"symbol": "LRCX", "contracts": 2, "dte": 48, "back_dte": None, "spot": 349.2, "mark": 2.10, "pl": 0.0, "profit_pct": 0.0,
         "net_delta": 15.2, "theta": 4.2, "vega": -9.6, "short_delta": 0.25, "max_loss": 790.0, "max_profit": 210.0,
         "legs": [], "error": None}
    s.update(over)
    return s


# ────────────────────────────────────────── premium gauge ──────────────────────────────────────────

class TestPremiumGauge:
    def test_golden_62_sells_with_the_exact_sentence(self):
        g = gauge62()
        assert g["verdict"] == "SELL" and g["basis"] == "rank" and g["state"] == "ok" and g["iv_n"] == 252
        assert g["iv_rank"] == 62.0 and round(g["iv_hv_premium"], 2) == 1.21 and round(g["term_ratio"], 2) == 1.11
        assert g["gates"] == {"buy": False, "sell_directional": True, "sell_neutral": True, "mid": False}
        assert g["verdict_why"] == "IV rank 62 (>= 50) and priced for 21% more movement than the stock has actually shown"
        assert g["provisional"] is False and g["earnings_days"] == 19
        assert tuple(g.keys()) == premium_gauge.IV_KEYS

    def test_rank_24_buys_and_the_band_cases(self):
        def at(rank, hv):
            lo, hi = 20.0, 60.0
            series = [lo + (hi - lo) * ((i * 37) % 251) / 250 for i in range(251)]
            cur = lo + (hi - lo) * rank / 100.0
            return premium_gauge.gauge(iv30=cur, iv_series=series + [cur], hv20=hv)
        assert at(24, 40.0)["verdict"] == "BUY" and at(24, 40.0)["gates"]["buy"] is True
        g = at(41, 36.4 / 1.21)
        assert g["verdict"] == "SELL" and "sellers are paid" in g["verdict_why"] and g["gates"]["mid"] is True
        assert at(41, 36.4 / 0.95)["verdict"] == "NEUTRAL"
        assert at(41, 36.4 / 0.85)["verdict"] == "BUY"

    def test_forming_percentile_rank_ok_and_unknown(self):
        g = premium_gauge.gauge(iv30=46.0, iv_series=[40.0 + i for i in range(18)] + [46.0], hv20=38.0)
        assert g["basis"] == "provisional" and g["provisional"] is True and g["state"] == "forming"
        assert g["gates"] == {k: False for k in premium_gauge.GATE_KEYS} and "19 of 60 days" in g["verdict_why"]
        g = premium_gauge.gauge(iv30=46.0, iv_series=[30.0 + i * 0.5 for i in range(39)] + [46.0], hv20=38.0)
        assert g["basis"] == "percentile" and g["state"] == "pct_only" and "(not a full year yet)" in g["verdict_why"]
        g = premium_gauge.gauge(iv30=46.0, iv_series=[28.0 + (29.0 * (i % 117) / 116) for i in range(117)] + [46.0], hv20=38.0)
        assert g["state"] == "rank_ok" and g["basis"] == "rank" and "over 118 days" in g["verdict_why"]
        g = premium_gauge.gauge(iv30=46.0, iv_series=[], hv20=None)
        assert g["verdict"] == "UNKNOWN" and g["basis"] == "unknown"
        assert g["verdict_why"] == ("We cannot yet say whether options are expensive - 0 of 60 days of history. "
                                    "If you have TWS on this PC, press Live to load a year.")
        assert g["verdict_why"] == option_words.iv_rank_words(g)        # the ONE string, both spellings
        g = premium_gauge.gauge(iv30=40.0, iv_series=[40.0] * 252, hv20=38.0)
        assert g["iv_rank"] is None

    def test_words(self):
        assert premium_gauge.term_words(1.11, 19, front_dte=28) == "front month 1.11x the back - an event is priced (earnings in 19d)"
        assert premium_gauge.term_words(None, 19) is None
        assert premium_gauge.term_words(0.93, None) == "front month cheaper than the back - calendar shape"
        assert premium_gauge.span_words({"state": "ok", "iv_n": 252}) == "over the last year"
        assert premium_gauge.span_words({"state": "rank_ok", "iv_n": 118}) == "over 118 days"
        assert premium_gauge.span_words({"state": "pct_only", "iv_n": 34}) == "against the last 34 days (not a full year yet)"


# ────────────────────────────────────────── mirror_setups ──────────────────────────────────────────

class TestMirrorSetups:
    def test_mirror_bars_round_trips(self):
        bars = bars_synth("uptrend_bounce", n=80, seed=3)
        twice = mirror_setups.mirror_bars(mirror_setups.mirror_bars(bars))
        assert [(b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in twice] == \
               [(b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in bars]
        m = mirror_setups.mirror_bars(bars)[0]
        assert m["high"] == -bars[0]["low"] and m["low"] == -bars[0]["high"]

    def test_resistance_reject_on_the_inverted_bounce(self):
        bars = bounce_bars()
        sup = sb.find(bars)
        assert sup is not None and sup["n_touches"] >= 2 and sup["vol_high"]
        rr = mirror_setups.find_resistance_reject(mirror_setups.mirror_bars(bars))
        assert rr is not None and rr["level"] == -sup["level"] and rr["candle"]["kind"] == "pin"
        assert rr["zone"] == [-sup["zone"][1], -sup["zone"][0]] and rr["n_touches"] == sup["n_touches"]
        assert mirror_setups.find_resistance_reject(bars) is None            # the bounce itself is not a rejection

    def test_breakdown_fires_only_on_a_fresh_break(self):
        bars = bars_synth("downtrend_breakdown", n=300, start=400.0, seed=3)
        bd = mirror_setups.find_breakdown(bars)
        assert bd is not None and bd["n_touches"] >= 2 and bd["broke_on"] == bars[-1]["time"] and bd["close"] < bd["level"]
        assert mirror_setups.find_breakdown(bars_synth("uptrend_bounce", n=300, seed=7)) is None
        assert mirror_setups.find_breakdown(bounce_bars()) is None
        assert mirror_setups.find_breakdown(bars_synth("flat", n=30)) is None


# ────────────────────────────────────────── chart_state ──────────────────────────────────────────

class TestChartState:
    def test_analyze_called_once_and_the_bounce_reads_up(self, monkeypatch):
        import app.services.ema_setup as es
        calls = []
        real = es.analyze

        def spy(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        monkeypatch.setattr(es, "analyze", spy)
        st = chart_state.read("SYN", bars=bounce_bars(), long_bars=None, today=TODAY,
                              expiries=("2026-11-20", "2026-12-19"), earnings=EARNINGS)
        assert len(calls) == 1
        assert st["trend"] == "up" and st["trend_days"] > 0 and st["atr"] > 0
        assert st["setup"]["kind"] == "support_bounce" and st["setup"]["quality"] >= 50 and st["setup"]["touches"] >= 2
        zone_lo = st["setup"]["zone"][0]
        assert st["plan"]["stop"] == round(min(st["close"] - st["atr"], zone_lo - LEVEL_PAD_ATR * st["atr"]), 2)
        assert st["plan"]["target"] == pytest.approx(st["close"] + 2 * (st["close"] - st["plan"]["stop"]), abs=0.02)
        assert st["levels"]["support"] == st["setup"]["level"] and st["earnings"] == EARNINGS
        assert st["sup"] is not None and st["tl"] is None and st["rng"] is None
        assert st["structure"]["state"] in ("bullish", "decelerated", "unclear")

    def test_plan_reproduces_the_golden_numbers(self):
        p = chart_state.plan_for("up", 349.2, 11.54, [339.1, 340.9])
        assert abs(p["stop"] - 336.2) < 0.05 and abs(p["target"] - 375.2) < 0.1 and p["entry"] == 349.2
        p = chart_state.plan_for("up", 405.81, 11.54, [404.6, 404.6], resistance=431.0)
        assert abs(p["stop"] - 394.27) < 0.3 and abs(p["target"] - 428.89) < 0.3
        p = chart_state.plan_for("up", 405.81, 11.54, [404.6, 404.6], resistance=425.0)
        assert p["target"] == 425.0                                  # a resistance between 1.5R and 2R caps the target
        p = chart_state.plan_for("down", 349.2, 11.54, [359.1, 360.9])
        assert p["stop"] == round(max(349.2 + 11.54, 360.9 + 0.25 * 11.54), 2) and p["target"] < 349.2
        assert chart_state.plan_for("neutral", 100.0, 2.0, [95, 105]) is None

    def test_down_unclear_and_never_sideways(self):
        st = chart_state.read("DN", bars=bars_synth("downtrend_breakdown", n=300, start=400.0, seed=3),
                              long_bars=None, today=TODAY, expiries=(), earnings=None)
        assert st["trend"] == "down" and st["setup"]["kind"] == "failed_support" and st["plan"]["stop"] > st["close"]
        assert st["plan"]["target"] < st["close"] and st["earnings"] is None
        st = chart_state.read("FLAT", bars=bars_synth("flat", n=300, seed=5), long_bars=None, today=TODAY, expiries=(), earnings=None)
        assert st["trend"] in ("up", "down", "unclear") and st["trend"] != "sideways"
        for kind in ("uptrend_bounce", "range", "slow_grind"):
            st = chart_state.read("X", bars=bars_synth(kind, n=300, seed=7), long_bars=None, today=TODAY, expiries=(), earnings=None)
            assert st["trend"] != "sideways"                             # sideways only through rng.sideways (step 3)

    def test_slow_drift(self):
        assert chart_state.read("F", bars=fast_bars(), long_bars=None, today=TODAY, expiries=(), earnings=None)["slow_drift"] is False
        assert chart_state.read("Q", bars=quiet_bars(), long_bars=None, today=TODAY, expiries=(), earnings=None)["slow_drift"] is True

    def test_edge_probes_never_raise(self):
        assert chart_state.read("S", bars=bars_synth("flat", n=30), long_bars=None, today=TODAY, expiries=(), earnings=None)["trend"] == "unclear"
        bars = bounce_bars()
        for b in bars:
            b["volume"] = None
        st = chart_state.read("V", bars=bars, long_bars=None, today=TODAY, expiries=(), earnings=None)
        assert st["trend"] == "up"
        bars = bounce_bars()
        bars[-1].update(open=344.0, high=344.0, low=344.0, close=344.0)       # a zero-range candle
        chart_state.read("Z", bars=bars, long_bars=None, today=TODAY, expiries=(), earnings=None)
        bars = bounce_bars()
        bars[-1]["session_frac"] = 0.1
        chart_state.read("O", bars=bars, long_bars=None, today=TODAY, expiries=(), earnings=None)
        bars = bounce_bars()
        bars[-5]["close"] = "n/a"
        st = chart_state.read("N", bars=bars, long_bars=None, today=TODAY, expiries=(), earnings=None)
        assert "trend" in st

    def test_stored_projection_and_round_trip(self):
        st = lrcx_chart()
        s = chart_state.stored_setup(st)
        for k in ("kind", "direction", "level", "zone", "touches", "quality", "close", "trend_days", "atr", "ema",
                  "plan", "stop", "target", "levels", "sup", "tl", "tl_bounce", "rng", "evidence"):
            assert k in s
        assert s["stop"] == st["plan"]["stop"] and s["target"] == st["plan"]["target"] and s["trend"] == "up"
        back = chart_state.from_stored(s)
        assert back["setup"]["kind"] == "support_bounce" and back["plan"] == st["plan"] and back["atr"] == ATR
        assert chart_state.from_stored(st) is st
        empty = chart_state.stored_setup(lrcx_chart(setup=None, plan=None))
        assert empty["kind"] is None and empty["stop"] is None and empty["trend"] == "up"


# ────────────────────────────────────────── strategy rules on the chart ──────────────────────────────────────────

class TestRulesOnTheChart:
    def test_rank_62_defined_risk_only_recommends_the_bull_put(self):
        rec = strategy_rules.recommend(lrcx_chart(), gauge62(), prefs(), snapshot_expiries=EXPIRIES)
        assert rec["recommended"] == "bull_put" and len(rec["strategies"]) == 10
        rows = {r["key"]: r for r in rec["strategies"]}
        assert rows["bull_put"]["fit"] == "recommended" and rows["bull_put"]["score"] == 90.1
        assert rows["buy_call"]["reason_key"] == "expensive" and rows["buy_call"]["score"] is None
        assert rows["leaps_call"]["label"] == "Buy LEAPS"
        for r in rec["strategies"]:
            assert r["reason_key"] in (None,) + strategy_rules.REASON_KEYS
        assert sum(1 for r in rec["strategies"] if r["fit"] == "rejected" and r["shown"]) <= 2

    def test_none_inside_rejects_for_earnings(self):
        rec = strategy_rules.recommend(lrcx_chart(), gauge62(), prefs(defined_risk_only=False), snapshot_expiries=EXPIRIES)
        rows = {r["key"]: r for r in rec["strategies"]}
        assert rec["recommended"] is None and rows["bull_put"]["reason_key"] == "earnings_inside"
        for s in strategy_rules.member_strings():
            assert "step" not in s.lower() and "phase" not in s.lower()


# ────────────────────────────────────────── strike picker ──────────────────────────────────────────

class TestStrikePicker:
    def _run(self, chart=None, chain=None, p=None, key="bull_put"):
        chain = chain or lrcx_chain()
        view = opt_legs.chain_view(chain, today=TODAY)
        return strike_picker.pick(key, view, chart or lrcx_chart(), gauge62(), p or prefs(), today=TODAY)

    def test_golden_shape_and_ordering(self):
        res = self._run()
        assert res["status"] == "ok" and res["considered"] > 0 and len(res["picks"]) == 3
        assert res["prefs_hash"] == option_prefs.prefs_hash(prefs()) and "under support 340.9" in res["rules_line"]
        scores = [p["score"] for p in res["picks"]]
        assert scores == sorted(scores, reverse=True)
        bound = 339.1 - LEVEL_PAD_ATR * ATR
        for p in res["picks"]:
            short, long_ = p["legs"]
            assert short["side"] == "sell" and long_["side"] == "buy" and short["right"] == "P" == long_["right"]
            assert set(short) == set(opt_legs.STORED_KEYS)
            assert 0.20 <= abs(short["delta"]) <= 0.30 and short["strike"] <= bound + 1e-9
            assert p["width"] in (10.0, 15.0) and 30 <= p["dte"] <= 60 and p["expiry"] in EXPIRIES
            credit = short["price"] - long_["price"]
            assert p["net"] == -round(credit, 4) and p["max_profit"] == round(credit * 100, 2)
            assert p["max_loss"] == round((p["width"] - credit) * 100, 2) and p["max_loss"] > 0
            assert p["breakevens"] == [round(short["strike"] - credit, 2)]
            assert p["pop"] == round(1 - abs(short["delta"]), 4) and p["pop_kind"] == "keep"
            assert 0.4 <= p["pop_model"] <= 0.95
            assert p["chart_stop"] == lrcx_chart()["plan"]["stop"] and p["chart_stop_pl"] < 0
            assert p["rule_stop_pl"] == -round(LOSS_STOP_FRACTION * p["max_loss"], 2)
            assert p["constraint"]["ok"] is True and p["liquidity"]["tier"] in ("clean", "limit")
            assert p["status"] == "ok" and p["degenerate"] is None and p["sizing"] is None
            assert p["greeks"]["delta"] > 0 and p["greeks"]["theta"] > 0 and p["greeks"]["vega"] < 0
            for k in ("delta", "theta", "vega", "pop", "collect", "risk"):
                assert p["words"][k]
            assert p["words"]["pop"] == option_words.pop_words(p["pop"], "keep")
            assert "you collect $" in p["words"]["collect"] and "you risk $" in p["words"]["risk"]
            names = [c["name"] for c in p["checks"]]
            assert "earnings inside expiry" in names and any(n.startswith("open interest") for n in names)
            earn = next(c for c in p["checks"] if c["name"] == "earnings inside expiry")
            assert earn["ok"] is False and earn["blocking"] is False and earn["detail"] == "Oct 22 is inside Nov 20"
            assert p["rules_line"] == res["rules_line"] and p["considered"] == res["considered"]
        assert res["picks"][0]["why"][0] == "best fit to your rules"
        top = res["picks"][0]
        assert top["score"] == round(-top["net"] / (top["width"] + top["net"]) * top["pop"] * 1.0, 3)

    def test_stop_loss_is_the_bumped_model_loss(self):
        from app.services import payoff
        p = self._run()["picks"][0]
        legs = payoff.calibrate(p["legs"], SPOT, TODAY, prefer_leg_iv=True)
        losses = [-payoff.pnl(legs, p["chart_stop"], d * p["dte"], TODAY, STOP_IV_BUMP) for d in (0, 0.5)]
        assert p["chart_stop_pl"] == -round(max(losses), 2)

    def test_constraint_removes_a_short_above_the_pad(self):
        wide = self._run(p=prefs(**{"credit_vertical.short_delta_hi": 0.40}))
        shorts = [p["legs"][0]["strike"] for p in wide["picks"]]
        assert all(s <= 339.1 - LEVEL_PAD_ATR * ATR for s in shorts)
        off = self._run(p=prefs(**{"credit_vertical.short_delta_hi": 0.40, "shared.chart_constraint": False}))
        assert any(p["legs"][0]["strike"] >= 340.0 for p in off["picks"])
        narrow = self._run(p=prefs(**{"credit_vertical.short_delta_lo": 0.36, "credit_vertical.short_delta_hi": 0.40}))
        assert narrow["status"] == "degenerate" and narrow["degenerate"]["reason_key"] == "constraint"
        assert narrow["degenerate"]["nearest"]["status"] == "nearest" and "the market is paying you" in narrow["degenerate"]["text"]

    def test_degenerate_vocabulary(self):
        far = self._run(chain=lrcx_chain(strikes=list(range(200, 261, 5))))
        assert far["status"] == "degenerate" and far["degenerate"]["reason_key"] == "no_band"
        assert "nearest:" in far["degenerate"]["text"] and far["degenerate"]["nearest"]["status"] == "nearest"
        stub = strike_picker.stored_picks(far)
        assert len(stub) == 1 and stub[0]["status"] == "nearest" and stub[0]["degenerate"]["reason_key"] == "no_band"
        floor = self._run(p=prefs(**{"credit_vertical.credit_pct_min": 60}))
        assert floor["status"] == "degenerate" and floor["degenerate"]["reason_key"] == "credit_floor"
        assert "your minimum is 60%" in floor["degenerate"]["text"]
        raw = chain_bs(SPOT, IV_BY_EXP, EXPIRIES, STRIKES, today=TODAY, skew=0.3, symbol="LRCX", iv30=46.0)
        for leg in raw["legs"].values():
            leg["open_interest"] = 100.0
        thin = self._run(chain=option_data.chain_from_legacy(raw))
        assert thin["status"] == "degenerate" and thin["degenerate"]["reason_key"] == "thin"
        assert "open interest under 500" in thin["degenerate"]["text"]
        none_inside = self._run(p=prefs(defined_risk_only=False))
        assert none_inside["status"] == "degenerate" and none_inside["degenerate"]["reason_key"] == "no_expiry"
        assert none_inside["degenerate"]["text"] == "Every 30-60 day expiry has earnings Oct 22 inside it."
        empty = self._run(chain=lrcx_chain(), p=prefs(**{"credit_vertical.dte_lo": 150, "credit_vertical.dte_hi": 170}))
        assert empty["degenerate"]["reason_key"] == "no_expiry"
        none_stub = strike_picker.stored_picks(empty)[0]
        assert none_stub["status"] == "none" and none_stub["legs"] == [] and none_stub["degenerate"]["reason_key"] == "no_expiry"
        for key in strike_picker.DEGENERATE_KEYS:
            assert key in option_words.DEGENERATE_TEXT

    def test_bear_call_is_the_mirror(self):
        res = self._run(chart=bear_chart(), key="bear_call")
        assert res["status"] == "ok" and res["family"] == "credit_vertical"
        bound = 360.9 + LEVEL_PAD_ATR * ATR
        for p in res["picks"]:
            short, long_ = p["legs"]
            assert short["right"] == "C" and short["side"] == "sell" and long_["strike"] > short["strike"]
            assert short["strike"] >= bound - 1e-9 and 0.20 <= abs(short["delta"]) <= 0.30
            assert p["breakevens"] == [round(short["strike"] + (short["price"] - long_["price"]), 2)]
            assert p["chart_stop"] == bear_chart()["plan"]["stop"] and p["chart_stop_pl"] < 0
            assert "over resistance" in p["rules_line"]
        assert "above" in res["picks"][0]["words"]["delta"]

    def test_stored_setup_as_the_chart_and_other_families(self):
        a = self._run()
        b = self._run(chart=chart_state.stored_setup(lrcx_chart()))
        assert [p["legs"] for p in a["picks"]] == [p["legs"] for p in b["picks"]]
        for key in ("buy_call", "bull_call", "iron_condor", "calendar", "leaps_call"):
            r = self._run(key=key)
            assert r["status"] == "degenerate" and r["degenerate"]["reason_key"] == "not_available_yet" and r["picks"] == []
        with pytest.raises(KeyError):
            self._run(key="covered_call")


# ────────────────────────────────────────── sizing ──────────────────────────────────────────

class TestSizing:
    def test_golden_8_2_10_gives_2(self):
        s = option_sizing.size(golden_pick(), 100000.0, prefs())
        assert (s["by_chart_stop"], s["by_gap"], s["by_notional"], s["contracts"]) == (8, 2, 10, 2)
        assert s["stop_t_days"] == 0 and s["stop_iv"] == round(0.46 * 1.10, 4) == 0.506
        assert s["max_loss_pct_nlv"] == 1.6 and s["nlv_source"] == "prefs" and s["fires_first"] == "chart"
        assert s["rule_stop_usd"] == 158.0 and s["rule_stop_kind"] == "20% of max loss"
        assert s["capital_at_risk_usd"] == 241.4 and s["max_loss_total_usd"] == 1580.0 and s["note"] is None
        assert s["line"] == "2 contracts: about $242 if the stop fires, up to $1,580 (1.6% of your account) if the stock gaps past it"

    def test_isrg_like_2_1_6_gives_1(self):
        p = dict(golden_pick(), family="debit_vertical", strategy="bull_call", width=35.0, max_loss=1562.0,
                 chart_stop_pl=-348.0, rule_stop_pl=-781.0, net=15.62)
        s = option_sizing.size(p, 100000.0, prefs())
        assert (s["by_chart_stop"], s["by_gap"], s["by_notional"], s["contracts"]) == (2, 1, 6, 1)
        assert s["line"].startswith("1 contract: about $348 if the stop fires, up to $1,562 (1.6% of your account)")

    def test_caps_notes_and_flips(self):
        p = golden_pick()
        pr = prefs(**{"shared.gap_mult": 3.0})
        pr["account"]["risk_pct"] = 5.0
        s = option_sizing.size(p, 100000.0, pr)
        assert (s["by_chart_stop"], s["by_gap"], s["by_notional"], s["contracts"]) == (41, 18, 10, 10)
        leaps = dict(p, family="leaps", strategy="leaps_call", width=None, max_loss=10383.0, chart_stop_pl=-8580.0, rule_stop_pl=-4153.2)
        s = option_sizing.size(leaps, 100000.0, prefs())
        assert (s["by_chart_stop"], s["by_gap"], s["by_notional"], s["contracts"]) == (0, 0, 0, 0)
        assert s["note"] == "Not even one contract fits your 1% - lower the risk or choose a narrower spread"
        assert s["line"] == s["note"]
        s = option_sizing.size(p, None, prefs(nlv=None))
        assert s["contracts"] is None and s["nlv_source"] is None
        assert s["note"] == "sized once you tell us the account value (My rules -> Shared)" and s["line"] == s["note"]
        s = option_sizing.size(dict(p, chart_stop_pl=0.0), 100000.0, prefs())
        assert s["by_chart_stop"] is None and s["contracts"] == 2 and "loses nothing" in s["note"]
        pr = prefs()
        pr["exits"] = {"loss_fraction": 0.10}
        s = option_sizing.size(p, 100000.0, pr)
        assert s["rule_stop_usd"] == 79.0 and s["fires_first"] == "rule"
        s = option_sizing.size(dict(p, max_loss=3000.0), 100000.0, prefs())
        assert s["by_gap"] == 0 and s["contracts"] == 0           # never forced up to one
        live = prefs()
        live["account"].update(nlv=50000.0, nlv_source="live")
        s = option_sizing.size(p, 50000.0, live)
        assert s["nlv_source"] == "live" and s["contracts"] == 1


# ────────────────────────────────────────── order ticket ──────────────────────────────────────────

class TestOrderTicket:
    def _ticket(self, dip=False, **kw):
        p = golden_pick()
        p["sizing"] = option_sizing.size(p, 100000.0, prefs())
        setup = chart_state.stored_setup(lrcx_chart())
        return order_ticket.build(p, setup, prefs(), dip=dip, **kw)

    def test_the_golden_text(self):
        t = self._ticket()
        assert t["contracts"] == 2 and t["condition"] is None and t["tif"] == "DAY" and t["refresh_first"] in (True, False)
        assert t["header"] == ("Prices are from 02 Oct 15:59 ET. Press Refresh after 21:30 Malaysia time (US open) and "
                               "re-open the ticket before sending; the credit will have moved.")
        assert t["net"] == {"kind": "credit", "limit": 2.10, "floor": 2.00, "per_contract_usd": 210.0, "total_usd": 420.0,
                            "work": ("enter at the mid (2.10); if unfilled in a few minutes step down 0.05 at a time, "
                                     "never below 2.00 (never below $200 a contract)")}
        sc = t["stop"]["chart"]
        assert sc["level"] == 336.2 and sc["loss_usd"] == 241.4 and sc["per_contract_usd"] == 120.7 and sc["gap_usd"] == 1580.0
        assert sc["trigger"] == "LRCX last <= 336.20" and sc["trigger_outside_rth"] is False and sc["fill"] == "market"
        assert t["stop"]["rule"] == {"kind": "20% of max loss", "loss_usd": 316.0, "per_contract_usd": 158.0, "close_at": 3.68}
        assert t["target"] == {"chart": None, "rule": {"kind": "50% of the credit", "close_at": 1.05, "profit_usd": 210.0}}
        assert t["time_stop"] == {"dte_floor": 21, "on": "2026-10-30"}
        assert t["must_happen"] == ("LRCX stays above 330 until Nov 20. You keep the credit if it does nothing, drifts up, "
                                    "or even dips a little.")
        assert t["warnings"] == ["earnings Oct 22 are inside this trade - allowed by your rules; the stop is the only protection"]
        tws, moo = order_ticket.render(t, "tws"), order_ticket.render(t, "moomoo")
        for text in (tws, moo):
            assert text.startswith(t["header"])
            assert "LRCX - Bull put spread (you are paid; the most you can lose is fixed) - 2 contracts - paste into" in text
            assert "Trigger outside RTH: No" in text and order_ticket.RTH_NOTE in text
            assert "(you would lose about $242 here; up to $1,580 if the stock gaps past it)" in text
            assert "Rule stop (no order - the Positions tab watches it)" in text and "3.68 or more (20% of max loss)" in text
            assert "Time stop (no order - the Positions tab watches it): close or roll with 21 days left (Oct 30)" in text
            assert "336.20" in text and "2.10" in text and "2.00" in text and "1.05" in text
            assert "Enter on the dip" not in text and "crashes through" not in text
            assert "defined risk" not in text and "natural" not in text
        assert "SELL 2 LRCX 20 NOV 26 330 P / BUY 2 LRCX 20 NOV 26 320 P" in tws
        assert "Type: Market (recommended)" in tws and "may not fill in a fast market" in tws
        assert "Conditional tab -> Add -> Price -> LRCX (STK, SMART) -> Last <= 336.20" in tws
        assert "BUY TO CLOSE 330 Put, qty 2 - Market" in moo and "SELL TO CLOSE 320 Put, qty 2" in moo
        assert moo.index("BUY TO CLOSE 330 Put") < moo.index("SELL TO CLOSE 320 Put")
        assert "Never sell the long leg before the short leg is closed - you would be short a naked put." in moo
        assert "sell LRCX 2026/11/20 330 Put, buy LRCX 2026/11/20 320 Put, qty 2, limit net credit 2.10 (never below 2.00)" in moo

    def test_dip_toggle_and_fresh_bounce(self):
        t = self._ticket(dip=True)
        c = t["condition"]
        assert c["op"] == "<=" and c["value"] == 341.92 and c["on"] == "LRCX"
        assert c["warning"] == ("This order will also fire if LRCX crashes through 341.92 on bad news. Only use it while "
                                "you are watching.")
        tws = order_ticket.render(t, "tws")
        assert "[Only if 'Enter on the dip' is switched on]" in tws and "Last <= 341.92" in tws and c["warning"] in tws
        fresh = chart_state.stored_setup(lrcx_chart(close=341.9, plan=chart_state.plan_for("up", 341.9, ATR, [339.1, 342.0])))
        p = golden_pick()
        p["sizing"] = option_sizing.size(p, 100000.0, prefs())
        assert order_ticket.build(p, fresh, prefs(), dip=True)["condition"] is None

    def test_refresh_first_through_the_clock(self):
        p = golden_pick()
        p["sizing"] = option_sizing.size(p, 100000.0, prefs())
        setup = chart_state.stored_setup(lrcx_chart())
        inside = _dt.datetime(2026, 10, 5, 14, 30, tzinfo=_dt.timezone.utc)        # Monday 10:30 ET
        assert order_ticket.build(p, setup, prefs(), now=inside)["refresh_first"] is True
        weekend = _dt.datetime(2026, 10, 3, 14, 30, tzinfo=_dt.timezone.utc)       # Saturday
        assert order_ticket.build(p, setup, prefs(), now=weekend)["refresh_first"] is False
        fresh = dict(p, as_of="2026-10-05T10:15:00")                                 # stamped after the open
        assert order_ticket.build(fresh, setup, prefs(), now=inside)["refresh_first"] is False

    def test_refused_rejected_and_unsized(self):
        p = golden_pick()
        p["checks"] = [{"name": "earnings inside expiry", "ok": False, "blocking": True, "detail": "Oct 22 is inside Nov 20"}]
        with pytest.raises(order_ticket.TicketRefused) as e:
            order_ticket.build(p, chart_state.stored_setup(lrcx_chart()), prefs(defined_risk_only=False))
        assert str(e.value) == "No ticket: earnings Oct 22 fall inside this trade and your rule says no."
        p = golden_pick()
        with pytest.raises(order_ticket.TicketRefused):
            order_ticket.build(p, chart_state.stored_setup(lrcx_chart()), prefs(defined_risk_only=False),
                               rejection="earnings 2026-10-22 (19d) sits inside every 30-60 day expiry")
        t = self._ticket(rejection="options too expensive to buy (IV rank 62)")
        for broker in ("tws", "moomoo"):
            assert order_ticket.render(t, broker).splitlines()[0] == "Not recommended today: options too expensive to buy (IV rank 62)."
        p = golden_pick()
        p["sizing"] = option_sizing.size(p, None, prefs(nlv=None))
        t = order_ticket.build(p, chart_state.stored_setup(lrcx_chart()), prefs(nlv=None))
        assert t["contracts"] is None and "- contracts (not sized yet)" in order_ticket.render(t, "tws")
        assert any("sized once you tell us" in w for w in t["warnings"])
        p = golden_pick()
        p["legs"][1]["oi"] = None
        t = order_ticket.build(p, chart_state.stored_setup(lrcx_chart()), prefs())
        assert any(w.startswith("open interest unknown on 320P") for w in t["warnings"])


# ────────────────────────────────────────── exits ──────────────────────────────────────────

class TestOptionExits:
    def test_mark_against_the_chain_and_the_call_chain(self):
        view = opt_legs.chain_view(lrcx_chain(), today=TODAY)
        t = trade()
        s = option_exits.mark(t, view, TODAY)
        short = next(l for l in view["by_expiry"]["2026-11-20"]["P"] if l["strike"] == 330.0)
        long_ = next(l for l in view["by_expiry"]["2026-11-20"]["P"] if l["strike"] == 320.0)
        assert s["error"] is None and s["mark"] == round(short["price"] - long_["price"], 4)
        assert s["pl"] == round((2.10 - s["mark"]) * 100 * 2, 2) and s["dte"] == 48 and s["spot"] == SPOT
        assert s["short_delta"] == abs(short["delta"]) and len(s["legs"]) == 2 and s["legs"][0]["mid"] == short["price"]
        assert s["net_delta"] == round((-short["delta"] + long_["delta"]) * 100 * 2, 2)
        assert s["pl_worst"] < s["pl"]
        legs = [dict(l, right="C") for l in t.legs]
        legs[0]["strike"], legs[1]["strike"] = 370.0, 380.0
        bc = trade(strategy="bear_call", legs=legs)
        sc = option_exits.mark(bc, view, TODAY)
        c370 = next(l for l in view["by_expiry"]["2026-11-20"]["C"] if l["strike"] == 370.0)
        c380 = next(l for l in view["by_expiry"]["2026-11-20"]["C"] if l["strike"] == 380.0)
        assert sc["mark"] == round(c370["price"] - c380["price"], 4) and sc["legs"][0]["delta"] == c370["delta"] > 0
        missing = trade(legs=[dict(t.legs[0], strike=333.0), t.legs[1]])
        sm = option_exits.mark(missing, view, TODAY)
        assert sm["error"].startswith("not on the chain: 333P") and sm["dte"] == 48
        assert option_exits.mark(trade(front_expiry="2026-09-18"), view, TODAY)["error"] == "expired"

    def test_credit_rows_fire_and_one_tick_short_does_not(self):
        setup = chart_state.stored_setup(lrcx_chart())
        g = lambda **kw: option_exits.grade(trade(), snap(**kw), setup, prefs(), earnings="2026-10-22")  # noqa: E731
        v = g(spot=335.0)
        assert v["state"] == "CLOSE" and v["stop_breach"] and "under the 340.9 support" in v["action"]
        assert g(spot=336.3)["state"] in ("WATCH", "OK")                 # one tick above the stop
        assert g(short_delta=0.35, dte=48)["state"] == "ROLL" and g(short_delta=0.35, dte=25)["state"] == "CLOSE"
        v = g(short_delta=0.41, dte=48)
        assert v["state"] == "CLOSE" and v["delta_breach"]
        w = g(short_delta=0.34, dte=48)
        assert w["state"] == "WATCH" and w["delta_breach"] is False
        assert g(pl=-0.20 * 790 * 2, dte=48)["state"] == "ROLL" and g(pl=-0.20 * 790 * 2, dte=25)["state"] == "CLOSE"
        assert g(pl=-0.19 * 790 * 2)["state"] == "WATCH"
        v = g(pl=0.5 * 210 * 2, profit_pct=0.5)
        assert v["state"] == "TAKE" and v["profit_breach"]
        assert g(pl=0.39 * 210 * 2, profit_pct=0.39)["state"] == "OK"
        assert g(dte=21)["state"] == "CLOSE" and g(dte=22)["state"] == "WATCH" and g(dte=30)["state"] == "OK"
        v = g(spot=335.0, short_delta=0.55)
        assert v["state"] == "CLOSE" and v["stop_breach"]                 # the chart stop is listed first
        assert g(spot=None, mark=None, short_delta=None, pl=None)["state"] == "OK"   # the chart still grades
        v = option_exits.grade(trade(), snap(spot=None, mark=None, short_delta=None, pl=None), {}, prefs())
        assert v["state"] == "UNKNOWN"
        assert option_exits.grade(trade(), snap(dte=-3, error="expired"), setup, prefs())["state"] == "EXPIRED"
        v = option_exits.grade(trade(roll_delta=0.30), snap(short_delta=0.31), setup, prefs(), earnings="2026-10-22")
        assert v["state"] == "ROLL"                                        # the per-trade override wins

    def test_earnings_row_for_every_family(self):
        setup = chart_state.stored_setup(lrcx_chart())
        text = ("Earnings Oct 22 now fall inside this trade (the date was unknown or later when you entered). "
                "Decide before the close that day.")
        for fam, strat in (("credit_vertical", "bull_put"), ("debit_vertical", "bull_call"), ("long", "buy_call"),
                           ("condor", "iron_condor"), ("time", "calendar")):
            t = trade(family=fam, strategy=strat, earnings_date_at_entry=None, net_entry=(-2.10 if fam in ("credit_vertical", "condor") else 3.0))
            v = option_exits.grade(t, snap(), setup, prefs(defined_risk_only=False), earnings={"date": "2026-10-22"})
            assert v["state"] == "WATCH" and v["urgent"] and v["earnings_breach"] and v["reasons"][0] == text
        quiet = option_exits.grade(trade(), snap(), setup, prefs(), earnings="2026-10-22")
        assert quiet["state"] == "OK" and quiet["earnings_breach"] is False
        later = option_exits.grade(trade(earnings_date_at_entry="2026-11-25"), snap(), setup, prefs(defined_risk_only=False), earnings="2026-10-22")
        assert later["state"] == "WATCH" and later["urgent"]
        v = option_exits.grade(trade(earnings_date_at_entry=None), snap(spot=335.0), setup, prefs(defined_risk_only=False), earnings="2026-10-22")
        assert v["state"] == "CLOSE" and v["earnings_breach"] and v["reasons"][0] == text    # a losing line still wins
        assert option_exits.grade(trade(front_expiry="2026-10-17", earnings_date_at_entry=None), snap(dte=14), setup,
                                  prefs(defined_risk_only=False), earnings="2026-10-22")["earnings_breach"] is False


# ────────────────────────────────────────── the composer ──────────────────────────────────────────

class TestOptionEngine:
    def _metrics(self, chain):
        series = [26.0 + (58.2581 - 26.0) * ((i * 37) % 251) / 250 for i in range(251)]
        return option_metrics.all_for(chain, bounce_bars(), EARNINGS, series, today=TODAY)

    def test_compute_on_the_synthetic_chart(self):
        chain = lrcx_chain()
        sig = option_engine.compute(chain, self._metrics(chain), lrcx_chart(), prefs())
        assert set(sig) >= {"status", "headline", "setup", "iv", "strategies", "picks", "computed_ms", "engine_version"}
        assert sig["status"] == "ok" and sig["engine_version"] == option_engine.ENGINE_VERSION and sig["trend"] == "up"
        assert len(sig["strategies"]) == 10 and sig["recommended"] == "bull_put"
        rows = {r["key"]: r for r in sig["strategies"]}
        assert rows["bull_put"]["fit"] == "recommended" and "stays above" in rows["bull_put"]["must_happen"]
        assert "Nov 20" in rows["bull_put"]["must_happen"] and rows["bull_put"]["why"].startswith("Uptrend for 34 days.")
        assert rows["buy_call"]["score"] is None and rows["buy_call"]["why"] is None and rows["buy_call"]["must_happen"] is None
        assert rows["leaps_call"]["label"] == "Buy LEAPS"
        assert list(sig["picks"]) == ["bull_put"] and [p["status"] for p in sig["picks"]["bull_put"]] == ["ok"] * 3
        assert sig["picks"]["bull_put"][0]["sizing"] is None
        assert sig["setup"]["stop"] == lrcx_chart()["plan"]["stop"] and sig["setup"]["kind"] == "support_bounce"
        assert sig["iv"]["verdict"] == "SELL" and sig["iv"]["gates"]["sell_directional"] is True
        assert sig["headline"].startswith("Uptrend: EMA 20 above 50 above 200 for 34 days. It bounced off support at 340.9 on high volume")
        assert "so you're paid to sell a put spread below that support" in sig["headline"]
        assert "step" not in sig["headline"].lower()
        text = json.dumps(sig)
        assert "step 1" not in text.lower() and "phase" not in text.lower()

    def test_compute_none_inside_no_chain_no_iv(self):
        chain = lrcx_chain()
        sig = option_engine.compute(chain, self._metrics(chain), lrcx_chart(), prefs(defined_risk_only=False))
        rows = {r["key"]: r for r in sig["strategies"]}
        assert sig["recommended"] is None and rows["bull_put"]["reason_key"] == "earnings_inside" and sig["picks"] == {}
        assert "earnings Oct 22 fall inside every expiry in your window" in sig["headline"]
        assert option_engine.compute(None, None, lrcx_chart(), prefs())["status"] == "no_chain"
        sig = option_engine.compute(chain, None, lrcx_chart(), prefs())
        assert sig["status"] == "no_iv" and sig["iv"]["verdict"] == "UNKNOWN"
        rows = {r["key"]: r for r in sig["strategies"]}
        assert rows["bull_put"]["reason_key"] == "not_rich_enough"
        sig = option_engine.compute(chain, self._metrics(chain), lrcx_chart(setup=None, plan=None), prefs())
        assert sig["status"] == "no_setup" and sig["recommended"] is None and sig["setup"]["kind"] is None

    def test_prefs_change_picks_only(self):
        chain = lrcx_chain()
        a = option_engine.compute(chain, self._metrics(chain), lrcx_chart(), prefs())
        b = option_engine.compute(chain, self._metrics(chain), lrcx_chart(), prefs(**{"credit_vertical.short_delta_hi": 0.40}))
        assert [(r["key"], r["fit"], r["reason_key"]) for r in a["strategies"]] == [(r["key"], r["fit"], r["reason_key"]) for r in b["strategies"]]
        assert a["iv"] == b["iv"] and a["setup"] == b["setup"] and a["headline"] == b["headline"]
        assert a["picks"] != b["picks"]


# ────────────────────────────────────────── the golden fixtures ──────────────────────────────────────────

EXPECTED = json.loads((regen.EXPECTED).read_text(encoding="utf-8")) if regen.EXPECTED.exists() else None


@pytest.mark.skipif(EXPECTED is None, reason="tests/fixtures/options/expected.json not generated yet")
class TestGoldenFixtures:
    @pytest.mark.parametrize("name", ["lrcx", "isrg"])
    def test_engines_reproduce_expected_json(self, name):
        fix = regen.load(name)
        assert fix["chain"].symbol == name.upper() and fix["chain"].n_contracts > 1000 and len(fix["bars"]) > 400
        got = regen.run(fix)
        exp = EXPECTED[name]
        assert got["chart"] == exp["chart"]
        assert got["iv"] == exp["iv"]
        assert got["recommended"] == exp["recommended"] and got["strategies"] == exp["strategies"]
        assert got["picks"] == exp["picks"] and got["sizing"] == exp["sizing"] and got["ticket"] == exp["ticket"]
        assert got["engine"] == exp["engine"]
        assert got["forced"] == exp["forced"]

    def test_lrcx_forced_credit_path_is_sane(self):
        f = EXPECTED["lrcx"]["forced"]
        assert f["recommended"] == "bull_put" and f["bull_put"]["status"] == "ok"
        bound = f["setup"]["zone"][0] - LEVEL_PAD_ATR * EXPECTED["lrcx"]["chart"]["atr"]
        for p in f["bull_put"]["picks"]:
            assert p["legs"][0][1] == "sell" and p["legs"][0][0] <= bound + 1e-6 and p["pop_kind"] == "keep"
            assert p["max_loss"] > 0 and p["chart_stop_pl"] < 0 and p["rule_stop_pl"] == -round(0.2 * p["max_loss"], 2)
            assert len(p["breakevens"]) == 1 and p["constraint_ok"]
        s = f["sizing"]
        assert s["contracts"] == min(x for x in (s["by_chart_stop"], s["by_gap"], s["by_notional"]) if x is not None)
        assert s["line"].startswith(f"{s['contracts']} contract")
        for text in (f["ticket"]["tws"], f["ticket"]["moomoo"]):
            assert "Trigger outside RTH: No" in text and order_ticket.RTH_NOTE in text and "Prices are from 02 Oct 15:59 ET" in text
            assert "Rule stop (no order - the Positions tab watches it)" in text
        assert f["ticket"]["condition_default"] is None and f["ticket"]["condition_dip"]["op"] == "<="
        assert EXPECTED["lrcx"]["iv"]["basis"] == "rank" and EXPECTED["lrcx"]["engine"]["status"] in option_engine.STATUSES

    def test_fixture_files_say_what_is_real(self):
        for name in ("lrcx", "isrg"):
            raw = regen.load(name)["raw"]
            assert raw["iv_series_synthetic"] is True and len(raw["iv_series"]) == 251 and raw["source"] == "cboe"
            assert raw["as_of"].startswith(raw["today"]) and raw["long_bars"][-1]["time"] <= raw["today"]
