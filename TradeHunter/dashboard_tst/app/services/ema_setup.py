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

from . import support_bounce as sb
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
# w1 - the WEEKLY SETUP (user, 2026-09-20: "i want a separate setup for pin bar
# forming in the Weekly EMA 20 or EMA50 provided EMA20 > EMA50 > EMA200"). Unlike
# c1-c7 it is not one ingredient but a whole setup, BOTH halves required:
#   * the weekly trend is stacked - weekly EMA20 > EMA50 > EMA200 - and
#   * the most recent weekly candle is a pin bar at the weekly EMA20 or EMA50.
# It stands on its own: it is judged on the weekly chart only, so the DAILY gate
# (c1) does not apply to a ticker that meets it, and it qualifies a ticker by
# itself. A weekly EMA200 is ~4 years of candles, which the 2-year fetch c1-c7 run
# on cannot give, so switching w1 on makes the scan read ~10 years per ticker
# instead (ONE fetch, not an extra one - the daily conditions are computed on its
# last 2 years and come out the same). A ticker listed for under WEEKLY_200_MIN
# weeks cannot be judged and is reported as such, never passed by default.
WEEKLY_200_MIN = 200     # weekly bars needed before the weekly EMA200 means anything
DEEP_RANGE = "10y"
# s1 - the SUPPORT BOUNCE setup (user, 2026-09-22, for the bull put spread): a
# setup of its own like w1, all of these required -
#   * EMA20 > EMA50 > EMA200 on the daily,
#   * the most recent daily candle is a bullish pin bar or a bullish engulfing
#     candle whose low tested a HORIZONTAL support level - a price where the stock
#     turned up at least once before in the last year (see support_bounce),
#   * that candle traded on HIGH volume (vs the ticker's own recent sessions).
# and these grade it higher when present (the user's "bonus" / "preferable"):
#   * two or more previous touches of the level,
#   * the level coincides with the daily EMA20 or EMA50,
#   * the level coincides with the weekly EMA20 or EMA50.
# The candle pattern and the volume are judged on the LATEST candle only, like
# c6 / c7. A bounce that has everything but the volume is shown as a near miss
# (a lighter chip, no qualification) so the member can see it forming.
COND_KEYS = ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "s1", "w1")
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
    "w1": ("W setup", "WEEKLY SETUP - a setup of its own, both parts required: the weekly trend is stacked (WEEKLY EMA20 > EMA50 > EMA200) AND the most recent weekly candle is a pin bar (bullish hammer) at the weekly EMA20 or EMA50. Judged on the weekly chart only: it qualifies a ticker by itself and the daily 'EMA 20>50>200' must does not apply to it. Reads about 10 years of history per ticker (a weekly EMA200 needs ~4), so the first scan with it on is slower; tickers listed under 4 years cannot be judged."),
    "s1": ("Support bounce", "SUPPORT BOUNCE - the bull-put-spread setup, all three required: EMA20 > EMA50 > EMA200 on the daily; the most recent daily candle is a bullish pin bar or a bullish engulfing candle whose low tested a HORIZONTAL support (a price the stock turned up from at least once before in the last year); and that candle traded on high volume for this ticker. Graded higher when the level has 2+ previous touches, and when it coincides with the daily EMA20 / EMA50 or the weekly EMA20 / EMA50. Click the ticker: the chart draws the level, its touches and the bounce candle. A bounce with everything but the volume shows as a lighter chip and does not qualify."),
}
# w1 starts OFF: it changes what the scan downloads, so it is switched on by the
# member who wants it rather than landing on everyone's lists unasked. s1 costs
# nothing extra (same 2-year fetch), so it starts ON.
COND_DEFAULT = {"c1": True, "c2": True, "c3": True, "c4": True, "c5": True,
                "c6": True, "c7": True, "s1": True, "w1": False}
COND_WEIGHT = {"c1": 100, "c2": 30, "c3": 30, "c4": 20, "c5": 25, "c6": 35, "c7": 40,
               "s1": 110, "w1": 120}
# s1's bonuses, on top of COND_WEIGHT["s1"]: each previous touch past the first,
# the daily-EMA confluence, the weekly-EMA confluence; and the near miss (bounce
# at support, volume not high) on its own.
S1_BONUS_TOUCH = 10
S1_BONUS_DEMA = 15
S1_BONUS_WEMA = 20
S1_NEAR_MISS = 30
# Which switches need the long history.
DEEP_KEYS = ("w1",)


def needs_deep(enabled: dict | None) -> bool:
    """Does this member's switch set need the ~10-year fetch?"""
    return any((enabled or {}).get(k) for k in DEEP_KEYS)


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
            "sup": None, "avg_vol20": None,
            "w_uptrend": None, "w_setup": None, "w_weeks": 0, "w_note": "",
            "w_ema20": None, "w_ema50": None, "w_ema200": None,
            "round10": None, "round10_step": None,
            "score": None, "uptrend": None, "rebound": None, "rebound_pct": None,
            "fresh": False, "held": False, "watch": False, "round": None,
            "round_step": None, "close": None, "ema20": None, "ema50": None,
            "ema200": None, "chips": [], "summary": reason}


def _ohlc(bars: list[dict]) -> list[tuple]:
    """``/prices`` dicts -> [(time, o, h, l, c)], skipping bars with no close."""
    out = []
    for b in bars:
        if b.get("close") is None:
            continue
        bc = float(b["close"])
        bo = float(b["open"]) if b.get("open") is not None else bc
        bh = float(b["high"]) if b.get("high") is not None else max(bo, bc)
        bl = float(b["low"]) if b.get("low") is not None else min(bo, bc)
        out.append((b.get("time"), bo, bh, bl, bc))
    return out


def analyze(bars: list[dict], long_bars: list[dict] | None = None) -> dict:
    """Score one ticker from ``/prices``-shaped daily bars (time/open/high/low/close).

    ``bars`` is the ~2-year window every daily condition is read from.
    ``long_bars`` is the same ticker over ~10 years, when the caller fetched it
    (``deep``): the WEEKLY reads then come from it, because a weekly EMA200 cannot
    be built from 104 candles. Without it the weekly pin bar still works off
    ``bars`` and the weekly setup (w1) reports that it could not be judged.
    """
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
    ohlc = _ohlc(bars)
    pin_d = pin_at_ema(ohlc, (("EMA20", e20), ("EMA50", e50)))
    # Weekly reads. ONE weekly series feeds both the weekly pin bar (c7) and the
    # weekly setup (w1), so the two can never disagree about the same candle - and
    # when the long history is present it is the series the W chart draws.
    pin_w = None
    w_uptrend = w_setup = None
    w20 = w50 = w200 = None
    w_note = ""
    weekly = to_weekly(_ohlc(long_bars) if long_bars else ohlc)
    if len(weekly) >= WEEKLY_MIN:
        wcloses = [w[4] for w in weekly]
        we20, we50 = ema(wcloses, 20), ema(wcloses, 50)
        pin_w = pin_at_ema(weekly, (("EMA20", we20), ("EMA50", we50)))
        w20, w50 = round(we20[-1], 2), round(we50[-1], 2)
        if len(weekly) >= WEEKLY_200_MIN:
            w200v = ema(wcloses, 200)[-1]
            w200 = round(w200v, 2)
            w_uptrend = we20[-1] > we50[-1] > w200v
            w_setup = bool(w_uptrend and pin_w is not None)
        else:
            w_note = (f"only {len(weekly)} weekly candles - a weekly EMA200 needs "
                      f"{WEEKLY_200_MIN}" + ("" if long_bars else " (long history not loaded)"))
    else:
        w_note = f"only {len(weekly)} weekly candles"
    # s1: the support bounce (services/support_bounce.py) on the latest candle,
    # with the daily / weekly EMA20-50 values for its "coincides with" read.
    # Judged regardless of the trend; conditions() adds the uptrend requirement.
    sup = None
    try:
        w_pairs = (("EMA20", we20[-1]), ("EMA50", we50[-1])) if len(weekly) >= WEEKLY_MIN else ()
        sup = sb.find(bars, (("EMA20", e20[-1]), ("EMA50", e50[-1])), w_pairs)
    except Exception:  # noqa: BLE001  - a detector must never take the whole setup down
        sup = None
    # 20-session average volume of COMPLETED sessions (the option-pair floor on
    # the IV Rank page's My list). None when the feed carried no volume.
    done = bars[:-1] if bars[-1].get("session_frac") is not None else bars
    vols = [b["volume"] for b in done[-20:] if b.get("volume")]
    avg_vol20 = int(sum(vols) / len(vols)) if len(vols) >= 10 else None

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
        "sup": sup, "avg_vol20": avg_vol20,
        "w_uptrend": w_uptrend, "w_setup": w_setup, "w_weeks": len(weekly), "w_note": w_note,
        "w_ema20": w20, "w_ema50": w50, "w_ema200": w200,
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
        # the full setup: uptrend + bounce candle at a tested level + high volume.
        # A bounce whose volume is not high (or not yet readable) is a near miss,
        # shown but not met - see rank().
        "s1": bool(setup.get("uptrend") and setup.get("sup") and setup["sup"].get("vol_high")),
        "w1": bool(setup.get("w_setup")),      # None (could not be judged) is not a pass
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
    # The weekly setup is a setup of its own (see w1 above): a ticker that meets it
    # is not subject to the DAILY gate, and the chip leads the row.
    w_hit = bool(enabled.get("w1") and met["w1"])
    if w_hit:
        p = setup["pin_w"]
        score += COND_WEIGHT["w1"]
        chips.append({"t": f"W setup · pin bar w{p['ema']}", "k": "wset",
                      "title": f"WEEKLY SETUP: weekly EMA20 {setup['w_ema20']} > EMA50 {setup['w_ema50']} "
                               f"> EMA200 {setup['w_ema200']}, and the weekly candle starting {p['time']} "
                               f"is a bullish hammer (tail {p['tail']}% of its range) that reached the "
                               f"weekly {p['ema']} and closed at or above it"})
    # The support bounce (s1): the full setup leads the row in its own colour; a
    # near miss (everything but the volume) gets a lighter chip and no score to
    # speak of, so it is visible without outranking a setup that is complete.
    sup = setup.get("sup")
    if enabled.get("s1") and sup and setup.get("uptrend"):
        n = sup.get("n_touches") or 0
        n_flip = sup.get("n_flip") or 0
        # "x3 (2 R>S)": how many of the touches are an old resistance retested
        # from above - a level made only of old highs is a first retest, not a
        # defended support, and the member should see that at a glance
        xtxt = f"x{n}" + (f" ({n_flip} R→S)" if n_flip else "")
        b = sup.get("bounce") or {}
        kind = "engulfing" if b.get("kind") == "engulf" else "pin bar"
        vr = sup.get("vol_ratio")
        vtxt = f"{vr:.1f}x vol" if vr is not None else "vol n/a"
        conf = ""
        if sup.get("d_ema"):
            conf += f" ≈ {sup['d_ema']}"
        if sup.get("w_ema"):
            conf += f" ≈ w{sup['w_ema']}"
        touches = ", ".join(t["time"] + (" (old resistance)" if t.get("kind") == "flip" else "")
                            for t in (sup.get("touches") or []))
        story = (f"Support {sup['level']:.2f}: {n} previous touch{'es' if n != 1 else ''} in the last year "
                 f"({touches}). The latest candle ({b.get('time')}) is a bullish {kind} whose low "
                 f"{b.get('low')} tested it and closed above it")
        if sup.get("d_ema"):
            conf_d = f"; the level sits on the daily {sup['d_ema']} {setup.get('ema20' if sup['d_ema'] == 'EMA20' else 'ema50')}"
            story += conf_d
        if sup.get("w_ema"):
            story += f"; and on the weekly {sup['w_ema']}"
        if met["s1"]:
            score += (COND_WEIGHT["s1"] + S1_BONUS_TOUCH * max(0, n - 1)
                      + (S1_BONUS_DEMA if sup.get("d_ema") else 0)
                      + (S1_BONUS_WEMA if sup.get("w_ema") else 0))
            chips.append({"t": f"support bounce {sup['level']:g} · {xtxt} · {kind} · {vtxt}{conf}",
                          "k": "sup",
                          "title": story + f". Volume {vtxt} the average of the 20 sessions before it - "
                                   "high for this ticker (its top 15% of days, at least 1.2x)"
                                   + (" (projected from the part of today's session traded so far)"
                                      if sup.get("vol_projected") else "") + " - a defended level."})
        else:
            score += S1_NEAR_MISS
            why = ("volume not readable yet - too early in today's session"
                   if sup.get("vol_high") is None and sup.get("vol_projected") is not None
                   else ("no volume figure" if vr is None else f"only {vtxt}, not a high-volume bounce"))
            chips.append({"t": f"support bounce {sup['level']:g} · {xtxt} · {kind} · light vol{conf}",
                          "k": "supx",
                          "title": story + f". NEAR MISS: {why}."})
    if enabled.get("c1"):
        if met["c1"]:
            score += COND_WEIGHT["c1"]
            chips.append({"t": "EMA stack", "k": "good",
                          "title": f"EMA20 {setup['ema20']} > EMA50 {setup['ema50']} > EMA200 {setup['ema200']}"})
        elif not w_hit:
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
            if key == "c7" and w_hit:
                continue        # the W-setup chip already names this candle and its EMA
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


def _last_two_years(long_bars: list[dict]) -> list[dict]:
    """The slice of a long daily history that a ``rng="2y"`` fetch would have
    returned, so the daily conditions come out the same whichever was fetched."""
    if not long_bars:
        return long_bars
    import datetime as _dt

    try:
        last = _dt.date.fromisoformat(str(long_bars[-1]["time"])[:10])
    except (KeyError, TypeError, ValueError):
        return long_bars
    try:
        cut = last.replace(year=last.year - 2)
    except ValueError:                      # 29 Feb -> 28 Feb two years back
        cut = last.replace(year=last.year - 2, day=28)
    iso = cut.isoformat()
    # strictly after: Yahoo's own "2y" starts the day AFTER the two-year mark
    # (measured: 501 bars either way; ">=" gave 502 and moved EMA200 by a cent)
    return [b for b in long_bars if str(b.get("time", "")) > iso]


def setup_for(symbol: str, deep: bool = False) -> dict:
    """One ticker's setup. ``deep`` reads ~10 years instead of 2 (needed by the
    weekly setup, w1) - ONE fetch either way: the daily conditions are computed on
    the last two years of it. Cached per (symbol, deep); a deep result also serves
    a shallow request, since it contains everything the shallow one has."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return _blank("no symbol")
    now = time.time()
    for key in ((sym, True),) if deep else ((sym, False), (sym, True)):
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    try:
        if deep:
            long_bars = fetch_daily_ohlc(sym, rng=DEEP_RANGE)
            out = analyze(_last_two_years(long_bars), long_bars=long_bars)
        else:
            out = analyze(fetch_daily_ohlc(sym, rng="2y"))
    except Exception:  # noqa: BLE001
        out = _blank("price history unavailable")
    _cache[(sym, bool(deep))] = (now + _TTL, out)
    return out


def setups_for_many(symbols, deep: bool = False) -> dict[str, dict]:
    """Concurrent, like structure_for_many: a fund's 70 holdings cost a few
    seconds cold and nothing warm (the price cache is shared with the charts).
    ``deep`` = ``needs_deep(enabled)``: see ``setup_for``."""
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(syms, ex.map(lambda s: setup_for(s, deep), syms)))
