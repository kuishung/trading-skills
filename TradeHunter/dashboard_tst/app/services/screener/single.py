"""Single-leg screens (OPTIONS_SCREENER_DESIGN.md §5): Options Screener, Long Call, Long Put,
Covered Call, Naked Put, Married Put.

Every contract is a candidate row; the filters are boolean masks over the frame's arrays, so
the whole market screens in a few vectorised passes. Covered call / naked put / married put
add their trade figures (§5 formulas) for the rows that survive the contract filters:

* Covered call - Return ``(p - max(0,S-K)) / (S-p)``; Ann Rtn ``Return x 365/DTE``; Ptnl Rtn
  ``(p + max(0,K-S)) / (S-p)``; BE ``S-p``; Profit prob ``P(S_T > BE)``.
* Naked put - Return ``p / (K-p)``; Ann Rtn; BE ``K-p``; Profit prob ``P(S_T > BE)``.
* Married put - BE ``S+p``; Max loss ``(S+p-K) x 100``; Downside ``(S+p-K)/(S+p)``;
  Profit prob ``P(S_T > BE)``.
"""
from __future__ import annotations

import numpy as np

from .fields import Plan, Table, TopK, base_mask, leg_mask, mask_table
from .frame import Frame, prob_above
from .screens import Screen

_TRADE_KINDS = ("covered", "naked", "married")


def trade_metrics(kind: str, t: Table) -> dict:
    """The covered call / naked put / married put figures for every row of ``t``."""
    c = t.frame.c
    i = t.legs[0]
    S, K, p = c["spot"][i], c["strike"][i], c["price"][i]
    dte, sig, T = c["dte"][i], c["iv"][i], c["T"][i]
    m: dict[str, np.ndarray] = {}
    with np.errstate(all="ignore"):
        if kind == "covered":
            basis = S - p
            ok = basis > 0
            ret = np.where(ok, (p - np.maximum(S - K, 0.0)) / basis * 100.0, np.nan)
            m["return_pct"] = ret
            m["ann_return"] = ret * 365.0 / np.maximum(dte, 1.0)
            m["ptnl_return"] = np.where(ok, (p + np.maximum(K - S, 0.0)) / basis * 100.0, np.nan)
            be = S - p
            m["max_profit"] = (K - S + p) * 100.0
            m["max_loss"] = np.where(ok, basis * 100.0, np.nan)
        elif kind == "naked":
            basis = K - p
            ok = basis > 0
            ret = np.where(ok, p / basis * 100.0, np.nan)
            m["return_pct"] = ret
            m["ann_return"] = ret * 365.0 / np.maximum(dte, 1.0)
            be = K - p
            m["max_profit"] = p * 100.0
            m["max_loss"] = np.where(ok, basis * 100.0, np.nan)
        else:  # married
            be = S + p
            m["max_loss"] = (S + p - K) * 100.0
            m["downside_pct"] = np.where(be > 0, (S + p - K) / be * 100.0, np.nan)
        m["be"] = be
        m["be_pct"] = (be - S) / S * 100.0
        win = prob_above(S, be, sig, T) * 100.0
        m["win_prob"] = win
        m["loss_prob"] = 100.0 - win
    return m


def run(frame: Frame, screen: Screen, plan: Plan):
    """-> (sorted table cut to ``plan.max_rows``, total matches, warnings, stopped=False)."""
    c = frame.c
    m = base_mask(frame, plan) & leg_mask(frame, plan, 1)
    if screen.right:
        m &= c["is_put"] == (1 if screen.right == "P" else 0)
    if screen.kind in _TRADE_KINDS:
        m &= np.isfinite(c["price"]) & (c["price"] > 0)
    t = Table(frame, [np.flatnonzero(m)])
    if screen.kind in _TRADE_KINDS:
        t.m = trade_metrics(screen.kind, t)
    if plan.s:
        t = t.take(mask_table(plan.s, t))
    top = TopK(plan.sort, plan.desc, plan.max_rows)
    top.add(t)
    return top.finish(frame, 1), top.total, [], False
