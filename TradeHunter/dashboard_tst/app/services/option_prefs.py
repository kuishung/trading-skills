"""A member's option rules - the ONE schema, the house defaults, the merge, the hash.

Every number the strike picker filters on and every line the My-rules drawer
renders comes from ``SCHEMA`` below: one table, two consumers. The engines read a
field's ``default / lo / hi / kind``; the drawer renders its ``label / help /
plain / step / unit`` (OPTIONS_MODULE_DESIGN.md II.2.3; part_B_engines.md B4.1
for the fields, part_D_ui.md D3.1 for the presentation columns). There is no
second fields table anywhere.

Storage is SPARSE: ``user_option_prefs.prefs`` (a JSON column, one row per
member) holds only the fields the member changed, merged over ``HOUSE`` on every
read - the ``sym_conds`` pattern (``ema_setup.clean_enabled`` over
``COND_DEFAULT``). So a later change to a house default flows to everyone who
never touched that field, and members on house defaults share ONE signal row per
symbol (the house hash).

Three things that look like rules are deliberately NOT fields:

* **account value / risk per trade** - read from ``trade_prefs`` (the same two
  numbers that size a Curated share trade; one account value sizes everything)
  and returned under ``read()``'s ``account`` key; the four credit exit lines
  likewise stay in ``trade_prefs``;
* **Telegram** - its OWN top-level key ``telegram`` in the same JSON, outside the
  blocks, written only by ``POST /options/telegram``;
* **the 10 % notional cap and the chart stop / target / pad** - constants in
  ``opt_constants`` (CLAUDE.md: global, never overridden).

None of the three is hashed: ``prefs_hash()`` covers ``PICK_FIELDS`` only - what
changes a pick - so an account-value or exit-line edit never invalidates a cached
pick (sizing runs at read time).

Units: every threshold is ticker-relative - a delta, an ATR multiple, a ratio, a
count of days or months, a percent of width / premium / share price. The only
absolute numbers are the member's own liquidity floors (open interest, bid/ask
width, day volume), which ARE absolute by nature.

Portable JSON only (the Postgres swap stays a config change). The model
``UserOptionPrefs`` (``user_id, prefs, prefs_hash, schema_version, updated_at``)
is imported lazily, so the pure functions here work before the migration lands.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import namedtuple

from . import trade_prefs
from .opt_constants import LEVEL_PAD_ATR, MAX_POSITION_PCT, STOP_ATR, TARGET_R  # noqa: F401  - shown as fixed conventions
from .strategy_rules import DEFINED_RISK, FAMILY_OF, STRATEGY_KEYS  # noqa: F401  - the ONE home of the keys

# default lo hi kind label help plain step unit - exactly this positional order.
# kind in {"num", "int", "bool", "choice"}; nothing else is a field.
Field = namedtuple("Field", "default lo hi kind label help plain step unit")

SCHEMA_VERSION = 1

BLOCKS = ("shared", "credit_vertical", "debit_vertical", "long", "leaps", "condor", "time")
TABS = ("shared", "credit", "debit", "condor", "time")
TAB_LABELS = {"shared": "Shared", "credit": "Credit spreads", "debit": "Buy call/put",
              "condor": "Iron condor", "time": "Time spreads"}
TAB_BLOCKS = {"shared": ("shared",), "credit": ("credit_vertical",),
              "debit": ("long", "debit_vertical", "leaps"),     # three sub-sections on one tab
              "condor": ("condor",), "time": ("time",)}
BLOCK_LABELS = {"shared": "Shared", "credit_vertical": "Credit spreads", "debit_vertical": "Spreads",
                "long": "Buy call / put", "leaps": "LEAPS", "condor": "Iron condor", "time": "Time spreads"}
CHOICES = {"earnings_rule": ("none_inside", "defined_risk_only")}      # there is NO "allowed"

# The block each strategy's picker reads (strategy_rules.FAMILY_OF, re-exported).
# {"bull_put": "credit_vertical", "bear_call": "credit_vertical", "bull_call": "debit_vertical",
#  "bear_put": "debit_vertical", "buy_call": "long", "buy_put": "long", "leaps_call": "leaps",
#  "iron_condor": "condor", "calendar": "time", "diagonal_call": "time"}
# The block whose premium_stop_pct a family without its own inherits (B5.1):
# the debit verticals and the calendar use the long block's 50, the diagonal the leaps block's 40.
_RULE_STOP_BLOCK = {"debit_vertical": "long", "calendar": "long", "diagonal_call": "leaps"}

SCHEMA: dict[str, dict[str, Field]] = {
    "shared": {
        "min_oi": Field(500, 0, 100000, "int", "Minimum open interest per leg",
                        "Contracts outstanding at a strike. Below this you may not get out.",
                        "every leg has at least {v} open contracts, so you can get out", 50, "contracts"),
        "oi_per_contract": Field(10, 1, 100, "int", "... and at least this many times your contracts",
                                 "Open interest scaled to your order: a 10-lot wants 100 open.",
                                 "and at least {v}x the contracts you trade", 1, "x"),
        "max_leg_spread": Field(0.50, 0.01, 5.00, "num", "Widest bid/ask allowed per leg",
                                "The cost of getting in and out. Wider markets eat the edge.",
                                "no leg quoted wider than ${v}", 0.05, "$"),
        "min_leg_volume": Field(20, 0, 10000, "int", "Traded today per leg (warning only)",
                                "Never vetoes a strike; it only adds a note.",
                                "a quiet strike (under {v} traded today) is flagged, not dropped", 10, "contracts"),
        "earnings_rule": Field("none_inside", None, None, "choice", "Earnings inside the trade",
                               "An earnings report before expiry is the one thing a stop cannot protect you from.",
                               "not allowed / defined-risk trades only", None, ""),
        "monthly_only": Field(False, None, None, "bool", "Monthly expiries only",
                              "Third-Friday expiries have the deepest markets.",
                              "monthly expiries only / any listed expiry", None, ""),
        "chart_constraint": Field(True, None, None, "bool", "Strikes must respect the chart",
                                  "Short strikes under support / above resistance / outside the range, and under the trend line when there is one.",
                                  "short strikes stay outside the level the chart says must hold", None, ""),
        "gap_mult": Field(2.0, 1.0, 5.0, "num", "Worst case allowed, as a multiple of your risk budget",
                          "A gap through the stop may cost this many times what you planned to risk - never seven times.",
                          "a gap through the stop may cost at most {v}x your risk budget", 0.5, "x"),
    },
    "credit_vertical": {   # bull_put, bear_call
        "short_delta_lo": Field(0.20, 0.05, 0.50, "num", "Short strike delta, from",
                                "About the chance the stock is past the strike at expiry. 0.20 is about 1-in-5.",
                                "about a 70-80% chance it expires worthless", 0.01, ""),
        "short_delta_hi": Field(0.30, 0.05, 0.50, "num", "... to",
                                "The top of the band: the closest strike you will sell.",
                                "the nearest strike you will sell", 0.01, ""),
        "width_atr_lo": Field(0.5, 0.1, 5, "num", "Spread width, in ATRs, from",
                              "Ticker-relative: about $6-17 on LRCX (ATR 11.5), $1-3 on a $40 name. The $ figure is shown beside it.",
                              "width {lo}-{hi} ATR (about ${lo_usd}-{hi_usd} on {sym})", 0.1, "x ATR"),
        "width_atr_hi": Field(1.5, 0.1, 5, "num", "... to",
                              "The widest spread you will sell, in the stock's daily range.",
                              "the widest spread you will sell", 0.1, "x ATR"),
        "long_offset_max": Field(3, 1, 6, "int", "Long strike at most this many listed strikes below",
                                 "3 lets the ATR width be met on $5-spaced chains.",
                                 "the long strike sits at most {v} listed strikes away", 1, "strikes"),
        "credit_pct_min": Field(25, 5, 60, "int", "Minimum credit, % of what you risk",
                                "The credit against the max loss (width less credit). 25-33% is the playbook's floor.",
                                "you must be paid at least {v}% of what you risk", 1, "%"),
        "dte_lo": Field(30, 7, 180, "int", "Days to expiry, from",
                        "30-60 is the sweet spot: enough decay, still manageable.",
                        "{lo}-{hi} days to expiry", 1, "days"),
        "dte_hi": Field(60, 7, 180, "int", "... to", "The furthest expiry you will sell.",
                        "the furthest expiry you will sell", 1, "days"),
        "iv_gate_min": Field(30, 0, 100, "int", "Sell only when IV rank is at least",
                             "Below this, selling is not paid enough.",
                             "sell only when IV rank is at least {v}", 1, ""),
    },
    "debit_vertical": {    # bull_call, bear_put
        "long_delta_lo": Field(0.60, 0.3, 0.95, "num", "Long strike delta, from",
                               "How much the option moves per $1 of stock. 0.60-0.70 moves like the stock without paying for deep ITM.",
                               "moves about 60-70 cents per $1 of the stock", 0.01, ""),
        "long_delta_hi": Field(0.70, 0.3, 0.95, "num", "... to", "The deepest strike you will buy.",
                               "the deepest strike you will buy", 0.01, ""),
        "short_delta_lo": Field(0.25, 0.05, 0.6, "num", "Short strike delta, from (soft - the chart target decides)",
                                "The leg you sell to cheapen the trade; the cap sits where the setup says the move ends.",
                                "the strike you give the upside away at", 0.01, ""),
        "short_delta_hi": Field(0.35, 0.05, 0.6, "num", "... to", "The top of the soft band.",
                                "the nearest strike you will sell", 0.01, ""),
        "reward_cost_min": Field(1.0, 0.2, 5, "num", "Minimum reward / cost",
                                 "1.0 = you can make what you pay.",
                                 "the most you can make is at least {v}x what you pay", 0.1, "x"),
        "dte_lo": Field(30, 7, 180, "int", "Days to expiry, from", "Enough time for the move without a year of decay.",
                        "{lo}-{hi} days to expiry", 1, "days"),
        "dte_hi": Field(60, 7, 180, "int", "... to", "The furthest expiry you will buy.",
                        "the furthest expiry you will buy", 1, "days"),
    },
    "long": {              # buy_call, buy_put
        "delta_lo": Field(0.60, 0.3, 0.95, "num", "Delta, from",
                          "How much the option moves per $1 of stock.",
                          "stock-like, with about 35% less capital", 0.01, ""),
        "delta_hi": Field(0.70, 0.3, 0.95, "num", "... to", "The deepest strike you will buy.",
                          "the deepest strike you will buy", 0.01, ""),
        "theta_pct_max": Field(1.0, 0.1, 5, "num", "Daily decay, at most % of the premium",
                               "What waiting costs you per day. 1% = a $500 option loses about $5 a day.",
                               "a $500 option may lose up to about $5 a day", 0.1, "%/day"),
        "dte_lo": Field(45, 14, 365, "int", "Days to expiry, from",
                        "45-90 gives the move time without buying a year of decay.",
                        "{lo}-{hi} days to expiry", 1, "days"),
        "dte_hi": Field(90, 14, 365, "int", "... to", "The furthest expiry you will buy.",
                        "the furthest expiry you will buy", 1, "days"),
        "premium_stop_pct": Field(50, 10, 100, "int", "Rule stop: close at this % of the premium lost",
                                  "If the stock stop is not hit but the option bleeds. Shared with the spreads and the time strategies.",
                                  "close if the option loses {v}% of what you paid (the chart stop usually fires first)", 5, "%"),
    },
    "leaps": {
        "delta_lo": Field(0.70, 0.5, 0.95, "num", "Delta (deep in the money), from",
                          "A deep call behaves like most of a share position.",
                          "behaves like 70-80 shares per contract", 0.01, ""),
        "delta_hi": Field(0.80, 0.5, 0.95, "num", "... to", "The deepest strike you will buy.",
                          "the deepest strike you will buy", 0.01, ""),
        "extrinsic_pct_max": Field(10, 1, 40, "int", "Max time value, % of the STOCK price",
                                   "What you pay for time rather than stock, measured against the share price.",
                                   "you pay at most {v}% of the share price for time", 1, "%"),
        "months_lo": Field(9, 6, 36, "int", "Months to expiry, from", "9-18 months: stock replacement, not a swing.",
                           "{lo}-{hi} months to expiry", 1, "months"),
        "months_hi": Field(18, 6, 36, "int", "... to", "The furthest expiry you will buy.",
                           "the furthest expiry you will buy", 1, "months"),
        "roll_dte": Field(180, 60, 365, "int", "Roll out when this many days remain",
                          "The roll date: roll while time value is still cheap.",
                          "roll out with {v} days left", 30, "days"),
        "delta_floor": Field(0.55, 0.3, 0.7, "num", "Roll down-and-out if delta falls under",
                             "Delta drift: under this the call no longer behaves like stock.",
                             "roll if delta drifts under {v}", 0.01, ""),
        "premium_stop_pct": Field(40, 10, 100, "int", "Rule stop: close at this % of what you paid",
                                  "The weekly trend may hold but the position has not. The diagonal's long leg inherits it.",
                                  "down {v}% of what you paid = out", 5, "%"),
    },
    "condor": {
        "short_delta_lo": Field(0.15, 0.05, 0.35, "num", "Short strike delta each side, from",
                                "0.15-0.20 is about an 80-85% chance each side expires worthless.",
                                "about a 1-in-6 chance on each side", 0.01, ""),
        "short_delta_hi": Field(0.20, 0.05, 0.35, "num", "... to", "The nearest strikes you will sell.",
                                "the nearest strikes you will sell", 0.01, ""),
        "wing_atr_lo": Field(0.5, 0.1, 5, "num", "Wing width, in ATRs, from",
                             "The $ figure is shown beside it.",
                             "wings {lo}-{hi} ATR (about ${lo_usd}-{hi_usd})", 0.1, "x ATR"),
        "wing_atr_hi": Field(1.5, 0.1, 5, "num", "... to", "The widest wing you will sell.",
                             "the widest wing you will sell", 0.1, "x ATR"),
        "credit_pct_min": Field(30, 5, 60, "int", "Minimum credit, % of what you risk",
                                "The credit against the max loss (the wider wing less the credit) - the same base as the verticals.",
                                "you must be paid at least {v}% of what you risk", 1, "%"),
        "dte_lo": Field(30, 7, 120, "int", "Days to expiry, from", "30-45 days: decay without a long wait.",
                        "{lo}-{hi} days to expiry", 1, "days"),
        "dte_hi": Field(45, 7, 120, "int", "... to", "The furthest expiry you will sell.",
                        "the furthest expiry you will sell", 1, "days"),
        "roll_delta": Field(0.30, 0.1, 0.6, "num", "Act when either short delta reaches",
                            "The action line for either side.",
                            "act when a short strike's delta reaches {v}", 0.01, ""),
        "loss_stop_pct_credit": Field(100, 25, 300, "int", "Rule stop: loss as % of the credit",
                                      "Close when the loss equals this share of the credit you took.",
                                      "close when the loss equals {v}% of the credit you took", 5, "%"),
    },
    "time": {              # calendar, diagonal_call
        "cal_front_lo": Field(20, 7, 60, "int", "Calendar: near expiry days, from", "The option you sell.",
                              "near leg {lo}-{hi} days", 1, "days"),
        "cal_front_hi": Field(30, 7, 60, "int", "... to", "The furthest near leg you will sell.",
                              "the furthest near leg", 1, "days"),
        "cal_back_lo": Field(50, 30, 180, "int", "Calendar: far expiry days, from", "The option you own.",
                             "far leg {lo}-{hi} days", 1, "days"),
        "cal_back_hi": Field(70, 30, 180, "int", "... to", "The furthest far leg you will buy.",
                             "the furthest far leg", 1, "days"),
        "cal_delta_tol": Field(0.05, 0.01, 0.2, "num", "How far from delta 0.50 the strike may sit",
                               "At the money.", "at the money (within {v} of delta 0.50)", 0.01, ""),
        "cal_take_pct": Field(25, 5, 100, "int", "Calendar: take profit at this % of the debit",
                              "Calendars pay in small steps.", "take profit at {v}% of the debit", 5, "%"),
        "diag_long_delta_lo": Field(0.70, 0.5, 0.95, "num", "Diagonal: long call delta, from",
                                    "The long-dated call you own (LEAPS band).", "long call delta {lo}-{hi}", 0.01, ""),
        "diag_long_delta_hi": Field(0.80, 0.5, 0.95, "num", "... to", "The deepest long call you will buy.",
                                    "the deepest long call", 0.01, ""),
        "diag_long_dte_lo": Field(180, 90, 730, "int", "Diagonal: long call days, from", "6-12 months.",
                                  "long call {lo}-{hi} days", 30, "days"),
        "diag_long_dte_hi": Field(365, 90, 730, "int", "... to", "The furthest long call you will buy.",
                                  "the furthest long call", 30, "days"),
        "diag_short_delta_lo": Field(0.20, 0.05, 0.5, "num", "Diagonal: short call delta, from",
                                     "The near-term call you rent out under the resistance.",
                                     "short call delta {lo}-{hi}", 0.01, ""),
        "diag_short_delta_hi": Field(0.30, 0.05, 0.5, "num", "... to", "The nearest short call you will sell.",
                                     "the nearest short call", 0.01, ""),
        "diag_short_dte_lo": Field(30, 7, 90, "int", "Diagonal: short call days, from", "One cycle at a time.",
                                   "short call {lo}-{hi} days", 1, "days"),
        "diag_short_dte_hi": Field(45, 7, 90, "int", "... to", "The furthest short call you will sell.",
                                   "the furthest short call", 1, "days"),
    },
}
FIELDS = SCHEMA     # the one table; the drawer iterates TAB_BLOCKS[tab] over it

HOUSE: dict[str, dict] = {b: {k: f.default for k, f in fields.items()} for b, fields in SCHEMA.items()}

TELEGRAM_DEFAULT = {"enabled": False, "chat_id": None, "verified": False, "quiet": False,
                    "paused_until": None, "pending": None}

# The fields that change a pick - and ONLY those. Sizing inputs (nlv, risk_pct,
# gap_mult), every exit line (premium_stop_pct, roll_dte, delta_floor, roll_delta,
# loss_stop_pct_credit, cal_take_pct), the trade_prefs lines, `account` and
# `telegram` are never hashed: changing them must not invalidate a cached pick.
LIQUIDITY_FIELDS = ("min_oi", "oi_per_contract", "max_leg_spread", "min_leg_volume")
PICK_FIELDS: dict[str, tuple[str, ...]] = {
    "shared": LIQUIDITY_FIELDS + ("earnings_rule", "monthly_only", "chart_constraint"),
    "credit_vertical": ("short_delta_lo", "short_delta_hi", "width_atr_lo", "width_atr_hi",
                        "long_offset_max", "credit_pct_min", "dte_lo", "dte_hi", "iv_gate_min"),
    "debit_vertical": ("long_delta_lo", "long_delta_hi", "short_delta_lo", "short_delta_hi",
                       "reward_cost_min", "dte_lo", "dte_hi"),
    "long": ("delta_lo", "delta_hi", "theta_pct_max", "dte_lo", "dte_hi"),
    "leaps": ("delta_lo", "delta_hi", "extrinsic_pct_max", "months_lo", "months_hi"),
    "condor": ("short_delta_lo", "short_delta_hi", "wing_atr_lo", "wing_atr_hi", "credit_pct_min", "dte_lo", "dte_hi"),
    "time": ("cal_front_lo", "cal_front_hi", "cal_back_lo", "cal_back_hi", "cal_delta_tol",
             "diag_long_delta_lo", "diag_long_delta_hi", "diag_long_dte_lo", "diag_long_dte_hi",
             "diag_short_delta_lo", "diag_short_delta_hi", "diag_short_dte_lo", "diag_short_dte_hi"),
}

# The trade_prefs keys a rules form may carry (Shared: account; Credit: the four
# exit lines). They go to trade_prefs.write, never into prefs, never into the hash.
TRADE_PREFS_FORM_KEYS = {"nlv": "nlv", "risk_pct": "risk_pct", "roll_delta": "roll_delta",
                         "loss_stop_pct": "loss_stop_pct", "profit_target_pct": "profit_target_pct",
                         "dte_floor": "dte_floor"}

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


# ------------------------------------------------------------------ coercion
def _coerce(value, f: Field, name: str):
    """One stored / posted value -> an in-bounds value of the field's kind, else
    the house default (trade_prefs._num semantics: bad or out-of-range -> default,
    never raise). Pure."""
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
        return s if s in CHOICES.get(name, ()) else f.default
    if isinstance(value, bool):          # True is not a number here
        return f.default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f.default
    if math.isnan(v) or math.isinf(v):
        return f.default
    if (f.lo is not None and v < f.lo) or (f.hi is not None and v > f.hi):
        return f.default
    if f.kind == "int":
        return int(round(v))
    return float(v)


def _migrate(raw: dict) -> dict:
    """Renamed / retired keys from earlier drafts, folded in on read so an old row
    still merges: shared.GAP_MULT -> shared.gap_mult; shared.max_position_pct is a
    constant now and is dropped; a flat telegram block is left alone."""
    raw = dict(raw or {})
    shared = raw.get("shared")
    if isinstance(shared, dict):
        shared = dict(shared)
        if "GAP_MULT" in shared and "gap_mult" not in shared:
            shared["gap_mult"] = shared.pop("GAP_MULT")
        shared.pop("GAP_MULT", None)
        shared.pop("max_position_pct", None)
        raw["shared"] = shared
    return raw


def clean(raw: dict | None) -> dict:
    """The pure merge: house defaults with a sparse override dict on top, per field
    (missing key -> default; a stored value outside [lo, hi] or of the wrong kind
    -> default, never raise). Returns the seven blocks plus ``_overridden``, the set
    of dotted field names the member changed ("credit_vertical.short_delta_hi").
    No I/O. ``HOUSE_HASH = prefs_hash(clean({}))``."""
    raw = _migrate(raw)
    out: dict = {}
    overridden: set[str] = set()
    for block, fields in SCHEMA.items():
        src = raw.get(block)
        src = src if isinstance(src, dict) else {}
        merged = {}
        for k, f in fields.items():
            v = _coerce(src.get(k), f, k)
            merged[k] = v
            if src.get(k) is not None and v != f.default:
                overridden.add(f"{block}.{k}")
        out[block] = merged
    out["_overridden"] = overridden
    return out


def _canon(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return round(v, 4)
    return v


def prefs_hash(merged: dict) -> str:
    """First 12 hex of sha1 over canonical JSON of the PICK-RELEVANT fields only
    (sorted keys, floats to 4 dp). Takes the MERGED dict (clean() / read() output).
    Never nlv, risk_pct, gap_mult, an exit line, `account`, `_overridden` or
    `telegram`. Stored in the String(16) column user_option_prefs.prefs_hash."""
    sub = {b: {k: _canon(merged[b][k]) for k in keys} for b, keys in PICK_FIELDS.items()}
    blob = json.dumps(sub, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


HOUSE_HASH = prefs_hash(clean({}))      # members on house defaults share this signal row


# --------------------------------------------------------------------- reads
def _row_of(user):
    """The member's UserOptionPrefs row through the one-to-one User.option_prefs, or
    None (no row yet, or the model has not landed)."""
    if user is None:
        return None
    try:
        return getattr(user, "option_prefs", None)
    except Exception:  # noqa: BLE001 - a detached instance, a missing relationship
        return None


def read(db, user) -> dict:
    """clean() over the member's stored overrides - the merged seven blocks and
    ``_overridden`` - plus two top-level keys that are NOT blocks and never hashed:

    * ``telegram`` = {enabled, chat_id, verified, quiet, paused_until, pending} -
      its own key in the same JSON, written only by POST /options/telegram;
    * ``account`` = {nlv, risk_pct, nlv_source} from trade_prefs.read(user);
      ``nlv_source`` is "prefs" when an account value is stored, else None ("live"
      is set per request by the Live path, never here).

    ``db`` is accepted for the routes' convenience; the row is reached through the
    one-to-one ``User.option_prefs``.
    """
    row = _row_of(user)
    stored = (getattr(row, "prefs", None) if row is not None else None) or {}
    out = clean(stored)
    tg = stored.get("telegram") if isinstance(stored.get("telegram"), dict) else {}
    out["telegram"] = {**TELEGRAM_DEFAULT, **tg}
    tp = trade_prefs.read(user) if user is not None else {"nlv": 0.0, "risk_pct": trade_prefs.DEFAULT_RISK_PCT}
    nlv = float(tp.get("nlv") or 0.0)
    out["account"] = {"nlv": nlv if nlv > 0 else None, "risk_pct": float(tp.get("risk_pct") or 0.0),
                      "nlv_source": "prefs" if nlv > 0 else None}
    return out


def for_strategy(prefs: dict, strategy: str) -> dict:
    """The flat dict strike_picker.pick reads for one strategy: the family block's
    merged values with the shared block folded in (no key collides), plus the
    inherited ``premium_stop_pct`` for a family that has none of its own (the debit
    verticals and the calendar take the long block's, the diagonal the leaps
    block's - B5.1)."""
    fam = family_of(strategy)
    out = dict(prefs.get("shared") or HOUSE["shared"])
    out.update(prefs.get(fam) or HOUSE[fam])
    if "premium_stop_pct" not in out:
        src = _RULE_STOP_BLOCK.get(strategy) or _RULE_STOP_BLOCK.get(fam)
        if src:
            out["premium_stop_pct"] = (prefs.get(src) or HOUSE[src])["premium_stop_pct"]
    return out


def family_of(strategy: str) -> str:
    """bull_put -> "credit_vertical" ... (Part C's payoff.build derives the family
    through it). KeyError for a key outside the catalog."""
    return FAMILY_OF[strategy]


def defined_risk(strategy: str) -> bool:
    """True ONLY for bull_put, bear_call, bull_call, bear_put, iron_condor (II.2.7):
    the strategies the earnings rule `defined_risk_only` may admit through a print.
    False for buy_call / buy_put / calendar / diagonal_call / leaps_call."""
    return strategy in DEFINED_RISK


def fields_for_tab(tab: str) -> list[tuple[str, str, Field]]:
    """(block, name, Field) rows the drawer renders for one tab, in SCHEMA order."""
    return [(b, k, f) for b in TAB_BLOCKS[tab] for k, f in SCHEMA[b].items()]


# -------------------------------------------------------------------- writes
def _model():
    """The ORM class, imported lazily so the pure functions above never need it."""
    from ..models import UserOptionPrefs   # noqa: WPS433 - lands with the module's migration
    return UserOptionPrefs


def _posted(form: dict, tab: str, block: str, name: str):
    """The posted value for one field: "block.name", "block__name", or the bare
    name when no other block on the tab carries it. None when absent."""
    for key in (f"{block}.{name}", f"{block}__{name}"):
        if key in form:
            return form[key]
    if name in form and sum(1 for b in TAB_BLOCKS[tab] if name in SCHEMA[b]) == 1:
        return form[name]
    return None


def _label(block: str, name: str) -> str:
    lab = SCHEMA[block][name].label
    if lab.startswith("..."):           # the "to" half of a band: name it after its block
        lab = f"{BLOCK_LABELS.get(block, block)}: {name.replace('_', ' ')}"
    return lab


def write(db, user, tab: str, form: dict) -> tuple[dict, list[str]]:
    """Store ONLY the fields that differ from the house default for the blocks of
    one tab (a field posted equal to its default drops back to "no override");
    recompute and store ``prefs_hash`` and ``schema_version``. Returns
    ``(read(db, user), errors)``; a non-empty ``errors`` means NOTHING was stored.

    Out-of-range input is REPORTED, not clamped (trade_prefs.write). A checkbox
    absent from a posted form is False. ``nlv`` / ``risk_pct`` / the four credit
    exit lines on the form go to ``trade_prefs.write`` (their own store, never
    hashed). ``telegram`` is never touched here.
    """
    if tab not in TABS:
        return read(db, user), [f"Unknown rules tab {tab!r}."]
    form = dict(form or {})
    errors: list[str] = []
    row = _row_of(user)
    stored = dict((getattr(row, "prefs", None) if row is not None else None) or {})
    new_prefs = {k: (dict(v) if isinstance(v, dict) else v) for k, v in stored.items()}

    for block in TAB_BLOCKS[tab]:
        over = dict(new_prefs.get(block) or {})
        for name, f in SCHEMA[block].items():
            posted = _posted(form, tab, block, name)
            if f.kind == "bool":
                val = _coerce(posted, f, name) if posted is not None else False
            elif posted is None or (isinstance(posted, str) and not posted.strip()):
                continue                              # not on the form: leave the override as it is
            elif f.kind == "choice":
                s = str(posted).strip()
                if s not in CHOICES.get(name, ()):
                    errors.append(f"{_label(block, name)} must be one of: {', '.join(CHOICES.get(name, ()))}.")
                    continue
                val = s
            else:
                try:
                    v = float(posted)
                    if math.isnan(v) or math.isinf(v):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{_label(block, name)} must be a number.")
                    continue
                if (f.lo is not None and v < f.lo) or (f.hi is not None and v > f.hi):
                    errors.append(f"{_label(block, name)} must be between {f.lo:g} and {f.hi:g}.")
                    continue
                val = int(round(v)) if f.kind == "int" else float(v)
            if val == f.default:
                over.pop(name, None)
            else:
                over[name] = val
        if over:
            new_prefs[block] = over
        else:
            new_prefs.pop(block, None)

    # band sanity: a "from" above its "to" is a typo, not a preference
    merged_preview = clean(new_prefs)
    for block in TAB_BLOCKS[tab]:
        vals = merged_preview[block]
        for lo_k in [k for k in vals if k.endswith("_lo")]:
            hi_k = lo_k[:-3] + "_hi"
            if hi_k in vals and vals[lo_k] > vals[hi_k]:
                errors.append(f"{BLOCK_LABELS.get(block, block)}: {lo_k[:-3].replace('_', ' ')} 'from' must not exceed 'to'.")

    # the trade_prefs keys that ride along on the Shared / Credit forms
    tp_kwargs = {dst: form[src] for src, dst in TRADE_PREFS_FORM_KEYS.items()
                 if src in form and str(form[src]).strip() != ""}
    if errors:
        return read(db, user), errors
    if tp_kwargs and user is not None:
        _, err = trade_prefs.write(db, user, **tp_kwargs)
        if err:
            return read(db, user), [err]

    _store(db, user, row, new_prefs)
    return read(db, user), []


def reset(db, user, tab: str | None) -> dict:
    """Drop the tab's overrides (``tab`` None or "all" = every block); telegram is
    untouched; the hash is recomputed. Returns read(db, user)."""
    row = _row_of(user)
    stored = dict((getattr(row, "prefs", None) if row is not None else None) or {})
    blocks = BLOCKS if tab in (None, "all") else TAB_BLOCKS.get(tab, ())
    for b in blocks:
        stored.pop(b, None)
    _store(db, user, row, stored)
    return read(db, user)


def _store(db, user, row, prefs: dict) -> None:
    """Write the sparse dict + its hash to the member's row (create it on first
    write). Reassigns the JSON so SQLAlchemy sees the change (trade_prefs.write)."""
    merged = clean(prefs)
    if row is None:
        model = _model()
        row = model(user_id=user.id, prefs={}, prefs_hash=HOUSE_HASH, schema_version=SCHEMA_VERSION)
        db.add(row)
        try:
            user.option_prefs = row
        except Exception:  # noqa: BLE001 - no relationship on a stub user
            pass
    row.prefs = {k: v for k, v in prefs.items()}
    row.prefs_hash = prefs_hash(merged)
    row.schema_version = SCHEMA_VERSION
    db.commit()


def distinct_hashes(db) -> list[str]:
    """``[HOUSE_HASH] + every DISTINCT user_option_prefs.prefs_hash`` (house first,
    the rest sorted, no duplicates) - the nightly job's per-symbol row list. Falls
    back to ``[HOUSE_HASH]`` before the model exists."""
    out = [HOUSE_HASH]
    try:
        model = _model()
    except ImportError:
        return out
    rows = db.query(model.prefs_hash).distinct().all()
    for (h,) in rows:
        if h and h != HOUSE_HASH and h not in out:
            out.append(h)
    return [out[0]] + sorted(out[1:])
