"""Sizing - contracts from risk % of NLV at the CHART stop, capped by the gap rule
and the 10 % notional cap (design/options/part_B_engines.md B5; OPTIONS_MODULE_DESIGN.md
II.2.4).

``size(pick, nlv, prefs) -> dict`` is THE sizing function on the platform. It runs at
READ time (the card, the picks partial, the ticket, the Live path) over a stored
pick - never by the nightly job, never by a template - so an account-value edit
changes the figure on the next read without invalidating a single cached pick,
and a Live press re-sizes the same picks from the broker's balance. The loss at
the chart stop was computed once, at write time, into ``pick["chart_stop_pl"]``;
this function only divides::

    risk_budget      = nlv x risk_pct / 100
    loss_at_stop_usd = max(0, -pick.chart_stop_pl)
    by_chart_stop    = floor(risk_budget / loss_at_stop_usd)        # None when the stop loses nothing
    by_gap           = floor(risk_budget x GAP_MULT / max_loss_usd)  # GAP_MULT = shared.gap_mult (house 2.0)
    by_notional      = floor(nlv x MAX_POSITION_PCT/100 / notional)  # the 10 % cap, a constant
    contracts        = min of the three that exist

``floor``, never round up, and NEVER ``max(1, ...)``: 0 is a valid answer with the
note "Not even one contract fits your 1% - lower the risk or choose a narrower
spread". ``notional`` is the collateral a contract ties up: the width x 100 for a
credit family (what the broker holds for a short vertical / the condor's wider
wing), the max loss for every other family (what you paid is what you can lose).
The golden figures: NLV 100,000 / 1 % / gap 2.0 on the 330/320 at 2.10 -> 8 / 2 /
10 -> 2 contracts, "about $242 if the stop fires, up to $1,580 (1.6% of your
account) if the stock gaps past it".

NLV resolution is the CALLER's (II.2.4): the Live figure for this request
(``prefs["account"]["nlv_source"] == "live"``) -> the stored ``trade_prefs`` value
(``"prefs"``) -> None, which sizes nothing and says so. Nothing here is written.
"""
from __future__ import annotations

import math

from . import option_words, payoff
from .opt_constants import (GAP_MULT_HOUSE, LOSS_STOP_FRACTION, MAX_POSITION_PCT, MULT,
                            STOP_IV_BUMP, STOP_TIMES)

CREDIT_FAMILIES = ("credit_vertical", "condor")
NLV_SOURCES = ("live", "prefs", None)


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _floor(x: float | None) -> int | None:
    if x is None or not math.isfinite(x):
        return None
    return int(math.floor(x + 1e-9))


def _shared(prefs: dict | None, key: str, default):
    """A shared-block field from the merged read() dict, a flat for_strategy() dict,
    or nothing."""
    p = prefs or {}
    shared = p.get("shared") if isinstance(p.get("shared"), dict) else None
    if shared and key in shared:
        return shared[key]
    if key in p:
        return p[key]
    return default


def _stop_time(pick: dict) -> tuple[int, float | None]:
    """``(stop_t_days, stop_iv)``: which of ``STOP_TIMES x dte`` produced the stored
    loss (re-evaluated from the legs - two model calls, microseconds) and the
    short leg's bumped sigma. ``(0, None)`` when the legs cannot be valued."""
    legs = [l for l in (pick.get("legs") or []) if isinstance(l, dict)]
    short = next((l for l in legs if l.get("side") == "sell"), legs[0] if legs else None)
    iv = _num(short.get("iv")) if short else None
    stop_iv = round(iv * (1.0 + STOP_IV_BUMP), 4) if iv else None
    stop = _num(pick.get("chart_stop"))
    dte = _num(pick.get("dte"))
    if stop is None or not legs or not dte or dte <= 0:
        return 0, stop_iv
    try:
        as_of = pick.get("as_of") or pick.get("snap_on")
        if as_of is None:
            import datetime as _dt
            front = _dt.date.fromisoformat(str(pick.get("expiry") or legs[0].get("expiry"))[:10])
            as_of = (front - _dt.timedelta(days=int(dte))).isoformat()
        pl = payoff._legs(legs)
        worst, worst_d = None, 0
        for frac in STOP_TIMES:
            d = frac * dte
            loss = -payoff.pnl(pl, stop, d, as_of, STOP_IV_BUMP, strict=False)
            if worst is None or loss > worst:
                worst, worst_d = loss, int(round(d))
        return worst_d, stop_iv
    except Exception:  # noqa: BLE001 - the stored loss is the figure; the t is decoration
        return 0, stop_iv


def size(pick: dict, nlv, prefs: dict | None) -> dict:
    """The II.2.4 dict for one stored pick.

    ``nlv`` is the account value the CALLER resolved (None = not known: contracts
    None and the account note); ``prefs`` the merged ``option_prefs.read()`` dict
    (``shared.gap_mult``, ``account.risk_pct`` / ``nlv_source``; a flat
    ``for_strategy`` dict works too). An optional ``prefs["exits"]["loss_fraction"]``
    (the member's ``trade_prefs`` line, never hashed) re-derives the credit rule
    stop; otherwise the pick's own ``rule_stop_pl`` (house 20 %) stands.
    """
    prefs = prefs or {}
    account = prefs.get("account") if isinstance(prefs.get("account"), dict) else {}
    nlv = _num(nlv)
    nlv = nlv if (nlv is not None and nlv > 0) else None
    risk_pct = _num(account.get("risk_pct"))
    if risk_pct is None:
        risk_pct = _num(prefs.get("risk_pct")) or 1.0
    gap_mult = _num(_shared(prefs, "gap_mult", GAP_MULT_HOUSE)) or GAP_MULT_HOUSE
    nlv_source = account.get("nlv_source") if nlv is not None else None
    if nlv is not None and nlv_source not in ("live", "prefs"):
        nlv_source = "prefs"

    family = pick.get("family") or "credit_vertical"
    max_loss = _num(pick.get("max_loss"))
    width = _num(pick.get("width"))
    loss_at_stop = max(0.0, -(_num(pick.get("chart_stop_pl")) or 0.0))
    stop_price = _num(pick.get("chart_stop"))
    stop_t, stop_iv = _stop_time(pick)

    # the rule stop: the pick's write-time figure, re-derived when the member's
    # own loss line is handed in (credit families) - never from the hash
    exits = prefs.get("exits") if isinstance(prefs.get("exits"), dict) else {}
    rule_usd = -(_num(pick.get("rule_stop_pl")) or 0.0)
    if family in CREDIT_FAMILIES:
        frac = _num(exits.get("loss_fraction"))
        if frac is None:
            frac = _num(prefs.get("loss_fraction"))
        if frac is None:
            frac = round(rule_usd / max_loss, 4) if (max_loss and rule_usd) else LOSS_STOP_FRACTION
        if max_loss:
            rule_usd = round(frac * max_loss, 2)
        rule_kind = f"{frac * 100:g}% of max loss"
    else:
        pct = _num(prefs.get("premium_stop_pct"))
        if pct is None:
            block = "leaps" if family == "leaps" else "long"
            pct = _num(((prefs.get(block) or {}) if isinstance(prefs.get(block), dict) else {}).get("premium_stop_pct"))
        rule_kind = f"{pct:g}% of what you paid" if pct is not None else "the rule stop"
    fires_first = None
    if loss_at_stop > 0 and rule_usd > 0:
        fires_first = "chart" if loss_at_stop <= rule_usd else "rule"
    elif rule_usd > 0:
        fires_first = "rule"
    elif loss_at_stop > 0:
        fires_first = "chart"

    notional = (width * MULT) if (family in CREDIT_FAMILIES and width) else max_loss
    out = {
        "nlv": nlv, "nlv_source": nlv_source, "risk_pct": risk_pct, "risk_budget": None,
        "gap_mult": gap_mult,
        "loss_at_stop_usd": round(loss_at_stop, 2), "stop_price": stop_price,
        "stop_t_days": stop_t, "stop_iv": stop_iv,
        "rule_stop_usd": round(rule_usd, 2), "rule_stop_kind": rule_kind, "fires_first": fires_first,
        "by_chart_stop": None, "by_gap": None, "by_notional": None, "contracts": None,
        "capital_at_risk_usd": None, "max_loss_total_usd": None, "max_loss_pct_nlv": None,
        "line": None, "note": None,
    }
    if nlv is None:
        out["note"] = option_words.NLV_MISSING
        out["line"] = option_words.sizing_line(out)
        return out
    budget = nlv * risk_pct / 100.0
    out["risk_budget"] = round(budget, 2)
    by_stop = _floor(budget / loss_at_stop) if loss_at_stop > 0 else None
    by_gap = _floor(budget * gap_mult / max_loss) if (max_loss and max_loss > 0) else None
    by_not = _floor(nlv * MAX_POSITION_PCT / 100.0 / notional) if (notional and notional > 0) else None
    caps = [c for c in (by_stop, by_gap, by_not) if c is not None]
    n = min(caps) if caps else None
    out.update({"by_chart_stop": by_stop, "by_gap": by_gap, "by_notional": by_not, "contracts": n})
    if n is None:
        out["note"] = "cannot size this pick: no max loss on it"
        out["line"] = option_words.sizing_line(out)
        return out
    out["capital_at_risk_usd"] = round(n * loss_at_stop, 2)
    out["max_loss_total_usd"] = round(n * (max_loss or 0.0), 2)
    out["max_loss_pct_nlv"] = round(n * (max_loss or 0.0) / nlv * 100.0, 1)
    if n <= 0:
        out["note"] = option_words.NOT_EVEN_ONE.format(risk=f"{risk_pct:g}")
    elif by_stop is None:
        out["note"] = option_words.STOP_LOSES_NOTHING
    out["line"] = option_words.sizing_line(out)
    return out
