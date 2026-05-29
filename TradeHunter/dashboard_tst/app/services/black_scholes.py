"""Black-Scholes option pricing + probability metrics. Pure stdlib math,
no external data or dependencies.

Used by the option win-rate module (DESIGN.md phase 4). The exact
definition of "maximum win rate" is still [OPEN] in DESIGN.md, so this
module exposes the standard building blocks rather than committing to one:

  - ``prob_itm``: the risk-neutral probability the option expires in the
    money (N(d2) for calls, N(-d2) for puts).
  - ``price`` and ``delta`` for completeness.

IMPORTANT CAVEAT to surface in the UI: risk-neutral probabilities are a
*pricing convention*, not a real-world forecast of winning. A real-world
win-rate would discount by the asset's actual drift, not the risk-free
rate. We'll make that distinction explicit when the UI is wired.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (stdlib)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float):
    if not (S > 0 and K > 0 and T > 0 and sigma > 0):
        raise ValueError("S, K, T and sigma must all be positive")
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


@dataclass
class BSResult:
    price: float
    delta: float
    prob_itm: float  # risk-neutral P(expire ITM)


def black_scholes(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    kind: str = "call",
    q: float = 0.0,
) -> BSResult:
    """Price + delta + risk-neutral ITM probability.

    S: spot, K: strike, T: years to expiry, r: risk-free rate,
    sigma: implied volatility (annualised), q: dividend yield.
    """
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if kind == "call":
        price = S * disc_q * norm_cdf(d1) - K * disc_r * norm_cdf(d2)
        delta = disc_q * norm_cdf(d1)
        prob_itm = norm_cdf(d2)
    elif kind == "put":
        price = K * disc_r * norm_cdf(-d2) - S * disc_q * norm_cdf(-d1)
        delta = -disc_q * norm_cdf(-d1)
        prob_itm = norm_cdf(-d2)
    else:
        raise ValueError("kind must be 'call' or 'put'")
    return BSResult(price=price, delta=delta, prob_itm=prob_itm)
