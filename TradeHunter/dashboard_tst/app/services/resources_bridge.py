"""Bridge to TradeHunter's shared resources/ library.

The platform REUSES the roof's analysis code (Finviz screening, MATP,
trend_state, patterns, bars_store, review/backtest) rather than
reimplementing it -- see DESIGN.md section 7. This module is the single
import seam; each function is a Phase-tagged stub for now.

Import bootstrap: dashboard_tst/app/services/ -> TradeHunter/ is three
parents up, added to sys.path so ``resources.*`` / ``review.*`` /
``journal.*`` resolve regardless of how the app is launched.
"""
from __future__ import annotations

import sys
from pathlib import Path

# .../TradeHunter/dashboard_tst/app/services/resources_bridge.py
#   parents[0]=services  [1]=app  [2]=dashboard_tst  [3]=TradeHunter
TRADEHUNTER_ROOT = Path(__file__).resolve().parents[3]
if str(TRADEHUNTER_ROOT) not in sys.path:
    sys.path.insert(0, str(TRADEHUNTER_ROOT))


def screen_universe(finviz_url: str, *, max_pages: int = 10, force_refresh: bool = False) -> list[dict]:
    """Resolve a Finviz screener URL to rows of {symbol, price, volume} via
    the shared resources.finviz_screener (public-page scrape + 1h cache)."""
    from resources import finviz_screener

    return finviz_screener.fetch_screener_rows(
        finviz_url, max_pages=max_pages, force_refresh=force_refresh
    )


def refresh_matp(symbols: list[str]) -> int:
    """Phase 2: run the resources/MATP pipeline and upsert MATPLevel rows.
    Returns the number of tickers refreshed."""
    raise NotImplementedError("Phase 2: wire resources/MATP pipeline -> MATPLevel")


def classify_trend(symbol: str) -> str:
    """Phase 2: resources.trend_state classification on the daily parquet."""
    raise NotImplementedError("Phase 2: wire resources.trend_state")


def detect_patterns(symbol: str, timeframe: str = "daily"):
    """Phase 3: resources.patterns over bars from resources.bars_store."""
    raise NotImplementedError("Phase 3: wire resources.patterns + bars_store")


def backtest_setup(symbol: str, entry: float, stop_loss: float, profit_target: float,
                   timeframe: str = "daily"):
    """Phase 5: simulate entry/SL/PT against parquet history via
    review.backtest + resources.bars_store; return a success-rate report."""
    raise NotImplementedError("Phase 5: wire review.backtest + resources.bars_store")
