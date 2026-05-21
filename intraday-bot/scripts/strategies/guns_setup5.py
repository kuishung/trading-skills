"""GUNS Setup 5 — Break of the First 1-Minute Candle.

When the PDF says fire this setup
--------------------------------
At 09:31 ET (right after the first 1-minute RTH candle closes), if
that candle is:
  - Bullish (close > open)
  - Closes above the 9 EMA, 20 EMA, 50 SMA, 200 SMA
  - "Not too big" relative to the stock's normal candle size

We submit a buy-stop-limit just above the first candle's high.

Mechanics
---------
Entry      Buy stop trigger at first-candle high + $0.01, limit 5c above.
Stop loss  Sell stop 1 cent below the first candle's low
           (or price-tier table if the tier is tighter — we take min).
TP         2R - 2.5R above entry (default 2.0R).
BE move    At +1R OR at first resistance (PMH, etc.) — handled by orch.

Eligibility checks (mechanical)
-------------------------------
1) Symbol is on today's watchlist.
2) PM volume >= 30K (defensive double-check).
3) First 1-min RTH candle exists (09:30:00-09:30:59 ET).
4) Candle is bullish (close > open).
5) Candle closes above the moving averages we can compute from the
   PM+early-RTH data we already have. With ~5.5h of 1-min PM bars
   plus this first RTH candle, EMA9/EMA20/SMA50 stabilise; SMA200
   needs daily-chart context we don't fetch here, so we treat the
   SMA200 check as informational only (logged, not blocking).
6) Candle not "too big": first-candle range <= candle_size_mult x
   median range of the prior 30 min of PM bars (default 2.0x).
   PDF says "not too big relative to normal candles" — this is the
   mechanizable proxy.

What we do NOT check
--------------------
- News catalyst / float — upstream watchlist responsibility.
- Level 2 large-bid/ask read (PDF discretionary overlay; no
  meaningful order-book depth in Alpaca paper feed).

Why entry_et = "09:31" matters
-------------------------------
The framework fires strategies at entry_et. We must wait until the
first RTH candle is closed (09:30:00-09:30:59 — closes at 09:31:00).
fetch_bars at 09:31 will include that candle.

Config block (cfg.strategies.guns_setup5)
-----------------------------------------
{
  "enabled": true,
  "entry_et": "09:31",
  "entry_cutoff_et": "09:33",
  "max_concurrent": 2,
  "take_profit_R": 2.0,
  "params": {
    "candle_size_mult": 2.0,        # max first-candle range vs median PM range
    "limit_cents_above_stop": 5,
    "require_above_ema9": true,
    "require_above_ema20": true,
    "require_above_sma50": true     # set false on illiquid names with thin PM
  }
}
"""
from __future__ import annotations

import statistics
import sys
from datetime import time as dtime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _common import get_rth_minute_bars  # noqa: E402
from _journal import journal  # noqa: E402
from signals import ema_series, split_pm_rth  # noqa: E402
from strategies.base import Strategy  # noqa: E402
from strategies._guns_common import (  # noqa: E402
    MIN_PM_VOLUME,
    build_long_buy_stop_limit_plan,
    load_guns_watchlist,
    pm_volume_total,
    price_tier_stop_cents,
)

STRATEGY_NAME = "guns_setup5"


def _params(strat: Strategy) -> dict:
    p = strat.params or {}
    return {
        "candle_size_mult": float(p.get("candle_size_mult", 2.0)),
        "limit_cents_above_stop": int(p.get("limit_cents_above_stop", 5)),
        "require_above_ema9": bool(p.get("require_above_ema9", True)),
        "require_above_ema20": bool(p.get("require_above_ema20", True)),
        "require_above_sma50": bool(p.get("require_above_sma50", True)),
    }


def _first_rth_minute(rth_bars: list[dict]) -> dict | None:
    """Return the 09:30 1-minute candle if present, else None."""
    for b in rth_bars:
        t = b["t"]
        local = t.time() if hasattr(t, "time") else t
        if local.hour == 9 and local.minute == 30:
            return b
        if local >= dtime(9, 31):
            return None  # we never saw 09:30 — bar is missing
    return None


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ---------- Strategy callables ----------

def pick_universe(date_iso: str, cfg: dict) -> list[str]:
    return load_guns_watchlist(date_iso, STRATEGY_NAME)


def fetch_bars(symbols: list[str], cfg: dict, strat: Strategy) -> dict[str, list[dict]]:
    """Full-day 1-min bars (PM + RTH up to now). We need the first RTH
    candle, which requires entry_et >= 09:31."""
    if not symbols:
        return {}
    return get_rth_minute_bars(symbols, cfg, fake_now=None)


def evaluate(symbol: str, bars: list[dict], strat: Strategy) -> dict | None:
    p = _params(strat)
    if not bars:
        journal(STRATEGY_NAME, "rejected", symbol=symbol, reason="no_bars")
        return None

    pm_bars, rth_bars = split_pm_rth(bars)
    pm_vol = pm_volume_total(pm_bars)
    if pm_vol < MIN_PM_VOLUME:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pm_volume_below_min",
                pm_volume=pm_vol, min_pm_volume=MIN_PM_VOLUME)
        return None

    first = _first_rth_minute(rth_bars)
    if first is None:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="first_rth_candle_missing")
        return None

    # Rule 4: bullish first candle.
    if first["c"] <= first["o"]:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="first_candle_not_bullish",
                o=first["o"], c=first["c"])
        return None

    # Rule 5: closes above EMAs/SMA. Build MA series on closes of all
    # bars up to and including the first RTH candle.
    closes_up_to_first = [b["c"] for b in pm_bars] + [first["c"]]
    ema9_now = ema_series(closes_up_to_first, 9)[-1] if closes_up_to_first else None
    ema20_now = ema_series(closes_up_to_first, 20)[-1] if closes_up_to_first else None
    sma50_now = _sma(closes_up_to_first, 50)

    close = first["c"]
    if p["require_above_ema9"] and ema9_now is not None and close < ema9_now:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="first_close_below_ema9",
                close=close, ema9=round(ema9_now, 4))
        return None
    if p["require_above_ema20"] and ema20_now is not None and close < ema20_now:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="first_close_below_ema20",
                close=close, ema20=round(ema20_now, 4))
        return None
    if p["require_above_sma50"] and sma50_now is not None and close < sma50_now:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="first_close_below_sma50",
                close=close, sma50=round(sma50_now, 4))
        return None

    # Rule 6: candle not too big. Compare first-candle range to the
    # median of the prior 30 minutes of PM bars (or whatever's available).
    first_range = first["h"] - first["l"]
    reference_window = pm_bars[-30:] if len(pm_bars) >= 30 else pm_bars
    pm_ranges = [b["h"] - b["l"] for b in reference_window if b["h"] > b["l"]]
    if pm_ranges:
        median_pm_range = statistics.median(pm_ranges)
        if median_pm_range > 0 and first_range > p["candle_size_mult"] * median_pm_range:
            journal(STRATEGY_NAME, "rejected", symbol=symbol,
                    reason="first_candle_too_big",
                    first_range=round(first_range, 3),
                    median_pm_range=round(median_pm_range, 3),
                    mult=p["candle_size_mult"])
            return None

    # Build the entry plan.
    trigger_price = round(first["h"] + 0.01, 2)
    # Stop = min(price-tier, one-cent-below-first-candle-low).
    tier_cents = price_tier_stop_cents(trigger_price)
    candle_stop_cents = max(int(round((trigger_price - (first["l"] - 0.01)) * 100)), 1)
    stop_cents = min(tier_cents, candle_stop_cents)

    plan = build_long_buy_stop_limit_plan(
        strategy_name=STRATEGY_NAME,
        symbol=symbol,
        trigger_price=trigger_price,
        limit_cents_above_stop=p["limit_cents_above_stop"],
        stop_distance_cents=stop_cents,
        take_profit_R=strat.take_profit_R,
        evidence={
            "first_candle_o": first["o"], "first_candle_h": first["h"],
            "first_candle_l": first["l"], "first_candle_c": first["c"],
            "first_candle_v": first.get("v"),
            "ema9": round(ema9_now, 4) if ema9_now else None,
            "ema20": round(ema20_now, 4) if ema20_now else None,
            "sma50": round(sma50_now, 4) if sma50_now else None,
            "tier_stop_cents": tier_cents,
            "candle_stop_cents": candle_stop_cents,
            "stop_cents_chosen": stop_cents,
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
        entry_et=str(block.get("entry_et", "09:31")),
        entry_cutoff_et=str(block.get("entry_cutoff_et", "09:33")),
        take_profit_R=float(block.get("take_profit_R", 2.0)),
        max_concurrent=int(block.get("max_concurrent", 2)),
        params=dict(block.get("params") or {}),
        pick_universe=pick_universe,
        fetch_bars=fetch_bars,
        evaluate=evaluate,
    )
