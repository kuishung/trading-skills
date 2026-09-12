"""Bull put spread selection + monitoring rules.

Mechanises the credit-spread playbook in
``options-strategies-reference/07-credit-spreads.md`` (Adam Khoo, Piranha Profits
Level 2) for one ticker at a time.

**This module is deliberately PURE.** It takes a chain snapshot, IV stats, the
account's net liquidation and an earnings date, and returns a verdict. It does no
I/O, knows nothing about TWS, and never places an order — so the rules can be
unit-tested against synthetic chains without a broker connection, which is the
only way any of this is verifiable while TWS is off.

It SUGGESTS and MONITORS. Placing the order is the user's action in TWS.

The rules, verbatim from the playbook
-------------------------------------
Entry
  * IV percentile >= 40-50 (sell premium only when it is rich)
  * no earnings on or before expiry
  * 45-60 DTE
  * short put delta 0.20-0.25  (~75-80% win probability)
  * long put 1-2 strikes below the short
  * bid/ask on each leg <= $0.40-0.50
  * 20% of max loss < 2% of net liquidation  -> position size
Management
  * short-put delta above 0.35-0.40 is the action line
  * > 30 DTE  -> adjust (roll the spread down)
  * <= 30 DTE -> close and cut the loss
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

# ---- rule constants (playbook defaults; overridable per call) ---------------
DTE_MIN, DTE_MAX = 45, 60
SHORT_DELTA_LO, SHORT_DELTA_HI = 0.20, 0.25
LONG_OFFSET_MIN, LONG_OFFSET_MAX = 1, 2        # strikes below the short
IV_PCT_MIN = 40.0
MAX_LEG_SPREAD = 0.50                          # $ bid/ask width per leg
RISK_FRACTION = 0.20                           # "20% of max loss"
NLV_RISK_PCT = 2.0                             # "< 2% of net liquidation"
DELTA_ADJUST = 0.35                            # action line
DELTA_CLOSE = 0.40
ADJUST_DTE = 30                                # >30d adjust, else close


# ---- Black-Scholes (only for the mid-life P/L curve) -----------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S: float, K: float, T: float, sigma: float, r: float = 0.04) -> float:
    """European put value. Used ONLY to draw the 'at 15 DTE' curve — the entry
    numbers all come from real quotes, never from a model."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass
class Candidate:
    expiry: str
    dte: int
    short_strike: float
    long_strike: float
    short_delta: float
    long_delta: float | None
    short_iv: float | None
    credit: float                 # per share
    width: float
    max_profit: float             # per contract ($)
    max_loss: float               # per contract ($)
    risk_20pct: float             # 20% of max loss, per contract ($)
    breakeven: float
    pop_est: float                # ~1 - |short delta|
    contracts: int
    capital_at_risk: float        # risk_20pct * contracts
    short_leg_spread: float
    long_leg_spread: float
    long_offset: int
    profile: dict = field(default_factory=dict)


def _mid(row: dict) -> float | None:
    b, a = row.get("bid"), row.get("ask")
    if b is None or a is None:
        return row.get("last")
    return (b + a) / 2.0


def _leg_spread(row: dict) -> float | None:
    b, a = row.get("bid"), row.get("ask")
    if b is None or a is None:
        return None
    return round(a - b, 4)


def _abs_delta(row: dict) -> float | None:
    d = row.get("delta")
    return None if d is None else abs(d)


def pl_profile(short_k: float, long_k: float, credit: float, contracts: int,
               *, spot: float, dte: int, short_iv: float | None,
               long_iv: float | None, points: int = 41) -> dict:
    """P/L across a price range, at expiry AND at 15 DTE.

    The playbook asks to 'check your potential loss or profit at different prices,
    15 days to expiration and at expiration' — so both curves are returned.
    """
    lo = min(long_k * 0.90, spot * 0.80)
    hi = max(spot * 1.12, short_k * 1.08)
    step = (hi - lo) / (points - 1)
    prices = [round(lo + i * step, 2) for i in range(points)]

    at_exp, at_15 = [], []
    t15 = 15.0 / 365.0
    sig_s = (short_iv / 100.0) if short_iv else None
    sig_l = (long_iv / 100.0) if long_iv else sig_s

    for S in prices:
        # expiry: credit minus the spread's intrinsic cost
        intrinsic = max(0.0, short_k - S) - max(0.0, long_k - S)
        at_exp.append(round((credit - intrinsic) * 100 * contracts, 2))

        if sig_s and dte > 15:
            val = bs_put(S, short_k, t15, sig_s) - bs_put(S, long_k, t15, sig_l or sig_s)
            at_15.append(round((credit - val) * 100 * contracts, 2))

    return {"prices": prices, "at_expiry": at_exp,
            "at_15dte": at_15 if len(at_15) == len(prices) else []}


def select(*, symbol: str, spot: float, expiry: str, dte: int,
           puts: list[dict], iv_percentile: float | None,
           earnings_date: str | None, net_liquidation: float | None,
           dte_min: int = DTE_MIN, dte_max: int = DTE_MAX,
           iv_pct_min: float = IV_PCT_MIN,
           max_leg_spread: float = MAX_LEG_SPREAD,
           nlv_risk_pct: float = NLV_RISK_PCT) -> dict:
    """Pick the best bull put spread in ONE expiry and grade it against the rules.

    Returns ``{"checks": [...], "candidate": {...}|None, "ok": bool}``. Checks are
    reported even when they fail, so the UI can show WHY a ticker is not a
    candidate today rather than just going blank.
    """
    checks: list[Check] = []

    # ---- gate 1: IV regime -------------------------------------------------
    if iv_percentile is None:
        checks.append(Check("IV percentile", False,
                            "No IV history available — cannot confirm premium is rich.",
                            blocking=True))
    else:
        ok = iv_percentile >= iv_pct_min
        checks.append(Check("IV percentile", ok,
                            f"{iv_percentile:.0f}% (need >= {iv_pct_min:.0f}%)"
                            + ("" if ok else " — premium is not rich enough to sell."),
                            blocking=True))

    # ---- gate 2: earnings must fall AFTER expiry ---------------------------
    if earnings_date:
        try:
            e = _dt.date.fromisoformat(earnings_date)
            x = _dt.date.fromisoformat(expiry)
            ok = e > x
            checks.append(Check("Earnings clear of expiry", ok,
                                f"next earnings {earnings_date}, expiry {expiry}"
                                + ("" if ok else " — earnings land inside the trade."),
                                blocking=True))
        except ValueError:
            checks.append(Check("Earnings clear of expiry", True,
                                f"unparsable earnings date {earnings_date!r} — not blocking",
                                blocking=False))
    else:
        checks.append(Check("Earnings clear of expiry", True,
                            "no earnings date known — verify manually", blocking=False))

    # ---- gate 3: DTE window ------------------------------------------------
    ok = dte_min <= dte <= dte_max
    checks.append(Check("Days to expiry", ok, f"{dte}d (ideal {dte_min}-{dte_max})",
                        blocking=False))

    # ---- pick the short leg on delta --------------------------------------
    usable = [p for p in puts if _abs_delta(p) is not None and _mid(p) is not None]
    if not usable:
        checks.append(Check("Short put by delta", False,
                            "No puts with deltas — TWS returned no option model.",
                            blocking=True))
        return {"ok": False, "checks": [c.__dict__ for c in checks], "candidate": None}

    target = (SHORT_DELTA_LO + SHORT_DELTA_HI) / 2.0
    in_band = [p for p in usable if SHORT_DELTA_LO <= _abs_delta(p) <= SHORT_DELTA_HI]
    pool = in_band or usable
    short = min(pool, key=lambda p: abs(_abs_delta(p) - target))
    sdelta = _abs_delta(short)
    checks.append(Check("Short put delta", bool(in_band),
                        f"{short['strike']:g}P delta {sdelta:.2f} "
                        f"(want {SHORT_DELTA_LO:.2f}-{SHORT_DELTA_HI:.2f})"
                        + ("" if in_band else " — nothing in band, closest shown."),
                        blocking=True))

    # ---- long leg: 1-2 strikes below, pick the better of the two ----------
    strikes = sorted({p["strike"] for p in usable})
    si = strikes.index(short["strike"])
    by_strike = {p["strike"]: p for p in usable}

    best = None
    for off in range(LONG_OFFSET_MIN, LONG_OFFSET_MAX + 1):
        li = si - off
        if li < 0:
            continue
        long_row = by_strike.get(strikes[li])
        if long_row is None:
            continue
        credit = (_mid(short) or 0) - (_mid(long_row) or 0)
        width = short["strike"] - long_row["strike"]
        if credit <= 0 or width <= 0:
            continue
        max_loss = (width - credit) * 100
        if max_loss <= 0:
            continue
        # prefer the higher credit-to-width ratio: more premium per dollar risked
        score = credit / width
        ss, ls = _leg_spread(short), _leg_spread(long_row)
        liquid = ((ss is None or ss <= max_leg_spread) and
                  (ls is None or ls <= max_leg_spread))
        # a liquid pair always beats an illiquid one, ratio only breaks ties
        rank = (1 if liquid else 0, score)
        if best is None or rank > best[0]:
            best = (rank, off, long_row, credit, width, max_loss, ss, ls, liquid)

    if best is None:
        checks.append(Check("Long put 1-2 strikes below", False,
                            "No usable long strike below the short (no credit or no quote).",
                            blocking=True))
        return {"ok": False, "checks": [c.__dict__ for c in checks], "candidate": None}

    _, off, long_row, credit, width, max_loss, ss, ls, liquid = best
    checks.append(Check("Long put 1-2 strikes below", True,
                        f"{long_row['strike']:g}P ({off} strike{'s' if off > 1 else ''} below, "
                        f"${width:g} wide)"))

    # ---- liquidity ---------------------------------------------------------
    checks.append(Check("Bid/ask <= $%.2f per leg" % max_leg_spread, liquid,
                        f"short {('%.2f' % ss) if ss is not None else 'n/a'}, "
                        f"long {('%.2f' % ls) if ls is not None else 'n/a'}"
                        + ("" if liquid else " — too wide, you'll bleed on the fill."),
                        blocking=True))

    # ---- sizing: 20% of max loss < 2% of NLV -------------------------------
    risk_20 = max_loss * RISK_FRACTION
    contracts = 0
    if net_liquidation and risk_20 > 0:
        budget = net_liquidation * (nlv_risk_pct / 100.0)
        contracts = int(budget // risk_20)
    if net_liquidation is None:
        checks.append(Check("Position size", False,
                            "Net liquidation unknown — connect TWS to size the trade.",
                            blocking=False))
    elif contracts < 1:
        checks.append(Check("Position size", False,
                            f"20% of max loss is ${risk_20:.0f}; {nlv_risk_pct:g}% of "
                            f"${net_liquidation:,.0f} NLV is ${net_liquidation * nlv_risk_pct / 100:,.0f}"
                            " — not even 1 contract fits.", blocking=True))
    else:
        checks.append(Check("Position size", True,
                            f"{contracts} contract{'s' if contracts > 1 else ''} "
                            f"— 20% of max loss = ${risk_20 * contracts:,.0f} "
                            f"(<= {nlv_risk_pct:g}% of ${net_liquidation:,.0f})"))

    qty = max(contracts, 1)
    cand = Candidate(
        expiry=expiry, dte=dte,
        short_strike=short["strike"], long_strike=long_row["strike"],
        short_delta=round(sdelta, 3),
        long_delta=round(_abs_delta(long_row), 3) if _abs_delta(long_row) else None,
        short_iv=short.get("iv"),
        credit=round(credit, 4), width=width,
        max_profit=round(credit * 100, 2), max_loss=round(max_loss, 2),
        risk_20pct=round(risk_20, 2),
        breakeven=round(short["strike"] - credit, 2),
        pop_est=round((1 - sdelta) * 100, 1),
        contracts=contracts,
        capital_at_risk=round(risk_20 * contracts, 2),
        short_leg_spread=ss if ss is not None else -1,
        long_leg_spread=ls if ls is not None else -1,
        long_offset=off,
        profile=pl_profile(short["strike"], long_row["strike"], credit, qty,
                           spot=spot, dte=dte, short_iv=short.get("iv"),
                           long_iv=long_row.get("iv")),
    )

    blocking_fail = any((not c.ok) and c.blocking for c in checks)
    return {"ok": not blocking_fail,
            "checks": [c.__dict__ for c in checks],
            "candidate": cand.__dict__}


# ---- monitoring -------------------------------------------------------------
def review(*, short_delta: float | None, dte: int,
           delta_adjust: float = DELTA_ADJUST, delta_close: float = DELTA_CLOSE,
           adjust_dte: int = ADJUST_DTE) -> dict:
    """Grade an OPEN spread against the management rule.

    'Don't let the delta go beyond 0.35-0.40.' Past the line the action depends
    only on time left: still room to roll (>30 DTE) -> adjust; otherwise close.
    """
    if short_delta is None:
        return {"state": "UNKNOWN", "action": "No delta from TWS — cannot grade.",
                "urgent": False}

    d = abs(short_delta)
    if d < delta_adjust:
        return {"state": "OK",
                "action": f"Short delta {d:.2f} is inside the line ({delta_adjust:.2f}). Hold.",
                "urgent": False}

    if dte > adjust_dte:
        return {"state": "ADJUST",
                "action": (f"Short delta {d:.2f} is past {delta_adjust:.2f} with {dte}d left — "
                           "roll down: buy to close the short put, sell to close the long, "
                           "then reopen the spread at lower strikes."),
                "urgent": True}

    return {"state": "CLOSE",
            "action": (f"Short delta {d:.2f} is past {delta_adjust:.2f} with only {dte}d left — "
                       "too late to roll; close the spread and cut the loss."),
            "urgent": True}


# ---------------------------------------------------------------- monitoring
# Two independent exit lines for an OPEN spread, both pure. `review` above is the
# playbook's delta-only rule and is left exactly as it was, because the Options
# tab documents those numbers. What the Portfolio page needs is stricter and has
# a second trigger, so it lives here rather than being smuggled into `review`:
#
#   1. DELTA   — short-put delta reaches the roll line (default 0.30).
#   2. LOSS    — unrealised loss reaches a fraction of MAX loss (default 20%).
#
# They are not redundant. Delta answers "is the market coming for my strike?" and
# fires early, on a slow drift. The loss line answers "how much have I already
# paid?" and fires on a gap that blows through the strike before delta has had a
# day to register it. Either one alone leaves a hole.
#
# The 20% figure interlocks with the sizing rule at the top of this module: size
# so that 20% of max loss is under 2% of net liquidation, and stopping out at 20%
# of max loss caps the trade's damage at that same 2%. The number is one decision
# expressed twice — at entry as size, in flight as a stop.

ROLL_DELTA = 0.30           # user's own line; the playbook's is 0.35-0.40
LOSS_STOP_FRACTION = 0.20   # "exit or roll at 20% of max loss"

# Two more lines (v4.62), on the winning side of the trade. A credit spread that
# has captured most of its credit is holding the same max loss for the last few
# cents, and the cents come slowest precisely when gamma is largest — so the
# playbook's "take 50%" and "don't sit inside three weeks" are both stated as
# lines the member can move rather than folklore they have to remember.
PROFIT_TARGET_FRACTION = 0.50   # close once 50% of the credit is captured
DTE_FLOOR = 21                  # close / roll at 21 days left, whatever the P/L


def spread_math(*, short_strike: float, long_strike: float, credit: float,
                contracts: int = 1) -> dict:
    """The fixed geometry of a bull put spread — everything that is knowable at
    entry and never changes afterwards. Separated from the live grading so the
    numbers on a row are identical whether or not a quote could be fetched."""
    width = float(short_strike) - float(long_strike)
    c = float(credit or 0.0)
    n = max(1, int(contracts or 1))
    max_profit = c * 100.0 * n
    max_loss = max(0.0, (width - c)) * 100.0 * n
    return {"width": width, "credit": c, "contracts": n,
            "max_profit": max_profit, "max_loss": max_loss,
            "breakeven": float(short_strike) - c,
            # what one contract loses at the 20% line — the figure the sizing
            # rule was built around, restated per contract
            "risk_20pct": max_loss * LOSS_STOP_FRACTION}


def monitor(*, short_delta: float | None, dte: int, pl: float | None,
            max_loss: float, roll_delta: float = ROLL_DELTA,
            loss_fraction: float = LOSS_STOP_FRACTION,
            adjust_dte: int = ADJUST_DTE,
            max_profit: float | None = None,
            profit_target: float | None = PROFIT_TARGET_FRACTION,
            dte_floor: int | None = DTE_FLOOR) -> dict:
    """Grade one open spread against every exit line.

    ``pl`` is unrealised profit/loss in dollars (negative = losing), ``max_loss``
    the dollar max loss from ``spread_math``. Either may be None/0 when no quote
    was available; the delta line is still graded, and vice versa. Returning
    ``UNKNOWN`` only when BOTH are missing is deliberate — a monitor that goes
    dark because one number is late is a monitor you stop trusting.

    Four lines, two on each side of the trade:

    * losing side — short delta at ``roll_delta``; loss at ``loss_fraction`` of
      max loss. Past either: ``ROLL`` with more than ``adjust_dte`` left, else
      ``CLOSE``.
    * winning side — ``profit_target`` (fraction of the credit captured, needs
      ``max_profit``) -> ``TAKE``; ``dte_floor`` (days left) -> ``CLOSE`` on
      time alone. ``None`` switches either off.

    The losing side wins ties: a trade that is both past its delta line and past
    its profit target is a contradiction the quotes will resolve within the day,
    and the safer word for that day is the defensive one.

    Returns ``{state, action, reasons, urgent, delta_breach, loss_breach,
    profit_breach, dte_breach, loss_pct, profit_pct}`` where state is
    OK | WATCH | ROLL | CLOSE | TAKE | UNKNOWN.
    """
    d = None if short_delta is None else abs(float(short_delta))

    loss_pct = None
    profit_pct = None
    if pl is not None and max_loss and max_loss > 0:
        # only a LOSS consumes the budget; profit is not negative loss here
        loss_pct = max(0.0, -float(pl)) / float(max_loss)
    if pl is not None and max_profit and max_profit > 0:
        profit_pct = float(pl) / float(max_profit)     # can be negative

    base = {"reasons": [], "urgent": False, "delta_breach": False,
            "loss_breach": False, "profit_breach": False, "dte_breach": False,
            "loss_pct": loss_pct, "profit_pct": profit_pct}

    dte_breach = dte_floor is not None and 0 <= dte <= int(dte_floor)

    if d is None and loss_pct is None:
        if dte_breach:
            # No quote, but the calendar needs none: time alone says close.
            return {**base, "state": "CLOSE", "urgent": True, "dte_breach": True,
                    "reasons": [f"{dte}d left is at your {dte_floor}d floor"],
                    "action": (f"No quote today, but only {dte}d left — at your "
                               f"{dte_floor}d floor. Close it, or roll to the next cycle.")}
        return {**base, "state": "UNKNOWN",
                "action": "No quote today — nothing to grade."}

    delta_breach = d is not None and d >= roll_delta
    loss_breach = loss_pct is not None and loss_pct >= loss_fraction
    profit_breach = (profit_target is not None and profit_pct is not None
                     and profit_pct >= float(profit_target))
    base.update({"delta_breach": delta_breach, "loss_breach": loss_breach,
                 "profit_breach": profit_breach, "dte_breach": dte_breach})

    reasons: list[str] = []
    if delta_breach:
        reasons.append(f"short delta {d:.2f} reached your {roll_delta:.2f} line")
    if loss_breach:
        reasons.append(f"down {loss_pct * 100:.0f}% of max loss "
                       f"(your line is {loss_fraction * 100:.0f}%)")

    if not reasons:
        # Winning side. Profit target first: it is the better news and the
        # cleaner instruction. Then the DTE floor, which says close even when
        # nothing else does.
        if profit_breach:
            return {**base, "state": "TAKE", "urgent": True,
                    "reasons": [f"{profit_pct * 100:.0f}% of the credit captured"],
                    "action": (f"{profit_pct * 100:.0f}% of the credit is captured (your "
                               f"target is {profit_target * 100:.0f}%) with {dte}d left. "
                               "Buy the spread back and take it — the rest comes slowest "
                               "and holds the whole max loss to earn it.")}
        if dte_breach:
            bits = [f"delta {d:.2f}"] if d is not None else []
            if profit_pct is not None:
                bits.append(f"{profit_pct * 100:.0f}% of credit captured")
            return {**base, "state": "CLOSE", "urgent": True,
                    "reasons": [f"{dte}d left is at your {dte_floor}d floor"],
                    "action": (f"{dte}d left, at your {dte_floor}d floor"
                               + (" (" + ", ".join(bits) + ")" if bits else "")
                               + ". Close it, or roll to the next cycle — gamma "
                                 "grows from here and a quiet trade can turn in a day.")}

        # A near-miss is worth saying out loud: the point of a daily check is to
        # see it coming, not to be told on the morning it is already too late.
        near = []
        if d is not None and d >= roll_delta * 0.8:
            near.append(f"delta {d:.2f} is closing on {roll_delta:.2f}")
        if loss_pct is not None and loss_pct >= loss_fraction * 0.5:
            near.append(f"down {loss_pct * 100:.0f}% of max loss")
        if (profit_target is not None and profit_pct is not None
                and profit_pct >= float(profit_target) * 0.8):
            near.append(f"{profit_pct * 100:.0f}% of credit captured, "
                        f"target {profit_target * 100:.0f}%")
        if dte_floor is not None and 0 <= dte <= int(dte_floor) + 3:
            near.append(f"{dte}d left, floor is {dte_floor}d")
        if near:
            return {**base, "state": "WATCH", "reasons": near,
                    "action": "Approaching a line: " + "; ".join(near) + "."}
        bits = []
        if d is not None:
            bits.append(f"delta {d:.2f}")
        if loss_pct is not None:
            bits.append(f"{loss_pct * 100:.0f}% of max loss used")
        if profit_pct is not None:
            bits.append(f"{profit_pct * 100:.0f}% of credit captured")
        return {**base, "state": "OK",
                "action": "Inside every line (" + ", ".join(bits) + "). Hold."}

    why = " and ".join(reasons)
    if dte > adjust_dte:
        return {**base, "state": "ROLL", "reasons": reasons, "urgent": True,
                "action": (f"{why.capitalize()} with {dte}d left — enough time to roll: "
                           "buy to close the short put, sell to close the long, then "
                           "reopen lower and/or further out.")}

    return {**base, "state": "CLOSE", "reasons": reasons, "urgent": True,
            "action": (f"{why.capitalize()} with only {dte}d left — a roll this close to "
                       "expiry buys little time for the credit it costs; close it.")}
