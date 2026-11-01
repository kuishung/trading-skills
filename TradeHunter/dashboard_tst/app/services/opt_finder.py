"""Option Finder (v4.219) - the user's Excel calculator run over every contract of the surface, ranked.

The user (2026-10-25), after the calculator page was removed: *"the inputs are Current Stock Price (fed from
TradeHunter), Target Stock Price (user input), Worst case Stock Price (the stop loss price that user input),
Time Decay (days) ... The legs data can be fed by TradeHunter ... the output is Breakeven Stock Price, Reward over
Risk, Return on Investment ... we make it easy first by working on the single leg naked call ... based on the vol
surface ... auto calculate the strike and each expiry and generate a report on screen ... rank it"*; then *"we use
the word option find and user can select based on what strategy they are looking at"*.

For each listed contract of the chosen strategy (v1: **buy one call**) within ``BAND`` of the price and with at
least ``MIN_OI`` open interest, the workbook's figures through ``option_calc.evaluate`` (the engine that IS the
sheet, tested against it): the leg's own IV from the last market pass, the platform's one rate, no dividend.
**Time decay** - the user confirmed both:

- ``mode="expiry"``: each contract valued at its OWN expiry (decay = its days to expiry; the value at the target /
  worst price is then the intrinsic value);
- ``mode="date"``: every contract valued on ONE date the member picks (decay = days from today to that date);
  contracts expiring before it are left out.

Every row carries the sheet's three answers - **breakeven stock price**, **reward over risk** (gain at the target
/ loss at the worst price), **return on investment** (gain at the target / cost) - and the inputs behind them; the
page sorts by the member's choice (reward over risk, ROI, or the breakeven closest to today's price).

``STRATEGIES`` lists what the dropdown will hold; only ``long_call`` is built (the rest show "coming soon").

**Entry price** (v4.234; the user: *"We need an entry price column ... when the current price radio button is selected
it is based on the last price (Massive has 15 delay); another radio button will be user defined entry price, the table
will be recalculated based on user price"*): ``entry`` = the STOCK price the member plans to buy at. None = the last
market pass's price (15 minutes delayed); a number = the member's own. Every figure is then worked from that price:
each call's entry price (what it costs with the stock there, at the contract's own IV - held where it is), the strike
band, the breakeven's distance and the chance above it. ``ENTRY_MAX_MOVE`` keeps the entry within 50 % of the price.
"""
from __future__ import annotations

import datetime as _dt
import math

from . import vol_surface
from .calc_chain import ChainError, load_chain
from .opt_constants import RISK_FREE
from .option_calc import Leg, Scenario, evaluate

BAND = 0.15                # strikes within 15 % of the price
MIN_OI = 100               # open interest of at least this many contracts (yesterday's)
MIN_DTE = 7                # contracts expiring within a week are left out (a target "by expiry" in days is a lottery ticket)
MIN_COST = 0.05            # nor a contract worth under 5 cents a share (no fill, and a near-zero cost makes every ratio explode)
DATE_MAX_DAYS = 800        # the "by a date" picker's far end
ENTRY_MAX_MOVE = 0.5       # v4.234: a member's entry price within 50 % of the last price

# key, label, view (bull / bear / range), legs, built
STRATEGIES = (
    ("long_call", "Buy a call", "bull", 1, True),
    ("long_put", "Buy a put", "bear", 1, False),
    ("bull_call_spread", "Bull call spread", "bull", 2, False),
    ("bear_put_spread", "Bear put spread", "bear", 2, False),
    ("range", "Iron condor / butterfly", "range", 4, False),
)
BUILT = {s[0] for s in STRATEGIES if s[4]}
MODES = ("expiry", "date")
SORTS = ("rr", "roi", "be", "prob")


class FinderError(ValueError):
    """Input the finder cannot use; the message is for the member."""


def _date(x) -> _dt.date | None:
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def _num(v, what: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise FinderError("The %s must be a number." % what) from None
    if not f > 0 or f != f:
        raise FinderError("The %s must be a positive number." % what)
    return f


def _r(x, nd=4):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else round(f, nd)


def prob_above(spot: float, level: float, sigma: float, days: float, r: float = RISK_FREE) -> float | None:
    """The model's chance that the stock ends ABOVE ``level`` after ``days``: lognormal, the contract's own IV,
    drifting at the rate (risk-neutral, the same model the prices come from) - N(d2) with K = ``level``."""
    if not (spot > 0 and level > 0 and sigma > 0 and days > 0):
        return None
    T = days / 365.0
    d2 = (math.log(spot / level) + (r - sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    return 0.5 * (1 + math.erf(d2 / math.sqrt(2)))


def find(sdb, symbol, *, target, worst, strategy="long_call", mode="expiry", on=None, today: _dt.date | None = None,
         band: float = BAND, min_oi: int = MIN_OI, r: float = RISK_FREE, entry=None) -> dict:
    """The ranked contracts for one ticker. Raises FinderError / ChainError with a message for the member."""
    if strategy not in BUILT:
        known = {s[0]: s[1] for s in STRATEGIES}
        raise FinderError("%s is coming soon - only Buy a call is built so far." % known.get(strategy, "That strategy"))
    if mode not in MODES:
        raise FinderError("Value the contracts at each expiry or by a date.")
    t, w = _num(target, "target price"), _num(worst, "worst-case price")
    today = today or _dt.date.today()
    ch = load_chain(sdb, symbol)
    spot = float(ch.spot or 0)
    if spot <= 0:
        raise ChainError("the last market pass has no price for %s" % ch.symbol)
    if entry is None or str(entry).strip() == "":
        px, px_src = spot, "last"                           # the last market pass's price (15 min delayed)
    else:
        px, px_src = _num(entry, "entry price"), "user"
        if abs(px - spot) > ENTRY_MAX_MOVE * spot:
            raise FinderError("Set an entry price within %d%% of today's price (%.2f)." % (round(ENTRY_MAX_MOVE * 100), spot))
    if t <= px:
        raise FinderError("Buy a call profits when the stock rises: set a target above %s (%.2f)."
                          % ("your entry price" if px_src == "user" else "today's price", px))
    if w >= t:
        raise FinderError("The worst-case price must be below the target price.")
    when = None
    if mode == "date":
        when = _date(on)
        if when is None:
            raise FinderError("Pick the date to value the calls on.")
        if when <= today:
            raise FinderError("Pick a date after today.")
        if (when - today).days > DATE_MAX_DAYS:
            raise FinderError("Pick a date within %d days." % DATE_MAX_DAYS)
    lo_k, hi_k = px * (1 - band), px * (1 + band)
    rows, n_seen, n_thin, n_cheap = [], 0, 0, 0
    earn = _date(ch.earnings_date)
    for c in ch.contracts:
        if c.right != "C" or not c.iv or c.iv <= 0 or not (lo_k <= c.strike <= hi_k):
            continue
        exp = _date(c.expiry)
        if exp is None:
            continue
        dte = (exp - today).days
        if dte < MIN_DTE:
            continue
        decay = dte if mode == "expiry" else (when - today).days
        if decay > dte:
            continue                                        # expires before the chosen date
        n_seen += 1
        if (c.oi or 0) < min_oi:
            n_thin += 1
            continue
        res = evaluate([Leg("c", c.strike, dte, c.iv, 1)], Scenario(px, t, w, decay, r=r), with_curves=False)
        if (res.entry_value or 0) < MIN_COST:
            n_cheap += 1
            continue
        be = res.breakevens[0] if res.breakevens else None
        no_loss = res.loss_at_worst is not None and res.loss_at_worst >= 0
        on_day = today + _dt.timedelta(days=decay)
        rows.append({
            "expiry": exp.isoformat(), "dte": dte, "strike": c.strike, "iv": _r(c.iv, 6), "oi": c.oi,
            "monthly": vol_surface.is_monthly(exp),         # v4.229: the standard monthly (the surface's "M")
            "volume": c.volume, "decay": decay, "valued_on": on_day.isoformat(),
            "cost": _r(res.entry_value), "at_target": _r(res.target_value), "at_worst": _r(res.worst_value),
            "gain": _r(res.gain_at_target), "loss": _r(res.loss_at_worst),
            "breakeven": _r(be), "be_pct": _r((be - px) / px, 6) if be is not None else None,
            "rr": _r(res.reward_over_risk), "no_loss": no_loss, "roi": _r(res.roi, 6),
            "prob": _r(prob_above(px, be, c.iv, decay, r), 6) if be is not None else None,
            "earnings": bool(earn and today < earn <= on_day),
        })
    rows.sort(key=lambda x: (-(1e12 if x["no_loss"] else x["rr"] if x["rr"] is not None else -1e9), x["expiry"], x["strike"]))
    sa = getattr(ch, "spot_as_of", None)
    return {"symbol": ch.symbol, "name": ch.name, "spot": spot, "spot_src": ch.spot_src, "session": ch.session,
            "spot_as_of": sa.replace(microsecond=0).isoformat() + "Z" if isinstance(sa, _dt.datetime) else None,
            "as_of": ch.as_of.isoformat() if ch.as_of else None, "today": today.isoformat(),
            "strategy": strategy, "mode": mode, "on": when.isoformat() if when else None,
            "target": t, "worst": w, "rate": r, "band": band, "min_oi": min_oi,
            "entry": px, "entry_src": px_src,                # v4.234: the stock price every figure is worked from
            "n_checked": n_seen, "n_thin": n_thin, "n_cheap": n_cheap, "min_dte": MIN_DTE, "min_cost": MIN_COST, "n": len(rows), "earnings_date": ch.earnings_date, "rows": rows}


__all__ = ["ENTRY_MAX_MOVE", "BAND", "MIN_OI", "MIN_DTE", "MIN_COST", "prob_above", "STRATEGIES", "BUILT", "MODES", "SORTS", "FinderError", "find"]
