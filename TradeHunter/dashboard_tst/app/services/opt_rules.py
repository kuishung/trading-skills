"""Options v2 rules - the one schema the rules panel renders and the screeners read
(OPTIONS_V2_DESIGN.md section 7).

A member browses ONE strategy at a time; the panel shows the ``shared`` block (the
stock / liquidity / data-age filters every strategy uses) above that strategy's own
block. Every field carries a plain-words ``label`` (shown), a ``help`` (tooltip), a
``unit`` and its bounds, so the template has no second fields table.

The earnings rule is PER STRATEGY (``earnings_rule`` in every strategy block, set
2026-10-09): ``none_inside`` (no report on or before the last expiry), ``short_leg``
(no report on or before the SOLD leg's expiry - the nearer leg of a diagonal /
calendar; every other strategy has one expiry, so it acts like ``none_inside``) and
``allow``. A one-expiry trade defaults to ``none_inside``, LEAPS to ``allow`` (9-18
months always spans reports) and the diagonal / calendar to ``short_leg`` (their
far leg is meant to hold through reports).

The bid/ask $ cap is PER STRATEGY too (``max_leg_spread`` in every strategy block,
moved 2026-10-09; 0 = off): $0.50 on the premium-selling three (``bull_put``,
``bear_call``, ``iron_condor`` - the user's v4.117 band) and off on the other seven,
whose deep / long-dated legs cost $20-$150 on a larger stock, where a flat $0.50 is
under 1% of the price and blocked every LEAPS on a stock above ~$110. The shared
``max_leg_spread_pct`` (% of the option's mid) still applies to every strategy.

Storage: the existing ``user_option_prefs`` row (one per member), ``prefs`` =
``{"schema": 2, "shared": {...}, "<strategy>": {...}}`` holding ONLY the fields the
member changed (sparse) - a later change to a default reaches everyone who never
touched that field. A v1 row (``schema`` absent / 1: blocks ``credit_vertical``,
``long``, ``time`` ...) is migrated on read by ``migrate_v1`` - each family block
copied into every strategy of that family, renamed keys mapped, everything else
dropped - and is rewritten as v2 on the member's first save. A ``shared``
``earnings_rule`` (v1, or a v2 row written before the rule moved) is moved on read
into the strategies whose default is ``none_inside``, and a ``shared``
``max_leg_spread`` into the premium-selling three only (``_lift_shared``; a
strategy's own value wins) - by key presence, the stored ``schema`` stays 2.

Thresholds are ticker-relative (CLAUDE.md): deltas, ATR multiples, % of width /
premium / stock price, days. The only absolute numbers are the member's own floors
(stock price, volumes, open interest, the optional bid/ask $ cap), which are
absolute by nature.

Data plan (v4.134, design §13.5): the data comes from Massive. The Options Starter
plan has NO bid/ask quotes, so the two bid/ask rules (``UNUSED_WITHOUT_QUOTES``)
check nothing there - the screener applies them only to options that carry a bid
and an ask, and the panel greys them out with ``QUOTES_NOTE`` while
``quotes_available()`` (``TST_MASSIVE_QUOTES``, default 0) says the plan has none.
The schema itself is unchanged: a member's saved values stay, ready for a plan with
quotes.

The ORM model is imported lazily so the pure functions work without a database.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any, NamedTuple

SCHEMA_VERSION = 2

STRATEGIES = ("buy_call", "buy_put", "bull_call", "bear_put", "leaps_call", "diagonal_call",
              "bull_put", "bear_call", "iron_condor", "calendar")          # dropdown order
DEFAULT_STRATEGY = "bull_put"

LABELS = {
    "buy_call": "Buy call",
    "buy_put": "Buy put",
    "bull_call": "Bull call spread",
    "bear_put": "Bear put spread",
    "leaps_call": "Buy LEAPS",
    "diagonal_call": "Diagonal call spread",
    "bull_put": "Bull put spread",
    "bear_call": "Bear call spread",
    "iron_condor": "Iron condor",
    "calendar": "Calendar spread",
}

FAMILY = {
    "buy_call": "single", "buy_put": "single",
    "bull_call": "debit_vertical", "bear_put": "debit_vertical",
    "leaps_call": "leaps",
    "diagonal_call": "diagonal",
    "bull_put": "credit_vertical", "bear_call": "credit_vertical",
    "iron_condor": "condor",
    "calendar": "calendar",
}
FAMILIES = ("single", "debit_vertical", "credit_vertical", "leaps", "diagonal", "condor", "calendar")

# The option right(s) each strategy trades ("CP" = both: the condor).
RIGHT = {"buy_call": "C", "buy_put": "P", "bull_call": "C", "bear_put": "P", "leaps_call": "C",
         "diagonal_call": "C", "bull_put": "P", "bear_call": "C", "iron_condor": "CP", "calendar": "C"}

KINDS = ("int", "num", "bool", "choice")


class Field(NamedTuple):
    """One rule. ``kind`` in KINDS; ``lo`` / ``hi`` bound a number (None for bool /
    choice); ``choices`` lists a choice field's values (``CHOICE_LABELS`` words them)."""
    default: Any
    lo: float | None
    hi: float | None
    kind: str
    label: str
    help: str
    unit: str = ""
    choices: tuple | None = None


EARNINGS_CHOICES = ("none_inside", "short_leg", "allow")
CHOICE_LABELS = {"earnings_rule": {"none_inside": "No earnings before the last expiry",
                                   "short_leg": "No earnings before the sold leg's expiry",
                                   "allow": "Earnings allowed"}}

_DELTA_HELP = ("How much the option's price moves for a $1 move in the stock (0.60 = 60 cents). "
               "Read without its sign, so puts use the same numbers.")
_SHORT_DELTA_HELP = ("Roughly the chance the stock finishes past the strike you sell at expiry "
                     "(0.25 = about 1 in 4). Read without its sign.")
_IV_RANK_HELP = ("Where today's 30-day implied volatility sits in its own one-year range: 0 = the "
                 "lowest of the year, 100 = the highest. Buyers prefer it low, sellers high.")
_DTE_HELP = "Calendar days from today to the expiry."


def _iv_rank(lo: float, hi: float) -> dict[str, Field]:
    return {
        "iv_rank_min": Field(lo, 0, 100, "int", "IV rank, from", _IV_RANK_HELP),
        "iv_rank_max": Field(hi, 0, 100, "int", "IV rank, up to", _IV_RANK_HELP),
    }


def _dte(lo: int, hi: int, who: str = "Days to expiry") -> dict[str, Field]:
    return {
        "dte_lo": Field(lo, 1, 1100, "int", f"{who}, from", _DTE_HELP, "days"),
        "dte_hi": Field(hi, 1, 1100, "int", f"{who}, up to", _DTE_HELP, "days"),
    }


# ---- the earnings rule, one per strategy (each strategy's help says why its default)
_EARN_TAIL = ("'No earnings before the last expiry' skips an expiry when the next report falls on or before "
              "it, and skips a stock whose next earnings date is not known. 'Earnings allowed' ignores reports.")
_EARN_ONE_EXPIRY = ("Every option here shares one expiry, so 'before the sold leg's expiry' works the same as "
                    "'before the last expiry'.")
_EARN_ONE_OPTION = ("This trade is one option, so 'before the sold leg's expiry' works the same as 'before the "
                    "last expiry'.")
_EARN_TWO_EXPIRY_TAIL = ("Both 'No earnings' choices skip a stock whose next earnings date is not known. "
                         "'Earnings allowed' ignores reports.")


def _earnings(default: str, help_text: str) -> dict[str, Field]:
    if default not in EARNINGS_CHOICES:
        raise ValueError(default)
    return {"earnings_rule": Field(default, None, None, "choice", "Earnings", help_text, "", EARNINGS_CHOICES)}


# ---- the bid/ask dollar band, one per strategy (moved out of "shared" 2026-10-09).
# A flat $ cap judges a $0.40 option and a $90 one alike: on the deep, long-dated,
# high-priced legs the buying strategies use (LEAPS, the diagonal's far call, the
# calendar's back month) a $0.50 cap is under 1% of the price - tighter than real
# markets on any larger stock - so it is OFF (0) there and the shared %-of-price rule
# does the work. Premium selling keeps the user's $0.50 band (v4.117 playbook).
PREMIUM_SELLING_SPREAD = 0.50
_SPREAD_TAIL = ("$ per share. A wide gap costs you on the way in and again on the way out. 0 turns this "
                "check off; the % of price rule in the shared block still applies.")


def _leg_spread(default: float) -> dict[str, Field]:
    if default > 0:
        why = (f"The gap between the bid and the ask on each option, {_SPREAD_TAIL} ${default:.2f} is the house "
               "band for selling premium: the credit is small, so the gap is a large part of it.")
    else:
        why = (f"The gap between the bid and the ask on each option, {_SPREAD_TAIL} Off by default here: this "
               "trade can buy deep or long-dated options priced $20-$150 on a larger stock, where a fixed "
               "dollar cap would block fair markets.")
    return {"max_leg_spread": Field(default, 0, 50, "num", "Widest bid/ask per option", why, "$")}


_SINGLE = {
    "delta_lo": Field(0.60, 0.01, 0.99, "num", "Delta, from", _DELTA_HELP),
    "delta_hi": Field(0.70, 0.01, 0.99, "num", "Delta, up to", _DELTA_HELP),
    **_dte(30, 60),
    "theta_pct_max": Field(1.0, 0.05, 25, "num", "Daily time decay at most",
                           "What the option loses per day from time passing, as a % of its price. "
                           "1% = a $5.00 option loses about 5 cents a day.", "% a day"),
    **_leg_spread(0),
    **_iv_rank(0, 50),
    **_earnings("none_inside",
                "A report can gap the stock either way overnight, and the option's price usually drops once "
                "the report is out. " + _EARN_ONE_OPTION + " " + _EARN_TAIL),
}

# Width band (changed 2026-10-09 from 0.5-2.0): a bought delta of 0.60-0.70 and a sold
# delta of 0.25-0.35 at 30-60 days sit about 3 to 5.5 ATR apart on ANY stock (the gap
# grows with sqrt(days), and ATR scales with the stock's own volatility), so the old
# 0.5-2.0 band could never list a trade. 1.0-6.0 covers that gap with room either side.
_DEBIT_VERTICAL = {
    "long_delta_lo": Field(0.60, 0.01, 0.99, "num", "Bought option delta, from", _DELTA_HELP),
    "long_delta_hi": Field(0.70, 0.01, 0.99, "num", "Bought option delta, up to", _DELTA_HELP),
    "short_delta_lo": Field(0.25, 0.01, 0.99, "num", "Sold option delta, from", _SHORT_DELTA_HELP),
    "short_delta_hi": Field(0.35, 0.01, 0.99, "num", "Sold option delta, up to", _SHORT_DELTA_HELP),
    "width_atr_lo": Field(1.0, 0.1, 10, "num", "Distance between strikes, from",
                          "Measured in the stock's average daily range (ATR, 14 days) so it "
                          "scales with each stock: 1 ATR is about $11 on a stock that moves $11 a day. With "
                          "the delta bands above, 30-60 days out the two strikes usually sit 3 to 5 ATR apart.",
                          "x ATR"),
    "width_atr_hi": Field(6.0, 0.1, 10, "num", "Distance between strikes, up to",
                          "The widest spread, in the stock's average daily range (ATR).", "x ATR"),
    "debit_pct_max": Field(60, 1, 100, "num", "Cost at most, % of the distance",
                           "What you pay against the most the spread can be worth at expiry. 60% = pay "
                           "at most $3.00 for a $5-wide spread.", "%"),
    **_dte(30, 60),
    **_leg_spread(0),
    **_iv_rank(0, 70),
    **_earnings("none_inside",
                "A report can gap the stock past both strikes overnight, before you can get out. "
                + _EARN_ONE_EXPIRY + " " + _EARN_TAIL),
}

_CREDIT_VERTICAL = {
    "short_delta_lo": Field(0.20, 0.01, 0.99, "num", "Sold option delta, from", _SHORT_DELTA_HELP),
    "short_delta_hi": Field(0.30, 0.01, 0.99, "num", "Sold option delta, up to", _SHORT_DELTA_HELP),
    "width_atr_lo": Field(0.5, 0.1, 10, "num", "Distance between strikes, from",
                          "Measured in the stock's average daily range (ATR, 14 days) so it "
                          "scales with each stock.", "x ATR"),
    "width_atr_hi": Field(1.5, 0.1, 10, "num", "Distance between strikes, up to",
                          "The widest spread you will sell, in the stock's average daily range (ATR).",
                          "x ATR"),
    "credit_pct_min": Field(25, 1, 300, "num", "Credit at least, % of the max loss",
                            "What you collect against what you can lose (the distance less the credit). "
                            "25% = collect at least $1.00 to risk $4.00.", "%"),
    **_dte(30, 60),
    **_leg_spread(PREMIUM_SELLING_SPREAD),
    **_iv_rank(30, 100),
    **_earnings("none_inside",
                "A report can gap the stock through the strike you sold overnight - straight to the max loss. "
                + _EARN_ONE_EXPIRY + " " + _EARN_TAIL),
}

SCHEMA: dict[str, dict[str, Field]] = {
    "shared": {
        "price_min": Field(20.0, 0, 100000, "num", "Stock price at least",
                           "Skip stocks priced under this. Cheap stocks tend to have few strikes and "
                           "wide option markets.", "$"),
        "stock_vol_min": Field(0, 0, 1_000_000_000, "int", "Stock volume at least (20-day average)",
                               "Average shares traded per day over the last 20 sessions. "
                               "0 turns this check off.", "shares"),
        "oi_min": Field(100, 0, 1_000_000, "int", "Open interest per option at least",
                        "Contracts already open at that strike. With too few you may not get out at a "
                        "fair price. An option whose open interest the data feed did not report fails this "
                        "check unless it is 0.", "contracts"),
        "opt_vol_min": Field(0, 0, 1_000_000, "int", "Option volume today per option at least",
                             "Contracts traded today at that strike. 0 turns this check off (early in "
                             "the session most strikes show 0).", "contracts"),
        "max_leg_spread_pct": Field(25, 1, 200, "num", "Widest bid/ask per option, % of its price",
                                    "The gap between the bid and the ask on each option, measured against "
                                    "the option's mid price, so cheap and dear options are judged fairly. "
                                    "Each strategy can also set a $ cap of its own. Checked only on options "
                                    "that carry a bid and an ask (a data plan without quotes has none).",
                                    "%"),
        "monthly_only": Field(False, None, None, "bool", "Monthly expiries only",
                              "Only the third-Friday expiries, which usually have the most open interest "
                              "and the tightest markets."),
        "max_age_h": Field(24, 0.25, 720, "num", "Ignore quotes older than",
                           "A trade is listed only when every option's quote is at most this old. The clock "
                           "stops while the US market is closed (nights, weekends, holidays): a quote taken "
                           "after the last close counts as current until the next open, and an older one "
                           "stays as old as it was at the close. Each quote keeps its own time - the data "
                           "feed's (Massive, 15 minutes delayed on the current plan).",
                           "hours"),
        "per_ticker": Field(3, 1, 50, "int", "Best trades per stock",
                            "List at most this many trades for each stock - the best by this strategy's "
                            "score.", "trades"),
    },
    "buy_call": dict(_SINGLE),
    "buy_put": dict(_SINGLE),
    "bull_call": dict(_DEBIT_VERTICAL),
    "bear_put": dict(_DEBIT_VERTICAL),
    "leaps_call": {
        "delta_lo": Field(0.70, 0.01, 0.99, "num", "Delta, from",
                          "A deep in-the-money call moves most of a dollar for each $1 of the stock - "
                          "0.80 behaves like about 80 shares."),
        "delta_hi": Field(0.85, 0.01, 0.99, "num", "Delta, up to", _DELTA_HELP),
        "months_lo": Field(9, 1, 36, "int", "Months to expiry, from",
                           "Long-dated calls bought instead of the stock (a month is 30.44 days).", "months"),
        "months_hi": Field(18, 1, 36, "int", "Months to expiry, up to",
                           "The furthest expiry you will buy.", "months"),
        # 25, not 10 (changed 2026-10-09): measured against the OPTION's price, a 9-18 month
        # call at delta 0.70-0.85 carries about 18-85% time value at 4% rates and IV 20-60%
        # (the interest on the strike alone is ~2-5% of the stock price), the deep end ~18-32%
        # - so 10 could never list a trade. 25 lists the deepest strikes of the nearer LEAPS.
        "extrinsic_pct_max": Field(25, 0, 100, "num", "Time value at most, % of the price",
                                   "The part of the price that is not intrinsic value - what you pay for "
                                   "time. Lower = more like owning the stock. A deep 9-month call at delta "
                                   "0.85 is usually about a fifth time value.", "%"),
        **_leg_spread(0),
        **_iv_rank(0, 50),
        **_earnings("allow",
                    "A call 9 to 18 months out always has reports before it expires. Bought instead of the "
                    "stock, it is meant to hold through them as the shares would, so reports are allowed by "
                    "default. " + _EARN_ONE_OPTION + " " + _EARN_TAIL),
    },
    "diagonal_call": {
        "long_delta_lo": Field(0.70, 0.01, 0.99, "num", "Bought call delta, from",
                               "The long-dated call you own. " + _DELTA_HELP),
        "long_delta_hi": Field(0.80, 0.01, 0.99, "num", "Bought call delta, up to", _DELTA_HELP),
        "long_dte_lo": Field(180, 30, 1100, "int", "Bought call days to expiry, from", _DTE_HELP, "days"),
        "long_dte_hi": Field(365, 30, 1100, "int", "Bought call days to expiry, up to", _DTE_HELP, "days"),
        "short_delta_lo": Field(0.20, 0.01, 0.99, "num", "Sold call delta, from",
                                "The near-term call you sell against it. " + _SHORT_DELTA_HELP),
        "short_delta_hi": Field(0.30, 0.01, 0.99, "num", "Sold call delta, up to", _SHORT_DELTA_HELP),
        "short_dte_lo": Field(30, 1, 365, "int", "Sold call days to expiry, from", _DTE_HELP, "days"),
        "short_dte_hi": Field(45, 1, 365, "int", "Sold call days to expiry, up to", _DTE_HELP, "days"),
        "debit_pct_spot_max": Field(25, 0.5, 100, "num", "Net cost at most, % of the stock price",
                                    "What the pair costs (bought call less sold call) against the stock "
                                    "price - the capital it ties up instead of 100 shares.", "%"),
        **_leg_spread(0),
        **_iv_rank(0, 60),
        **_earnings("short_leg",
                    "The default keeps reports out of the life of the near call you sell - a report can gap "
                    "the stock through its strike. The far call you own may hold through a report, as the "
                    "shares would. 'No earnings before the last expiry' keeps reports out of the far call's "
                    "life too (with a 6 to 12 month call that rarely passes). " + _EARN_TWO_EXPIRY_TAIL),
    },
    "bull_put": dict(_CREDIT_VERTICAL),
    "bear_call": dict(_CREDIT_VERTICAL),
    "iron_condor": {
        "short_delta_lo": Field(0.15, 0.01, 0.99, "num", "Sold options' delta, from",
                                "Both sold options (the put below and the call above). " + _SHORT_DELTA_HELP),
        "short_delta_hi": Field(0.20, 0.01, 0.99, "num", "Sold options' delta, up to", _SHORT_DELTA_HELP),
        "wing_atr_lo": Field(0.5, 0.1, 10, "num", "Wing width, from",
                             "The distance from each sold strike to its bought strike, in the stock's "
                             "average daily range (ATR, 14 days).", "x ATR"),
        "wing_atr_hi": Field(1.5, 0.1, 10, "num", "Wing width, up to",
                             "The widest wing, in the stock's average daily range (ATR).", "x ATR"),
        "credit_pct_min": Field(30, 1, 300, "num", "Credit at least, % of the max loss",
                                "What you collect against what you can lose (the wider wing less the "
                                "credit).", "%"),
        **_dte(30, 45),
        **_leg_spread(PREMIUM_SELLING_SPREAD),
        **_iv_rank(50, 100),
        **_earnings("none_inside",
                    "A report can gap the stock through either sold strike overnight - straight to that "
                    "side's max loss. " + _EARN_ONE_EXPIRY + " " + _EARN_TAIL),
    },
    "calendar": {
        "delta_tol": Field(0.05, 0.01, 0.25, "num", "Strike within this of delta 0.50",
                           "The calendar uses the call nearest at the money; this is how far its delta "
                           "may sit from 0.50."),
        "front_dte_lo": Field(20, 1, 365, "int", "Near (sold) option days to expiry, from", _DTE_HELP, "days"),
        "front_dte_hi": Field(30, 1, 365, "int", "Near (sold) option days to expiry, up to", _DTE_HELP, "days"),
        "back_dte_lo": Field(50, 2, 1100, "int", "Far (bought) option days to expiry, from", _DTE_HELP, "days"),
        "back_dte_hi": Field(70, 2, 1100, "int", "Far (bought) option days to expiry, up to", _DTE_HELP, "days"),
        "front_iv_ge_back": Field(False, None, None, "bool", "Near IV at least the far IV",
                                  "Only list calendars where the option you sell is priced at the same or "
                                  "a higher implied volatility than the one you buy."),
        **_leg_spread(0),
        **_iv_rank(0, 60),
        **_earnings("short_leg",
                    "The default keeps reports out of the life of the near call you sell. A report after it "
                    "expires but before the far call does is a common calendar: the far call's price tends to "
                    "rise into the report. 'No earnings before the last expiry' keeps reports out of both. "
                    + _EARN_TWO_EXPIRY_TAIL),
    },
}
assert set(SCHEMA) == {"shared", *STRATEGIES}, "SCHEMA must hold shared + every strategy"

BLOCKS = ("shared",) + STRATEGIES

# Where a block-level shared ``earnings_rule`` (v1, or a v2 row saved before the rule
# moved into each strategy on 2026-10-09) goes on read: every strategy whose own
# default is none_inside. LEAPS / diagonal / calendar keep their own defaults.
EARNINGS_FROM_SHARED = tuple(s for s in STRATEGIES if SCHEMA[s]["earnings_rule"].default == "none_inside")
# Where a stored shared ``max_leg_spread`` (v1, or a v2 row saved before the $ band moved
# into each strategy on 2026-10-09) goes on read: the premium-selling strategies only (the
# ones whose own default is on). Everywhere else the band is off by default - carrying a
# shared $0.50-style cap onto LEAPS / diagonal / calendar would bring back the very block
# the move removed.
SPREAD_FROM_SHARED = tuple(s for s in STRATEGIES if SCHEMA[s]["max_leg_spread"].default > 0)
# v1's "defined_risk_only" let reports inside the defined-risk spreads and kept them out
# of single options: read as "allow" on those spreads, the default elsewhere.
_V1_DEFINED_RISK = frozenset({"bull_call", "bear_put", "bull_put", "bear_call", "iron_condor"})
# fields that used to live in the shared block - a stale "shared.<name>" form key
# resolves to the strategy being edited
_MOVED_FROM_SHARED = frozenset({"earnings_rule", "max_leg_spread"})

# ---- the data plan (v4.134, design §13.5). Massive's Options Starter plan returns no
# bid/ask quotes (``last_quote`` comes only with Advanced and up), so the two bid/ask
# rules have nothing to check: the screener skips them on every option without a bid
# and an ask (and says so in its funnel); the panel greys these fields out with the
# note below while ``quotes_available()`` is False. Saved values are kept as they are.
QUOTES_ENV = "TST_MASSIVE_QUOTES"
QUOTES_NOTE = "not used - the current data plan (Massive Starter) has no bid/ask"
UNUSED_WITHOUT_QUOTES = {"max_leg_spread": QUOTES_NOTE, "max_leg_spread_pct": QUOTES_NOTE}
assert all(any(k in SCHEMA[b] for b in BLOCKS) for k in UNUSED_WITHOUT_QUOTES)


def quotes_available() -> bool:
    """Whether the data plan includes bid/ask quotes: ``TST_MASSIVE_QUOTES`` (``app/.env``)
    is 1 / true / yes / on. Default 0 - the Options Starter plan has none. Read on every
    call, so a changed setting needs no import-time reload."""
    return str(os.environ.get(QUOTES_ENV, "0") or "0").strip().lower() in _TRUE

# band pairs a "from" above its "to" would empty the list
_PAIR_SUFFIXES = (("_lo", "_hi"), ("_min", "_max"))

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}
_SKIP = object()


# ------------------------------------------------------------------ the pure part
def fields(strategy: str) -> list[tuple[str, str, Field]]:
    """``(block, name, Field)`` rows the panel renders: shared first, then the
    strategy's own, in SCHEMA order. KeyError for an unknown strategy."""
    if strategy not in STRATEGIES:
        raise KeyError(strategy)
    return [("shared", k, f) for k, f in SCHEMA["shared"].items()] + \
           [(strategy, k, f) for k, f in SCHEMA[strategy].items()]


_STEPS = {"max_leg_spread": 0.05, "width_atr_lo": 0.1, "width_atr_hi": 0.1, "wing_atr_lo": 0.1,
          "wing_atr_hi": 0.1, "theta_pct_max": 0.1, "max_age_h": 0.25}


def step(name: str, f: Field) -> float | None:
    """The input step the panel uses for a number field (None for bool / choice)."""
    if f.kind == "int":
        return 1
    if f.kind != "num":
        return None
    if f.hi is not None and f.hi <= 1:
        return 0.01
    return _STEPS.get(name, 1)


def defaults(strategy: str) -> dict:
    """The flat house rules for one strategy: shared defaults + the strategy's."""
    if strategy not in STRATEGIES:
        raise KeyError(strategy)
    out = {k: f.default for k, f in SCHEMA["shared"].items()}
    out.update({k: f.default for k, f in SCHEMA[strategy].items()})
    return out


def house() -> dict:
    """Every block at its default, in the merged v2 shape ``read`` returns."""
    return clean({"schema": SCHEMA_VERSION})


def _coerce(value, f: Field):
    """A stored value -> an in-bounds value of the field's kind (clamped), or the
    default when it is missing / of the wrong kind. Never raises."""
    if value is None:
        return f.default
    if f.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        return True if s in _TRUE else False if s in _FALSE else f.default
    if f.kind == "choice":
        s = str(value).strip()
        return s if s in (f.choices or ()) else f.default
    if isinstance(value, bool):
        return f.default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f.default
    if not math.isfinite(v):
        return f.default
    if f.lo is not None and v < f.lo:
        v = float(f.lo)
    if f.hi is not None and v > f.hi:
        v = float(f.hi)
    return int(round(v)) if f.kind == "int" else float(v)


def _clean_block(block: str, src) -> dict:
    src = src if isinstance(src, dict) else {}
    return {k: _coerce(src.get(k), f) for k, f in SCHEMA[block].items()}


def _sparse(raw: dict) -> dict:
    """A v2 dict (full or sparse) -> only the in-schema fields that differ from the
    default, coerced. Unknown blocks and keys are dropped."""
    out: dict = {}
    for block in BLOCKS:
        src = raw.get(block)
        if not isinstance(src, dict):
            continue
        over = {}
        for k, f in SCHEMA[block].items():
            if src.get(k) is None:
                continue
            v = _coerce(src[k], f)
            if v != f.default:
                over[k] = v
        if over:
            out[block] = over
    return out


# v1 layout (option_prefs, schema 1): one block per engine family. Each family block
# is copied into every strategy of that family; these keys are renamed on the way.
_V1_FAMILY_STRATEGIES = {
    "credit_vertical": ("bull_put", "bear_call"),
    "debit_vertical": ("bull_call", "bear_put"),
    "long": ("buy_call", "buy_put"),
    "leaps": ("leaps_call",),
    "condor": ("iron_condor",),
    "time": ("calendar", "diagonal_call"),
}
_V1_RENAMES = {
    "shared": {"min_oi": "oi_min"},
    "credit_vertical": {"iv_gate_min": "iv_rank_min"},
    "time": {"cal_front_lo": "front_dte_lo", "cal_front_hi": "front_dte_hi",
             "cal_back_lo": "back_dte_lo", "cal_back_hi": "back_dte_hi", "cal_delta_tol": "delta_tol",
             "diag_long_delta_lo": "long_delta_lo", "diag_long_delta_hi": "long_delta_hi",
             "diag_long_dte_lo": "long_dte_lo", "diag_long_dte_hi": "long_dte_hi",
             "diag_short_delta_lo": "short_delta_lo", "diag_short_delta_hi": "short_delta_hi",
             "diag_short_dte_lo": "short_dte_lo", "diag_short_dte_hi": "short_dte_hi"},
}
# same name, different meaning: v1 measured LEAPS time value against the STOCK price,
# v2 against the option's price - carrying the number over would change the filter
_V1_DROP = {"leaps": {"extrinsic_pct_max"}}


def _legacy_earnings(value, strategy: str):
    """A shared-block earnings choice -> the value for ``strategy`` (v1's
    "defined_risk_only" read per strategy; anything else unchanged - ``_coerce``
    later turns an unknown value into the default)."""
    if isinstance(value, str) and value.strip() == "defined_risk_only":
        return "allow" if strategy in _V1_DEFINED_RISK else "none_inside"
    return value


def _same(value, strategy: str):
    return value


# field moved out of "shared" -> (the strategies it goes to, how the old value reads there)
_LIFT = {"earnings_rule": (EARNINGS_FROM_SHARED, _legacy_earnings),
         "max_leg_spread": (SPREAD_FROM_SHARED, _same)}


def _lift_shared(raw: dict) -> dict:
    """``raw`` with every block-level shared field that has moved into the strategies
    (``_LIFT``: ``earnings_rule`` -> EARNINGS_FROM_SHARED, ``max_leg_spread`` ->
    SPREAD_FROM_SHARED) copied into each of its strategies that has no value of its
    own, and the shared key dropped. A new dict - ``raw`` and its blocks are never
    mutated (a stored row's JSON); ``raw`` itself when there is nothing to move."""
    shared = raw.get("shared")
    if not isinstance(shared, dict) or not any(k in shared for k in _LIFT):
        return raw
    out = dict(raw)
    shared = dict(shared)
    out["shared"] = shared
    for name, (strategies, read_as) in _LIFT.items():
        if name not in shared:
            continue
        value = shared.pop(name)
        if value is None:
            continue
        for s in strategies:
            block = dict(out[s]) if isinstance(out.get(s), dict) else {}
            if block.get(name) is None:
                block[name] = read_as(value, s)
            out[s] = block
    return out


_lift_shared_earnings = _lift_shared      # the name OPTIONS_V2_DESIGN.md section 7 uses


def migrate_v1(raw: dict | None) -> dict:
    """A v1 ``prefs`` dict -> the sparse v2 dict (no ``schema`` key). Shared keys
    that still exist are kept (``min_oi`` becomes ``oi_min``); the shared earnings
    choice moves into the strategies of EARNINGS_FROM_SHARED ("defined_risk_only"
    = "allow" on the defined-risk spreads) and the shared bid/ask $ cap into those
    of SPREAD_FROM_SHARED; each family block is copied into every strategy of that
    family (renamed keys mapped); everything else - telegram, exit lines, retired
    fields - is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    staged: dict[str, dict] = {}
    shared = raw.get("shared")
    if isinstance(shared, dict):
        ren = _V1_RENAMES["shared"]
        staged["shared"] = {ren.get(k, k): v for k, v in shared.items()}
    for fam, strategies in _V1_FAMILY_STRATEGIES.items():
        block = raw.get(fam)
        if not isinstance(block, dict):
            continue
        ren = _V1_RENAMES.get(fam, {})
        drop = _V1_DROP.get(fam, set())
        vals = {ren.get(k, k): v for k, v in block.items() if k not in drop}
        for strategy in strategies:
            staged.setdefault(strategy, {}).update({k: v for k, v in vals.items() if k in SCHEMA[strategy]})
    return _sparse(_lift_shared(staged))


def _as_v2_sparse(raw) -> dict:
    """Whatever is stored -> the sparse v2 dict (migrating a v1 row, and a v2 row's
    pre-2026-10-09 shared earnings rule / bid-ask $ cap - by key presence, ``schema``
    stays 2)."""
    raw = raw if isinstance(raw, dict) else {}
    try:
        version = int(raw.get("schema") or 1)
    except (TypeError, ValueError):
        version = 1
    return _sparse(_lift_shared(raw)) if version >= SCHEMA_VERSION else migrate_v1(raw)


def clean(raw: dict | None) -> dict:
    """Stored prefs (v1 or v2, sparse or full) -> the merged v2 dict: ``schema`` 2,
    ``shared`` and every strategy, each with every field. Pure."""
    sparse = _as_v2_sparse(raw)
    out: dict = {"schema": SCHEMA_VERSION}
    for block in BLOCKS:
        out[block] = _clean_block(block, sparse.get(block))
    return out


def for_strategy(prefs: dict | None, strategy: str) -> dict:
    """``{"strategy", "shared": {...}, "rules": {...}}`` - what ``opt_screen`` reads.
    ``prefs`` is ``read``'s output (a sparse or partial dict also works: missing
    fields take their defaults). KeyError for an unknown strategy."""
    if strategy not in STRATEGIES:
        raise KeyError(strategy)
    p = prefs if isinstance(prefs, dict) else {}
    if p.get("schema") is not None or any(k in p for k in _V1_FAMILY_STRATEGIES):
        p = clean(p)          # a stored row (v1 or v2); a bare {"shared", "<strategy>"} dict is read as-is
    else:
        p = _lift_shared(p)      # ... except an old-style shared earnings rule / $ cap
    return {"strategy": strategy, "shared": _clean_block("shared", p.get("shared")),
            "rules": _clean_block(strategy, p.get(strategy))}


def prefs_hash(merged: dict) -> str:
    """12 hex of sha1 over the merged v2 dict (sorted keys, floats to 4 dp) - stored
    in ``user_option_prefs.prefs_hash`` (a cache key for a member's results)."""
    def canon(v):
        return round(v, 4) if isinstance(v, float) else v
    sub = {b: {k: canon(v) for k, v in (merged.get(b) or {}).items()} for b in BLOCKS}
    blob = json.dumps(sub, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _fmt(v: float) -> str:
    return f"{v:g}"


def parse(value, f: Field, *, name: str = ""):
    """A posted form value -> ``(value, error)``. ``value`` is ``_SKIP`` when nothing
    should be stored (blank number / choice, or unreadable input). Booleans read
    "on" / "true" / "1" / "yes" as True and "" / "off" / "false" / "0" / "no" as
    False. A number outside the bounds is clamped AND reported."""
    label = f.label or name
    if f.kind == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)):
            return bool(value), None
        s = str(value if value is not None else "").strip().lower()
        if s in _TRUE:
            return True, None
        if s in _FALSE:
            return False, None
        return _SKIP, f"{label}: '{value}' is not on or off."
    if value is None:
        return _SKIP, None
    if f.kind == "choice":
        s = str(value).strip()
        if s == "":
            return _SKIP, None
        if s in (f.choices or ()):
            return s, None
        words = CHOICE_LABELS.get(name, {})
        allowed = ", ".join(words.get(c, c) for c in (f.choices or ()))
        return _SKIP, f"{label}: choose one of {allowed}."
    if isinstance(value, bool):
        return _SKIP, f"{label} must be a number."
    s = str(value).strip().replace(",", "") if isinstance(value, str) else value
    if s == "":
        return _SKIP, None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return _SKIP, f"{label} must be a number."
    if not math.isfinite(v):
        return _SKIP, f"{label} must be a number."
    err = None
    if f.lo is not None and v < f.lo:
        err = f"{label}: {_fmt(v)} is under the lowest allowed, set to {_fmt(f.lo)}."
        v = float(f.lo)
    elif f.hi is not None and v > f.hi:
        err = f"{label}: {_fmt(v)} is over the highest allowed, set to {_fmt(f.hi)}."
        v = float(f.hi)
    return (int(round(v)) if f.kind == "int" else float(v)), err


def _resolve(key: str, strategy: str) -> tuple[str | None, str | None]:
    """A posted form key -> ``(block, field)``: "shared.x" / "shared__x",
    "<strategy>.x" / "<strategy>__x", or a bare name (shared first, then the
    strategy - no name is in both). "shared.earnings_rule" (a page from before the
    rule moved) is the strategy's. Anything else -> (None, None)."""
    key = str(key)
    for sep in (".", "__"):
        if sep in key:
            block, name = key.split(sep, 1)
            if block in ("shared", strategy) and name in SCHEMA[block]:
                return block, name
            if block == "shared" and name in _MOVED_FROM_SHARED and name in SCHEMA[strategy]:
                return strategy, name
            return None, None
    if key in SCHEMA["shared"]:
        return "shared", key
    if key in SCHEMA[strategy]:
        return strategy, key
    return None, None


def band_errors(merged: dict, strategy: str) -> list[str]:
    """Plain-words notes for every "from" above its "to" in the shared block and
    the strategy's block (no trade can pass such a band)."""
    out = []
    for block in ("shared", strategy):
        vals = merged.get(block) or {}
        for lo_sfx, hi_sfx in _PAIR_SUFFIXES:
            for lo_k in [k for k in SCHEMA[block] if k.endswith(lo_sfx)]:
                hi_k = lo_k[: -len(lo_sfx)] + hi_sfx
                if hi_k in vals and lo_k in vals and vals[lo_k] > vals[hi_k]:
                    out.append(f"{SCHEMA[block][lo_k].label} ({_fmt(vals[lo_k])}) is above "
                               f"{SCHEMA[block][hi_k].label} ({_fmt(vals[hi_k])}): no trade can pass.")
    return out


# ------------------------------------------------------------------ storage
def _model():
    from ..models import UserOptionPrefs   # noqa: WPS433 - lazy: the pure part needs no ORM
    return UserOptionPrefs


def _row(db, user):
    """The member's user_option_prefs row or None."""
    if user is None:
        return None
    uid = getattr(user, "id", None)
    if db is not None and uid is not None:
        model = _model()
        return db.query(model).filter(model.user_id == uid).one_or_none()
    try:
        return getattr(user, "option_prefs", None)
    except Exception:  # noqa: BLE001 - a detached stub without the relationship
        return None


def _stored(row) -> dict:
    return (getattr(row, "prefs", None) if row is not None else None) or {}


def read(db, user) -> dict:
    """The member's merged v2 rules (``clean`` over the stored row; a v1 row is
    migrated in memory, never written here). House defaults when there is no row."""
    return clean(_stored(_row(db, user)))


def _store(db, user, row, sparse: dict) -> None:
    """Write ``{"schema": 2, **sparse}`` + hash + schema_version (creating the row
    on first write). The JSON is reassigned so SQLAlchemy sees the change."""
    merged = clean({"schema": SCHEMA_VERSION, **sparse})
    if row is None:
        model = _model()
        row = model(user_id=user.id, prefs={}, prefs_hash=prefs_hash(merged), schema_version=SCHEMA_VERSION)
        db.add(row)
    row.prefs = {"schema": SCHEMA_VERSION, **{k: dict(v) for k, v in sparse.items()}}
    row.prefs_hash = prefs_hash(merged)
    row.schema_version = SCHEMA_VERSION
    db.commit()


def write(db, user, strategy: str, form: dict | None) -> tuple[dict, list[str]]:
    """Partial update: only the posted fields of the shared block and ``strategy``'s
    block change (keys as ``_resolve`` reads them; unknown keys are ignored). A
    number outside its bounds is clamped and stored, and reported; an unreadable
    value is reported and not stored. A field posted equal to its default drops
    back to "not changed". Returns ``(read(db, user), errors)``; ``errors`` also
    names any "from" left above its "to"."""
    if strategy not in STRATEGIES:
        return read(db, user), [f"Unknown strategy {strategy!r}."]
    row = _row(db, user)
    sparse = _as_v2_sparse(_stored(row))
    errors: list[str] = []
    changed = False
    for key, raw in (form or {}).items():
        block, name = _resolve(key, strategy)
        if block is None:
            continue
        f = SCHEMA[block][name]
        val, err = parse(raw, f, name=name)
        if err:
            errors.append(err)
        if val is _SKIP:
            continue
        over = dict(sparse.get(block) or {})
        if val == f.default:
            over.pop(name, None)
        else:
            over[name] = val
        if over:
            sparse[block] = over
        else:
            sparse.pop(block, None)
        changed = True
    if changed and user is not None:
        _store(db, user, row, sparse)        # a v1 row is rewritten as v2 here
    merged = clean({"schema": SCHEMA_VERSION, **sparse})
    errors.extend(band_errors(merged, strategy))
    return merged, errors


def reset(db, user, strategy: str, *, shared: bool = True) -> dict:
    """Back to the defaults for what the panel shows: ``strategy``'s block and (by
    default) the shared block. ``strategy`` "all" / None clears every block.
    Returns the merged rules; an unknown strategy changes nothing."""
    if strategy not in (None, "all") and strategy not in STRATEGIES:
        return read(db, user)
    row = _row(db, user)
    sparse = _as_v2_sparse(_stored(row))
    if strategy in (None, "all"):
        sparse = {}
    else:
        sparse.pop(strategy, None)
        if shared:
            sparse.pop("shared", None)
    if user is not None:
        _store(db, user, row, sparse)
    return clean({"schema": SCHEMA_VERSION, **sparse})
