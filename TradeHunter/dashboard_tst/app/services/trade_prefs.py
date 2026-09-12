"""Per-member trade preferences: sizing (entry offset, account size, risk budget)
and the option-spread exit lines the Portfolio monitor grades against.

These three numbers answer "how big is this trade?", and BOTH surfaces that ask
that question read them from here — the chart's trade-setup editor and the Curated
table. Keeping one reader means the quantity shown next to a drawing and the
quantity shown next to the curated call it became are the same number by
construction, not by two implementations agreeing.

Stored in ``User.prefs`` (a JSON column) rather than new columns: they are UI
preferences, one row per user already exists, and ``prefs`` is where the sector
filter and the entry offset already live. Portable JSON, so the Postgres swap
stays a config change (platform data-handling rule).

Position size is DERIVED, never stored:

    risk per unit = |entry - stop|          what one share can lose
    risk budget   = NLV x risk %            what the account is willing to lose
    quantity      = risk budget / risk per unit

so changing the account size re-sizes every open idea at once, which is the whole
point of keeping it as a preference. A curated call therefore records the LEVELS
(the judgement) and nothing about size (an account fact that moves on its own).
"""
from __future__ import annotations

# Defaults for a member who has never set them. The offset is a style; the NLV is
# deliberately 0 = "not told yet", which renders as a blank quantity rather than a
# confident but invented one.
DEFAULT_ENTRY_OFFSET_PCT = 0.3
DEFAULT_NLV = 0.0
DEFAULT_RISK_PCT = 1.0

# Bounds. Beyond these the input is a fat-finger, not a preference: an "entry near
# the level" 20% away is not near it, and risking more than 100% of the account is
# not a risk budget.
MAX_OFFSET_PCT = 20.0
MAX_NLV = 1e12
MAX_RISK_PCT = 100.0

# Option-spread exit lines (Portfolio page). These are MANAGEMENT settings, not
# sizing ones, but they live here for the same reason the others do: one row per
# user already exists, ``prefs`` is portable JSON, and both the Portfolio monitor
# and the per-trade override form must read the same defaults or a member's line
# would mean one thing on the board and another on the row.
#
# The defaults are the member's own (delta 0.30 / 20% of max loss), which are
# TIGHTER than the Adam Khoo playbook's 0.35-0.40 that ``bull_put.review`` still
# implements for the Options tab. Both are deliberate; see bull_put.ROLL_DELTA.
DEFAULT_ROLL_DELTA = 0.30
DEFAULT_LOSS_STOP_PCT = 20.0
MAX_ROLL_DELTA = 1.0
MAX_LOSS_STOP_PCT = 100.0
# Winning-side lines (v4.62). 0 = that line switched off for this member.
DEFAULT_PROFIT_TARGET_PCT = 50.0    # close once this % of the credit is captured
DEFAULT_DTE_FLOOR = 21              # close / roll at this many days left
MAX_PROFIT_TARGET_PCT = 100.0
MAX_DTE_FLOOR = 365


def _num(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return v if lo <= v <= hi else default


def read(user) -> dict:
    """This member's sizing preferences, always complete and always in bounds."""
    prefs = getattr(user, "prefs", None) or {}
    return {
        "offset_pct": _num(prefs.get("trade_entry_offset_pct"),
                           DEFAULT_ENTRY_OFFSET_PCT, 0.0, MAX_OFFSET_PCT),
        "nlv": _num(prefs.get("trade_nlv"), DEFAULT_NLV, 0.0, MAX_NLV),
        "risk_pct": _num(prefs.get("trade_risk_pct"),
                         DEFAULT_RISK_PCT, 0.0, MAX_RISK_PCT),
        "roll_delta": _num(prefs.get("spread_roll_delta"),
                           DEFAULT_ROLL_DELTA, 0.0, MAX_ROLL_DELTA),
        "loss_stop_pct": _num(prefs.get("spread_loss_stop_pct"),
                              DEFAULT_LOSS_STOP_PCT, 0.0, MAX_LOSS_STOP_PCT),
        # what spread_monitor.snapshot_rows expects: a fraction, not a percent
        "loss_fraction": _num(prefs.get("spread_loss_stop_pct"),
                              DEFAULT_LOSS_STOP_PCT, 0.0, MAX_LOSS_STOP_PCT) / 100.0,
        "profit_target_pct": _num(prefs.get("spread_profit_target_pct"),
                                  DEFAULT_PROFIT_TARGET_PCT, 0.0, MAX_PROFIT_TARGET_PCT),
        "dte_floor": int(_num(prefs.get("spread_dte_floor"),
                              DEFAULT_DTE_FLOOR, 0.0, MAX_DTE_FLOOR)),
    }


def write(db, user, *, offset_pct=None, nlv=None, risk_pct=None,
          roll_delta=None, loss_stop_pct=None, profit_target_pct=None,
          dte_floor=None) -> tuple[dict, str]:
    """Update whichever of the three were supplied. Returns (prefs, error).

    Out-of-range input is REPORTED, not silently clamped: a member who typed
    1,000,000% risk meant something, and quietly storing 100 would size every
    future trade off a number they never chose.
    """
    prefs = dict(getattr(user, "prefs", None) or {})
    err = ""

    def take(value, key, lo, hi, what):
        nonlocal err
        if value is None or value == "":
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            err = err or "%s must be a number." % what
            return
        if not lo <= v <= hi:
            err = err or "%s must be between %g and %g." % (what, lo, hi)
            return
        prefs[key] = v

    take(offset_pct, "trade_entry_offset_pct", 0.0, MAX_OFFSET_PCT, "Entry offset")
    take(nlv, "trade_nlv", 0.0, MAX_NLV, "Account value")
    take(risk_pct, "trade_risk_pct", 0.0, MAX_RISK_PCT, "Risk per trade")
    take(roll_delta, "spread_roll_delta", 0.0, MAX_ROLL_DELTA, "Roll delta")
    take(loss_stop_pct, "spread_loss_stop_pct", 0.0, MAX_LOSS_STOP_PCT,
         "Loss stop (% of max loss)")
    take(profit_target_pct, "spread_profit_target_pct", 0.0, MAX_PROFIT_TARGET_PCT,
         "Profit target (% of credit)")
    take(dte_floor, "spread_dte_floor", 0.0, MAX_DTE_FLOOR, "DTE floor")
    if err:
        return read(user), err

    user.prefs = prefs      # reassign: SQLAlchemy won't see an in-place mutation
    db.commit()
    return read(user), ""


def size(entry, stop, prefs: dict) -> dict:
    """What this plan costs and how much of it to buy.

    ``{risk_per_unit, risk_budget, qty, cost, exposure_pct}``, with None wherever
    the answer is not knowable — no NLV on file, or a stop sitting on the entry
    (zero risk per unit, which would divide by zero and imply infinite size).

    Quantity is floored to whole shares: rounding UP would spend more of the risk
    budget than the member allowed, which is the one direction that must never
    happen silently.
    """
    out = {"risk_per_unit": None, "risk_budget": None, "qty": None,
           "cost": None, "exposure_pct": None}
    try:
        entry = float(entry)
        stop = float(stop)
    except (TypeError, ValueError):
        return out
    rpu = abs(entry - stop)
    if rpu <= 0 or entry <= 0:
        return out
    out["risk_per_unit"] = rpu

    nlv = prefs.get("nlv") or 0.0
    risk_pct = prefs.get("risk_pct") or 0.0
    if nlv <= 0 or risk_pct <= 0:
        return out                      # sized once the member says what the account is

    budget = nlv * risk_pct / 100.0
    out["risk_budget"] = budget
    qty = int(budget // rpu)
    out["qty"] = qty
    out["cost"] = qty * entry
    out["exposure_pct"] = (qty * entry) / nlv * 100.0 if nlv else None
    return out
