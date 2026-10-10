"""Multi-leg screens (OPTIONS_SCREENER_DESIGN.md §6): verticals, straddles / strangles, the
protective collar, calendars / diagonals, butterflies, condors and the iron structures.

How it stays tractable on the whole market
------------------------------------------
1. **Filter before pairing.** Each leg gets a boolean mask over every contract: the
   underlying filters, the contract filters (they apply to every leg), that leg's own
   ``legN.*`` filters, its right, and a usable estimated price. Only contracts that pass
   their leg's mask are ever paired.
2. **Pair inside groups with index arithmetic, no Python loops over contracts.** The frame
   is sorted (symbol, right, expiry, strike), so the strikes of one (symbol, right, expiry)
   group are a contiguous ascending run: a partner "k listed strikes above" is index ``i+k``.
   Ranges of partners are expanded with ``np.repeat`` (``_expand``); exact partners (the
   other wing of a butterfly at the same width, the put at the call's strike) are found with
   ``np.searchsorted`` on the frame's sorted int64 keys (``ck`` / ``sk``, strikes in
   thousandths of a dollar - equal widths are exact to the 0.1 cent).
3. **Bounds.** Adjacent strikes of a structure are at most ``MAX_APART`` listed strikes
   apart. Cross-right legs (strangle, collar, iron condor) take the call among the
   ``MAX_APART`` listed call strikes above the higher of the put strike and the stock price.
   Calendars pair every later expiry at the same strike; diagonals every later expiry with
   the far strike within ``MAX_APART`` listed strikes on the correct side.
4. **Chunks and a cap.** Anchors are processed in chunks sized so no stage holds more than
   ``PAIR_BUDGET`` candidates; the metrics, the strategy-level filters and a running top-k
   sort run per chunk, so memory stays flat. Past ``COMBO_CAP`` candidate combinations the
   run stops with a warning (the rows found so far are kept).

Metrics (per share unless noted; $ figures per contract, x100): net (+ = debit), BEs,
max profit / loss, Max Profit % = max profit / max loss, Risk/Reward = max loss / max
profit, probabilities ``P(S_T > X) = N(d2)`` with sigma = the IV (and T) of the leg whose
strike is nearest X, net greeks (buy +, sell -; the collar adds the stock's delta of 1),
IV/HV = mean leg IV / HV20, IV skew = near IV - far IV.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .fields import Plan, Table, TopK, base_mask, leg_mask, mask_table
from .frame import KEY_SPAN, Frame, prob_above
from .screens import Screen

MAX_APART = 10            # adjacent strikes of a structure at most this many listed strikes apart
COMBO_CAP = 4_000_000     # candidate combinations per run before it stops with a warning
PAIR_BUDGET = 2_000_000   # candidates a chunk's widest pairing stage may hold
MIN_CHUNK = 1000          # anchors per chunk at least (keeps numpy calls large enough to pay off)

_I64 = np.int64
_EMPTY = np.zeros(0, dtype=_I64)


def _expand(starts, ends):
    """Every (row, position) with ``starts[row] <= position < ends[row]``, vectorised."""
    starts = np.asarray(starts, dtype=_I64)
    ends = np.asarray(ends, dtype=_I64)
    cnt = np.maximum(ends - starts, 0)
    total = int(cnt.sum())
    if total == 0:
        return _EMPTY, _EMPTY
    rep = np.repeat(np.arange(len(starts), dtype=_I64), cnt)
    off = np.cumsum(cnt) - cnt
    pos = starts[rep] + (np.arange(total, dtype=_I64) - off[rep])
    return rep, pos


def _find(keys: np.ndarray, targets: np.ndarray):
    """Exact matches of ``targets`` in the sorted ``keys``: (found mask, position)."""
    if len(keys) == 0 or len(targets) == 0:
        return np.zeros(len(targets), dtype=bool), np.zeros(len(targets), dtype=_I64)
    pos = np.searchsorted(keys, targets)
    posc = np.minimum(pos, len(keys) - 1)
    return (pos < len(keys)) & (keys[posc] == targets), posc


def _calls_above(fr: Frame, a: np.ndarray, from_spot: bool):
    """For put rows ``a``: the listed calls of the same (symbol, expiry) with a strike above
    the put's (or above the stock price when ``from_spot``), up to ``MAX_APART`` past the
    higher of the put strike and the stock price. -> (row of a, call-subset position)."""
    c = fr.c
    cpos, csk = fr.right_index(put=False)
    if len(csk) == 0 or len(a) == 0:
        return _EMPTY, _EMPTY
    se = c["g_se"][a]
    spot_m = np.rint(np.nan_to_num(c["spot"][a]) * 1000.0).astype(_I64)
    anchor = np.maximum(c["km"][a], spot_m)
    mid = np.searchsorted(csk, se * KEY_SPAN + anchor, side="right")
    end = np.searchsorted(csk, (se + 1) * KEY_SPAN, side="left")
    lo = mid if from_spot else np.searchsorted(csk, c["sk"][a], side="right")
    hi = np.minimum(mid + MAX_APART, end)
    return _expand(lo, hi)


# ─────────────────────────────────── pairing ───────────────────────────────────
# Each takes (frame, leg masks by role, anchor contract indices, context) and returns
# {role: contract indices}, one combination per position.

def _pair_vertical(fr, M, a, ctx):
    c = fr.c
    rep, j = _expand(a + 1, np.minimum(a + 1 + MAX_APART, c["gend"][a]))
    keep = M["hi"][j]
    return {"lo": a[rep][keep], "hi": j[keep]}


def _pair_butterfly(fr, M, a, ctx):
    c = fr.c
    ck = c["ck"]
    rep, i1 = _expand(np.maximum(c["gstart"][a], a - MAX_APART), a)
    keep = M["k1"][i1]
    i1, i2 = i1[keep], a[rep][keep]
    ok, i3 = _find(ck, 2 * ck[i2] - ck[i1])
    ok &= M["k3"][i3] & (i3 - i2 <= MAX_APART)
    return {"k1": i1[ok], "k2": i2[ok], "k3": i3[ok]}


def _pair_condor(fr, M, a, ctx):
    c = fr.c
    ck, gend = c["ck"], c["gend"]
    rep, i2 = _expand(a + 1, np.minimum(a + 1 + MAX_APART, gend[a]))
    keep = M["k2"][i2]
    i1, i2 = a[rep][keep], i2[keep]
    rep, i3 = _expand(i2 + 1, np.minimum(i2 + 1 + MAX_APART, gend[i2]))
    keep = M["k3"][i3]
    i1, i2, i3 = i1[rep][keep], i2[rep][keep], i3[keep]
    ok, i4 = _find(ck, ck[i3] + (ck[i2] - ck[i1]))
    ok &= M["k4"][i4] & (i4 - i3 <= MAX_APART)
    return {"k1": i1[ok], "k2": i2[ok], "k3": i3[ok], "k4": i4[ok]}


def _pair_straddle(fr, M, a, ctx):
    ppos, psk = fr.right_index(put=True)
    if len(ppos) == 0 or len(a) == 0:
        return {"C": _EMPTY, "P": _EMPTY}
    ok, k = _find(psk, fr.c["sk"][a])
    ip = ppos[k]
    ok &= M["P"][ip]
    return {"C": a[ok], "P": ip[ok]}


def _pair_strangle(fr, M, a, ctx):
    cpos, _ = fr.right_index(put=False)
    rep, k = _calls_above(fr, a, from_spot=False)
    ic = cpos[k]
    keep = M["C"][ic]
    return {"P": a[rep][keep], "C": ic[keep]}


def _pair_collar(fr, M, a, ctx):
    cpos, _ = fr.right_index(put=False)
    rep, k = _calls_above(fr, a, from_spot=True)
    ic = cpos[k]
    keep = M["C"][ic]
    return {"P": a[rep][keep], "C": ic[keep]}


def _pair_iron_butterfly(fr, M, a, ctx):
    c = fr.c
    cpos, csk = fr.right_index(put=False)
    if len(cpos) == 0 or len(a) == 0:
        return {r: _EMPTY for r in ("p1", "p2", "c2", "c3")}
    ok, k2 = _find(csk, c["sk"][a])                 # the call body at the put body's strike
    ok &= M["c2"][cpos[k2]]
    a, k2 = a[ok], k2[ok]
    rep, ip1 = _expand(np.maximum(c["gstart"][a], a - MAX_APART), a)
    keep = M["p1"][ip1]
    ip1, ip2, k2 = ip1[keep], a[rep][keep], k2[rep][keep]
    ok, k3 = _find(csk, csk[k2] + (c["km"][ip2] - c["km"][ip1]))   # the call wing, same width
    ok &= M["c3"][cpos[k3]] & (k3 - k2 <= MAX_APART)
    return {"p1": ip1[ok], "p2": ip2[ok], "c2": cpos[k2[ok]], "c3": cpos[k3[ok]]}


def _pair_iron_condor(fr, M, a, ctx):
    c = fr.c
    cpos, csk = fr.right_index(put=False)
    rep, ip1 = _expand(np.maximum(c["gstart"][a], a - MAX_APART), a)
    keep = M["p1"][ip1]
    ip1, ip2 = ip1[keep], a[rep][keep]
    w = c["km"][ip2] - c["km"][ip1]
    rep, k3 = _calls_above(fr, ip2, from_spot=False)
    if len(k3) == 0:
        return {r: _EMPTY for r in ("p1", "p2", "c3", "c4")}
    ic3 = cpos[k3]
    keep = M["c3"][ic3]
    ip1, ip2, w, k3, ic3 = ip1[rep][keep], ip2[rep][keep], w[rep][keep], k3[keep], ic3[keep]
    ok, k4 = _find(csk, csk[k3] + w)
    ic4 = cpos[k4]
    ok &= M["c4"][ic4] & (k4 - k3 <= MAX_APART)
    return {"p1": ip1[ok], "p2": ip2[ok], "c3": ic3[ok], "c4": ic4[ok]}


def _pair_calendar(fr, M, a, ctx):
    order, rank, gend_k = fr.strike_order()
    p = rank[a]
    rep, q = _expand(p + 1, gend_k[p])
    jf = order[q]
    keep = M["far"][jf]
    return {"near": a[rep][keep], "far": jf[keep]}


def _pair_diagonal(fr, M, a, ctx):
    c, g = fr.c, fr.g
    ck = c["ck"]
    sr_end = fr.sr_group_end()
    gi = c["g_sre"][a]
    rep, gf = _expand(gi + 1, sr_end[gi])
    keep = ctx["group_has_far"][gf]
    near, gf = a[rep][keep], gf[keep]
    t = gf * KEY_SPAN + c["km"][near]
    if ctx["far_lower"]:                       # calls: the far strike sits below the near one
        hi = np.searchsorted(ck, t, side="left")
        lo = np.maximum(g["start"][gf], hi - MAX_APART)
    else:                                      # puts: the far strike sits above the near one
        lo = np.searchsorted(ck, t, side="right")
        hi = np.minimum(lo + MAX_APART, g["end"][gf])
    rep, jf = _expand(lo, hi)
    keep = M["far"][jf]
    return {"near": near[rep][keep], "far": jf[keep]}


@dataclass(frozen=True)
class Kind:
    anchor: str
    pair: Callable
    fanout: int          # rough candidates per anchor at the widest stage (sizes the chunks)


KINDS = {
    "vertical": Kind("lo", _pair_vertical, MAX_APART),
    "straddle": Kind("C", _pair_straddle, 1),
    "strangle": Kind("P", _pair_strangle, 2 * MAX_APART),
    "collar": Kind("P", _pair_collar, MAX_APART),
    "butterfly": Kind("k2", _pair_butterfly, MAX_APART),
    "condor": Kind("k1", _pair_condor, MAX_APART * MAX_APART),
    "iron_butterfly": Kind("p2", _pair_iron_butterfly, MAX_APART),
    "iron_condor": Kind("p2", _pair_iron_condor, 2 * MAX_APART * MAX_APART),
    "calendar": Kind("near", _pair_calendar, 40),
    "diagonal": Kind("near", _pair_diagonal, 40 * MAX_APART),
}


# ─────────────────────────────────── metrics ───────────────────────────────────

_PROB_KEYS = ("win_prob", "loss_prob")
_GREEK_KEYS = ("net_delta", "net_gamma", "net_theta", "net_vega")


class _Legs:
    """Per-leg columns of a combination table and the probability helpers."""

    def __init__(self, screen: Screen, t: Table):
        c = t.frame.c
        self.S = c["spot"][t.legs[0]]
        self.K = [c["strike"][x] for x in t.legs]
        self.P = [c["price"][x] for x in t.legs]
        self.IV = [c["iv"][x] for x in t.legs]
        self.T = [c["T"][x] for x in t.legs]
        self.valid = [np.isfinite(v) & (v > 0) for v in self.IV]
        self.R = {leg.role: k for k, leg in enumerate(screen.legs)}

    def k(self, role: str) -> np.ndarray:
        return self.K[self.R[role]]

    def pa(self, X) -> np.ndarray:
        """P(S_T > X) with sigma and T of the leg whose strike is nearest X (the first leg
        on a tie; legs without an IV are skipped)."""
        X = np.asarray(X, dtype=float)
        best = np.full(len(X), np.inf)
        sig = np.full(len(X), np.nan)
        tt = np.full(len(X), np.nan)
        with np.errstate(invalid="ignore"):
            for K, iv, T, ok in zip(self.K, self.IV, self.T, self.valid):
                dk = np.abs(K - X)
                better = ok & (dk < best)
                best = np.where(better, dk, best)
                sig = np.where(better, iv, sig)
                tt = np.where(better, T, tt)
        return prob_above(self.S, X, sig, tt)

    def pk(self, role: str) -> np.ndarray:
        """P(S_T > K) at a leg's own strike: that leg is the nearest, so its own IV."""
        k = self.R[role]
        p = prob_above(self.S, self.K[k], self.IV[k], self.T[k])
        bad = ~self.valid[k]
        if bad.any():
            p = np.where(bad, self.pa(self.K[k]), p)
        return p


def compute(screen: Screen, t: Table, need: set | None = None):
    """-> (metric arrays, validity mask) for every combination of ``t``.

    ``need`` (a set of strategy-level keys) limits the costly figures - probabilities,
    greeks, IV/HV - to the ones a filter or the sort reads; None computes everything. The
    cheap figures (net, break-evens, max profit / loss, width, IV skew) are always there."""
    def want(*keys) -> bool:
        return need is None or any(k in need for k in keys)

    c = t.frame.c
    legs = screen.legs
    g = _Legs(screen, t)
    S = g.S
    wts = [float(leg.sign * leg.qty) for leg in legs]
    net = sum(w * p for w, p in zip(wts, g.P))       # per share, + = paid
    m: dict[str, np.ndarray] = {"net_debit": net * 100.0, "net_credit": -net * 100.0}
    if want(*_GREEK_KEYS):
        for gk in ("delta", "gamma", "theta", "vega"):
            m["net_" + gk] = sum(w * c[gk][x] for w, x in zip(wts, t.legs))
        if screen.stock:
            m["net_delta"] = m["net_delta"] + 1.0
    ok = np.isfinite(net) & np.isfinite(S)
    nan = np.full(t.n, np.nan)
    kind = screen.kind
    k = g.k
    pa, pk = g.pa, g.pk
    with np.errstate(all="ignore"):
        if want("avg_iv_hv"):
            hv = t.frame.u["hv20"][t.sym]
            m["avg_iv_hv"] = np.where(hv > 0, (sum(g.IV) / len(g.IV)) * 100.0 / hv, np.nan)
        d, cr = net, -net                              # debit (long) / credit (short)
        if kind == "vertical":
            lo, hi = k("lo"), k("hi")
            w = hi - lo
            m["width"] = w
            put = legs[0].right == "P"
            x = cr if screen.credit else d
            ok &= (x > 0) & (x < w)
            if screen.credit:
                be = hi - cr if put else lo + cr       # bull put sells hi / bear call sells lo
                m["max_profit"], m["max_loss"] = cr * 100.0, (w - cr) * 100.0
            else:
                be = hi - d if put else lo + d         # bear put buys hi / bull call buys lo
                m["max_profit"], m["max_loss"] = (w - d) * 100.0, d * 100.0
            m["be"] = be
            if want(*_PROB_KEYS):
                above = pa(be)
                # bull put / bull call profit above the BE; bear call / bear put below it
                win = above if (put == screen.credit) else 1.0 - above
                m["win_prob"], m["loss_prob"] = win, 1.0 - win
            if want("max_profit_prob"):
                if put:   # bull put: above the short (hi); bear put: below the short (lo)
                    m["max_profit_prob"] = pk("hi") if screen.credit else 1.0 - pk("lo")
                else:     # bear call: below the short (lo); bull call: above the short (hi)
                    m["max_profit_prob"] = 1.0 - pk("lo") if screen.credit else pk("hi")
        elif kind in ("straddle", "strangle"):
            kp, kc = k("P"), k("C")
            m["width"] = kc - kp
            x = cr if screen.credit else d
            ok &= x > 0
            be_lo, be_hi = kp - x, kc + x
            m["be_lo"], m["be_hi"] = be_lo, be_hi
            if screen.credit:
                m["max_profit"], m["max_loss"] = x * 100.0, nan
            else:
                m["max_profit"], m["max_loss"] = nan, x * 100.0
            if want(*_PROB_KEYS):
                p_out = (1.0 - pa(be_lo)) + pa(be_hi)
                loss = p_out if screen.credit else 1.0 - p_out
                m["win_prob"], m["loss_prob"] = 1.0 - loss, loss
            if want("max_profit_prob"):
                # short: the stock between the strikes (a straddle: one price -> 0); long: unlimited
                m["max_profit_prob"] = (np.clip(pk("P") - pk("C"), 0.0, 1.0) if screen.credit else nan)
        elif kind == "collar":
            k1, k2 = k("P"), k("C")
            nc = cr                                    # call sold - put bought, per share
            ok &= (k1 < S) & (S < k2)
            be = S - nc
            m["width"] = k2 - k1
            m["be"] = be
            m["max_profit"] = (k2 - S + nc) * 100.0
            m["max_loss"] = (S - k1 - nc) * 100.0
            m["cost_pct"] = -nc / S * 100.0
            m["upside_pct"] = np.where(be > 0, (k2 - be) / be * 100.0, np.nan)
            m["downside_pct"] = np.where(be > 0, (be - k1) / be * 100.0, np.nan)
            if want(*_PROB_KEYS):
                win = pa(be)
                m["win_prob"], m["loss_prob"] = win, 1.0 - win
            if want("max_profit_prob"):
                m["max_profit_prob"] = pk("C")
        elif kind in ("butterfly", "condor", "iron_butterfly", "iron_condor"):
            # roles of the inner (body) and outer (wing) strikes
            inner, outer = {"butterfly": (("k2", "k2"), ("k1", "k3")),
                            "condor": (("k2", "k3"), ("k1", "k4")),
                            "iron_butterfly": (("p2", "c2"), ("p1", "c3")),
                            "iron_condor": (("p2", "c3"), ("p1", "c4"))}[kind]
            kin_lo, kin_hi = k(inner[0]), k(inner[1])
            kout_lo, kout_hi = k(outer[0]), k(outer[1])
            w = kin_lo - kout_lo
            m["width"] = w
            x = cr if screen.credit else d
            ok &= (x > 0) & (x < w)
            iron = kind.startswith("iron")
            # butterfly / condor: the long trade profits INSIDE (Kout_lo + x, Kout_hi - x);
            # iron: the long trade profits OUTSIDE (Kin_lo - x, Kin_hi + x)
            be_lo, be_hi = (kin_lo - x, kin_hi + x) if iron else (kout_lo + x, kout_hi - x)
            m["be_lo"], m["be_hi"] = be_lo, be_hi
            if screen.credit:
                m["max_profit"], m["max_loss"] = x * 100.0, (w - x) * 100.0
            else:
                m["max_profit"], m["max_loss"] = (w - x) * 100.0, x * 100.0
            if want(*_PROB_KEYS):
                p_mid = np.clip(pa(be_lo) - pa(be_hi), 0.0, 1.0)
                long_win = 1.0 - p_mid if iron else p_mid
                win = 1.0 - long_win if screen.credit else long_win
                m["win_prob"], m["loss_prob"] = win, 1.0 - win
            if want("max_profit_prob"):
                # the long trade's max profit sits between the inner strikes (butterfly / condor)
                # or beyond the wings (iron); the short trade's is the other one. A body at a
                # single strike (butterfly, iron butterfly) is one price -> probability 0.
                at_body = (not iron) != screen.credit
                if at_body:
                    single = kind in ("butterfly", "iron_butterfly")
                    m["max_profit_prob"] = (np.zeros(t.n) if single
                                            else np.clip(pk(inner[0]) - pk(inner[1]), 0.0, 1.0))
                else:
                    m["max_profit_prob"] = np.clip((1.0 - pk(outer[0])) + pk(outer[1]), 0.0, 1.0)
        elif kind in ("calendar", "diagonal"):
            near, far = g.R["near"], g.R["far"]
            ok &= (cr > 0) if screen.credit else (d > 0)
            m["iv_skew"] = (g.IV[near] - g.IV[far]) * 100.0
            m["width"] = np.abs(g.K[near] - g.K[far])

        for b in ("be", "be_hi", "be_lo"):
            if b in m:
                m[b + "_pct"] = (m[b] - S) / S * 100.0
        if "max_profit" in m and "max_loss" in m:
            mp, ml = m["max_profit"], m["max_loss"]
            m["max_profit_pct"] = np.where(ml > 0, mp / ml * 100.0, np.nan)
            m["risk_reward"] = np.where(mp > 0, ml / mp, np.nan)
        for key in ("win_prob", "loss_prob", "max_profit_prob"):
            if key in m:
                m[key] = np.clip(m[key], 0.0, 1.0) * 100.0
    return m, ok


# ─────────────────────────────────── the run ───────────────────────────────────

def leg_masks(frame: Frame, screen: Screen, plan: Plan) -> dict:
    """Each leg's candidates: base filters, its right, a usable est. price, its own filters."""
    c = frame.c
    base = base_mask(frame, plan) & np.isfinite(c["price"]) & (c["price"] >= 0)
    M = {}
    for n, leg in enumerate(screen.legs, 1):
        m = base & (c["is_put"] == (1 if leg.right == "P" else 0)) & leg_mask(frame, plan, n)
        if screen.kind == "collar":
            m &= (c["strike"] < c["spot"]) if leg.right == "P" else (c["strike"] > c["spot"])
        M[leg.role] = m
    return M


def run(frame: Frame, screen: Screen, plan: Plan, *, combo_cap: int | None = None):
    """-> (sorted table cut to ``plan.max_rows`` with every metric, total matches, warnings,
    stopped). Per chunk only the metrics the strategy filters and the sort read are
    computed; the kept rows get the full set at the end."""
    cap = COMBO_CAP if combo_cap is None else combo_cap
    kind = KINDS[screen.kind]
    M = leg_masks(frame, screen, plan)
    ctx: dict = {}
    if screen.kind == "diagonal":
        starts = frame.g["start"]
        ctx["group_has_far"] = ((np.add.reduceat(M["far"].astype(_I64), starts) > 0) if len(starts)
                                else np.zeros(0, bool))
        ctx["far_lower"] = screen.legs[0].right == "C"
    need = {f.field.key for f in plan.s}
    if plan.sort.level == "strategy":
        need.add(plan.sort.key)
    anchors = np.flatnonzero(M[kind.anchor])
    step = max(MIN_CHUNK, PAIR_BUDGET // max(kind.fanout, 1))
    top = TopK(plan.sort, plan.desc, plan.max_rows)
    warnings: list[str] = []
    produced = 0
    stopped = False
    for s in range(0, len(anchors), step):
        part = anchors[s:s + step]
        roles = kind.pair(frame, M, part, ctx)
        legs = [roles[leg.role] for leg in screen.legs]
        if len(legs[0]) == 0:
            continue
        produced += len(legs[0])
        t = Table(frame, legs)
        t.m, ok = compute(screen, t, need)
        t = t.take(ok)
        if plan.s and t.n:
            t = t.take(mask_table(plan.s, t))
        top.add(t)
        if produced >= cap and s + step < len(anchors):
            last = frame.symbols[int(frame.c["sym"][part[-1]])]
            warnings.append(f"Stopped after {produced:,} candidate combinations (symbols up to {last}): "
                            "narrow the days to expiration or the deltas to cover the whole market.")
            stopped = True
            break
    kept = top.finish(frame, len(screen.legs))
    if kept.n:
        kept.m, _ = compute(screen, kept)
    return kept, top.total, warnings, stopped
