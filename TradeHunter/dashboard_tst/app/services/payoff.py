"""Risk / reward (payoff) engine - generic option legs -> the two P&L curves,
breakevens, max profit / max loss, the model probability, both stops, R and the
SVG the card draws (OPTIONS_MODULE_DESIGN.md II.2.9, design/options/part_C_chart_engines.md C3).

ONE code path for all ten strategies (and for arbitrary legs): the strategy key
only selects the labels, the analytic max-P/L shortcut and the POP flavour,
through its engine family (``option_prefs.family_of``). Pure stdlib + the
platform's ``black_scholes`` - no DB, no network, no FastAPI import - so every
number here can be checked against a synthetic chain with TWS off.

The two curves
--------------
* **at expiry** - the FRONT expiry (``horizon``): a leg expiring there is worth
  its intrinsic value, piecewise linear between strikes, and because the grid
  holds every strike the polyline through the grid IS the function; a leg that
  expires later (calendar, diagonal) is Black-Scholes-valued with its remaining
  life, so a two-expiry structure's "expiry" line is a smooth estimate.
* **today** - T+0 at t = 0 (not tomorrow), ``RISK_FREE`` 0.04, q = 0, with the
  sigma per leg from ``calibrate``: solved from the dealt price first (so a fresh
  idea's today line passes through P&L 0 at spot), then the chain's ``iv``, then
  a sibling leg's sigma, then the caller's ``sigma_fallback`` (iv30 / 100, then
  HV20 / 100). For an open position (``pl_now`` given) the leg's ``iv`` is the
  sigma the route solved from TODAY's mids, so it is preferred over solving the
  entry price.

Units: every leg ``iv`` is a FRACTION by the time it reaches this module
(II.2.12); ``normalise_iv(v, unit=)`` converts a percent feed and never guesses
from the magnitude (a deep-ITM Cboe row prints 3.1099 and is kept). ``iv_bump``
is a RELATIVE lift of a leg's sigma - ``sigma_used = iv x (1 + iv_bump)`` -
defined ONCE in ``leg_value``; ``STOP_IV_BUMP`` 0.10 reads a 0.46 leg at 0.506,
never 0.56. Every $ figure is per ONE contract (``MULT`` 100); ``max_loss`` and
``max_profit`` are POSITIVE magnitudes (the max-loss hline's ``y`` is the only
negative spelling of the loss). Member strings are sentence case and plain
("about -$121 today", never a greek name on its own).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import math
import uuid
from dataclasses import dataclass

from .black_scholes import black_scholes, norm_cdf
from .opt_constants import LOSS_STOP_FRACTION, MULT, RISK_FREE, STOP_IV_BUMP  # noqa: F401  (STOP_IV_BUMP re-exported for callers)

__all__ = [
    "Leg", "NoSigma", "normalise_iv", "implied_vol", "calibrate", "horizon", "leg_value", "pnl",
    "grid", "expiry_curve", "curve_at", "breakevens", "extremes", "pop", "price_at_pnl",
    "svg_paths", "build", "MULT", "RISK_FREE", "STOP_IV_BUMP", "PAD_ATR", "CAPTION", "CAPTION_TWO_EXPIRY",
]

PAD_ATR = 2.0           # the view pads 2 ATR beyond the furthest strike / marker (ticker-relative, never a percent)
GRID_POINTS = 201       # evenly spaced points; strikes, marker xs and breakevens are added so the kinks are exact
SCAN_ATR = 8.0          # the breakeven / rule-stop search looks this far beyond the strikes (or half the price, whichever is wider)
SCAN_POINTS = 400       # intervals of that search; every root is then refined by bisection, so the step only has to bracket it
IV_LO, IV_HI = 0.01, 5.0    # the sigma bracket implied_vol bisects (the same bounds the data layer's sanity check uses)
IV_ITERS = 60           # 60 halvings of [0.01, 5] -> 4e-18: far past the cent
BE_ITERS = 40           # bisection steps that refine a breakeven on a smooth (two-expiry) curve
MARKER_ROWS = 4         # label rows at the top of the plot (y 18 / 30 / 42 / 54): a label takes the first row it fits in
LABEL_CHAR_PX = 5.3     # the width of one character of the 10 px label font, for that fit
IV_UNITS = ("fraction", "percent")

# Families whose P&L is a credit kept: POP label "chance of keeping it", rule stop a
# fraction of max loss, take-profit hline at half the credit (strategy_rules.CREDIT_FAMILIES
# holds the same three strategies by KEY; this is the same set by FAMILY).
CREDIT_FAMILY_NAMES = frozenset({"credit_vertical", "condor"})
FAMILIES = ("credit_vertical", "debit_vertical", "long", "leaps", "condor", "time")

CAPTION = ("Dashed line: what the trade would be worth if the stock moved there today, at today's "
           "implied volatility - an estimate. Solid line: at expiry ({dte} days).")
CAPTION_TWO_EXPIRY = ("Drawn at the near expiry ({front}); the far option is valued by the model, so the "
                      "solid line is an estimate too.")
WARN_NO_SIGMA = "today line needs a quote"
WARN_MAX_LOSS = "max loss recomputed numerically"
WARN_NO_ATR = "ATR missing: the view is padded 5% of spot"
WARN_R_ZERO = "R undefined: the stop does not lose money"
WARN_FAR_INTRINSIC = "far option valued at intrinsic (no quote)"
ERR_EXPIRED = "expired"
ERR_NO_EDGE = "no edge: the spread pays nothing"

# SVG geometry (C4.2): viewBox 0 0 640 300, margins t 8 / r 12 / b 36 / l 48.
SVG_W, SVG_H = 640, 300
PLOT_X0, PLOT_W = 48, 580
PLOT_Y0, PLOT_H = 8, 256


class NoSigma(ValueError):
    """A leg with time left has no sigma to value it with (no quote, no chain iv,
    no fallback) - the today line cannot be drawn (C3.12)."""


# ------------------------------------------------------------- legs
@dataclass(frozen=True)
class Leg:
    """One contract of the structure. ``qty`` is the INTERNAL signed quantity:
    +long / -short, so a bull put 330/320 x1 is [P330 qty -1, P320 qty +1].
    ``price`` is the per-share price the leg was (or would be) dealt at: the mid
    for an idea, the fill for a position. ``iv`` is a FRACTION (0.2797) and, once
    ``calibrate`` has run, the sigma the curves use; ``iv_source`` says where it
    came from (``solved`` / ``leg`` / ``sibling`` / ``fallback`` / ``none``).
    ``delta`` is the chain's signed figure, used only for the delta-POP."""

    right: str
    strike: float
    expiry: str
    qty: int
    price: float
    iv: float | None = None
    delta: float | None = None
    iv_source: str = "none"

    @classmethod
    def from_dict(cls, leg: dict, *, price: float | None = None) -> "Leg":
        """From the stored / API leg shape - the ONE leg shape every part uses
        (II.2.8): ``{expiry, right in {C, P}, strike, side in {sell, buy}, qty:
        positive int, price (mid), bid, ask, iv (FRACTION), delta (signed), oi,
        volume}``. ``qty = +qty`` for ``side == "buy"``, ``-qty`` for ``"sell"``.
        ``price=`` overrides the dict's mid (the Positions tab passes
        ``entry_price``). The key is ``oi`` (never ``open_interest``) once past
        ``opt_legs.norm_leg``, so a raw row is refused here; the iv is a fraction
        because the source normalised it - this method never rescales."""
        if not isinstance(leg, dict):
            raise ValueError("leg must be a dict in the stored / API shape")
        if "open_interest" in leg:
            raise ValueError("leg carries open_interest: pass it through opt_legs.norm_leg first (the key is oi)")
        right = str(leg.get("right") or "").strip().upper()[:1]
        if right not in ("C", "P"):
            raise ValueError(f"leg right must be C or P, not {leg.get('right')!r}")
        side = str(leg.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"leg side must be buy or sell, not {leg.get('side')!r}")
        try:
            qty = int(leg.get("qty") if leg.get("qty") is not None else 1)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"leg qty must be a positive int, not {leg.get('qty')!r}") from exc
        if qty <= 0:
            raise ValueError(f"leg qty must be a positive int, not {qty!r}")
        try:
            strike = float(leg["strike"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("leg strike missing") from exc
        if not strike > 0:
            raise ValueError(f"leg strike must be positive, not {strike!r}")
        expiry = str(leg.get("expiry") or "")
        _date(expiry)                                      # ValueError when not YYYY-MM-DD
        p = price if price is not None else leg.get("price")
        if p is None:
            raise ValueError("leg has no price (no two-sided quote)")
        p = float(p)
        if p < 0:
            raise ValueError(f"leg price must not be negative, not {p!r}")
        iv = _num(leg.get("iv"))
        iv = iv if (iv is not None and iv > 0) else None
        delta = _num(leg.get("delta"))
        return cls(right=right, strike=strike, expiry=expiry, qty=qty if side == "buy" else -qty,
                   price=p, iv=iv, delta=delta, iv_source="leg" if iv is not None else "none")

    @property
    def side(self) -> str:
        return "buy" if self.qty > 0 else "sell"

    @property
    def kind(self) -> str:
        return "call" if self.right == "C" else "put"

    def intrinsic(self, S: float) -> float:
        return max(S - self.strike, 0.0) if self.right == "C" else max(self.strike - S, 0.0)

    def as_dict(self) -> dict:
        """The API leg shape (positive qty + side) plus ``iv_source`` - what the
        payoff dict's ``legs`` carries."""
        return {"expiry": self.expiry, "right": self.right, "strike": self.strike, "side": self.side,
                "qty": abs(self.qty), "price": self.price, "iv": self.iv, "delta": self.delta,
                "iv_source": self.iv_source}


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _date(v) -> _dt.date:
    """A date from an ISO string / date / datetime; ValueError otherwise."""
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a YYYY-MM-DD date: {v!r}") from exc


def _as_of(as_of) -> _dt.date:
    return _dt.date.today() if as_of is None else _date(as_of)


def _legs(legs) -> list[Leg]:
    out = []
    for leg in legs or ():
        out.append(leg if isinstance(leg, Leg) else Leg.from_dict(leg))
    if not out:
        raise ValueError("no legs")
    return out


def dte(leg: Leg, as_of=None) -> int:
    """Calendar days from ``as_of`` to the leg's expiry (negative = past)."""
    return (_date(leg.expiry) - _as_of(as_of)).days


# ------------------------------------------------------------- units / sigma
def normalise_iv(v, *, unit: str) -> float | None:
    """A per-contract IV in the unit the chain's SOURCE speaks -> a fraction.
    ``unit`` is REQUIRED and one of ``fraction`` / ``percent``: Cboe, Alpaca and
    every ``ContractRow`` are fractions (a deep-ITM row legitimately prints
    3.1099 and is NOT rescaled); only the legacy raw bridge payload is percent.
    There is no magnitude heuristic. ``<= 0`` / None -> None."""
    if unit not in IV_UNITS:
        raise ValueError(f"normalise_iv: unit must be one of {IV_UNITS}, not {unit!r}")
    f = _num(v)
    if f is None or f <= 0:
        return None
    return f / 100.0 if unit == "percent" else f


def implied_vol(price: float, S: float, K: float, T: float, kind: str) -> float | None:
    """The sigma that prices ``price``: bisection on [0.01, 5.0], 60 iterations.
    None when the price sits at or under the model's floor (a stale quote at or
    below intrinsic) or above its ceiling, or when there is no time left."""
    p = _num(price)
    if p is None or p <= 0 or not (S > 0 and K > 0) or T is None or T <= 0:
        return None
    kind = "call" if str(kind).lower().startswith("c") else "put"
    try:
        lo_p = black_scholes(S, K, T, RISK_FREE, IV_LO, kind).price
        hi_p = black_scholes(S, K, T, RISK_FREE, IV_HI, kind).price
    except ValueError:
        return None
    if p <= lo_p or p >= hi_p:
        return None
    lo, hi = IV_LO, IV_HI
    for _ in range(IV_ITERS):
        mid = 0.5 * (lo + hi)
        if black_scholes(S, K, T, RISK_FREE, mid, kind).price > p:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def calibrate(legs, spot: float, as_of=None, *, sigma_fallback: float | None = None,
              prefer_leg_iv: bool = False) -> list[Leg]:
    """Give every leg the sigma its curves use, in this order: solved from the
    dealt ``price`` -> the leg's own ``iv`` (a fraction) -> a sibling leg's sigma
    (same expiry first) -> ``sigma_fallback`` (the chain's iv30 / 100, else HV20 /
    100, chosen by the caller). ``prefer_leg_iv`` (an open position: the leg's iv
    is today's solved sigma, its price the entry fill) puts the leg's iv first.
    A leg with no time left keeps whatever it has - it is intrinsic anyway."""
    legs = _legs(legs)
    today = _as_of(as_of)
    got: list[tuple[float | None, str]] = []
    for leg in legs:
        T = dte(leg, today) / 365.0
        sigma, src = None, "none"
        if T > 0:
            if prefer_leg_iv and leg.iv:
                sigma, src = leg.iv, "leg"
            else:
                sigma = implied_vol(leg.price, spot, leg.strike, T, leg.kind)
                if sigma:
                    src = "solved"
                elif leg.iv:
                    sigma, src = leg.iv, "leg"
        elif leg.iv:
            sigma, src = leg.iv, "leg"
        got.append((sigma, src))
    out = []
    for i, leg in enumerate(legs):
        sigma, src = got[i]
        if sigma is None:
            same = [s for j, (s, _) in enumerate(got) if j != i and s and legs[j].expiry == leg.expiry]
            other = [s for j, (s, _) in enumerate(got) if j != i and s]
            if same or other:
                sigma, src = (same or other)[0], "sibling"
            elif sigma_fallback and sigma_fallback > 0:
                sigma, src = float(sigma_fallback), "fallback"
        out.append(dataclasses.replace(leg, iv=sigma, iv_source=src))
    return out


# ------------------------------------------------------------- valuation
def horizon(legs, as_of=None) -> tuple[str, int]:
    """``(expiry, dte)`` of the FRONT expiry - everything "at expiry" is at this date."""
    legs = _legs(legs)
    front = min(legs, key=lambda l: _date(l.expiry))
    return front.expiry, dte(front, as_of)


def leg_value(leg: Leg, S: float, days_ahead: float = 0, as_of=None, iv_bump: float = 0.0,
              *, strict: bool = True) -> float:
    """The per-share model value of one leg at stock price ``S`` with
    ``days_ahead`` elapsed: ``T = max(dte - days_ahead, 0) / 365``; ``T == 0`` ->
    intrinsic; else Black-Scholes at ``RISK_FREE``, q = 0, with
    ``sigma_used = leg.iv x (1 + iv_bump)`` - THE one definition of the lift
    (``STOP_IV_BUMP`` 0.10 reads 0.46 as 0.506). ``strict=False`` values a leg
    without a sigma at intrinsic instead of raising ``NoSigma``."""
    T = max(dte(leg, as_of) - days_ahead, 0) / 365.0
    if T <= 0 or S <= 0:
        return leg.intrinsic(max(S, 0.0))
    if not leg.iv or leg.iv <= 0:
        if strict:
            raise NoSigma(f"no sigma for {leg.expiry} {leg.strike}{leg.right}")
        return leg.intrinsic(S)
    sigma = leg.iv * (1.0 + float(iv_bump))
    return black_scholes(S, leg.strike, T, RISK_FREE, sigma, leg.kind).price


def pnl(legs, S: float, days_ahead: float = 0, as_of=None, iv_bump: float = 0.0,
        *, strict: bool = True) -> float:
    """``sum(qty x (leg_value - price) x MULT)`` - per one contract of the structure."""
    return sum(leg.qty * (leg_value(leg, S, days_ahead, as_of, iv_bump, strict=strict) - leg.price) * MULT
               for leg in _legs(legs))


def grid(legs, spot: float, atr: float | None, marker_xs=(), *, breakevens=()) -> list[float]:
    """201 evenly spaced prices over ``[min(strikes, spot, markers) - 2 ATR, max(...)
    + 2 ATR]`` plus every strike, marker x and breakeven, sorted and de-duplicated
    at 4 dp - so the kinks are exact grid points. No ATR -> 5 % of spot."""
    legs = _legs(legs)
    pts = [l.strike for l in legs] + [float(spot)] + [float(x) for x in marker_xs if _num(x) is not None]
    a = _num(atr)
    pad = PAD_ATR * a if (a and a > 0) else 0.05 * float(spot)
    lo, hi = max(min(pts) - pad, 0.01), max(pts) + pad
    xs = {round(lo + i * (hi - lo) / (GRID_POINTS - 1), 4) for i in range(GRID_POINTS)}
    xs.update(round(p, 4) for p in pts)
    xs.update(round(float(b), 4) for b in breakevens if _num(b) is not None and lo <= float(b) <= hi)
    return sorted(xs)


def expiry_curve(legs, xs, as_of=None) -> list[float]:
    """P&L at the horizon (front) expiry at every grid point: front legs are
    intrinsic (exact), later legs are model-valued with their own sigma (a leg
    without one is intrinsic - the caller warns)."""
    legs = _legs(legs)
    _, days = horizon(legs, as_of)
    return [pnl(legs, x, days, as_of, strict=False) for x in xs]


def curve_at(legs, xs, days_ahead: float = 0, as_of=None, iv_bump: float = 0.0) -> list[float] | None:
    """The T+n curve (``days_ahead = 0`` is today, at t = 0) or None when a leg
    with time left has no sigma (C3.12: "today line needs a quote")."""
    legs = _legs(legs)
    try:
        return [pnl(legs, x, days_ahead, as_of, iv_bump) for x in xs]
    except NoSigma:
        return None


def _crossings(xs, ys, target: float, f=None) -> list[float]:
    """The prices where the polyline ``(xs, ys)`` crosses ``target``: linear
    interpolation per interval, refined by ``BE_ITERS`` bisection steps of ``f``
    (the continuous function) when one is given. Sorted, de-duplicated."""
    out: list[float] = []
    eps = 1e-9
    n = len(xs)
    for i in range(n):
        a = ys[i] - target
        if abs(a) <= eps:
            # an exact hit counts once: a run of grid points sitting ON the target (a
            # leg dealt at 0 makes a whole ray read 0) yields its edges, not every point
            prev_off = i > 0 and abs(ys[i - 1] - target) > eps
            next_off = i + 1 < n and abs(ys[i + 1] - target) > eps
            if prev_off or next_off or n == 1:
                out.append(xs[i])
            continue
        if i == 0:
            continue
        b = ys[i - 1] - target
        if abs(b) <= eps or a * b > 0:
            continue
        x0, x1 = xs[i - 1], xs[i]
        x = x0 + (x1 - x0) * (-b) / (a - b)
        if f is not None:
            lo, hi, flo = x0, x1, b
            for _ in range(BE_ITERS):
                mid = 0.5 * (lo + hi)
                fm = f(mid) - target
                if abs(fm) <= eps:
                    lo = hi = mid
                    break
                if (fm < 0) == (flo < 0):
                    lo, flo = mid, fm
                else:
                    hi = mid
            x = 0.5 * (lo + hi)
        out.append(x)
    res: list[float] = []
    for x in sorted(round(v, 6) for v in out):
        if not res or abs(x - res[-1]) > 1e-4:
            res.append(x)
    return res


def breakevens(xs, ys, legs=None, as_of=None) -> list[float]:
    """Every price where the expiry curve crosses zero - ALWAYS a list (``[]``
    when none). Exact for a single-expiry structure (the kinks are grid points);
    a smooth two-expiry curve is refined by bisection on ``pnl`` at the horizon."""
    if not xs:
        return []
    f = None
    if legs:
        legs = _legs(legs)
        front, days = horizon(legs, as_of)
        if any(l.expiry != front for l in legs):
            f = lambda S: pnl(legs, S, days, as_of, strict=False)   # noqa: E731
    return _crossings(list(xs), list(ys), 0.0, f)


def _scan_range(legs: list[Leg], spot: float, atr: float | None, marker_xs=()) -> tuple[float, float]:
    """The bracket the breakeven / rule-stop search covers: ``SCAN_ATR`` beyond the
    strikes, spot and markers, or half the price when the ATR is small or missing -
    far wider than the view, so a root the 2-ATR grid would miss (a calendar's
    breakevens, a long call's premium stop) is still found and then framed."""
    pts = [l.strike for l in legs] + [float(spot)] + [float(x) for x in marker_xs if _num(x) is not None]
    a = _num(atr)
    reach = SCAN_ATR * a if (a and a > 0) else 0.0
    lo = max(min(pts) - max(reach, 0.5 * min(pts)), 0.01)
    hi = max(pts) + max(reach, 0.5 * max(pts))
    return lo, hi


def _find_crossings(legs: list[Leg], target: float, days_ahead: float, lo: float, hi: float, as_of=None,
                    iv_bump: float = 0.0, *, strict: bool = False) -> list[float]:
    """The prices in ``[lo, hi]`` where the T+n curve equals ``target`` dollars:
    a ``SCAN_POINTS`` sweep (every strike inserted, so a kink is exact) refined by
    bisection. ``strict`` = a leg without a sigma makes it ``[]`` (the today
    curve) instead of valuing it at intrinsic (the expiry curve)."""
    if not (hi > lo):
        return []
    pts = {round(lo + (hi - lo) * i / SCAN_POINTS, 6) for i in range(SCAN_POINTS + 1)}
    pts.update(round(l.strike, 6) for l in legs if lo <= l.strike <= hi)
    xs = sorted(pts)

    def f(S: float) -> float:
        return pnl(legs, S, days_ahead, as_of, iv_bump, strict=strict)

    try:
        ys = [f(x) for x in xs]
    except NoSigma:
        return []
    return _crossings(xs, ys, target, f)


def _structure(legs: list[Leg]) -> dict:
    net = sum(l.qty * l.price for l in legs)             # per share; negative = credit
    shorts = [l for l in legs if l.qty < 0]
    longs = [l for l in legs if l.qty > 0]
    n = max([abs(l.qty) for l in legs] or [1])
    return {"net": net, "credit": -net, "debit": net, "shorts": shorts, "longs": longs, "n": n,
            "calls": [l for l in legs if l.right == "C"], "puts": [l for l in legs if l.right == "P"]}


def extremes(family: str | None, legs, xs, ys) -> dict:
    """``max_profit`` / ``max_loss`` as POSITIVE $ per contract, with
    ``unlimited_profit`` / ``unlimited_loss``: analytic per family (a credit
    vertical's ``(width - credit) x 100`` equals ``bull_put.spread_math`` to the
    cent), numeric for the time family's profit and for arbitrary legs. The
    analytic figure is cross-checked against the grid where the extreme lies on
    it; a disagreement over $0.01 lets the numeric win and adds a warning."""
    legs = _legs(legs)
    st = _structure(legs)
    ys = list(ys)
    num_max, num_min = (max(ys), min(ys)) if ys else (None, None)
    warnings: list[str] = []
    res = {"max_profit": None, "max_loss": None, "unlimited_profit": False, "unlimited_loss": False,
           "modelled": False, "max_profit_x": None, "warnings": warnings}
    n, net = st["n"], st["net"]
    front = min(_date(l.expiry) for l in legs)
    single_expiry = all(_date(l.expiry) == front for l in legs)
    # the right ray's slope (per $ of stock) tells whether a profit or a loss is unbounded
    ray = MULT * sum(l.qty for l in st["calls"]) if single_expiry else (
        (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) if len(xs) > 1 else 0.0)
    unl_profit, unl_loss = ray > 1e-9, ray < -1e-9

    def vertical(short, long_):
        return abs(short.strike - long_.strike)

    analytic = None
    try:
        if family == "credit_vertical" and len(st["shorts"]) == 1 and len(st["longs"]) == 1:
            credit = st["credit"]
            w = vertical(st["shorts"][0], st["longs"][0]) * n
            analytic = (credit * MULT, (w - credit) * MULT, True, True)
        elif family == "debit_vertical" and len(st["shorts"]) == 1 and len(st["longs"]) == 1:
            debit = st["debit"]
            w = vertical(st["shorts"][0], st["longs"][0]) * n
            analytic = ((w - debit) * MULT, debit * MULT, True, True)
        elif family in ("long", "leaps") and len(legs) == 1 and legs[0].qty > 0:
            debit = st["debit"]
            if legs[0].right == "C":
                analytic = (None, debit * MULT, False, True)
            else:
                analytic = ((legs[0].strike * n - debit) * MULT, debit * MULT, False, True)
        elif family == "condor" and len(st["puts"]) == 2 and len(st["calls"]) == 2 \
                and len(st["shorts"]) == 2 and len(st["longs"]) == 2:
            credit = st["credit"]
            wp = vertical(*st["puts"]) * n
            wc = vertical(*st["calls"]) * n
            analytic = (credit * MULT, (max(wp, wc) - credit) * MULT, True, True)
        elif family == "time" and single_expiry is False:
            res["modelled"] = True
            analytic = (num_max, st["debit"] * MULT, False, False)
    except (TypeError, ValueError):
        analytic = None

    if analytic is not None:
        mp, ml, check_p, check_l = analytic
        if check_p and mp is not None and num_max is not None and abs(mp - num_max) > 0.01:
            warnings.append("max profit recomputed numerically")
            mp = num_max
        if check_l and ml is not None and num_min is not None and abs(ml + num_min) > 0.01:
            warnings.append(WARN_MAX_LOSS)
            ml = -num_min
        res["max_profit"] = None if (mp is None or (family in ("long", "leaps") and unl_profit)) else round(mp, 2)
        res["max_loss"] = None if ml is None else round(abs(ml), 2)
        res["unlimited_profit"] = mp is None and family in ("long", "leaps")
    else:
        # arbitrary legs (or a family whose legs do not match its shape): numeric,
        # including the S -> 0 end (intrinsic) the grid never reaches
        at_zero = sum(l.qty * (l.intrinsic(0.0) - l.price) * MULT for l in legs)
        hi_v = max([v for v in (num_max, at_zero) if v is not None] or [0.0])
        lo_v = min([v for v in (num_min, at_zero) if v is not None] or [0.0])
        res["max_profit"] = None if unl_profit else round(hi_v, 2)
        res["max_loss"] = None if unl_loss else round(max(-lo_v, 0.0), 2)
        res["unlimited_profit"], res["unlimited_loss"] = unl_profit, unl_loss
        if family is not None and family in FAMILIES:
            warnings.append("legs do not match the strategy's shape: max profit / loss read from the grid")
    if ys and res["max_profit"] is not None:
        i = max(range(len(ys)), key=lambda k: ys[k])
        res["max_profit_x"] = round(xs[i], 2)
    return res


def pop(family: str | None, legs, spot: float, sigma_h: float | None, T_h: float | None, xs, ys) -> float | None:
    """THE probability-of-profit function of the module (II.2.8): the risk-neutral
    lognormal mass ``P(S_T in the profit region)`` at the horizon, summed over
    every grid interval of ``(xs, ys)`` that is profitable (closed at the
    breakevens, which are grid points) plus the two tails. ``sigma_h`` = the ATM
    sigma of the horizon expiry, ``T_h`` = horizon DTE / 365. None when there is
    no sigma or no time. A pricing convention, not a forecast (black_scholes.py)."""
    s, t = _num(sigma_h), _num(T_h)
    if not s or s <= 0 or not t or t <= 0 or not xs or not ys or not spot or spot <= 0:
        return None
    mu = (RISK_FREE - 0.5 * s * s) * t
    sd = s * math.sqrt(t)

    def F(x: float) -> float:
        return norm_cdf((math.log(x / spot) - mu) / sd) if x > 0 else 0.0

    eps = 1e-9
    prob = 0.0
    for i in range(1, len(xs)):
        a, b = ys[i - 1], ys[i]
        if a >= -eps and b >= -eps and (a > eps or b > eps):
            prob += F(xs[i]) - F(xs[i - 1])
    if ys[0] > eps:
        prob += F(xs[0])
    if ys[-1] > eps:
        prob += 1.0 - F(xs[-1])
    return min(max(prob, 0.0), 1.0)


def price_at_pnl(legs, target: float, lo: float, hi: float, days_ahead: float = 0, as_of=None,
                 iv_bump: float = 0.0) -> float | None:
    """The stock price in ``[lo, hi]`` at which the T+n curve shows ``target``
    dollars (the rule stop's price). Bisection on the first bracket found
    scanning up from ``lo``; None when the curve never reaches the target there
    or a leg has no sigma."""
    legs = _legs(legs)

    def f(S: float) -> float:
        return pnl(legs, S, days_ahead, as_of, iv_bump) - target

    try:
        n = 64
        pts = [lo + (hi - lo) * i / n for i in range(n + 1)]
        vals = [f(x) for x in pts]
    except NoSigma:
        return None
    for i in range(n + 1):
        if abs(vals[i]) <= 1e-9:
            return round(pts[i], 4)
        if i and vals[i - 1] * vals[i] < 0:
            a, b, fa = pts[i - 1], pts[i], vals[i - 1]
            for _ in range(IV_ITERS):
                m = 0.5 * (a + b)
                fm = f(m)
                if abs(fm) <= 1e-9:
                    a = b = m
                    break
                if (fm < 0) == (fa < 0):
                    a, fa = m, fm
                else:
                    b = m
            return round(0.5 * (a + b), 4)
    return None


# ------------------------------------------------------------- labels
def _money(v: float) -> str:
    """-158 -> "-$158", 1715.4 -> "+$1,715", 0 -> "$0" (sentence-plain, ASCII minus)."""
    r = int(round(v))
    if r == 0:
        return "$0"
    return ("-" if r < 0 else "+") + "$" + f"{abs(r):,}"


def _money_abs(v: float) -> str:
    return "$" + f"{abs(int(round(v))):,}"


def _rfmt(v: float) -> str:
    return f"{v:+.2f} R"


def _px(v: float, nd: int = 2) -> str:
    """A price with trailing zeros trimmed: 336.20 -> "336.2", 340.00 -> "340"."""
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return s or "0"


def _expiry_label(expiry: str) -> str:
    """'2026-11-20' -> 'Nov 20' (strategy_rules.expiry_label's spelling)."""
    try:
        d = _date(expiry)
    except ValueError:
        return str(expiry)
    return f"{d.strftime('%b')} {d.day}"


def _label(legs: list[Leg]) -> str:
    """'Nov 20 330/320 put', 'Dec 18 400 call', 'Nov 20 300/310 put · 380/390 call',
    'Oct 30 100C / Dec 4 100C' (two expiries)."""
    expiries = {l.expiry for l in legs}
    if len(expiries) > 1:
        return " / ".join(f"{_expiry_label(l.expiry)} {_px(l.strike)}{l.right}" for l in legs)
    exp = _expiry_label(legs[0].expiry)
    parts = []
    for right, word in (("P", "put"), ("C", "call")):
        ks = [_px(l.strike) for l in legs if l.right == right]
        if ks:
            parts.append(f"{'/'.join(ks)} {word}")
    return f"{exp} {' · '.join(parts)}"


def _strategy_label(strategy: str | None) -> str | None:
    if not strategy:
        return None
    try:
        from .strategy_rules import LABELS
        return LABELS.get(strategy)
    except Exception:  # noqa: BLE001  - labels are decoration
        return None


def _family_of(strategy: str | None) -> str | None:
    """The engine family through option_prefs.family_of (II.2.9); None for
    arbitrary legs (``strategy=None``) - never a key stored or shown."""
    if not strategy:
        return None
    from .option_prefs import family_of
    return family_of(strategy)


def _house_premium_stop_pct(strategy: str) -> float | None:
    """The house ``premium_stop_pct`` a debit family's rule stop falls back on when
    the route passes none: the long block's 50, the leaps block's 40 (inherited
    by the diagonal) - read from option_prefs so there is one home for it."""
    try:
        from .option_prefs import HOUSE, for_strategy
        return _num(for_strategy(HOUSE, strategy).get("premium_stop_pct"))
    except Exception:  # noqa: BLE001
        return {"leaps": 40.0}.get(_family_of(strategy) or "", 50.0)


# ------------------------------------------------------------- SVG
def _nice(step: float) -> float:
    """The 1 / 2 / 5 x 10^k value at or above ``step``."""
    if step <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(step))
    for m in (1, 2, 5, 10):
        if m * mag >= step:
            return m * mag
    return 10 * mag


def _nice_step(span: float, target: int) -> float:
    """The 1 / 2 / 2.5 / 5 x 10^k step that cuts ``span`` into about ``target``
    ticks (never more than 1.6 x target)."""
    if span <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(span / target))
    for m in (1, 2, 2.5, 5, 10):
        if span / (m * mag) <= target * 1.6:
            return m * mag
    return 10 * mag


def _ticks(lo: float, hi: float, step: float) -> list[float]:
    if step <= 0 or hi <= lo:
        return []
    start = math.ceil(lo / step) * step
    out = []
    v = start
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


def svg_paths(result: dict) -> dict:
    """The pixel work of the pane, server-side, so the template has no arithmetic
    (C4.2 / C4.3): ``x = 48 + (S - lo) / (hi - lo) x 580``, ``y = 8 + (ymax - v) /
    (ymax - ymin) x 256`` with ``[ymin, ymax]`` the min / max over both curves and
    every hline, padded 6 %, zero always inside. Returns the two path strings,
    the profit / loss zone polygons (split exactly at the breakevens), the hlines,
    the markers with their label row, the axis ticks, the zero line and the
    Positions dot."""
    xs = result.get("xs") or []
    ye = result.get("at_expiry") or []
    yt = result.get("today")
    if not xs or not ye:
        return {}
    lo, hi = xs[0], xs[-1]
    vals = list(ye) + (list(yt) if yt else []) + [h["y"] for h in result.get("hlines") or []] + [0.0]
    for m in result.get("markers") or []:
        for k in ("y_today", "y_expiry"):
            if m.get(k) is not None:
                vals.append(m[k])
    ymin, ymax = min(vals), max(vals)
    if ymax - ymin < 1e-9:
        ymin, ymax = ymin - 1.0, ymax + 1.0
    pad = 0.06 * (ymax - ymin)
    ymin, ymax = ymin - pad, ymax + pad
    xr = (hi - lo) or 1.0
    yr = (ymax - ymin) or 1.0

    def sx(x: float) -> float:
        return PLOT_X0 + (x - lo) / xr * PLOT_W

    def sy(v: float) -> float:
        return PLOT_Y0 + (ymax - v) / yr * PLOT_H

    def path(ys) -> str:
        return "M" + " L".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(xs, ys))

    y0 = sy(0.0)
    eps = 1e-9

    def zones(sign: int) -> list[str]:
        out, i, n = [], 0, len(xs)
        while i < n:
            if sign * ye[i] > eps:
                j = i
                while j + 1 < n and sign * ye[j + 1] > eps:
                    j += 1
                pts = []
                if i > 0 and sign * ye[i - 1] <= eps:
                    a, b = ye[i - 1], ye[i]
                    xb = xs[i - 1] + (xs[i] - xs[i - 1]) * (-a) / (b - a) if b != a else xs[i]
                    pts.append((sx(xb), y0))
                pts += [(sx(xs[k]), sy(ye[k])) for k in range(i, j + 1)]
                if j + 1 < n and sign * ye[j + 1] <= eps:
                    a, b = ye[j], ye[j + 1]
                    xb = xs[j] + (xs[j + 1] - xs[j]) * (-a) / (b - a) if b != a else xs[j]
                    pts.append((sx(xb), y0))
                else:
                    pts.append((sx(xs[j]), y0))
                if not (i > 0 and sign * ye[i - 1] <= eps):
                    pts.append((sx(xs[i]), y0))
                out.append("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z")
                i = j + 1
            else:
                i += 1
        return out

    # marker labels take the first of MARKER_ROWS rows where they fit beside what is
    # already there (estimated at LABEL_CHAR_PX per character), so C5.1's cluster -
    # breakeven, short strike, rule stop, trend line, chart stop, support within
    # 25 points - never overprints; near an edge the label hangs inward
    marks = []
    rows_right = [-1e9] * MARKER_ROWS
    for m in sorted(result.get("markers") or [], key=lambda m: m["x"]):
        x = sx(m["x"])
        w = len(str(m["label"])) * LABEL_CHAR_PX
        if x < PLOT_X0 + 22:
            anchor, left, right, lx = "start", x + 3, x + 3 + w, x + 3
        elif x > PLOT_X0 + PLOT_W - 22:
            anchor, left, right, lx = "end", x - 3 - w, x - 3, x - 3
        else:
            anchor, left, right, lx = "middle", x - w / 2, x + w / 2, x
        row = next((r for r in range(MARKER_ROWS) if left > rows_right[r] + 6), None)
        if row is None:
            row = min(range(MARKER_ROWS), key=lambda r: rows_right[r])
        rows_right[row] = right
        d = {"x_px": round(x, 1), "row": row, "label": m["label"], "kind": m["kind"], "level": m.get("level"),
             "anchor": anchor, "label_x": round(lx, 1), "label_y": PLOT_Y0 + 10 + 12 * row}
        if m.get("y_today") is not None:
            d["y_px"] = round(sy(m["y_today"]), 1)
        marks.append(d)
    boxes = [(m["label_x"] - (len(m["label"]) * LABEL_CHAR_PX if m["anchor"] == "end" else
                              len(m["label"]) * LABEL_CHAR_PX / 2 if m["anchor"] == "middle" else 0),
              m["label_y"] - 9, m["label_y"] + 2, len(m["label"]) * LABEL_CHAR_PX) for m in marks]
    boxes = [(l, l + w, t, b) for (l, t, b, w) in boxes]

    def clashes(l: float, r: float, t: float, b: float) -> bool:
        return any(l < br and r > bl and t < bb and b > bt for (bl, br, bt, bb) in boxes)

    # an hline's label sits right-aligned just above its line; when a marker label is
    # already there (a max-profit line near the top band) it tries the left end,
    # then below the line, so nothing overprints
    right_x, left_x = PLOT_X0 + PLOT_W - 2, PLOT_X0 + 2
    hl = []
    for h in result.get("hlines") or []:
        yp = sy(h["y"])
        w = len(str(h["label"])) * LABEL_CHAR_PX
        above, below = yp - 3, yp + 11
        choices = [("end", right_x, above), ("start", left_x, above), ("end", right_x, below), ("start", left_x, below)]
        pick = None
        for anchor, lx, ly in choices:
            if ly - 9 < PLOT_Y0 or ly > PLOT_Y0 + PLOT_H:
                continue
            l = lx - w if anchor == "end" else lx
            if not clashes(l, l + w, ly - 9, ly + 2):
                pick = (anchor, lx, ly)
                break
        anchor, lx, ly = pick or choices[0]
        l = lx - w if anchor == "end" else lx
        boxes.append((l, l + w, ly - 9, ly + 2))
        hl.append({"y_px": round(yp, 1), "label": h["label"], "kind": h["kind"],
                   "anchor": anchor, "label_x": round(lx, 1), "label_y": round(ly, 1)})
    # x ticks every nice(ATR) dollars ($1 / 2 / 5 / 10 / 25 ...), so a $30 and a $900
    # stock read alike; a view the ATR would over- or under-tick falls back to 1/8th
    a = _num(result.get("atr"))
    xstep = _nice(a) if (a and a > 0) else _nice(xr / 8)
    if not (3.0 <= xr / xstep <= 14.0):
        xstep = _nice(xr / 8)
    x_ticks = [[round(sx(v), 1), _px(v)] for v in _ticks(lo, hi, xstep)]
    mode = (result.get("units") or {}).get("mode", "$")
    ystep = _nice_step(yr, 5)
    y_ticks = []
    for v in _ticks(ymin, ymax, ystep):
        lbl = ("0" if abs(v) < 1e-9 else (_money(v) if mode == "$" else f"{v:+.1f} R"))
        y_ticks.append([round(sy(v), 1), lbl])
    dot = None
    for m in result.get("markers") or []:
        if m.get("kind") == "now" and m.get("y_today") is not None:
            dot = {"x_px": round(sx(m["x"]), 1), "y_px": round(sy(m["y_today"]), 1)}
    return {
        "expiry_path": path(ye), "today_path": path(yt) if yt else None,
        "profit_zones": zones(+1), "loss_zones": zones(-1),
        "hlines": hl, "markers": marks, "x_ticks": x_ticks, "y_ticks": y_ticks,
        "zero_y": round(y0, 1), "dot": dot, "arrow": bool(result.get("unlimited_profit")),
        "lo": lo, "hi": hi, "ymin": round(ymin, 4), "ymax": round(ymax, 4),
    }


# ------------------------------------------------------------- build
def _empty(strategy, family, symbol, spot, atr, as_of, units, legs, error, warnings) -> dict:
    return {"strategy": strategy, "family": family, "label": _label(legs) if legs else None, "symbol": symbol,
            "spot": spot, "atr": atr, "as_of": as_of, "horizon": None,
            "legs": [l.as_dict() for l in legs] if legs else [],
            "xs": [], "at_expiry": [], "today": None, "breakevens": [],
            "max_profit": None, "max_loss": None, "unlimited_profit": False, "unlimited_loss": False,
            "pop": {"label": None, "value": None, "basis": None, "model": None, "model_basis": None},
            "markers": [], "hlines": [], "units": {"mode": "$", "r_dollars": 0.0, "r_basis": None},
            "caption": None, "legend": None, "warnings": list(warnings), "error": error,
            "uid": uuid.uuid4().hex[:8], "svg": None, "series": None}


def build(legs, *, strategy: str | None, spot: float, atr: float | None, as_of,
          chart_stop: float | None = None, target: float | None = None, levels=(),
          sigma_fallback: float | None = None, pl_now: float | None = None,
          premium_stop_pct: float | None = None, loss_fraction: float = LOSS_STOP_FRACTION,
          units: str = "$", symbol: str | None = None) -> dict:
    """The payoff dict of II.2.6 / C3.10 for ONE contract of ``legs`` (Leg objects
    or API leg dicts).

    * ``strategy`` - a catalog key (family via ``option_prefs.family_of``) or None
      for arbitrary legs (generic labels, numeric extremes).
    * ``chart_stop`` / ``target`` - the setup's ``plan.stop`` / ``plan.target``;
      ``levels`` - ``({x, label, kind in {support, resistance, trend_line,
      target}}, ...)`` from the route (a ``target`` level becomes the target
      marker when no ``target`` was passed).
    * ``sigma_fallback`` - iv30 / 100 (else HV20 / 100) for a leg no price or iv
      can size; ``pl_now`` - an open position's P&L (the ``now`` marker's dot;
      the legs' ``iv`` is then preferred over solving the entry price).
    * ``premium_stop_pct`` - the debit families' rule stop (the long block's 50,
      the leaps block's 40 for leaps_call / diagonal_call; the house value when
      None); ``loss_fraction`` - the credit families' (0.20 of max loss).
    * ``units`` - ``"$"`` or ``"R"``: the server renders either (every y divided
      by ``units.r_dollars``), the client never recomputes.

    Never raises for a bad structure: ``error`` carries ``"expired"`` / ``"no
    edge: the spread pays nothing"`` / a one-line reason and the rest of the dict
    is empty, so the pane always renders inside the same height."""
    units = "R" if str(units).upper() == "R" else "$"
    warnings: list[str] = []
    spot = float(spot)
    atr_v = _num(atr)
    try:
        legs_in = _legs(legs)
    except (ValueError, TypeError) as exc:
        return _empty(strategy, None, symbol, spot, atr_v, str(as_of), units, [], f"legs invalid: {exc}", warnings)
    if symbol is None:
        for raw in legs or ():
            if isinstance(raw, dict) and raw.get("symbol"):
                symbol = str(raw["symbol"])
                break
    try:
        family = _family_of(strategy)
    except KeyError:
        return _empty(strategy, None, symbol, spot, atr_v, str(as_of), units, legs_in,
                      f"unknown strategy {strategy!r}", warnings)
    today = _as_of(as_of)
    as_of_s = today.isoformat()

    front, days = horizon(legs_in, today)
    if days < 0:
        return _empty(strategy, family, symbol, spot, atr_v, as_of_s, units, legs_in, ERR_EXPIRED, warnings)
    st = _structure(legs_in)
    if family in CREDIT_FAMILY_NAMES:
        width_ok = True
        if family == "credit_vertical" and len(legs_in) == 2:
            width_ok = abs(legs_in[0].strike - legs_in[1].strike) > 0
        if st["credit"] <= 0 or not width_ok:
            return _empty(strategy, family, symbol, spot, atr_v, as_of_s, units, legs_in, ERR_NO_EDGE, warnings)
    if not (atr_v and atr_v > 0):
        warnings.append(WARN_NO_ATR)

    legs_c = calibrate(legs_in, spot, today, sigma_fallback=sigma_fallback, prefer_leg_iv=pl_now is not None)
    two_expiry = any(l.expiry != front for l in legs_c)
    if two_expiry and any(l.expiry != front and not l.iv for l in legs_c):
        warnings.append(WARN_FAR_INTRINSIC)

    # the markers' prices first (they are grid points), then the curves, then the
    # breakevens join the grid so the zones split exactly there
    lv = []
    for lvl in levels or ():
        x = _num((lvl or {}).get("x"))
        if x is not None:
            lv.append({"x": x, "label": lvl.get("label"), "kind": str(lvl.get("kind") or "level")})
    if target is None:
        for lvl in lv:
            if lvl["kind"] == "target":
                target = lvl["x"]
                break
    marker_xs = [spot] + [v for v in (chart_stop, target) if _num(v) is not None] + [l["x"] for l in lv]
    # the breakevens are markers too, so they are searched on the wide bracket
    # FIRST and the grid then pads 2 ATR beyond them (a calendar's 93.90 / 107.88
    # sit outside a 2-ATR frame around its one strike)
    lo_w, hi_w = _scan_range(legs_c, spot, atr_v, marker_xs)
    bes = _find_crossings(legs_c, 0.0, days, lo_w, hi_w, today)
    xs = grid(legs_c, spot, atr_v, marker_xs + bes, breakevens=bes)
    ye = expiry_curve(legs_c, xs, today)
    yt = curve_at(legs_c, xs, 0, today)
    if yt is None:
        warnings.append(WARN_NO_SIGMA)
    ext = extremes(family, legs_c, xs, ye)
    warnings.extend(ext["warnings"])
    max_profit, max_loss = ext["max_profit"], ext["max_loss"]

    def at_today(S: float) -> float | None:
        if yt is None:
            return None
        try:
            return pnl(legs_c, S, 0, today)
        except NoSigma:
            return None

    def at_expiry(S: float) -> float:
        return pnl(legs_c, S, days, today, strict=False)

    # ---- R and the rule stop (every family, R1)
    r_dollars, r_basis = 0.0, None
    rule_pl, rule_kind = None, None
    if family in CREDIT_FAMILY_NAMES:
        frac = float(loss_fraction if loss_fraction is not None else LOSS_STOP_FRACTION)
        if max_loss:
            r_dollars, r_basis = round(frac * max_loss, 2), f"{_px(frac * 100)}% of max loss"
            rule_pl, rule_kind = -round(frac * max_loss, 2), r_basis
    else:
        stop_pl = at_today(chart_stop) if chart_stop is not None else None
        if stop_pl is not None and stop_pl < 0:
            r_dollars, r_basis = round(-stop_pl, 2), "loss at the chart stop today"
        elif chart_stop is not None and stop_pl is not None:
            warnings.append(WARN_R_ZERO)
        elif max_loss:
            r_dollars, r_basis = round(max_loss, 2), "max loss"
        if family is not None:
            pct = _num(premium_stop_pct)
            if pct is None and strategy:
                pct = _house_premium_stop_pct(strategy)
            debit = st["debit"]
            if pct is not None and pct > 0 and debit > 0:
                rule_pl = -round(pct / 100.0 * debit * MULT, 2)
                rule_kind = f"{_px(pct)}% of the premium" if family in ("long", "leaps") else f"{_px(pct)}% of what you paid"

    # the rule stop's price(s): where the TODAY curve reaches the rule's dollars
    # (the expiry curve when no sigma could be found), searched on the wide
    # bracket; a hit outside the frame widens the grid so the marker is drawn
    rule_xs: list[float] = []
    if rule_pl is not None:
        if yt is not None:
            rule_xs = _find_crossings(legs_c, rule_pl, 0, lo_w, hi_w, today, strict=True)
        else:
            rule_xs = _find_crossings(legs_c, rule_pl, days, lo_w, hi_w, today)
        if rule_xs and (rule_xs[0] < xs[0] or rule_xs[-1] > xs[-1] or any(round(x, 4) not in xs for x in rule_xs)):
            xs = grid(legs_c, spot, atr_v, marker_xs + bes + rule_xs, breakevens=bes)
            ye = expiry_curve(legs_c, xs, today)
            yt = curve_at(legs_c, xs, 0, today)
            ext2 = extremes(family, legs_c, xs, ye)
            ext["max_profit_x"], ext["unlimited_profit"], ext["unlimited_loss"] = (
                ext2["max_profit_x"], ext2["unlimited_profit"], ext2["unlimited_loss"])
            if ext["modelled"]:
                max_profit = ext["max_profit"] = ext2["max_profit"]

    mode = units
    if mode == "R" and not (r_dollars and r_dollars > 0):
        mode = "$"
        if WARN_R_ZERO not in warnings:
            warnings.append(WARN_R_ZERO)
    scale = r_dollars if mode == "R" else 1.0

    def fmt(v: float | None) -> str:
        if v is None:
            return "-"
        return _money(v) if mode == "$" else _rfmt(v / scale)

    def y(v: float | None) -> float | None:
        return None if v is None else round(v / scale, 2 if mode == "$" else 4)

    # ---- markers
    markers: list[dict] = []
    now = {"x": round(spot, 2), "label": f"now {spot:.2f}", "kind": "now"}
    if pl_now is not None:
        now["y_today"] = y(float(pl_now))
    markers.append(now)
    for b in bes:
        markers.append({"x": round(b, 2), "label": f"breakeven {b:.2f}", "kind": "breakeven"})
    if chart_stop is not None:
        s_t, s_e = at_today(chart_stop), at_expiry(chart_stop)
        lbl = (f"chart stop {_px(chart_stop)} · about {fmt(s_t)} today" if s_t is not None
               else f"chart stop {_px(chart_stop)} · {fmt(s_e)} at expiry")
        markers.append({"x": round(float(chart_stop), 2), "label": lbl, "kind": "stop",
                        "y_today": y(s_t), "y_expiry": y(s_e)})
    for x in rule_xs:
        markers.append({"x": round(x, 2), "label": f"rule stop about {x:.1f}", "kind": "rule_stop",
                        "y_today": y(rule_pl)})
    if target is not None:
        t_t, t_e = at_today(target), at_expiry(target)
        lbl = (f"target {_px(target)} · {fmt(t_t)} today" if t_t is not None
               else f"target {_px(target)} · {fmt(t_e)} at expiry")
        markers.append({"x": round(float(target), 2), "label": lbl, "kind": "target",
                        "y_today": y(t_t), "y_expiry": y(t_e)})
    level_words = {"support": "support", "resistance": "resistance", "trend_line": "trend line at expiry"}
    for lvl in lv:
        if lvl["kind"] == "target":
            continue
        word = level_words.get(lvl["kind"], lvl["kind"].replace("_", " "))
        markers.append({"x": round(lvl["x"], 2), "label": lvl["label"] or f"{word} {_px(lvl['x'])}",
                        "kind": "level", "level": lvl["kind"]})
    # one strike marker per price: a calendar's two legs share a strike (and are told
    # apart by their expiry), a condor has four
    by_strike: dict[float, list[str]] = {}
    for l in legs_c:
        word = f"{'short' if l.qty < 0 else 'long'} {_px(l.strike)}"
        if two_expiry:
            word += f" {_expiry_label(l.expiry)}"
        by_strike.setdefault(round(l.strike, 2), []).append(word)
    for x, words in by_strike.items():
        markers.append({"x": x, "label": " · ".join(words), "kind": "strike"})

    # ---- hlines
    hlines: list[dict] = []
    if rule_pl is not None:
        hlines.append({"y": y(rule_pl), "label": f"rule stop {fmt(rule_pl)} ({rule_kind})", "kind": "rule_stop"})
    if family in CREDIT_FAMILY_NAMES and st["credit"] > 0:
        tp = 0.5 * st["credit"] * MULT
        hlines.append({"y": y(tp), "label": f"take profit {fmt(tp)} (50% of credit)", "kind": "target"})
    if max_profit is not None and not ext["unlimited_profit"]:
        hlines.append({"y": y(max_profit), "label": "max profit" + (" (about, modelled)" if ext["modelled"] else ""),
                       "kind": "max_profit"})
    if max_loss is not None:
        hlines.append({"y": y(-max_loss), "label": f"max loss {_money_abs(max_loss) if mode == '$' else _rfmt(-max_loss / scale)}",
                       "kind": "max_loss"})

    # ---- POP: the delta figure (credit families) and the model figure (every family)
    front_legs = [l for l in legs_c if l.expiry == front and l.iv]
    sigma_h = min(front_legs, key=lambda l: abs(l.strike - spot)).iv if front_legs else (
        float(sigma_fallback) if sigma_fallback and sigma_fallback > 0 else None)
    model = pop(family, legs_c, spot, sigma_h, days / 365.0, xs, ye)
    model_basis = f"lognormal, sigma {sigma_h * 100:.0f}%, {days} d" if (model is not None and sigma_h) else None
    delta_fig, delta_basis = None, None
    if family in CREDIT_FAMILY_NAMES:
        shorts = [l for l in legs_c if l.qty < 0 and l.delta is not None]
        if family == "credit_vertical" and len(shorts) == 1:
            d = abs(shorts[0].delta)
            delta_fig, delta_basis = 1.0 - d, f"1 - short delta {d:.2f}"
        elif family == "condor" and len(shorts) == 2:
            ds = [abs(l.delta) for l in shorts]
            delta_fig, delta_basis = 1.0 - sum(ds), f"1 - short deltas {ds[0]:.2f} + {ds[1]:.2f}"
    if family in CREDIT_FAMILY_NAMES:
        pop_d = {"label": "chance of keeping it",
                 "value": round(delta_fig, 3) if delta_fig is not None else (round(model, 3) if model is not None else None),
                 "basis": delta_basis if delta_fig is not None else model_basis,
                 "model": round(model, 3) if model is not None else None, "model_basis": model_basis}
    else:
        pop_d = {"label": "chance of profit",
                 "value": round(model, 3) if model is not None else None, "basis": model_basis,
                 "model": round(model, 3) if model is not None else None, "model_basis": model_basis}

    # ---- words
    caption = CAPTION.format(dte=days)
    if two_expiry:
        caption += " " + CAPTION_TWO_EXPIRY.format(front=_expiry_label(front))
    leg_parts = []
    if bes:
        leg_parts.append("breakeven " + " / ".join(f"{b:.2f}" for b in bes))
    mp_s = "unlimited" if ext["unlimited_profit"] else (fmt(max_profit) if max_profit is not None else "-")
    ml_s = "unlimited" if ext["unlimited_loss"] else (fmt(-max_loss) if max_loss is not None else "-")
    leg_parts.append(f"max {mp_s} / {ml_s}")
    if pop_d["value"] is not None:
        leg_parts.append(f"{pop_d['label']} {pop_d['value'] * 100:.0f}%")
        if family in CREDIT_FAMILY_NAMES and pop_d["model"] is not None and delta_fig is not None:
            leg_parts.append(f"model estimate {pop_d['model'] * 100:.0f}%")
    leg_parts.append("per contract")
    legend = " · ".join(leg_parts)

    result = {
        "strategy": strategy, "family": family, "label": _label(legs_c), "strategy_label": _strategy_label(strategy),
        "symbol": symbol, "spot": round(spot, 2), "atr": atr_v, "as_of": as_of_s,
        "horizon": {"expiry": front, "dte": days},
        "legs": [l.as_dict() for l in legs_c],
        "xs": xs, "at_expiry": [y(v) for v in ye], "today": [y(v) for v in yt] if yt is not None else None,
        "breakevens": [round(b, 2) for b in bes],
        "max_profit": y(max_profit), "max_loss": y(max_loss),
        "unlimited_profit": ext["unlimited_profit"], "unlimited_loss": ext["unlimited_loss"],
        "pop": pop_d, "markers": markers, "hlines": hlines,
        "units": {"mode": mode, "r_dollars": r_dollars, "r_basis": r_basis},
        "caption": caption, "legend": legend, "warnings": warnings, "error": None,
        "uid": uuid.uuid4().hex[:8],
    }
    result["svg"] = svg_paths(result)
    result["series"] = {"xs": xs, "at_expiry": result["at_expiry"], "today": result["today"],
                        "units": dict(result["units"])}
    return result
