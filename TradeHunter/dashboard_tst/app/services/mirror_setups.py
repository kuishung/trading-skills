"""The bear-side setups ``support_bounce`` has no mirror for - by MIRRORING the bars.

The design asks for the support-bounce detector's mirror (a rejection at a
horizontal resistance) and for the breakdown of a defended level. Rather than
re-deriving a second set of pivot / touch / invalidation rules for highs, this
module price-negates the bars and reuses ``support_bounce``'s internals
unchanged, so both edges obey exactly the same ATR-relative rules and any future
fix to ``support_bounce`` fixes both (design/options/part_B_engines.md B2.4;
OPTIONS_MODULE_DESIGN.md II.2.11).

* ``mirror_bars``            - o' = -o, h' = -l, l' = -h, c' = -c, volume kept: a swing
                               HIGH becomes a swing LOW, "close held above" becomes
                               "close held below", the true range is unchanged so the
                               ATR is identical.
* ``find_resistance_reject`` - ``support_bounce.find`` on the mirrored bars, un-mirrored
                               on the way out: ``is_pin_bar`` on mirrored OHLC is exactly
                               a shooting star, ``is_engulfing`` exactly a bearish
                               engulfing - no new candle code.
* ``find_breakdown``         - the LEVEL search without the bounce-candle gate
                               (``_swings`` / ``_members`` / ``_touches`` called directly),
                               then: the latest close under ``level - BREAK_ATR x ATR``,
                               the close ``BREAK_RECENT`` sessions back still above it (it
                               broke today or yesterday - a stale break is a downtrend,
                               not a setup), and ``volume_read`` saying high.

Only these three live here. The range box and the sideways verdict are Part C's
``range_box.py``; nothing range-shaped belongs in this file. Every threshold is
``support_bounce``'s own ATR multiple (CLAUDE.md: ticker-relative only).
"""
from __future__ import annotations

import math

from . import support_bounce as sb

LEVEL_LOOKBACK = 120      # sessions a defended level may come from (six months; a year-old level is history)
MIN_TOUCHES = 2           # "low" touches a level needs before its break means anything
BREAK_RECENT = 2          # the break must be today's or yesterday's close

_PRICE_KEYS = ("open", "high", "low", "close")


def mirror_bars(bars: list[dict]) -> list[dict]:
    """Price-negated bars: o'=-o, h'=-l, l'=-h, c'=-c, volume (and ``session_frac``,
    ``time``) kept. Mirroring twice gives the original back."""
    out = []
    for b in bars or []:
        m = dict(b)
        try:
            o, h, l, c = (float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]))
        except (KeyError, TypeError, ValueError):
            out.append(m)
            continue
        m.update(open=-o, high=-l, low=-h, close=-c)
        out.append(m)
    return out


def _neg(v):
    try:
        return None if v is None else -float(v)
    except (TypeError, ValueError):
        return None


def _unmirror(r: dict) -> dict:
    """A ``support_bounce.find`` result read on mirrored bars -> real prices: level ->
    -level, zone [lo, hi] -> [-hi, -lo], touches' price -> -price, ``bounce.low`` ->
    ``candle.high`` (the wick that tested the resistance)."""
    lo, hi = r.get("zone") or (None, None)
    b = r.get("bounce") or {}
    kind = b.get("kind")
    out = dict(r)
    out["level"] = round(-float(r["level"]), 2) if r.get("level") is not None else None
    out["zone"] = [round(-float(hi), 2), round(-float(lo), 2)] if (lo is not None and hi is not None) else None
    out["touches"] = [{**t, "price": round(-float(t["price"]), 2)} for t in (r.get("touches") or [])]
    out["candle"] = {"time": b.get("time"), "kind": kind, "high": _neg(b.get("low"))}
    out["bounce"] = {"time": b.get("time"), "kind": kind, "high": _neg(b.get("low"))}
    out["d_ema"] = r.get("d_ema")
    out["w_ema"] = r.get("w_ema")
    out["mirrored"] = True
    return out


def find_resistance_reject(bars: list[dict], d_emas=(), w_emas=()) -> dict | None:
    """A shooting star / bearish engulfing on the LATEST candle at a horizontal
    resistance price has turned down from before - ``support_bounce.find`` on the
    mirrored bars, prices un-mirrored on the way out. ``d_emas`` / ``w_emas`` are the
    same ``((name, value), ...)`` pairs ``support_bounce.find`` takes; they are
    negated here. None when the latest candle is not a rejection."""
    if not bars:
        return None
    try:
        r = sb.find(mirror_bars(bars),
                    [(n, -float(v)) for n, v in (d_emas or ()) if v is not None],
                    [(n, -float(v)) for n, v in (w_emas or ()) if v is not None])
    except Exception:  # noqa: BLE001 - a detector must never take the whole read down
        return None
    return None if r is None else _unmirror(r)


def _series(bars: list[dict]):
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    return highs, lows, closes


def _levels(bars: list[dict], lo_i: int, end: int) -> list[dict]:
    """Every defended level in ``[lo_i, end]``: seeds from ``_swings``, members from
    ``_members``, distinct still-valid touches from ``_touches`` - ``support_bounce.find``'s
    loop minus the bounce test and the 'established / approached' gates. Returns
    ``[{level, zone, touches, n_low, n_flip, atr}]`` with ``n_low >= MIN_TOUCHES``,
    most-touched first."""
    try:
        highs, lows, closes = _series(bars)
    except (KeyError, TypeError, ValueError):
        return []
    n = len(closes)
    if n < sb.MIN_BARS or end <= lo_i or end >= n:
        return []
    atr = sb.atr_series(highs, lows, closes)
    a = atr[end]
    if not a or a <= 0:
        return []
    tol = sb.TOL_ATR * a
    swings = sb._swings(highs, lows, atr, lo_i, end)
    if not swings:
        return []
    flips = [s for s in swings if s[2] == "flip"]
    out: list[dict] = []
    seen: set = set()
    for _, seed, _ in swings:
        lvl = seed
        for _ in range(2):
            near = [s for s in swings if abs(s[1] - lvl) <= tol]
            lvl = sum(s[1] for s in near) / len(near)
        mem = sb._members(lvl, tol, flips, highs, lows, closes, lo_i, end)
        key = tuple(m[0] for m in mem)
        if not mem or key in seen:
            continue
        seen.add(key)
        touches = sb._touches(mem, highs, lows, closes, atr, lvl, tol, end)
        if not touches:
            continue
        prices = [t[1] for t in touches]
        lvl = sum(prices) / len(prices)
        n_low = sum(1 for t in touches if t[2] == "low")
        if n_low < MIN_TOUCHES:
            continue
        out.append({
            "level": round(lvl, 2), "zone": [round(min(prices), 2), round(max(prices), 2)],
            "touches": [{"time": bars[i].get("time"), "price": round(p, 2), "kind": k} for i, p, k in touches],
            "n_touches": len(touches), "n_low": n_low,
            "n_flip": sum(1 for t in touches if t[2] == "flip"), "atr": round(a, 4),
        })
    out.sort(key=lambda d: (-d["n_low"], -d["n_touches"]))
    return out


def _defended_levels(bars: list[dict], *, lookback: int = LEVEL_LOOKBACK) -> list[dict]:
    """The defended SUPPORT levels of the last ``lookback`` sessions (``_levels`` over
    the bars as they are), most-touched first - what ``chart_state`` reads
    ``levels.support`` from when no bounce is on the latest candle. Run it on
    ``mirror_bars(bars)`` (and negate) for the resistances."""
    n = len(bars or [])
    end = n - 1 - sb.MIN_SEP
    lo_i = max(sb.PIVOT + 14, n - 1 - lookback)
    if n < sb.MIN_BARS or end <= lo_i:
        return []
    return _levels(bars, lo_i, end)


def find_breakdown(bars: list[dict]) -> dict | None:
    """The most-touched level in the last ``LEVEL_LOOKBACK`` sessions whose break is
    FRESH (B2.3's ``failed_support`` row): the latest close is under ``level -
    BREAK_ATR x ATR``, the close ``BREAK_RECENT`` sessions back was not, and the
    breaking candle traded on high volume for this ticker. None when no level has
    ``MIN_TOUCHES``, when the latest close is still above the line, or when the break
    is older than ``BREAK_RECENT`` sessions (a stale break is a downtrend, not a
    setup). Returns ``{level, zone, touches, n_touches, n_low, n_flip, broke_on,
    vol_ratio, vol_high, vol_projected, atr, close}``."""
    n = len(bars or [])
    if n < sb.MIN_BARS:
        return None
    try:
        highs, lows, closes = _series(bars)
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (highs[-1], lows[-1], closes[-1])):
        return None
    atr = sb.atr_series(highs, lows, closes)
    a = atr[-2]
    if not a or a <= 0:
        return None
    # the level search ends BREAK_RECENT + MIN_SEP bars back: the break candle and the
    # sessions right before it are the test, not history
    end = n - 1 - BREAK_RECENT - sb.MIN_SEP
    lo_i = max(sb.PIVOT + 14, n - 1 - LEVEL_LOOKBACK)
    if end <= lo_i:
        return None
    levels = _levels(bars, lo_i, end)
    close = closes[-1]
    for lv in levels:
        line = lv["level"] - sb.BREAK_ATR * a
        if close >= line:
            continue                                 # still above the break line: not broken
        # the break must be fresh: the close BREAK_RECENT sessions back was still above
        prev = closes[-1 - BREAK_RECENT]
        if prev < line:
            continue                                 # it lived under the level already: a downtrend, not a setup
        # which session broke it - today, or yesterday
        broke_i = n - 1
        for k in range(n - BREAK_RECENT, n):
            if closes[k] < line:
                broke_i = k
                break
        vol_ratio, vol_high, projected = sb.volume_read(bars, broke_i)
        return {
            "level": lv["level"], "zone": lv["zone"], "touches": lv["touches"],
            "n_touches": lv["n_touches"], "n_low": lv["n_low"], "n_flip": lv["n_flip"],
            "broke_on": bars[broke_i].get("time"), "close": round(close, 2),
            "vol_ratio": vol_ratio, "vol_high": vol_high, "vol_projected": projected,
            "atr": round(a, 4),
        }
    return None
