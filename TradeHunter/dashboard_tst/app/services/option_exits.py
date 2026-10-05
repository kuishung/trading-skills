"""Exits per family - the Positions monitor over ``option_trades`` (design/options/
part_B_engines.md B7; OPTIONS_MODULE_DESIGN.md II.2.2, II.2.14).

Three functions, the ``spread_monitor`` / ``bull_put.monitor`` shape generalised to
ANY legs:

* ``mark(trade, chain_view, today) -> snap`` - each stored leg priced off the chain
  by ITS OWN right (a bear call marks against the CALL chain), ``mark`` = the cost to
  close per share, ``pl = (-mark - net_entry) x 100 x contracts`` - ONE formula whose
  sign is carried by ``net_entry`` (negative = credit received), the worst-case
  mark, the position greeks in the units a trader reads them in, and the per-leg
  ``{mid, delta, iv}`` the day's check stores. A missing leg becomes ``error``
  naming it (with the listed-expiries hint); the geometry still renders.
* ``grade(trade, snap, chart, prefs, *, earnings=None) -> verdict`` - the
  ``bull_put.monitor`` contract (``state, action, reasons, urgent, *_breach,
  loss_pct, profit_pct``) plus ``stop_breach / target_breach / roll_breach /
  earnings_breach``. First in every family block: the EARNINGS row - a date that
  was unknown or later at entry and now falls inside the trade -> WATCH (urgent).
  Then the family's rows; losing-side lines win ties; WATCH at 80 % of a losing
  line, 50 % of the loss line, within 3 days of a time line.
* ``sweep(db, *, user_id=None, on=None, fresh=True)`` - step 4 of the nightly order:
  every open trade, one chain per underlying, per-member prefs, ``record_check``
  upsert per ET day; urgent verdicts returned for the badge.

The credit_vertical block (step 1): chart stop (the underlying CLOSED beyond
``chart_stop``) CLOSE · short delta ``>= roll_delta`` ROLL (dte > 30) / CLOSE, with
the house line 0.35 adjust / 0.40 close · loss ``>= loss_fraction`` (20 % of max
loss) ROLL / CLOSE · profit ``>= profit_target`` (50 % of the credit) TAKE · time
``dte <= dte_floor`` (21) CLOSE. The other families get the generic chart-stop /
chart-target / premium-stop / time rows until their own blocks land (steps 2-4).
Every line is the member's own (a per-trade override wins over their default,
which wins over the house figure); nothing here places an order.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math

from . import bull_put, chart_state, opt_legs, option_prefs, strategy_rules
from .opt_constants import (ADJUST_DTE, DELTA_ADJUST, DELTA_CLOSE, DTE_FLOOR, LOSS_STOP_FRACTION,
                            MULT, PROFIT_TARGET_FRACTION)

log = logging.getLogger(__name__)

STATES = ("OK", "WATCH", "ROLL", "CLOSE", "TAKE", "UNKNOWN", "EXPIRED")
LOSING = ("ROLL", "CLOSE")
CREDIT_FAMILIES = ("credit_vertical", "condor")
EARNINGS_TEXT = ("Earnings {date} now fall inside this trade (the date was unknown or later when you entered). "
                 "Decide before the close that day.")
WATCH_NEAR = 0.8          # WATCH at 80 % of a losing line
WATCH_LOSS = 0.5          # ... at 50 % of the loss line
WATCH_DAYS = 3            # ... within 3 days of a time line


# ------------------------------------------------------------------- helpers
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _date(v) -> _dt.date | None:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _g(v, nd: int = 2) -> str:
    f = _num(v)
    return "?" if f is None else f"{round(f, nd):g}"


def _attr(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _legs_of(trade) -> list[dict]:
    return [l for l in (_attr(trade, "legs") or []) if isinstance(l, dict)]


def _view_of(chain, today) -> dict:
    if isinstance(chain, dict) and "by_expiry" in chain:
        return chain
    return opt_legs.chain_view(chain, today=today)


def _find_leg(view: dict, leg: dict) -> dict | None:
    """The chain row for a stored leg, by ITS OWN expiry / right / strike."""
    exp = str(leg.get("expiry") or "")[:10]
    right = str(leg.get("right") or "").upper()[:1]
    k = _num(leg.get("strike"))
    rows = ((view.get("by_expiry") or {}).get(exp) or {}).get(right) or []
    if k is None:
        return None
    for r in rows:
        if abs(float(r["strike"]) - k) < 1e-6:
            return r
    return None


# ------------------------------------------------------------------- mark()
def mark(trade, chain_view, today=None) -> dict:
    """Today's snapshot of one tracked trade off a chain (B7.2): ``{symbol, dte,
    back_dte, spot, iv30, as_of, source, mark, mark_worst, pl, pl_worst, profit_pct,
    net_delta, theta, vega, legs: [{mid, delta, iv, strike, right, expiry, side}],
    short_delta, max_loss, max_profit, error}``. Never raises: a leg that is not on
    the chain becomes ``error``, the geometry still comes back."""
    legs = _legs_of(trade)
    sym = (_attr(trade, "symbol") or "").upper()
    n = max(1, int(_attr(trade, "contracts") or 1))
    net_entry = _num(_attr(trade, "net_entry")) or 0.0
    family = _attr(trade, "family") or "credit_vertical"
    today_d = _date(today) or _date(_attr(trade, "checked_on"))
    if today_d is None:
        from .spread_monitor import et_today
        today_d = _dt.date.fromisoformat(et_today())
    exps = sorted({str(l.get("expiry"))[:10] for l in legs if l.get("expiry")})
    front = _attr(trade, "front_expiry") or (exps[0] if exps else None)
    back = _attr(trade, "back_expiry") or (exps[-1] if len(exps) > 1 else None)
    dte = (_date(front) - today_d).days if _date(front) else None
    back_dte = (_date(back) - today_d).days if _date(back) else None
    max_loss = _num(_attr(trade, "max_loss"))
    if max_loss is None and net_entry > 0:
        max_loss = round(net_entry * MULT, 2)
    max_profit = round(-net_entry * MULT, 2) if net_entry < 0 else None
    out = {
        "symbol": sym, "strategy": _attr(trade, "strategy"), "family": family, "contracts": n,
        "front_expiry": front, "back_expiry": back, "dte": dte, "back_dte": back_dte,
        "net_entry": net_entry, "max_loss": max_loss, "max_profit": max_profit,
        "spot": None, "iv30": None, "as_of": None, "source": None,
        "mark": None, "mark_worst": None, "pl": None, "pl_worst": None, "profit_pct": None,
        "net_delta": None, "theta": None, "vega": None, "short_delta": None,
        "legs": [], "error": None,
    }
    if dte is not None and dte < 0:
        out["error"] = "expired"
        return out
    try:
        view = _view_of(chain_view, today_d)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"chain unreadable: {exc}"
        return out
    out["spot"] = _num(view.get("spot"))
    out["iv30"] = _num(view.get("iv30"))
    out["as_of"] = view.get("as_of")
    out["source"] = view.get("source")
    if not legs:
        out["error"] = "no legs stored on this trade"
        return out
    rows = []
    missing = []
    for l in legs:
        r = _find_leg(view, l)
        if r is None:
            missing.append(f"{_g(l.get('strike'))}{l.get('right')} {l.get('expiry')}")
        rows.append(r)
    if missing:
        listed = sorted(view.get("by_expiry") or {})
        hint = ""
        bad = [l.get("expiry") for l in legs if l.get("expiry") not in listed]
        if bad:
            near = [e for e in listed if e >= today_d.isoformat()][:4]
            hint = f" - {bad[0]} is not a listed expiry; nearest: {', '.join(near)}" if near else f" - {bad[0]} is not a listed expiry"
        out["error"] = f"not on the chain: {', '.join(missing)}{hint}"
    mark_v = worst = 0.0
    have_mid = have_worst = True
    nd = th = vg = 0.0
    have_d = have_t = have_v = True
    seen = []
    for l, r in zip(legs, rows):
        q = int(l.get("qty") or 1)
        sign = -1.0 if l.get("side") == "sell" else 1.0        # position sign: long +, short -
        mid = _num(r.get("price")) if r else None
        d = _num(r.get("delta")) if r else None
        iv = _num(r.get("iv")) if r else None
        seen.append({"mid": mid, "delta": d, "iv": iv, "strike": l.get("strike"), "right": l.get("right"),
                     "expiry": l.get("expiry"), "side": l.get("side")})
        if mid is None:
            have_mid = False
        else:
            mark_v += -sign * mid * q                   # closing a short costs its mid; a long pays back
        if r is None or (l.get("side") == "sell" and _num(r.get("ask")) is None) or (l.get("side") != "sell" and _num(r.get("bid")) is None):
            have_worst = False
        else:
            worst += (float(r["ask"]) if l.get("side") == "sell" else -float(r["bid"])) * q
        if d is None:
            have_d = False
        else:
            nd += sign * d * q
        t = _num((r or {}).get("theta"))
        if t is None:
            have_t = False
        else:
            th += sign * t * q
        v = _num((r or {}).get("vega"))
        if v is None:
            have_v = False
        else:
            vg += sign * v * q
        if l.get("side") == "sell" and d is not None and out["short_delta"] is None:
            out["short_delta"] = abs(d)
    out["legs"] = seen
    if have_mid:
        out["mark"] = round(mark_v, 4)
        out["pl"] = round((-mark_v - net_entry) * MULT * n, 2)
        if net_entry < 0:
            out["profit_pct"] = round((-net_entry - mark_v) / (-net_entry), 4)
        elif net_entry > 0:
            out["profit_pct"] = round((-mark_v - net_entry) / net_entry, 4)
    if have_worst:
        out["mark_worst"] = round(worst, 4)
        out["pl_worst"] = round((-worst - net_entry) * MULT * n, 2)
    if have_d:
        out["net_delta"] = round(nd * MULT * n, 2)
    if have_t:
        out["theta"] = round(th * MULT * n, 2)
    if have_v:
        out["vega"] = round(vg * MULT * n, 2)
    return out


# ------------------------------------------------------------------- the lines
def _lines(trade, prefs: dict | None) -> dict:
    """The member's exit lines for this trade: the per-trade override wins, then
    the member's ``trade_prefs`` figures handed in as ``prefs["exits"]`` (never
    hashed), then the house values (0.35 adjust / 0.40 close, 20 %, 50 %, 21)."""
    ex = (prefs or {}).get("exits") if isinstance((prefs or {}).get("exits"), dict) else {}
    rd = _num(_attr(trade, "roll_delta"))
    if rd is None:
        rd = _num(ex.get("roll_delta"))
    lf = _num(_attr(trade, "loss_stop_pct"))
    lf = lf / 100.0 if lf is not None else _num(ex.get("loss_fraction"))
    pt = _num(_attr(trade, "profit_target_pct"))
    pt = pt / 100.0 if pt is not None else (_num(ex.get("profit_target_pct")) / 100.0 if _num(ex.get("profit_target_pct")) is not None else None)
    df = _attr(trade, "dte_floor")
    df = int(df) if df is not None else (int(ex["dte_floor"]) if ex.get("dte_floor") is not None else None)
    return {
        "roll_delta": rd if rd is not None else DELTA_ADJUST,
        "delta_close": DELTA_CLOSE if rd is None else max(DELTA_CLOSE, rd),
        "loss_fraction": lf if lf is not None else LOSS_STOP_FRACTION,
        "profit_target": (pt if pt is not None else PROFIT_TARGET_FRACTION) or None,
        "dte_floor": (df if df is not None else DTE_FLOOR) or None,
        "adjust_dte": ADJUST_DTE,
    }


def _rule_allows_earnings(strategy: str, prefs: dict | None) -> bool:
    p = prefs or {}
    rule = (p.get("shared") or {}).get("earnings_rule") if isinstance(p.get("shared"), dict) else p.get("earnings_rule")
    return rule == "defined_risk_only" and option_prefs.defined_risk(strategy)


def _earnings_row(trade, snap: dict, prefs: dict | None, earnings) -> str | None:
    """The row that applies to every family first: earnings that appeared or moved
    after entry and now fall inside the trade's life (the SHORT leg's expiry for a
    diagonal, the BACK expiry for a calendar)."""
    e = earnings
    if isinstance(e, dict):
        e = e.get("date")
    e_d = _date(e)
    if e_d is None:
        return None
    strategy = _attr(trade, "strategy") or ""
    if strategy == "calendar":
        life_end = _attr(trade, "back_expiry") or _attr(trade, "front_expiry") or snap.get("back_expiry") or snap.get("front_expiry")
    else:
        life_end = _attr(trade, "front_expiry") or snap.get("front_expiry")
    end_d = _date(life_end)
    if end_d is None or e_d > end_d:
        return None
    if _rule_allows_earnings(strategy, prefs) or (strategy == "leaps_call"):
        return None
    at_entry = _date(_attr(trade, "earnings_date_at_entry"))
    if at_entry is not None and at_entry <= e_d:
        return None                                   # known, and not later, at entry
    return EARNINGS_TEXT.format(date=strategy_rules.expiry_label(e_d.isoformat()))


def _totals(snap: dict) -> tuple[float | None, float | None]:
    """The position's max loss / max profit in $: the per-contract figures the snap
    carries times the contracts - the base a total ``pl`` is graded against."""
    n = max(1, int(snap.get("contracts") or 1))
    ml, mp = _num(snap.get("max_loss")), _num(snap.get("max_profit"))
    return (ml * n if ml is not None else None), (mp * n if mp is not None else None)


def _base(snap: dict, lines: dict) -> dict:
    pl = _num(snap.get("pl"))
    max_loss, _mp = _totals(snap)
    loss_pct = max(0.0, -pl) / max_loss if (pl is not None and max_loss) else None
    return {"state": "OK", "action": "", "reasons": [], "urgent": False,
            "delta_breach": False, "loss_breach": False, "profit_breach": False, "dte_breach": False,
            "stop_breach": False, "target_breach": False, "roll_breach": False, "earnings_breach": False,
            "loss_pct": loss_pct, "profit_pct": _num(snap.get("profit_pct")), "lines": lines}


def _chart_of(chart) -> dict:
    if not chart:
        return {}
    if isinstance(chart.get("setup"), dict) or ("setups" in chart and "kind" not in chart):
        return chart
    return chart_state.from_stored(chart)


def _grade_credit(trade, snap: dict, chart: dict, lines: dict) -> dict:
    sym = snap.get("symbol") or (_attr(trade, "symbol") or "").upper()
    bull = (_attr(trade, "strategy") or "bull_put") == "bull_put"
    spot = _num(snap.get("spot"))
    dte = snap.get("dte")
    stop = _num(_attr(trade, "chart_stop"))
    setup = chart.get("setup") or {}
    level = _num(setup.get("level"))
    v = _base(snap, lines)
    # 1. the chart stop: the underlying CLOSED beyond it - the thesis is gone
    if spot is not None and stop is not None and ((spot <= stop) if bull else (spot >= stop)):
        word = ("under", "support") if bull else ("over", "resistance")
        lvl = _g(level) if level is not None else _g(stop)
        v.update(state="CLOSE", urgent=True, stop_breach=True,
                 reasons=[f"{sym} closed at {_g(spot)}, {word[0]} the {lvl} {word[1]}"],
                 action=(f"{sym} closed at {_g(spot)} - {word[0]} the {lvl} {word[1]} the trade was sold against. "
                         "Close it; the thesis is gone, whatever the P/L."))
        return v
    # 2-5. delta / loss / profit / time through bull_put.monitor (the contract)
    if dte is None:
        v.update(state="UNKNOWN", action="No expiry on this trade - nothing to grade.")
        return v
    max_loss_t, max_profit_t = _totals(snap)
    m = bull_put.monitor(short_delta=snap.get("short_delta"), dte=int(dte), pl=snap.get("pl"),
                         max_loss=max_loss_t or 0.0, roll_delta=lines["roll_delta"],
                         loss_fraction=lines["loss_fraction"], adjust_dte=lines["adjust_dte"],
                         max_profit=max_profit_t, profit_target=lines["profit_target"],
                         dte_floor=lines["dte_floor"])
    v.update({k: m.get(k) for k in ("state", "action", "reasons", "urgent", "delta_breach", "loss_breach",
                                    "profit_breach", "dte_breach", "loss_pct", "profit_pct")})
    v["roll_breach"] = bool(v.get("delta_breach"))
    if v["state"] == "UNKNOWN" and spot is not None and stop is not None:
        # no option quote, but the chart line was graded and holds: say that, not "unknown"
        v.update(state="OK", action=(f"No option quote today - the chart stop at {_g(stop)} holds ({sym} at {_g(spot)}). "
                                     "Nothing else to grade until a quote arrives."))
    d = _num(snap.get("short_delta"))
    if d is not None and d >= lines["delta_close"] and v["state"] != "CLOSE":
        v.update(state="CLOSE", urgent=True, delta_breach=True, roll_breach=True,
                 reasons=[f"short delta {d:.2f} is past the {lines['delta_close']:.2f} close line"] + list(v.get("reasons") or []),
                 action=(f"Short delta {d:.2f} is past {lines['delta_close']:.2f} - the strike is being run over; "
                         "close the spread and cut the loss (a roll this deep buys little time)."))
    if spot is not None and stop is not None and v["state"] in ("OK", "WATCH"):
        room = (spot - stop) if bull else (stop - spot)
        atr = _num(chart.get("atr") or setup.get("atr"))
        if atr and room <= 0.5 * atr:
            v["reasons"] = list(v.get("reasons") or []) + [f"{sym} at {_g(spot)} is within half an ATR of the stop {_g(stop)}"]
            if v["state"] == "OK":
                v.update(state="WATCH", action=f"Approaching the chart stop: {sym} at {_g(spot)}, the stop is {_g(stop)}.")
    return v


def _grade_generic(trade, snap: dict, chart: dict, lines: dict, prefs: dict | None) -> dict:
    """The chart-stop / chart-target / premium-stop / time rows every debit family
    shares until its own block lands (steps 2-4)."""
    sym = snap.get("symbol") or (_attr(trade, "symbol") or "").upper()
    strategy = _attr(trade, "strategy") or ""
    bull = strategy in ("bull_call", "buy_call", "leaps_call", "diagonal_call")
    neutral = strategy in ("iron_condor", "calendar")
    spot = _num(snap.get("spot"))
    dte = snap.get("dte")
    stop, target = _num(_attr(trade, "chart_stop")), _num(_attr(trade, "chart_target"))
    meta = _attr(trade, "meta") if isinstance(_attr(trade, "meta"), dict) else {}
    pl = _num(snap.get("pl"))
    debit = _num(_attr(trade, "net_entry")) or 0.0
    n = max(1, int(_attr(trade, "contracts") or 1))
    paid = debit * MULT * n
    v = _base(snap, lines)
    flat = option_prefs.for_strategy(prefs, strategy) if (isinstance(prefs, dict) and isinstance(prefs.get("shared"), dict)) \
        else option_prefs.for_strategy(option_prefs.HOUSE, strategy) if strategy in strategy_rules.STRATEGY_KEYS else {}
    pct = _num(flat.get("premium_stop_pct"))
    if neutral:
        # a condor / calendar has no one-sided stop: its edges were stored in meta at entry
        bes = meta.get("entry_breakevens") or [None, None]
        lo = _num(meta.get("range_low", bes[0]))
        hi = _num(meta.get("range_high", bes[-1]))
        if spot is not None and lo is not None and hi is not None and (spot < lo or spot > hi):
            v.update(state="CLOSE", urgent=True, stop_breach=True,
                     reasons=[f"{sym} closed at {_g(spot)}, outside {_g(lo)}-{_g(hi)}"],
                     action=f"{sym} closed at {_g(spot)}, outside {_g(lo)}-{_g(hi)}. The sideways read is wrong - close.")
            return v
    elif spot is not None and stop is not None and ((spot <= stop) if bull else (spot >= stop)):
        v.update(state="CLOSE", urgent=True, stop_breach=True,
                 reasons=[f"{sym} closed at {_g(spot)}, through the stop {_g(stop)}"],
                 action=f"{sym} closed at {_g(spot)}, through the stop {_g(stop)}. Sell the position.")
        return v
    if pl is not None and paid > 0 and pct is not None and -pl >= pct / 100.0 * paid:
        v.update(state="CLOSE", urgent=True, loss_breach=True,
                 reasons=[f"down {-pl / paid * 100:.0f}% of what you paid (your line is {pct:g}%)"],
                 action=f"Down {-pl / paid * 100:.0f}% of what you paid (your line is {pct:g}%). Close it.")
        return v
    if not neutral and spot is not None and target is not None and ((spot >= target) if bull else (spot <= target)):
        v.update(state="TAKE", urgent=True, target_breach=True,
                 reasons=[f"target {_g(target)} reached"],
                 action=f"Target {_g(target)} reached. Sell, or sell half and move the stop to the entry.")
        return v
    floor = lines["dte_floor"]
    if dte is not None and floor is not None and 0 <= dte <= floor:
        if pl is not None and pl < 0:
            v.update(state="CLOSE", urgent=True, dte_breach=True, reasons=[f"{dte}d left and under water"],
                     action=f"{dte}d left and under water: time is now against you faster than the stock can help. Close it.")
        else:
            v.update(state="WATCH", dte_breach=True, reasons=[f"{dte}d left, in profit"],
                     action=f"{dte}d left, in profit: decide - time decay is fastest from here.")
        return v
    near = []
    if pl is not None and paid > 0 and pct is not None and -pl >= WATCH_NEAR * pct / 100.0 * paid:
        near.append(f"down {-pl / paid * 100:.0f}% of what you paid (line {pct:g}%)")
    if dte is not None and floor is not None and 0 <= dte <= floor + WATCH_DAYS:
        near.append(f"{dte}d left, floor is {floor}d")
    if near:
        v.update(state="WATCH", reasons=near, action="Approaching a line: " + "; ".join(near) + ".")
        return v
    if spot is None and pl is None:
        v.update(state="UNKNOWN", action="No quote today - nothing to grade.")
        return v
    bits = []
    if pl is not None:
        bits.append(f"P/L {pl:+,.0f}")
    if dte is not None:
        bits.append(f"{dte}d left")
    v.update(state="OK", action="Inside every line (" + ", ".join(bits) + "). Hold.")
    return v


def grade(trade, snap: dict, chart=None, prefs: dict | None = None, *, earnings=None) -> dict:
    """The verdict for one trade from today's ``snap`` (``mark``), the chart (the
    stored ``setup`` projection or a ChartState) and the member's merged rules
    (``prefs["exits"]`` carries their ``trade_prefs`` lines when the caller has
    them). ``earnings`` is the date now on file (a ``{"date"}`` dict or a string);
    the earnings row fires WATCH (urgent) when it appeared or moved inside the
    trade since entry, and a losing-side line still wins the state."""
    snap = dict(snap or {})
    chart = _chart_of(chart)
    lines = _lines(trade, prefs)
    if snap.get("spot") is None and chart.get("close") is not None:
        snap["spot"] = chart.get("close")              # no quote today: the chart's close still grades the stop
    if snap.get("error") == "expired" or (snap.get("dte") is not None and snap["dte"] < 0):
        v = _base(snap, lines)
        dte = snap.get("dte")
        v.update(state="EXPIRED", action=f"Expired {abs(dte) if dte is not None else ''}d ago. Mark it closed to take it off the board.".replace("  ", " "))
        return v
    family = _attr(trade, "family") or "credit_vertical"
    if snap.get("spot") is None and snap.get("mark") is None and snap.get("short_delta") is None and not chart.get("close"):
        v = _base(snap, lines)
        dte = snap.get("dte")
        if dte is not None and lines["dte_floor"] is not None and 0 <= dte <= lines["dte_floor"]:
            v.update(state="CLOSE", urgent=True, dte_breach=True, reasons=[f"{dte}d left is at your {lines['dte_floor']}d floor"],
                     action=f"No quote today, but only {dte}d left - at your {lines['dte_floor']}d floor. Close it, or roll to the next cycle.")
        else:
            v.update(state="UNKNOWN", action="No quote today - nothing to grade.")
        earn = _earnings_row(trade, snap, prefs, earnings)
        if earn:
            v.update(state="WATCH" if v["state"] == "UNKNOWN" else v["state"], urgent=True, earnings_breach=True,
                     reasons=[earn] + list(v["reasons"]), action=earn + (" " + v["action"] if v["state"] != "WATCH" else ""))
        return v
    if family == "credit_vertical":
        v = _grade_credit(trade, snap, chart, lines)
    else:
        v = _grade_generic(trade, snap, chart, lines, prefs)
    earn = _earnings_row(trade, snap, prefs, earnings)
    if earn:
        v["earnings_breach"] = True
        v["urgent"] = True
        v["reasons"] = [earn] + list(v.get("reasons") or [])
        if v["state"] not in LOSING:
            v["state"] = "WATCH"
            v["action"] = earn
        else:
            v["action"] = earn + " " + (v.get("action") or "")
    return v


# ------------------------------------------------------------------- record / sweep
def record_check(db, trade, snap: dict, verdict: dict, *, on: str | None = None, source: str | None = None):
    """Upsert today's ``OptionTradeCheck`` for one trade (portable query-then-write,
    never INSERT OR REPLACE). Returns the row; does not commit."""
    from ..models import OptionTradeCheck
    from .spread_monitor import et_today

    day = on or et_today()
    row = (db.query(OptionTradeCheck)
             .filter(OptionTradeCheck.trade_id == trade.id, OptionTradeCheck.checked_on == day)
             .one_or_none())
    if row is None:
        row = OptionTradeCheck(trade_id=trade.id, checked_on=day)
        db.add(row)
    v = verdict or {}
    row.spot = snap.get("spot")
    row.mark = snap.get("mark")
    row.pl = snap.get("pl")
    row.loss_pct = v.get("loss_pct")
    row.profit_pct = v.get("profit_pct") if v.get("profit_pct") is not None else snap.get("profit_pct")
    row.dte = snap.get("dte")
    row.back_dte = snap.get("back_dte")
    row.net_delta = snap.get("net_delta")
    row.theta = snap.get("theta")
    row.vega = snap.get("vega")
    row.legs = [{"mid": l.get("mid"), "delta": l.get("delta"), "iv": l.get("iv")} for l in (snap.get("legs") or [])]
    row.state = (v.get("state") or "UNKNOWN")[:10]
    row.action = v.get("action")
    row.reasons = list(v.get("reasons") or [])
    row.urgent = bool(v.get("urgent"))
    row.source = (source or snap.get("source") or "cboe")[:12]
    row.error = snap.get("error")
    return row


def _latest_setup(db, symbol: str) -> tuple[dict, dict]:
    """The newest stored signal's ``setup`` and ``iv`` for a symbol (house row or any)."""
    from ..models import OptionSignal

    row = (db.query(OptionSignal).filter(OptionSignal.symbol == symbol)
             .order_by(OptionSignal.snap_on.desc(), OptionSignal.id.desc()).first())
    if row is None:
        return {}, {}
    return (row.setup if isinstance(row.setup, dict) else {}), (row.iv if isinstance(row.iv, dict) else {})


def sweep(db, *, user_id: int | None = None, on: str | None = None, fresh: bool = True) -> dict:
    """Check every OPEN ``option_trades`` row and record the result (step 4 of the
    nightly order). One chain fetch per underlying (``option_data.fetch_chain``, the
    stored snapshot as the fallback), per-member prefs + ``trade_prefs`` exit lines,
    the stored signal's setup / earnings, ``setup_for(sym, deep=True)`` only for
    underlyings holding a LEAPS / diagonal (the weekly trend). Returns ``{checked_on,
    trades, states, actionable}``; commits."""
    from ..models import OptionTrade
    from . import option_store, trade_prefs
    from .spread_monitor import et_today

    day = on or et_today()
    q = db.query(OptionTrade).filter(OptionTrade.status == "open")
    if user_id is not None:
        q = q.filter(OptionTrade.user_id == user_id)
    trades = q.order_by(OptionTrade.symbol, OptionTrade.id).all()
    chains: dict[str, object] = {}
    weekly: dict[str, bool | None] = {}
    prefs_by_user: dict[int, dict] = {}
    states: dict[str, int] = {}
    actionable: list[dict] = []
    for t in trades:
        sym = (t.symbol or "").upper()
        if sym not in chains:
            chain = None
            try:
                from . import option_data
                chain = option_data.fetch_chain(sym, fresh=fresh, retries=1)
            except Exception as exc:  # noqa: BLE001 - the stored snapshot still prices it
                log.warning("sweep %s: live chain failed (%s); using the stored snapshot", sym, exc)
                try:
                    chain = option_store.latest_chain(db, sym)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("sweep %s: stored chain failed too: %s", sym, exc2)
            chains[sym] = chain
        if t.user_id not in prefs_by_user:
            try:
                p = option_prefs.read(db, t.user)
                p["exits"] = trade_prefs.read(t.user)
            except Exception:  # noqa: BLE001
                p = dict(option_prefs.HOUSE)
            prefs_by_user[t.user_id] = p
        prefs = prefs_by_user[t.user_id]
        setup, iv = _latest_setup(db, sym)
        if t.strategy in ("leaps_call", "diagonal_call") and sym not in weekly:
            try:
                from . import ema_setup
                weekly[sym] = ema_setup.setup_for(sym, deep=True).get("w_uptrend")
            except Exception:  # noqa: BLE001
                weekly[sym] = None
        if sym in weekly and isinstance(setup, dict):
            setup = dict(setup, w_uptrend=weekly[sym])
        chain = chains.get(sym)
        if chain is None:
            snap = mark(t, {"by_expiry": {}, "spot": None}, day)
            snap["error"] = snap.get("error") or "no chain today"
        else:
            snap = mark(t, chain, day)
        earnings = iv.get("earnings_date") if isinstance(iv, dict) else None
        verdict = grade(t, snap, setup, prefs, earnings=earnings)
        record_check(db, t, snap, verdict, on=day, source=snap.get("source") or "cboe")
        st = verdict.get("state") or "UNKNOWN"
        states[st] = states.get(st, 0) + 1
        if verdict.get("urgent"):
            actionable.append({"user_id": t.user_id, "trade_id": t.id, "symbol": sym, "strategy": t.strategy,
                               "front_expiry": t.front_expiry, "contracts": t.contracts, "dte": snap.get("dte"),
                               "pl": snap.get("pl"), "state": st, "action": verdict.get("action")})
    db.commit()
    return {"checked_on": day, "trades": len(trades), "states": states, "actionable": actionable}
