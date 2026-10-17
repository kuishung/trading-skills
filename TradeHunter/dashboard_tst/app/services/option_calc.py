"""Option calculator engine (OPTION_CALCULATOR_DESIGN.md §4).

The user's Excel strategy calculator (*Option Pricing – Clean Sample 20261017.xlsm*,
notes in ``strategies-reference/OPTION_PRICING_CALCULATOR.md``) as ONE callable
function: up to four legs plus a scenario in, the sheet's decision indicators and
payoff curves out. No page, no database, no data source - the manual calculator page
(step 2) and the setup-pairing loop (step 3) are both callers of :func:`evaluate`.

The math is the sheet's, kept identical so the tie-out test reproduces it:

* Black-Scholes-Merton with a continuous dividend yield, ``T = days / 365``.
* The position is REPRICED ON THE TARGET DATE (each leg at ``dte - decay_days``), not
  at expiry; each leg keeps its own implied volatility.
* ``entry_value`` = Σ price × qty at today's spot; ``target_value`` / ``worst_value`` =
  the net value on the target date at the target / worst price; ``gain`` and ``loss``
  are those minus the entry value; ``reward_over_risk = -gain / loss``; ``roi = gain /
  entry_value``; ``total_decay`` = the value at spot on the target date minus the entry
  value (the sheet's "Total Theta": decay over the whole window, not per day).

Where it differs from the sheet on purpose (the "free fixes" of the design, §4): the
dividend exponent is right; breakevens are EVERY crossing of the target-date curve,
found by bisection on the exact function; the price grid is dense and sized to the
scenario; a leg already expired on the target date is worth its intrinsic value; the
normal CDF is exact (``math.erf``), not the screener's 1e-7 approximation.

Vectorised: :func:`bs_price` takes an array of underlying prices, so a leg's whole
value curve is one call and a leg set is a weighted sum of curves. That is what lets
the pairing loop score thousands of leg sets per second.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from .opt_constants import RISK_FREE

DAY_COUNT = 365.0          # the sheet's calendar-day convention (design §5, decision 2)
GRID_POINTS = 401          # points on the dense price grid
GRID_MIN_BUFFER = 0.05     # the grid extends at least this fraction of spot past the scenario
ROOT_ITERS = 60            # bisection halvings for a breakeven (60 of a $2 bracket -> 2e-18)
MAX_LEGS = 4

_erf = np.frompyfunc(math.erf, 1, 1)


# ────────────────────────────────── inputs ──────────────────────────────────

def _right(v) -> str:
    s = str(v or "").strip().upper()
    if s.startswith("C"):
        return "C"
    if s.startswith("P"):
        return "P"
    raise ValueError(f"right must be a call or a put, got {v!r}")


@dataclass(frozen=True)
class Leg:
    """One option leg as the user enters it (sheet rows 4-13).

    ``dte`` = calendar days to expiry today; ``iv`` a FRACTION (0.20 = 20 %); ``qty``
    = the sheet's weight: + long, - short, in contracts. ``q`` overrides the
    scenario's dividend yield for this leg when given."""
    right: str
    strike: float
    dte: float
    iv: float
    qty: int = 1
    q: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "right", _right(self.right))
        if not self.strike > 0:
            raise ValueError("strike must be positive")
        if self.dte < 0:
            raise ValueError("dte cannot be negative")
        if self.iv < 0:
            raise ValueError("iv cannot be negative")


@dataclass(frozen=True)
class Scenario:
    """What the user types (sheet C4-C7, C28) plus the market settings.

    ``decay_days`` sets the target date; ``iv_override`` prices every leg with one flat
    IV (the sheet's single number) when given - for checking against the workbook;
    ``iv_shift`` is ADDED to every leg's IV on the target date only (reserved for an IV
    scenario; 0 = held constant, design §5 decision 3)."""
    spot: float
    target: float
    worst: float
    decay_days: float
    sets: int = 1
    multiplier: int = 100
    r: float = RISK_FREE
    q: float = 0.0
    iv_shift: float = 0.0
    iv_override: float | None = None

    def __post_init__(self):
        for name in ("spot", "target", "worst"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if self.decay_days < 0:
            raise ValueError("decay_days cannot be negative")


# ────────────────────────────────── math ──────────────────────────────────

def ncdf(x) -> np.ndarray:
    """The standard normal CDF, exact to double precision, vectorised."""
    x = np.asarray(x, dtype=float)
    # frompyfunc hands back a Python float for a 0-d input, an object array otherwise
    e = np.asarray(_erf(x / math.sqrt(2.0)), dtype=float)
    return 0.5 * (1.0 + e)


def bs_price(S, K: float, T: float, sigma: float, right: str, r: float = RISK_FREE,
             q: float = 0.0) -> np.ndarray:
    """Black-Scholes-Merton price of one contract for an ARRAY of underlying prices
    ``S``. ``T`` in years. ``T <= 0`` -> intrinsic value; ``sigma <= 0`` -> the
    discounted forward intrinsic. ``S <= 0`` -> a call is 0, a put is K e^-rT."""
    S = np.asarray(S, dtype=float)
    call = _right(right) == "C"
    if T <= 0:
        return np.maximum(S - K, 0.0) if call else np.maximum(K - S, 0.0)
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    if sigma <= 0:
        fwd = S * dq - K * dr
        return np.maximum(fwd, 0.0) if call else np.maximum(-fwd, 0.0)
    v = sigma * math.sqrt(T)
    with np.errstate(all="ignore"):
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
        d2 = d1 - v
        if call:
            p = S * dq * ncdf(d1) - K * dr * ncdf(d2)
        else:
            p = K * dr * ncdf(-d2) - S * dq * ncdf(-d1)
    bad = ~(S > 0)
    if np.any(bad):
        p = np.where(bad, 0.0 if call else K * dr, p)
    return np.maximum(p, 0.0)


def bs_delta(S, K: float, T: float, sigma: float, right: str, r: float = RISK_FREE,
             q: float = 0.0) -> np.ndarray:
    """Delta per contract, same conventions as :func:`bs_price` (puts negative).
    At or past expiry: 1 / -1 in the money, 0 out, 0.5 / -0.5 exactly at the strike."""
    S = np.asarray(S, dtype=float)
    call = _right(right) == "C"
    if T <= 0:
        itm = (S > K) if call else (S < K)
        d = np.where(itm, 1.0, 0.0) + np.where(S == K, 0.5, 0.0)
        return d if call else -d
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    if sigma <= 0:
        itm = (S * dq > K * dr) if call else (S * dq < K * dr)
        return np.where(itm, dq, 0.0) * (1.0 if call else -1.0)
    v = sigma * math.sqrt(T)
    with np.errstate(all="ignore"):
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
        d = dq * ncdf(d1) if call else -dq * ncdf(-d1)
    return np.where(S > 0, d, 0.0 if call else -dq)


def d1_d2(S: float, K: float, T: float, sigma: float, r: float, q: float) -> tuple[float, float]:
    """The sheet's rows 15-16 for one leg (NaN when not defined)."""
    if not (S > 0 and K > 0 and T > 0 and sigma > 0):
        return math.nan, math.nan
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


# ────────────────────────────────── curves ──────────────────────────────────

def _leg_T(leg: Leg, scen: Scenario, at_target: bool) -> float:
    days = leg.dte - scen.decay_days if at_target else leg.dte
    return max(days, 0.0) / DAY_COUNT


def _leg_iv(leg: Leg, scen: Scenario, at_target: bool) -> float:
    iv = scen.iv_override if scen.iv_override is not None else leg.iv
    return max(iv + (scen.iv_shift if at_target else 0.0), 0.0)


def _leg_q(leg: Leg, scen: Scenario) -> float:
    return scen.q if leg.q is None else leg.q


def leg_curve(leg: Leg, S, scen: Scenario, *, at_target: bool) -> np.ndarray:
    """Price PER CONTRACT of ``leg`` over the prices ``S``, today or on the target date.
    Multiply by ``leg.qty`` for the leg's contribution."""
    return bs_price(S, leg.strike, _leg_T(leg, scen, at_target), _leg_iv(leg, scen, at_target),
                    leg.right, scen.r, _leg_q(leg, scen))


def leg_delta_curve(leg: Leg, S, scen: Scenario, *, at_target: bool) -> np.ndarray:
    return bs_delta(S, leg.strike, _leg_T(leg, scen, at_target), _leg_iv(leg, scen, at_target),
                    leg.right, scen.r, _leg_q(leg, scen))


def net_value(legs, S, scen: Scenario, *, at_target: bool) -> np.ndarray:
    """The position's value (Σ price × qty) over the prices ``S``."""
    S = np.asarray(S, dtype=float)
    out = np.zeros_like(S)
    for leg in legs:
        if leg.qty:
            out = out + leg.qty * leg_curve(leg, S, scen, at_target=at_target)
    return out


def price_grid(legs, scen: Scenario, n: int = GRID_POINTS) -> np.ndarray:
    """A dense price grid covering spot, target, worst and every active strike, widened
    on each side by one expected-move unit (spot × mean IV × √T of the longest leg) and
    never less than ``GRID_MIN_BUFFER`` of spot. The anchor prices themselves are on the
    grid so the curves pass exactly through them."""
    active = [l for l in legs if l.qty]
    pts = [scen.spot, scen.target, scen.worst] + [l.strike for l in active]
    lo, hi = min(pts), max(pts)
    dte_max = max((l.dte for l in active), default=0.0)
    ivs = [_leg_iv(l, scen, False) for l in active]
    iv_ref = (sum(ivs) / len(ivs)) if ivs else 0.0
    buf = max(scen.spot * iv_ref * math.sqrt(dte_max / DAY_COUNT), scen.spot * GRID_MIN_BUFFER)
    lo, hi = max(lo - buf, 0.01), hi + buf
    grid = np.linspace(lo, hi, n)
    return np.unique(np.concatenate([grid, np.array(pts, dtype=float)]))


def _breakevens(legs, scen: Scenario, grid: np.ndarray, pnl: np.ndarray) -> list[float]:
    """Every price on the target date where the P/L crosses zero: sign changes on the
    grid, each refined by bisection on the exact curve."""
    entry = float(net_value(legs, scen.spot, scen, at_target=False))

    def f(x: float) -> float:
        return float(net_value(legs, x, scen, at_target=True)) - entry

    roots: list[float] = []
    for i in range(len(grid) - 1):
        a, b, fa, fb = float(grid[i]), float(grid[i + 1]), float(pnl[i]), float(pnl[i + 1])
        if fa == 0.0:
            roots.append(a)
            continue
        if fa * fb >= 0.0:
            continue
        for _ in range(ROOT_ITERS):
            m = 0.5 * (a + b)
            fm = f(m)
            if fm == 0.0:
                a = b = m
                break
            if (fa < 0) == (fm < 0):
                a, fa = m, fm
            else:
                b, fb = m, fm
        roots.append(0.5 * (a + b))
    if len(grid) and float(pnl[-1]) == 0.0:
        roots.append(float(grid[-1]))
    return [round(x, 6) for x in roots]


# ────────────────────────────────── results ──────────────────────────────────

@dataclass
class LegResult:
    right: str
    strike: float
    qty: int
    iv: float
    dte_entry: float
    dte_target: float
    d1: float                 # sheet row 15 (entry)
    d2: float                 # sheet row 16 (entry)
    price_entry: float        # sheet row 18, per contract
    delta_entry: float        # sheet row 19
    price_target: float       # sheet row 18, target-date columns
    delta_target: float
    value_entry: float        # price × qty (sheet row 23)
    value_target: float
    decay: float              # value_target - value_entry (sheet row 24 "Total Theta")


@dataclass
class Result:
    legs: list[LegResult]
    entry_value: float            # C10  net cost today (+ debit, - credit)
    decayed_value: float          # C14  value at spot on the target date
    target_value: float           # C11
    worst_value: float            # C12
    gain_at_target: float         # D11
    loss_at_worst: float          # D12
    reward_over_risk: float | None   # C25  -gain/loss; None when the worst case is not a loss
    roi: float | None             # E11  gain / entry_value; None for a zero-cost position
    roi_on_max_loss: float | None # gain / |max_loss| - the sensible denominator for a credit
    total_decay: float            # C14 - C10
    net_delta_entry: float
    net_delta_target: float       # Q25
    breakevens: list[float]       # on the target date, every crossing
    max_profit: float             # best P/L on the grid, target date
    max_loss: float               # worst P/L on the grid, target date (<= 0)
    total_gains: float            # C29  gain × multiplier × sets
    total_risk: float             # C30
    total_outlay: float           # C31
    grid: list[float] = field(default_factory=list)
    leg_curves_target: list[list[float]] = field(default_factory=list)   # per leg, value × qty
    net_curve_target: list[float] = field(default_factory=list)          # sheet column Q
    net_curve_entry: list[float] = field(default_factory=list)           # sheet column L (entry)

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(legs, scen: Scenario, *, grid: np.ndarray | None = None,
             with_curves: bool = True) -> Result:
    """The sheet's decision indicators for ``legs`` under ``scen``.

    ``legs`` may hold up to :data:`MAX_LEGS` :class:`Leg` (or dicts with the same
    keys). ``grid`` reuses a precomputed price grid; ``with_curves=False`` leaves the
    curve lists empty (the pairing loop keeps only the figures)."""
    legs = [l if isinstance(l, Leg) else Leg(**l) for l in legs]
    if not legs or len(legs) > MAX_LEGS:
        raise ValueError(f"1 to {MAX_LEGS} legs")
    S0 = scen.spot
    per: list[LegResult] = []
    for leg in legs:
        pe = float(leg_curve(leg, S0, scen, at_target=False))
        pt = float(leg_curve(leg, S0, scen, at_target=True))
        d1, d2 = d1_d2(S0, leg.strike, _leg_T(leg, scen, False), _leg_iv(leg, scen, False),
                       scen.r, _leg_q(leg, scen))
        per.append(LegResult(
            right=leg.right, strike=leg.strike, qty=leg.qty, iv=_leg_iv(leg, scen, False),
            dte_entry=leg.dte, dte_target=max(leg.dte - scen.decay_days, 0.0), d1=d1, d2=d2,
            price_entry=pe, delta_entry=float(leg_delta_curve(leg, S0, scen, at_target=False)),
            price_target=pt, delta_target=float(leg_delta_curve(leg, S0, scen, at_target=True)),
            value_entry=pe * leg.qty, value_target=pt * leg.qty, decay=(pt - pe) * leg.qty))
    entry = sum(p.value_entry for p in per)
    decayed = sum(p.value_target for p in per)
    target_v = float(net_value(legs, scen.target, scen, at_target=True))
    worst_v = float(net_value(legs, scen.worst, scen, at_target=True))
    gain, loss = target_v - entry, worst_v - entry

    g = price_grid(legs, scen) if grid is None else np.asarray(grid, dtype=float)
    curves = [leg.qty * leg_curve(leg, g, scen, at_target=True) for leg in legs]
    net_t = np.sum(curves, axis=0) if curves else np.zeros_like(g)
    pnl = net_t - entry
    mult = scen.multiplier * scen.sets
    res = Result(
        legs=per, entry_value=entry, decayed_value=decayed, target_value=target_v,
        worst_value=worst_v, gain_at_target=gain, loss_at_worst=loss,
        reward_over_risk=(-gain / loss) if loss < 0 else None,
        roi=(gain / entry) if entry else None,
        roi_on_max_loss=None, total_decay=decayed - entry,
        net_delta_entry=sum(p.delta_entry * p.qty for p in per),
        net_delta_target=sum(p.delta_target * p.qty for p in per),
        breakevens=_breakevens(legs, scen, g, pnl),
        max_profit=float(np.max(pnl)), max_loss=float(min(np.min(pnl), 0.0)),
        total_gains=gain * mult, total_risk=loss * mult, total_outlay=entry * mult)
    if res.max_loss < 0:
        res.roi_on_max_loss = gain / abs(res.max_loss)
    if with_curves:
        res.grid = g.tolist()
        res.leg_curves_target = [c.tolist() for c in curves]
        res.net_curve_target = net_t.tolist()
        res.net_curve_entry = net_value(legs, g, scen, at_target=False).tolist()
    return res
