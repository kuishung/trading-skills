"""Signal computations for guns-bot.

Pure functions: given raw bar data, return decisions. No I/O. Kept small and
unit-test friendly. Wrapped by trade_day.py with the Alpaca fetch layer.

Vocabulary:
- "PM" = pre-market session, defined as 04:00–09:30 ET.
- "RTH" = regular trading hours, 09:30–16:00 ET.
- Bars are dicts with keys: t (datetime), o, h, l, c, v.
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from statistics import median


# ---------- EMAs ----------

def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


# ---------- Pre-market analysis ----------

def split_pm_rth(bars: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split 1-min bars into (pre-market 04:00-09:29:59, RTH 09:30+)."""
    pm: list[dict] = []
    rth: list[dict] = []
    for b in bars:
        t = b["t"]
        local = t.time() if isinstance(t, datetime) else t
        if dtime(4, 0) <= local < dtime(9, 30):
            pm.append(b)
        elif local >= dtime(9, 30):
            rth.append(b)
    return pm, rth


def pm_summary(pm_bars: list[dict]) -> dict | None:
    """Return PM high/low/volume/open/last_close, or None if no PM bars."""
    if not pm_bars:
        return None
    return {
        "pm_high": max(b["h"] for b in pm_bars),
        "pm_low": min(b["l"] for b in pm_bars),
        "pm_volume": sum(b["v"] for b in pm_bars),
        "pm_open": pm_bars[0]["o"],
        "pm_last_close": pm_bars[-1]["c"],
        "n_bars": len(pm_bars),
    }


def is_consolidating_near_pm_high(
    pm_bars: list[dict],
    pm_high: float,
    window_min: int,
    band_pct: float,
) -> bool:
    """True if every bar in the last `window_min` minutes of PM closed within
    `band_pct` of pm_high (i.e. range-bound just under the high). Skips empty
    bar gaps — only the bars that exist must satisfy the band."""
    if not pm_bars or pm_high <= 0:
        return False
    last_t = pm_bars[-1]["t"]
    if not isinstance(last_t, datetime):
        return False
    cutoff = last_t.timestamp() - window_min * 60
    window_bars = [b for b in pm_bars if b["t"].timestamp() >= cutoff]
    if len(window_bars) < max(3, window_min // 5):
        return False
    band = pm_high * (band_pct / 100.0)
    return all(pm_high - b["c"] <= band and b["c"] > 0 for b in window_bars)


# ---------- Setup 1: break of PM high ----------

def stop_loss_distance_dollars(entry_price: float) -> float:
    """Adam Khoo's stop-by-price-band rule (uses the conservative end of each band)."""
    if entry_price < 20:
        return 0.15
    if entry_price < 30:
        return 0.20
    if entry_price < 50:
        return 0.30
    if entry_price < 100:
        return 0.50
    # Above $100 isn't in the deck — extrapolate at 0.6% of price, capped at $1.
    return min(max(entry_price * 0.006, 0.50), 1.00)


def build_setup1_plan(
    symbol: str,
    pm_high: float,
    pm_summary_obj: dict,
    take_profit_R: float,
) -> dict:
    """Compute entry/stop/target levels for Setup 1 (break of PM high).

    Entry rule from deck:
      - Buy-stop 1¢ above PM high
      - Limit 3-5¢ above stop (we use 3¢)
      - Stop-loss by price band
      - TP at 2R-2.5R (caller picks via take_profit_R)
    """
    stop_trigger = round(pm_high + 0.01, 2)
    limit_price = round(stop_trigger + 0.03, 2)
    # Effective entry for sizing/RR — assume worst case (limit_price).
    entry_for_math = limit_price
    sl_dist = stop_loss_distance_dollars(entry_for_math)
    stop_loss = round(entry_for_math - sl_dist, 2)
    take_profit = round(entry_for_math + take_profit_R * sl_dist, 2)
    return {
        "setup": 1,
        "symbol": symbol,
        "entry_stop_trigger": stop_trigger,
        "entry_limit": limit_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": round(sl_dist, 4),
        "pm_high": round(pm_high, 4),
        "pm_volume": pm_summary_obj["pm_volume"],
    }


# ---------- Setup 5: break of first 1-min candle ----------

def evaluate_setup5_first_minute(
    first_min: dict,
    prior_pm_bars: list[dict],
    take_profit_R: float,
    symbol: str,
) -> dict | None:
    """Return a plan dict if the first 1-min RTH candle qualifies for Setup 5,
    else None.

    Deck requirements:
      - first 1-min candle is bullish (close > open)
      - first 1-min candle closes above 9 EMA AND 20 EMA
      - first 1-min candle is "not too big" relative to normal — proxy: range
        ≤ 1.5× the median range of the last 20 PM bars (a reasonable normal-size
        baseline; the deck is qualitative on this point)
    """
    o, h, l, c = first_min["o"], first_min["h"], first_min["l"], first_min["c"]
    if c <= o:
        return None  # not bullish

    # Build EMAs from the trailing PM closes + this first-minute close so the
    # EMA is up-to-date at the bar's close.
    closes = [b["c"] for b in prior_pm_bars[-100:]] + [c]
    if len(closes) < 21:
        return None  # not enough history to compute EMA20
    e9 = ema_series(closes, 9)[-1]
    e20 = ema_series(closes, 20)[-1]
    if not (c > e9 and c > e20):
        return None

    # Size check — last 20 PM bars' median range
    recent_ranges = [b["h"] - b["l"] for b in prior_pm_bars[-20:] if (b["h"] - b["l"]) > 0]
    candle_range = h - l
    if recent_ranges:
        norm = median(recent_ranges)
        if norm > 0 and candle_range > 1.5 * norm:
            return None

    entry_stop_trigger = round(h + 0.01, 2)
    entry_limit = round(entry_stop_trigger + 0.03, 2)
    sl_dist = round(entry_limit - (l - 0.01), 4)
    if sl_dist <= 0:
        return None
    stop_loss = round(l - 0.01, 2)
    take_profit = round(entry_limit + take_profit_R * sl_dist, 2)
    return {
        "setup": 5,
        "symbol": symbol,
        "entry_stop_trigger": entry_stop_trigger,
        "entry_limit": entry_limit,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": sl_dist,
        "first_min_high": h,
        "first_min_low": l,
        "first_min_close": c,
        "ema9_at_close": round(e9, 4),
        "ema20_at_close": round(e20, 4),
    }


# ---------- Sizing ----------

def position_size(equity: float, risk_pct: float, risk_per_share: float) -> int:
    """Number of whole shares to risk `risk_pct` of equity per trade.
    Returns 0 if the math doesn't yield at least 1 share."""
    if equity <= 0 or risk_per_share <= 0:
        return 0
    dollars = equity * risk_pct
    qty = int(dollars // risk_per_share)
    return max(qty, 0)


# ---------- Spread check ----------

def spread_ok(bid: float, ask: float, max_cents: float) -> bool:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False
    return (ask - bid) * 100 <= max_cents
