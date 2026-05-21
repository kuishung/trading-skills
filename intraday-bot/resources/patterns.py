"""Pattern detection primitives — Layer-1 resource.

Pure functions on the standard bar dict shape:
    {"t": datetime, "o": float, "h": float, "l": float, "c": float, "v": int}

No I/O. No strategy logic. No vendor SDK imports. Every function is
side-effect free and deterministic given identical inputs, which means
you can unit-test it with synthetic bars and use it identically on
live, replayed, or backtest data.

Functions return STRUCTURED dicts (not bare booleans) so a calling
strategy can use the same output for both an eligibility check AND
a journal-evidence payload — no second-pass computation needed.

What's here
-----------
Signal-math helpers:
    ema(values, period)                 exponential moving average series
    sma(values, period)                 simple moving average series
    vwap(bars)                          cumulative VWAP series

Bar utilities:
    aggregate_to_n_min(bars, n=5)       resample 1-min bars to N-min bars

Pattern primitives:
    find_pivots(bars, left, right)      local extrema (pivot highs + lows)
    consolidation(bars, lookback,
                  max_range_pct)        tight-range detection
    trend(bars, ema_period, ...)        EMA-slope-based direction
    higher_highs_lows(pivots, ...)      HH/HL sequence classification
    bull_flag(bars, ...)                pole + flag detector

Level / breakout:
    breakout_signal(bars, level, ...)   has the last bar broken `level`?
    ma_resistance(bars, current_price,
                  periods)              closest MA above current_price

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
