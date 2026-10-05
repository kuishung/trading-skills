"""Shared fixtures for the Options module tests (part_A_data.md A8, part_B_engines.md B9).

* ``load_cboe_small()`` - the trimmed REAL Cboe MSFT payload (`cboe_MSFT_small.json`:
  3 expiries x 15 strikes of the 2026-10-02 session, plus three hand-added edge
  rows listed under its ``_synthetic_rows`` key).
* ``chain_bs(...)`` - a Cboe-shaped chain priced by Black-Scholes, bid/ask = mid +/-
  spread/2, OI / volume from a seed: the synthetic chain every engine test runs on
  with TWS off and Cboe down.
* ``bars_synth(kind)`` - daily bars in ``prices.fetch_daily_ohlc``'s shape for the
  chart cases (uptrend + bounce, downtrend + breakdown, range, flat, slow grind,
  constant).

Everything is deterministic (seeded) so a test's numbers never drift.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
CBOE_SMALL = HERE / "cboe_MSFT_small.json"

RISK_FREE = 0.04


def load_cboe_small() -> dict:
    """The whole fixture file (``data`` holds the feed's payload)."""
    with CBOE_SMALL.open(encoding="utf-8") as fh:
        return json.load(fh)


def cboe_small_data() -> dict:
    return load_cboe_small()["data"]


# ------------------------------------------------------------- Black-Scholes
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(S: float, K: float, T: float, sigma: float, kind: str, r: float = RISK_FREE) -> dict:
    """price, delta, gamma, theta (per day), vega (per vol point), rho (per point)
    for a European option; T in years, sigma a FRACTION. T <= 0 -> intrinsic."""
    kind = "call" if str(kind).upper().startswith("C") else "put"
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, S - K) if kind == "call" else max(0.0, K - S)
        return {"price": intrinsic, "delta": (1.0 if S > K else 0.0) if kind == "call" else (-1.0 if S < K else 0.0),
                "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    disc = math.exp(-r * T)
    if kind == "call":
        price = S * _ncdf(d1) - K * disc * _ncdf(d2)
        delta = _ncdf(d1)
        theta = (-S * _npdf(d1) * sigma / (2 * sq) - r * K * disc * _ncdf(d2)) / 365.0
        rho = K * T * disc * _ncdf(d2) / 100.0
    else:
        price = K * disc * _ncdf(-d2) - S * _ncdf(-d1)
        delta = _ncdf(d1) - 1.0
        theta = (-S * _npdf(d1) * sigma / (2 * sq) + r * K * disc * _ncdf(-d2)) / 365.0
        rho = -K * T * disc * _ncdf(-d2) / 100.0
    gamma = _npdf(d1) / (S * sigma * sq)
    vega = S * _npdf(d1) * sq / 100.0
    return {"price": price, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


def chain_bs(spot: float, iv, expiries, strikes, *, today: str = "2026-10-03", skew: float = 0.0,
             spread: float = 0.10, oi_seed: int = 1000, symbol: str = "SYN", r: float = RISK_FREE,
             as_of: str | None = None, iv30: float | None = None) -> dict:
    """A Cboe-shaped chain (the ``option_quotes.fetch_chain`` dict, ``header``
    included) priced by Black-Scholes at ``r``.

    ``iv`` is a FRACTION - one float for the whole chain or ``{expiry: iv}``;
    ``skew`` tilts it per strike as ``sigma_K = iv * (1 + skew * (spot - K) /
    spot)`` (positive = lower strikes richer, the normal equity shape); bid / ask =
    mid -/+ spread / 2 (never under 0.01); OI and volume come from ``oi_seed`` and
    fall off away from the money. ``as_of`` defaults to ``today`` 15:59:59 ET.
    """
    rnd = random.Random(oi_seed)
    t0 = _dt.date.fromisoformat(today)
    legs: dict[tuple, dict] = {}
    for exp in expiries:
        dte = (_dt.date.fromisoformat(exp) - t0).days
        T = max(dte, 0) / 365.0
        base = iv[exp] if isinstance(iv, dict) else float(iv)
        for K in strikes:
            K = float(K)
            sigma = base * (1.0 + skew * (spot - K) / spot)
            sigma = max(0.01, sigma)
            closeness = max(0.0, 1.0 - abs(K - spot) / (0.25 * spot))
            for right in ("C", "P"):
                g = bs_greeks(spot, K, T, sigma, right, r)
                mid = round(max(0.01, g["price"]), 2)
                half = spread / 2.0
                bid = round(max(0.0, mid - half), 2)
                ask = round(mid + half, 2)
                oi = int(200 + 4800 * closeness * rnd.uniform(0.6, 1.0))
                vol = int(oi * rnd.uniform(0.02, 0.15))
                legs[(exp, right, round(K, 3))] = {
                    "expiry": exp, "right": right, "strike": K,
                    "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
                    "iv": round(sigma, 4), "delta": round(g["delta"], 4),
                    "gamma": round(g["gamma"], 5), "theta": round(g["theta"], 4),
                    "vega": round(g["vega"], 4), "theo": round(g["price"], 4),
                    "open_interest": float(oi), "volume": float(vol),
                    "rho": round(g["rho"], 4), "last": mid, "bid_size": float(rnd.randint(1, 80)),
                    "ask_size": float(rnd.randint(1, 80)), "prev_close": mid,
                }
    stamp = as_of or f"{today}T15:59:59"
    atm = iv[min(iv, key=lambda e: abs((_dt.date.fromisoformat(e) - t0).days - 30))] if isinstance(iv, dict) else float(iv)
    header = {"symbol": symbol, "current_price": spot, "iv30": round(atm * 100.0, 3) if iv30 is None else iv30,
              "last_trade_time": stamp, "prev_day_close": spot, "close": spot, "volume": 1_000_000}
    return {"symbol": symbol, "spot": spot, "iv30": header["iv30"], "as_of": stamp,
            "fetched_at": f"{today}T20:00:00+00:00", "legs": legs, "header": header}


# ----------------------------------------------------------------- bars
KINDS = ("uptrend_bounce", "downtrend_breakdown", "range", "flat", "slow_grind", "constant")


def _business_days(end: str, n: int) -> list[_dt.date]:
    d = _dt.date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= _dt.timedelta(days=1)
    return out[::-1]


def bars_synth(kind: str = "uptrend_bounce", n: int = 300, start: float = 100.0, seed: int = 7,
               end: str = "2026-10-03", open_last: bool = False) -> list[dict]:
    """Daily bars ``{time, open, high, low, close, volume}`` (prices.fetch_daily_ohlc's
    shape), oldest first, ending on ``end``. ``open_last=True`` marks the last bar
    with ``session_frac`` (a still-open session). Kinds: uptrend_bounce (a drift up
    with a pullback to a prior low and a high-volume bounce on the last bar),
    downtrend_breakdown (the mirror, breaking a prior low on the last bar), range
    (mean reversion between two edges, both touched), flat (noise only),
    slow_grind (a small steady drift), constant (every close equal)."""
    if kind not in KINDS:
        raise ValueError(f"bars_synth: kind must be one of {KINDS}")
    rnd = random.Random(seed)
    days = _business_days(end, n)
    drift, vol = {"uptrend_bounce": (0.0008, 0.012), "downtrend_breakdown": (-0.0008, 0.012),
                  "range": (0.0, 0.009), "flat": (0.0, 0.003), "slow_grind": (0.0003, 0.005),
                  "constant": (0.0, 0.0)}[kind]
    out: list[dict] = []
    c = start
    lo_edge, hi_edge = start * 0.95, start * 1.05
    for i, d in enumerate(days):
        o = c
        if kind == "constant":
            c = start
        elif kind == "range":
            pull = (start - c) / start * 0.15
            c = c * (1 + pull + rnd.gauss(0, vol))
            c = min(max(c, lo_edge * 0.995), hi_edge * 1.005)
        else:
            c = c * (1 + drift + rnd.gauss(0, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        v = int(1_000_000 * rnd.uniform(0.7, 1.3))
        out.append({"time": d.isoformat(), "open": round(o, 2), "high": round(h, 2),
                    "low": round(l, 2), "close": round(c, 2), "volume": v})
    if kind == "uptrend_bounce" and n >= 30:
        # a pullback to the low of 15 bars back, then a pin-bar bounce on 1.8x volume
        level = min(b["low"] for b in out[-20:-5])
        for k, b in enumerate(out[-6:-1], start=1):
            tgt = out[-7]["close"] - (out[-7]["close"] - level) * k / 5
            b.update(open=round(tgt * 1.004, 2), close=round(tgt, 2), high=round(tgt * 1.008, 2), low=round(tgt * 0.996, 2))
        last = out[-1]
        last.update(open=round(level * 1.002, 2), low=round(level * 0.994, 2), high=round(level * 1.02, 2),
                    close=round(level * 1.018, 2), volume=int(1.8 * 1_000_000))
    if kind == "downtrend_breakdown" and n >= 30:
        level = min(b["low"] for b in out[-40:-2])
        last = out[-1]
        last.update(open=round(level * 1.006, 2), high=round(level * 1.01, 2), low=round(level * 0.97, 2),
                    close=round(level * 0.975, 2), volume=int(1.9 * 1_000_000))
    if open_last and out:
        out[-1]["session_frac"] = 0.4
    return out
