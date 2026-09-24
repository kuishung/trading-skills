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
that price has turned at before? ``find()`` is pure arithmetic over the
``/prices``-shaped bars (time/open/high/low/close/volume) and returns the level,
its previous touches, the bounce candle and the volume read - or None.

How a level is found
--------------------
* **Where levels may be** is seeded by the swing points of the prior 252
  sessions (one year), ending ``MIN_SEP`` bars before the bounce candle -
  anything closer is the same visit, not history. A swing LOW has no lower low
  within ``PIVOT`` bars either side (``<=``, so two EQUAL lows - the double
  bottom - both survive); a swing HIGH is the mirror. Either must be a real
  reaction: price came at least ``REACT_ATR`` into the point and left by as much
  within ``REACT_BARS`` bars. Seeds within ``TOL_ATR`` of each other are one
  level (a hand-drawn line has width).
* **What touched it** is then read from EVERY bar, not only the swings (v4.125):
  a SHELF - several sessions whose lows sit on the level while a spike a day or
  two away is deeper - is what a trader counts as a touch, and a swing test
  misses exactly that. A "low" touch is any bar whose low is within ``TOL_ATR``
  of the level and whose close did not fall through the zone (a bar closing well
  under it sliced the level; it did not touch it). A "flip" touch is a swing
  HIGH at the level: an old resistance that price has since broken above and
  may now be retesting from above.
* **Distinct events**: members closer than ``MIN_SEP`` bars, or with no
  excursion of ``SEP_ATR`` away from the level between them, are ONE touch (a
  week sitting on a price is one test of it). Each event must have ARRIVED
  (for a low, from at least ``REACT_ATR`` above within ``REACT_BARS`` bars; for
  a flip, from as far below) and LEFT (the last low touch by a rally of
  ``SEP_ATR`` above the level before the bounce; a flip by the breakout close
  above it).
* **Invalidation and re-validation**, the same for both kinds: a level price
  then LIVED UNDER - ``BREAK_BARS`` consecutive closes more than ``BREAK_ATR``
  below it - was broken, and a touch from before that break counts again only
  once price has closed back above the level (the reclaim). A single close
  below is a spring, still support.
* **The test**, against the ZONE the touches actually span, not a razor line
  at their mean: the bounce low (for an engulfing, the lower of its two lows -
  the engulfed candle is the one that tapped the level) reached no more than
  ``REACH_ATR`` above the highest touch and speared no more than ``PIERCE_ATR``
  below the lowest; the close held at or above the zone.
* **The level must be ESTABLISHED and APPROACHED** (v4.125): at least
  ``ABOVE_MIN`` of the ``REACT_BARS`` closes before the pattern were above the
  level (a resistance broken through three days ago is a breakout, not yet a
  support), none of them was more than ``BREAK_ATR`` under it (a level broken
  and reclaimed last week is a reclaim, not a bounce off a held level), and a
  high at least ``REACT_ATR`` above the level printed in that window (price
  came DOWN to the level; a month sitting on it and a hammer is not a bounce).
* **Choice** among levels the candle tested: most "low" touches, then most
  touches, then the nearest. The chip and the chart say which touches are
  resistance-turned-support ("R->S"), because a level made only of old highs is a
  first retest, not a defended support.

Every threshold is a multiple of the ticker's own ATR(14) (Wilder) as it stood
BEFORE the bounce candle (its own range must not move its own goalposts), a
fraction of the candle's own range, or a ratio to the ticker's own volume - no
dollars, no share counts (CLAUDE.md: ticker-relative thresholds only).

Volume: ``vol_ratio`` = the pattern's volume (the bounce candle, or the larger
of the two candles of an engulfing) over the mean of the ``VOL_LOOKBACK``
sessions before the pattern. ``vol_high`` = at least ``VOL_FLOOR`` times that
mean AND inside the top ``VOL_TOP_PCT`` of THIS ticker's own daily
volume-to-20-day-mean ratios over the last year - "high" for a name whose
volume barely varies is a smaller multiple than for a lumpy one (v4.125; a
fixed 1.5x fired on 4% of bounces). While today's session is open the bar
holds only the volume traded SO FAR, so it is projected to a full day from
``session_frac`` (see ``services.prices``) once at least ``VOL_MIN_FRAC`` of
the session has run; earlier the read is "not yet" (None), never a verdict.

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
import math

LOOKBACK = 252          # sessions of history a touch may come from (one year)
PIVOT = 3               # a swing point has no lower low / higher high this many bars either side
MIN_SEP = 5             # bars between distinct touches, and before the bounce candle
REACT_BARS = 10         # the approach / departure of a swing is measured over this many bars
REACT_ATR = 1.0         # ... and must be at least this much (x ATR)
TOL_ATR = 0.35          # cluster half-width (x ATR): the line's thickness
SEP_ATR = 1.0           # two touches need an excursion this far from the level between them
REACH_ATR = 0.25        # the bounce low may stop this far ABOVE the zone and still have tested it
PIERCE_ATR = 0.5        # ... and may spear this far BELOW it (a spring); deeper is a break
BREAK_ATR = 0.5         # closes more than this below the level ...
BREAK_BARS = 3          # ... for this many consecutive sessions = lived under it = broken
ABOVE_MIN = 5           # of the REACT_BARS closes before the pattern, at least this many above the level
ENGULF_BODY_RNG = 0.5   # an engulfing body is at least half its own range
ENGULF_BODY_ATR = 0.5   # ... and at least half a normal day's range
VOL_LOOKBACK = 20       # sessions the average volume is taken over
VOL_FLOOR = 1.2         # high volume is at least this many times the average ...
VOL_TOP_PCT = 0.15      # ... and inside this top share of the ticker's own year of ratios
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


def _members(level, tol, flips, highs, lows, closes, lo_i, hi_i) -> list[tuple]:
    """Every bar in [lo_i, hi_i] that touched ``level`` as support - low within
    the zone, close not through it - plus the swing-high ``flips`` within the
    zone. Sorted by index."""
    out = [(i, lows[i], "low") for i in range(lo_i, hi_i + 1)
           if abs(lows[i] - level) <= tol and closes[i] >= level - tol]
    out += [(i, p, "flip") for i, p, _ in flips if abs(p - level) <= tol]
    out.sort()
    return out


def _touches(members, highs, lows, closes, atr, level, tol, end) -> list[tuple]:
    """Distinct, still-valid touches of ``level`` among ``members`` (sorted by
    index). ``end`` = the last bar that is "before the bounce pattern"."""
    # merge into distinct events: MIN_SEP bars apart AND an excursion away between
    events: list[list] = []          # [idx, price, kind, first_member_idx, last_member_idx]
    for i, p, kind in members:
        if events:
            e = events[-1]
            j = e[4]
            between_h = max(highs[j + 1:i]) if i > j + 1 else highs[i]
            between_l = min(lows[j + 1:i]) if i > j + 1 else lows[i]
            away = between_h >= level + SEP_ATR * atr[i] or between_l <= level - SEP_ATR * atr[i]
            if i - j < MIN_SEP or not away:
                e[4] = i
                if kind == "low" and (e[2] != "low" or p < e[1]):
                    e[0], e[1], e[2] = i, p, kind
                continue
        events.append([i, p, kind, i, i])
    # each event ARRIVED at the level: a low from above, a flip from below
    arrived = []
    for e in events:
        i0 = e[3]
        a = atr[i0] or atr[end]
        if not a:
            continue
        win_h = highs[max(0, i0 - REACT_BARS):i0]
        win_l = lows[max(0, i0 - REACT_BARS):i0]
        if e[2] == "low" and win_h and max(win_h) >= level + REACT_ATR * a:
            arrived.append(e)
        elif e[2] == "flip" and win_l and min(win_l) <= level - REACT_ATR * a:
            arrived.append(e)
    events = arrived
    # the last touch must have LEFT before the bounce revisits: a low by a rally
    # away, a flip by the breakout above it
    while events:
        e = events[-1]
        j = e[4]
        if j + 1 <= end:
            if e[2] == "low" and max(highs[j + 1:end + 1]) >= level + SEP_ATR * atr[end]:
                break
            if e[2] == "flip" and any(closes[k] > level + tol for k in range(j + 1, end + 1)):
                break
        events.pop()
    if not events:
        return []
    # invalidation / re-validation: the start of the last spell price LIVED
    # UNDER the level; a touch from before it counts again only once price has
    # closed back above the level (a flip needs that breakout close in any case)
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
    for i, p, kind, _, last_i in events:
        since = max(last_i, last_break)
        if kind == "low" and last_break <= last_i:
            kept.append((i, p, kind))          # never broken since: a held support
        elif any(closes[k] > level + tol for k in range(since + 1, end + 1)):
            kept.append((i, p, kind))          # reclaimed / broken out above since
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


def _vol(bars, i) -> float | None:
    v = bars[i].get("volume")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def volume_read(bars: list[dict], pat_start: int | None = None) -> tuple:
    """(vol_ratio, vol_high, projected) for the pattern that starts at
    ``pat_start`` (default: the last bar) vs the mean of the ``VOL_LOOKBACK``
    sessions before it. ``projected`` is True when today's partial volume was
    scaled up by the session fraction, None otherwise; ``vol_high`` is None when
    the read is not possible yet (too early in the session) or there is no
    volume. "High" = at least VOL_FLOOR x the mean AND inside the top
    VOL_TOP_PCT of this ticker's own daily ratios over the last year."""
    n = len(bars)
    if pat_start is None:
        pat_start = n - 1
    if pat_start < VOL_LOOKBACK + 1:
        return None, None, None
    prev = [_vol(bars, j) for j in range(pat_start - VOL_LOOKBACK, pat_start)]
    prev = [v for v in prev if v]
    if len(prev) < VOL_LOOKBACK // 2:
        return None, None, None
    mean = sum(prev) / len(prev)
    # the pattern's volume: the bounce candle (projected if its session is open),
    # or for an engulfing the larger of its two candles
    frac = bars[-1].get("session_frac")
    v_last = _vol(bars, n - 1)
    if v_last is None:
        return None, None, None
    projected = None
    if frac is not None and frac < 1.0:
        if frac < VOL_MIN_FRAC:
            return None, None, True             # too early in the session to say
        v_last = v_last / frac
        projected = True
    v = v_last
    for j in range(pat_start, n - 1):
        vj = _vol(bars, j)
        if vj and vj > v:
            v = vj
    ratio = v / mean
    # this ticker's own distribution of daily volume / prior-20-day mean
    ratios = []
    for j in range(max(VOL_LOOKBACK, pat_start - LOOKBACK), pat_start):
        vj = _vol(bars, j)
        if not vj:
            continue
        win = [_vol(bars, k) for k in range(j - VOL_LOOKBACK, j)]
        win = [w for w in win if w]
        if len(win) >= VOL_LOOKBACK // 2:
            ratios.append(vj / (sum(win) / len(win)))
    if len(ratios) >= 40:
        ratios.sort()
        cut = ratios[int(len(ratios) * (1.0 - VOL_TOP_PCT))]
        thresh = max(VOL_FLOOR, cut)
    else:
        thresh = VOL_FLOOR + 0.3                # too little history: a plain 1.5x
    return round(ratio, 2), ratio >= thresh, projected


def find(bars: list[dict], d_emas=(), w_emas=()) -> dict | None:
    """The support bounce on the LATEST candle of ``bars`` (``/prices`` dicts), or
    None. ``d_emas`` / ``w_emas`` are ((name, last value), ...) of the daily and
    weekly EMA20 / EMA50 for the confluence read (weekly ones may be omitted).

    Returns {level, zone: [lo, hi], atr, touches: [{time, price, kind}] (oldest
    first, previous touches only), n_touches, n_low, n_flip, bounce: {time,
    kind, low}, vol_ratio, vol_high, vol_projected, d_ema, w_ema}."""
    n = len(bars)
    if n < MIN_BARS:
        return None
    try:
        cur = tuple(float(bars[-1][k]) for k in ("open", "high", "low", "close"))
        prev = tuple(float(bars[-2][k]) for k in ("open", "high", "low", "close"))
        o, h, l, c = cur
        pin = is_pin_bar(o, h, l, c)
        # cheap shape gate before any series work
        if not pin and not (c > o and prev[3] <= prev[0] and o <= min(prev[0], prev[3])
                            and c >= max(prev[0], prev[3])):
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (o, h, l, c)):
        return None
    atr = atr_series(highs, lows, closes)
    # the ATR as it stood before the pattern: a wide bounce candle must not
    # widen its own tolerances
    a_pin = atr[-2]
    if not a_pin or a_pin <= 0:
        return None
    if pin:
        kind, test_low, pat_start = "pin", l, n - 1
        a = a_pin
    else:
        a = atr[-3] if n >= 3 and atr[-3] else a_pin
        if not is_engulfing(prev, cur, a):
            return None
        kind, test_low, pat_start = "engulf", min(l, prev[2]), n - 2
    before = pat_start - 1                       # last bar of history before the pattern

    tol, reach, pierce = TOL_ATR * a, REACH_ATR * a, PIERCE_ATR * a
    end = n - 1 - MIN_SEP                        # the last bar a touch may sit on
    lo_i = max(PIVOT + 14, n - 1 - LOOKBACK)
    if end < lo_i or before - REACT_BARS < 0:
        return None
    swings = _swings(highs, lows, atr, lo_i, end)
    # only swings that could seed a level the bounce candle reached
    cand = [s for s in swings if test_low - reach - tol <= s[1] <= test_low + pierce + tol]
    if not cand:
        return None
    flips = [s for s in swings if s[2] == "flip"]
    # the bars before the pattern that decide "established" and "approached"
    pre_c = closes[before - REACT_BARS + 1:before + 1]
    pre_h = highs[before - REACT_BARS + 1:before + 1]
    pre_a = atr[before - REACT_BARS + 1:before + 1]
    best = None
    seen = set()
    for _, seed, _ in cand:
        lvl = seed
        for _ in range(2):                      # seed -> mean of nearby swings -> re-gather
            near = [s for s in cand if abs(s[1] - lvl) <= tol]
            lvl = sum(s[1] for s in near) / len(near)
        mem = _members(lvl, tol, flips, highs, lows, closes, lo_i, end)
        key = tuple(m[0] for m in mem)
        if not mem or key in seen:
            continue
        seen.add(key)
        touches = _touches(mem, highs, lows, closes, atr, lvl, tol, before)
        if not touches:
            continue
        prices = [t[1] for t in touches]
        lvl = sum(prices) / len(prices)
        z_lo, z_hi = min(prices), max(prices)
        # the test, against the zone the touches span
        if not (z_lo - pierce <= test_low <= z_hi + reach and c >= lvl - tol):
            continue
        # established above it, approached from above, not broken last week
        if sum(1 for x in pre_c if x >= lvl) < ABOVE_MIN:
            continue
        if max(pre_h) < lvl + REACT_ATR * a:
            continue
        if any(x < lvl - BREAK_ATR * (aa or a) for x, aa in zip(pre_c, pre_a)):
            continue
        n_low = sum(1 for t in touches if t[2] == "low")
        rank = (n_low, len(touches), -abs(test_low - lvl))
        if best is None or rank > best[0]:
            best = (rank, lvl, touches, z_lo, z_hi)
    if best is None:
        return None
    _, lvl, touches, z_lo, z_hi = best

    vol_ratio, vol_high, projected = volume_read(bars, pat_start)
    d_ema = _nearest(lvl, d_emas, DEMA_TOL_ATR * a)
    w_ema = _nearest(lvl, w_emas, WEMA_TOL_ATR * a) if w_emas else None
    return {
        "level": round(lvl, 2),
        "zone": [round(z_lo, 2), round(z_hi, 2)],
        "atr": round(a, 4),
        "touches": [{"time": bars[i].get("time"), "price": round(p, 2), "kind": k} for i, p, k in touches],
        "n_touches": len(touches),
        "n_low": sum(1 for t in touches if t[2] == "low"),
        "n_flip": sum(1 for t in touches if t[2] == "flip"),
        "bounce": {"time": bars[-1].get("time"), "kind": kind, "low": round(test_low, 2)},
        "vol_ratio": vol_ratio, "vol_high": vol_high, "vol_projected": projected,
        "d_ema": d_ema, "w_ema": w_ema,
    }
