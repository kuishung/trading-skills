"""GUNS Setup 1 — Break of the Pre-Market High.

When the PDF says fire this setup
--------------------------------
Pre-market price is *consolidating very near* the PMH. The trader
pre-stages a buy-stop-limit just above PMH; market open then either
fills it (breakout) or the stop-out triggers (failed break).

Mechanics
---------
Entry      Buy stop trigger at PMH + $0.01, limit 3-5 cents above.
Stop loss  Price-tier table (10-50c by price bracket).
TP         2R - 2.5R above entry (we use 2.0R as default).
BE move    At +1R, the orchestrator's poll_breakeven_moves handles this.

Eligibility checks we apply mechanically
----------------------------------------
1) Symbol is on today's watchlist file.
2) PM volume >= 30,000 shares (PDF minimum).
3) Last close (or PMH proxy) >= $1.50 (PDF minimum).
4) "Very near PMH" — the last 15 minutes of PM bars must trade within
   `consol_band_pct` of the PMH (default 1.5%). This is the closest
   mechanical proxy for the PDF's visual rule.

What we do NOT check (deliberately deferred)
--------------------------------------------
- News catalyst type. The PDF requires earnings/FDA/analyst/clinical
  pass and bans M&A. The watchlist upstream is expected to enforce
  this (the sibling intraday-premarket-brief skill does it).
- Float < 100M / ideal 10-20M. Same reason — upstream watchlist
  filter, not enforced here.
- "Big window to next resistance" — discretionary; we trust the
  watchlist curator.

Config block (cfg.strategies.guns_setup1)
-----------------------------------------
{
  "enabled": true,
  "entry_et": "09:30",
  "entry_cutoff_et": "09:35",
  "max_concurrent": 2,
  "take_profit_R": 2.0,
  "params": {
    "consol_band_pct": 1.5,        # PM consolidation tolerance to PMH
    "consol_lookback_min": 15,     # window for consolidation check
    "limit_cents_above_stop": 5    # buy-stop-limit slippage cap
  }
}
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _common import get_pm_bars  # noqa: E402
from _journal import journal  # noqa: E402
from strategies.base import Strategy  # noqa: E402
from strategies._guns_common import (  # noqa: E402
    MIN_PM_VOLUME, MIN_PRICE,
    build_long_buy_stop_limit_plan,
    load_guns_watchlist,
    pm_high_and_consolidation_range,
    pm_volume_total,
    price_tier_stop_cents,
)

STRATEGY_NAME = "guns_setup1"


def _params(strat: Strategy) -> dict:
    p = strat.params or {}
    return {
        "consol_band_pct": float(p.get("consol_band_pct", 1.5)),
        "consol_lookback_min": int(p.get("consol_lookback_min", 15)),
        "limit_cents_above_stop": int(p.get("limit_cents_above_stop", 5)),
    }


# ---------- Strategy callables ----------

def pick_universe(date_iso: str, cfg: dict) -> list[str]:
    return load_guns_watchlist(date_iso, STRATEGY_NAME)


def fetch_bars(symbols: list[str], cfg: dict, strat: Strategy) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    return get_pm_bars(symbols, cfg, fake_now=None)


def evaluate(symbol: str, bars: list[dict], strat: Strategy) -> dict | None:
    """Return a long plan-dict if PM is consolidating near PMH; else None.

    Every rejection path is journaled with a `reason` so the EOD report
    can show why a watchlist symbol didn't fire.
    """
    p = _params(strat)
    if not bars:
        journal(STRATEGY_NAME, "rejected", symbol=symbol, reason="no_pm_bars")
        return None

    pmh, consol_high, _consol_low = pm_high_and_consolidation_range(
        bars, consol_lookback_min=p["consol_lookback_min"]
    )
    if pmh is None or consol_high is None:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pm_structure_unavailable")
        return None

    # Mandatory price / volume floors.
    if pmh < MIN_PRICE:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pmh_below_min_price", pmh=pmh, min_price=MIN_PRICE)
        return None
    pm_vol = pm_volume_total(bars)
    if pm_vol < MIN_PM_VOLUME:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pm_volume_below_min",
                pm_volume=pm_vol, min_pm_volume=MIN_PM_VOLUME)
        return None

    # Consolidation tolerance: how far below PMH did the recent window
    # stay? PDF says "very near" — we approximate with a band %.
    gap_to_pmh_pct = (pmh - consol_high) / pmh * 100.0
    if gap_to_pmh_pct > p["consol_band_pct"]:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="not_consolidating_near_pmh",
                pmh=pmh, consol_high=consol_high,
                gap_pct=round(gap_to_pmh_pct, 2),
                tolerance_pct=p["consol_band_pct"])
        return None

    # Build the order parameters from the PDF's recipe.
    trigger_price = round(pmh + 0.01, 2)
    stop_cents = price_tier_stop_cents(trigger_price)
    plan = build_long_buy_stop_limit_plan(
        strategy_name=STRATEGY_NAME,
        symbol=symbol,
        trigger_price=trigger_price,
        limit_cents_above_stop=p["limit_cents_above_stop"],
        stop_distance_cents=stop_cents,
        take_profit_R=strat.take_profit_R,
        evidence={
            "pmh": pmh,
            "pm_volume": pm_vol,
            "consol_high": consol_high,
            "gap_to_pmh_pct": round(gap_to_pmh_pct, 2),
            "stop_cents": stop_cents,
        },
    )
    journal(STRATEGY_NAME, "planned", symbol=symbol, plan=plan)
    return plan


# ---------- Factory ----------

def build(cfg: dict) -> Strategy:
    block = (cfg.get("strategies") or {}).get(STRATEGY_NAME) or {}
    return Strategy(
        name=STRATEGY_NAME,
        enabled=bool(block.get("enabled", False)),
        entry_et=str(block.get("entry_et", "09:30")),
        entry_cutoff_et=str(block.get("entry_cutoff_et", "09:35")),
        take_profit_R=float(block.get("take_profit_R", 2.0)),
        max_concurrent=int(block.get("max_concurrent", 2)),
        params=dict(block.get("params") or {}),
        pick_universe=pick_universe,
        fetch_bars=fetch_bars,
        evaluate=evaluate,
    )
