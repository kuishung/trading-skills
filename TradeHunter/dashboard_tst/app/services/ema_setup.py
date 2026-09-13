"""EMA-rebound setup ranking for a list of tickers (the Sector ETFs holdings panel).

The user's rule, 2026-09-13, for sorting a fund's holdings:

    1. Uptrend: EMA20 > EMA50 > EMA200.
    2. Rebound on an EMA - either EMA20 or EMA50.
    3. The rebound is only NEW while the close sits 0.1% to 0.5% above the EMA.
    4. A price that coincides with a psychological round number (5, 10 ...)
       ranks higher.

This is MATP's "buy the dip in an uptrend" entry (resources/MATP
daily_bounce_alert: HOT / WARM / WATCHING) restated as a sortable score, so the
best-looking setups in a sector float to the top of the list instead of being
found by opening seventy charts.

Definitions, so the score can be argued with:

* **Uptrend** - strictly EMA20 > EMA50 > EMA200 on the last daily close. The
  EMAs are seeded like MATP's ``classify_trend`` (first value), so the stack
  agrees with the trend states shown elsewhere.
* **Rebound** - within the last ``TOUCH_BARS`` sessions a bar's LOW reached the
  EMA (low <= EMA of that day) and the close held above it; the latest close is
  above the EMA. Checked on EMA20 first, then EMA50; the nearer one is reported.
* **Fresh** - the latest close is ``FRESH_MIN`` to ``FRESH_MAX`` above that EMA
  (0.1% to 0.5%). Above 0.5% the bounce has already run and the entry is late;
  below 0.1% it has not yet held.
* **Watching** - no touch yet, but the close is within ``WATCH_PCT`` above an EMA
  in an uptrend: the setup may form in the next session.
* **Round number** - the close is within ``ROUND_TOL`` of a multiple of 5; a
  multiple of 10 counts more, 50 / 100 more still. Levels below $5 are ignored.

Everything is computed from LIVE daily bars (``services.prices``, Yahoo), never
parquet - this is an operational "now" view (CLAUDE.md scope rule). Cached per
symbol for 15 minutes like the structure monitor.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from .prices import fetch_daily_ohlc

TOUCH_BARS = 3          # a touch this many sessions back still counts as "the rebound"
FRESH_MIN = 0.001       # 0.1%
FRESH_MAX = 0.005       # 0.5%
HOLD_MAX = 0.03         # a rebound that has run more than 3% is no longer a rebound
WATCH_PCT = 0.015       # within 1.5% above an EMA, no touch yet
ROUND_TOL = 0.005       # within 0.5% of a round number

# Score weights. Uptrend dominates; within uptrends a FRESH rebound beats a held
# one beats a forming one; a round number breaks ties upward.
W_UPTREND = 100
W_FRESH = 50
W_HELD = 25
W_WATCH = 10
W_ROUND = {5: 8, 10: 15, 50: 20, 100: 25}

_TTL = 900.0
_cache: dict[str, tuple[float, dict]] = {}


def ema(values: list[float], period: int) -> list[float]:
    """EMA seeded with the first value - identical to MATP classify_trend.ema."""
    if not values:
        return []
    a = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def round_level(price: float) -> tuple[float, int] | None:
    """The strongest psychological level within ROUND_TOL of ``price``, as
    (level, step) with step in {100, 50, 10, 5}. None if there is none."""
    if price is None or price < 5:
        return None
    for step in (100, 50, 10, 5):
        lvl = round(price / step) * step
        if lvl >= 5 and abs(price - lvl) / price <= ROUND_TOL:
            return float(lvl), step
    return None


def _blank(reason: str = "not enough price history") -> dict:
    return {"score": None, "uptrend": None, "rebound": None, "rebound_pct": None,
            "fresh": False, "held": False, "watch": False, "round": None,
            "round_step": None, "close": None, "ema20": None, "ema50": None,
            "ema200": None, "chips": [], "summary": reason}


def analyze(bars: list[dict]) -> dict:
    """Score one ticker from ``/prices``-shaped daily bars (time/open/high/low/close)."""
    closes = [float(b["close"]) for b in bars if b.get("close") is not None]
    if len(closes) < 60:
        return _blank()
    lows = [float(b.get("low", b["close"])) for b in bars if b.get("close") is not None]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    c = closes[-1]
    uptrend = e20[-1] > e50[-1] > e200[-1]
    enough_for_200 = len(closes) >= 200

    # Rebound: EMA20 first, then EMA50. A touch = a bar whose low reached the EMA
    # of that day while its close held above it, within the last TOUCH_BARS.
    rebound = None
    dist = None
    for name, series in (("EMA20", e20), ("EMA50", e50)):
        if c <= series[-1]:
            continue
        touched = any(lows[i] <= series[i] <= closes[i]
                      for i in range(max(0, len(closes) - TOUCH_BARS), len(closes)))
        if touched:
            rebound = name
            dist = (c - series[-1]) / series[-1]
            break
    fresh = rebound is not None and FRESH_MIN <= dist <= FRESH_MAX
    held = rebound is not None and not fresh and dist <= HOLD_MAX
    if rebound is not None and dist > HOLD_MAX:
        rebound, dist = None, None      # it bounced, but that was a while ago in price terms

    # Watching: no rebound, but sitting just above an EMA (nearest one wins).
    watch = None
    watch_dist = None
    if rebound is None:
        for name, series in (("EMA20", e20), ("EMA50", e50)):
            d = (c - series[-1]) / series[-1]
            if 0 <= d <= WATCH_PCT and (watch_dist is None or d < watch_dist):
                watch, watch_dist = name, d

    rl = round_level(c)

    score = 0
    chips: list[dict] = []
    if uptrend:
        score += W_UPTREND
        chips.append({"t": "EMA stack", "k": "good",
                      "title": f"EMA20 {e20[-1]:.2f} > EMA50 {e50[-1]:.2f} > EMA200 {e200[-1]:.2f}"})
    if rebound:
        if fresh:
            score += W_FRESH
            chips.append({"t": f"{rebound} rebound +{dist * 100:.1f}%", "k": "hot",
                          "title": f"Touched {rebound} within the last {TOUCH_BARS} sessions and "
                                   f"closed {dist * 100:.2f}% above it - inside the 0.1-0.5% fresh band"})
        else:
            score += W_HELD
            chips.append({"t": f"{rebound} held +{dist * 100:.1f}%", "k": "warm",
                          "title": f"Bounced off {rebound}; close is {dist * 100:.2f}% above it, "
                                   "past the 0.5% fresh band"})
    elif watch:
        score += W_WATCH
        chips.append({"t": f"near {watch} +{watch_dist * 100:.1f}%", "k": "watch",
                      "title": f"No touch yet; close is {watch_dist * 100:.2f}% above {watch}"})
    if rl:
        lvl, step = rl
        score += W_ROUND[step]
        chips.append({"t": f"round {lvl:g}", "k": "round",
                      "title": f"Close {c:.2f} is within 0.5% of the {step}-multiple {lvl:g}"})

    parts = []
    parts.append("uptrend" if uptrend else ("EMA stack not aligned" if enough_for_200
                                             else "EMA stack (short history)"))
    if rebound:
        parts.append(f"{'fresh ' if fresh else ''}{rebound} rebound {dist * 100:+.2f}%")
    elif watch:
        parts.append(f"near {watch} {watch_dist * 100:+.2f}%")
    if rl:
        parts.append(f"round {rl[0]:g}")
    return {
        "score": score, "uptrend": uptrend, "rebound": rebound,
        "rebound_pct": None if dist is None else round(dist * 100, 3),
        "fresh": fresh, "held": held, "watch": watch,
        "round": None if not rl else rl[0], "round_step": None if not rl else rl[1],
        "close": c, "ema20": round(e20[-1], 2), "ema50": round(e50[-1], 2),
        "ema200": round(e200[-1], 2), "chips": chips, "summary": ", ".join(parts),
    }


def setup_for(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    if not sym:
        return _blank("no symbol")
    now = time.time()
    hit = _cache.get(sym)
    if hit and hit[0] > now:
        return hit[1]
    try:
        out = analyze(fetch_daily_ohlc(sym, rng="2y"))
    except Exception:  # noqa: BLE001
        out = _blank("price history unavailable")
    _cache[sym] = (now + _TTL, out)
    return out


def setups_for_many(symbols) -> dict[str, dict]:
    """Concurrent, like structure_for_many: a fund's 70 holdings cost a few
    seconds cold and nothing warm (the price cache is shared with the charts)."""
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(syms, ex.map(setup_for, syms)))
