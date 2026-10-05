"""The strike picker - "the best return under MY greek rules" (design/options/
part_B_engines.md B4; OPTIONS_MODULE_DESIGN.md II.2.6, II.2.8).

``pick(strategy_key, chain_view, chart, gauge, prefs, *, today=None) -> PickResult``
- pure, no NLV (sizing is ``option_sizing.size`` afterwards, at read time), no I/O.
Internally: ``enumerate_<family>()`` -> ``apply_constraints()`` -> ``liquidity()`` ->
``score()`` -> top 3, each step reporting what it removed so "no trade today" reads
as a decision, not a blank.

Step 1 builds the credit_vertical enumerator (bull_put AND bear_call, the one
code path mirrored by the right of the leg):

* shorts: every strike whose |delta| sits in the member's band (else the two
  nearest, flagged ``in_band False`` - the ``no_band`` case);
* longs: the next 1..``long_offset_max`` listed strikes further out of the money
  whose width lies inside ``[width_atr_lo, width_atr_hi] x ATR`` - ticker-relative,
  $6-17 on LRCX and $1-3 on a $40 name (CLAUDE.md);
* the chart constraint (hard, switchable off only by ``shared.chart_constraint``):
  a bull put's short strike sits under ``min(support zone low, trend line at expiry)
  - LEVEL_PAD_ATR x ATR``; a bear call's over the mirror;
* liquidity from ``opt_legs.liquidity`` (the playbook's bid/ask tiers, open
  interest scaled to the order, volume a warning only);
* score ``credit / (width - credit) x POP x liquidity_factor`` - the return on the
  money at risk, the design's "credit / max loss x POP"; hard floor ``credit >=
  credit_pct_min/100 x (width - credit)``;
* POP = ``1 - |delta_short|`` (``pop_kind = "keep"``), with the model's lognormal
  figure beside it (``payoff.pop``); the chart stop's loss and the rule stop's loss
  priced at write time (``chart_stop_pl`` / ``rule_stop_pl``) for the sizing, the
  ticket and the payoff pane.

Every other family answers the degenerate case ``not_available_yet`` until its
enumerator lands (steps 2-4); the recommender never recommends those rules, so
the stub is what the card stores for an also-fits chip.

The Pick dict is II.2.8's, key for key; the degenerate ``reason_key`` is one of
``no_chain / no_expiry / no_band / constraint / credit_floor / thin / theta_cap /
extrinsic_cap / no_term / safety`` and the member reads ``option_words``' sentence
for it, never the key.
"""
from __future__ import annotations

import datetime as _dt
import math

from . import chart_state, opt_legs, option_prefs, option_words, payoff, strategy_rules
from .opt_constants import (ALT_NEAR_SHORTS, LEVEL_PAD_ATR, LOSS_STOP_FRACTION, MULT,
                            STOP_IV_BUMP, STOP_TIMES)

TOP_N = 3
WINDOW_SLACK_DAYS = strategy_rules.WINDOW_SLACK_DAYS       # an empty window admits the one expiry within 7 days
DEGENERATE_KEYS = ("no_chain", "no_expiry", "no_band", "constraint", "credit_floor", "thin",
                   "theta_cap", "extrinsic_cap", "no_term", "safety")
BUILT_FAMILIES = ("credit_vertical",)


# ------------------------------------------------------------------- helpers
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _g(v, nd: int = 2) -> str:
    f = _num(v)
    return "?" if f is None else f"{round(f, nd):g}"


def _date(v) -> _dt.date | None:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _flat_prefs(prefs: dict | None, key: str) -> dict:
    """The flat dict the picker reads: ``option_prefs.for_strategy`` over a merged
    read() dict (blocks), or a flat dict over the house values."""
    if isinstance(prefs, dict) and isinstance(prefs.get("shared"), dict):
        return option_prefs.for_strategy(prefs, key)
    flat = dict(option_prefs.for_strategy(option_prefs.HOUSE, key))
    flat.update(prefs or {})
    return flat


def _hash(prefs: dict | None) -> str | None:
    if isinstance(prefs, dict) and isinstance(prefs.get("shared"), dict):
        try:
            return option_prefs.prefs_hash(prefs)
        except Exception:  # noqa: BLE001
            return None
    return None


def _view_of(chain_view, today) -> dict:
    if isinstance(chain_view, dict) and "by_expiry" in chain_view:
        v = chain_view
    else:
        v = opt_legs.chain_view(chain_view, today=today)
    if today is not None:
        v = dict(v)
        v["dte"] = {e: opt_legs.dte_of(e, today) for e in v.get("by_expiry") or {}}
    return v


def _chart_of(chart, symbol: str | None) -> dict:
    """A ChartState from either shape (the nightly's ChartState, the route's stored
    ``setup`` projection)."""
    if not chart:
        return chart_state.from_stored(None, symbol=symbol)
    return chart_state.from_stored(chart, symbol=symbol)


def _earnings_of(gauge: dict | None, chart: dict) -> _dt.date | None:
    g = gauge or {}
    d = _date(g.get("earnings_date"))
    if d is None and isinstance(chart.get("earnings"), dict):
        d = _date(chart["earnings"].get("date"))
    return d


def _today_of(today, view: dict) -> _dt.date:
    d = _date(today)
    if d is not None:
        return d
    # from the view's dte map: expiry - dte
    for e, dte in (view.get("dte") or {}).items():
        ed = _date(e)
        if ed is not None and dte is not None:
            return ed - _dt.timedelta(days=int(dte))
    from .spread_monitor import et_today
    return _dt.date.fromisoformat(et_today())


# ------------------------------------------------------------------- the window
def window(view: dict, lo: int, hi: int, *, monthly_only: bool = False,
           earnings: _dt.date | None = None, earnings_allowed: bool = True) -> dict:
    """``{"expiries": [(expiry, dte, note)], "earnings_removed": bool, "empty_reason"}``:
    every listed expiry with ``lo <= dte <= hi`` (monthly-only filtered when asked);
    when none, the one expiry within ``WINDOW_SLACK_DAYS`` of the window with the
    note "48 DTE, just outside your 30-45"; then the expiries with earnings inside
    are dropped when the rule's earnings policy forbids them."""
    dtes = {e: d for e, d in (view.get("dte") or {}).items() if d is not None and d >= 1}
    if monthly_only:
        from .spread_scan import is_monthly
        dtes = {e: d for e, d in dtes.items() if is_monthly(e)}
    inside = [(e, d, "") for e, d in sorted(dtes.items(), key=lambda x: x[1]) if lo <= d <= hi]
    if not inside:
        near = [(min(abs(d - lo), abs(d - hi)), d, e) for e, d in dtes.items()
                if (lo - WINDOW_SLACK_DAYS) <= d <= (hi + WINDOW_SLACK_DAYS)]
        if near:
            near.sort()
            _, d, e = near[0]
            inside = [(e, d, f"{d} DTE, just outside your {lo}-{hi}")]
    if not inside:
        return {"expiries": [], "earnings_removed": False,
                "empty_reason": f"No listed expiry sits in your {lo}-{hi} day window."}
    if earnings is not None and not earnings_allowed:
        kept = [(e, d, n) for e, d, n in inside if _date(e) is not None and earnings > _date(e)]
        if not kept:
            return {"expiries": [], "earnings_removed": True,
                    "empty_reason": option_words.degenerate_words("no_expiry", lo=lo, hi=hi,
                                                                  date=strategy_rules.expiry_label(earnings.isoformat()))}
        inside = kept
    return {"expiries": inside, "earnings_removed": False, "empty_reason": None}


# ------------------------------------------------------------------- the chart constraint
def _bound(key: str, chart: dict, expiry: str, atr: float | None) -> tuple[float | None, str, dict]:
    """``(bound, detail, parts)`` - the price a bull put's short strike must sit UNDER
    (a bear call's OVER): the support zone's low edge (the resistance zone's high
    edge) / the EMA level / the line's value today, and the trend line's value at
    expiry when a line exists and is not broken, less (plus) ``LEVEL_PAD_ATR x ATR``."""
    setup = chart.get("setup") or {}
    levels = chart.get("levels") or {}
    tl = chart.get("tl") or {}
    pad = LEVEL_PAD_ATR * atr if atr else 0.0
    parts: dict = {"pad": round(pad, 2)}
    kind = setup.get("kind")
    zone = setup.get("zone") or []
    bull = key == "bull_put"
    lvl = None
    name = "support" if bull else "resistance"
    if setup and (setup.get("direction") == ("up" if bull else "down")):
        if kind in ("support_bounce", "breakout_retest", "resistance_reject") and len(zone) == 2 and _num(zone[0 if bull else 1]) is not None:
            lvl = float(zone[0] if bull else zone[1])
            name = {"support_bounce": "support", "breakout_retest": "breakout level",
                    "resistance_reject": "resistance"}[kind]
        elif _num(setup.get("level")) is not None:
            lvl = float(setup["level"])
            name = {"ema_rebound": "moving average", "trendline_bounce": "trend line",
                    "failed_support": "broken support"}.get(kind, name)
    if lvl is None:
        lv = _num(levels.get("support" if bull else "resistance"))
        if lv is not None:
            lvl = lv
    line = None
    if tl and not tl.get("broken") and isinstance(tl.get("value_at"), dict):
        line = _num(tl["value_at"].get(expiry))
        if line is None:
            line = _num(tl.get("value_today"))
    if lvl is None and line is None:
        return None, "no chart level to check against", parts
    if bull:
        base = min(x for x in (lvl, line) if x is not None)
        bound = base - pad
        parts.update(level=lvl, line=line, bound=round(bound, 2), name=name)
        detail = f"under {name} {_g(setup.get('level') or lvl)} (pad {LEVEL_PAD_ATR:g} ATR = {_g(bound, 1)})"
        if line is not None:
            detail += f" and under the trend line at expiry ({_g(line, 1)})"
        return round(bound, 4), detail, parts
    base = max(x for x in (lvl, line) if x is not None)
    bound = base + pad
    parts.update(level=lvl, line=line, bound=round(bound, 2), name=name)
    detail = f"over {name} {_g(setup.get('level') or lvl)} (pad {LEVEL_PAD_ATR:g} ATR = {_g(bound, 1)})"
    if line is not None:
        detail += f" and over the trend line at expiry ({_g(line, 1)})"
    return round(bound, 4), detail, parts


# ------------------------------------------------------------------- pricing bits
def _leg(row: dict, side: str) -> dict:
    """A stored leg (the eleven keys) with side / qty filled; the engine extras ride
    along under ``_x`` for the greeks and the words, never stored."""
    leg = opt_legs.stored_leg(row)
    leg["side"] = side
    leg["qty"] = 1
    leg["_x"] = {k: row.get(k) for k in opt_legs.EXTRA_KEYS}
    return leg


def _clean_legs(legs: list[dict]) -> list[dict]:
    return [{k: v for k, v in l.items() if k != "_x"} for l in legs]


def _greeks(legs: list[dict]) -> dict:
    """Position-signed per share: the sold leg negated."""
    out = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    for l in legs:
        sign = -1.0 if l.get("side") == "sell" else 1.0
        x = l.get("_x") or {}
        d = _num(l.get("delta"))
        if d is not None:
            out["delta"] += sign * d
        for k in ("theta", "vega", "gamma"):
            v = _num(x.get(k))
            if v is not None:
                out[k] += sign * v
    return {k: round(v, 4) for k, v in out.items()}


def _stop_losses(legs: list[dict], *, spot: float, stop: float | None, dte: int, today: _dt.date,
                 sigma_fallback: float | None) -> tuple[float | None, list]:
    """``chart_stop_pl`` (negative $ per contract, 0 when the stop cannot lose on the
    model) as the larger loss over ``STOP_TIMES x dte`` at ``sigma = leg.iv x (1 +
    STOP_IV_BUMP)`` - the relative lift defined once in ``payoff.leg_value`` - and
    the calibrated legs for the POP model."""
    clean = _clean_legs(legs)
    try:
        legs_c = payoff.calibrate(clean, spot, today, sigma_fallback=sigma_fallback, prefer_leg_iv=True)
    except Exception:  # noqa: BLE001
        return None, []
    if stop is None:
        return None, legs_c
    worst = 0.0
    for frac in STOP_TIMES:
        d = frac * dte
        try:
            loss = -payoff.pnl(legs_c, stop, d, today, STOP_IV_BUMP, strict=False)
        except Exception:  # noqa: BLE001
            continue
        worst = max(worst, loss)
    return -round(worst, 2), legs_c


def _pop_model(family: str, legs_c: list, *, spot: float, atr: float | None, today: _dt.date,
               sigma_h: float | None, dte: int) -> float | None:
    if not legs_c or not sigma_h or dte <= 0:
        return None
    try:
        xs = payoff.grid(legs_c, spot, atr)
        ys = payoff.expiry_curve(legs_c, xs, today)
        p = payoff.pop(family, legs_c, spot, sigma_h, dte / 365.0, xs, ys)
    except Exception:  # noqa: BLE001
        return None
    return None if p is None else round(p, 3)


def _atm_sigma(view: dict, expiry: str, spot: float) -> float | None:
    """The ATM sigma of one expiry: the iv of the leg nearest spot (either right)."""
    sides = (view.get("by_expiry") or {}).get(expiry) or {}
    best = None
    for right in ("P", "C"):
        for l in sides.get(right) or []:
            iv = _num(l.get("iv"))
            if iv is None or l.get("strike") is None:
                continue
            dist = abs(float(l["strike"]) - spot)
            if best is None or dist < best[0]:
                best = (dist, iv)
    return best[1] if best else None


# ------------------------------------------------------------------- words
def words(pick: dict, chart: dict | None = None) -> dict:
    """Every number a member sees on a pick, in words (B4.7): the short delta as
    odds, theta / vega per contract and day, the POP sentence (``pop_kind`` decides
    which), collect / pay from the worst likely fill to the mid, and the risk line
    with the chart stop's cost."""
    legs = pick.get("legs") or []
    sym = pick.get("symbol") or (chart or {}).get("symbol") or "the stock"
    family = pick.get("family") or "credit_vertical"
    credit = family in ("credit_vertical", "condor")
    side = "credit" if credit else "debit"
    short = next((l for l in legs if l.get("side") == "sell"), None)
    long_ = next((l for l in legs if l.get("side") == "buy"), None)
    g = pick.get("greeks") or {}
    net = _num(pick.get("net"))
    mid_usd = abs(net) * MULT if net is not None else None
    worst = _num((pick.get("liquidity") or {}).get("worst_fill"))
    worst_usd = abs(worst) * MULT if worst is not None else None
    if credit and short is not None:
        delta = option_words.delta_words(short.get("delta"), "credit", short.get("right") or "P", sym, short.get("strike"))
    elif long_ is not None:
        delta = option_words.delta_words(g.get("delta", long_.get("delta")), "debit", long_.get("right") or "C", sym, long_.get("strike"))
    else:
        delta = "delta unknown"
    out = {
        "delta": delta,
        "theta": option_words.theta_words((g.get("theta") or 0.0) * MULT, side, mid_usd if not credit else None),
        "vega": option_words.vega_words((g.get("vega") or 0.0) * MULT),
        "pop": option_words.pop_words(pick.get("pop"), pick.get("pop_kind") or ("keep" if credit else "profit")),
        "model": option_words.pop_model_words(pick.get("pop_model")),
        "collect": option_words.collect_words(worst_usd, mid_usd, side),
        "risk": option_words.risk_words(pick.get("max_loss"), pick.get("chart_stop"), pick.get("chart_stop_pl")),
    }
    return out


# ------------------------------------------------------------------- credit vertical
def _enumerate_credit(key: str, view: dict, prefs: dict, atr: float | None, win: list) -> tuple[list[dict], dict]:
    """Every short / long pair of the admitted expiries that pays a credit, as
    candidate dicts, plus the diagnostics ``{in_band_any, near_shorts}``."""
    right = "P" if key == "bull_put" else "C"
    lo_d, hi_d = float(prefs.get("short_delta_lo", 0.20)), float(prefs.get("short_delta_hi", 0.30))
    w_lo, w_hi = float(prefs.get("width_atr_lo", 0.5)), float(prefs.get("width_atr_hi", 1.5))
    max_off = int(prefs.get("long_offset_max", 3))
    target = (lo_d + hi_d) / 2.0
    cands: list[dict] = []
    near_shorts: list[dict] = []
    in_band_any = False
    per_expiry: list[tuple[str, int, str, list[dict]]] = []
    for exp, dte, note in win:
        legs = ((view.get("by_expiry") or {}).get(exp) or {}).get(right) or []
        usable = [l for l in legs if _num(l.get("delta")) is not None and _num(l.get("price")) is not None]
        if not usable:
            continue
        shorts = [l for l in usable if lo_d <= abs(float(l["delta"])) <= hi_d]
        if shorts:
            in_band_any = True
        per_expiry.append((exp, dte, note, usable))
        near = sorted(usable, key=lambda l: (abs(abs(float(l["delta"])) - target), float(l["strike"])))[:ALT_NEAR_SHORTS]
        near_shorts.extend([dict(l, _expiry=exp) for l in near])
    for exp, dte, note, usable in per_expiry:
        shorts = [l for l in usable if lo_d <= abs(float(l["delta"])) <= hi_d]
        in_band = True
        if not shorts:
            if in_band_any:
                continue                                 # another expiry has in-band shorts: stay in band
            in_band = False
            shorts = sorted(usable, key=lambda l: (abs(abs(float(l["delta"])) - target), float(l["strike"])))[:ALT_NEAR_SHORTS]
        strikes = sorted({float(l["strike"]) for l in usable})
        by_k = {float(l["strike"]): l for l in usable}
        for short in shorts:
            si = strikes.index(float(short["strike"]))
            for off in range(1, max_off + 1):
                j = si - off if right == "P" else si + off
                if j < 0 or j >= len(strikes):
                    break
                long_ = by_k[strikes[j]]
                width = abs(float(short["strike"]) - float(long_["strike"]))
                credit = float(short["price"]) - float(long_["price"])
                if credit <= 0 or width <= 0:
                    continue
                if atr and not (w_lo * atr - 1e-9 <= width <= w_hi * atr + 1e-9):
                    continue
                cands.append({
                    "expiry": exp, "dte": int(dte), "note": note, "right": right,
                    "short": short, "long": long_, "in_band": in_band,
                    "width": round(width, 4), "credit": round(credit, 4),
                    "max_loss_ps": round(width - credit, 4),
                })
    return cands, {"in_band_any": in_band_any, "near_shorts": near_shorts}


def _build_pick(c: dict, key: str, *, symbol: str, family: str, view: dict, chart: dict, prefs: dict,
                atr: float | None, spot: float, today: _dt.date, earnings: _dt.date | None,
                earnings_allowed: bool, bound_info: tuple, liq: dict, with_model: bool = True) -> dict:
    """One candidate -> the Pick dict of II.2.8."""
    short, long_ = c["short"], c["long"]
    legs = [_leg(short, "sell"), _leg(long_, "buy")]
    credit, width = c["credit"], c["width"]
    net = -round(credit, 4)
    max_profit = round(credit * MULT, 2)
    max_loss = round((width - credit) * MULT, 2)
    sd = abs(float(short["delta"]))
    pop = round(1.0 - sd, 4)
    right = c["right"]
    be = round(float(short["strike"]) - credit, 2) if right == "P" else round(float(short["strike"]) + credit, 2)
    plan = chart.get("plan") or {}
    stop = _num(plan.get("stop"))
    sigma_fb = (_num(view.get("iv30")) or 0.0) / 100.0 or None
    chart_stop_pl, legs_c = _stop_losses(legs, spot=spot, stop=stop, dte=c["dte"], today=today, sigma_fallback=sigma_fb)
    frac = float(prefs.get("loss_fraction", LOSS_STOP_FRACTION) or LOSS_STOP_FRACTION)
    rule_stop_pl = -round(frac * max_loss, 2)
    pop_model = None
    if with_model:
        sigma_h = _atm_sigma(view, c["expiry"], spot) or _num(short.get("iv")) or sigma_fb
        pop_model = _pop_model(family, legs_c, spot=spot, atr=atr, today=today, sigma_h=sigma_h, dte=c["dte"])
    bound, b_detail, _parts = bound_info
    c_ok = c.get("constraint_ok", True)
    if bound is None:
        constraint = {"ok": True, "detail": b_detail}
    else:
        k = float(short["strike"])
        where = ("sits under" if right == "P" else "sits over") if c_ok else ("sits above" if right == "P" else "sits below")
        constraint = {"ok": c_ok, "detail": f"{_g(k)} {where} {_g(bound, 1)} - {b_detail}"}
    # checks
    need = liq.get("oi_needed")
    oi_known = liq.get("min_oi") is not None
    checks = [
        {"name": f"open interest >= {need:,}" if need else "open interest", "ok": bool(oi_known and liq.get("tier") != "thin"),
         "blocking": bool(oi_known), "detail": ("open interest unknown - check in TWS" if not oi_known
                                                 else f"lowest leg {liq.get('min_oi'):,}")},
        {"name": f"bid/ask <= ${float(prefs.get('max_leg_spread', 0.5)):.2f} per leg",
         "ok": liq.get("tier") in ("clean", "limit"), "blocking": True,
         "detail": f"widest leg {_g(liq.get('widest'))}" + (" - at the limit" if liq.get("tier") == "limit" else "")},
        {"name": f"days to expiry {int(prefs.get('dte_lo', 30))}-{int(prefs.get('dte_hi', 60))}",
         "ok": not c.get("note"), "blocking": False, "detail": c.get("note") or f"{c['dte']}d"},
        {"name": f"credit >= {int(prefs.get('credit_pct_min', 25))}% of the risk",
         "ok": c.get("floor_ok", True), "blocking": True,
         "detail": f"{credit:.2f} is {credit / (width - credit) * 100:.0f}% of the {width - credit:.2f} at risk"},
    ]
    if bound is not None:
        checks.append({"name": constraint["detail"].split(" - ", 1)[-1], "ok": c_ok, "blocking": bool(prefs.get("chart_constraint", True)),
                       "detail": constraint["detail"]})
    exp_d = _date(c["expiry"])
    if earnings is not None and exp_d is not None:
        inside = earnings <= exp_d
        checks.append({"name": "earnings inside expiry", "ok": not inside, "blocking": bool(inside and not earnings_allowed),
                       "detail": f"{strategy_rules.expiry_label(earnings.isoformat())} is "
                                 f"{'inside' if inside else 'after'} {strategy_rules.expiry_label(c['expiry'])}"})
    else:
        checks.append({"name": "earnings inside expiry", "ok": True, "blocking": False,
                       "detail": "earnings date unknown - check before you trade"})
    checks.append({"name": f"traded today >= {int(prefs.get('min_leg_volume', 20))} per leg",
                   "ok": liq.get("vol_ok") is not False, "blocking": False,
                   "detail": "no volume reported" if liq.get("vol_ok") is None else ("barely traded today" if liq.get("vol_ok") is False else "ok")})
    pick = {
        "symbol": symbol, "strategy": key, "family": family,
        "legs": _clean_legs(legs),
        "expiry": c["expiry"], "dte": int(c["dte"]),
        "net": net, "width": round(width, 2),
        "max_profit": max_profit, "max_loss": max_loss, "breakevens": [be],
        "pop": pop, "pop_kind": "keep", "pop_model": pop_model,
        "greeks": _greeks(legs),
        "liquidity": {k: liq.get(k) for k in ("tier", "widest", "min_oi", "vol_ok", "worst_fill", "notes")},
        "constraint": constraint,
        "chart_stop": stop, "chart_stop_pl": chart_stop_pl, "rule_stop_pl": rule_stop_pl,
        "checks": checks,
        "score": c.get("score"), "why": [], "words": {},
        "sizing": None, "status": "ok", "rules_line": None, "considered": None, "degenerate": None,
    }
    pick["words"] = words(pick, chart)
    return pick


def _why(picks: list[dict], right: str) -> None:
    """The ``why`` list per pick, from the top-3 set (``bull_put.rank_pairs``'
    vocabulary extended)."""
    if not picks:
        return
    def ratio(p):
        return -p["net"] / (p["width"] + p["net"]) if (p["width"] + p["net"]) > 0 else 0.0
    best_ratio = max(ratio(p) for p in picks)
    best_pop = max(p["pop"] for p in picks)
    shorts = [next(l["strike"] for l in p["legs"] if l["side"] == "sell") for p in picks]
    most_room = min(shorts) if right == "P" else max(shorts)
    least_loss = min(p["max_loss"] for p in picks)
    most_credit = max(p["max_profit"] for p in picks)
    for i, p in enumerate(picks):
        why = []
        if i == 0:
            why.append("best fit to your rules")
        if ratio(p) == best_ratio:
            why.append("most credit per $ risked")
        if p["pop"] == best_pop and len(picks) > 1:
            why.append("highest chance" if i else "highest chance of keeping it")
            if i:
                why.append("safer")
        k = next(l["strike"] for l in p["legs"] if l["side"] == "sell")
        if k == most_room and len(picks) > 1:
            why.append("most room below the price" if right == "P" else "most room above the price")
        if p["max_loss"] == least_loss and len(picks) > 1:
            why.append("smallest max loss")
        if p["max_profit"] == most_credit and len(picks) > 1 and p["max_profit"] != picks[0]["max_profit"]:
            why.append("most credit per contract")
        liq = p.get("liquidity") or {}
        if liq.get("tier") == "limit":
            why.append("bid/ask at the limit")
        if liq.get("min_oi") is None:
            why.append("open interest unknown - check in TWS")
        if liq.get("vol_ok") is False:
            why.append("barely traded today")
        p["why"] = why


def _degenerate(reason_key: str, text: str, nearest: dict | None) -> dict:
    return {"reason_key": reason_key, "text": text, "nearest": nearest,
            "fix": option_words.DEGENERATE_FIX.get(reason_key)}


def _pick_credit(key: str, view: dict, chart: dict, gauge: dict, prefs: dict, today: _dt.date) -> dict:
    family = "credit_vertical"
    symbol = view.get("symbol") or chart.get("symbol") or ""
    spot = _num(view.get("spot")) or _num(chart.get("close"))
    atr = _num(chart.get("atr"))
    right = "P" if key == "bull_put" else "C"
    chart_words = {"atr": atr, "setup": chart.get("setup") or {}, "tl": chart.get("tl")}
    rules_line = option_words.rule_words(prefs, key, chart_words)
    rule = strategy_rules.rule(key)
    earnings = _earnings_of(gauge, chart)
    allowed = (rule.earnings == "any"
               or (rule.earnings == "defined_risk" and prefs.get("earnings_rule") == "defined_risk_only"))
    base = {"strategy": key, "family": family, "prefs_hash": None, "status": "degenerate",
            "picks": [], "considered": 0, "degenerate": None, "rules_line": rules_line, "window": []}

    rows_any = any((sides.get(right) or []) for sides in (view.get("by_expiry") or {}).values())
    if not rows_any or not spot:
        as_of = view.get("as_of") or "-"
        base["degenerate"] = _degenerate("no_chain", option_words.degenerate_words("no_chain", sym=symbol or "this ticker", as_of=as_of), None)
        return base
    win = window(view, int(prefs.get("dte_lo", 30)), int(prefs.get("dte_hi", 60)),
                 monthly_only=bool(prefs.get("monthly_only")), earnings=earnings, earnings_allowed=allowed)
    base["window"] = [{"expiry": e, "dte": d, "note": n} for e, d, n in win["expiries"]]
    if not win["expiries"]:
        base["degenerate"] = _degenerate("no_expiry", win["empty_reason"], None)
        return base

    cands, diag = _enumerate_credit(key, view, prefs, atr, win["expiries"])
    base["considered"] = len(cands)
    liq_kw = dict(min_oi=int(prefs.get("min_oi", 500)), oi_per_contract=int(prefs.get("oi_per_contract", 10)),
                  max_leg_spread=float(prefs.get("max_leg_spread", 0.5)), min_leg_volume=int(prefs.get("min_leg_volume", 20)))
    lo_d, hi_d = float(prefs.get("short_delta_lo", 0.20)), float(prefs.get("short_delta_hi", 0.30))
    pct_min = float(prefs.get("credit_pct_min", 25)) / 100.0

    def finish(c: dict) -> dict:
        # the full norm_leg rows (spread / quote_ok on top) are what liquidity() reads
        liq = opt_legs.liquidity([dict(c["short"], side="sell", qty=1), dict(c["long"], side="buy", qty=1)],
                                 contracts=1, **liq_kw)
        c["liq"] = liq
        c["floor_ok"] = c["credit"] >= pct_min * c["max_loss_ps"] - 1e-9
        c["score"] = round(c["credit"] / c["max_loss_ps"] * (1.0 - abs(float(c["short"]["delta"]))) * liq["factor"], 3)
        return c

    def nearest_of(lst: list[dict], *, with_model: bool = False) -> dict | None:
        if not lst:
            return None
        c = max(lst, key=lambda x: x["score"])
        p = _build_pick(c, key, symbol=symbol, family=family, view=view, chart=chart, prefs=prefs, atr=atr, spot=spot,
                        today=today, earnings=earnings, earnings_allowed=allowed,
                        bound_info=_bound(key, chart, c["expiry"], atr), liq=c["liq"], with_model=with_model)
        p["status"] = "nearest"
        return p

    cands = [finish(c) for c in cands]
    if not diag["in_band_any"]:
        near = " and ".join(f"{_g(l['strike'])}{right} delta {abs(float(l['delta'])):.2f}" for l in diag["near_shorts"][:ALT_NEAR_SHORTS])
        text = option_words.degenerate_words("no_band", lo=lo_d, hi=hi_d, near=near or "none listed")
        base["degenerate"] = _degenerate("no_band", text, nearest_of(cands))
        return base

    # ---- the chart constraint
    use_constraint = bool(prefs.get("chart_constraint", True))
    passed, failed = [], []
    for c in cands:
        bound, detail, parts = _bound(key, chart, c["expiry"], atr)
        k = float(c["short"]["strike"])
        ok = True
        if use_constraint and bound is not None:
            ok = (k <= bound + 1e-9) if right == "P" else (k >= bound - 1e-9)
        c["constraint_ok"] = ok
        c["bound"] = (bound, detail, parts)
        (passed if ok else failed).append(c)
    if not passed:
        c0 = max(failed, key=lambda x: x["score"]) if failed else None
        bound, detail, parts = c0["bound"] if c0 else (None, "", {})
        text = option_words.degenerate_words(
            "constraint", lo=lo_d, hi=hi_d, side="under" if right == "P" else "over", bound=_g(bound, 1),
            why=f"{parts.get('name', 'the level')} {_g(parts.get('level'), 1)} {'less' if right == 'P' else 'plus'} {LEVEL_PAD_ATR:g} ATR",
            near=(f"{_g(c0['short']['strike'])}{right} (delta {abs(float(c0['short']['delta'])):.2f})" if c0 else "none"))
        base["degenerate"] = _degenerate("constraint", text, nearest_of(failed))
        return base

    # ---- liquidity
    liquid = [c for c in passed if c["liq"]["ok"]]
    if not liquid:
        best = max(passed, key=lambda x: x["score"])
        if best["liq"]["tier"] == "wide":
            text = (f"The strikes under your rules are quoted wider than ${float(prefs.get('max_leg_spread', 0.5)):.2f} "
                    f"per leg (the best pair's widest leg is ${best['liq']['widest']:.2f}).")
        else:
            text = option_words.degenerate_words("thin", oi=int(prefs.get("min_oi", 500)))
        base["degenerate"] = _degenerate("thin", text, nearest_of(passed))
        return base

    # ---- the credit floor
    paid = [c for c in liquid if c["floor_ok"]]
    if not paid:
        best = max(liquid, key=lambda c: c["credit"] / c["max_loss_ps"])
        got = best["credit"] / best["max_loss_ps"] * 100.0
        text = option_words.degenerate_words("credit_floor", got=got, want=pct_min * 100.0)
        base["degenerate"] = _degenerate("credit_floor", text, nearest_of(liquid))
        return base

    # ---- score, top 3
    paid.sort(key=lambda c: (-c["score"], abs(abs(float(c["short"]["delta"])) - (lo_d + hi_d) / 2), c["width"], c["dte"]))
    top = paid[:TOP_N]
    picks = [_build_pick(c, key, symbol=symbol, family=family, view=view, chart=chart, prefs=prefs, atr=atr, spot=spot,
                         today=today, earnings=earnings, earnings_allowed=allowed, bound_info=c["bound"], liq=c["liq"])
             for c in top]
    _why(picks, right)
    for p in picks:
        p["rules_line"] = rules_line
        p["considered"] = base["considered"]
        p["degenerate"] = None
    base.update({"status": "ok", "picks": picks})
    return base


# ------------------------------------------------------------------- the entry point
def pick(strategy_key: str, chain_view, chart, gauge, prefs, *, today=None) -> dict:
    """``PickResult`` for one strategy over one chain (B4.8)::

        {strategy, family, prefs_hash, status: "ok" | "degenerate", picks: [Pick x <= 3],
         considered, degenerate: None | {reason_key, text, nearest, fix}, rules_line}

    ``chain_view`` is ``opt_legs.chain_view``'s dict (or any chain it accepts);
    ``chart`` a ChartState or the stored ``setup`` projection; ``gauge`` the
    signal's ``iv`` dict (its ``earnings_date`` is the earnings gate); ``prefs`` the
    merged ``option_prefs.read()`` dict (or a flat ``for_strategy`` dict);
    ``today`` the ET date the DTEs count from (default: the view's).
    """
    key = strategy_key
    if key not in strategy_rules.STRATEGY_KEYS:
        raise KeyError(f"unknown strategy {key!r}")
    family = strategy_rules.FAMILY_OF[key]
    flat = _flat_prefs(prefs, key)
    view = _view_of(chain_view, today)
    symbol = view.get("symbol") or (chart or {}).get("symbol")
    ch = _chart_of(chart, symbol)
    today_d = _today_of(today, view)
    if family not in BUILT_FAMILIES:
        out = {"strategy": key, "family": family, "prefs_hash": _hash(prefs), "status": "degenerate",
               "picks": [], "considered": 0,
               "degenerate": _degenerate("not_available_yet", option_words.DEGENERATE_TEXT["not_available_yet"], None),
               "rules_line": option_words.rule_words(flat, key, {"atr": ch.get("atr"), "setup": ch.get("setup") or {}, "tl": ch.get("tl")}),
               "window": []}
        return out
    out = _pick_credit(key, view, ch, gauge or {}, flat, today_d)
    out["prefs_hash"] = _hash(prefs)
    for p in out.get("picks") or []:
        p["rules_line"] = out["rules_line"]
    return out


def stub(result: dict) -> dict:
    """The ONE Pick-shaped stub the signal stores when nothing passes (B4.8):
    ``nearest`` (greyed, with the one number that failed) or ``none`` (no legs)."""
    deg = result.get("degenerate") or {}
    near = deg.get("nearest")
    if isinstance(near, dict):
        p = dict(near)
        p["status"] = "nearest"
    else:
        p = {"symbol": None, "strategy": result.get("strategy"), "family": result.get("family"),
             "legs": [], "expiry": None, "dte": None, "net": None, "width": None,
             "max_profit": None, "max_loss": None, "breakevens": [], "pop": None, "pop_kind": None,
             "pop_model": None, "greeks": None, "liquidity": None, "constraint": None,
             "chart_stop": None, "chart_stop_pl": None, "rule_stop_pl": None, "checks": [],
             "score": None, "why": [], "words": {}, "sizing": None, "status": "none"}
    p["rules_line"] = result.get("rules_line")
    p["considered"] = result.get("considered")
    p["degenerate"] = {k: deg.get(k) for k in ("reason_key", "text", "fix")} if deg else None
    return p


def stored_picks(result: dict) -> list[dict]:
    """``result["picks"]`` when there are any, else ``[stub(result)]`` - what
    ``option_signal.picks[key]`` holds."""
    picks = result.get("picks") or []
    return picks if picks else [stub(result)]
