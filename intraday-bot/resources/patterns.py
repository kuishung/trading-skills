"""Pattern detection primitives — Layer-1 resource.

For horizontal support/resistance specifically: the canonical framework
is `strategies-reference/SR.md`. `horizontal_resistance_np` is the
numerical implementation; `sr_levels.horizontal_support_np` mirrors it
for the support side. Both follow SR.md's 1Y/1D, swing-pivot, mountain-
anchored model. Known deviations from SR.md (`min_touches=1`, no
volume tracking, etc.) are documented in `sr_levels.py`'s module
docstring -- check there before adding "missing" S/R features.



Two API conventions live here, side by side:

  A. **List-of-dict** functions — accept `list[dict]` of the bar shape
     `{"t": datetime, "o": float, "h": float, "l": float, "c": float, "v": int}`.
     Designed for live / replay tape and per-bar streaming. Pure Python,
     no numpy needed. (`ema`, `sma`, `vwap`, `find_pivots`,
     `consolidation`, `trend`, `higher_highs_lows`, `bull_flag`,
     `breakout_signal`, `ma_resistance`, `aggregate_to_n_min`.)

  B. **Numpy-array** functions — accept raw `ndarray` columns
     `(opens, highs, lows, closes)` plus an `atr` scalar. Suffixed
     `_np`. Designed for batch historical sweeps where every symbol
     in a 1000+ universe is analysed once. (`atr_wilder_np`, `ema_np`,
     `thrust_bar_np`, `window_slopes_np`, `horizontal_resistance_np`.)

Both conventions share the same contract: NO I/O, NO strategy logic,
NO vendor SDK imports. Side-effect free and deterministic given
identical inputs. Strategies and review tools consume; primitives
never call strategies back.

What's here
-----------
List-of-dict (live tape, per-bar streaming):
    ema(values, period)                 exponential moving average series
    sma(values, period)                 simple moving average series
    vwap(bars)                          cumulative VWAP series
    aggregate_to_n_min(bars, n=5)       resample 1-min bars to N-min bars
    find_pivots(bars, left, right)      local extrema (pivot highs + lows)
    consolidation(bars, lookback,
                  max_range_pct)        tight-range detection
    trend(bars, ema_period, ...)        EMA-slope-based direction
    higher_highs_lows(pivots, ...)      HH/HL sequence classification
    bull_flag(bars, ...)                pole + flag detector
    breakout_signal(bars, level, ...)   has the last bar broken `level`?
    ma_resistance(bars, current_price,
                  periods)              closest MA above current_price

Numpy-array (batch historical sweep, daily scans):
    atr_wilder_np(highs, lows,
                  closes, period=14)    Wilder ATR last value
    ema_np(arr, period)                 EMA series, numpy in/out
    thrust_bar_np(...)                  flush-up / thrust-bar detector
    window_slopes_np(...)               slope-of-highs / slope-of-lows
                                        in a trailing window
    horizontal_resistance_np(...)       mountain-anchored cluster-based
                                        horizontal resistance finder
                                        with range expansion + ceiling

Design notes
------------
Pattern functions accept tunable parameters as keyword-only args so
callers (strategies) can configure them without re-implementing the
core logic. Defaults are reasonable for 1-minute equity bars; for
5-min or daily timeframes the caller should override (e.g. lookback
counts scale with timeframe).

Bar-shape contract is permissive: missing 'v' defaults to 0 and missing
't' is tolerated (some functions need timestamps, others don't).
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

# --- intraday-bot bootstrap (in case future helpers need cross-layer imports) ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---


# =====================================================================
# Signal-math helpers
# =====================================================================

def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average. Same length as `values`. The first
    `period - 1` slots are seeded with the cumulative SMA so the series
    is usable from the very first bar (no NaN warm-up).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out: list[float] = []
    for i, v in enumerate(values):
        if i == 0:
            out.append(float(v))
        elif i < period:
            # SMA warm-up keeps the series usable before the EMA kernel
            # has converged; matches what TradingView shows.
            out.append(sum(values[: i + 1]) / (i + 1))
        else:
            out.append((v - out[-1]) * k + out[-1])
    return out


def sma(values: list[float], period: int) -> list[float]:
    """Simple moving average. Same length as `values`. Slots [0, period-1)
    are filled with the cumulative average (so the series is usable
    early; not None-padded)."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    out: list[float] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(sum(values[: i + 1]) / (i + 1))
        else:
            out.append(sum(values[i + 1 - period : i + 1]) / period)
    return out


def vwap(bars: list[dict]) -> list[float]:
    """Cumulative VWAP, one value per bar. Uses typical price = (H+L+C)/3.
    Zero-volume bars contribute nothing; the running VWAP carries over.
    """
    out: list[float] = []
    cum_pv = 0.0
    cum_v = 0
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3.0
        v = int(b.get("v", 0) or 0)
        cum_pv += typical * v
        cum_v += v
        out.append(cum_pv / cum_v if cum_v > 0 else typical)
    return out


# =====================================================================
# Bar utilities
# =====================================================================

def aggregate_to_n_min(bars: list[dict], n: int = 5) -> list[dict]:
    """Resample 1-min bars to N-min bars. Buckets by floor(minute / n)
    starting from bars[0].t. Each output bar:
       t = first input bar's timestamp in the bucket
       o = first input bar's open
       h = max(highs)
       l = min(lows)
       c = last input bar's close
       v = sum(volumes)
    Skips empty buckets. Caller is responsible for bars being sorted
    by time. Useful for building 5-min PM charts from 1-min IBKR data.
    """
    if n <= 1 or not bars:
        return list(bars)
    out: list[dict] = []
    bucket: list[dict] = []
    bucket_key: tuple | None = None

    def _flush():
        if not bucket:
            return
        out.append({
            "t": bucket[0]["t"],
            "o": bucket[0]["o"],
            "h": max(b["h"] for b in bucket),
            "l": min(b["l"] for b in bucket),
            "c": bucket[-1]["c"],
            "v": sum(int(b.get("v", 0) or 0) for b in bucket),
        })

    for b in bars:
        t = b["t"]
        # Bucket key: (year, month, day, hour, minute // n)
        key = (t.year, t.month, t.day, t.hour, t.minute // n)
        if bucket_key is None or key == bucket_key:
            bucket.append(b)
            bucket_key = key
        else:
            _flush()
            bucket = [b]
            bucket_key = key
    _flush()
    return out


# =====================================================================
# Pivots — the foundation for most patterns
# =====================================================================

def find_pivots(bars: list[dict], *, left: int = 3, right: int = 3) -> list[dict]:
    """Local extrema. A bar i is a pivot HIGH if its `h` is strictly
    greater than the `h` of every bar in [i-left, i) AND every bar in
    (i, i+right]. Same logic for pivot LOW on `l`.

    Returns a list of dicts in chronological order:
        {"idx": int, "t": datetime, "type": "high"|"low", "price": float}

    `left` and `right` control sensitivity. left=right=3 is a reasonable
    default for 1-min intraday bars; larger values (5-10) for daily.
    A single bar can be BOTH a pivot high and low (e.g. an isolated
    spike) — both entries are emitted.
    """
    n = len(bars)
    out: list[dict] = []
    for i in range(left, n - right):
        h = bars[i]["h"]
        l = bars[i]["l"]
        if all(bars[j]["h"] < h for j in range(i - left, i)) and \
           all(bars[j]["h"] < h for j in range(i + 1, i + 1 + right)):
            out.append({"idx": i, "t": bars[i].get("t"),
                        "type": "high", "price": h})
        if all(bars[j]["l"] > l for j in range(i - left, i)) and \
           all(bars[j]["l"] > l for j in range(i + 1, i + 1 + right)):
            out.append({"idx": i, "t": bars[i].get("t"),
                        "type": "low", "price": l})
    return out


# =====================================================================
# Consolidation (tight-range detection)
# =====================================================================

def consolidation(
    bars: list[dict],
    *,
    lookback_bars: int = 15,
    max_range_pct: float = 2.0,
) -> dict:
    """Tight-range check on the LAST `lookback_bars` bars.

    range_pct = (max(highs) - min(lows)) / max(highs) * 100

    Returns:
      {is_consol, high, low, range_pct, n_bars, reason?}
    `is_consol` is True iff range_pct <= max_range_pct and n_bars > 1.
    """
    if len(bars) < 2:
        return {"is_consol": False, "n_bars": len(bars),
                "high": None, "low": None, "range_pct": None,
                "reason": "too few bars"}
    window = bars[-lookback_bars:]
    hi = max(b["h"] for b in window)
    lo = min(b["l"] for b in window)
    if hi <= 0:
        return {"is_consol": False, "n_bars": len(window),
                "high": hi, "low": lo, "range_pct": None,
                "reason": "invalid high"}
    range_pct = (hi - lo) / hi * 100.0
    return {
        "is_consol": range_pct <= max_range_pct,
        "n_bars": len(window),
        "high": hi,
        "low": lo,
        "range_pct": range_pct,
        "max_range_pct_threshold": max_range_pct,
    }


# =====================================================================
# Trend (EMA-slope direction)
# =====================================================================

def trend(
    bars: list[dict],
    *,
    ema_period: int = 20,
    slope_lookback: int = 5,
    up_threshold_pct_per_bar: float = 0.05,
    down_threshold_pct_per_bar: float = -0.05,
) -> dict:
    """EMA-slope-based trend classification.

    Computes EMA(ema_period) on closes. Slope is measured as
       (ema_now - ema_lookback_ago) / ema_lookback_ago / lookback * 100
    in percent per bar.

    direction:
      "up"        slope > up_threshold_pct_per_bar
      "down"      slope < down_threshold_pct_per_bar
      "sideways"  else
      "unknown"   insufficient bars
    """
    n_needed = ema_period + slope_lookback
    if len(bars) < n_needed:
        return {"direction": "unknown",
                "reason": f"need >= {n_needed} bars, have {len(bars)}"}
    closes = [b["c"] for b in bars]
    e = ema(closes, ema_period)
    ema_now = e[-1]
    ema_lookback_ago = e[-1 - slope_lookback]
    if ema_lookback_ago == 0:
        return {"direction": "unknown", "reason": "zero EMA lookback"}
    slope = ((ema_now - ema_lookback_ago) / ema_lookback_ago) \
        / slope_lookback * 100.0
    if slope > up_threshold_pct_per_bar:
        direction = "up"
    elif slope < down_threshold_pct_per_bar:
        direction = "down"
    else:
        direction = "sideways"
    return {
        "direction": direction,
        "ema_period": ema_period,
        "ema_now": ema_now,
        "ema_lookback_ago": ema_lookback_ago,
        "slope_pct_per_bar": slope,
    }


# =====================================================================
# Higher-highs / higher-lows sequence analysis
# =====================================================================

def higher_highs_lows(pivots: list[dict], *, lookback_pivots: int = 4) -> dict:
    """Classify trend structure from a pivot list.

    Counts how many consecutive pivot-high pairs are HH (b > a) and how
    many pivot-low pairs are HL (b > a) within the last `lookback_pivots`
    pivots. Returns:

      "uptrend"   — every recent HH and HL is monotonically rising
      "downtrend" — every recent pair is LH and LL (no HH or HL at all)
      "mixed"     — partial / messy
      "unknown"   — insufficient pivots
    """
    if len(pivots) < 2:
        return {"structure": "unknown",
                "reason": "need >= 2 pivots", "hh_count": 0, "hl_count": 0}
    recent = pivots[-lookback_pivots:]
    highs = [p for p in recent if p["type"] == "high"]
    lows = [p for p in recent if p["type"] == "low"]
    hh = sum(1 for a, b in zip(highs, highs[1:]) if b["price"] > a["price"])
    hl = sum(1 for a, b in zip(lows, lows[1:]) if b["price"] > a["price"])
    n_hp = max(len(highs) - 1, 0)
    n_lp = max(len(lows) - 1, 0)
    if (n_hp + n_lp) == 0:
        structure = "unknown"
    elif hh == n_hp and hl == n_lp and (n_hp + n_lp) > 0:
        structure = "uptrend"
    elif hh == 0 and hl == 0 and (n_hp + n_lp) > 0:
        structure = "downtrend"
    else:
        structure = "mixed"
    return {
        "structure": structure,
        "hh_count": hh,
        "hl_count": hl,
        "n_high_pairs": n_hp,
        "n_low_pairs": n_lp,
        "pivots_inspected": len(recent),
    }


# =====================================================================
# Bull flag — the workhorse pattern for GUNS setups 3 & 4
# =====================================================================

def bull_flag(
    bars: list[dict],
    *,
    pole_min_pct: float = 2.0,
    flag_min_bars: int = 3,
    flag_max_bars: int = 15,
    flag_max_range_pct_of_pole: float = 0.5,
    flag_max_pullback_pct_of_pole: float = 0.5,
    pivot_left: int = 2,
    pivot_right: int = 2,
) -> dict:
    """Detect a bull flag at the END of the bar series.

    A bull flag = strong UPWARD pole + sideways/slightly-down flag
    at the top, with the flag staying near the pole's high (not
    retracing too deep).

    Algorithm:
      1. Find pivots (left=pivot_left, right=pivot_right).
      2. Take the latest pivot HIGH = pole top.
      3. Take the latest pivot LOW BEFORE the pole top = pole base.
      4. Pole height (%) = (top - base) / base * 100; must be
         >= pole_min_pct.
      5. Flag bars = bars AFTER the pole-top index.
      6. Flag count in [flag_min_bars, flag_max_bars].
      7. Flag range = max(flag_highs) - min(flag_lows); must be
         < pole_height * flag_max_range_pct_of_pole.
      8. Pullback = pole_top - min(flag_lows); must be
         < pole_height * flag_max_pullback_pct_of_pole.

    Returns a dict with `detected` and full evidence. When not
    detected, `reason` explains which check failed.

    Tuning by timeframe:
      1-min PM bars  pole_min_pct=2.0,  flag_max_bars=15
      5-min PM bars  pole_min_pct=2.0,  flag_max_bars=6
      Daily          pole_min_pct=5.0,  flag_max_bars=10
    """
    if not bars:
        return {"detected": False, "reason": "no bars"}
    pivots = find_pivots(bars, left=pivot_left, right=pivot_right)
    highs = [p for p in pivots if p["type"] == "high"]
    if not highs:
        return {"detected": False, "reason": "no pivot high"}
    # Pole top = the HIGHEST pivot high in the visible bar set.
    # Using "latest" instead would let flag-internal micro-pivots
    # masquerade as the pole top, shrinking the apparent pole and
    # leaving only 1-2 bars of "flag" after it. The biggest swing
    # is what we care about; everything after it is the flag.
    pole_top = max(highs, key=lambda p: p["price"])
    # Pole base = the LOWEST pivot low BEFORE the pole top, again
    # picking the biggest swing rather than the latest swing.
    lows_before = [p for p in pivots
                   if p["type"] == "low" and p["idx"] < pole_top["idx"]]
    if not lows_before:
        return {"detected": False, "reason": "no pivot low before pivot high"}
    pole_base = min(lows_before, key=lambda p: p["price"])

    pole_height = pole_top["price"] - pole_base["price"]
    if pole_base["price"] <= 0:
        return {"detected": False, "reason": "invalid pole base"}
    pole_height_pct = pole_height / pole_base["price"] * 100.0
    if pole_height_pct < pole_min_pct:
        return {"detected": False, "reason": "pole too small",
                "pole_height_pct": pole_height_pct,
                "pole_min_pct_threshold": pole_min_pct}

    flag_bars = bars[pole_top["idx"] + 1:]
    if len(flag_bars) < flag_min_bars:
        return {"detected": False, "reason": "flag too few bars",
                "flag_n_bars": len(flag_bars),
                "flag_min_bars_threshold": flag_min_bars}
    if len(flag_bars) > flag_max_bars:
        return {"detected": False, "reason": "flag too many bars",
                "flag_n_bars": len(flag_bars),
                "flag_max_bars_threshold": flag_max_bars}

    flag_high = max(b["h"] for b in flag_bars)
    flag_low = min(b["l"] for b in flag_bars)
    flag_range = flag_high - flag_low
    flag_range_vs_pole = flag_range / pole_height if pole_height > 0 else math.inf
    if flag_range_vs_pole > flag_max_range_pct_of_pole:
        return {"detected": False, "reason": "flag range too wide",
                "flag_range_vs_pole": flag_range_vs_pole,
                "threshold": flag_max_range_pct_of_pole}

    pullback = pole_top["price"] - flag_low
    pullback_vs_pole = pullback / pole_height if pole_height > 0 else math.inf
    if pullback_vs_pole > flag_max_pullback_pct_of_pole:
        return {"detected": False, "reason": "flag pulled back too deep",
                "pullback_vs_pole": pullback_vs_pole,
                "threshold": flag_max_pullback_pct_of_pole}

    closes = [b["c"] for b in flag_bars]
    if closes[0] != 0 and len(closes) > 1:
        slope = (closes[-1] - closes[0]) / closes[0] / len(closes) * 100.0
    else:
        slope = 0.0

    return {
        "detected": True,
        "pole_base_price": pole_base["price"],
        "pole_base_t": pole_base.get("t"),
        "pole_base_idx": pole_base["idx"],
        "pole_top_price": pole_top["price"],
        "pole_top_t": pole_top.get("t"),
        "pole_top_idx": pole_top["idx"],
        "pole_height": pole_height,
        "pole_height_pct": pole_height_pct,
        "flag_n_bars": len(flag_bars),
        "flag_high": flag_high,
        "flag_low": flag_low,
        "flag_range": flag_range,
        "flag_range_vs_pole": flag_range_vs_pole,
        "pullback": pullback,
        "pullback_vs_pole": pullback_vs_pole,
        "flag_slope_pct_per_bar": slope,
        # The price level a strategy should set its buy-stop above
        # to enter on the flag break.
        "breakout_trigger": flag_high,
    }


# =====================================================================
# Breakout signal — has the latest bar broken `level`?
# =====================================================================

def breakout_signal(
    bars: list[dict],
    level: float,
    *,
    direction: str = "up",
    min_volume_mult: float = 1.0,
) -> dict:
    """Did the last bar break `level` in the given direction?

    direction:
      "up"   — last close AND last high above level
      "down" — last close AND last low below level

    `min_volume_mult` is an optional volume confirmation: last bar's
    volume must be >= avg(prior bar volumes) * min_volume_mult.
    Set to 0 to disable. Returns `vol_confirms` as None when there
    are no prior bars to compute an average.
    """
    if not bars:
        return {"broken": False, "reason": "no bars"}
    last = bars[-1]
    if direction == "up":
        broken = last["c"] > level and last["h"] > level
    elif direction == "down":
        broken = last["c"] < level and last["l"] < level
    else:
        return {"broken": False, "reason": f"unknown direction {direction!r}"}

    prior_vols = [int(b.get("v", 0) or 0) for b in bars[:-1]]
    last_vol = int(last.get("v", 0) or 0)
    if prior_vols and sum(prior_vols) > 0:
        avg_prior_vol = sum(prior_vols) / len(prior_vols)
        vol_confirms = last_vol >= avg_prior_vol * min_volume_mult
    else:
        avg_prior_vol = None
        vol_confirms = None

    return {
        "broken": broken,
        "level": level,
        "direction": direction,
        "last_close": last["c"],
        "last_high": last["h"],
        "last_low": last["l"],
        "last_volume": last_vol,
        "avg_prior_volume": avg_prior_vol,
        "vol_confirms": vol_confirms,
        "min_volume_mult_threshold": min_volume_mult,
    }


# =====================================================================
# Moving-average resistance (used for the "Daily MA resistance" ask)
# =====================================================================

def ma_resistance(
    bars: list[dict],
    current_price: float,
    *,
    periods: tuple[int, ...] = (20, 50, 200),
    ma_kind: str = "sma",
) -> dict:
    """Find the closest moving-average value ABOVE current_price.

    Computes MAs of each period on closes and finds the lowest one
    that's strictly greater than current_price. That's the "next
    resistance overhead" anchor a strategy can use as a take-profit
    or as a 'too-close-to-resistance' rejection.

    `ma_kind`: "sma" or "ema".

    Returns:
      {
        "current_price": float,
        "mas": {period: ma_value, ...},
        "resistance_above": ma_value or None,
        "resistance_period": period or None,
        "distance_pct": pct above current or None,
      }

    Typically called with DAILY bars to find the 20/50/200-SMA acting
    as overhead resistance — but works on any timeframe.
    """
    ma_func = sma if ma_kind == "sma" else ema
    if not bars:
        return {"current_price": current_price, "mas": {},
                "resistance_above": None,
                "resistance_period": None,
                "distance_pct": None,
                "reason": "no bars"}
    closes = [b["c"] for b in bars]
    mas: dict[int, float] = {}
    for p in periods:
        if len(closes) < p:
            # MA undefined for too-short series; skip silently
            continue
        s = ma_func(closes, p)
        mas[p] = s[-1]
    above = [(p, v) for p, v in mas.items() if v > current_price]
    if not above:
        return {"current_price": current_price, "mas": mas,
                "resistance_above": None,
                "resistance_period": None,
                "distance_pct": None,
                "reason": "no MA above current price"}
    # Pick the closest one
    above.sort(key=lambda pv: pv[1])
    p_chosen, v_chosen = above[0]
    return {
        "current_price": current_price,
        "mas": mas,
        "resistance_above": v_chosen,
        "resistance_period": p_chosen,
        "distance_pct": (v_chosen - current_price) / current_price * 100.0,
        "ma_kind": ma_kind,
    }


# =====================================================================
# Numpy-array primitives — daily-bar batch analysis
# =====================================================================
#
# These are the same kinds of primitives as above but optimised for
# batch sweeps: a scanner that runs every symbol in a 1500-name
# universe through the same checks once a day.
#
# Input convention: numpy float arrays of equal length, one element
# per bar, in chronological order. The caller is responsible for
# building (opens, highs, lows, closes) once from its bar source
# (e.g. resources.bars_store.read_bars).
#
# All functions are pure and import numpy lazily so callers that
# only use the list-of-dict API above don't pay the numpy import
# cost.

def _np():
    """Lazy numpy import — module-level import would force numpy on
    every consumer of `patterns`, including ones that only use the
    list-of-dict API."""
    import numpy as np  # type: ignore
    return np


def atr_wilder_np(highs, lows, closes, period: int = 14) -> float:
    """Wilder ATR — returns the LAST-bar value as a Python float.

    Inputs are numpy arrays (or anything numpy can convert) of equal
    length. Bootstraps the first `period` bars with the mean of TR,
    then iterates the Wilder smoothing kernel:
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

    Returns 0.0 if there are too few bars (< period + 1). Callers
    typically gate on `atr <= 0` as "not enough history".
    """
    np = _np()
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    if len(h) < period + 1:
        return 0.0
    tr = np.maximum.reduce([
        h[1:] - l[1:],
        np.abs(h[1:] - c[:-1]),
        np.abs(l[1:] - c[:-1]),
    ])
    atr = np.zeros_like(tr)
    atr[:period] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return float(atr[-1])


def ema_np(arr, period: int):
    """EMA series — numpy in, numpy out. Seeded with the first value
    (NOT the SMA bootstrap that the list-of-dict `ema` uses) so this
    is the cheap variant for batch jobs that don't need TradingView-
    parity. Callers that DO need parity should use `patterns.ema(list)`
    instead."""
    np = _np()
    a = np.asarray(arr, dtype=float)
    out = np.zeros_like(a)
    if len(a) == 0:
        return out
    out[0] = a[0]
    alpha = 2.0 / (period + 1)
    for i in range(1, len(a)):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def thrust_bar_np(opens, highs, lows, closes, atr: float,
                  *,
                  body_atr_min: float = 1.5,
                  prior_high_window: int = 5,
                  lookback: int = 15,
                  ) -> int | None:
    """Find the most recent "thrust bar" in the last `lookback` bars.

    A thrust bar (also called a flush-up bar) is a single bullish
    candle big enough to puncture the prior range:
       - body (close - open) > body_atr_min × ATR
       - close > max(prior `prior_high_window` bars' highs)
       - body must be bullish (close > open)

    Returns the offset from the end as a NEGATIVE int (e.g. -11 if
    the bar was 11 days ago) or None if no thrust bar exists in the
    window. Multiple matches → the most recent one wins.

    Use case: DITP's "flush-up profit-taking" caution — a thrust bar
    near a resistance traps quick-profit holders at that level, so
    any retest carries elevated selling pressure.
    """
    np = _np()
    if atr <= 0:
        return None
    o = np.asarray(opens, dtype=float)
    h = np.asarray(highs, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = len(c)
    lookback = min(lookback, n)
    most_recent_match: int | None = None
    for offset in range(-lookback, 0):
        if n + offset - prior_high_window < 0:
            continue
        body = abs(float(c[offset]) - float(o[offset]))
        if body <= body_atr_min * atr:
            continue
        if float(c[offset]) <= float(o[offset]):
            continue  # body must be bullish
        prior_high = h[offset - prior_high_window:offset].max()
        if float(c[offset]) > prior_high:
            most_recent_match = offset
    return most_recent_match


def window_slopes_np(opens, highs, lows, closes, resistance: float,
                     atr: float,
                     *,
                     lookback_bars: int = 10,
                     tick_size: float = 0.01,
                     cluster_tolerance_ticks: int = 3,
                     ) -> dict:
    """Slope-of-highs / slope-of-lows + window dimensions for the LAST
    `lookback_bars` bars. Used to classify a window's shape (rectangle
    vs ascending triangle vs direct approach) at a resistance level.

    Returns a dict with:
      slope_highs_pct_per_bar      polyfit(highs)[0] / last_close
      slope_lows_pct_per_bar       polyfit(lows)[0] / last_close
      rect_height_atr              (max(highs) - min(lows)) / atr
      highs_near_resistance        N of recent highs within cluster
                                   band of `resistance`
      is_bullish_markup            last bar passes the bullish-markup
                                   anatomy test (caller decides body /
                                   tail thresholds)
      last_open / last_high /
      last_low / last_close        the actual signal-candle anatomy
      bars_inspected               actual window size (clipped to len)

    Pure geometric output — the caller maps it to strategy semantics
    (DITP's A/B/C variants, ORB's range, etc.) without re-fitting.
    """
    np = _np()
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens, dtype=float)
    N = min(lookback_bars, len(h))
    if N < 2:
        return {
            "slope_highs_pct_per_bar": 0.0,
            "slope_lows_pct_per_bar": 0.0,
            "rect_height_atr": float("inf") if atr <= 0 else 0.0,
            "highs_near_resistance": 0,
            "is_bullish_markup": False,
            "last_open": float(o[-1]) if len(o) else 0.0,
            "last_high": float(h[-1]) if len(h) else 0.0,
            "last_low":  float(l[-1]) if len(l) else 0.0,
            "last_close": float(c[-1]) if len(c) else 0.0,
            "bars_inspected": N,
        }
    rh = h[-N:]
    rl = l[-N:]
    x = np.arange(N, dtype=float)
    slope_h = float(np.polyfit(x, rh, 1)[0])
    slope_l = float(np.polyfit(x, rl, 1)[0])
    px = float(c[-1])
    rect_height = float(rh.max() - rl.min())
    # Absolute tick-based "near resistance" count per user rule
    # 2026-05-27 ("the placeholder cannot be too wide... plus minus 3 tick").
    cluster_band = cluster_tolerance_ticks * tick_size
    near = int(np.sum(np.abs(rh - resistance) <= cluster_band)) \
        if resistance > 0 else 0
    return {
        "slope_highs_pct_per_bar": (slope_h / px) if px else 0.0,
        "slope_lows_pct_per_bar":  (slope_l / px) if px else 0.0,
        "rect_height_atr":         (rect_height / atr) if atr > 0 else float("inf"),
        "highs_near_resistance":   near,
        "last_open":   float(o[-1]),
        "last_high":   float(h[-1]),
        "last_low":    float(l[-1]),
        "last_close":  float(c[-1]),
        "bars_inspected": N,
    }


def horizontal_resistance_np(highs, lows, closes, current_price: float,
                             atr: float,
                             *,
                             lookback: int = 120,
                             swing_radius: int = 3,
                             min_touches: int = 1,
                             tick_size: float = 0.01,
                             cluster_tolerance_ticks: int = 3,
                             range_pct: float = 0.02,
                             mountain_min_age_bars: int = 5,
                             mountain_pullback_atr: float = 0.5,
                             max_below_window_high_pct: float = 1.0,
                             ) -> dict | None:
    """Find THE MOST RECENT mountain-anchored horizontal resistance
    above `current_price` — the most-recent-in-time mountain top above
    current. User clarification 2026-05-27 (AAOI case): "nearest" means
    most recent in time, not lowest in price. AAOI has $233.67 (May 13,
    9 days ago) and $191.87 (May 1, 17 days ago) both above current
    $178. The lowest-above rule picked $191.87 incorrectly; the
    most-recent rule picks $233.67 which is the active resistance.

    Each mountain peak is an independent P2 setup: the most-recent
    one is the level the current price action is unfolding around.
    Older mountains (further back in time) — even if they happen to
    be lower in price than the most recent — are stale levels that
    the market has moved past.

    Two-stage process:

    1. Enumerate every swing high in `lookback` (a bar is a swing high
       if it's the local max within ± `swing_radius`).
    2. Filter to "mountain tops" — swing highs that are
         (a) old enough: ≥ `mountain_min_age_bars` from the latest bar
         (b) followed by a real pullback: at least one subsequent low
             ≤ peak − `mountain_pullback_atr` × atr

    The level chosen = the MOST RECENT IN TIME mountain peak above
    `current_price` (= the resistance the current price action is
    unfolding around). If no mountain qualifies, falls back to the
    most-recent non-mountain swing above — caller's downstream
    cautioner can tag this as "fresh resistance".

    The RANGE around that level = consensus of mountain tops within
    ± `range_pct` of the chosen level. Non-mountain swings are NOT
    part of the consensus.

    Ceiling gate (DISABLED by default since 2026-05-27 reintegration):
    `max_below_window_high_pct=1.0` effectively disables the gate.
    User framework: higher mountains above the chosen level are FUTURE
    P2 setups, not disqualifiers. The TSLA-style "midway-bounce-in-
    a-downtrend" discrimination this gate used to provide is now
    handled by the EMA-stack trend gate in P1/P2/P3 detectors. Set
    `max_below_window_high_pct=0.02` (legacy strict value) if you
    need the old behavior.

    Cluster gate: the chosen level must have ≥ `min_touches` swing
    highs (mountain OR non-mountain) within ± `cluster_tolerance_ticks
    × tick_size` (default ±3 ticks at $0.01 tick size = ±$0.03).
    Switched from a percentage tolerance to absolute-tick tolerance
    on 2026-05-27 per user rule: *"the placeholder cannot be too
    wide"* + *"plus minus 3 tick"*. The previous 1% percentage scaled
    badly with price -- for a $400 stock that meant ±$4, far wider
    than the user intent. Absolute ticks stays tight across the
    universe. Multi-touch confirmation still surfaces via the
    `cluster_touches` field (callers can score on it).

    Mountain validation defaults (lowered 2026-05-27 from
    `mountain_min_age_bars=15, mountain_pullback_atr=2.0` to
    `5, 0.5`): the user's chart-reading framework identifies levels
    that have been "actively tested" by recent price action, not by
    requiring weeks of age and deep structural pullback. GOOGL's
    $382.77 valley (9 trading days old, 2.68-ATR rally since) doesn't
    satisfy the strict gates but IS a P1 support per the user's read.
    The relaxed defaults align with this. Minimum 5 trading days is
    just above the swing_radius=3 floor (a swing low at index i
    requires i+3 bars to confirm) so we don't surface unconfirmed
    swings. DITP P2 scanner keeps strict tuning (15 / 2.0) via
    explicit P2Config override to preserve legacy watchlist quality.

    Returns a dict or None:
      level                  resistance price (the immediate-nearest
                             mountain climax, or fresh-resistance fallback)
      cluster_touches        # swing highs in cluster band
      mountain_anchors       # of those touches that qualified as
                             mountain tops
      range_low / range_high # consensus zone (mountains only)
      range_mountains        # mountains inside the range zone
                             (0 if fresh-resistance fallback)

    Returns None when there are not enough bars, no swings above
    current price, ceiling gate fails, or cluster fails.
    """
    np = _np()
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    lb = min(lookback, len(h))
    window_h = h[-lb:]
    window_l = l[-lb:]
    if len(window_h) < swing_radius * 2 + 1:
        return None

    # 1. Swing highs in window
    swings: list[tuple[int, float]] = []
    for i in range(swing_radius, len(window_h) - swing_radius):
        if window_h[i] == window_h[i - swing_radius:i + swing_radius + 1].max():
            swings.append((i, float(window_h[i])))
    if len(swings) < min_touches:
        return None

    # 2. Mountain filter: old enough + pullback below peak
    mountains: list[tuple[int, float]] = []
    last_idx = len(window_h) - 1
    for i, hi in swings:
        if (last_idx - i) < mountain_min_age_bars:
            continue
        if i + 1 >= len(window_l):
            continue
        post = window_l[i + 1:]
        if (post.min() if len(post) else float("inf")) <= hi - mountain_pullback_atr * atr:
            mountains.append((i, hi))
    mountain_idxs = {i for i, _ in mountains}
    max_mountain_high = max((hi for _, hi in mountains), default=0.0)

    # 3. Filter to peaks ABOVE current price, then pick the most-recent
    #    in time. v1.5.0 reverts the v1.4.0 coupled rule that returned
    #    None when the SINGLE most-recent peak in the entire lookback
    #    was below current. The coupling was over-restrictive: NVDA
    #    2026-05-28 has $236.54 (8d, above) AND $212.19 (143d, below)
    #    -- BOTH are valid (R above = $236.54 next resistance; broken-R
    #    = $212.19 polarity-flip P3 candidate). The two finders are now
    #    independent; downstream P3 detector's anatomy gates filter
    #    bad candidates (upper-tail rejection, etc).
    mountains_above = [(i, hi) for i, hi in mountains if hi > current_price]
    swings_above = [(i, hi) for i, hi in swings if hi > current_price]
    if mountains_above:
        i_imm, h_imm = max(mountains_above, key=lambda x: x[0])
    elif swings_above:
        i_imm, h_imm = max(swings_above, key=lambda x: x[0])
    else:
        return None
    level = float(h_imm)

    # Range = consensus of mountains within ± range_pct of level.
    # Non-mountain swings are not part of the consensus (see DITP user
    # rule "outlier wicks aren't part of resistance").
    if mountains_above:
        range_mtns_only = [hi for _, hi in mountains_above
                           if abs(hi - level) / level <= range_pct]
    else:
        range_mtns_only = [level]
    range_low = min(range_mtns_only)
    range_high = max(range_mtns_only)
    n_range_mountains = len(range_mtns_only) if mountains_above else 0

    # Ceiling gate: chosen level must be the highest mountain
    if max_mountain_high > 0 and level < max_mountain_high * (1 - max_below_window_high_pct):
        return None

    # Cluster touches around the chosen level. Absolute tick-based
    # tolerance per user rule 2026-05-27: "the placeholder cannot be
    # too wide... plus minus 3 tick".
    cluster_band = cluster_tolerance_ticks * tick_size
    cluster = [(i, hh) for i, hh in swings
               if abs(hh - level) <= cluster_band]
    if len(cluster) < min_touches:
        return None
    n_mountains_in_cluster = sum(1 for i, _ in cluster if i in mountain_idxs)
    return {
        "level": level,
        "cluster_touches": len(cluster),
        "mountain_anchors": n_mountains_in_cluster,
        "range_low": range_low,
        "range_high": range_high,
        "range_mountains": n_range_mountains,
    }


# =====================================================================
# CLI demo with synthetic bars
# =====================================================================

def _make_synthetic_bars(scenario: str = "bull_flag") -> list[dict]:
    """Generate small bar series for testing."""
    base_t = datetime(2026, 5, 21, 9, 30)
    bars: list[dict] = []
    if scenario == "bull_flag":
        # Lead-in with a clear dip so find_pivots can fix a pivot LOW
        # before the pole takes off.
        starts = [10.10, 10.05, 9.95, 9.90, 9.95, 10.05]
        for i, c in enumerate(starts):
            o = c - 0.02
            bars.append({"t": base_t + timedelta(minutes=i),
                         "o": o, "h": c + 0.03, "l": o - 0.03,
                         "c": c, "v": 25000})
        # Strong upward pole (12 bars, ~+6%).
        pole_start_t = base_t + timedelta(minutes=len(bars))
        for i in range(12):
            o = 10.05 + i * 0.05
            c = o + 0.05
            bars.append({"t": pole_start_t + timedelta(minutes=i),
                         "o": o, "h": c + 0.02, "l": o - 0.02,
                         "c": c, "v": 60000 + i * 2000})
        # Flag — 6 sideways/slight-down bars near pole top (~$10.65).
        flag_start_t = base_t + timedelta(minutes=len(bars))
        for i in range(6):
            o = 10.62 + (i % 3) * 0.02 - 0.01
            c = o + 0.01
            bars.append({"t": flag_start_t + timedelta(minutes=i),
                         "o": o, "h": c + 0.03, "l": o - 0.04,
                         "c": c, "v": 35000 - i * 1500})
    elif scenario == "consolidation":
        # 20 bars in a 1% range around $10
        for i in range(20):
            o = 10.0 + (((i * 7) % 11) - 5) * 0.005
            c = o + 0.005
            bars.append({"t": base_t + timedelta(minutes=i),
                         "o": o, "h": o + 0.01, "l": o - 0.01,
                         "c": c, "v": 20000})
    elif scenario == "uptrend":
        # 30 bars steady rise
        for i in range(30):
            o = 10.0 + i * 0.05
            c = o + 0.04
            bars.append({"t": base_t + timedelta(minutes=i),
                         "o": o, "h": c + 0.01, "l": o - 0.01,
                         "c": c, "v": 25000 + i * 100})
    return bars


def _cli(argv: list[str]) -> int:
    """py resources/patterns.py [bull_flag|consolidation|uptrend|aggregate]"""
    scenario = argv[0] if argv else "bull_flag"

    if scenario == "aggregate":
        bars1 = _make_synthetic_bars("uptrend")
        bars5 = aggregate_to_n_min(bars1, n=5)
        print(f"1-min bars: {len(bars1)} -> 5-min bars: {len(bars5)}")
        for b in bars5:
            print(f"  {b['t'].strftime('%H:%M')}  "
                  f"O={b['o']:.2f} H={b['h']:.2f} L={b['l']:.2f} "
                  f"C={b['c']:.2f} V={b['v']}")
        return 0

    bars = _make_synthetic_bars(scenario)
    print(f"Synthetic '{scenario}' scenario: {len(bars)} bars")
    print()

    print("--- find_pivots(left=2, right=2) ---")
    pivs = find_pivots(bars, left=2, right=2)
    for p in pivs:
        print(f"  idx={p['idx']:>2}  {p['type']:<4}  ${p['price']:.2f}  {p['t'].strftime('%H:%M')}")
    print()

    print("--- consolidation(lookback=10, max_range_pct=2.0) ---")
    c = consolidation(bars, lookback_bars=10, max_range_pct=2.0)
    for k, v in c.items():
        print(f"  {k}: {v}")
    print()

    print("--- trend(ema_period=10) ---")
    tr = trend(bars, ema_period=10, slope_lookback=3)
    for k, v in tr.items():
        print(f"  {k}: {v}")
    print()

    print("--- higher_highs_lows ---")
    hh = higher_highs_lows(pivs)
    for k, v in hh.items():
        print(f"  {k}: {v}")
    print()

    print("--- bull_flag ---")
    bf = bull_flag(bars)
    for k, v in bf.items():
        if hasattr(v, "strftime"):
            v = v.strftime("%H:%M")
        elif isinstance(v, float):
            v = f"{v:.4f}"
        print(f"  {k}: {v}")
    print()

    if bf.get("detected"):
        print(f"--- breakout_signal(level=flag_high={bf['breakout_trigger']:.2f}) ---")
        b = breakout_signal(bars, bf["breakout_trigger"], direction="up", min_volume_mult=1.0)
        for k, v in b.items():
            print(f"  {k}: {v}")
        print()

    print("--- ma_resistance(current_price=last_close) ---")
    last = bars[-1]["c"]
    mr = ma_resistance(bars, last, periods=(5, 10, 20), ma_kind="sma")
    for k, v in mr.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
