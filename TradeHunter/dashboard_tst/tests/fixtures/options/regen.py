"""Regenerate the golden Options fixtures (OPTIONS_MODULE_DESIGN.md II.2.19, II.6).

    cd dashboard_tst
    py tests/fixtures/options/regen.py            # capture LRCX + ISRG live, then recompute expected.json
    py tests/fixtures/options/regen.py --no-fetch # recompute expected.json from the saved captures only

The golden numbers are GENERATED, not typed (integrator ruling, 2026-10-04): the
LRCX / ISRG figures quoted in the design are illustrative and not Black-Scholes-
consistent with each other, so the tests assert against what the engines produce
from THESE files, printed here once into ``expected.json``.

What a fixture holds (``lrcx.json`` / ``isrg.json``):

* a REAL Cboe chain (``option_data.CboeSource``) - every contract row, the header,
  the feed's ``as_of`` and the ET date it describes (``today``);
* ~10 years of REAL daily bars (``services.prices.fetch_daily_ohlc``), from which
  the two-year window the daily reads use is sliced (``ema_setup._last_two_years``);
* the next earnings date (``services.prices.fetch_next_earnings``; None when Yahoo
  did not answer - the engines then say "earnings date unknown");
* a SYNTHETIC daily IV30 history (``iv_series``, 251 points, percent, oldest first,
  today excluded): a capture has no database behind it, so the year of readings
  the rank needs is generated around the chain's own IV30 - flagged
  ``iv_series_synthetic: true`` - placing LRCX near rank 62 and ISRG near rank 41,
  the regimes the design's two worked examples sit in. The chain, the bars and
  the earnings date are real; the rank is not a market fact.

The fixture member runs house rules with ``earnings_rule = defined_risk_only``
(II.2.19: the earnings check is non-blocking and the ticket warns) and NLV
100,000 at 1 % (``option_prefs`` + the sizing of II.2.4).
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASH = HERE.parent.parent.parent
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

FIXTURES = {"lrcx": {"symbol": "LRCX", "rank_target": 0.615, "lo_mult": 0.60, "hi_mult": 1.25},
            "isrg": {"symbol": "ISRG", "rank_target": 0.405, "lo_mult": 0.70, "hi_mult": 1.44}}
NLV = 100000.0
RISK_PCT = 1.0
EXPECTED = HERE / "expected.json"


def _round(obj, nd: int = 4):
    """Floats rounded to ``nd`` places, recursively, so a regenerated file is stable."""
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, nd) for v in obj]
    return obj


def synth_iv_series(iv30: float, *, lo_mult: float, hi_mult: float, n: int = 251) -> list[float]:
    """A deterministic year of readings spanning ``[iv30 x lo_mult, iv30 x hi_mult]``
    (both ends present, so the rank of today's iv30 is exactly ``(1 - lo) / (hi - lo)``)."""
    lo, hi = iv30 * lo_mult, iv30 * hi_mult
    step = 37                                   # coprime with n: every slot 0..n-1 is visited once
    return [round(lo + (hi - lo) * ((i * step) % n) / (n - 1), 4) for i in range(n)]


# ------------------------------------------------------------------- capture
def capture(symbol: str, spec: dict) -> dict:
    from app.services import option_data, prices

    chain = option_data.CboeSource().fetch_chain(symbol, fresh=True, retries=2)
    raw = chain.as_legacy()
    legs = {f"{k[0]}|{k[1]}|{k[2]:g}": v for k, v in raw["legs"].items()}
    long_bars = prices.fetch_daily_ohlc(symbol, rng="10y")
    if not long_bars:
        raise RuntimeError(f"{symbol}: no bars from Yahoo")
    earnings = prices.fetch_next_earnings(symbol)
    iv30 = chain.iv30 if chain.iv30 is not None else 30.0
    return {
        "symbol": symbol,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "today": chain.snap_on, "as_of": raw["as_of"], "as_of_utc": chain.as_of.isoformat(timespec="seconds"),
        "spot": chain.spot, "iv30": chain.iv30, "source": chain.source, "partial": chain.partial,
        "header": raw.get("header") or {}, "legs": legs,
        "long_bars": long_bars, "earnings": earnings,
        "iv_series": synth_iv_series(iv30, lo_mult=spec["lo_mult"], hi_mult=spec["hi_mult"]),
        "iv_series_synthetic": True,
        "note": ("Real Cboe chain, real Yahoo bars and earnings date; the IV history is synthetic (a capture "
                 "has no database), spanning iv30 x %.2f .. iv30 x %.2f so today's rank is about %d."
                 % (spec["lo_mult"], spec["hi_mult"], round(spec["rank_target"] * 100))),
    }


# ------------------------------------------------------------------- load
def load(name: str) -> dict:
    """``{symbol, today, chain (a Chain), bars, long_bars, earnings, iv_series, as_of}`` from a
    saved fixture - what every test and ``run()`` consume."""
    from app.services import ema_setup, option_data

    with (HERE / f"{name}.json").open(encoding="utf-8") as fh:
        fix = json.load(fh)
    legs = {}
    for key, leg in fix["legs"].items():
        exp, right, strike = key.split("|")
        legs[(exp, right, round(float(strike), 3))] = leg
    raw = {"symbol": fix["symbol"], "spot": fix["spot"], "iv30": fix["iv30"], "as_of": fix["as_of"],
           "fetched_at": fix.get("captured_at"), "legs": legs, "header": fix.get("header") or {}}
    chain = option_data.chain_from_legacy(raw, source=fix.get("source") or "cboe")
    long_bars = fix["long_bars"]
    bars = ema_setup._last_two_years(long_bars)
    return {"symbol": fix["symbol"], "today": fix["today"], "as_of": fix["as_of"], "as_of_utc": fix.get("as_of_utc"),
            "chain": chain, "bars": bars, "long_bars": long_bars, "earnings": fix.get("earnings"),
            "iv_series": fix["iv_series"], "iv_series_synthetic": bool(fix.get("iv_series_synthetic")),
            "raw": fix}


def member_prefs(**overrides) -> dict:
    """The fixture member: house rules + ``earnings_rule = defined_risk_only``, NLV 100,000 at 1 %."""
    from app.services import option_prefs

    raw = {"shared": {"earnings_rule": "defined_risk_only"}}
    for k, v in overrides.items():
        block, field = k.split(".")
        raw.setdefault(block, {})[field] = v
    prefs = option_prefs.clean(raw)
    prefs["account"] = {"nlv": NLV, "risk_pct": RISK_PCT, "nlv_source": "prefs"}
    return prefs


# ------------------------------------------------------------------- run
def run(fix: dict, prefs: dict | None = None) -> dict:
    """Every engine over one loaded fixture -> the values ``expected.json`` records."""
    from app.services import (chart_state, opt_legs, option_engine, option_metrics, option_sizing,
                              order_ticket, premium_gauge, strategy_rules, strike_picker)

    prefs = prefs or member_prefs()
    chain, bars, long_bars, today = fix["chain"], fix["bars"], fix["long_bars"], fix["today"]
    expiries = chain.expiries()
    state = chart_state.read(fix["symbol"], bars=bars, long_bars=long_bars, today=today, expiries=expiries,
                             earnings=fix["earnings"])
    metrics = option_metrics.all_for(chain, bars, fix["earnings"], fix["iv_series"], today=today)
    iv = premium_gauge.from_metrics(metrics)
    rec = strategy_rules.recommend(state, iv, prefs, snapshot_expiries=expiries)
    view = opt_legs.chain_view(chain, today=today)
    out = {
        "symbol": fix["symbol"], "today": today, "as_of": fix["as_of"],
        "chart": {
            "trend": state["trend"], "trend_days": state["trend_days"], "atr": state["atr"], "close": state["close"],
            "setup": ({k: state["setup"].get(k) for k in ("kind", "direction", "level", "zone", "touches", "quality")}
                      if state["setup"] else None),
            "setups": [(s["kind"], s["quality"]) for s in state["setups"]],
            "plan": state["plan"], "levels": state["levels"], "structure": (state["structure"] or {}).get("state"),
            "w_uptrend": state["w_uptrend"], "slow_drift": state["slow_drift"], "earnings": state["earnings"],
        },
        "iv": {k: iv.get(k) for k in ("iv30", "hv20", "iv_hv_premium", "iv_rank", "iv_pct", "iv_n", "state", "basis",
                                      "provisional", "iv_front", "iv_back", "term_ratio", "verdict", "verdict_why",
                                      "gates", "earnings_date", "earnings_days")},
        "recommended": rec["recommended"],
        "strategies": [{k: r.get(k) for k in ("key", "fit", "score", "reason_key", "shown", "step")} for r in rec["strategies"]],
        "picks": {}, "sizing": None, "ticket": None, "engine": None,
    }
    for row in rec["strategies"]:
        if row["fit"] in ("recommended", "also_fits") and row["step"] <= strategy_rules.CURRENT_STEP:
            res = strike_picker.pick(row["key"], view, state, iv, prefs, today=today)
            deg = res.get("degenerate") or {}
            out["picks"][row["key"]] = {
                "status": res["status"], "considered": res["considered"], "rules_line": res["rules_line"],
                "degenerate": {k: deg.get(k) for k in ("reason_key", "text")} if deg else None,
                "picks": [{
                    "expiry": p["expiry"], "dte": p["dte"],
                    "legs": [[l["strike"], l["side"], l["right"], l["price"], l["delta"], l["iv"]] for l in p["legs"]],
                    "net": p["net"], "width": p["width"], "max_profit": p["max_profit"], "max_loss": p["max_loss"],
                    "breakevens": p["breakevens"], "pop": p["pop"], "pop_kind": p["pop_kind"], "pop_model": p["pop_model"],
                    "chart_stop": p["chart_stop"], "chart_stop_pl": p["chart_stop_pl"], "rule_stop_pl": p["rule_stop_pl"],
                    "score": p["score"], "constraint_ok": p["constraint"]["ok"], "tier": p["liquidity"]["tier"],
                    "worst_fill": p["liquidity"]["worst_fill"], "why": p["why"], "greeks": p["greeks"],
                } for p in res["picks"]],
            }
    key = rec["recommended"]
    top = None
    if key and out["picks"].get(key, {}).get("status") == "ok":
        res = strike_picker.pick(key, view, state, iv, prefs, today=today)
        top = res["picks"][0]
        sz = option_sizing.size(top, NLV, prefs)
        out["sizing"] = {k: sz.get(k) for k in ("by_chart_stop", "by_gap", "by_notional", "contracts", "stop_t_days",
                                                "stop_iv", "max_loss_pct_nlv", "fires_first", "rule_stop_usd",
                                                "loss_at_stop_usd", "nlv_source", "line", "note")}
        top = dict(top, sizing=sz)
        setup = chart_state.stored_setup(state)
        setup["quotes_as_of"] = fix["as_of"]          # the feed's ET wall-time stamp
        t = order_ticket.build(top, setup, prefs, dip=False, now=None)
        t_dip = order_ticket.build(top, setup, prefs, dip=True, now=None)
        out["ticket"] = {"contracts": t["contracts"], "header": t["header"], "net": t["net"],
                         "condition_default": t["condition"], "condition_dip": t_dip["condition"],
                         "stop": t["stop"], "target": t["target"], "exits_text": t["exits_text"],
                         "warnings": t["warnings"],
                         "tws": order_ticket.render(t, "tws"), "moomoo": order_ticket.render(t, "moomoo")}
    out["forced"] = _forced(fix, state, iv, view, prefs)
    sig = option_engine.compute(chain, metrics, state, prefs)
    out["engine"] = {"status": sig["status"], "headline": sig["headline"], "engine_version": sig["engine_version"],
                     "picks_status": {k: [p.get("status") for p in v] for k, v in sig["picks"].items()},
                     "recommended": sig.get("recommended")}
    return _round(out)


def forced_state(state: dict, *, spot: float, atr: float) -> dict:
    """The live chart with a support bounce PLANTED one ATR under the close (an
    uptrend, three touches, high volume): the chain is real, the setup is not - so
    the credit-spread picker, the sizing and the ticket run on a real chain even on
    a day the live chart carries no setup. Marked ``forced`` in expected.json."""
    from app.services import chart_state

    level = round(spot - atr, 2)
    zone = [round(level - 0.08 * atr, 2), round(level + 0.08 * atr, 2)]
    setup = {"kind": "support_bounce", "direction": "up", "level": level, "zone": zone, "touches": 3,
             "quality": 80, "vol_high": True, "vol_ratio": 1.7, "candle": {"time": state.get("as_of"), "kind": "pin", "low": zone[0]},
             "summary": f"bounce off {level:g} (3 touches) on high volume (planted)"}
    st = dict(state)
    st.update(trend="up", trend_days=max(int(state.get("trend_days") or 0), 34), setup=setup,
              setups=[setup] + [s for s in (state.get("setups") or [])],
              structure={"state": "bullish", "reason": "planted"})
    st["levels"] = dict(state.get("levels") or {}, support=level)
    st["plan"] = chart_state.plan_for("up", spot, atr, zone, resistance=(state.get("levels") or {}).get("resistance"))
    return st


FORCED_MAX_LEG_SPREAD = 1.50     # the forced run's one relaxed rule: a close-of-day chain on a $350 stock quotes $13 puts $0.65-1.35 wide


def _forced(fix: dict, state: dict, iv: dict, view: dict, prefs: dict) -> dict:
    from app.services import chart_state, option_sizing, order_ticket, strategy_rules, strike_picker

    prefs = member_prefs(**{"shared.max_leg_spread": FORCED_MAX_LEG_SPREAD})
    atr = state.get("atr") or 0.03 * (view.get("spot") or 100.0)
    spot = float(view.get("spot") or state.get("close"))
    st = forced_state(state, spot=spot, atr=atr)
    rec = strategy_rules.recommend(st, iv, prefs, snapshot_expiries=sorted(view.get("by_expiry") or {}))
    out = {"prefs": f"house + shared.earnings_rule=defined_risk_only + shared.max_leg_spread={FORCED_MAX_LEG_SPREAD}",
           "setup": st["setup"], "plan": st["plan"], "recommended": rec["recommended"],
           "strategies": [{k: r.get(k) for k in ("key", "fit", "score", "reason_key")} for r in rec["strategies"]],
           "bull_put": None, "sizing": None, "ticket": None}
    res = strike_picker.pick("bull_put", view, st, iv, prefs, today=fix["today"])
    deg = res.get("degenerate") or {}
    out["bull_put"] = {
        "status": res["status"], "considered": res["considered"], "rules_line": res["rules_line"],
        "degenerate": {k: deg.get(k) for k in ("reason_key", "text")} if deg else None,
        "picks": [{
            "expiry": p["expiry"], "dte": p["dte"],
            "legs": [[l["strike"], l["side"], l["right"], l["price"], l["delta"], l["iv"]] for l in p["legs"]],
            "net": p["net"], "width": p["width"], "max_profit": p["max_profit"], "max_loss": p["max_loss"],
            "breakevens": p["breakevens"], "pop": p["pop"], "pop_kind": p["pop_kind"], "pop_model": p["pop_model"],
            "chart_stop": p["chart_stop"], "chart_stop_pl": p["chart_stop_pl"], "rule_stop_pl": p["rule_stop_pl"],
            "score": p["score"], "constraint_ok": p["constraint"]["ok"], "tier": p["liquidity"]["tier"],
            "worst_fill": p["liquidity"]["worst_fill"], "why": p["why"], "greeks": p["greeks"],
            "checks": [[c["name"], c["ok"], c.get("blocking")] for c in p["checks"]],
        } for p in res["picks"]],
    }
    if res["status"] == "ok":
        top = res["picks"][0]
        sz = option_sizing.size(top, NLV, prefs)
        out["sizing"] = {k: sz.get(k) for k in ("by_chart_stop", "by_gap", "by_notional", "contracts", "stop_t_days",
                                                "stop_iv", "max_loss_pct_nlv", "fires_first", "rule_stop_usd",
                                                "loss_at_stop_usd", "nlv_source", "line", "note")}
        top = dict(top, sizing=sz)
        setup = chart_state.stored_setup(st)
        setup["quotes_as_of"] = fix["as_of"]          # the feed's ET wall-time stamp
        t = order_ticket.build(top, setup, prefs, dip=False, now=None)
        t_dip = order_ticket.build(top, setup, prefs, dip=True, now=None)
        out["ticket"] = {"contracts": t["contracts"], "header": t["header"], "net": t["net"],
                         "condition_default": t["condition"], "condition_dip": t_dip["condition"],
                         "stop": t["stop"], "target": t["target"], "exits_text": t["exits_text"],
                         "warnings": t["warnings"],
                         "tws": order_ticket.render(t, "tws"), "moomoo": order_ticket.render(t, "moomoo")}
    return out


def main(argv: list[str]) -> int:
    fetch = "--no-fetch" not in argv
    if fetch:
        for name, spec in FIXTURES.items():
            print(f"capturing {spec['symbol']} ...", flush=True)
            fix = capture(spec["symbol"], spec)
            with (HERE / f"{name}.json").open("w", encoding="utf-8", newline="\n") as fh:
                json.dump(fix, fh, indent=0, separators=(",", ":"))
            print(f"  {spec['symbol']}: {len(fix['legs'])} contracts, spot {fix['spot']}, iv30 {fix['iv30']}, "
                  f"as_of {fix['as_of']}, bars {len(fix['long_bars'])}, earnings {fix['earnings']}")
    expected = {"generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "prefs": "house + shared.earnings_rule=defined_risk_only, NLV 100000 at 1%"}
    for name in FIXTURES:
        fix = load(name)
        expected[name] = run(fix)
        e = expected[name]
        print(f"{name}: trend {e['chart']['trend']} setup {e['chart']['setup'] and e['chart']['setup']['kind']} "
              f"plan {e['chart']['plan']} iv {e['iv']['verdict']} rank {e['iv']['iv_rank']} "
              f"recommended {e['recommended']} picks {{{', '.join(f'{k}: {v['status']}' for k, v in e['picks'].items())}}} "
              f"forced {e['forced']['bull_put']['status']} {len(e['forced']['bull_put']['picks'])} picks")
    with EXPECTED.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(expected, fh, indent=1)
    print(f"wrote {EXPECTED}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
