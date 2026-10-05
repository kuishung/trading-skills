"""The strategy rule table and the recommender - rules, explainable, no LLM.

ONE row per strategy (``StrategyRule``), ten rows, in the user's order; the
recommender walks the table and says for every row whether it fits today's chart
and today's premium and, when it does not, WHY in a fixed vocabulary the page can
colour and translate (OPTIONS_MODULE_DESIGN.md II.2.6-II.2.7, part_B_engines.md
B3). Nothing here reads a chain or a database: ``recommend()`` takes the chart
state (``chart_state.read``), the premium gauge's dict (``premium_gauge.gauge``)
and the member's merged rules (``option_prefs.read``), and returns plain dicts the
nightly job stores on the signal row as-is.

**This module is the ONE home of ``STRATEGY_KEYS``.** ``option_prefs`` imports
them from here (never the other way round - this module must not import
``option_prefs``), and the order is the catalog order AND the tie-break order:
``buy_call, buy_put, bull_call, bear_put, leaps_call, diagonal_call, bull_put,
bear_call, iron_condor, calendar``.

Build phases
------------
Every rule carries a ``step`` (the phase of the design's build order its strike
picker lands in) and ``CURRENT_STEP`` says how far the build has got. **A rule
whose picker is not built can never be ``recommended``**: when it fits it goes to
``also_fits`` with ``reason_key = "not_available_yet"`` and the chip text
"{label} · not available yet"; it has no picks and no ticket. The words "step",
"phase" or a build number never reach a member - they are internal to this file.
"""
from __future__ import annotations

import datetime as _dt
import string
from dataclasses import dataclass

from .opt_constants import (
    BUY_MAX_RANK,
    IV_RANK_MIN_OBS,
    MID_HI,
    MID_LO,
    SELL_DIR_MIN_RANK,
    SELL_NEUTRAL_MIN_RANK,
    SLOW_DRIFT_ATR,
    TERM_EVENT,
)

# ---------------------------------------------------------------- the keys
# The user's order (design 5.2, 2026-10-03). Catalog order on the page, tie-break
# order in the recommender, the order option_prefs and every template iterate.
STRATEGY_KEYS = (
    "buy_call", "buy_put", "bull_call", "bear_put", "leaps_call", "diagonal_call",
    "bull_put", "bear_call", "iron_condor", "calendar",
)

# How far the build has got (design 9 / II.4). 1 = the credit spreads are live;
# bumped to 2 / 3 / 4 as each phase's picker lands. Internal; never shown.
CURRENT_STEP = 1

# The strategies whose POP is "chance of keeping the credit" (pop_kind = keep);
# every other key is "chance of profit" (Part C's payoff.pop).
CREDIT_FAMILIES = frozenset({"bull_put", "bear_call", "iron_condor"})

# The earnings policy's "defined risk" set (II.2.7, ruling R11): allowed through
# earnings ONLY when the member's shared rule is defined_risk_only. buy_call /
# buy_put / calendar / diagonal_call are none_inside regardless of the member's
# rule (a long option or a time spread has no cap a stop can defend across a
# print); leaps_call is "any" (a 9-18 month option crosses several reports by
# construction). option_prefs.defined_risk() reads this set.
DEFINED_RISK = frozenset({"bull_put", "bear_call", "bull_call", "bear_put", "iron_condor"})

FAMILIES = ("credit_vertical", "debit_vertical", "long", "leaps", "condor", "time")

# The fixed rejection vocabulary (II.2.6). Part D's option_words renders the chip
# text; the long sentence sits in each row's ``reasons`` list. Nothing else may be
# written into ``reason_key``.
REASON_KEYS = (
    "expensive", "cheap_options", "not_rich_enough", "trending_not_sideways", "no_range",
    "wrong_direction", "no_setup", "earnings_inside", "front_iv_under_back",
    "no_long_dated", "no_weekly_trend", "not_available_yet",
)
# The short chip text per key, verbatim from II.2.6 (sentence case, plain words).
CHIP_TEXT = {
    "expensive": "expensive",
    "cheap_options": "cheap options",
    "not_rich_enough": "premium not rich enough",
    "trending_not_sideways": "trending, not sideways",
    "no_range": "no range",
    "wrong_direction": "wrong direction",
    "no_setup": "no setup",
    "earnings_inside": "earnings inside",
    "front_iv_under_back": "near-term not dearer",
    "no_long_dated": "no 9-18 month options stored",
    "no_weekly_trend": "no weekly trend",
    "not_available_yet": "not available yet",
}

FITS = ("recommended", "also_fits", "rejected")
FIT_ORDER = {"recommended": 0, "also_fits": 1, "rejected": 2}
NEAR_MISS_SHOWN = 2         # at most this many single-fail rejects are shown as greyed chips (decision 9)

# The two-expiry strategies: which leg's life earnings must not cross.
#   calendar  -> the BACK expiry (the long leg's whole life)
#   diagonal  -> the SHORT (front) leg
EARNINGS_LEG = {"calendar": "back", "diagonal_call": "front"}


# ------------------------------------------------------------- the rule row
@dataclass(frozen=True)
class StrategyRule:
    key: str                 # "bull_put"
    label: str               # "Bull put spread" - the member-facing name (LEAPS is "Buy LEAPS", never "Buy LEAPS call")
    family: str              # one of FAMILIES: the option_prefs block the picker reads
    direction: str           # "up" | "down" | "neutral"
    side: str                # "credit" | "debit"
    trends: tuple            # ChartState.trend values that fit
    setups: tuple            # setup kinds that fit; ("*",) = no setup needed
    iv_gate: str             # gauge gate: "buy" | "sell_directional" | "sell_neutral" | "mid_or_buy" | "any"
    term: str                # "any" | "front_ge_back" (hard) | "front_ge_back_soft" (a bonus, never a veto)
    earnings: str            # "none_inside" | "defined_risk" | "any" - what the earnings rule allows for this family
    weekly: bool             # needs ChartState.w_uptrend (LEAPS)
    needs: tuple             # extra chart facts: "support_level" | "resistance_level" | "target_level" | "range" | "slow_drift"
    dte: tuple               # (lo, hi) days; for a two-expiry strategy the FRONT leg's window
    step: int                # build phase (1..4) - internal, never shown
    priority: int            # base score when several fit (higher first)
    why: str                 # template, rendered with render()
    must_happen: str         # template: what has to happen for this to work
    not_yet: str             # the headline's conclusion when this rule fits but its picker is not built
    dte_back: tuple | None = None   # the BACK / long leg's window for the two-expiry strategies


# ----------------------------------------------------------- the ten rows
# Design 5.2 verbatim; II.2.7 is the binding table. Templates are rendered with
# render(key, ctx) - a missing key renders as a plain phrase, never a KeyError.
RULES: tuple[StrategyRule, ...] = (
    StrategyRule(
        key="buy_call", label="Buy call", family="long", direction="up", side="debit",
        trends=("up",),
        setups=("support_bounce", "trendline_bounce", "ema_rebound", "breakout_retest"),
        iv_gate="buy", term="any", earnings="none_inside", weekly=False, needs=(),
        dte=(45, 90), step=2, priority=60,
        why="{trend_sentence} It {setup_sentence}. {iv_clause}, so you buy the move rather than sell insurance.",
        must_happen="{symbol} reaches {target} (2R) before {expiry_label}; the stop is {stop}.",
        not_yet="The chart would suit buying a call outright; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="buy_put", label="Buy put", family="long", direction="down", side="debit",
        trends=("down",),
        setups=("failed_support", "resistance_reject", "trendline_bounce"),
        iv_gate="buy", term="any", earnings="none_inside", weekly=False, needs=(),
        dte=(45, 90), step=2, priority=60,
        why="{trend_sentence} It {setup_sentence}. {iv_clause}, so you buy the move down rather than sell insurance.",
        must_happen="{symbol} falls to {target} (2R) before {expiry_label}; the stop is {stop}.",
        not_yet="The chart would suit buying a put outright; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="bull_call", label="Bull call spread", family="debit_vertical", direction="up", side="debit",
        trends=("up",),
        setups=("support_bounce", "trendline_bounce", "ema_rebound", "breakout_retest"),
        iv_gate="mid_or_buy", term="any", earnings="defined_risk", weekly=False,
        needs=("target_level",), dte=(30, 60), step=2, priority=55,
        why="{trend_sentence} It {setup_sentence}. {iv_clause}: a bare call is dear, so the spread sells a call at your target {target} to pay for part of it.",
        must_happen="{symbol} closes above {breakeven} by {expiry_label}; above {short_strike} you have the whole {max_profit}.",
        not_yet="The chart would suit a call spread paid for up front; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="bear_put", label="Bear put spread", family="debit_vertical", direction="down", side="debit",
        trends=("down",),
        setups=("failed_support", "resistance_reject", "trendline_bounce"),
        iv_gate="mid_or_buy", term="any", earnings="defined_risk", weekly=False,
        needs=("target_level",), dte=(30, 60), step=2, priority=55,
        why="{trend_sentence} It {setup_sentence}. {iv_clause}: a bare put is dear, so the spread sells a put at your target {target} to pay for part of it.",
        must_happen="{symbol} closes below {breakeven} by {expiry_label}; below {short_strike} you have the whole {max_profit}.",
        not_yet="The chart would suit a put spread paid for up front; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="leaps_call", label="Buy LEAPS", family="leaps", direction="up", side="debit",
        trends=("up", "sideways"), setups=("*",),
        iv_gate="mid_or_buy", term="any", earnings="any", weekly=True, needs=(),
        dte=(270, 540), step=4, priority=40,
        why="Long-term uptrend (weekly EMA 20 above 50 above 200). A long-dated call bought deep in the money moves almost like {shares} shares, for about {cost_words} of the money, and only {extrinsic_pct}% of the share price pays for time.",
        must_happen="{symbol} keeps its weekly uptrend over the next {months} months; you roll it out when {roll_dte} days remain.",
        not_yet="The long-term chart would suit a long-dated call; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="diagonal_call", label="Diagonal call spread", family="time", direction="up", side="debit",
        trends=("up",), setups=("*",),
        iv_gate="mid_or_buy", term="front_ge_back_soft", earnings="none_inside", weekly=False,
        needs=("slow_drift", "resistance_level"), dte=(30, 45), dte_back=(180, 365), step=4, priority=45,
        why="Slow uptrend with a resistance at {resistance}: own a long-dated call (delta {long_delta}) and rent out a near-term call under that resistance each month.",
        must_happen="{symbol} grinds up but stays under {short_strike} by {short_expiry_label}; you re-sell the short call every cycle.",
        not_yet="The slow grind under a resistance would suit renting out calls against a long-dated one; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="bull_put", label="Bull put spread", family="credit_vertical", direction="up", side="credit",
        trends=("up",),
        setups=("support_bounce", "trendline_bounce", "ema_rebound"),
        iv_gate="sell_directional", term="any", earnings="defined_risk", weekly=False,
        needs=("support_level",), dte=(30, 60), step=1, priority=60,
        why="{trend_sentence} It {setup_sentence}. {iv_clause}, so you are paid to sell a put spread below that {level_name}.",
        must_happen="{symbol} stays above {short_strike} until {expiry_label}. You keep the credit if it does nothing, drifts up, or even dips a little.",
        not_yet="The chart would suit selling a put spread under support; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="bear_call", label="Bear call spread", family="credit_vertical", direction="down", side="credit",
        trends=("down",),
        setups=("resistance_reject", "trendline_bounce", "failed_support"),
        iv_gate="sell_directional", term="any", earnings="defined_risk", weekly=False,
        needs=("resistance_level",), dte=(30, 60), step=1, priority=60,
        why="{trend_sentence} It {setup_sentence}. {iv_clause}, so you are paid to sell a call spread above that {level_name}.",
        must_happen="{symbol} stays below {short_strike} until {expiry_label}. You keep the credit if it does nothing, drifts down, or even pops a little.",
        not_yet="The chart would suit selling a call spread over resistance; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="iron_condor", label="Iron condor", family="condor", direction="neutral", side="credit",
        trends=("sideways",), setups=("range",),
        iv_gate="sell_neutral", term="any", earnings="defined_risk", weekly=False,
        needs=("range",), dte=(30, 45), step=3, priority=60,
        why="Sideways: EMAs flat, price between {lower} (touched {lt}x) and {upper} ({ut}x). {iv_clause}, so you sell both sides outside the range.",
        must_happen="{symbol} stays between {short_put} and {short_call} until {expiry_label}.",
        not_yet="The sideways chart would suit selling both sides of the range; that strategy is not in TradeHunter yet.",
    ),
    StrategyRule(
        key="calendar", label="Calendar spread", family="time", direction="neutral", side="debit",
        trends=("sideways", "up", "down"), setups=("*",),
        iv_gate="any", term="front_ge_back", earnings="none_inside", weekly=False,
        needs=("slow_drift",), dte=(20, 30), dte_back=(50, 70), step=4, priority=45,
        why="Price is sitting near {strike} and the front month is priced {term}x the back: you sell the dear near-term option and own the cheaper later one.",
        must_happen="{symbol} is near {strike} on {front_expiry_label} (between {be_lo} and {be_hi}).",
        not_yet="The quiet chart with a dear near month would suit a calendar spread; that strategy is not in TradeHunter yet.",
    ),
)

RULES_BY_KEY = {r.key: r for r in RULES}
RULES_INDEX = {r.key: i for i, r in enumerate(RULES)}
LABELS = {r.key: r.label for r in RULES}
FAMILY_OF = {r.key: r.family for r in RULES}

assert tuple(r.key for r in RULES) == STRATEGY_KEYS, "RULES must be in STRATEGY_KEYS order"


def rule(key: str) -> StrategyRule:
    """The row for one key; KeyError for a key outside the catalog."""
    return RULES_BY_KEY[key]


# ------------------------------------------------------------ the wording
# Plain words for the setup a rule wants, used in "no {x} on the chart today".
SETUP_WORD = {
    "buy_call": "fresh bullish setup", "buy_put": "fresh bearish setup",
    "bull_call": "fresh bullish setup", "bear_put": "fresh bearish setup",
    "leaps_call": "setup", "diagonal_call": "setup",
    "bull_put": "support bounce", "bear_call": "resistance rejection",
    "iron_condor": "range", "calendar": "quiet range",
}
# What a chart fact is called when it is missing.
NEED_KEY = {
    "range": "no_range", "support_level": "no_setup", "resistance_level": "no_setup",
    "target_level": "no_setup", "slow_drift": "no_setup",
}
NEED_REASON = {
    "range": "no range with both edges touched",
    "support_level": "no support level under the price to sell against",
    "resistance_level": "no resistance level over the price to sell against",
    "target_level": "no target on the chart to cap the spread at",
    "slow_drift": f"not a slow grind (the EMA20 moved more than {SLOW_DRIFT_ATR:g} ATR in 10 sessions)",
}
_TREND_WORD = {"up": "an uptrend", "down": "a downtrend", "sideways": "sideways", "unclear": "no clear trend"}
_LEVEL_NAME = {
    "support_bounce": "support", "trendline_bounce": "trend line", "ema_rebound": "moving average",
    "breakout_retest": "breakout level", "resistance_reject": "resistance", "failed_support": "broken support",
    "range": "range edge",
}
# Readable stand-ins for template keys that only a PICK can fill (the recommender
# runs before the picker; option_engine re-renders with the pick in ctx).
PLACEHOLDERS = {
    "short_strike": "the short strike", "expiry_label": "expiry", "breakeven": "the breakeven",
    "max_profit": "max profit", "shares": "70-80", "cost_words": "a third", "long_delta": "0.70-0.80",
    "short_expiry_label": "the near expiry", "short_put": "the put strike", "short_call": "the call strike",
    "front_expiry_label": "the near expiry", "be_lo": "the lower breakeven", "be_hi": "the upper breakeven",
    "strike": "the strike", "target": "the target", "stop": "the stop", "resistance": "resistance",
    "lower": "the range low", "upper": "the range high", "lt": "?", "ut": "?", "term": "?",
    "months": "9-18", "roll_dte": "180", "extrinsic_pct": "10", "level_name": "level",
    "trend_sentence": "", "setup_sentence": "set up", "iv_clause": "", "symbol": "the stock",
}


class _Missing:
    """A template key the context does not carry: renders as its plain phrase under
    ANY format spec, so '{short_strike:g}' never raises before the pick exists."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text

    def __format__(self, spec: str) -> str:  # noqa: D105
        return self.text

    def __str__(self) -> str:  # noqa: D105
        return self.text


class _Ctx(dict):
    def __missing__(self, key):  # noqa: D105
        return _Missing(PLACEHOLDERS.get(key, key.replace("_", " ")))


class _Fmt(string.Formatter):
    """format_map that also survives a format spec on a None value."""

    def format_field(self, value, spec):  # noqa: D102
        if value is None:
            return _Missing("?").__format__(spec)
        try:
            return super().format_field(value, spec)
        except (ValueError, TypeError):
            return str(value)


_FMT = _Fmt()


def render(key: str, ctx: dict) -> tuple[str, str]:
    """(why, must_happen) for one rule from a context dict. Missing keys render as
    plain phrases (PLACEHOLDERS); formatting never raises."""
    r = RULES_BY_KEY[key]
    c = _Ctx(ctx or {})
    why = _FMT.vformat(r.why, (), c)
    must = _FMT.vformat(r.must_happen, (), c)
    return _squash(why), _squash(must)


def _squash(s: str) -> str:
    s = " ".join(s.split()).replace(" .", ".").replace(" ,", ",").replace(".,", ".")
    return s.lstrip(", ").strip()


def _g(v, nd: int = 2) -> str:
    """A price as a member reads it: 340 / 336.2 / 327.9 (never 340.0 or 327.90000001)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"{round(f, nd):g}"


def span_words(gauge: dict) -> str:
    """The gauge's day-count phrase for a headline: 'over the last year' (state ok),
    'over N days' (rank_ok), 'against the last N days (not a full year yet)'
    (pct_only). Mirrors premium_gauge.span_words so this module needs no import."""
    state, n = (gauge or {}).get("state"), int((gauge or {}).get("iv_n") or 0)
    if state == "ok":
        return "over the last year"
    if state == "rank_ok":
        return f"over {n} days"
    return f"against the last {n} days (not a full year yet)"


def _measure(gauge: dict) -> float | None:
    """What the gates were evaluated on: the rank when trusted, else the percentile."""
    g = gauge or {}
    if g.get("basis") in ("provisional", "unknown"):
        return None
    if g.get("iv_rank") is not None:
        return float(g["iv_rank"])
    if g.get("iv_pct") is not None:
        return float(g["iv_pct"])
    return None


def context(chart: dict, gauge: dict, prefs: dict | None = None, pick: dict | None = None) -> dict:
    """The template context: ChartState + gauge (+ the pick when it exists).
    option_engine calls this again with ``pick`` to fill the strike-level keys."""
    chart = chart or {}
    gauge = gauge or {}
    prefs = prefs or {}
    setup = chart.get("setup") or {}
    plan = chart.get("plan") or {}
    rng = chart.get("rng") or {}
    levels = chart.get("levels") or {}
    trend = chart.get("trend") or "unclear"
    days = chart.get("trend_days")
    if trend in ("up", "down"):
        word = "Uptrend" if trend == "up" else "Downtrend"
        trend_sentence = f"{word} for {int(days)} days." if days else f"{word}."
    elif trend == "sideways":
        trend_sentence = "Sideways: EMAs flat."
    else:
        trend_sentence = "No clear trend."
    kind = setup.get("kind")
    lvl = setup.get("level")
    vol = " on high volume" if setup.get("vol_high") else ""
    if kind == "support_bounce":
        setup_sentence = f"bounced off support at {_g(lvl)}{vol}"
    elif kind == "resistance_reject":
        setup_sentence = f"was rejected at resistance {_g(lvl)}{vol}"
    elif kind == "trendline_bounce":
        setup_sentence = f"bounced off the trend line at {_g(lvl)} ({setup.get('touches') or '?'} touches){vol}"
    elif kind == "ema_rebound":
        setup_sentence = f"rebounded on the {setup.get('ema') or 'moving average'} at {_g(lvl)}"
    elif kind == "breakout_retest":
        setup_sentence = f"is retesting the breakout level {_g(lvl)}{vol}"
    elif kind == "failed_support":
        setup_sentence = f"broke support at {_g(lvl)}{vol}"
    elif kind == "range":
        setup_sentence = f"is holding between {_g(rng.get('low'))} and {_g(rng.get('high'))}"
    else:
        setup_sentence = "has no fresh setup"
    measure = _measure(gauge)
    verdict = gauge.get("verdict")
    if measure is None:
        iv_clause = (gauge.get("verdict_why") or "We cannot yet say whether options are expensive").rstrip(". ")
    else:
        basis = "rank" if gauge.get("basis") == "rank" else "percentile"
        word = {"SELL": "expensive", "BUY": "cheap"}.get(verdict, "middling")
        iv_clause = f"Options are {word} (IV {basis} {measure:.0f} {span_words(gauge)})"
    ctx = {
        "symbol": chart.get("symbol") or "the stock",
        "trend_sentence": trend_sentence, "setup_sentence": setup_sentence, "iv_clause": iv_clause,
        "measure": measure, "basis": gauge.get("basis"), "span": span_words(gauge),
        "level_name": _LEVEL_NAME.get(kind, "level"), "level": _g(lvl) if lvl is not None else None,
        "target": _g(plan.get("target")) if plan.get("target") is not None else None,
        "stop": _g(plan.get("stop")) if plan.get("stop") is not None else None,
        "resistance": _g(levels.get("resistance")) if levels.get("resistance") is not None else None,
        "support": _g(levels.get("support")) if levels.get("support") is not None else None,
        "lower": _g(rng.get("low")) if rng.get("low") is not None else None,
        "upper": _g(rng.get("high")) if rng.get("high") is not None else None,
        "lt": rng.get("n_low"), "ut": rng.get("n_high"),
        "term": f"{gauge['term_ratio']:.2f}" if gauge.get("term_ratio") else None,
        "strike": _g(chart.get("close")) if chart.get("close") is not None else None,
    }
    leaps = prefs.get("leaps") or {}
    time_ = prefs.get("time") or {}
    if leaps:
        ctx["months"] = f"{leaps.get('months_lo', 9)}-{leaps.get('months_hi', 18)}"
        ctx["roll_dte"] = leaps.get("roll_dte", 180)
        ctx["extrinsic_pct"] = leaps.get("extrinsic_pct_max", 10)
    if time_:
        ctx["long_delta"] = f"{time_.get('diag_long_delta_lo', 0.7):.2f}-{time_.get('diag_long_delta_hi', 0.8):.2f}"
    if pick:
        ctx.update(_pick_ctx(pick))
    return ctx


def _pick_ctx(pick: dict) -> dict:
    """The strike-level keys from a Pick (II.2.8): the short / long legs, expiry
    labels, breakevens, max profit."""
    out: dict = {}
    legs = pick.get("legs") or []
    sells = [l for l in legs if l.get("side") == "sell"]
    buys = [l for l in legs if l.get("side") == "buy"]
    if sells:
        out["short_strike"] = _g(sells[0].get("strike"))
        puts = [l for l in sells if l.get("right") == "P"]
        calls = [l for l in sells if l.get("right") == "C"]
        if puts:
            out["short_put"] = _g(puts[0].get("strike"))
        if calls:
            out["short_call"] = _g(calls[0].get("strike"))
        if sells[0].get("expiry"):
            out["short_expiry_label"] = expiry_label(sells[0]["expiry"])
    if buys and buys[0].get("delta") is not None:
        d = abs(float(buys[0]["delta"]))
        out["long_delta"] = f"{d:.2f}"
        out["shares"] = f"{d * 100:.0f}"
    if pick.get("expiry"):
        out["expiry_label"] = expiry_label(pick["expiry"])
        out["front_expiry_label"] = out["expiry_label"]
    bes = pick.get("breakevens") or []
    if bes:
        out["breakeven"] = _g(bes[0])
        out["be_lo"] = _g(min(bes))
        out["be_hi"] = _g(max(bes))
    if pick.get("max_profit") is not None:
        out["max_profit"] = f"${float(pick['max_profit']):,.0f}"
    if legs and legs[0].get("strike") is not None and len({l.get("strike") for l in legs}) == 1:
        out["strike"] = _g(legs[0]["strike"])
    return out


def expiry_label(expiry: str) -> str:
    """'2026-11-20' -> 'Nov 20' (the label every member string uses)."""
    try:
        d = _dt.date.fromisoformat(expiry)
    except (TypeError, ValueError):
        return str(expiry)
    return f"{d.strftime('%b')} {d.day}"


# ------------------------------------------------------------- the checks
def _gate(gates: dict, name: str) -> bool:
    if name == "any":
        return True
    if name == "mid_or_buy":
        return bool(gates.get("mid") or gates.get("buy"))
    return bool(gates.get(name))


def _iv_fail(rule: StrategyRule, measure: float) -> tuple[str, str]:
    """(reason_key, text) when a gate is closed - the key and sentence depend on the
    rule's side and on where the measure sits, not on the gauge's verdict word."""
    m = measure
    if rule.side == "debit":
        if rule.iv_gate == "mid_or_buy":
            if rule.family in ("leaps", "time"):
                return "expensive", f"the long leg would be bought at IV rank {m:.0f} - too expensive"
            return "expensive", f"options too expensive to buy (IV rank {m:.0f}) - a spread you pay for is dear"
        if m >= SELL_NEUTRAL_MIN_RANK:
            return "expensive", f"options too expensive to buy (IV rank {m:.0f})"
        return "expensive", f"not cheap enough to buy outright (IV rank {m:.0f} > {BUY_MAX_RANK})"
    # credit side
    if rule.iv_gate == "sell_neutral" and m >= SELL_DIR_MIN_RANK:
        return "not_rich_enough", f"premium not rich enough for a condor (IV rank {m:.0f} < {SELL_NEUTRAL_MIN_RANK})"
    return "cheap_options", f"options too cheap to sell (IV rank {m:.0f})"


def _trend_fail(rule: StrategyRule, trend: str) -> tuple[str, str]:
    if rule.direction == "neutral":
        if trend in ("up", "down"):
            return "trending_not_sideways", "trending, not sideways"
        return "trending_not_sideways", "no clear range (the trend is unclear)"
    want = "an uptrend" if rule.direction == "up" else "a downtrend"
    have = _TREND_WORD.get(trend, "no clear trend")
    if trend == "unclear":
        return "wrong_direction", f"no clear trend (needs {want})"
    return "wrong_direction", f"not {want} ({have})"


def _sits(chart: dict) -> bool:
    """The calendar's 'price expected to sit near a strike' (Part C2.5): a sideways
    range, or a flat stack with price mid-range, or a slow drift."""
    rng = chart.get("rng") or {}
    if rng.get("sideways"):
        return True
    pos = rng.get("pos_pct")
    if rng.get("stack_flat") and pos is not None and 0.35 <= float(pos) <= 0.65:
        return True
    return bool(chart.get("slow_drift"))


def _has(chart: dict, need: str) -> bool:
    levels = chart.get("levels") or {}
    plan = chart.get("plan") or {}
    setup = chart.get("setup") or {}
    if need == "range":
        return chart.get("rng") is not None
    if need == "support_level":
        return levels.get("support") is not None or (setup.get("direction") == "up" and setup.get("level") is not None)
    if need == "resistance_level":
        return levels.get("resistance") is not None or (setup.get("direction") == "down" and setup.get("level") is not None)
    if need == "target_level":
        return plan.get("target") is not None
    if need == "slow_drift":
        return _sits(chart)
    return True


def _today(chart: dict) -> _dt.date:
    try:
        return _dt.date.fromisoformat(str(chart.get("as_of"))[:10])
    except (TypeError, ValueError):
        from .spread_monitor import et_today
        return _dt.date.fromisoformat(et_today())


def _dte(expiry: str, today: _dt.date) -> int | None:
    try:
        return (_dt.date.fromisoformat(str(expiry)[:10]) - today).days
    except (TypeError, ValueError):
        return None


WINDOW_SLACK_DAYS = 7      # an empty DTE window admits the one expiry within this many days of it (the picker's rule, B4.2)


def _window(expiries, lo: int, hi: int, today: _dt.date) -> list[_dt.date]:
    """The listed expiries inside [lo, hi] DTE - and, when none is, the nearest one
    within WINDOW_SLACK_DAYS of the window (the expiry the picker would admit with
    the note "48 DTE, just outside your 30-45"), so the earnings gate judges the
    same expiries the picker enumerates."""
    dated = []
    for x in expiries or ():
        d = _dte(x, today)
        if d is not None:
            dated.append((d, _dt.date.fromisoformat(str(x)[:10])))
    inside = [x for d, x in dated if lo <= d <= hi]
    if inside:
        return inside
    near = [(min(abs(d - lo), abs(d - hi)), d, x) for d, x in dated
            if (lo - WINDOW_SLACK_DAYS) <= d <= (hi + WINDOW_SLACK_DAYS)]
    if near:
        near.sort()
        return [near[0][2]]
    return []


def earnings_block(rule: StrategyRule | str, earnings: dict | None, expiries, prefs: dict | None,
                   *, today: _dt.date | None = None) -> str | None:
    """A reason string when earnings sit inside EVERY expiry of the rule's window
    and the member's rule does not allow it; else None (B3.4).

    ``earnings`` is prices.fetch_next_earnings' ``{"date", "days"}`` (None = unknown:
    never blocks - the card says "earnings date unknown", the Telegram push skips).
    ``expiries`` are the snapshot's ISO expiries; the window is the rule's ``dte``
    (the BACK window for a calendar, the SHORT window for a diagonal). Some expiry
    clear of earnings -> None: the picker skips the ones that are not.
    """
    r = rule if isinstance(rule, StrategyRule) else RULES_BY_KEY[rule]
    if not earnings or not earnings.get("date"):
        return None
    try:
        e_date = _dt.date.fromisoformat(str(earnings["date"])[:10])
    except (TypeError, ValueError):
        return None
    today = today or _dt.date.today()
    if e_date < today:
        return None                                   # a past date is stale, not a risk
    lo, hi = r.dte_back if (EARNINGS_LEG.get(r.key) == "back" and r.dte_back) else r.dte
    window = _window(expiries, lo, hi, today)
    if not window:
        return None
    inside = [x for x in window if e_date <= x]
    shared = (prefs or {}).get("shared") or {}
    allowed = r.earnings == "any" or (r.earnings == "defined_risk"
                                      and shared.get("earnings_rule") == "defined_risk_only")
    if inside and not allowed and len(inside) == len(window):
        days = earnings.get("days")
        try:
            days = int(days) if days is not None else (e_date - today).days
        except (TypeError, ValueError):
            days = (e_date - today).days
        if EARNINGS_LEG.get(r.key) == "back":
            return f"earnings {e_date.isoformat()} ({days}d) sits inside the back month ({lo}-{hi} days)"
        return f"earnings {e_date.isoformat()} ({days}d) sits inside every {lo}-{hi} day expiry"
    return None


def _no_long_dated(rule: StrategyRule, expiries, today: _dt.date) -> tuple[str, str] | None:
    """The two-expiry / LEAPS rules need a long-dated expiry in the snapshot; when
    the expiries are known and none sits in the long window the rule cannot work."""
    if not expiries or rule.key not in ("leaps_call", "diagonal_call"):
        return None
    lo, hi = rule.dte_back if rule.dte_back else rule.dte
    for x in expiries:
        d = _dte(x, today)
        if d is not None and lo <= d <= hi:
            return None
    months = f"{lo // 30}-{hi // 30}"
    return "no_long_dated", f"no {months} month options stored"


def _iv_fit(rule: StrategyRule, measure: float | None) -> float:
    """How far inside its gate the measure sits, 0..20; 0 when the basis is provisional."""
    if measure is None:
        return 0.0
    m = measure
    if rule.side == "credit":
        fit = (m - SELL_DIR_MIN_RANK) / 70.0 * 20.0
    elif rule.iv_gate == "buy":
        fit = (BUY_MAX_RANK - m) / 30.0 * 20.0
    elif rule.iv_gate == "mid_or_buy":
        fit = 20.0 - abs(m - (MID_LO + MID_HI) / 2.0)
    else:
        fit = 0.0
    return max(0.0, min(20.0, fit))


def _score(rule: StrategyRule, chart: dict, gauge: dict) -> float:
    """priority + iv_fit (0-20) + setup_quality/5 + term bonus 10 + structure bonus 5
    - 10 if unbuilt (II.2.7). Deterministic; every term is explainable."""
    setup = chart.get("setup") or {}
    quality = float(setup.get("quality") or 0.0)
    term = gauge.get("term_ratio")
    structure = (chart.get("structure") or {}).get("state")
    want_structure = "bullish" if rule.direction == "up" else "decelerated"
    s = float(rule.priority)
    s += _iv_fit(rule, _measure(gauge))
    s += quality / 5.0
    if rule.term in ("front_ge_back", "front_ge_back_soft") and term and float(term) >= TERM_EVENT:
        s += 10.0
    if structure == want_structure:
        s += 5.0
    if rule.step > CURRENT_STEP:
        s -= 10.0
    return round(s, 1)


# ------------------------------------------------------------ recommend()
def recommend(chart: dict, gauge: dict, prefs: dict | None = None, *,
              snapshot_expiries=()) -> dict:
    """Every rule judged against today's chart and premium.

    Returns ``{"strategies": [row x 10], "recommended": key | None}``; each row is
    ``{key, label, fit, score, step, why, must_happen, reasons, reason_key, shown}``
    in the order recommended -> also_fits -> rejected, score descending, catalog
    order on ties. A rejected row has ``score`` / ``why`` / ``must_happen`` None and
    its FIRST failure's key in ``reason_key`` (checks run in a fixed order: trend,
    setup, weekly trend, IV gate, term, chart facts, long-dated expiries,
    earnings). ``shown`` is True on every recommended / also_fits row and on at most
    two single-failure rejects (catalog order) - the greyed chips with a reason.

    The only preference read is ``prefs["shared"]["earnings_rule"]``, so the ten
    rows are the same for every member's hash of a symbol (B3.5); picks differ.
    """
    chart = chart or {}
    gauge = gauge or {}
    prefs = prefs or {}
    gates = gauge.get("gates") or {}
    basis = gauge.get("basis")
    measure = _measure(gauge)
    today = _today(chart)
    ctx = context(chart, gauge, prefs)
    trend = chart.get("trend") or "unclear"
    setup = chart.get("setup")

    rows: list[dict] = []
    for r in RULES:
        fails: list[tuple[str, str]] = []
        warns: list[str] = []
        if trend not in r.trends:
            fails.append(_trend_fail(r, trend))
        if r.setups != ("*",) and (setup is None or setup.get("kind") not in r.setups):
            fails.append(("no_setup", f"no {SETUP_WORD[r.key]} on the chart today"))
        if r.weekly and not chart.get("w_uptrend"):
            fails.append(("no_weekly_trend",
                          "no weekly uptrend (EMA 20 above 50 above 200 on the weekly chart)"))
        if r.iv_gate != "any":
            n = int(gauge.get("iv_n") or 0)
            if basis in ("provisional", "unknown", None) or measure is None:
                if r.side == "credit":      # a sell needs a measured rank; a provisional read never opens a sell gate
                    fails.append(("not_rich_enough",
                                  f"IV history too short to say options are expensive ({n} of {IV_RANK_MIN_OBS} days)"))
                else:                       # a buyer is protected by price, not by the gate: pass with a warning
                    warns.append(f"IV history too short to say options are cheap ({n} of {IV_RANK_MIN_OBS} days)")
            elif not _gate(gates, r.iv_gate):
                fails.append(_iv_fail(r, measure))
        if r.term == "front_ge_back":
            t = gauge.get("term_ratio")
            if not (t and float(t) >= 1.0):
                fails.append(("front_iv_under_back", "near-term options are not dearer than the later month"))
        for need in r.needs:
            if not _has(chart, need):
                fails.append((NEED_KEY[need], NEED_REASON[need]))
        ld = _no_long_dated(r, snapshot_expiries, today)
        if ld:
            fails.append(ld)
        e = earnings_block(r, chart.get("earnings"), snapshot_expiries, prefs, today=today)
        if e:
            fails.append(("earnings_inside", e))
        if r.key == "leaps_call" and chart.get("earnings") and (chart["earnings"] or {}).get("date"):
            warns.append(f"crosses earnings on {chart['earnings']['date']}: the stop is the weekly trend, not the print")
        fit = "rejected" if fails else "fit"
        why, must = render(r.key, ctx) if not fails else (None, None)
        rows.append({
            "key": r.key, "label": r.label, "fit": fit,
            "score": None if fails else _score(r, chart, gauge), "step": r.step,
            "why": why, "must_happen": must,
            "reasons": [t for _, t in fails] + warns,
            "reason_key": fails[0][0] if fails else None,
            "shown": False,
        })

    fits = sorted([x for x in rows if x["fit"] == "fit"],
                  key=lambda x: (-x["score"], RULES_INDEX[x["key"]]))
    built = [x for x in fits if x["step"] <= CURRENT_STEP]
    for x in fits:
        x["fit"] = "also_fits"
        x["shown"] = True
        if x["step"] > CURRENT_STEP:
            x["reason_key"] = "not_available_yet"
            x["reasons"] = ["not available yet"]
    if built:
        built[0]["fit"] = "recommended"
    near = sorted([x for x in rows if x["fit"] == "rejected" and len(x["reasons"]) == 1],
                  key=lambda x: RULES_INDEX[x["key"]])[:NEAR_MISS_SHOWN]
    for x in near:
        x["shown"] = True
    strategies = sorted(rows, key=lambda x: (FIT_ORDER[x["fit"]], -(x["score"] or 0.0), RULES_INDEX[x["key"]]))
    return {"strategies": strategies, "recommended": built[0]["key"] if built else None}


def not_yet_sentence(key: str) -> str:
    """The headline's conclusion when the only fit is an unbuilt rule."""
    return RULES_BY_KEY[key].not_yet


def member_strings() -> list[str]:
    """Every member-facing string this module can emit (for the 'no step' test)."""
    out = list(LABELS.values()) + list(CHIP_TEXT.values()) + list(NEED_REASON.values()) + list(SETUP_WORD.values())
    for r in RULES:
        out += [r.why, r.must_happen, r.not_yet]
    return out
