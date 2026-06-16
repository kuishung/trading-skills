"""Layer 1 + geometric primitives — PATTERN-AGNOSTIC measurement tools.

Turns raw bars into the geometric numbers any per-pattern detector consumes:
swing points, fitted lines (slope/R²), line touches, convergence (apex),
contraction, and ATR. No pattern logic lives here — this is the "eyes".

Contract rules baked into every function (DETECTOR_DESIGN.md):
  * TICKER-RELATIVE — measurements normalised to ATR/% so one detector works on
    a $5 and a $500 stock (CLAUDE.md normalization rule). Never absolute $.
  * NO LOOKAHEAD — a value "as of bar i" uses only bars[:i+1]; a swing pivot is
    confirmed only once its right-side flanking bars exist.

Bar shape is the bars_store dict: {t, o, h, l, c, v} (t = ISO string).

Build phase: D1 — IMPLEMENTED. Pure functions (no numpy / no I/O), unit-checked
against real parquet bars. Signatures + contracts unchanged from the skeleton.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Swing:
    """A confirmed pivot."""
    idx: int        # index into the bars list
    t: str          # ISO timestamp
    price: float    # pivot price (high for 'high', low for 'low')
    kind: str       # 'high' | 'low'


@dataclass
class LineFit:
    """A least-squares line through swing points, in (bar-index, price) space."""
    slope: float        # price units per bar
    intercept: float
    r2: float           # goodness of fit, 0..1
    n: int              # points fitted

    def at(self, x: float) -> float:
        """Line price at bar-index x."""
        return self.slope * x + self.intercept


# --------------------------------------------------------------------------- #
# Volatility unit
# --------------------------------------------------------------------------- #
def atr(bars: list[dict], period: int = 14) -> float:
    """Average True Range over the last `period` bars — the ticker's volatility
    unit. Every other threshold is expressed as a multiple of this so the
    detector is ticker-relative. D1.

    TR_i = max(high-low, |high-prev_close|, |low-prev_close|). Returns the mean
    of the last `period` TRs (or all available if fewer). 0.0 if < 1 bar."""
    if not bars:
        return 0.0
    if len(bars) == 1:
        b = bars[0]
        return float(b["h"]) - float(b["l"])
    trs: list[float] = []
    prev_close = float(bars[0]["c"])
    for b in bars[1:]:
        h, l, = float(b["h"]), float(b["l"])
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = float(b["c"])
    window = trs[-period:] if period > 0 else trs
    return sum(window) / len(window) if window else 0.0


# --------------------------------------------------------------------------- #
# Layer 1 — swing (pivot) extraction
# --------------------------------------------------------------------------- #
def swings(bars: list[dict], *, left: int = 2, right: int = 2,
           prominence_atr: float = 0.5) -> list[Swing]:
    """Layer 1 — fractal pivot highs/lows. A pivot high needs `left`/`right`
    flanking bars with strictly lower highs (lows symmetric). NO LOOKAHEAD: a
    pivot at i is emitted only after `right` later bars exist (so the last
    `right` bars never yield a pivot — they aren't confirmed yet). `prominence_atr`
    drops noise pivots whose margin over the flanking bars is < prominence_atr *
    ATR. Returns Swings in ascending index order. D1."""
    n = len(bars)
    if n < left + right + 1:
        return []
    atr_val = atr(bars)
    min_prom = prominence_atr * atr_val
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    out: list[Swing] = []
    # only i in [left, n-right-1] can be confirmed (right-side bars must exist)
    for i in range(left, n - right):
        hi, lo = highs[i], lows[i]
        left_h = highs[i - left:i]
        right_h = highs[i + 1:i + 1 + right]
        if hi > max(left_h) and hi > max(right_h):
            margin = hi - max(max(left_h), max(right_h))
            if margin >= min_prom:
                out.append(Swing(idx=i, t=bars[i]["t"], price=hi, kind="high"))
                continue  # a bar is a high OR a low, not both
        left_l = lows[i - left:i]
        right_l = lows[i + 1:i + 1 + right]
        if lo < min(left_l) and lo < min(right_l):
            margin = min(min(left_l), min(right_l)) - lo
            if margin >= min_prom:
                out.append(Swing(idx=i, t=bars[i]["t"], price=lo, kind="low"))
    return out


# --------------------------------------------------------------------------- #
# Line fitting + trendline measurements
# --------------------------------------------------------------------------- #
def fit_line(points: list[tuple[int, float]]) -> LineFit:
    """Least-squares line through (bar_index, price) points. R² is the coefficient
    of determination (1.0 = perfect, 0.0 = no better than the mean; a flat-but-
    exact line of identical prices is treated as R²=1.0). D1."""
    n = len(points)
    if n == 0:
        return LineFit(0.0, 0.0, 0.0, 0)
    if n == 1:
        return LineFit(0.0, points[0][1], 1.0, 1)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0:                       # all points share an x — undefined slope
        return LineFit(0.0, my, 0.0, n)
    slope = sxy / sxx
    intercept = my - slope * mx
    if syy == 0:                       # all prices identical → a perfect flat line
        return LineFit(slope, intercept, 1.0, n)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / syy
    if r2 < 0.0:
        r2 = 0.0
    return LineFit(slope, intercept, r2, n)


def touches(line: LineFit, sw: list[Swing], atr_val: float, *,
            tol_atr: float = 0.25) -> int:
    """How many swings sit within tol_atr * ATR of `line` (a real trendline is
    touched repeatedly, not defined by 2 points). D1."""
    if atr_val <= 0:
        return 0
    tol = tol_atr * atr_val
    return sum(1 for s in sw if abs(s.price - line.at(s.idx)) <= tol)


def convergence(high_line: LineFit, low_line: LineFit) -> float | None:
    """The x (bar-offset) where the two lines meet, in the SAME coordinate space
    the lines were fit. When lines are fit with the window's last bar at x=0 (the
    convention the feature engine uses), this value IS the bars-ahead-to-apex.
    None if the lines are parallel or do not converge ahead (intersection at or
    behind the last bar, i.e. x <= 0 — diverging going forward). D1."""
    denom = high_line.slope - low_line.slope
    if denom == 0:                     # parallel
        return None
    x = (low_line.intercept - high_line.intercept) / denom
    return x if x > 0 else None


def contraction(bars: list[dict], atr_val: float) -> float:
    """Price range at the window END / range at the window START — i.e. the
    high-low span of the last third of the window divided by that of the first
    third. < 1.0 means the range is squeezing toward the apex. D1.

    (ATR cancels in the ratio; `atr_val` is used only as a floor on the start
    range so a dead-flat opening can't produce a divide-by-zero / huge ratio.)"""
    n = len(bars)
    if n < 2:
        return 1.0
    seg = max(2, n // 3)
    start, end = bars[:seg], bars[-seg:]

    def span(seg_bars: list[dict]) -> float:
        return max(float(b["h"]) for b in seg_bars) - min(float(b["l"]) for b in seg_bars)

    start_span = span(start)
    end_span = span(end)
    floor = max(1e-9, 0.1 * atr_val)   # avoid blow-up on a flat start
    return end_span / max(start_span, floor)
