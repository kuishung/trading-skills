"""The SUPPORT BOUNCE - a bullish pin bar or engulfing candle at a horizontal support.

The user's bull-put-spread entry (2026-09-22), in their words:

    2. Bouncing off horizontal support - if the support coincides with the daily
       EMA20 or EMA50 that is an added bonus.
    3. If the daily support coincides with the weekly EMA20 or EMA50 - added
       advantage.
    4. Bouncing off means a bullish pin bar or a bullish engulfing candle (mark-up
       bar) formed at the support level.
    5. The horizontal support preferably has 2 and above previous touching points
       in the 1 year [daily] time frame.
    6. The bouncing-off candle records high volume, so the bounce has solid
       participants defending the level.

(Rule 1, EMA20 > EMA50 > EMA200, is the caller's - ``ema_setup`` already reads it.)

This module answers: does the LATEST daily candle bounce off a horizontal level
that price has turned up from before? ``find()`` is pure arithmetic over the
``/prices``-shaped bars (time/open/high/low/close/volume) and returns the level,
its previous touches, the bounce candle and the volume read - or None.

How a level is found
--------------------
* **Touch candidates** are swing points of the prior 252 sessions (one year),
  ending ``MIN_SEP`` bars before the bounce candle - anything closer is the same
  visit, not history. A swing LOW has no lower low within ``PIVOT`` bars either
  side (``<=``, so two EQUAL lows - the double bottom - both survive; a strict
  test would lose exactly the case the rule is about). A swing HIGH is the
  mirror: an old resistance that price later broke above and may now be
  retesting from above - the "flip" touch. Either must be a real REACTION, not a
  wobble: price came at least ``REACT_ATR`` into the point and left by at least
  as much within ``REACT_BARS`` bars.
* **Clustering**: every candidate the bounce could have reached seeds a level;
  its members are the candidates within ``TOL_ATR`` (a hand-drawn line has
  width). The level is the mean of the members' prices.
* **Distinct events**: two members closer than ``MIN_SEP`` bars, or with no
  excursion of ``SEP_ATR`` away from the level between them, are ONE touch
  (a week sitting on a price is one test of it). The last touch must be followed
  by a rally of ``SEP_ATR`` above the level before the bounce - otherwise the
  bounce is the tail of that same visit.
* **Invalidation**: a level price then LIVED UNDER - ``BREAK_BARS`` consecutive
  closes more than ``BREAK_ATR`` below it - was broken; that touch and every
  older "low" touch are dropped. A single close below is a spring, still
  support. A "flip" touch counts only once price has closed back above the
  level (the breakout that turned it) and is kept through a break: it is what
  price did while under.
* **The test**: the bounce candle's low (for an engulfing, the lower of its two
  lows - the engulfed candle is the one that tapped the level) reached the zone
  from above (no more than ``REACH_ATR`` short of it) and speared no deeper than
  ``PIERCE_ATR`` through it; the close held at or above the level.
* **Choice** among levels the candle tested: most touches, then the nearest.

Every threshold is a multiple of the ticker's own ATR(14) (Wilder), a fraction
of the candle's own range, or a ratio to the ticker's own average volume - no
dollars, no share counts (CLAUDE.md: ticker-relative thresholds only).

Volume: ``vol_ratio`` = the bounce candle's volume over the mean of the 20
sessions before it; ``vol_high`` = at least ``VOL_HIGH`` times that mean (roughly
+1.5 sigma for a large cap's day-to-day scatter - the bar visibly stands above
its neighbours). While today's session is open the bar holds only the volume
traded SO FAR, so it is projected to a full day from ``session_frac`` (see
``services.prices``) once at least ``VOL_MIN_FRAC`` of the session has run;
earlier than that the read is "not yet" (None), never a verdict.

The candle patterns:
* pin bar - the app's own rule, verbatim from ``ema_setup``: lower wick at least
  60% of the range, upper wick at most 20%, lower wick at least twice the body.
* bullish engulfing (the user's "mark-up bar") - a green candle whose body
  swallows the prior candle's body: prior candle red or a doji; open at or below
  the prior body's bottom AND close at or above its top (equal allowed - at two
  decimals an open exactly on the prior close is the norm) with a STRICTLY
  bigger body (an identical body swallows nothing); and decisive - body at least
  half its own range (it did not close in its wicks) and at least
  ``ENGULF_BODY_ATR`` of a day's range (a big candle swallowing a doji is not a
  mark-up bar).
"""
from __future__ import annotations

import datetime as _dt

LOOKBACK = 252          # sessions of history a touch may come from (one year)
PIVOT = 3               # a swing point has no lower low / higher high this many bars either side
MIN_SEP = 5             # bars between distinct touches, and before the bounce candle
REACT_BARS = 10         # the approach / departure of a swing is measured over this many bars
REACT_ATR = 1.0         # ... and must be at least this much (x ATR at the swing)
TOL_ATR = 0.35          # cluster half-width (x ATR): the line's thickness
SEP_ATR = 1.0           # two touches need an excursion this far from the level between them
REACH_ATR = 0.25        # the bounce low may stop this far ABOVE the level and still have tested it
PIERCE_ATR = 0.5        # ... and may spear this far BELOW it (a spring); deeper is a break
BREAK_ATR = 0.5         # closes more than this below the level ...
BREAK_BARS = 3          # ... for this many consecutive sessions = lived under it = broken
ENGULF_BODY_RNG = 0.5   # an engulfing body is at least half its own range
ENGULF_BODY_ATR = 0.5   # ... and at least half a normal day's range
VOL_LOOKBACK = 20       # sessions the average volume is taken over
VOL_HIGH = 1.5          # x that average = high volume
VOL_MIN_FRAC = 0.25     # today's volume is projected only after this much of the session
DEMA_TOL_ATR = 0.4      # the level "coincides" with a daily EMA within this
WEMA_TOL_ATR = 0.5      # ... and with a weekly EMA within this (a coarser object)
MIN_BARS = 60


def is_pin_bar(o: float, h: float, l: float, c: float) -> bool:
    """A bullish hammer, judged only by the bar's own proportions (= ema_setup)."""
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    lower = min(o, c) - l
    upper = h - max(o, c)
    return lower >= 0.60 * rng and upper <= 0.20 * rng and lower >= 2.0 * body


def is_engulfing(prev: tuple, cur: tuple, atr: float) -> bool:
    """Bullish engulfing / mark-up bar (see the module docstring). ``prev`` and
    ``cur`` are (o, h, l, c)."""
    po, _, _, pc = prev
    o, h, l, c = cur
    rng = h - l
    if rng <= 0 or not (c > o and pc <= po):
        return False
    body, pbody = c - o, po - pc
    return (o <= min(po, pc) and c >= max(po, pc) and body > pbody
            and body >= ENGULF_BODY_RNG * rng and body >= ENGULF_BODY_ATR * atr)


def atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list:
    """Wilder ATR: SMA of the first ``period`` true ranges, then the running
    average. ``None`` until it exists (the same series the chart's ATR line is)."""
    n = len(closes)
    out: list = [None] * n
    if n < period + 1:
        return out
    trs = [0.0] * n
    for i in range(1, n):
        pc = closes[i - 1]
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
    a = sum(trs[1:period + 1]) / period
    out[period] = a
    for i in range(period + 1, n):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def _swings(highs, lows, atr, lo_i, hi_i) -> list[tuple]:
    """Swing lows ("low") and swing highs ("flip") in [lo_i, hi_i] that were real
    reactions, as (index, price, kind). Tie-tolerant."""
    out = []
    n = len(lows)
    for i in range(lo_i, hi_i + 1):
        a = atr[i]
        if not a:
            continue
        li, hi = lows[i], highs[i]
        is_low = is_high = True
        for j in range(max(0, i - PIVOT), min(n - 1, i + PIVOT) + 1):
            if j == i:
                continue
            if lows[j] < li:
                is_low = False
            if highs[j] > hi:
                is_high = False
            if not (is_low or is_high):
                break
        if not (is_low or is_high):
            continue
        need = REACT_ATR * a
        before_h = highs[max(0, i - REACT_BARS):i]
        after_h = highs[i + 1:i + REACT_BARS + 1]
        before_l = lows[max(0, i - REACT_BARS):i]
        after_l = lows[i + 1:i + REACT_BARS + 1]
        if is_low and before_h and after_h:
            if max(before_h) - li >= need and max(after_h) - li >= need:
                out.append((i, li, "low"))
        if is_high and before_l and after_l:
            if hi - min(before_l) >= need and hi - min(after_l) >= need:
                out.append((i, hi, "flip"))
    return out


def _touches(members, highs, lows, closes, atr, level, tol, end) -> list[tuple]:
    """Distinct, still-valid touches of ``level`` among cluster ``members`` (sorted
    by index). ``end`` = the last bar that is "before the bounce"."""
    # merge into distinct events: MIN_SEP bars apart AND an excursion away between
    events: list[list] = []          # [idx, price, kind, last_member_idx]
    for i, p, kind in members:
        if events:
            e = events[-1]
            j = e[3]
            between_h = max(highs[j + 1:i]) if i > j + 1 else highs[i]
            between_l = min(lows[j + 1:i]) if i > j + 1 else lows[i]
            away = between_h >= level + SEP_ATR * atr[i] or between_l <= level - SEP_ATR * atr[i]
            if i - j < MIN_SEP or not away:
                e[3] = i
                if kind == "low" and (e[2] != "low" or p < e[1]):
                    e[0], e[1], e[2] = i, p, kind
                continue
        events.append([i, p, kind, i])
    # the last touch must be followed by a rally away before the bounce revisits
    while events:
        j = events[-1][3]
        if j + 1 <= end and max(highs[j + 1:end + 1]) >= level + SEP_ATR * atr[end]:
            break
        events.pop()
    if not events:
        return []
    # invalidation: where price LIVED UNDER the level (BREAK_BARS closes in a row
    # more than BREAK_ATR below it) - the start of the last such run
    last_break = -1
    run = 0
    for j in range(events[0][0] + 1, end + 1):
        if closes[j] < level - BREAK_ATR * atr[j]:
            run += 1
            if run == BREAK_BARS:
                last_break = j - BREAK_BARS + 1
        else:
            run = 0
    kept = []
    for i, p, kind, _ in events:
        if kind == "low":
            # a support touch price then lived under is a broken level, gone
            if last_break > i:
                continue
        else:
            # an old resistance is a support touch only once price has closed back
            # above it - and only as good as the LATEST breakout: after the last
            # spell under the level there must be a close above it (a swing high
            # that price broke above, fell under for a month and broke above again
            # is one flip, dated by the old high, valid again since that breakout)
            since = max(i, last_break)
            if not any(closes[j] > level + tol for j in range(since + 1, end + 1)):
                continue
        kept.append((i, p, kind))
    return kept


def _weekly_closes(times, closes) -> list[float]:
    out: list[float] = []
    key = None
    for t, c in zip(times, closes):
        try:
            k = _dt.date.fromisoformat(str(t)[:10]).isocalendar()[:2]
        except ValueError:
            continue
        if k != key:
            key = k
            out.append(c)
        else:
            out[-1] = c
    return out


def _nearest(level, pairs, tol):
    best = None
    for name, val in pairs:
        if val is None:
            continue
        d = abs(level - val)
        if d <= tol and (best is None or d < best[1]):
            best = (name, d)
    return best[0] if best else None


def volume_read(bars: list[dict]) -> tuple:
    """(vol_ratio, vol_high, projected) for the LAST bar vs the mean of the
    ``VOL_LOOKBACK`` sessions before it. ``projected`` is True when today's
    partial volume was scaled up by the session fraction, None when no session
    fraction was involved; ``vol_high`` is None when the read is not possible
    yet (too early in the session) or there is no volume at all."""
    if len(bars) < VOL_LOOKBACK + 1:
        return None, None, None
    prev = [b.get("volume") for b in bars[-VOL_LOOKBACK - 1:-1]]
    prev = [float(v) for v in prev if v]
    v = bars[-1].get("volume")
    if not v or len(prev) < VOL_LOOKBACK // 2:
        return None, None, None
    mean = sum(prev) / len(prev)
    if mean <= 0:
        return None, None, None
    v = float(v)
    frac = bars[-1].get("session_frac")
    projected = None
    if frac is not None and frac < 1.0:
        if frac < VOL_MIN_FRAC:
            return round(v / mean, 2), None, True
        v = v / frac
        projected = True
    ratio = v / mean
    return round(ratio, 2), ratio >= VOL_HIGH, projected


def find(bars: list[dict], d_emas=(), w_emas=()) -> dict | None:
    """The support bounce on the LATEST candle of ``bars`` (``/prices`` dicts), or
    None. ``d_emas`` / ``w_emas`` are ((name, last value), ...) of the daily and
    weekly EMA20 / EMA50 for the confluence read (weekly ones may be omitted).

    Returns {level, zone: [lo, hi], atr, touches: [{time, price, kind}] (oldest
    first, previous touches only), n_touches, bounce: {time, kind, low},
    vol_ratio, vol_high, vol_projected, d_ema, w_ema}."""
    n = len(bars)
    if n < MIN_BARS:
        return None
    try:
        cur = tuple(float(bars[-1][k]) for k in ("open", "high", "low", "close"))
        prev = tuple(float(bars[-2][k]) for k in ("open", "high", "low", "close"))
    except (KeyError, TypeError, ValueError):
        return None
    o, h, l, c = cur
    pin = is_pin_bar(o, h, l, c)
    # cheap shape gate before any series work
    if not pin and not (c > o and prev[3] <= prev[0] and o <= min(prev[0], prev[3]) and c >= max(prev[0], prev[3])):
        return None
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    atr = atr_series(highs, lows, closes)
    a = atr[-1]
    if not a or a <= 0:
        return None
    if pin:
        kind, test_low = "pin", l
    elif is_engulfing(prev, cur, a):
        kind, test_low = "engulf", min(l, prev[2])
    else:
        return None

    tol, reach, pierce = TOL_ATR * a, REACH_ATR * a, PIERCE_ATR * a
    end = n - 1 - MIN_SEP                       # the last bar a touch may sit on
    lo_i = max(PIVOT + 14, n - 1 - LOOKBACK)
    if end < lo_i:
        return None
    swings = _swings(highs, lows, atr, lo_i, end)
    # only swings that could form a level the bounce candle reached
    cand = [s for s in swings if test_low - reach - tol <= s[1] <= test_low + pierce + tol]
    if not cand:
        return None
    best = None
    seen = set()
    for _, seed, _ in cand:
        lvl = seed
        mem = []
        for _ in range(2):                      # seed -> mean -> re-gather
            mem = [s for s in cand if abs(s[1] - lvl) <= tol]
            lvl = sum(s[1] for s in mem) / len(mem)
        mem.sort()
        key = tuple(s[0] for s in mem)
        if key in seen:
            continue
        seen.add(key)
        # history ends where the bounce pattern begins: the rally away from the
        # last touch, and an old resistance's breakout, must have happened BEFORE
        # the bounce candle(s) - a candle that is itself the breakout is not a
        # retest from above
        touches = _touches(mem, highs, lows, closes, atr, lvl, tol, n - 3 if kind == "engulf" else n - 2)
        if not touches:
            continue
        lvl = sum(t[1] for t in touches) / len(touches)
        if not (lvl - pierce <= test_low <= lvl + reach and c >= lvl):
            continue
        rank = (len(touches), -abs(test_low - lvl))
        if best is None or rank > best[0]:
            best = (rank, lvl, touches)
    if best is None:
        return None
    _, lvl, touches = best

    vol_ratio, vol_high, projected = volume_read(bars)
    d_ema = _nearest(lvl, d_emas, DEMA_TOL_ATR * a)
    w_ema = _nearest(lvl, w_emas, WEMA_TOL_ATR * a) if w_emas else None
    return {
        "level": round(lvl, 2),
        "zone": [round(lvl - tol, 2), round(lvl + tol, 2)],
        "atr": round(a, 4),
        "touches": [{"time": bars[i].get("time"), "price": round(p, 2), "kind": k} for i, p, k in touches],
        "n_touches": len(touches),
        "bounce": {"time": bars[-1].get("time"), "kind": kind, "low": round(test_low, 2)},
        "vol_ratio": vol_ratio, "vol_high": vol_high, "vol_projected": projected,
        "d_ema": d_ema, "w_ema": w_ema,
    }
