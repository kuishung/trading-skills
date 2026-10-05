"""The chart-state contract: ONE dict per ticker that says what the chart is doing
today - trend, setup, levels, the plan (stop / target) - for the strategy
recommender and the strike picker (design/options/part_B_engines.md B2;
OPTIONS_MODULE_DESIGN.md II.2.6, II.2.14).

``read(symbol, *, bars, long_bars, today, expiries) -> ChartState`` - the one
spelling every part uses (keyword-only; ``expiries=`` never ``at=``; the ATR is
computed inside from the bars).

**One detector run.** ``read()`` calls ``ema_setup.analyze(bars, long_bars)`` ONCE
(with ``at=expiries`` once ``analyze`` grows that argument in step 2) and reads
``sup`` (the support bounce), ``tl`` / ``tl_bounce`` (the trend line, step 2) and
``rng`` (the range box, step 3) from its return dict - it never re-runs
``support_bounce.find`` itself. The only detectors it calls directly are the two
bear-side mirrors in ``mirror_setups`` (``analyze()`` does not compute them) and
``structure.classify``.

``plan`` is THE source of the chart stop on the platform: the picker's
``chart_stop``, the sizing, the ticket, the payoff marker and the monitor all read
the same number. For a long setup ``stop = min(entry - STOP_ATR x ATR, zone_lo -
LEVEL_PAD_ATR x ATR)`` - one ATR under the entry, and in any case under the level
that must hold (LRCX: 339.1 - 0.25 x 11.54 = 336.2; ISRG: 405.81 - 11.54 = 394.27);
``target = entry + TARGET_R x (entry - stop)``, capped at a resistance that sits
between 1.5R and 2R. Every number is an ATR multiple or an R multiple (CLAUDE.md).

``stored_setup(state)`` is the projection the nightly job writes into
``option_signal.setup`` (B2.7): the primary setup's fields, the close / trend /
ATR / EMAs, ``plan`` plus the flat aliases ``stop`` / ``target``, ``levels``, and
Part C's ``sup / tl / tl_bounce / rng`` dicts verbatim.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import math

from . import ema_setup, mirror_setups
from . import support_bounce as sb
from .opt_constants import LEVEL_PAD_ATR, SLOW_DRIFT_ATR, STOP_ATR, TARGET_R

TRENDS = ("up", "down", "sideways", "unclear")
SETUP_KINDS = ("support_bounce", "resistance_reject", "trendline_bounce", "ema_rebound",
               "breakout_retest", "failed_support", "range")
DIRECTION_OF = {"support_bounce": "up", "resistance_reject": "down", "trendline_bounce": "up",
                "ema_rebound": "up", "breakout_retest": "up", "failed_support": "down", "range": "neutral"}
TREND_OF_DIRECTION = {"up": "up", "down": "down", "neutral": "sideways"}

SLOW_DRIFT_BARS = 10        # the EMA20 move is measured over this many sessions
RETEST_BARS = 20            # a breakout retest needs the breakout close within this many sessions
TARGET_CAP_LO_R, TARGET_CAP_HI_R = 1.5, 2.0     # a resistance between 1.5R and 2R caps the target


# ------------------------------------------------------------------- helpers
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, nd: int = 2):
    f = _num(v)
    return None if f is None else round(f, nd)


def _closes(bars: list[dict]) -> list[float]:
    """The numeric closes, in order; a bar whose close is not a number is skipped
    (the honest read, never a raise - support_bounce's edge probes apply here too)."""
    out = []
    for b in bars:
        c = _num(b.get("close")) if isinstance(b, dict) else None
        if c is not None:
            out.append(c)
    return out


def atr_of(bars: list[dict]) -> float | None:
    """ATR(14) Wilder as it stood BEFORE today's bar (``support_bounce.atr_series``):
    the day's own range must not move its own goalposts."""
    try:
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
    except (KeyError, TypeError, ValueError):
        return None
    if len(closes) < 16:
        return None
    series = sb.atr_series(highs, lows, closes)
    a = series[-2] if len(series) >= 2 else None
    return round(a, 4) if a else None


def _stack(e20: float, e50: float, e200: float) -> str:
    if e20 > e50 > e200:
        return "up"
    if e20 < e50 < e200:
        return "down"
    return "mixed"


def _trend_days(closes: list[float]) -> int:
    """Sessions, counting back from today, over which the EMA stack ordering has not
    changed (an uptrend 3 days old is a different thing from one 90 days old)."""
    if len(closes) < 2:
        return 0
    e20, e50, e200 = ema_setup.ema(closes, 20), ema_setup.ema(closes, 50), ema_setup.ema(closes, 200)
    last = _stack(e20[-1], e50[-1], e200[-1])
    n = 0
    for i in range(len(closes) - 1, -1, -1):
        if _stack(e20[i], e50[i], e200[i]) != last:
            break
        n += 1
    return n


def _slow_drift(closes: list[float], atr: float | None) -> bool | None:
    """|EMA20 now - EMA20 ten bars ago| <= SLOW_DRIFT_ATR x ATR."""
    if not atr or len(closes) < SLOW_DRIFT_BARS + 1:
        return None
    e20 = ema_setup.ema(closes, 20)
    return abs(e20[-1] - e20[-1 - SLOW_DRIFT_BARS]) <= SLOW_DRIFT_ATR * atr


def _call_analyze(bars, long_bars, expiries):
    """ONE ``ema_setup.analyze`` call; ``at=`` is passed only once analyze accepts it
    (step 2), so step 1 runs the shipped signature unchanged."""
    try:
        params = inspect.signature(ema_setup.analyze).parameters
    except (TypeError, ValueError):
        params = {}
    if "at" in params:
        return ema_setup.analyze(bars, long_bars, at=tuple(expiries or ()))
    return ema_setup.analyze(bars, long_bars)


# ------------------------------------------------------------------- quality
def _quality_bounce(sup: dict) -> int:
    """50 + 10 per touch past the first (cap 80) + 10 vol_high + 5 d_ema + 5 w_ema; a
    bounce whose volume is not high (or not readable) caps at 45 - ``ema_setup.rank``'s
    near miss."""
    n = int(sup.get("n_touches") or 0)
    if not sup.get("vol_high"):
        return 45
    q = min(80, 50 + 10 * max(0, n - 1)) + 10
    q += 5 if sup.get("d_ema") else 0
    q += 5 if sup.get("w_ema") else 0
    return int(min(100, q))


def _quality_breakdown(bd: dict) -> int:
    n = int(bd.get("n_touches") or 0)
    q = min(60, 40 + 10 * max(0, n - 2))
    return int(q + (10 if bd.get("vol_high") else 0))


# ------------------------------------------------------------------- setups
def _setup_from_sup(sup: dict, bars: list[dict], atr: float, *, kind: str = "support_bounce") -> dict:
    b = sup.get("bounce") or {}
    out = {
        "kind": kind, "direction": DIRECTION_OF[kind],
        "level": _r(sup.get("level")), "zone": list(sup.get("zone") or [None, None]),
        "touches": int(sup.get("n_touches") or 0), "n_low": sup.get("n_low"), "n_flip": sup.get("n_flip"),
        "vol_high": sup.get("vol_high"), "vol_ratio": sup.get("vol_ratio"),
        "candle": {"time": b.get("time"), "kind": b.get("kind"), "low": b.get("low")},
        "d_ema": sup.get("d_ema"), "w_ema": sup.get("w_ema"), "bounce": b,
        "quality": _quality_bounce(sup),
    }
    out["summary"] = (f"{'bounce off' if kind == 'support_bounce' else 'retest of'} {out['level']:g} "
                      f"({out['touches']} touch{'es' if out['touches'] != 1 else ''})"
                      + (" on high volume" if sup.get("vol_high") else ""))
    return out


def _breakout_retest(sup: dict, bars: list[dict], atr: float) -> dict | None:
    """The ``sup`` result with ``n_low == 0 and n_flip >= 1`` (a level made ONLY of old
    resistance highs - a first retest, not a defended support) AND the breakout close
    above the zone within the last RETEST_BARS sessions."""
    if not sup or (sup.get("n_low") or 0) != 0 or (sup.get("n_flip") or 0) < 1:
        return None
    zone = sup.get("zone") or []
    if len(zone) != 2 or atr is None:
        return None
    tol = sb.TOL_ATR * atr
    closes = _closes(bars)
    broke_on = None
    for i in range(max(0, len(bars) - RETEST_BARS), len(bars)):
        if closes[i] > zone[1] + tol:
            broke_on = bars[i].get("time")
            break
    if broke_on is None:
        return None
    out = _setup_from_sup(sup, bars, atr, kind="breakout_retest")
    out["broke_on"] = broke_on
    out["quality"] = int(min(50, 35 + (10 if sup.get("vol_high") else 0)))
    return out


def _ema_rebound(an: dict, close: float) -> dict | None:
    """``ema_setup.analyze``'s rebound (fresh / held) or a daily pin bar at an EMA."""
    reb, pin = an.get("rebound"), an.get("pin_d")
    if not reb and not pin:
        return None
    name = reb or (pin or {}).get("ema")
    level = an.get("ema20") if name == "EMA20" else an.get("ema50")
    if level is None:
        return None
    fresh, held = bool(an.get("fresh")), bool(an.get("held"))
    if fresh:
        q = 45
    elif held:
        q = 30
    else:
        q = 35
    if pin and reb:
        q += 10
    pct = (close - level) / level * 100.0 if level else None
    return {
        "kind": "ema_rebound", "direction": "up", "level": _r(level), "zone": [_r(level), _r(level)],
        "touches": 1, "ema": name, "fresh": fresh, "held": held, "pin": pin,
        "vol_high": None, "candle": {"time": (pin or {}).get("time"), "kind": "pin" if pin else "rebound", "low": None},
        "quality": int(q), "summary": f"{'fresh ' if fresh else ''}rebound on the {name}"
                                     + (f" ({pct:+.2f}%)" if pct is not None else ""),
    }


def _failed_support(bd: dict) -> dict:
    return {
        "kind": "failed_support", "direction": "down", "level": _r(bd.get("level")),
        "zone": list(bd.get("zone") or [None, None]), "touches": int(bd.get("n_touches") or 0),
        "broke_on": bd.get("broke_on"), "vol_high": bd.get("vol_high"), "vol_ratio": bd.get("vol_ratio"),
        "candle": {"time": bd.get("broke_on"), "kind": "break", "low": None},
        "quality": _quality_breakdown(bd),
        "summary": f"broke support {bd.get('level'):g} ({bd.get('n_touches')} touches)"
                   + (" on high volume" if bd.get("vol_high") else ""),
    }


def _resistance_reject(rr: dict, bars: list[dict], atr: float) -> dict:
    out = _setup_from_sup(rr, bars, atr, kind="resistance_reject")
    c = rr.get("candle") or {}
    out["candle"] = {"time": c.get("time"), "kind": c.get("kind"), "high": c.get("high")}
    out["summary"] = (f"rejected at resistance {out['level']:g} ({out['touches']} touch"
                      f"{'es' if out['touches'] != 1 else ''})" + (" on high volume" if rr.get("vol_high") else ""))
    return out


def _trendline_bounce(tl: dict | None, tlb: dict | None) -> dict | None:
    if not tl or not tlb:
        return None
    n = int(tl.get("n_touches") or 0)
    q = min(70, 40 + 10 * max(0, n - 2)) + (10 if tlb.get("vol_high") else 0)
    return {
        "kind": "trendline_bounce", "direction": "up" if tl.get("direction") != "down" else "down",
        "level": _r(tl.get("value_today")), "zone": [_r(tl.get("value_today")), _r(tl.get("value_today"))],
        "touches": n, "line": tl, "vol_high": tlb.get("vol_high"), "candle": tlb.get("candle") or {},
        "quality": int(q), "summary": f"bounce at the trend line ({n} touches)",
    }


def _range_setup(rng: dict | None) -> dict | None:
    if not rng:
        return None
    n = min(int(rng.get("n_low") or 0), int(rng.get("n_high") or 0))
    q = min(70, 40 + 5 * max(0, int(rng.get("n_low") or 0) - 2) + 5 * max(0, int(rng.get("n_high") or 0) - 2))
    return {
        "kind": "range", "direction": "neutral", "level": _r(rng.get("low")), "zone": list(rng.get("zone_low") or [None, None]),
        "touches": n, "rng": rng, "vol_high": None, "candle": {}, "quality": int(q),
        "summary": f"range {rng.get('low')}-{rng.get('high')}",
    }


# ------------------------------------------------------------------- levels + plan
def _nearest_level(levels: list[dict], close: float, *, below: bool) -> float | None:
    cands = [lv["level"] for lv in levels if lv.get("level") is not None
             and ((lv["level"] < close) if below else (lv["level"] > close))]
    if not cands:
        return None
    return max(cands) if below else min(cands)


def levels_for(bars: list[dict], close: float, setup: dict | None, rr: dict | None,
               rng: dict | None) -> dict:
    """``{support, resistance, target_up, target_dn}``: the primary / bounce level
    when there is one, else the nearest defended level under / over the close
    (``mirror_setups``' level search, un-mirrored for the highs), the range edges
    when a range exists."""
    support = resistance = None
    if rng:
        support, resistance = _num(rng.get("low")), _num(rng.get("high"))
    if setup and setup.get("direction") == "up" and setup.get("level") is not None:
        support = setup["level"]
    if setup and setup.get("direction") == "down" and setup.get("level") is not None:
        resistance = setup["level"]
    if rr and rr.get("level") is not None and resistance is None:
        resistance = rr["level"]
    if support is None:
        try:
            support = _nearest_level(mirror_setups._defended_levels(bars), close, below=True)
        except Exception:  # noqa: BLE001
            support = None
    if resistance is None:
        try:
            mirrored = mirror_setups._defended_levels(mirror_setups.mirror_bars(bars))
            resistance = _nearest_level([{"level": -lv["level"]} for lv in mirrored], close, below=False)
        except Exception:  # noqa: BLE001
            resistance = None
    return {"support": _r(support), "resistance": _r(resistance),
            "target_up": _r(resistance), "target_dn": _r(support)}


def plan_for(direction: str, entry: float, atr: float, zone, *, resistance: float | None = None,
             support: float | None = None) -> dict | None:
    """``{entry, stop, target, r}`` (B2.6). Long: ``stop = min(entry - STOP_ATR x ATR,
    zone_lo - LEVEL_PAD_ATR x ATR)``, ``target = entry + TARGET_R x R`` capped at a
    resistance between 1.5R and 2R; short: the mirror, floored at a support. None
    for a neutral setup (the range's edges are its stop) or without an ATR."""
    entry, atr = _num(entry), _num(atr)
    if entry is None or not atr or atr <= 0 or direction not in ("up", "down"):
        return None
    pad = LEVEL_PAD_ATR * atr
    zone = list(zone or [])
    if direction == "up":
        lo = _num(zone[0]) if zone else None
        stop = entry - STOP_ATR * atr
        if lo is not None:
            stop = min(stop, lo - pad)
        r = entry - stop
        target = entry + TARGET_R * r
        res = _num(resistance)
        if res is not None and entry + TARGET_CAP_LO_R * r <= res < target:
            target = res
    else:
        hi = _num(zone[-1]) if zone else None
        stop = entry + STOP_ATR * atr
        if hi is not None:
            stop = max(stop, hi + pad)
        r = stop - entry
        target = entry - TARGET_R * r
        sup = _num(support)
        if sup is not None and target < sup <= entry - TARGET_CAP_LO_R * r:
            target = sup
    return {"entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2), "r": round(r, 2)}


# ------------------------------------------------------------------- read()
def _blank(symbol: str, today, reason: str) -> dict:
    return {
        "symbol": symbol, "as_of": str(today)[:10] if today else None, "close": None, "atr": None,
        "ema": {"e20": None, "e50": None, "e200": None}, "w_ema": None,
        "trend": "unclear", "trend_days": 0, "w_uptrend": None, "slow_drift": None,
        "structure": {"state": "unclear", "reason": reason},
        "sup": None, "tl": None, "tl_bounce": None, "rng": None,
        "setup": None, "setups": [],
        "levels": {"support": None, "resistance": None, "target_up": None, "target_dn": None},
        "earnings": None, "plan": None, "evidence": [reason], "expiries": [],
    }


_FETCH = object()       # read()'s earnings default: fetch; an explicit None means "unknown, do not fetch"


def read(symbol: str, *, bars: list[dict], long_bars: list[dict] | None = None, today=None,
         expiries=(), earnings=_FETCH) -> dict:
    """ONE ChartState dict for ``symbol`` from ``prices.fetch_daily_ohlc``-shaped daily
    bars (B2.1). ``long_bars`` is the ~10-year history when the caller fetched it
    (the weekly reads come from it); ``today`` the ET date (the chain's ``snap_on``);
    ``expiries`` every expiry listed in the snapshot (forwarded to the trend line
    once it exists). ``earnings`` may be supplied (``{"date", "days"}``, or None for
    "unknown"); when it is not, it is read through ``prices.fetch_next_earnings``
    (soft-fail None). Never
    raises on thin or odd bars: the honest read (``trend unclear``, no setup)."""
    sym = (symbol or "").strip().upper()
    bars = list(bars or [])
    if today is None:
        today = bars[-1].get("time") if bars else None
    today_s = str(today)[:10] if today else None
    closes = _closes(bars)
    if len(closes) < 60:
        return _blank(sym, today_s, "not enough price history")
    try:
        an = _call_analyze(bars, long_bars, expiries)
    except Exception as exc:  # noqa: BLE001 - a detector must never take the read down
        return _blank(sym, today_s, f"setup read failed: {exc}")
    if an.get("score") is None and an.get("close") is None:
        return _blank(sym, today_s, an.get("summary") or "not enough price history")

    close = float(an.get("close") or closes[-1])
    atr = atr_of(bars)
    e20, e50, e200 = an.get("ema20"), an.get("ema50"), an.get("ema200")
    sup, tl, tlb, rng = an.get("sup"), an.get("tl"), an.get("tl_bounce"), an.get("rng")

    # ---- trend (B2.2): sideways FIRST (iff rng.sideways), then the stack
    if rng and rng.get("sideways"):
        trend = "sideways"
    elif an.get("uptrend"):
        trend = "up"
    elif e20 is not None and e50 is not None and e200 is not None and e20 < e50 < e200:
        trend = "down"
    else:
        trend = "unclear"
    trend_days = _trend_days(closes)
    slow = _slow_drift(closes, atr)

    # ---- structure (HH/HL vs LH/LL) - soft-fail to unclear
    try:
        from . import structure as _structure
        st = _structure.classify(bars)
        structure = {"state": st.get("state", "unclear"), "reason": st.get("reason", "")}
    except Exception as exc:  # noqa: BLE001
        structure = {"state": "unclear", "reason": f"structure unavailable ({exc})"}

    # ---- the bear-side mirrors (the detectors analyze() does not compute)
    d_pairs = tuple((n, v) for n, v in (("EMA20", e20), ("EMA50", e50)) if v is not None)
    w_pairs = tuple((n, v) for n, v in (("EMA20", an.get("w_ema20")), ("EMA50", an.get("w_ema50"))) if v is not None)
    rr = mirror_setups.find_resistance_reject(bars, d_pairs, w_pairs) if atr else None
    bd = mirror_setups.find_breakdown(bars) if atr else None

    # ---- every setup found
    setups: list[dict] = []
    if sup and atr:
        retest = _breakout_retest(sup, bars, atr)
        setups.append(retest if retest is not None else _setup_from_sup(sup, bars, atr))
    if rr and atr:
        setups.append(_resistance_reject(rr, bars, atr))
    tlb_setup = _trendline_bounce(tl, tlb)
    if tlb_setup:
        setups.append(tlb_setup)
    reb = _ema_rebound(an, close)
    if reb:
        setups.append(reb)
    if bd:
        setups.append(_failed_support(bd))
    rs = _range_setup(rng) if (rng and rng.get("sideways")) else None
    if rs:
        setups.append(rs)
    setups.sort(key=lambda s: -int(s.get("quality") or 0))

    # ---- the primary setup: the best one whose direction agrees with the trend
    agreeing = [s for s in setups if TREND_OF_DIRECTION.get(s["direction"]) == trend]
    setup = agreeing[0] if agreeing else None
    if setup is not None:
        setups = [setup] + [s for s in setups if s is not setup]

    levels = levels_for(bars, close, setup, rr, rng)
    plan = None
    if setup is not None and setup["direction"] in ("up", "down"):
        plan = plan_for(setup["direction"], close, atr, setup.get("zone"),
                        resistance=levels.get("resistance"), support=levels.get("support"))

    # ---- earnings
    if earnings is _FETCH:
        try:
            from . import prices
            earnings = prices.fetch_next_earnings(sym) if sym else None
        except Exception:  # noqa: BLE001
            earnings = None
    if isinstance(earnings, str):
        earnings = {"date": earnings[:10], "days": None}
    if isinstance(earnings, dict) and earnings.get("date") and today_s:
        try:
            earnings = {"date": str(earnings["date"])[:10],
                        "days": (_dt.date.fromisoformat(str(earnings["date"])[:10]) - _dt.date.fromisoformat(today_s)).days}
        except ValueError:
            earnings = None
    elif not (isinstance(earnings, dict) and earnings.get("date")):
        earnings = None

    # ---- evidence
    evidence: list[str] = []
    if trend == "up":
        evidence.append(f"EMA20 > EMA50 > EMA200 for {trend_days} sessions")
    elif trend == "down":
        evidence.append(f"EMA20 < EMA50 < EMA200 for {trend_days} sessions")
    elif trend == "sideways":
        evidence.append("EMAs flat inside a range")
    else:
        evidence.append("EMA stack mixed")
    for s in setups:
        if s.get("summary"):
            evidence.append(s["summary"])
    if structure.get("reason"):
        evidence.append(structure["reason"])
    if an.get("w_uptrend"):
        evidence.append("weekly EMA20 > EMA50 > EMA200")

    w_ema = None
    if an.get("w_ema20") is not None:
        w_ema = {"e20": an.get("w_ema20"), "e50": an.get("w_ema50"), "e200": an.get("w_ema200")}
    return {
        "symbol": sym, "as_of": today_s, "close": round(close, 2), "atr": atr,
        "ema": {"e20": e20, "e50": e50, "e200": e200}, "w_ema": w_ema,
        "trend": trend, "trend_days": trend_days, "w_uptrend": an.get("w_uptrend"), "slow_drift": slow,
        "structure": structure,
        "sup": sup, "tl": tl, "tl_bounce": tlb, "rng": rng,
        "setup": setup, "setups": setups, "levels": levels, "earnings": earnings, "plan": plan,
        "evidence": evidence, "expiries": list(expiries or ()),
    }


def stored_setup(state: dict) -> dict:
    """The ``option_signal.setup`` projection (B2.7 / II.2.6): the primary setup's
    ``kind / direction / level / zone / touches / quality`` (None when there is no
    setup), the chart's ``close / trend / trend_days / atr / ema``, ``plan`` plus the
    flat aliases ``stop`` / ``target``, ``levels``, Part C's ``sup / tl / tl_bounce /
    rng`` verbatim and the ``evidence`` list. Extra context the card's words need
    (``symbol``, ``as_of``, ``structure``, ``w_uptrend``, ``slow_drift``, ``vol_high``,
    ``earnings``) rides along."""
    state = state or {}
    s = state.get("setup") or {}
    plan = state.get("plan")
    return {
        "kind": s.get("kind"), "direction": s.get("direction"), "level": s.get("level"),
        "zone": s.get("zone"), "touches": s.get("touches"), "quality": s.get("quality"),
        "vol_high": s.get("vol_high"), "vol_ratio": s.get("vol_ratio"), "ema_name": s.get("ema"),
        "summary": s.get("summary"),
        "symbol": state.get("symbol"), "as_of": state.get("as_of"),
        "close": state.get("close"), "trend": state.get("trend"), "trend_days": state.get("trend_days"),
        "atr": state.get("atr"), "ema": state.get("ema"), "w_uptrend": state.get("w_uptrend"),
        "slow_drift": state.get("slow_drift"), "structure": state.get("structure"),
        "plan": plan,
        "stop": plan.get("stop") if plan else None, "target": plan.get("target") if plan else None,
        "levels": {"support": (state.get("levels") or {}).get("support"),
                   "resistance": (state.get("levels") or {}).get("resistance")},
        "sup": state.get("sup"), "tl": state.get("tl"), "tl_bounce": state.get("tl_bounce"), "rng": state.get("rng"),
        "earnings": state.get("earnings"),
        "evidence": list(state.get("evidence") or []),
    }


def from_stored(setup: dict | None, *, symbol: str | None = None) -> dict:
    """A ChartState-shaped dict from a STORED ``option_signal.setup`` projection, so
    the picker, the ticket and the recommender accept either shape (the live path
    and the ticket route pass the stored setup). A ChartState passed in comes back
    as it is."""
    if not setup:
        return _blank(symbol or "", None, "no setup stored")
    if isinstance(setup.get("setup"), dict) or ("setups" in setup and "kind" not in setup):
        return setup                                     # already a ChartState
    kind = setup.get("kind")
    primary = None
    if kind:
        primary = {k: setup.get(k) for k in ("kind", "direction", "level", "zone", "touches", "quality",
                                               "vol_high", "vol_ratio", "summary")}
        primary["ema"] = setup.get("ema_name")
        if kind == "trendline_bounce":
            primary["line"] = setup.get("tl")
        if kind == "range":
            primary["rng"] = setup.get("rng")
    levels = dict(setup.get("levels") or {})
    return {
        "symbol": setup.get("symbol") or symbol, "as_of": setup.get("as_of"), "close": setup.get("close"),
        "atr": setup.get("atr"), "ema": setup.get("ema") or {}, "w_ema": None,
        "trend": setup.get("trend") or ({"up": "up", "down": "down", "neutral": "sideways"}.get(setup.get("direction"), "unclear")),
        "trend_days": setup.get("trend_days"), "w_uptrend": setup.get("w_uptrend"), "slow_drift": setup.get("slow_drift"),
        "structure": setup.get("structure") or {"state": "unclear", "reason": ""},
        "sup": setup.get("sup"), "tl": setup.get("tl"), "tl_bounce": setup.get("tl_bounce"), "rng": setup.get("rng"),
        "setup": primary, "setups": [primary] if primary else [],
        "levels": {"support": levels.get("support"), "resistance": levels.get("resistance"),
                   "target_up": levels.get("resistance"), "target_dn": levels.get("support")},
        "earnings": setup.get("earnings"),
        "plan": setup.get("plan") or ({"entry": setup.get("close"), "stop": setup.get("stop"), "target": setup.get("target"),
                                        "r": None} if setup.get("stop") is not None else None),
        "evidence": list(setup.get("evidence") or []), "expiries": [],
    }
