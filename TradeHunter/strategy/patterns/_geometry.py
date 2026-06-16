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

Build phase: D1. Stubs — signatures + contracts frozen so the rest of the
skeleton is written against a stable interface.
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


def atr(bars: list[dict], period: int = 14) -> float:
    """Average True Range over the last `period` bars — the ticker's volatility
    unit. Every other threshold is expressed as a multiple of this so the
    detector is ticker-relative. D1."""
    raise NotImplementedError("D1: ATR")


def swings(bars: list[dict], *, left: int = 2, right: int = 2,
           prominence_atr: float = 0.5) -> list[Swing]:
    """Layer 1 — fractal pivot highs/lows. A pivot high needs `left`/`right`
    flanking bars with lower highs (lows symmetric). NO LOOKAHEAD: a pivot at i
    is emitted only after `right` later bars exist. `prominence_atr` drops noise
    pivots whose prominence < prominence_atr * ATR. Returns ascending Swings. D1."""
    raise NotImplementedError("D1: swing extraction")


def fit_line(points: list[tuple[int, float]]) -> LineFit:
    """Least-squares line through (bar_index, price) points. D1."""
    raise NotImplementedError("D1: line fit")


def touches(line: LineFit, sw: list[Swing], atr_val: float, *,
            tol_atr: float = 0.25) -> int:
    """How many swings sit within tol_atr * ATR of `line` (a real trendline is
    touched repeatedly, not defined by 2 points). D1."""
    raise NotImplementedError("D1: touches")


def convergence(high_line: LineFit, low_line: LineFit) -> float | None:
    """Bars-ahead (from the window's last bar) where the two lines meet — the
    apex. None if they diverge or are parallel. D1."""
    raise NotImplementedError("D1: convergence")


def contraction(bars: list[dict], atr_val: float) -> float:
    """Price range (in ATR) at the window end / range at the window start.
    < 1.0 means the range is squeezing toward the apex. D1."""
    raise NotImplementedError("D1: contraction")
