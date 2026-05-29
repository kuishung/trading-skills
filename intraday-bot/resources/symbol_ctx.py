"""Per-symbol shared computation context.

Hoists work that EVERY DITP detector recomputes independently into a
single SymbolContext built once per symbol before the detector loop
runs. Added 2026-05-29 efficiency Pass 2 #1 per dashboard audit.

Without this module, a 30-symbol Run-All scan does (per symbol):
  - bars_store.load_bars         × 10 (one call per detector)
  - np.array([b["c"]...]) etc.   × 10 detectors × 4 arrays = 40 allocs
  - ema_np(closes, period)       × 10 detectors × 3 EMAs = 30 calls
  - atr_wilder_np                × 10 calls

With SymbolContext those numbers collapse to:
  - 1 bar source (or 0 if bars already in memory)
  - 4 numpy array allocations
  - 3 EMA passes
  - 1 ATR pass

Each detector still does its setup-specific work (S/R discovery,
classify variant, etc.) -- those don't generalize. The hoisted layer
is strictly the shared prelude that was previously copy/pasted across
all 10 detector files.

Public API:
  SymbolContext(...)       -- frozen dataclass holding all the cached arrays
  build_context(symbol, bars=None, *, min_bars=15) -> SymbolContext | None
                           -- the factory. Pass bars to reuse an in-memory
                              fetch (dashboard scan path); pass None to
                              read parquet via bars_store (CLI / backtest).

Backward compatibility: detectors that don't yet accept `ctx=None` keep
working unchanged because they continue to do the load + computation
themselves. The refactor adds `ctx` as an OPTIONAL kwarg; when missing,
each detector rebuilds the same Context internally so the API contract
is preserved.

Same module is callable from CLI / backtest paths (where `bars` is
None) -- the per-symbol cost there is dominated by parquet I/O, not
the numpy work, so the benefit is small but the code-sharing benefit
(no duplicated prelude across 10 files) is meaningful.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# --- intraday-bot bootstrap so this module is importable from anywhere ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
for _p in [str(_root)] + [str(_root / s) for s in ("scripts", "resources")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import numpy as np  # type: ignore  # noqa: E402

from patterns import ema_np, atr_wilder_np  # noqa: E402  resources/patterns.py
import bars_store  # noqa: E402  resources/bars_store.py


@dataclass(frozen=True)
class SymbolContext:
    """All per-symbol arrays + indicators a DITP detector typically needs.

    `frozen=True` because callers should treat this as read-only; any
    mutation indicates an architectural mistake (different detectors
    needing different bar windows = build different contexts).

    Fields:
      symbol  : uppercase ticker
      bars    : list[dict] of {t,o,h,l,c,v} -- preserved for detectors
                that need to walk raw bars (touch lookback, variant
                classifier, etc.)
      closes/opens/highs/lows : numpy float arrays, length == len(bars)
      ema20/ema50/ema200      : numpy float arrays (full series; index
                                -1 is "today", same convention every
                                detector already uses)
      atr14   : Wilder ATR(14), scalar -- current bar's value
    """
    symbol:  str
    bars:    list
    closes:  np.ndarray
    opens:   np.ndarray
    highs:   np.ndarray
    lows:    np.ndarray
    ema20:   np.ndarray
    ema50:   np.ndarray
    ema200:  np.ndarray
    atr14:   float


def build_context(
    symbol: str,
    bars: list | None = None,
    *,
    min_bars: int = 15,
) -> SymbolContext | None:
    """Build a SymbolContext for one symbol, or return None if there
    isn't enough data.

    `bars` is the daily-bars list as produced by `bars_store.load_bars`
    (shape `[{t,o,h,l,c,v}, ...]`). When None, this loads it via
    `bars_store.load_bars(symbol, timeframe="daily")` -- useful for
    CLI / backtest callers. The dashboard's batched scan path passes
    `bars` directly to avoid the load.

    `min_bars=15` is the absolute floor for ATR(14) numerical stability.
    Detectors with stricter floors (e.g., ema_rebound requires >= 210
    bars for EMA200 stability) check that themselves; this is just a
    safety net that prevents NaN-laden arrays from being returned.
    """
    if bars is None:
        bars = bars_store.load_bars(symbol, timeframe="daily")
    if not bars or len(bars) < min_bars:
        return None

    closes = np.array([b["c"] for b in bars], dtype=float)
    opens  = np.array([b["o"] for b in bars], dtype=float)
    highs  = np.array([b["h"] for b in bars], dtype=float)
    lows   = np.array([b["l"] for b in bars], dtype=float)

    atr = atr_wilder_np(highs, lows, closes, period=14)
    if atr <= 0:
        # Defensive: ATR(0) means degenerate inputs (single bar / flat
        # series). Detectors would all reject this anyway, but skipping
        # the rest of the ema_np computes avoids wasted work.
        return None

    ema20  = ema_np(closes, 20)
    ema50  = ema_np(closes, 50)
    ema200 = ema_np(closes, 200)

    return SymbolContext(
        symbol = symbol.upper(),
        bars   = bars,
        closes = closes,
        opens  = opens,
        highs  = highs,
        lows   = lows,
        ema20  = ema20,
        ema50  = ema50,
        ema200 = ema200,
        atr14  = float(atr),
    )
