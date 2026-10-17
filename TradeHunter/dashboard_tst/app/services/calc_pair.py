"""Setup pairing (OPTION_CALCULATOR_DESIGN.md step 3, §7): from the two prices the member
typed, find the leg sets that score best, family by family.

    search(chain, target, worst, criteria, today, ...) -> (rows, summary)

1. **Direction** from the expected price against spot: above = bullish families, below =
   bearish (range trades need a second worst price - not in this version).
2. **Horizons**: the first ``horizons_n`` monthly expiries at least three weeks out, each
   evaluated a few days before that expiry (``target_date``); or one horizon when the
   member gave the days. A horizon whose target date lies past an earnings date is
   dropped when the member avoids earnings.
3. **Window** per horizon: expiries on or after the target date (the first two), strikes
   between the worst and the expected price widened by one expected-move unit (spot x ATM
   IV x sqrt T), open interest at least ``min_oi``, at most ``MAX_STRIKES`` per expiry and
   right nearest the move. This is what keeps the search to seconds.
4. **Enumerate** every leg set of each family inside the window and score them all at
   once with numpy: each contract's value curve on the target date is computed once
   (``option_calc.bs_price`` over the grid), a leg set is a weighted sum of curves.
5. **Filter** by the criteria (gain at the expected price > 0, reward over risk, the
   max-loss cap, debit / credit preference, defined risk) and **rank** by reward over
   risk; the best ``1 + alternatives`` per family then go through ``option_calc.evaluate``
   for the full figures and the curves, plus a model probability of profit (lognormal at
   the ATM IV, risk-neutral - a pricing convention, not a forecast). ``score`` = reward
   over risk (capped at 10) x probability of profit.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

import numpy as np

from .calc_chain import Chain, ChainError, Contract, dte
from .calc_store import figures_from, curves_from
from .opt_constants import RISK_FREE
from .option_calc import Leg, Scenario, bs_price, evaluate, ncdf

MAX_STRIKES = 24           # strikes per (expiry, right) in the window, nearest the move
WINDOW_EXPIRIES = 2        # expiries searched per horizon (the first on / after the target date, and the next)
GRID_N = 121               # grid points for the vectorised stage (the top picks get the engine's dense grid)
MIN_BUFFER = 0.02          # the window extends at least 2 % of spot past the prices
MONTHLY_MIN_DAYS = 21      # the first monthly searched is at least three weeks out
TARGET_BEFORE_EXPIRY = 5   # a horizon's target date = its monthly expiry minus this many days
RR_CAP = 10.0              # reward over risk capped in the score
RR_NO_LOSS = 99.0          # reward over risk reported when the worst case still profits
MAX_CANDIDATES = 2_000_000 # a family x horizon larger than this is thinned before scoring

# family -> (direction, legs, defined risk, label, debit/credit)
FAMILIES = {
    "long_call":        ("bull", 1, True,  "Long call",          "debit"),
    "bull_call_spread": ("bull", 2, True,  "Bull call spread",   "debit"),
    "bull_put_spread":  ("bull", 2, True,  "Bull put spread",    "credit"),
    "call_calendar":    ("bull", 2, True,  "Call calendar",      "debit"),
    "call_diagonal":    ("bull", 2, True,  "Call diagonal",      "debit"),
    "short_put":        ("bull", 1, False, "Short put",          "credit"),
    "long_put":         ("bear", 1, True,  "Long put",           "debit"),
    "bear_put_spread":  ("bear", 2, True,  "Bear put spread",    "debit"),
    "bear_call_spread": ("bear", 2, True,  "Bear call spread",   "credit"),
    "put_calendar":     ("bear", 2, True,  "Put calendar",       "debit"),
    "put_diagonal":     ("bear", 2, True,  "Put diagonal",       "debit"),
    "short_call":       ("bear", 1, False, "Short call",         "credit"),
}


@dataclass
class Horizon:
    days: int
    target_date: str
    expiries: list[str] = field(default_factory=list)
    reason: str | None = None          # why it was dropped


@dataclass
class _Table:
    """The window's contracts as arrays, priced today (``p0``) and on the target date
    over ``grid`` (``v``)."""
    contracts: list[Contract]
    grid: np.ndarray
    i_target: int
    i_worst: int
    p0: np.ndarray            # (n,)
    v: np.ndarray             # (n, g)
    expiry: np.ndarray        # (n,) str
    right: np.ndarray         # (n,) str
    strike: np.ndarray        # (n,) float
    by: dict                  # (expiry, right) -> np.ndarray of indices sorted by strike


def _monthly(d: _dt.date) -> bool:
    from .opt_massive import monthly_expiry  # noqa: PLC0415

    return monthly_expiry(d.year, d.month) == d


def direction_of(spot: float, target: float) -> str:
    if target > spot:
        return "bull"
    if target < spot:
        return "bear"
    raise ChainError("the expected price equals the current price - pick one above or below it")


def horizons_for(chain: Chain, today: _dt.date, *, horizon_days: int | None, horizons_n: int,
                 avoid_earnings: bool) -> list[Horizon]:
    """The horizons to search (see the module doc). Dropped ones carry a ``reason``."""
    exps = [e for e in chain.expiries() if _dt.date.fromisoformat(e) > today]
    out: list[Horizon] = []
    if horizon_days:
        out.append(Horizon(days=int(horizon_days), target_date=(today + _dt.timedelta(days=int(horizon_days))).isoformat()))
    else:
        for e in exps:
            d = _dt.date.fromisoformat(e)
            if (d - today).days >= MONTHLY_MIN_DAYS and _monthly(d):
                days = max((d - today).days - TARGET_BEFORE_EXPIRY, 1)
                out.append(Horizon(days=days, target_date=(today + _dt.timedelta(days=days)).isoformat()))
            if len(out) >= max(1, int(horizons_n)):
                break
        if not out and exps:                       # no monthlies listed: the farthest listed expiry
            d = _dt.date.fromisoformat(exps[-1])
            days = max((d - today).days - TARGET_BEFORE_EXPIRY, 1)
            out.append(Horizon(days=days, target_date=(today + _dt.timedelta(days=days)).isoformat()))
    earn = chain.earnings_date
    for h in out:
        h.expiries = [e for e in exps if e >= h.target_date][:WINDOW_EXPIRIES]
        if not h.expiries:
            h.reason = "no expiry on or after %s" % h.target_date
        elif avoid_earnings and earn and today.isoformat() < earn <= h.target_date:
            h.reason = "earnings on %s fall inside this horizon" % earn
    return out


def _atm_iv(chain: Chain, expiry: str, spot: float) -> float:
    near = sorted((abs(c.strike - spot), c.iv) for c in chain.contracts
                  if c.expiry == expiry and c.iv and c.iv > 0)
    ivs = [iv for _, iv in near[:6]]
    return float(np.median(ivs)) if ivs else 0.30


def window_for(chain: Chain, h: Horizon, spot: float, target: float, worst: float, *,
               min_oi: int) -> tuple[float, float, list[Contract]]:
    """The window's price bounds and contracts for one horizon."""
    iv_ref = _atm_iv(chain, h.expiries[0], spot)
    buf = max(spot * iv_ref * math.sqrt(h.days / 365.0), spot * MIN_BUFFER)
    lo, hi = max(min(spot, target, worst) - buf, 0.01), max(spot, target, worst) + buf
    cs = chain.in_window(expiries=h.expiries, lo=lo, hi=hi, min_oi=min_oi)
    mid = 0.5 * (spot + target)
    groups: dict[tuple, list[Contract]] = {}
    for c in cs:
        groups.setdefault((c.expiry, c.right), []).append(c)
    kept: list[Contract] = []
    for g in groups.values():
        g.sort(key=lambda c: abs(c.strike - mid))
        kept.extend(sorted(g[:MAX_STRIKES], key=lambda c: c.strike))
    return lo, hi, kept


def _table(cs: list[Contract], h: Horizon, spot: float, target: float, worst: float, lo: float,
           hi: float, today: _dt.date, r: float) -> _Table:
    grid = np.unique(np.concatenate([np.linspace(lo, hi, GRID_N), np.array([spot, target, worst])]))
    i_t = int(np.searchsorted(grid, target))
    i_w = int(np.searchsorted(grid, worst))
    n = len(cs)
    p0, v = np.zeros(n), np.zeros((n, len(grid)))
    for i, c in enumerate(cs):
        d = dte(c.expiry, today)
        p0[i] = float(bs_price(spot, c.strike, d / 365.0, c.iv, c.right, r))
        v[i] = bs_price(grid, c.strike, max(d - h.days, 0) / 365.0, c.iv, c.right, r)
    expiry = np.array([c.expiry for c in cs])
    right = np.array([c.right for c in cs])
    strike = np.array([c.strike for c in cs], dtype=float)
    by: dict = {}
    for key in {(e, rt) for e, rt in zip(expiry, right)}:
        idx = np.where((expiry == key[0]) & (right == key[1]))[0]
        by[key] = idx[np.argsort(strike[idx])]
    return _Table(cs, grid, i_t, i_w, p0, v, expiry, right, strike, by)


# ────────────────────────────────── enumerators ──────────────────────────────────
# each returns (idx [m, legs], qty [legs]) - the contract indices of every leg set

def _singles(t: _Table, right: str, qty: int):
    idx = np.concatenate([t.by[k] for k in t.by if k[1] == right] or [np.zeros(0, dtype=int)])
    return idx.reshape(-1, 1), np.array([qty])


def _verticals(t: _Table, right: str, long_lower: bool):
    """Same expiry, two strikes K1 < K2: long the lower / short the higher (``long_lower``)
    or the reverse."""
    parts = []
    for (e, rt), idx in t.by.items():
        if rt != right or len(idx) < 2:
            continue
        i, j = np.triu_indices(len(idx), 1)
        parts.append(np.stack([idx[i], idx[j]], axis=1))
    if not parts:
        return np.zeros((0, 2), dtype=int), np.array([1, -1])
    pairs = np.concatenate(parts)
    return pairs, (np.array([1, -1]) if long_lower else np.array([-1, 1]))


def _calendars(t: _Table, right: str):
    """Same strike, short the nearer expiry, long the farther."""
    exps = sorted({k[0] for k in t.by if k[1] == right})
    parts = []
    for a in range(len(exps)):
        for b in range(a + 1, len(exps)):
            na, nb = t.by.get((exps[a], right)), t.by.get((exps[b], right))
            if na is None or nb is None:
                continue
            ka, kb = t.strike[na], t.strike[nb]
            common, ia, ib = np.intersect1d(ka, kb, return_indices=True)
            if len(common):
                parts.append(np.stack([na[ia], nb[ib]], axis=1))
    if not parts:
        return np.zeros((0, 2), dtype=int), np.array([-1, 1])
    return np.concatenate(parts), np.array([-1, 1])


def _diagonals(t: _Table, right: str, bull: bool):
    """Long the farther expiry, short the nearer at a different strike: for a bullish
    call diagonal the short strike is ABOVE the long; for a bearish put diagonal BELOW."""
    exps = sorted({k[0] for k in t.by if k[1] == right})
    parts = []
    for a in range(len(exps)):
        for b in range(a + 1, len(exps)):
            near, far = t.by.get((exps[a], right)), t.by.get((exps[b], right))
            if near is None or far is None:
                continue
            kn, kf = t.strike[near], t.strike[far]
            fi, ni = np.meshgrid(np.arange(len(far)), np.arange(len(near)), indexing="ij")
            ok = (kn[ni] > kf[fi]) if bull else (kn[ni] < kf[fi])
            if ok.any():
                parts.append(np.stack([far[fi[ok]], near[ni[ok]]], axis=1))
    if not parts:
        return np.zeros((0, 2), dtype=int), np.array([1, -1])
    return np.concatenate(parts), np.array([1, -1])


def _enumerate(family: str, t: _Table):
    f = {
        "long_call": lambda: _singles(t, "C", 1), "short_put": lambda: _singles(t, "P", -1),
        "long_put": lambda: _singles(t, "P", 1), "short_call": lambda: _singles(t, "C", -1),
        "bull_call_spread": lambda: _verticals(t, "C", True), "bull_put_spread": lambda: _verticals(t, "P", True),
        "bear_put_spread": lambda: _verticals(t, "P", False), "bear_call_spread": lambda: _verticals(t, "C", False),
        "call_calendar": lambda: _calendars(t, "C"), "put_calendar": lambda: _calendars(t, "P"),
        "call_diagonal": lambda: _diagonals(t, "C", True), "put_diagonal": lambda: _diagonals(t, "P", False),
    }[family]
    return f()


# ────────────────────────────────── scoring ──────────────────────────────────

def _score_block(t: _Table, idx: np.ndarray, qty: np.ndarray):
    """entry, gain, loss, max_loss, max_profit for every leg set in ``idx``."""
    entry = (t.p0[idx] * qty).sum(axis=1)                      # (m,)
    net = np.einsum("ml,mlg->mg", np.broadcast_to(qty, idx.shape).astype(float), t.v[idx])
    pnl = net - entry[:, None]
    gain = pnl[:, t.i_target]
    loss = pnl[:, t.i_worst]
    return entry, gain, loss, pnl.min(axis=1), pnl.max(axis=1)


def _prob_profit(res, spot: float, sigma: float, T: float, r: float) -> float:
    """Model probability the P/L is positive on the target date: lognormal mass over the
    grid segments where the net curve beats the entry cost."""
    g = np.asarray(res.grid)
    pnl = np.asarray(res.net_curve_target) - res.entry_value
    if not len(g) or sigma <= 0 or T <= 0:
        return float("nan")
    v = sigma * math.sqrt(T)
    with np.errstate(all="ignore"):
        d = (np.log(g / spot) - (r - 0.5 * sigma * sigma) * T) / v
    cdf = ncdf(d)
    cdf = np.concatenate([[0.0], cdf, [1.0]])              # below the grid, above the grid
    pos = np.concatenate([[pnl[0] > 0], pnl > 0, [pnl[-1] > 0]])
    seg = np.diff(cdf)                                      # mass of each segment between grid points
    on = pos[1:] | pos[:-1]                                 # a segment counts when either end profits
    return float(np.clip((seg * on).sum(), 0.0, 1.0))


def search(chain: Chain, target: float, worst: float, criteria: dict, today: _dt.date, *,
           horizon_days: int | None = None, r: float = RISK_FREE, sets: int = 1,
           families: list[str] | None = None) -> tuple[list[dict], dict]:
    """Rows for ``calc_store.write_results`` (one per family and rank) and a summary dict
    (direction, horizons, windows, counts, what was dropped and why)."""
    spot = chain.spot
    if not spot or spot <= 0:
        raise ChainError("the last market pass has no spot price for %s" % chain.symbol)
    direction = direction_of(spot, target)
    alts = int(criteria.get("alternatives", 3))
    min_rr = float(criteria.get("min_rr", 0.0) or 0.0)
    max_loss_cap = float(criteria.get("max_loss", 0.0) or 0.0)
    credit_pref = criteria.get("credit_pref", "any")
    defined_only = bool(criteria.get("defined_risk", True))
    fams = [f for f, (d, _n, defined, _l, dc) in FAMILIES.items()
            if d == direction and (defined or not defined_only)
            and (credit_pref == "any" or dc == credit_pref)
            and (families is None or f in families)]
    hs = horizons_for(chain, today, horizon_days=horizon_days, horizons_n=int(criteria.get("horizons_n", 3)),
                      avoid_earnings=bool(criteria.get("avoid_earnings", True)))
    summary = {"symbol": chain.symbol, "direction": direction, "spot": spot, "target": target, "worst": worst,
               "families": fams, "horizons": [], "n_candidates": 0, "n_kept": 0, "dropped": {},
               "criteria": dict(criteria), "today": today.isoformat()}
    best: dict[str, list[tuple]] = {f: [] for f in fams}        # family -> (rr, roi, h, legs idx, qty, figs)
    for h in hs:
        hrec = {"days": h.days, "target_date": h.target_date, "expiries": h.expiries, "reason": h.reason}
        summary["horizons"].append(hrec)
        if h.reason:
            continue
        lo, hi, cs = window_for(chain, h, spot, target, worst, min_oi=int(criteria.get("min_oi", 0)))
        hrec.update({"lo": round(lo, 4), "hi": round(hi, 4), "n_contracts": len(cs)})
        if len(cs) < 1:
            hrec["reason"] = "no contract in the window with open interest %s+" % criteria.get("min_oi", 0)
            continue
        t = _table(cs, h, spot, target, worst, lo, hi, today, r)
        for fam in fams:
            idx, qty = _enumerate(fam, t)
            m = len(idx)
            summary["n_candidates"] += m
            if m == 0:
                continue
            if m > MAX_CANDIDATES:
                idx = idx[np.linspace(0, m - 1, MAX_CANDIDATES).astype(int)]
            entry, gain, loss, mn, mx = _score_block(t, idx, qty)
            rr = np.where(loss < 0, -gain / np.where(loss < 0, loss, -1.0), RR_NO_LOSS)
            keep = gain > 0
            drop = summary["dropped"]
            drop["no gain at the expected price"] = drop.get("no gain at the expected price", 0) + int((~keep).sum())
            k2 = keep & (rr >= min_rr)
            drop["reward over risk under %g" % min_rr] = drop.get("reward over risk under %g" % min_rr, 0) + int((keep & ~k2).sum())
            if max_loss_cap > 0:
                k3 = k2 & (-mn * 100.0 * sets <= max_loss_cap)
                drop["max loss over the cap"] = drop.get("max loss over the cap", 0) + int((k2 & ~k3).sum())
                k2 = k3
            if not k2.any():
                continue
            sel = np.where(k2)[0]
            roi = np.where(entry != 0, gain / np.abs(np.where(entry != 0, entry, 1.0)), 0.0)
            order = sel[np.lexsort((-roi[sel], -rr[sel]))][: (1 + alts) * 2]
            for o in order:
                best[fam].append((float(rr[o]), float(roi[o]), h, idx[o], qty,
                                  {"entry": float(entry[o]), "gain": float(gain[o]), "loss": float(loss[o]),
                                   "max_loss": float(mn[o]), "max_profit": float(mx[o])}, t))
    rows: list[dict] = []
    for fam, cands in best.items():
        cands.sort(key=lambda c: (-c[0], -c[1]))
        seen, rank = set(), 0
        for rr, roi, h, idx, qty, quick, t in cands:
            key = tuple((t.contracts[i].key, int(q)) for i, q in zip(idx, qty))
            if key in seen:
                continue
            seen.add(key)
            legs, resolved = [], []
            for i, q in zip(idx, qty):
                c = t.contracts[i]
                d = dte(c.expiry, today)
                legs.append(Leg(right=c.right, strike=c.strike, dte=d, iv=c.iv, qty=int(q)))
                resolved.append({"right": c.right, "strike": c.strike, "expiry": c.expiry, "qty": int(q),
                                 "dte": d, "iv": c.iv, "oi": c.oi, "volume": c.volume, "price": c.price,
                                 "delta": c.delta})
            res = evaluate(legs, Scenario(spot, target, worst, h.days, sets=sets, r=r))
            figs = figures_from(res)
            sigma = _atm_iv(chain, h.expiries[0], spot)
            p = _prob_profit(res, spot, sigma, h.days / 365.0, r)
            rr_f = res.reward_over_risk if res.reward_over_risk is not None else RR_NO_LOSS
            score = min(rr_f, RR_CAP) * (p if p == p else 0.5)
            rank += 1
            figs.update({"prob_profit": None if p != p else round(p, 4), "score": round(score, 4),
                         "family_label": FAMILIES[fam][3], "direction": direction,
                         "defined_risk": FAMILIES[fam][2], "debit_credit": FAMILIES[fam][4],
                         "atm_iv": round(sigma, 4)})
            rows.append({"family": fam, "rank": rank, "horizon_days": h.days, "target_date": h.target_date,
                         "legs": resolved, "figures": figs, "score": score,
                         "curves": curves_from(res) if rank == 1 else None})
            if rank > alts:
                break
    summary["n_kept"] = len(rows)
    rows.sort(key=lambda r: (r["rank"], -(r["score"] or 0)))
    return rows, summary
