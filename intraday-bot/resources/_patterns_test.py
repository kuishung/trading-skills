"""Evaluation harness for resources/patterns.py.

Crafted positive / negative / edge cases for every public function.
Each test asserts something specific and reports PASS/FAIL with the
delta when it fails.

Run:
    py resources/_patterns_test.py
    py resources/_patterns_test.py --verbose
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patterns as P  # noqa: E402


# ---------- tiny test framework ----------

PASS = "PASS"
FAIL = "FAIL"


class Result:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = ""):
        if ok:
            self.passed += 1
            if "--verbose" in sys.argv:
                print(f"  {PASS} {label}")
        else:
            self.failed += 1
            line = f"  {FAIL} {label}"
            if detail:
                line += f"  ({detail})"
            print(line)
            self.failures.append(label)

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} checks passed", end="")
        if self.failed:
            print(f" — {self.failed} FAILED")
        else:
            print(" -- clean")
        return 0 if self.failed == 0 else 1


def near(a: float, b: float, tol: float = 1e-6) -> bool:
    """Float comparison with tolerance."""
    return abs(a - b) <= tol


def make_bars(prices: list[float], *, vol: int = 10_000,
              wick: float = 0.02) -> list[dict]:
    """Build bars from a close-price sequence. Each bar is anchored at
    its close (no open=prev-close gap), so highs/lows differ from
    neighbours when prices differ — which is required for find_pivots
    to treat a local extremum as strictly-greater/less than neighbours.

    Timestamps start at 09:30 ET, 1-min apart.
    """
    base = datetime(2026, 5, 21, 9, 30)
    bars: list[dict] = []
    for i, c in enumerate(prices):
        bars.append({"t": base + timedelta(minutes=i),
                     "o": c, "h": c + wick, "l": c - wick,
                     "c": c, "v": vol})
    return bars


# ---------- ema / sma / vwap ----------

def test_ema_sma(r: Result):
    print("\n=== ema / sma ===")
    # SMA[3] of [1,2,3,4,5] = [1, 1.5, 2, 3, 4]
    s = P.sma([1, 2, 3, 4, 5], 3)
    r.check("sma length matches input", len(s) == 5)
    r.check("sma early-warm-up = cum avg",
            near(s[0], 1.0) and near(s[1], 1.5),
            f"got s[:2]={s[:2]}")
    r.check("sma converged = window avg",
            near(s[2], 2.0) and near(s[3], 3.0) and near(s[4], 4.0),
            f"got s[2:]={s[2:]}")

    # EMA shape: monotonically tracks a rising series
    e = P.ema([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3)
    r.check("ema length matches input", len(e) == 10)
    r.check("ema rises with rising input", all(e[i + 1] >= e[i] for i in range(9)))
    # EMA on constant series equals that constant
    e_flat = P.ema([5.0] * 10, 3)
    r.check("ema of constant = constant", all(near(v, 5.0) for v in e_flat))

    # SMA of empty
    r.check("sma of empty list = []", P.sma([], 5) == [])
    # Bad period raises
    try:
        P.sma([1, 2, 3], 0)
        r.check("sma period=0 raises", False, "did not raise")
    except ValueError:
        r.check("sma period=0 raises", True)


def test_vwap(r: Result):
    print("\n=== vwap ===")
    # All same price, increasing volume → vwap = price
    bars = [{"t": 0, "o": 10, "h": 10, "l": 10, "c": 10, "v": v}
            for v in (1000, 2000, 3000)]
    vw = P.vwap(bars)
    r.check("vwap of flat price = that price",
            all(near(v, 10.0) for v in vw),
            f"got {vw}")
    # Zero-volume bars don't crash
    bars0 = [{"t": 0, "o": 10, "h": 10, "l": 10, "c": 10, "v": 0}]
    r.check("vwap with zero volume = typical price",
            near(P.vwap(bars0)[0], 10.0))


# ---------- aggregate_to_n_min ----------

def test_aggregate(r: Result):
    print("\n=== aggregate_to_n_min ===")
    # 10 one-minute bars at 09:30 → 2 five-minute bars
    bars1 = make_bars([10.0 + 0.1 * i for i in range(10)], vol=1000)
    bars5 = P.aggregate_to_n_min(bars1, n=5)
    r.check("10 1-min bars → 2 5-min bars", len(bars5) == 2,
            f"got {len(bars5)}")
    r.check("5-min open = first 1-min open",
            near(bars5[0]["o"], bars1[0]["o"]),
            f"{bars5[0]['o']} vs {bars1[0]['o']}")
    r.check("5-min close = last 1-min close in bucket",
            near(bars5[0]["c"], bars1[4]["c"]),
            f"{bars5[0]['c']} vs {bars1[4]['c']}")
    r.check("5-min high = max of bucket highs",
            near(bars5[0]["h"], max(b["h"] for b in bars1[:5])))
    r.check("5-min low = min of bucket lows",
            near(bars5[0]["l"], min(b["l"] for b in bars1[:5])))
    r.check("5-min volume = sum of bucket volumes",
            bars5[0]["v"] == sum(b["v"] for b in bars1[:5]))
    # n=1 is a no-op
    bars1_again = P.aggregate_to_n_min(bars1, n=1)
    r.check("n=1 returns bars as-is", len(bars1_again) == len(bars1))


# ---------- find_pivots ----------

def test_find_pivots(r: Result):
    print("\n=== find_pivots ===")
    # Pure V shape: low at index 2, no high
    closes = [10, 9, 8, 9, 10]
    bars = make_bars(closes, wick=0.0)
    pivs = P.find_pivots(bars, left=2, right=2)
    lows = [p for p in pivs if p["type"] == "low"]
    r.check("V shape finds the bottom as pivot low",
            len(lows) == 1 and lows[0]["idx"] == 2,
            f"got pivots={pivs}")

    # Inverted V: high at index 2
    bars = make_bars([10, 11, 12, 11, 10], wick=0.0)
    pivs = P.find_pivots(bars, left=2, right=2)
    highs = [p for p in pivs if p["type"] == "high"]
    r.check("inverted-V finds the peak as pivot high",
            len(highs) == 1 and highs[0]["idx"] == 2)

    # Monotonic rise — should have NO pivots (left/right=2)
    bars = make_bars([10, 11, 12, 13, 14, 15, 16], wick=0.0)
    pivs = P.find_pivots(bars, left=2, right=2)
    r.check("monotonic rise yields no pivots",
            len(pivs) == 0,
            f"got {len(pivs)} pivots")

    # Equal highs at endpoints — bar i needs STRICTLY greater highs to be a pivot
    bars = make_bars([10, 10, 10, 11, 10, 10, 10], wick=0.0)
    pivs = P.find_pivots(bars, left=2, right=2)
    r.check("plateau with one spike: one pivot high",
            len([p for p in pivs if p["type"] == "high"]) == 1)

    # Too few bars for left+right
    bars = make_bars([10, 11, 10], wick=0.0)
    pivs = P.find_pivots(bars, left=2, right=2)
    r.check("3 bars, left=right=2 yields no pivots", len(pivs) == 0)


# ---------- consolidation ----------

def test_consolidation(r: Result):
    print("\n=== consolidation ===")
    # Tight range: 1% spread on 20 bars
    bars = make_bars([10.0 + (i % 5 - 2) * 0.01 for i in range(20)], wick=0.0)
    c = P.consolidation(bars, lookback_bars=10, max_range_pct=2.0)
    r.check("tight series → is_consol True", c["is_consol"] is True,
            f"range_pct={c['range_pct']}")

    # Wide range: 5% spread on the lookback window
    bars = make_bars([10.0 + i * 0.025 for i in range(20)], wick=0.0)
    c = P.consolidation(bars, lookback_bars=10, max_range_pct=2.0)
    r.check("wide series → is_consol False", c["is_consol"] is False,
            f"range_pct={c['range_pct']}")

    # Edge: 1 bar → False with reason
    c = P.consolidation(make_bars([10.0], wick=0.0), lookback_bars=10,
                       max_range_pct=2.0)
    r.check("1 bar → is_consol False with 'too few bars'",
            c["is_consol"] is False and "few bars" in (c.get("reason") or ""))

    # Lookback longer than series: should use what's available
    bars = make_bars([10.0, 10.01, 10.02], wick=0.0)
    c = P.consolidation(bars, lookback_bars=100, max_range_pct=1.0)
    r.check("lookback > len(bars) handled gracefully",
            c["n_bars"] == 3)


# ---------- trend ----------

def test_trend(r: Result):
    print("\n=== trend ===")
    # Clear uptrend: rising series
    bars = make_bars([10.0 + i * 0.1 for i in range(30)], wick=0.0)
    t = P.trend(bars, ema_period=10, slope_lookback=5)
    r.check("rising series → trend up", t["direction"] == "up",
            f"got {t['direction']} slope={t.get('slope_pct_per_bar')}")

    # Clear downtrend
    bars = make_bars([20.0 - i * 0.1 for i in range(30)], wick=0.0)
    t = P.trend(bars, ema_period=10, slope_lookback=5)
    r.check("falling series → trend down", t["direction"] == "down",
            f"got {t['direction']}")

    # Sideways: zigzag in a tight range
    closes = [10.0 + ((i % 4) - 1.5) * 0.005 for i in range(30)]
    bars = make_bars(closes, wick=0.0)
    t = P.trend(bars, ema_period=10, slope_lookback=5)
    r.check("tight zigzag → trend sideways",
            t["direction"] == "sideways",
            f"got {t['direction']} slope={t.get('slope_pct_per_bar')}")

    # Insufficient data
    t = P.trend(make_bars([10, 11, 12], wick=0.0),
                ema_period=20, slope_lookback=5)
    r.check("too-few bars → direction 'unknown'",
            t["direction"] == "unknown")


# ---------- higher_highs_lows ----------

def test_hh_hl(r: Result):
    print("\n=== higher_highs_lows ===")
    # Construct synthetic pivot sequence: HH HL HH HL
    pivs = [
        {"idx": 0, "type": "low",  "price": 9.0},
        {"idx": 5, "type": "high", "price": 10.0},
        {"idx": 8, "type": "low",  "price": 9.5},
        {"idx": 12, "type": "high", "price": 10.5},
        {"idx": 15, "type": "low",  "price": 9.8},
        {"idx": 20, "type": "high", "price": 11.0},
    ]
    out = P.higher_highs_lows(pivs, lookback_pivots=6)
    r.check("rising pivot sequence → uptrend",
            out["structure"] == "uptrend",
            f"got {out['structure']}, hh={out['hh_count']}, hl={out['hl_count']}")

    # Descending sequence
    pivs = [
        {"idx": 0, "type": "high", "price": 11.0},
        {"idx": 5, "type": "low",  "price": 10.5},
        {"idx": 8, "type": "high", "price": 10.5},
        {"idx": 12, "type": "low",  "price": 10.0},
        {"idx": 15, "type": "high", "price": 10.0},
        {"idx": 20, "type": "low",  "price": 9.5},
    ]
    out = P.higher_highs_lows(pivs, lookback_pivots=6)
    r.check("descending pivot sequence → downtrend",
            out["structure"] == "downtrend",
            f"got {out['structure']}")

    # Empty
    out = P.higher_highs_lows([], lookback_pivots=4)
    r.check("empty pivots → unknown", out["structure"] == "unknown")


# ---------- bull_flag ----------

def test_bull_flag(r: Result):
    print("\n=== bull_flag ===")

    # Textbook positive: clear pivot low → strong pole → flat flag
    # whose every bar's HIGH stays strictly below the pole top's high
    # (otherwise find_pivots correctly relocates the pole top into the flag).
    starts = [10.10, 10.05, 9.95, 9.90, 9.95, 10.05]
    closes = starts[:]
    for i in range(12):     # pole rises ~6%
        closes.append(10.05 + (i + 1) * 0.05)   # ends at 10.65
    # Flag: 5 sideways closes all ≤ 10.60 so their highs (close+wick) all
    # stay below the pole top's high (10.65 + wick).
    for i in range(5):
        closes.append(10.58 + (i % 3) * 0.02 - 0.02)
    bars = make_bars(closes, wick=0.03)
    bf = P.bull_flag(bars, pole_min_pct=2.0)
    r.check("textbook bull flag detected",
            bf.get("detected") is True,
            f"reason={bf.get('reason')}")

    # Negative #1: weak pole (only ~1% rise) — should be rejected. The
    # specific reason can be "pole too small" OR "flag too few bars"
    # depending on whether flag wicks become higher pivots than the
    # pole top; what matters is that the detector says no.
    closes = [10.10, 10.05, 9.95, 9.90, 9.95, 10.05]
    for i in range(12):
        closes.append(10.05 + (i + 1) * 0.005)   # only +6c rise
    for i in range(5):
        closes.append(10.10 + (i % 3) * 0.01)
    bars = make_bars(closes, wick=0.03)
    bf = P.bull_flag(bars, pole_min_pct=2.0)
    r.check("weak pole rejected (any reason)",
            bf.get("detected") is False,
            f"got reason={bf.get('reason')}, pct={bf.get('pole_height_pct')}")

    # Negative #2: deep retracement (flag falls back to near pole base)
    closes = [10.10, 10.05, 9.95, 9.90, 9.95, 10.05]
    for i in range(12):
        closes.append(10.05 + (i + 1) * 0.05)
    # Flag drops nearly all the way back
    for i in range(5):
        closes.append(10.65 - (i + 1) * 0.10)
    bars = make_bars(closes, wick=0.03)
    bf = P.bull_flag(bars, pole_min_pct=2.0,
                     flag_max_pullback_pct_of_pole=0.5)
    r.check("deep retracement rejected",
            bf.get("detected") is False
            and bf.get("reason") in ("flag pulled back too deep",
                                     "flag range too wide"),
            f"got reason={bf.get('reason')}, pullback_vs_pole={bf.get('pullback_vs_pole')}")

    # Negative #3: no clear pivot low (monotonic rise from bar 0)
    closes = [10.0 + i * 0.05 for i in range(20)]
    bars = make_bars(closes, wick=0.02)
    bf = P.bull_flag(bars, pole_min_pct=2.0)
    r.check("monotonic rise rejected (no pivot low before high)",
            bf.get("detected") is False,
            f"got reason={bf.get('reason')}")

    # Edge: empty input
    bf = P.bull_flag([], pole_min_pct=2.0)
    r.check("empty input → not detected with reason",
            bf.get("detected") is False and "bars" in (bf.get("reason") or ""))


# ---------- breakout_signal ----------

def test_breakout_signal(r: Result):
    print("\n=== breakout_signal ===")
    # Clean upside break: last close above level
    bars = make_bars([9.95, 9.97, 9.96, 10.01], wick=0.01)
    bars[-1]["v"] = 50_000   # spike
    b = P.breakout_signal(bars, level=10.00, direction="up",
                          min_volume_mult=1.0)
    r.check("close above level → broken True",
            b["broken"] is True, f"got {b}")
    r.check("volume confirms when last_vol > avg_prior*mult",
            b["vol_confirms"] is True)

    # No break — last close below level
    bars = make_bars([9.95, 9.97, 9.96, 9.99], wick=0.01)
    b = P.breakout_signal(bars, level=10.00, direction="up")
    r.check("close just under level → broken False",
            b["broken"] is False)

    # Downside break
    bars = make_bars([10.05, 10.03, 10.02, 9.99], wick=0.01)
    b = P.breakout_signal(bars, level=10.00, direction="down")
    r.check("downside break detected",
            b["broken"] is True)

    # Volume not confirmed (last bar's vol < avg * mult)
    bars = make_bars([9.95, 9.97, 9.96, 10.01], vol=20_000, wick=0.01)
    bars[-1]["v"] = 5_000   # weak
    b = P.breakout_signal(bars, level=10.00, direction="up",
                          min_volume_mult=2.0)
    r.check("weak vol → vol_confirms False",
            b["broken"] is True and b["vol_confirms"] is False)

    # Empty bars
    b = P.breakout_signal([], level=10.00, direction="up")
    r.check("empty bars → broken False", b["broken"] is False)

    # Unknown direction
    bars = make_bars([10, 10.5], wick=0.0)
    b = P.breakout_signal(bars, 10.0, direction="sideways")
    r.check("unknown direction → broken False with reason",
            b["broken"] is False and "direction" in (b.get("reason") or ""))


# ---------- ma_resistance ----------

def test_ma_resistance(r: Result):
    print("\n=== ma_resistance ===")
    # 50 bars rising from 10 → 14.9 (steady). Current = 14.9.
    # SMA(20) ≈ middle of last 20 bars ≈ 14.0 (BELOW current).
    # SMA(50) ≈ middle of all bars ≈ 12.45 (BELOW current).
    # No MA above current → resistance_above = None.
    bars = make_bars([10.0 + i * 0.1 for i in range(50)], wick=0.0)
    mr = P.ma_resistance(bars, current_price=bars[-1]["c"],
                         periods=(20, 50), ma_kind="sma")
    r.check("all MAs below price → resistance_above None",
            mr["resistance_above"] is None,
            f"got {mr['mas']}")

    # Series rises then dips: current price below the longer-period MA
    closes = [10.0 + i * 0.1 for i in range(30)]    # 10 → 12.9
    closes += [12.0 - i * 0.05 for i in range(20)]  # 12 → 11.05 — dip
    bars = make_bars(closes, wick=0.0)
    current = bars[-1]["c"]
    mr = P.ma_resistance(bars, current_price=current,
                         periods=(10, 30, 50), ma_kind="sma")
    above = [p for p, v in mr["mas"].items() if v > current]
    r.check("resistance_period present when at least one MA above price",
            len(above) > 0 and mr["resistance_above"] is not None,
            f"current={current}, mas={mr['mas']}")
    r.check("resistance is the LOWEST MA above price",
            mr["resistance_above"] == min(mr["mas"][p] for p in above))

    # Periods longer than data are silently skipped
    bars = make_bars([10.0] * 5, wick=0.0)
    mr = P.ma_resistance(bars, 10.0, periods=(20, 50), ma_kind="sma")
    r.check("MAs longer than data are skipped (empty mas dict)",
            mr["mas"] == {},
            f"got {mr['mas']}")


# ---------- main ----------

def main():
    r = Result()
    test_ema_sma(r)
    test_vwap(r)
    test_aggregate(r)
    test_find_pivots(r)
    test_consolidation(r)
    test_trend(r)
    test_hh_hl(r)
    test_bull_flag(r)
    test_breakout_signal(r)
    test_ma_resistance(r)
    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
