"""Every member-facing sentence of the Options module, in one place.

A greek, a rate or a rule never reaches a member as a bare number: each one is
accompanied by a sentence from this module (design/options/part_D_ui.md D2.7;
OPTIONS_MODULE_DESIGN.md II.2.6 / II.2.8). All are functions so the numbers are
filled in; the templates never compose them, the engines never restate them.

Three strings are the ONE spelling of their kind on the platform and have no second
variant anywhere (II.2.6, II.2.8):

* ``IV_UNKNOWN`` - "We cannot yet say whether options are expensive - {n} of 60 days
  of history. If you have TWS on this PC, press Live to load a year." (the gauge,
  the card, the basket cell and the empty states all print this one);
* ``pop_words(pop, "keep")`` - the credit sentence;
* ``pop_words(pop, "profit")`` - the debit sentence.

``headline(setup, iv, strategies)`` is composed by the engine at WRITE time and
stored in ``option_signal.headline``; a chip click never changes it.

House rules: sentence case, plain words, no "step N" / "phase" anywhere (the
build-phase words never reach a member), ASCII punctuation so a ``<pre>`` or a
Telegram message renders the same everywhere.
"""
from __future__ import annotations

import datetime as _dt
import math

from . import strategy_rules
from .opt_constants import IV_RANK_MIN_OBS

# ---------------------------------------------------------------- the ONE strings
IV_UNKNOWN = ("We cannot yet say whether options are expensive - {n} of %d days of history. "
              "If you have TWS on this PC, press Live to load a year." % IV_RANK_MIN_OBS)

POP_KEEP = ("About a {p}% chance of keeping the credit - an estimate from today's option prices "
            "(the short strike's delta), not a promise. Earnings, news and gaps are not in that number.")
POP_PROFIT = ("About a {p}% chance of profit if held to expiry, at today's volatility; this trade is "
              "managed by the chart stop and target, so the real odds depend on the move, not this number.")
POP_KEEP_SHORT = "about {p}% chance of keeping it (estimate)"        # Telegram
POP_PROFIT_SHORT = "about {p}% chance of profit (estimate)"

NLV_MISSING = "sized once you tell us the account value (My rules -> Shared)"
NOT_EVEN_ONE = "Not even one contract fits your {risk}% - lower the risk or choose a narrower spread"
STOP_LOSES_NOTHING = "the chart stop loses nothing on the model; sized by the gap and 10% caps"

TRACKING_ONLY = "Tracking only: TradeHunter never sends an order."

# The conclusion per recommended strategy (D2.7), after the gauge clause.
CONCLUSION = {
    "bull_put": "so you're paid to sell a put spread below that support",
    "bear_call": "so you're paid to sell a call spread above that resistance",
    "buy_call": "so a call is cheap enough to buy here, with the stop just under the setup",
    "buy_put": "so a put is cheap enough to buy on the breakdown",
    "bull_call": "so a call spread capped at the target costs less than a plain call",
    "bear_put": "so a put spread capped at the target costs less than a plain put",
    "leaps_call": "so a long-dated deep call can stand in for the stock",
    "iron_condor": "so you're paid to sell both sides of the range",
    "calendar": "so selling the near month against a later one collects the difference",
    "diagonal_call": "so a long-dated call can fund selling monthly calls under the resistance",
}
NOTHING_FITS = "so there is nothing to do today - check again tomorrow"

# The basket column's short idea word per strategy key.
IDEA_SHORT = {
    "bull_put": "sell put", "bear_call": "sell call", "buy_call": "buy call", "buy_put": "buy put",
    "bull_call": "call sprd", "bear_put": "put sprd", "leaps_call": "LEAPS", "iron_condor": "condor",
    "calendar": "calendar", "diagonal_call": "diagonal",
}

# The degenerate picker cases (II.2.8 vocabulary) as the member reads them; the
# picker fills {..} with its own numbers through degenerate_words().
DEGENERATE_TEXT = {
    "no_chain": "No option data for {sym} (as of {as_of}). Press Refresh.",
    "no_expiry": "Every {lo}-{hi} day expiry has earnings {date} inside it.",
    "no_band": "No strike sits in your delta {lo:.2f}-{hi:.2f} band; nearest: {near}.",
    "constraint": ("No strike in your band ({lo:.2f}-{hi:.2f}) sits {side} {bound} ({why}): the nearest "
                   "{side} it is {near} - the market is paying you to sell closer than the chart allows."),
    "credit_floor": "The best pair pays {got:.0f}% of what it risks; your minimum is {want:.0f}%.",
    "thin": "The strikes under your rules are too thin (open interest under {oi:,}).",
    "theta_cap": "Every {lo}-{hi} day {right} in your band loses more than {cap:g}% a day.",
    "extrinsic_cap": ("No {lo}-{hi} month call is deep enough: the least time value is {got:.1f}% of the "
                      "share price (cap {cap:g}%)."),
    "no_term": "The near-term month is cheaper than the later one in every pair - no calendar edge today.",
    "safety": "No short call under resistance {res} covers the long call's cost if the stock rips.",
    "not_available_yet": "That strategy is not in TradeHunter yet.",
}
DEGENERATE_FIX = {
    "no_chain": None,
    "no_expiry": "wait until after earnings, or allow defined-risk trades through earnings in My rules -> Shared",
    "no_band": "widen the band",
    "constraint": "widen the band downward, or switch off the chart rule (not recommended - the drawer shows the consequence sentence before Save)",
    "credit_floor": "lower the minimum, or wait for IV to rise",
    "thin": "lower the OI floor, or pick a more liquid name",
    "theta_cap": "go further out in time, or raise the ceiling",
    "extrinsic_cap": "raise the cap, or wait for IV to fall",
    "no_term": None,
    "safety": "a nearer resistance, or a lower long delta",
    "not_available_yet": None,
}


# -------------------------------------------------------------------- helpers
def _f(v, nd: int = 2) -> str:
    """A price as a member reads it: 340 / 336.2 / 327.9 (never 340.0)."""
    try:
        return f"{round(float(v), nd):g}"
    except (TypeError, ValueError):
        return "?"


def _p(v) -> str:
    """A fraction 0..1 as a whole percent: 0.75 -> '75'."""
    try:
        return f"{round(float(v) * 100.0):.0f}"
    except (TypeError, ValueError):
        return "?"


def _usd(v, up: bool = False) -> str:
    """$1,580 - rounded UP to the dollar when ``up`` (a stated loss is never understated)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "$?"
    n = math.ceil(f - 1e-9) if up else round(f)
    return f"${n:,.0f}"


def expiry_label(expiry: str) -> str:
    """'2026-11-20' -> 'Nov 20' (the label every member string uses)."""
    return strategy_rules.expiry_label(expiry)


def label(key: str) -> str:
    """The member-facing strategy name ('Buy LEAPS', never 'Buy LEAPS call')."""
    return strategy_rules.LABELS.get(key, str(key).replace("_", " "))


def chip_text(reason_key: str | None) -> str | None:
    """The chip's short form of a rejection key (II.2.6), None when not rejected."""
    if not reason_key:
        return None
    return strategy_rules.CHIP_TEXT.get(reason_key, str(reason_key).replace("_", " "))


def idea_short(key: str | None) -> str:
    return IDEA_SHORT.get(key or "", "-")


# ------------------------------------------------------------ greeks and rates
def delta_words(d, side: str, right: str = "P", symbol: str | None = None, strike=None) -> str:
    """Credit side: 'delta 0.25 - about a 1-in-4 chance LRCX is below 330 at expiry; put another
    way, roughly a 75% chance of keeping the credit.' Debit side: 'delta 0.65 - the option moves
    about 65 cents for every $1 the stock moves; one contract behaves like about 65 shares.'"""
    try:
        a = abs(float(d))
    except (TypeError, ValueError):
        return "delta unknown"
    if side == "credit":
        n = max(1, round(1.0 / a)) if a > 0 else None
        where = "below" if (right or "P").upper().startswith("P") else "above"
        who = symbol or "the stock"
        at = f" {_f(strike)}" if strike is not None else " this strike"
        odds = f"about a 1-in-{n} chance" if n else "a small chance"
        return (f"delta {a:.2f} - {odds} {who} is {where}{at} at expiry; put another way, "
                f"roughly a {100 - round(a * 100):.0f}% chance of keeping the credit")
    cents = round(a * 100)
    return (f"delta {a:.2f} - the option moves about {cents} cents for every $1 the stock moves; "
            f"one contract behaves like about {cents} shares")


def theta_words(t_per_day_usd, side: str, premium_usd=None) -> str:
    """Sellers: 'theta +$6/day - time is paying you about $6 a day while the stock sits still.'
    Buyers: 'theta -$9/day - waiting costs about $9 a day (0.8% of what you paid); the stock has
    to move enough to pay for that.'"""
    try:
        t = float(t_per_day_usd)
    except (TypeError, ValueError):
        return "theta unknown"
    a = abs(t)
    if side == "credit" or t > 0:
        return f"theta +${a:,.0f}/day - time is paying you about ${a:,.0f} a day while the stock sits still"
    pct = ""
    try:
        if premium_usd and float(premium_usd) > 0:
            pct = f" ({a / float(premium_usd) * 100:.1f}% of what you paid)"
    except (TypeError, ValueError):
        pct = ""
    return (f"theta -${a:,.0f}/day - waiting costs about ${a:,.0f} a day{pct}; the stock has to move "
            "enough to pay for that")


def vega_words(v_usd) -> str:
    """'vega $18 - if implied volatility rises one point this position loses about $18 (you are
    short volatility).' / '... gains about $22.'"""
    try:
        v = float(v_usd)
    except (TypeError, ValueError):
        return "vega unknown"
    a = abs(v)
    if v < 0:
        return (f"vega ${a:,.0f} - if implied volatility rises one point this position loses about "
                f"${a:,.0f} (you are short volatility)")
    return f"vega ${a:,.0f} - if implied volatility rises one point this position gains about ${a:,.0f}"


def gamma_words(g) -> str:
    """Shown only inside the full-chain expander, never on the card."""
    try:
        a = abs(float(g))
    except (TypeError, ValueError):
        return "gamma unknown"
    return (f"gamma {a:.2f} - the delta changes by about {a:.2f} for each $1 move; small means the "
            "trade's risk changes slowly")


def iv_words(iv_fraction, spot, dte) -> str:
    """'implied volatility 46% - the market's guess at how much the stock will move in a year;
    about +/-$18 (5.2%) over this trade.'"""
    try:
        iv, s, d = float(iv_fraction), float(spot), float(dte)
    except (TypeError, ValueError):
        return "implied volatility unknown"
    move = s * iv * math.sqrt(max(d, 0) / 365.0)
    pct = move / s * 100.0 if s else 0.0
    return (f"implied volatility {iv * 100:.0f}% - the market's guess at how much the stock will move "
            f"in a year; about +/-${move:,.0f} ({pct:.1f}%) over this trade")


def iv_rank_words(iv: dict) -> str:
    """The IV line under the gauge, by basis. ``basis == 'unknown'`` prints the ONE
    IV-unknown sentence (IV_UNKNOWN); there is no second variant anywhere."""
    iv = iv or {}
    basis = iv.get("basis")
    n = int(iv.get("iv_n") or 0)
    if basis == "rank":
        r = iv.get("iv_rank")
        span = "over the last year" if iv.get("state") == "ok" else f"over {n} days"
        return (f"IV rank {_f(r, 0)} {span} ({n} days) - today's IV sits {_f(r, 0)}% of the way from the "
                f"period's lowest to its highest. Above 50 options are expensive (sellers are paid); "
                "below 30 they are cheap by this stock's own standards")
    if basis == "percentile":
        p = iv.get("iv_pct")
        return (f"IV percentile {_f(p, 0)} over the last {n} days (not a full year yet) - IV was lower "
                f"than today on {_f(p, 0)}% of those days")
    if basis == "provisional":
        return (f"IV {_f(iv.get('iv30'), 0)}% against {n} days of history - too short to rank; nothing "
                "here is firm yet")
    return IV_UNKNOWN.format(n=n)


def iv_unknown_words(n: int) -> str:
    """The ONE IV-unknown sentence with the day count filled in."""
    return IV_UNKNOWN.format(n=int(n or 0))


def iv_pct_words(iv: dict) -> str:
    iv = iv or {}
    return (f"IV percentile {_f(iv.get('iv_pct'), 0)} - IV was lower than today on "
            f"{_f(iv.get('iv_pct'), 0)}% of the past {int(iv.get('iv_n') or 0)} trading days")


def hv_words(iv: dict) -> str:
    return (f"realised volatility {_f((iv or {}).get('hv20'), 0)}% - how much the stock actually "
            "moved over the last 20 days, annualised")


def iv_hv_words(iv: dict) -> str | None:
    """'IV 46% vs realised 38% - options are priced for 21% more movement than the stock has
    actually shown; sellers are paid for that gap.' The figure is the RATIO iv30 / hv20 minus one."""
    iv = iv or {}
    prem = iv.get("iv_hv_premium")
    if prem is None or iv.get("iv30") is None or iv.get("hv20") is None:
        return None
    prem = float(prem)
    head = f"IV {_f(iv['iv30'], 0)}% vs realised {_f(iv['hv20'], 0)}% - options are priced for "
    if prem >= 1.0:
        return head + (f"{(prem - 1) * 100:.0f}% more movement than the stock has actually shown; "
                       "sellers are paid for that gap")
    return head + f"{(1 - prem) * 100:.0f}% less movement than the stock has shown; buyers are getting it cheap"


def term_words(iv: dict) -> str | None:
    """The term chip's long form from the signal's iv dict; None when the ratio is unknown.
    (premium_gauge.term_words is the short chip text from the raw ratio.)"""
    iv = iv or {}
    t = iv.get("term_ratio")
    if t is None:
        return None
    t = float(t)
    if t >= 1.05:
        return (f"near-term options are dearer than later ones (front IV {_f(iv.get('iv_front'), 0)}% vs "
                f"back {_f(iv.get('iv_back'), 0)}%) - the market expects an event before the first expiry")
    if t <= 0.95:
        return ("later options are dearer than near ones - nothing special is priced in soon; "
                "calendars are not paid here")
    return "near and later options are priced alike"


def oi_words(oi, rule) -> str:
    if oi is None:
        return f"open interest unknown - the feed did not say; your rule is at least {int(rule):,}"
    return (f"open interest {int(oi):,} - contracts outstanding at this strike. You need enough to get "
            f"out again; your rule is at least {int(rule):,}")


def width_words(w, rule) -> str:
    if w is None:
        return "bid/ask unknown - no two-sided quote on this leg right now"
    return (f"bid/ask ${float(w):.2f} wide - the cost of getting in and out. Your rule allows up to "
            f"${float(rule):.2f}; wider than that eats the edge")


def pop_words(pop, pop_kind: str) -> str:
    """The ONE sentence per pop_kind (II.2.8): keep -> the credit sentence, profit -> the
    debit sentence. The number is a fraction 0..1."""
    p = _p(pop)
    return (POP_KEEP if pop_kind == "keep" else POP_PROFIT).format(p=p)


def pop_short(pop, pop_kind: str) -> str:
    """The Telegram form: 'about 75% chance of keeping it (estimate)'."""
    return (POP_KEEP_SHORT if pop_kind == "keep" else POP_PROFIT_SHORT).format(p=_p(pop))


def pop_model_words(m) -> str | None:
    """'model estimate 73%' - the secondary figure beside the big one."""
    return None if m is None else f"model estimate {_p(m)}%"


def max_loss_words(x, family: str) -> str:
    if family in ("credit_vertical", "condor"):
        return (f"the most you can lose: ${float(x):,.0f} per contract (the width minus the credit), "
                "if the stock is past both strikes at expiry")
    return f"the most you can lose: ${float(x):,.0f} per contract - what you paid"


def breakeven_words(be, spot, side: str = "credit", direction: str = "up") -> str:
    try:
        b, s = float(be), float(spot)
    except (TypeError, ValueError):
        return "breakeven unknown"
    pct = abs(b - s) / s * 100.0 if s else 0.0
    if side == "credit":
        verb = "fall" if direction == "up" else "rise"
        return f"breakeven {b:.2f} - the stock can {verb} {pct:.1f}% and this still makes money at expiry"
    verb = "rise" if direction == "up" else "fall"
    return f"breakeven {b:.2f} - the stock must {verb} {pct:.1f}% by expiry just to get your money back"


def dte_words(dte, lo, hi, family: str = "credit_vertical") -> str:
    inside = "inside" if (lo is not None and hi is not None and lo <= dte <= hi) else "outside"
    if family in ("credit_vertical", "condor"):
        return (f"{dte} days to expiry - {inside} your {lo}-{hi} day window; long enough for time to work "
                "for you, short enough to manage")
    return f"{dte} days - {inside} your {lo}-{hi} day window; enough time for the move, before decay bites"


def expected_move_words(em, spot, dte=None) -> str | None:
    try:
        e, s = float(em), float(spot)
    except (TypeError, ValueError):
        return None
    when = "by expiry" if dte is None else f"over {int(dte)} days"
    return f"expected move +/-${e:,.0f} ({e / s * 100:.1f}%) {when} - one standard deviation at today's IV"


def earnings_words(earnings_date, expiry: str | None = None) -> str:
    """'earnings Oct 22 falls INSIDE this expiry - the one thing a stop cannot protect you from.' /
    'earnings Oct 22 is after this expiry.' / 'no earnings date on file - check before you trade.'"""
    if not earnings_date:
        return "no earnings date on file - check before you trade"
    lab = expiry_label(str(earnings_date)[:10])
    if expiry and str(earnings_date)[:10] <= str(expiry)[:10]:
        return f"earnings {lab} falls INSIDE this expiry - the one thing a stop cannot protect you from"
    return f"earnings {lab} is after this expiry"


def earnings_state(earnings_date, expiry: str | None, earnings_rule: str | None = None,
                   defined: bool = False) -> dict:
    """``{inside, text}`` for the card / ticket footer: inside True when the date is on or
    before the expiry; the text says what the member's rule makes of it."""
    if not earnings_date:
        return {"inside": None, "text": earnings_words(None)}
    inside = bool(expiry and str(earnings_date)[:10] <= str(expiry)[:10])
    lab = expiry_label(str(earnings_date)[:10])
    if not inside:
        return {"inside": False, "text": f"earnings {lab} is after this expiry"}
    if earnings_rule == "defined_risk_only" and defined:
        return {"inside": True, "text": f"earnings {lab} is inside this expiry (defined-risk trades only)"}
    return {"inside": True, "text": f"earnings {lab} falls INSIDE this expiry and your rule says no"}


# ------------------------------------------------------------------- sizing
def sizing_line(sizing: dict | None) -> str:
    """ALWAYS both figures: '{n} contracts: about ${loss_at_stop} if the stop fires, up to
    ${max_loss_total} ({pct}% of your account) if the stock gaps past it'. The $ figures are
    rounded UP (a stated loss is never understated), the percent to one decimal. 0 contracts
    and a missing account value print their notes instead."""
    if not sizing:
        return NLV_MISSING
    n = sizing.get("contracts")
    if n is None:
        return sizing.get("note") or NLV_MISSING
    if int(n) <= 0:
        return sizing.get("note") or NOT_EVEN_ONE.format(risk=_f(sizing.get("risk_pct", 1.0)))
    n = int(n)
    loss = sizing.get("capital_at_risk_usd")
    if loss is None:
        loss = n * float(sizing.get("loss_at_stop_usd") or 0.0)
    total = sizing.get("max_loss_total_usd")
    if total is None:
        total = n * float(sizing.get("max_loss_usd") or 0.0)
    pct = sizing.get("max_loss_pct_nlv")
    word = "contract" if n == 1 else "contracts"
    return (f"{n} {word}: about {_usd(loss, up=True)} if the stop fires, up to {_usd(total, up=True)} "
            f"({float(pct or 0.0):.1f}% of your account) if the stock gaps past it")


def collect_words(worst_usd, mid_usd, side: str = "credit") -> str:
    """'you collect $200-$210 (worst likely fill to mid)' / 'you pay $1,562-$1,587 (mid to worst
    likely fill)' - per contract."""
    if side == "credit":
        if worst_usd is None:
            return f"you collect about {_usd(mid_usd)} (no two-sided quote for the worst fill)"
        return f"you collect {_usd(worst_usd)}-{_usd(mid_usd)} (worst likely fill to mid)"
    if worst_usd is None:
        return f"you pay about {_usd(mid_usd)} (no two-sided quote for the worst fill)"
    return f"you pay {_usd(mid_usd)}-{_usd(worst_usd)} (mid to worst likely fill)"


def risk_words(max_loss_usd, chart_stop=None, chart_stop_pl=None) -> str:
    """'you risk $790, but the chart stop at 336.2 would lose about $121'."""
    s = f"you risk {_usd(max_loss_usd)}"
    if chart_stop is not None and chart_stop_pl is not None:
        s += f", but the chart stop at {_f(chart_stop, 1)} would lose about {_usd(-float(chart_stop_pl), up=True)}"
    return s


# ---------------------------------------------------------------- the rules line
def rule_words(prefs: dict, strategy: str, chart: dict | None = None) -> str:
    """The picks banner, from the member's merged rules for one strategy (the flat dict
    ``option_prefs.for_strategy`` returns, or the full read() dict): 'delta 0.20-0.30 ·
    30-60 days · width 0.5-1.5 ATR ($6-17) · credit >= 25% of the risk · under support 340.9'."""
    p = dict(prefs or {})
    fam = strategy_rules.FAMILY_OF.get(strategy, "credit_vertical")
    if fam in p and isinstance(p.get(fam), dict):          # the full read() dict
        flat = dict(p.get("shared") or {})
        flat.update(p[fam])
        p = flat
    chart = chart or {}
    atr = chart.get("atr") or (chart.get("setup") or {}).get("atr")
    bits: list[str] = []

    def _atr_usd(lo, hi):
        if not atr:
            return ""
        return f" (${lo * float(atr):,.0f}-{hi * float(atr):,.0f})"

    if fam == "credit_vertical":
        bits.append(f"delta {p.get('short_delta_lo', 0.2):.2f}-{p.get('short_delta_hi', 0.3):.2f}")
        bits.append(f"{int(p.get('dte_lo', 30))}-{int(p.get('dte_hi', 60))} days")
        lo, hi = float(p.get("width_atr_lo", 0.5)), float(p.get("width_atr_hi", 1.5))
        bits.append(f"width {lo:g}-{hi:g} ATR{_atr_usd(lo, hi)}")
        bits.append(f"credit >= {int(p.get('credit_pct_min', 25))}% of the risk")
    elif fam == "debit_vertical":
        bits.append(f"long delta {p.get('long_delta_lo', 0.6):.2f}-{p.get('long_delta_hi', 0.7):.2f}")
        bits.append(f"short at the chart target (delta {p.get('short_delta_lo', 0.25):.2f}-{p.get('short_delta_hi', 0.35):.2f} preferred)")
        bits.append(f"{int(p.get('dte_lo', 30))}-{int(p.get('dte_hi', 60))} days")
        bits.append(f"reward >= {float(p.get('reward_cost_min', 1.0)):g}x the cost")
    elif fam == "long":
        bits.append(f"delta {p.get('delta_lo', 0.6):.2f}-{p.get('delta_hi', 0.7):.2f}")
        bits.append(f"{int(p.get('dte_lo', 45))}-{int(p.get('dte_hi', 90))} days")
        bits.append(f"decay under {float(p.get('theta_pct_max', 1.0)):g}% a day")
    elif fam == "leaps":
        bits.append(f"delta {p.get('delta_lo', 0.7):.2f}-{p.get('delta_hi', 0.8):.2f}")
        bits.append(f"{int(p.get('months_lo', 9))}-{int(p.get('months_hi', 18))} months")
        bits.append(f"time value under {float(p.get('extrinsic_pct_max', 10)):g}% of the share price")
    elif fam == "condor":
        bits.append(f"short delta {p.get('short_delta_lo', 0.15):.2f}-{p.get('short_delta_hi', 0.2):.2f}")
        lo, hi = float(p.get("wing_atr_lo", 0.5)), float(p.get("wing_atr_hi", 1.5))
        bits.append(f"wings {lo:g}-{hi:g} ATR{_atr_usd(lo, hi)}")
        bits.append(f"{int(p.get('dte_lo', 30))}-{int(p.get('dte_hi', 45))} days")
        bits.append(f"credit >= {int(p.get('credit_pct_min', 30))}% of the risk")
    elif fam == "time":
        if strategy == "calendar":
            bits.append(f"front {int(p.get('cal_front_lo', 20))}-{int(p.get('cal_front_hi', 30))} days")
            bits.append(f"back {int(p.get('cal_back_lo', 50))}-{int(p.get('cal_back_hi', 70))} days")
            bits.append("near month dearer than the far one")
        else:
            bits.append(f"long delta {p.get('diag_long_delta_lo', 0.7):.2f}-{p.get('diag_long_delta_hi', 0.8):.2f}")
            bits.append(f"short delta {p.get('diag_short_delta_lo', 0.2):.2f}-{p.get('diag_short_delta_hi', 0.3):.2f}")
    if p.get("chart_constraint", True):
        setup = chart.get("setup") or {}
        lvl = setup.get("level")
        if fam == "credit_vertical" and lvl is not None:
            if strategy == "bull_put":
                bits.append(f"under support {_f(lvl, 1)}")
            else:
                bits.append(f"over resistance {_f(lvl, 1)}")
            tl = chart.get("tl")
            if tl and not tl.get("broken"):
                bits[-1] += " + trend line"
        elif fam == "condor":
            bits.append("both shorts outside the range")
    if p.get("monthly_only"):
        bits.append("monthly expiries only")
    return " · ".join(bits)


# ---------------------------------------------------------- headline pieces
def trend_sentence(setup: dict) -> str:
    """'Uptrend: EMA 20 above 50 above 200 for 34 days' and the other three."""
    setup = setup or {}
    trend = setup.get("trend")
    if trend is None:
        d = setup.get("direction")
        trend = {"long": "up", "short": "down", "up": "up", "down": "down"}.get(d, "unclear")
    days = setup.get("trend_days")
    rng = setup.get("rng") or {}
    span = f" for {int(days)} day{'s' if int(days) != 1 else ''}" if days else ""
    if trend == "up":
        return "Uptrend: EMA 20 above 50 above 200" + span
    if trend == "down":
        return "Downtrend: EMA 20 below 50 below 200" + span
    if trend == "sideways":
        return (f"Sideways: the EMAs are flat and price has held between {_f(rng.get('low'))} and "
                f"{_f(rng.get('high'))} ({rng.get('n_low') or 0} touches below, {rng.get('n_high') or 0} above)")
    return "No clear trend: the EMAs are tangled"


def setup_sentence(setup: dict) -> str:
    """'It bounced off support at 340 on high volume (1.7x normal)' and the other kinds;
    'No fresh setup today' when the chart carries none; a trend-line warning adds
    '- but today closed under the line' (II.2.10)."""
    setup = setup or {}
    kind = setup.get("kind")
    lvl = setup.get("level")
    sup = setup.get("sup") or {}
    vol_high = setup.get("vol_high")
    if vol_high is None:
        vol_high = sup.get("vol_high")
    vr = setup.get("vol_ratio")
    if vr is None:
        vr = sup.get("vol_ratio")
    vol = ""
    if vol_high:
        vol = " on high volume" + (f" ({float(vr):.1f}x normal)" if vr else "")
    tl = setup.get("tl") or {}
    warn = " - but today closed under the line" if tl.get("warning") else ""
    if kind == "support_bounce":
        s = f"It bounced off support at {_f(lvl)}{vol}"
    elif kind == "resistance_reject":
        s = f"It was rejected at resistance {_f(lvl)}{vol}"
    elif kind == "trendline_bounce":
        n = setup.get("touches") or tl.get("n_touches") or "?"
        s = f"It bounced at the trend line ({n} touches){vol}"
    elif kind == "ema_rebound":
        ema = setup.get("ema_name") or (setup.get("ema") if isinstance(setup.get("ema"), str) else None) or "EMA20"
        s = f"It rebounded off the {str(ema).replace('EMA', '')}-day average"
    elif kind == "breakout_retest":
        s = f"It broke out above {_f(lvl)} and is retesting it"
    elif kind == "failed_support":
        s = f"Support at {_f(lvl)} gave way and price is back under it"
    elif kind == "range":
        s = "It is holding inside the range"
    else:
        s = "No fresh setup today"
        if tl.get("n_touches"):
            s = f"Price is riding a trend line with {tl['n_touches']} touches"
    return s + warn


def gauge(iv: dict) -> str:
    """The gauge clause of the headline: 'Options are expensive (IV rank 62 over the last
    year)' / '... look expensive against the last 34 days (not a full year yet)' / the ONE
    IV-unknown sentence."""
    iv = iv or {}
    verdict = iv.get("verdict") or "UNKNOWN"
    basis = iv.get("basis")
    n = int(iv.get("iv_n") or 0)
    if verdict == "UNKNOWN" or basis in (None, "unknown"):
        return iv_unknown_words(n)
    if basis == "provisional":
        return (iv.get("verdict_why") or iv_unknown_words(n)).rstrip(". ")
    word = {"SELL": "expensive", "BUY": "cheap"}.get(verdict, "fairly priced")
    if basis == "rank":
        r = _f(iv.get("iv_rank"), 0)
        span = "over the last year" if iv.get("state") == "ok" else f"over {n} days"
        return f"Options are {word} (IV rank {r} {span})"
    if verdict == "NEUTRAL":
        return f"Options are fairly priced against the last {n} days (not a full year yet)"
    return f"Options look {word} against the last {n} days (not a full year yet)"


def headline(setup: dict, iv: dict, strategies: list) -> str:
    """The card's one-paragraph read, composed at WRITE time:
    '{trend}. {setup sentence}. {gauge}, {conclusion}.' - the conclusion is the
    recommended strategy's, the unbuilt fit's 'not in TradeHunter yet' sentence, the
    earnings note, or 'so there is nothing to do today - check again tomorrow'."""
    setup = setup or {}
    strategies = strategies or []
    rec = next((s for s in strategies if s.get("fit") == "recommended"), None)
    g = gauge(iv)
    parts = [trend_sentence(setup), setup_sentence(setup)]
    if rec is not None:
        tail = f"{g}, {CONCLUSION.get(rec.get('key'), NOTHING_FITS)}."
    else:
        unbuilt = [s for s in strategies if s.get("fit") == "also_fits" and s.get("reason_key") == "not_available_yet"]
        earn = [s for s in strategies if s.get("fit") == "rejected" and s.get("reason_key") == "earnings_inside" and s.get("shown")]
        if unbuilt:
            tail = f"{g}. {strategy_rules.not_yet_sentence(unbuilt[0]['key'])}"
        elif earn:
            date = (iv or {}).get("earnings_date")
            when = expiry_label(date) if date else "soon"
            tail = (f"{g} - but earnings {when} fall inside every expiry in your window. Nothing to do "
                    "until after earnings; the idea will return once the date has passed.")
        else:
            tail = f"{g}, {NOTHING_FITS}."
    text = ". ".join(p.rstrip(".") for p in parts if p) + ". " + tail
    return " ".join(text.split())


def chip_row(strategies: list) -> list[dict]:
    """The chip row from the stored strategies list: the recommended, every also_fits and
    the shown near-miss rejects; the rest go behind 'other strategies'. Each chip:
    ``{key, label, text, fit, reason_key, reason, shown, score}``."""
    out = []
    for s in strategies or []:
        key = s.get("key")
        fit = s.get("fit")
        rk = s.get("reason_key")
        lab = s.get("label") or label(key)
        if fit == "recommended":
            text = lab
        elif fit == "also_fits":
            text = lab + (" · not available yet" if rk == "not_available_yet" else "")
        else:
            text = lab + (f" · {chip_text(rk)}" if rk else "")
        out.append({"key": key, "label": lab, "text": text, "fit": fit, "reason_key": rk,
                    "reason": (s.get("reasons") or [None])[0], "shown": bool(s.get("shown")),
                    "score": s.get("score")})
    return out


def degenerate_words(reason_key: str, **kw) -> str:
    """The member's sentence for a picker's degenerate case (never the key)."""
    tmpl = DEGENERATE_TEXT.get(reason_key, "No strike passes your rules today.")

    class _D(dict):
        def __missing__(self, k):
            return "?"
    try:
        return tmpl.format_map(_D(kw))
    except (ValueError, TypeError):
        return tmpl


# ------------------------------------------------------------ clocks and ages
def age_badge(age_h, stale: bool = False) -> dict:
    """The data-age dot: ``{tone, text}`` - emerald under 20 h, amber 20-72 h, rose older
    or with no signal / stale."""
    if age_h is None:
        return {"tone": "rose", "text": "no data yet"}
    h = float(age_h)
    if stale or h > 72:
        return {"tone": "rose", "text": f"{h / 24:.0f} days old" if h >= 48 else f"{h:.0f} h old"}
    if h > 20:
        return {"tone": "amber", "text": f"{h:.0f} h old"}
    return {"tone": "emerald", "text": f"{h:.0f} h old" if h >= 1 else "fresh"}


def et_clock(as_of) -> str:
    """A feed stamp as the member reads it: '02 Oct 16:00 ET'. Accepts a naive UTC
    datetime, an aware datetime, or an ISO string (a bare 'YYYY-MM-DDTHH:MM' is read as
    New York wall time, the Cboe convention)."""
    if as_of is None:
        return "an unknown time"
    try:
        from .clock import et_now
    except Exception:  # noqa: BLE001
        et_now = None
    d = None
    if isinstance(as_of, _dt.datetime):
        d = as_of
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        if et_now is not None:
            d = et_now(d)
    else:
        s = str(as_of).strip()
        try:
            d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            try:
                d = _dt.datetime.fromisoformat(s[:19])
            except ValueError:
                return s
        if d.tzinfo is not None and et_now is not None:
            d = et_now(d)
    return f"{d.day:02d} {d.strftime('%b')} {d.strftime('%H:%M')} ET"
