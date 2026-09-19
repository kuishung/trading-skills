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

# The Sector & Industry ticker panel's four switchable conditions (user,
# 2026-09-15). Each can be turned on or off per member; the list is re-sorted
# by whichever are on. Condition 1 is "a must": when on, a ticker that fails
# it sinks below every ticker that passes, whatever else it has.
#   c1  EMA20 > EMA50 > EMA200
#   c2  price went 1-2% BELOW EMA20 or EMA50 recently (the dip)
#   c3  price now sits 0.3-2% ABOVE EMA20 or EMA50 (the rebound)
#   c4  price within 0.5% of a 10 / 50 / 100 round number
#   c5  price sits 0.3-1.5% BELOW EMA20 or EMA50
#   c6  DAILY pin bar (bullish hammer) at EMA20 or EMA50
#   c7  WEEKLY pin bar (bullish hammer) at the weekly EMA20 or EMA50
DIP_BARS = 5             # sessions the dip may sit back
DIP_MIN, DIP_MAX = 1.0, 2.0          # percent below the EMA
REB_MIN, REB_MAX = 0.3, 2.0          # percent above the EMA
BELOW_MIN, BELOW_MAX = 0.3, 1.5      # percent BELOW the EMA (c5: testing it from underneath)
# c6 / c7 (user, 2026-09-19): "filter pin bar (bullish hammer) forming in daily
# EMA20 EMA50 ... another one is for weekly EMA20 EMA50". Every threshold is a
# fraction of the bar's OWN range, so the same rule fits a $5 and a $500 ticker
# (CLAUDE.md: no absolute thresholds).
#   pin bar   lower wick >= 60% of the range, upper wick <= 20%, and the lower
#             wick at least twice the body - a long tail rejecting lower prices
#   at an EMA the tail reached the average (the low may stop up to a quarter of
#             the bar's range short of it) and the close held at or above it
# ONLY the most recent candle counts (user, 2026-09-19: "the pin bar d or w i
# want it to the last recent candle") - today's daily bar / this week's weekly
# bar, still in progress while the market is open. v4.111 also accepted the bar
# before it; that put stale hammers in the list, so it is gone.
PIN_BARS = 1             # the latest candle only
PIN_WICK_MIN = 0.60      # lower wick, fraction of the bar's range
PIN_UPPER_MAX = 0.20     # upper wick, fraction of the bar's range
PIN_BODY_WICK = 2.0      # lower wick >= this many bodies
PIN_NEAR = 0.25          # the low may stop this fraction of the range above the EMA
WEEKLY_MIN = 60          # weekly bars needed before the weekly EMA50 means anything
COND_KEYS = ("c1", "c2", "c3", "c4", "c5", "c6", "c7")
COND_LABELS = {
    "c1": ("EMA 20>50>200", "Uptrend: EMA20 above EMA50 above EMA200 on the last close. A must: when on, tickers that fail it sort below every ticker that passes."),
    "c2": ("dip 1-2%", f"Within the last {DIP_BARS} sessions a low reached 1-2% BELOW EMA20 (or EMA50): the pullback that sets up the rebound."),
    "c3": ("rebound 0.3-2%", "The last close sits 0.3-2% ABOVE EMA20 (or EMA50): price has come back off the average."),
    "c4": ("round 10/50/100", "The last close is within 0.5% of a multiple of 10, 50 or 100."),
    # c5 (user, 2026-09-15): "those tickers that do below EMA20 or EMA50 by
    # 0.3% to 1.5%" - price sitting just UNDER the average, the mirror of c3.
    "c5": ("below 0.3-1.5%", "The last close sits 0.3-1.5% BELOW EMA20 (or EMA50): price is testing the average from underneath, not yet back above it."),
    "c6": ("pin bar D", "DAILY pin bar (bullish hammer) at EMA20 or EMA50: the most recent daily candle (today's, still forming while the market is open) has a long lower tail (60%+ of its range, small upper wick, tail at least twice the body) that reached the average while the close held at or above it."),
    "c7": ("pin bar W", "WEEKLY pin bar (bullish hammer) at the WEEKLY EMA20 or EMA50: the most recent weekly candle (this week's, still forming until Friday's close) has a long lower tail that reached the weekly average while the close held at or above it."),
}
COND_DEFAULT = {"c1": True, "c2": True, "c3": True, "c4": True, "c5": True,
                "c6": True, "c7": True}
COND_WEIGHT = {"c1": 100, "c2": 30, "c3": 30, "c4": 20, "c5": 25, "c6": 35, "c7": 40}


def ema(values: list[float], period: int) -> list[float]:
    """EMA seeded with the first value - identical to MATP classify_trend.ema."""
    if not values:
        return []
    a = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def round_level(price: float, steps=(100, 50, 10, 5)) -> tuple[float, int] | None:
    """The strongest psychological level within ROUND_TOL of ``price``, as
    (level, step) with step in ``steps``. None if there is none."""
    if price is None or price < 5:
        return None
    for step in steps:
        lvl = round(price / step) * step
        if lvl >= 5 and abs(price - lvl) / price <= ROUND_TOL:
            return float(lvl), step
    return None


def is_pin_bar(o: float, h: float, l: float, c: float) -> bool:
    """A bullish hammer, judged only by the bar's own proportions (see PIN_*)."""
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    lower = min(o, c) - l
    upper = h - max(o, c)
    return (lower >= PIN_WICK_MIN * rng and upper <= PIN_UPPER_MAX * rng
            and lower >= PIN_BODY_WICK * body)


def pin_at_ema(ohlc: list[tuple], emas) -> dict | None:
    """The most recent pin bar, within the last PIN_BARS bars, whose tail tested
    one of ``emas`` ((name, series) pairs, tried in order) and closed at or above
    it. ``ohlc`` is [(time, o, h, l, c)]. ``{ema, ago, time, tail}`` or None;
    ``ago`` 0 = the latest (forming) bar, ``tail`` = lower wick as % of the range."""
    n = len(ohlc)
    for ago in range(min(PIN_BARS, n)):
        i = n - 1 - ago
        t, o, h, l, c = ohlc[i]
        if not is_pin_bar(o, h, l, c):
            continue
        rng = h - l
        for name, series in emas:
            e = series[i]
            if l <= e + PIN_NEAR * rng and c >= e:
                return {"ema": name, "ago": ago, "time": t,
                        "tail": round((min(o, c) - l) / rng * 100.0)}
    return None


def to_weekly(ohlc: list[tuple]) -> list[tuple]:
    """Daily [(time 'YYYY-MM-DD', o, h, l, c)] -> weekly bars by ISO week; the
    last one is the week in progress. ``time`` = the week's first session."""
    import datetime as _dt

    out: list[list] = []
    key = None
    for t, o, h, l, c in ohlc:
        try:
            k = _dt.date.fromisoformat(str(t)[:10]).isocalendar()[:2]
        except ValueError:
            continue
        if k != key:
            key = k
            out.append([t, o, h, l, c])
        else:
            w = out[-1]
            w[2], w[3], w[4] = max(w[2], h), min(w[3], l), c
    return [tuple(w) for w in out]


def _blank(reason: str = "not enough price history") -> dict:
    return {"dip_ema": None, "dip_pct": None, "above_ema": None, "above_pct": None,
            "below_ema": None, "below_pct": None, "pin_d": None, "pin_w": None,
            "round10": None, "round10_step": None,
            "score": None, "uptrend": None, "rebound": None, "rebound_pct": None,
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

    # --- the industry panel's conditions (see COND_LABELS) -------------------
    # c2: the deepest low under EMA20 (else EMA50) in the last DIP_BARS sessions
    dip_ema = dip_pct = None
    for name, series in (("EMA20", e20), ("EMA50", e50)):
        deepest = 0.0
        for i in range(max(0, len(closes) - DIP_BARS), len(closes)):
            if lows[i] < series[i]:
                deepest = max(deepest, (series[i] - lows[i]) / series[i] * 100.0)
        if DIP_MIN <= deepest <= DIP_MAX:
            dip_ema, dip_pct = name, round(deepest, 2)
            break
        if dip_ema is None and deepest > 0 and dip_pct is None:
            dip_pct = round(deepest, 2)     # report the depth even when out of band
    # c3: how far the close sits above the nearest EMA it is above
    above_ema = above_pct = None
    for name, series in (("EMA20", e20), ("EMA50", e50)):
        d = (c - series[-1]) / series[-1] * 100.0
        if d > 0 and (above_pct is None or d < above_pct):
            above_ema, above_pct = name, round(d, 2)
    # c5: how far the close sits BELOW the nearest EMA it is under
    below_ema = below_pct = None
    for name, series in (("EMA20", e20), ("EMA50", e50)):
        d = (series[-1] - c) / series[-1] * 100.0
        if d > 0 and (below_pct is None or d < below_pct):
            below_ema, below_pct = name, round(d, 2)
    # c4: 10 / 50 / 100 only (the Setup sort's 5s are too dense here)
    r10 = round_level(c, (100, 50, 10))
    # c6 / c7: a pin bar (bullish hammer) at the daily / the weekly EMA20 or EMA50.
    # Same index as ``closes`` (same filter), so the EMA series line up bar for bar.
    ohlc = []
    for b in bars:
        if b.get("close") is None:
            continue
        bc = float(b["close"])
        bo = float(b["open"]) if b.get("open") is not None else bc
        bh = float(b["high"]) if b.get("high") is not None else max(bo, bc)
        bl = float(b["low"]) if b.get("low") is not None else min(bo, bc)
        ohlc.append((b.get("time"), bo, bh, bl, bc))
    pin_d = pin_at_ema(ohlc, (("EMA20", e20), ("EMA50", e50)))
    pin_w = None
    weekly = to_weekly(ohlc)
    if len(weekly) >= WEEKLY_MIN:
        wcloses = [w[4] for w in weekly]
        pin_w = pin_at_ema(weekly, (("EMA20", ema(wcloses, 20)), ("EMA50", ema(wcloses, 50))))

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
        "dip_ema": dip_ema, "dip_pct": dip_pct,
        "above_ema": above_ema, "above_pct": above_pct,
        "below_ema": below_ema, "below_pct": below_pct,
        "pin_d": pin_d, "pin_w": pin_w,
        "round10": None if not r10 else r10[0], "round10_step": None if not r10 else r10[1],
        "score": score, "uptrend": uptrend, "rebound": rebound,
        "rebound_pct": None if dist is None else round(dist * 100, 3),
        "fresh": fresh, "held": held, "watch": watch,
        "round": None if not rl else rl[0], "round_step": None if not rl else rl[1],
        "close": c, "ema20": round(e20[-1], 2), "ema50": round(e50[-1], 2),
        "ema200": round(e200[-1], 2), "chips": chips, "summary": ", ".join(parts),
    }


def conditions(setup: dict) -> dict:
    """Which of the four industry-panel conditions this setup meets."""
    if setup.get("score") is None:
        return {k: None for k in COND_KEYS}
    return {
        "c1": bool(setup.get("uptrend")),
        "c2": setup.get("dip_ema") is not None,
        "c3": (setup.get("above_pct") is not None
               and REB_MIN <= setup["above_pct"] <= REB_MAX),
        "c4": setup.get("round10") is not None,
        "c5": (setup.get("below_pct") is not None
               and BELOW_MIN <= setup["below_pct"] <= BELOW_MAX),
        "c6": setup.get("pin_d") is not None,
        "c7": setup.get("pin_w") is not None,
    }


def clean_enabled(raw) -> dict:
    """A complete {c1..c4: bool} from prefs / a form; missing keys -> default."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k in COND_KEYS:
        v = raw.get(k, COND_DEFAULT[k])
        out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
    return out


def rank(setup: dict, enabled: dict) -> dict:
    """Score a ticker against the ENABLED conditions.

    ``{score, met: {c: bool}, chips: [...], gated: bool}``. A ticker with no
    price history scores None and sorts last. Condition 1, when enabled, is a
    gate: failing it costs more than every other condition can pay, so such a
    ticker can never rank above one that passes. Ties break towards the
    tighter rebound (smallest distance above its EMA) in the caller.
    """
    met = conditions(setup)
    if setup.get("score") is None:
        return {"score": None, "met": met, "chips": [], "gated": False,
                "summary": setup.get("summary") or "no price history"}
    score = 0
    chips: list[dict] = []
    gated = False
    if enabled.get("c1"):
        if met["c1"]:
            score += COND_WEIGHT["c1"]
            chips.append({"t": "EMA stack", "k": "good",
                          "title": f"EMA20 {setup['ema20']} > EMA50 {setup['ema50']} > EMA200 {setup['ema200']}"})
        else:
            score -= 1000
            gated = True
    if enabled.get("c2") and met["c2"]:
        score += COND_WEIGHT["c2"]
        chips.append({"t": f"dip {setup['dip_pct']:.1f}% under {setup['dip_ema']}", "k": "warm",
                      "title": f"A low in the last {DIP_BARS} sessions reached {setup['dip_pct']:.2f}% below {setup['dip_ema']}"})
    if enabled.get("c3") and met["c3"]:
        score += COND_WEIGHT["c3"]
        chips.append({"t": f"rebound +{setup['above_pct']:.1f}% {setup['above_ema']}", "k": "hot",
                      "title": f"Close is {setup['above_pct']:.2f}% above {setup['above_ema']}"})
    if enabled.get("c4") and met["c4"]:
        score += COND_WEIGHT["c4"]
        chips.append({"t": f"round {setup['round10']:g}", "k": "round",
                      "title": f"Close {setup['close']:.2f} is within 0.5% of {setup['round10']:g}"})
    if enabled.get("c5") and met["c5"]:
        score += COND_WEIGHT["c5"]
        chips.append({"t": f"below -{setup['below_pct']:.1f}% {setup['below_ema']}", "k": "watch",
                      "title": f"Close is {setup['below_pct']:.2f}% below {setup['below_ema']} - testing it from underneath"})
    for key, field, word, tf in (("c6", "pin_d", "daily", "D"), ("c7", "pin_w", "weekly", "W")):
        if enabled.get(key) and met[key]:
            p = setup[field]
            start = "candle of" if key == "c6" else "week starting"
            score += COND_WEIGHT[key]
            chips.append({"t": f"pin bar {tf} {p['ema']}", "k": "pin",
                          "title": f"Bullish hammer on the most recent {word} candle ({start} "
                                   f"{p['time']}): the lower tail is {p['tail']}% of the bar's range, "
                                   f"it reached the {word} {p['ema']} and the close held at or above it"})
    # Qualifies = passes the gate (if on) AND meets at least one of the other
    # switched-on conditions (or none of the others are on). The list FADES
    # tickers that do not (user, 2026-09-15: "those not qualifying to the
    # technical will be faded"), so a sector-wide list still reads at a glance.
    others = [k for k in COND_KEYS if k != "c1" and enabled.get(k)]
    qualifies = (not gated) and (not others or any(met[k] for k in others))
    return {"score": score, "met": met, "chips": chips, "gated": gated,
            "qualifies": qualifies, "summary": setup.get("summary") or ""}


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
