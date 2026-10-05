"""The order ticket - what the card hands over to paste into TWS or moomoo
(design/options/part_B_engines.md B6; OPTIONS_MODULE_DESIGN.md II.2.5, II.2.6).

``build(pick, setup, prefs, *, dip=False, rejection=None, now=None) -> Ticket`` (a
dict) and ``render(ticket, broker in {"tws", "moomoo"}) -> str``. The platform
places nothing (DESIGN.md security posture): the ticket is text.

The rules the text embodies:

* **the entry carries NO condition by default** - a DAY limit order placed while
  the market is open (limit = the mid, worked down 0.05 at a time, floor = the
  worst net still inside ``credit_pct_min``). A conditional entry placed by an
  absent member fires on ANY fall through its price, a gap through support on bad
  news included, and the broker allows one condition per order, so it cannot be
  bounded from below. "Enter on the dip" is an explicit toggle (``dip=True``),
  printed WITH its crash sentence, and only where a dip level exists (B6.1);
* **the ONE conditional order pushed hard is the chart-stop EXIT** (GTC): the
  trigger on the underlying's LAST, "Trigger outside RTH: No", and the sentence
  that says it fires on the live price during regular hours (unlike the monitor's
  daily close). TWS: Market recommended, the model's mark at the stop as the
  secondary limit; moomoo: two orders in a fixed order - the SHORT leg bought back
  first, then the long leg sold - with the naked-leg sentence;
* take profit at the family's rule (half the credit), GTC; the rule stop and the
  time stop as "no order - the Positions tab watches it";
* the header always states ``as_of`` and the "Press Refresh after 21:30 Malaysia
  time" instruction; ``refresh_first`` through ``clock._us_session_open``;
* a strategy rejected for earnings inside (and not admitted by
  ``defined_risk_only``) gets NO ticket: ``TicketRefused``; any other rejection is
  line 1 of both renderings.

Jargon stays out: "(you are paid; the most you can lose is fixed)", "worst likely
fill", never "defined risk" or "natural". ASCII punctuation throughout, so the
``<pre>`` and a copy into a broker's notes render the same everywhere.
"""
from __future__ import annotations

import datetime as _dt
import math

from . import chart_state, clock, option_prefs, option_words, payoff, strategy_rules
from .opt_constants import DTE_FLOOR, LEVEL_PAD_ATR, LOSS_STOP_FRACTION, MULT, PROFIT_TARGET_FRACTION, STOP_IV_BUMP

TICK = 0.05
FRESH_MAX = 0.005          # ema_setup.FRESH_MAX: a close within 0.5 % of the level IS at the level (no dip to wait for)
DEFAULT_OFFSET_PCT = 0.3   # trade_prefs.DEFAULT_ENTRY_OFFSET_PCT: the Curated "enter near the level" habit
MOOMOO_LIMIT_MULT = 1.15   # the short leg's buy-back limit = the model's value at the stop x this, so it fills in a fast market

HEADER = ("Prices are from {as_of}. Press Refresh after 21:30 Malaysia time (US open) and re-open the "
          "ticket before sending; the credit will have moved.")
RTH_NOTE = ("This order fires on the live price during regular hours, so it can fire on an intraday dip the "
            "close would have survived. If you prefer the close-based rule, leave this order off and act on "
            "the Positions tab's verdict instead.")
CRASH = "This order will also fire if {sym} crashes through {price} on bad news. Only use it while you are watching."
NAKED = "Never sell the long leg before the short leg is closed - you would be short a naked {word}."
WATCHED = "no order - the Positions tab watches it"
SIDE_LINE = {"credit": "(you are paid; the most you can lose is fixed)",
             "debit": "(you pay; the most you can lose is what you paid)"}
CREDIT_FAMILIES = ("credit_vertical", "condor")


class TicketRefused(Exception):
    """No ticket can be built: earnings fall inside the trade and the member's rule
    says no (the strike table is hidden too)."""


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


def _usd(v, up: bool = True) -> str:
    f = _num(v)
    if f is None:
        return "$?"
    n = math.ceil(f - 1e-9) if up else round(f)
    return f"${n:,.0f}"


def _tick_up(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 2)


def _tick_down(x: float) -> float:
    return round(math.floor(x / TICK + 1e-9) * TICK, 2)


def _date(v) -> _dt.date | None:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _tws_date(expiry: str) -> str:
    d = _date(expiry)
    return expiry if d is None else d.strftime("%d %b %y").upper()


def _moomoo_date(expiry: str) -> str:
    d = _date(expiry)
    return expiry if d is None else d.strftime("%Y/%m/%d")


def _as_of_text(as_of) -> str:
    """'02 Oct 16:00 ET' for a feed stamp; '03 Oct's close' when only a date is known."""
    if as_of is None:
        return "an earlier session"
    s = str(as_of)
    if len(s) <= 10:
        d = _date(s)
        return f"{d.day:02d} {d.strftime('%b')} close" if d else s
    return option_words.et_clock(as_of)


def _resolve_as_of(pick: dict, setup: dict, as_of):
    for cand in (as_of, pick.get("as_of"), setup.get("quotes_as_of"), setup.get("chain_as_of"), setup.get("as_of")):
        if cand:
            return cand
    return None


def _rule_allows_earnings(strategy: str, prefs: dict) -> bool:
    rule = ((prefs or {}).get("shared") or {}).get("earnings_rule") if isinstance((prefs or {}).get("shared"), dict) \
        else (prefs or {}).get("earnings_rule")
    return rule == "defined_risk_only" and option_prefs.defined_risk(strategy)


def _flat(prefs: dict | None, strategy: str) -> dict:
    if isinstance(prefs, dict) and isinstance(prefs.get("shared"), dict):
        return option_prefs.for_strategy(prefs, strategy)
    flat = dict(option_prefs.for_strategy(option_prefs.HOUSE, strategy))
    flat.update(prefs or {})
    return flat


def _exits(prefs: dict | None) -> dict:
    """The member's credit exit lines when the caller handed them over
    (``prefs["exits"]`` = ``trade_prefs.read()``), else the house ones."""
    ex = (prefs or {}).get("exits") if isinstance((prefs or {}).get("exits"), dict) else {}
    lf = _num(ex.get("loss_fraction"))
    pt = _num(ex.get("profit_target_pct"))
    df = ex.get("dte_floor")
    return {"loss_fraction": lf if lf is not None else LOSS_STOP_FRACTION,
            "profit_target": (pt / 100.0) if pt is not None else PROFIT_TARGET_FRACTION,
            "dte_floor": int(df) if df is not None else DTE_FLOOR}


def _dip_condition(pick: dict, setup: dict, prefs: dict, symbol: str, bull: bool, spot: float | None) -> dict | None:
    """B6.1's table: a level to wait for, or None (fresh bounce, no setup, a family
    the toggle is not offered to)."""
    kind = setup.get("kind")
    level = _num(setup.get("level"))
    atr = _num(setup.get("atr")) or 0.0
    zone = setup.get("zone") or []
    if spot is None or level is None or kind in (None, "range"):
        return None
    acct = prefs.get("account") if isinstance(prefs.get("account"), dict) else {}
    offset = _num(acct.get("offset_pct"))
    if offset is None:
        offset = _num(prefs.get("offset_pct"))
    if offset is None:
        offset = DEFAULT_OFFSET_PCT
    if kind == "breakout_retest" and len(zone) == 2:
        value = round(float(zone[1]) + LEVEL_PAD_ATR * atr, 2)
        return {"on": symbol, "field": "last", "op": ">=", "value": value,
                "why": f"enter when the retest of {_g(zone[1])} holds and price turns up, not while it is still testing",
                "warning": CRASH.format(sym=symbol, price=f"{value:.2f}")}
    if kind == "failed_support":
        value = round(level - LEVEL_PAD_ATR * atr, 2)
        return {"on": symbol, "field": "last", "op": "<=", "value": value,
                "why": f"confirm the break of {_g(level)} before selling into it",
                "warning": CRASH.format(sym=symbol, price=f"{value:.2f}")}
    run = (spot - level) / level if bull else (level - spot) / level
    if run <= FRESH_MAX:
        return None                                  # the price IS at the level
    if bull:
        value = round(level * (1.0 + offset / 100.0), 2)
        op, side_word = "<=", "above"
    else:
        value = round(level * (1.0 - offset / 100.0), 2)
        op, side_word = ">=", "below"
    name = {"support_bounce": "support", "ema_rebound": "the moving average", "trendline_bounce": "the trend line",
            "resistance_reject": "resistance"}.get(kind, "the level")
    return {"on": symbol, "field": "last", "op": op, "value": value,
            "why": (f"the close ({spot:.2f}) has run {run * 100:.1f}% past the level: enter on the dip to "
                    f"{offset:g}% {side_word} {name} {_g(level)} rather than chase"),
            "warning": CRASH.format(sym=symbol, price=f"{value:.2f}")}


def _model_mark(legs: list[dict], spot_stop: float, as_of, *, days_ahead: float = 0.0) -> tuple[float | None, dict]:
    """The spread's model mark (cost to close per share, positive) at a stock price,
    IV lifted by STOP_IV_BUMP, and each leg's model value - the ticket's secondary
    limits."""
    try:
        pl = payoff._legs(legs)
    except Exception:  # noqa: BLE001
        return None, {}
    per_leg: dict = {}
    total = 0.0
    try:
        for leg in pl:
            v = payoff.leg_value(leg, spot_stop, days_ahead, as_of, STOP_IV_BUMP, strict=False)
            per_leg[(leg.expiry, leg.right, leg.strike)] = round(v, 2)
            total += (-leg.qty) * v              # closing a short leg costs its value; a long leg pays back
    except Exception:  # noqa: BLE001
        return None, per_leg
    return round(total, 2), per_leg


# ------------------------------------------------------------------- build()
def build(pick: dict, setup: dict | None, prefs: dict | None, *, dip: bool = False, rejection: str | None = None,
          now: _dt.datetime | None = None, as_of=None, why: str | None = None, must_happen: str | None = None) -> dict:
    """The Ticket dict (B6.1 / II.2.6) for ONE stored pick.

    ``pick`` carries its read-time ``sizing`` (``option_sizing.size``; ``contracts``
    None / 0 prints as "-" with the sizing note); ``setup`` is the signal's stored
    ``setup`` (or a ChartState); ``prefs`` the member's merged rules; ``dip`` the
    explicit "Enter on the dip" toggle; ``rejection`` the recommender's sentence
    when the strategy was rejected (line 1 of both renderings); ``now`` pins the
    clock for the "Refresh first" check; ``as_of`` the feed stamp when the pick /
    setup do not carry one; ``why`` / ``must_happen`` the strategy row's sentences
    when the caller has them.

    Raises ``TicketRefused`` when earnings fall inside the trade and the member's
    rule does not admit it (the pick's blocking earnings check).
    """
    pick = dict(pick or {})
    prefs = prefs or {}
    strategy = pick.get("strategy") or "bull_put"
    family = pick.get("family") or strategy_rules.FAMILY_OF.get(strategy, "credit_vertical")
    label = strategy_rules.LABELS.get(strategy, strategy)
    state = chart_state.from_stored(setup, symbol=pick.get("symbol"))
    setup_d = state.get("setup") or {}
    setup_d = dict(setup_d, atr=state.get("atr"), close=state.get("close"))
    symbol = pick.get("symbol") or state.get("symbol") or "the stock"
    flat = _flat(prefs, strategy)
    legs = [l for l in (pick.get("legs") or []) if isinstance(l, dict)]
    if not legs:
        raise ValueError("ticket: the pick has no legs")

    # ---- the earnings gate: a blocking earnings check means no ticket at all
    for c in pick.get("checks") or []:
        if isinstance(c, dict) and c.get("name") == "earnings inside expiry" and not c.get("ok") and c.get("blocking"):
            date = (c.get("detail") or "").split(" is ")[0] or "ahead"
            raise TicketRefused(f"No ticket: earnings {date} fall inside this trade and your rule says no.")
    if rejection and "earnings" in rejection.lower() and not _rule_allows_earnings(strategy, prefs):
        raise TicketRefused(f"No ticket: {rejection.rstrip('.')} and your rule says no.")

    credit_family = family in CREDIT_FAMILIES
    net = _num(pick.get("net")) or 0.0
    credit = -net if credit_family else None
    debit = net if not credit_family else None
    width = _num(pick.get("width")) or 0.0
    max_loss = _num(pick.get("max_loss")) or 0.0
    sizing = pick.get("sizing") if isinstance(pick.get("sizing"), dict) else {}
    n = sizing.get("contracts")
    n = int(n) if isinstance(n, (int, float)) and n > 0 else None
    qty = n or 1
    as_of_v = _resolve_as_of(pick, setup or {}, as_of)
    today = _date(as_of_v) or _date(state.get("as_of")) or clock.et_date(now)
    bull = strategy in ("bull_put", "bull_call", "buy_call", "leaps_call", "diagonal_call")
    spot = _num(state.get("close"))
    plan = state.get("plan") or {}
    stop_lvl = _num(pick.get("chart_stop")) if pick.get("chart_stop") is not None else _num(plan.get("stop"))
    exits = _exits(prefs)

    # ---- legs, OCC-style
    short = next((l for l in legs if l.get("side") == "sell"), None)
    long_ = next((l for l in legs if l.get("side") == "buy"), None)
    t_legs = []
    for l in legs:
        t_legs.append({"action": "SELL" if l.get("side") == "sell" else "BUY", "qty": qty,
                       "right": l.get("right"), "strike": _num(l.get("strike")), "expiry": l.get("expiry"),
                       "ref_mid": _num(l.get("price")), "ref_bid": _num(l.get("bid")), "ref_ask": _num(l.get("ask"))})

    # ---- the entry net: limit at the mid, floor / ceiling from the member's floor rule
    if credit_family:
        pct = float(flat.get("credit_pct_min", 25)) / 100.0
        floor = _tick_up(pct * width / (1.0 + pct)) if width else _tick_up(credit * 0.9)
        floor = min(floor, round(credit, 2))
        net_d = {"kind": "credit", "limit": round(credit, 2), "floor": floor,
                 "per_contract_usd": round(credit * MULT, 2), "total_usd": round(credit * MULT * qty, 2),
                 "work": (f"enter at the mid ({credit:.2f}); if unfilled in a few minutes step down {TICK:.2f} at a "
                          f"time, never below {floor:.2f} (never below {_usd(floor * MULT, up=False)} a contract)")}
    else:
        rc = float(flat.get("reward_cost_min", 1.0) or 1.0)
        ceiling = _tick_down(width / (1.0 + rc)) if width else _tick_up(debit * 1.1)
        liq = pick.get("liquidity") if isinstance(pick.get("liquidity"), dict) else {}
        widest = _num(liq.get("widest")) or 0.0
        work_ceiling = min(round(debit + 0.5 * widest, 2), ceiling) if width else ceiling
        net_d = {"kind": "debit", "limit": round(debit, 2), "ceiling": ceiling, "work_ceiling": work_ceiling,
                 "per_contract_usd": round(debit * MULT, 2), "total_usd": round(debit * MULT * qty, 2),
                 "work": (f"enter at the mid ({debit:.2f}); if unfilled in a few minutes step up {TICK:.2f} at a "
                          f"time, never above {work_ceiling:.2f}")}

    # ---- the dip toggle
    condition = _dip_condition(pick, setup_d, prefs, symbol, bull, spot) if dip else None

    # ---- the chart stop (the one conditional order) and the rule stop
    loss_pc = max(0.0, -(_num(pick.get("chart_stop_pl")) or 0.0))
    close_at = None
    per_leg = {}
    if stop_lvl is not None:
        close_at, per_leg = _model_mark(legs, stop_lvl, today)
    stop_chart = None
    if stop_lvl is not None:
        stop_chart = {
            "level": round(stop_lvl, 2), "loss_usd": round(loss_pc * qty, 2), "per_contract_usd": round(loss_pc, 2),
            "gap_usd": round(max_loss * qty, 2),
            "trigger": f"{symbol} last {'<=' if bull else '>='} {stop_lvl:.2f}",
            "trigger_outside_rth": False, "fill": "market", "close_at": close_at, "rth_note": RTH_NOTE,
        }
    rule_pc = max(0.0, -(_num(pick.get("rule_stop_pl")) or 0.0))
    if credit_family and max_loss:
        rule_pc = round(exits["loss_fraction"] * max_loss, 2)
        rule_kind = f"{exits['loss_fraction'] * 100:g}% of max loss"
        rule_close_at = round(credit + rule_pc / MULT, 2)
    else:
        pct_ps = _num(flat.get("premium_stop_pct"))
        rule_kind = f"{pct_ps:g}% of what you paid" if pct_ps is not None else "the rule stop"
        rule_close_at = round(debit - rule_pc / MULT, 2) if debit is not None else None
    stop_rule = {"kind": rule_kind, "loss_usd": round(rule_pc * qty, 2), "per_contract_usd": round(rule_pc, 2),
                 "close_at": rule_close_at}

    # ---- the target
    if credit_family:
        tp = round(credit * exits["profit_target"], 2)
        target = {"chart": None, "rule": {"kind": f"{exits['profit_target'] * 100:g}% of the credit",
                                         "close_at": tp, "profit_usd": round((credit - tp) * MULT * qty, 2)}}
    else:
        t_lvl = _num(plan.get("target"))
        t_chart = None
        if t_lvl is not None:
            dte = int(pick.get("dte") or 0)
            mark_t, _ = _model_mark(legs, t_lvl, today, days_ahead=dte / 2.0)
            t_chart = {"level": round(t_lvl, 2), "close_at": (round(-mark_t, 2) if mark_t is not None else None),
                       "trigger": f"{symbol} last {'>=' if bull else '<='} {t_lvl:.2f}"}
        target = {"chart": t_chart, "rule": None}

    # ---- the time stop
    front = min((l.get("expiry") for l in legs if l.get("expiry")), default=pick.get("expiry"))
    time_stop_on = None
    fd = _date(front)
    if fd is not None:
        time_stop_on = (fd - _dt.timedelta(days=exits["dte_floor"])).isoformat()

    # ---- words
    level_name = {"support_bounce": "support", "ema_rebound": "moving average", "trendline_bounce": "trend line",
                  "breakout_retest": "breakout level", "resistance_reject": "resistance",
                  "failed_support": "broken support"}.get(setup_d.get("kind"), "level")
    lvl = _num(setup_d.get("level"))
    if credit_family:
        exits_text = (f"Close if {symbol} closes {'under' if bull else 'over'} {stop_lvl:.2f} (the {_g(lvl)} {level_name} failed)"
                      if stop_lvl is not None else f"Close if the {level_name} fails")
        exits_text += (f" - or if the spread is marked at {stop_rule['close_at']:.2f} ({rule_kind}). "
                       f"Take profit by buying it back at {target['rule']['close_at']:.2f} (half the credit)."
                       if stop_rule["close_at"] is not None else ".")
        if time_stop_on:
            exits_text += (f" Close or roll with {exits['dte_floor']} days left "
                           f"({strategy_rules.expiry_label(time_stop_on)}) whatever the P/L.")
    else:
        exits_text = (f"Sell if {symbol} closes {'under' if bull else 'over'} {stop_lvl:.2f}" if stop_lvl is not None else "Sell on the chart stop")
        if stop_rule["close_at"] is not None:
            exits_text += f" - or if the position is worth {stop_rule['close_at']:.2f} or less ({rule_kind})."
        if target.get("chart"):
            exits_text += f" Take profit at {target['chart']['level']:.2f} (the chart target)."
    if must_happen is None:
        try:
            must_happen = strategy_rules.render(strategy, strategy_rules.context(state, {}, prefs, pick=pick))[1]
        except Exception:  # noqa: BLE001
            must_happen = None
    rationale = why or f"{label}: {pick.get('rules_line') or ''}".strip(": ")

    warnings: list[str] = []
    for l in legs:
        if l.get("oi") is None:
            warnings.append(f"open interest unknown on {_g(l.get('strike'))}{l.get('right')} - check the OI column in TWS before ordering")
    for c in pick.get("checks") or []:
        if isinstance(c, dict) and c.get("name") == "earnings inside expiry" and not c.get("ok"):
            when = (c.get("detail") or "").split(" is ")[0]
            warnings.append(f"earnings {when} are inside this trade - allowed by your rules; the stop is the only protection")
        if isinstance(c, dict) and c.get("name", "").startswith("days to expiry") and not c.get("ok") and c.get("detail"):
            warnings.append(c["detail"])
    liq = pick.get("liquidity") if isinstance(pick.get("liquidity"), dict) else {}
    if liq.get("tier") == "limit":
        warnings.append("bid/ask at the limit - work the order at the mid and do not chase it")
    if any(l.get("bid") is None or l.get("ask") is None for l in legs):
        warnings.append("no two-sided quote on a leg right now - re-check after the open before ordering")
    if n is None:
        warnings.append(sizing.get("note") or option_words.sizing_line(sizing))

    refresh_first = bool(as_of_v is not None and len(str(as_of_v)) > 10
                         and clock.older_than_last_close(_naive(as_of_v), now) and clock._us_session_open(now))
    ticket = {
        "symbol": symbol, "strategy": strategy, "label": label,
        "side_line": SIDE_LINE["credit" if credit_family else "debit"],
        "contracts": n, "header": HEADER.format(as_of=_as_of_text(as_of_v)),
        "refresh_first": refresh_first, "rejection": rejection or None,
        "legs": t_legs, "net": net_d, "condition": condition, "tif": "DAY",
        "stop": {"chart": stop_chart, "rule": stop_rule}, "target": target,
        "time_stop": {"dte_floor": exits["dte_floor"], "on": time_stop_on},
        "exits_text": exits_text, "rationale": rationale, "must_happen": must_happen,
        "warnings": warnings, "as_of": (as_of_v.isoformat() if isinstance(as_of_v, _dt.datetime) else as_of_v),
        "source": pick.get("source") or (setup or {}).get("quotes_source") or "cboe",
        "_per_leg_at_stop": {f"{k[0]}|{k[1]}|{k[2]:g}": v for k, v in per_leg.items()},
        "_bull": bull, "_family": family,
    }
    return ticket


def _naive(as_of):
    """A feed stamp as the naive-UTC datetime ``clock.older_than_last_close`` reads: a
    datetime as it is (naive = UTC, the as_of convention); an aware ISO string
    converted; a bare ISO string read as New York wall time (the Cboe feed's
    ``last_trade_time`` convention, the same one option_words.et_clock applies)."""
    if isinstance(as_of, _dt.datetime):
        return as_of
    s = str(as_of).strip()
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        return d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    try:
        from .option_data import _et_naive_to_utc
        return _et_naive_to_utc(s)
    except Exception:  # noqa: BLE001
        return d


# ------------------------------------------------------------------- render()
def _qty(ticket: dict) -> str:
    n = ticket.get("contracts")
    return str(n) if n else "-"


def _contracts_words(ticket: dict) -> str:
    n = ticket.get("contracts")
    if not n:
        return "- contracts (not sized yet)"
    return f"{n} contract{'s' if n != 1 else ''}"


def _leg_tws(sym: str, l: dict, qty: str) -> str:
    return f"{l['action']} {qty} {sym} {_tws_date(l['expiry'])} {_g(l['strike'])} {l['right']}"


def _close_tws(sym: str, l: dict, qty: str) -> str:
    action = "BUY" if l["action"] == "SELL" else "SELL"
    return f"{action} {qty} {sym} {_tws_date(l['expiry'])} {_g(l['strike'])} {l['right']}"


def _loss_line(t: dict) -> str | None:
    sc = (t.get("stop") or {}).get("chart")
    if not sc:
        return None
    return f"(you would lose about {_usd(sc['loss_usd'])} here; up to {_usd(sc['gap_usd'])} if the stock gaps past it)"


def render(ticket: dict, broker: str = "tws") -> str:
    """The broker text: ``tws`` (Strategy Builder + the Conditional tab) or ``moomoo``
    (the Options Strategy ticket + Price-condition orders). ``ticket["rejection"]``
    is the first line when present."""
    broker = (broker or "tws").lower()
    if broker not in ("tws", "moomoo"):
        raise ValueError("broker must be tws or moomoo")
    t = ticket
    sym = t["symbol"]
    qty = _qty(t)
    legs = t["legs"]
    net = t["net"]
    credit = net["kind"] == "credit"
    bull = t.get("_bull", True)
    sc, sr = (t.get("stop") or {}).get("chart"), (t.get("stop") or {}).get("rule")
    tgt = t.get("target") or {}
    ts = t.get("time_stop") or {}
    cond = t.get("condition")
    lines: list[str] = []
    if t.get("rejection"):
        lines.append(f"Not recommended today: {t['rejection'].rstrip('.')}.")
    lines.append(t["header"])
    lines.append(f"{sym} - {t['label']} {t['side_line']} - {_contracts_words(t)} - paste into {'TWS' if broker == 'tws' else 'moomoo'}")
    short = next((l for l in legs if l["action"] == "SELL"), None)
    long_ = next((l for l in legs if l["action"] == "BUY"), None)
    word = "put" if (short or long_ or {}).get("right") == "P" else "call"
    combo = len(legs) > 1
    if broker == "tws":
        lines.append(f"ORDER 1 - ENTRY ({'combo, ' if combo else ''}DAY, no condition - place it while the market is open)")
        if combo:
            lines.append("  Strategy Builder -> Vertical: " + " / ".join(_leg_tws(sym, l, qty) for l in legs))
        else:
            lines.append("  " + _leg_tws(sym, legs[0], qty))
        if credit:
            lines.append(f"  Limit CREDIT {net['limit']:.2f} (mid). Work it: if not filled in a few minutes, lower {TICK:.2f} at a time, never below {net['floor']:.2f}.")
        else:
            lines.append(f"  Limit DEBIT {net['limit']:.2f} (mid). Work it: if not filled in a few minutes, raise {TICK:.2f} at a time, never above {net['work_ceiling']:.2f}.")
        if cond:
            lines.append(f"  [Only if 'Enter on the dip' is switched on] Conditional tab -> Add -> Price -> {sym} (STK, SMART) -> Last {cond['op']} {cond['value']:.2f} -> submit.")
            lines.append(f"    {cond['warning']}")
        if sc:
            lines.append(f"ORDER 2 - CHART STOP ({'combo, ' if combo else ''}GTC, conditional) - the one order to set up before you walk away")
            lines.append("  " + " / ".join(_close_tws(sym, l, qty) for l in legs) + (" (close the spread)" if combo else " (close the position)"))
            if sc.get("close_at") is not None:
                lines.append(f"  Type: Market (recommended). Limit {'DEBIT' if credit else 'CREDIT'} {abs(sc['close_at']):.2f} (the model's mark at the stop) is the second choice - it may not fill in a fast market.")
            else:
                lines.append("  Type: Market (recommended).")
            lines.append(f"  Conditional tab -> Add -> Price -> {sym} (STK, SMART) -> Last {'<=' if bull else '>='} {sc['level']:.2f} -> Trigger outside RTH: No -> transmit when true")
            lines.append(f"  {sc['rth_note']}")
            lines.append(f"  {_loss_line(t)}")
        if credit and tgt.get("rule"):
            lines.append(f"ORDER 3 - TAKE PROFIT ({'combo, ' if combo else ''}GTC)")
            lines.append("  " + " / ".join(_close_tws(sym, l, qty) for l in legs) + f" - Limit DEBIT {tgt['rule']['close_at']:.2f} (half the credit kept). No condition.")
        elif tgt.get("chart"):
            tc = tgt["chart"]
            lines.append(f"ORDER 3 - TAKE PROFIT ({'combo, ' if combo else ''}GTC, conditional)")
            lines.append("  " + " / ".join(_close_tws(sym, l, qty) for l in legs)
                         + (f" - Limit CREDIT {tc['close_at']:.2f} (the model's value at the target around mid-life)" if tc.get("close_at") is not None else ""))
            lines.append(f"  Conditional tab -> Add -> Price -> {sym} (STK, SMART) -> Last {'>=' if bull else '<='} {tc['level']:.2f} -> Trigger outside RTH: No")
        if sr and sr.get("close_at") is not None:
            if credit:
                lines.append(f"Rule stop ({WATCHED}): if the spread is marked at {sr['close_at']:.2f} or more ({sr['kind']}), close it.")
            else:
                lines.append(f"Rule stop ({WATCHED}): if the position is worth {sr['close_at']:.2f} or less ({sr['kind']}), close it.")
        if ts.get("on"):
            lines.append(f"Time stop ({WATCHED}): close or roll with {ts['dte_floor']} days left ({strategy_rules.expiry_label(ts['on'])}), whatever the P/L.")
    else:
        lines.append("ENTRY (today, no condition - place it while the market is open)")
        if combo:
            preset = t["label"].replace(" spread", " Spread").replace("Bull ", "Bull ").title() if "spread" in t["label"].lower() else t["label"]
            legs_txt = ", ".join(f"{'sell' if l['action'] == 'SELL' else 'buy'} {sym} {_moomoo_date(l['expiry'])} {_g(l['strike'])} {'Put' if l['right'] == 'P' else 'Call'}" for l in legs)
            lines.append(f"  Options -> Strategy -> {preset}: {legs_txt}, qty {qty}, limit net {'credit' if credit else 'debit'} {net['limit']:.2f} "
                         + (f"(never below {net['floor']:.2f})" if credit else f"(never above {net['work_ceiling']:.2f})") + ". DAY.")
        else:
            l = legs[0]
            lines.append(f"  Options -> {'Buy' if l['action'] == 'BUY' else 'Sell'} {sym} {_moomoo_date(l['expiry'])} {_g(l['strike'])} {'Put' if l['right'] == 'P' else 'Call'}, qty {qty}, limit {net['limit']:.2f}. DAY.")
        if cond:
            lines.append(f"  [Only if 'Enter on the dip' is switched on] Conditional Order -> Price condition -> symbol {sym} -> last {cond['op']} {cond['value']:.2f} -> then the same {'strategy ' if combo else ''}ticket.")
            lines.append(f"    {cond['warning']}")
        if sc:
            lines.append("CHART STOP (GTC, conditional) - " + ("two orders, in this order" if combo else "one order"))
            lines.append(f"  1. Conditional Order -> Price condition -> monitor symbol {sym} -> trigger when last {'<=' if bull else '>='} {sc['level']:.2f} -> Trigger outside RTH: No")
            per_leg = t.get("_per_leg_at_stop") or {}
            if short is not None and combo:
                key = f"{short['expiry']}|{short['right']}|{short['strike']:g}"
                v = per_leg.get(key)
                lim = f", or limit {v * MOOMOO_LIMIT_MULT:.2f} (the model's {v:.2f} x {MOOMOO_LIMIT_MULT:.2f})" if v is not None else ""
                lines.append(f"     -> order: BUY TO CLOSE {_g(short['strike'])} {'Put' if short['right'] == 'P' else 'Call'}, qty {qty} - Market{lim}")
                if long_ is not None:
                    lines.append(f"  2. Then SELL TO CLOSE {_g(long_['strike'])} {'Put' if long_['right'] == 'P' else 'Call'}, qty {qty} - on the same trigger, or by hand once order 1 has filled")
                lines.append(f"  {NAKED.format(word=word)}")
            else:
                l = legs[0]
                lines.append(f"     -> order: {'SELL' if l['action'] == 'BUY' else 'BUY'} TO CLOSE {_g(l['strike'])} {'Put' if l['right'] == 'P' else 'Call'}, qty {qty} - Market")
            lines.append(f"  {sc['rth_note']}")
            lines.append(f"  {_loss_line(t)}")
        if credit and tgt.get("rule"):
            lines.append("TAKE PROFIT (GTC)")
            lines.append(f"  Strategy ticket: close the spread at net debit {tgt['rule']['close_at']:.2f}. No condition.")
        elif tgt.get("chart"):
            tc = tgt["chart"]
            lines.append("TAKE PROFIT (GTC, conditional)")
            lines.append(f"  Conditional Order -> Price condition -> symbol {sym} -> last {'>=' if bull else '<='} {tc['level']:.2f} -> close the position"
                         + (f" at net credit {tc['close_at']:.2f}" if tc.get("close_at") is not None else "") + ".")
        if sr and sr.get("close_at") is not None:
            if credit:
                lines.append(f"Rule stop ({WATCHED}): close if the spread is marked at {sr['close_at']:.2f} or more ({sr['kind']}).")
            else:
                lines.append(f"Rule stop ({WATCHED}): close if the position is worth {sr['close_at']:.2f} or less ({sr['kind']}).")
        if ts.get("on"):
            lines.append(f"Time stop ({WATCHED}): close or roll with {ts['dte_floor']} days left ({strategy_rules.expiry_label(ts['on'])}), whatever the P/L.")
    for w in t.get("warnings") or []:
        lines.append(f"Note: {w}")
    return "\n".join(lines)
