"""Server-side option quotes WITH GREEKS, from Cboe's public delayed feed.

Why this exists
---------------
``routes/options.py`` grades a chain the member's own browser fetched from their
own TWS (``bridge/ibkr_bridge.py``). That is exact and live, and it is the right
source while someone is sitting in front of the page — but the server can never
reach ``127.0.0.1:9224``, so **nothing can be checked on a day the member does
not open the page**. A daily monitor needs a source the SERVER can read.

Cboe publishes a delayed quote snapshot per underlying as flat JSON on their CDN:

    https://cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json

It carries, per contract: ``bid``, ``ask``, ``iv``, ``open_interest``, ``volume``,
``theo`` and the greeks — ``delta``, ``gamma``, ``theta``, ``vega``, ``rho`` — plus
the underlying's spot and ``iv30`` at the top. No key, no auth, no account.

That matters more than it sounds: it means the daily delta is a REAL delta off a
real chain, not a Black-Scholes guess from a modelled IV. Verified 2026-09-10
against MSFT 2026-09-18 460P/450P — spot 491.635 matched the broker screen to the
cent, short delta -0.0637.

What it is NOT
--------------
* **~15 minutes delayed.** Irrelevant for a once-a-day management check; do not
  use it for anything that needs the live tape.
* **Undocumented.** It is a public CDN endpoint, not a contracted API. Every
  caller must survive it going away — hence ``ChainError`` and the fact that the
  Portfolio page renders positions with or without quotes.
* **Not an execution feed.** Nothing here places, modifies or cancels an order.

Coverage is US-listed equity/ETF options. Index options use Cboe's underscore
form (``_SPX``); ``cboe_symbol`` handles the ones we are likely to see.
"""
from __future__ import annotations

import datetime as _dt
import re
import threading
import time

import httpx

BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradeHunter/1.0"

# The payload is ~1.5-2 MB per underlying, so a cache is not an optimisation, it
# is the difference between one fetch and one per position on the same ticker.
# 15 minutes because that is the feed's own delay — asking more often cannot
# return anything newer.
_TTL = 900.0
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# OCC-21 contract symbol: root + YYMMDD + C/P + strike * 1000, zero-padded to 8.
#   MSFT260918P00460000  ->  MSFT 2026-09-18 P 460.0
_OCC = re.compile(
    r"^(?P<root>[A-Z0-9._]+?)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
    r"(?P<cp>[CP])(?P<k>\d{8})$"
)

# Underlyings whose Cboe symbol is not simply the ticker. Index options live
# under an underscore form; everything else is the plain root.
_SYMBOL_MAP = {"SPX": "_SPX", "VIX": "_VIX", "NDX": "_NDX", "RUT": "_RUT",
               "XSP": "_XSP", "DJX": "_DJX"}


class ChainError(RuntimeError):
    """The chain could not be fetched or parsed. Callers must degrade, not crash:
    a Portfolio that cannot show today's delta still has to show the position."""


def cboe_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return _SYMBOL_MAP.get(s, s)


def parse_occ(code: str) -> dict | None:
    """``'MSFT260918P00460000'`` -> ``{'expiry','right','strike'}``; None if it is
    not an OCC symbol. Kept public so the monitor can label a leg without
    re-deriving the format."""
    m = _OCC.match((code or "").strip().upper())
    if not m:
        return None
    g = m.groupdict()
    return {"expiry": f"20{g['y']}-{g['m']}-{g['d']}",
            "right": g["cp"],
            "strike": int(g["k"]) / 1000.0}


def _num(v):
    """Cboe sends 0.0 for 'no quote' as often as it sends a real zero. Only the
    obviously-missing cases become None here; deciding whether a 0.0 bid is
    meaningful is the caller's judgement, not this parser's."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN -> None


def fetch_chain(symbol: str, *, ttl: float = _TTL) -> dict:
    """The whole delayed chain for one underlying, parsed and indexed.

    Returns::

        {"symbol", "spot", "iv30", "as_of", "fetched_at",
         "legs": {(expiry, right, strike): {bid, ask, mid, iv, delta, gamma,
                                            theta, vega, open_interest, volume,
                                            theo}}}

    Raises ``ChainError`` on any network or shape failure.
    """
    sym = cboe_symbol(symbol)
    if not sym:
        raise ChainError("no symbol")

    now = time.time()
    with _lock:
        hit = _cache.get(sym)
        if hit and (now - hit[0]) < ttl:
            return hit[1]

    try:
        r = httpx.get(BASE.format(sym=sym), headers={"User-Agent": _UA},
                      timeout=30.0, follow_redirects=True)
        r.raise_for_status()
        data = r.json()["data"]
    except httpx.HTTPStatusError as exc:
        # The CDN answers 403 (not 404) for a symbol it has no file for — measured
        # 2026-09-10 against a nonsense ticker. Both mean the same thing to a
        # member, and "HTTP 403" reads like a permissions problem with the feed
        # when it is really a typo in the ticker. Say which it is.
        if exc.response.status_code in (403, 404):
            raise ChainError(
                f"{symbol}: no Cboe option chain — check the ticker") from exc
        raise ChainError(f"{symbol}: Cboe HTTP {exc.response.status_code}") from exc
    except Exception as exc:  # noqa: BLE001  - network, JSON, missing key
        raise ChainError(f"{symbol}: {type(exc).__name__}: {exc}") from exc

    legs: dict[tuple, dict] = {}
    for o in data.get("options") or []:
        p = parse_occ(o.get("option") or "")
        if p is None:
            continue
        bid, ask = _num(o.get("bid")), _num(o.get("ask"))
        mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
        legs[(p["expiry"], p["right"], round(p["strike"], 3))] = {
            "expiry": p["expiry"], "right": p["right"], "strike": p["strike"],
            "bid": bid, "ask": ask, "mid": mid,
            "iv": _num(o.get("iv")), "delta": _num(o.get("delta")),
            "gamma": _num(o.get("gamma")), "theta": _num(o.get("theta")),
            "vega": _num(o.get("vega")), "theo": _num(o.get("theo")),
            "open_interest": _num(o.get("open_interest")),
            "volume": _num(o.get("volume")),
        }

    if not legs:
        raise ChainError(f"{symbol}: chain returned no parseable contracts")

    out = {
        "symbol": (symbol or "").strip().upper(),
        "spot": _num(data.get("current_price")),
        "iv30": _num(data.get("iv30")),
        "as_of": data.get("last_trade_time"),
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "legs": legs,
    }
    with _lock:
        _cache[sym] = (now, out)
    return out


def leg(chain: dict, expiry: str, right: str, strike: float) -> dict | None:
    """One contract out of a fetched chain, or None if it is not listed."""
    return (chain.get("legs") or {}).get(
        (expiry, (right or "P").upper(), round(float(strike), 3))
    )


def expiries(chain: dict, right: str = "P") -> list[str]:
    """Sorted expiries that actually carry contracts — used to tell 'you typed the
    wrong date' apart from 'the feed is empty'."""
    r = (right or "P").upper()
    return sorted({k[0] for k in (chain.get("legs") or {}) if k[1] == r})


def strikes(chain: dict, expiry: str, right: str = "P") -> list[float]:
    """Sorted listed strikes for one expiry — same purpose as ``expiries``."""
    r = (right or "P").upper()
    return sorted(k[2] for k in (chain.get("legs") or {})
                  if k[0] == expiry and k[1] == r)


def clear_cache() -> None:
    """Drop the in-process cache. For tests and for the daily sweep, which wants
    a genuinely fresh read rather than whatever a web request warmed 10 min ago."""
    with _lock:
        _cache.clear()
