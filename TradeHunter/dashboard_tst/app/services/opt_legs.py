"""Leg normalisation - ONE shape for every engine, whatever the chain's source.

Two chain shapes exist today and disagree on units and key names: the Cboe dict
(``option_quotes.fetch_chain``: IV a FRACTION, ``open_interest``) and the raw IBKR
bridge row (``bridge/ibkr_bridge.py:_row``: IV in PERCENT, ``oi``). Part A's
``ContractRow`` already carries a fraction for every source it wraps. This module
is where the three meet: ``norm_leg(row, unit=)`` produces the one dict every
engine reads (part_B_engines.md B0.2, OPTIONS_MODULE_DESIGN.md II.2.8), and
``chain_view()`` turns any chain into ``{by_expiry, dte, spot, iv30, ...}``.

**The unit is decided by the SOURCE, never by magnitude.** There is no "a value
over three must be a percent" test anywhere: a deep-in-the-money Cboe contract
legitimately prints ``iv`` 3.1099 as a fraction, so a magnitude rule would
corrupt a real row. The caller says ``unit="fraction"`` (Cboe, Alpaca, a snapshot
row, a ``ContractRow`` of any source - including one that came through
``BridgePayloadSource``, which already divided) or ``unit="percent"`` (ONLY the
legacy raw bridge payload the old Watchlist tab still posts).

The mid / width / liquidity helpers are ``bull_put``'s own (``_mid``,
``_leg_spread``, ``_abs_delta``, ``_count``, ``oi_needed``), re-stated here under
public names so the new engines never import a private name; ``bull_put`` itself
is untouched.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import math

from . import bull_put
from .opt_constants import (
    IDEAL_LEG_SPREAD,
    IV_SANITY_HI,
    IV_SANITY_LO,
    LIQ_FACTOR_CLEAN,
    LIQ_FACTOR_LIMIT,
    LIQ_FACTOR_OI_UNKNOWN,
    MAX_LEG_SPREAD,
    MIN_LEG_VOLUME,
    MIN_OPEN_INTEREST,
    OI_PER_CONTRACT,
)

UNITS = ("fraction", "percent")

# The stored / API leg every part shares (contract LEG) - the first eleven keys
# of norm_leg's output. Everything else is an engine-only extra.
STORED_KEYS = ("expiry", "right", "strike", "side", "qty", "price", "bid", "ask", "iv", "delta", "oi", "volume")
EXTRA_KEYS = ("last", "gamma", "theta", "vega", "spread", "quote_ok")

oi_needed = bull_put.oi_needed      # the playbook's OI floor for an order of N contracts


# ------------------------------------------------------------- primitives
def _num(v):
    """A float, or None for anything that is not one (NaN, inf, text, None)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _price(v):
    """A per-share price; negative -> None (a feed artefact, never a quote)."""
    f = _num(v)
    return None if (f is None or f < 0) else f


def count(row: dict, key: str) -> int | None:
    """Open interest / volume off a chain row: an int, or None when the feed did
    not say (bull_put._count: anything that is not a sane non-negative number is
    "unknown", not zero)."""
    return bull_put._count(row, key)


def mid(row: dict) -> float | None:
    """(bid + ask) / 2, falling back to ``last`` when a side is missing
    (bull_put._mid - the legacy tab's reading; norm_leg's ``price`` is stricter)."""
    return bull_put._mid(row)


def leg_spread(row: dict) -> float | None:
    """ask - bid, or None when either side is missing (bull_put._leg_spread)."""
    return bull_put._leg_spread(row)


def abs_delta(row: dict) -> float | None:
    return bull_put._abs_delta(row)


def width(a: float, b: float) -> float:
    """The distance between two strikes, always positive."""
    return abs(float(a) - float(b))


def _get(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _as_dict(row) -> dict:
    if isinstance(row, dict):
        return row
    if dataclasses.is_dataclass(row):
        return dataclasses.asdict(row)
    return {k: getattr(row, k) for k in dir(row) if not k.startswith("_") and not callable(getattr(row, k))}


# ---------------------------------------------------------------- norm_leg
def norm_leg(row, *, unit: str, expiry: str | None = None, right: str | None = None) -> dict:
    """One contract row of ANY source -> the engine leg (B0.2).

    ``unit`` is REQUIRED: "fraction" (Cboe / Alpaca / snapshot / ContractRow) or
    "percent" (a raw bridge row only; divided by 100 here). ``expiry`` / ``right``
    fill in what a bridge row lacks (its chain carries one expiry per call).

    Output keys, in order: expiry, right, strike, side (None), qty (None), price
    (the mid, None without a two-sided quote), bid, ask, iv (fraction), delta
    (signed), oi, volume, then the engine-only extras last, gamma, theta, vega,
    spread, quote_ok. A 0.0 bid with a 0.0 ask is "no quote": bid = ask = price =
    None, quote_ok False. IV outside (IV_SANITY_LO, IV_SANITY_HI) -> None after the
    unit conversion; |delta| > 1 -> None; a negative price -> None.
    """
    if unit not in UNITS:
        raise ValueError(f"norm_leg: unit must be one of {UNITS}, not {unit!r}")
    r = _as_dict(row)
    exp = r.get("expiry") or expiry
    rt = (r.get("right") or right or "").upper()[:1]
    strike = _num(r.get("strike"))
    bid, ask = _price(r.get("bid")), _price(r.get("ask"))
    if bid == 0.0 and ask == 0.0:          # Cboe sends 0.0/0.0 for "no quote"
        bid = ask = None
    quote_ok = bid is not None and ask is not None and bid > 0 and ask > 0
    price = round((bid + ask) / 2.0, 4) if quote_ok else None
    iv = _num(r.get("iv"))
    if iv is not None:
        if unit == "percent":
            iv = iv / 100.0
        if not (IV_SANITY_LO < iv < IV_SANITY_HI):
            iv = None
    delta = _num(r.get("delta"))
    if delta is not None and abs(delta) > 1.0:
        delta = None
    oi = count(r, "oi") if "oi" in r else count(r, "open_interest")
    return {
        "expiry": exp, "right": rt, "strike": strike,
        "side": None, "qty": None,
        "price": price, "bid": bid, "ask": ask,
        "iv": iv, "delta": delta,
        "oi": oi, "volume": count(r, "volume"),
        "last": _price(r.get("last", r.get("last_trade_price"))),
        "gamma": _num(r.get("gamma")), "theta": _num(r.get("theta")), "vega": _num(r.get("vega")),
        "spread": round(ask - bid, 4) if (bid is not None and ask is not None) else None,
        "quote_ok": quote_ok,
    }


def stored_leg(leg: dict) -> dict:
    """The eleven stored / API keys only - what a leg looks like on a Pick, an
    OptionTrade or the wire; the engine-only extras are stripped."""
    return {k: leg.get(k) for k in STORED_KEYS}


# ------------------------------------------------------------- chain views
def _unit_of(chain) -> str:
    """A raw bridge dict ({"puts", "calls"}) is the only percent chain; every other
    shape (a Chain of any source, a Cboe dict, snapshot rows) is a fraction."""
    if isinstance(chain, dict) and ("puts" in chain or "calls" in chain) and "legs" not in chain and "rows" not in chain:
        return "percent"
    return "fraction"


def _bridge_expiry(s) -> str | None:
    """'20261120' -> '2026-11-20' (the bridge's expiry spelling)."""
    if not s:
        return None
    s = str(s)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def dte_of(expiry: str, today: _dt.date | str | None = None) -> int | None:
    """Calendar days from ``today`` (an ET date; default spread_monitor.et_today())
    to ``expiry`` - the same arithmetic as spread_monitor._dte, with the ET date so
    a Malaysian evening does not count one day too few. None for a bad expiry."""
    if today is None:
        from .spread_monitor import et_today
        today = et_today()
    if isinstance(today, str):
        today = _dt.date.fromisoformat(today[:10])
    try:
        return (_dt.date.fromisoformat(str(expiry)[:10]) - today).days
    except (TypeError, ValueError):
        return None


def _rows_of(chain) -> list[tuple[dict, str | None, str | None]]:
    """(row, expiry, right) triples from any chain shape."""
    rows = _get(chain, "rows", None)
    if rows is not None and not isinstance(chain, dict):          # a Chain dataclass
        return [(r, None, None) for r in rows]
    if isinstance(chain, dict):
        if "rows" in chain:
            return [(r, None, None) for r in (chain.get("rows") or [])]
        if "legs" in chain:
            out = []
            for key, leg in (chain.get("legs") or {}).items():
                exp, rt = (key[0], key[1]) if isinstance(key, tuple) and len(key) >= 2 else (None, None)
                out.append((leg, exp, rt))
            return out
        if "puts" in chain or "calls" in chain:
            exp = _bridge_expiry(chain.get("expiry"))
            return ([(r, exp, "P") for r in (chain.get("puts") or [])]
                    + [(r, exp, "C") for r in (chain.get("calls") or [])])
    if isinstance(chain, (list, tuple)):
        return [(r, None, None) for r in chain]
    return []


def chain_view(chain, *, today: _dt.date | str | None = None, unit: str | None = None) -> dict:
    """Any chain shape -> ``{"spot", "iv30", "as_of", "source", "symbol",
    "by_expiry": {expiry: {"P": [legs by strike], "C": [...]}}, "dte": {expiry: int}}``.

    Accepts Part A's ``Chain`` (any source; its rows are fractions), the Cboe dict
    (``legs`` keyed (expiry, right, strike)), a raw bridge dict (``puts`` /
    ``calls`` for ONE expiry, IV in percent), a dict with ``rows`` (snapshot rows
    re-read from the DB) or a bare list of rows. The unit is taken from the shape
    (only the raw bridge dict is percent) unless ``unit`` overrides it.
    """
    u = unit or _unit_of(chain)
    by_expiry: dict[str, dict[str, list[dict]]] = {}
    for row, exp, rt in _rows_of(chain):
        leg = norm_leg(row, unit=u, expiry=exp, right=rt)
        if leg["expiry"] is None or leg["right"] not in ("P", "C") or leg["strike"] is None or leg["strike"] <= 0:
            continue
        by_expiry.setdefault(leg["expiry"], {"P": [], "C": []})[leg["right"]].append(leg)
    for sides in by_expiry.values():
        sides["P"].sort(key=lambda l: l["strike"])
        sides["C"].sort(key=lambda l: l["strike"])
    if today is None:
        snap = _get(chain, "snap_on", None) if not isinstance(chain, dict) else None
        today = snap or None
    expiries = sorted(by_expiry)
    as_of = _get(chain, "as_of", None)
    if isinstance(as_of, _dt.datetime):
        as_of = as_of.isoformat(timespec="seconds")
    source = _get(chain, "source", None) or ("bridge" if u == "percent" else "cboe")
    return {
        "symbol": _get(chain, "symbol", None),
        "spot": _num(_get(chain, "spot", None)),
        "iv30": _num(_get(chain, "iv30", None)),
        "as_of": as_of, "source": source,
        "by_expiry": {e: by_expiry[e] for e in expiries},
        "dte": {e: dte_of(e, today) for e in expiries},
    }


def by_expiry(view_or_chain) -> dict:
    """The ``by_expiry`` map of a view (or of any chain, built on the spot)."""
    if isinstance(view_or_chain, dict) and "by_expiry" in view_or_chain:
        return view_or_chain["by_expiry"]
    return chain_view(view_or_chain)["by_expiry"]


def legs_at(view: dict, expiry: str, right: str) -> list[dict]:
    """The legs of one expiry / right, sorted by strike ([] when not listed)."""
    return ((view.get("by_expiry") or {}).get(expiry) or {}).get((right or "P").upper()[:1], [])


def nearest_strike(legs_or_strikes, target: float) -> dict | float | None:
    """The leg (or strike) whose strike is nearest ``target``; None on an empty list.
    Ties go to the LOWER strike (the conservative side for a put, the same rule for
    a call keeps the choice deterministic)."""
    items = list(legs_or_strikes or [])
    if not items:
        return None
    key = (lambda x: _get(x, "strike") if not isinstance(x, (int, float)) else x)
    return min(items, key=lambda x: (abs(float(key(x)) - float(target)), float(key(x))))


# --------------------------------------------------------------- liquidity
def ba_tier(widest: float | None, *, max_leg_spread: float = MAX_LEG_SPREAD,
            ideal_leg_spread: float = IDEAL_LEG_SPREAD) -> str | None:
    """Where the WIDEST leg falls in the playbook's band (bull_put._pair):
    "clean" (<= ideal), "limit" (<= max), "wide" (over - excluded), None (unknown)."""
    if widest is None:
        return None
    return "clean" if widest <= ideal_leg_spread else "limit" if widest <= max_leg_spread else "wide"


def natural(legs: list[dict]) -> float | None:
    """The worst likely fill of a position, per share: sold legs at the bid, bought
    legs at the ask, signed like ``net`` (negative = a credit). None when a leg has
    no quote on the side that matters."""
    total = 0.0
    for leg in legs:
        q = int(leg.get("qty") or 1)
        if leg.get("side") == "sell":
            if leg.get("bid") is None:
                return None
            total -= float(leg["bid"]) * q
        else:
            if leg.get("ask") is None:
                return None
            total += float(leg["ask"]) * q
    return round(total, 4)


def liquidity(legs: list[dict], *, contracts: int = 1,
              min_oi: int = MIN_OPEN_INTEREST, oi_per_contract: int = OI_PER_CONTRACT,
              max_leg_spread: float = MAX_LEG_SPREAD, min_leg_volume: int = MIN_LEG_VOLUME,
              ideal_leg_spread: float = IDEAL_LEG_SPREAD) -> dict:
    """The Pick's ``liquidity`` dict (B4.4) for a set of legs:
    ``{tier, widest, min_oi, vol_ok, worst_fill, notes, factor, oi_needed, ok}``.

    tier = the WORST leg's bid/ask tier ("clean" / "limit" / "wide"), "thin" when
    any leg's open interest is known and under ``oi_needed`` (known-thin EXCLUDES),
    "unknown" when every leg's bid/ask is missing. An unreported OI never excludes
    ("TWS did not say" is not evidence) but costs LIQ_FACTOR_OI_UNKNOWN; volume
    only adds a note. ``ok`` is False for "wide" and "thin".
    """
    need = oi_needed(contracts, min_open_interest=min_oi, oi_per_contract=oi_per_contract)
    spreads = [l.get("spread") for l in legs]
    known = [s for s in spreads if s is not None]
    widest = max(known) if known else None
    tier = ba_tier(widest, max_leg_spread=max_leg_spread, ideal_leg_spread=ideal_leg_spread)
    ois = [l.get("oi") for l in legs]
    oi_known = [o for o in ois if o is not None]
    oi_unknown = len(oi_known) < len(legs)
    thin = any(o < need for o in oi_known)
    vols = [l.get("volume") for l in legs]
    vol_ok = None if any(v is None for v in vols) else all(v >= min_leg_volume for v in vols)
    notes: list[str] = []
    if tier == "limit":
        notes.append("bid/ask at the limit")
    if tier == "wide":
        notes.append("bid/ask too wide")
    if tier is None:
        notes.append("no bid/ask on these legs right now")
    if thin:
        notes.append(f"open interest under {need:,}")
    if oi_unknown:
        notes.append("open interest unknown - check in TWS")
    if vol_ok is False:
        notes.append("barely traded today")
    out_tier = "thin" if thin else (tier or "unknown")
    factor = (LIQ_FACTOR_LIMIT if tier == "limit" else LIQ_FACTOR_CLEAN)
    if oi_unknown:
        factor = min(factor, LIQ_FACTOR_OI_UNKNOWN)
    return {
        "tier": out_tier, "widest": widest,
        "min_oi": min(oi_known) if oi_known else None,
        "vol_ok": vol_ok, "worst_fill": natural(legs), "notes": notes,
        "factor": factor, "oi_needed": need,
        "ok": out_tier not in ("wide", "thin"),
    }
