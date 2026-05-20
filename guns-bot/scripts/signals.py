"""Signal computations for the ORB bot.

Pure functions: given raw bar data, return decisions. No I/O. Kept small
and unit-test friendly. Consumed by trade_day.py.

Strategy: 5-min Opening Range Breakout on "Stocks in Play" per
Zarattini, Barbon & Aziz (2024). Long if first 5-min bar is bullish,
short if bearish. Stop at opposite OR end, take-profit at 10R.

Vocabulary:
- "PM" = pre-market session, defined as 04:00-09:30 ET.
- "RTH" = regular trading hours, 09:30-16:00 ET.
- Bars are dicts with keys: t (datetime), o, h, l, c, v.
"""
from __future__ import annotations

from datetime import datetime, time as dtime


# ---------- EMAs (kept for diagnostics / future use) ----------

def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


# ---------- PM / RTH bar slicing ----------

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
    """Return PM high/low/volume/open/last_close, or None if empty."""
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


# ---------- ORB (5-min Opening Range Breakout) ----------

def evaluate_orb_breakout(
    first_n_bars: list[dict],
    symbol: str,
    take_profit_R: float = 10.0,
    *,
    orb_minutes: int = 5,
    slip_cents: float = 1.0,        # cents above/below OR for stop trigger
    limit_slip_cents: float = 3.0,  # cents beyond stop for limit (max chase)
) -> dict | None:
    """Build a long or short bracket plan from the first N 1-min RTH bars.

    Strategy: Zarattini/Barbon/Aziz 2024 — "Stocks in Play" 5-min ORB.
    Direction is the sign of the OR bar (close vs open of the period).
    Entry is a stop-limit order at the OR high (long) or OR low (short).
    Stop is the opposite end of the OR. Take-profit is take_profit_R x risk.

    Returns a plan dict shaped for submit_setup_entry, with a `side`
    field ("long" or "short"). Returns None if:
      - fewer than orb_minutes bars provided
      - the OR bar is neutral (close == open)
      - resulting risk_per_share is non-positive
    """
    if len(first_n_bars) < orb_minutes:
        return None
    or_bars = first_n_bars[:orb_minutes]
    or_open = or_bars[0]["o"]
    or_close = or_bars[-1]["c"]
    or_high = max(b["h"] for b in or_bars)
    or_low = min(b["l"] for b in or_bars)

    if or_close > or_open:
        side = "long"
    elif or_close < or_open:
        side = "short"
    else:
        return None  # neutral — skip

    slip = slip_cents / 100.0
    limit_slip = limit_slip_cents / 100.0

    if side == "long":
        entry_stop = round(or_high + slip, 2)
        entry_limit = round(entry_stop + limit_slip, 2)
        stop_loss = round(or_low - slip, 2)
        risk_per_share = round(entry_limit - stop_loss, 4)
        if risk_per_share <= 0:
            return None
        take_profit = round(entry_limit + take_profit_R * risk_per_share, 2)
    else:  # short
        entry_stop = round(or_low - slip, 2)
        entry_limit = round(entry_stop - limit_slip, 2)
        stop_loss = round(or_high + slip, 2)
        risk_per_share = round(stop_loss - entry_limit, 4)
        if risk_per_share <= 0:
            return None
        take_profit = round(entry_limit - take_profit_R * risk_per_share, 2)

    return {
        "strategy": "orb_5min",
        "symbol": symbol,
        "side": side,
        "or_high": round(or_high, 4),
        "or_low": round(or_low, 4),
        "or_open": round(or_open, 4),
        "or_close": round(or_close, 4),
        "entry_stop_trigger": entry_stop,
        "entry_limit": entry_limit,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": risk_per_share,
        "take_profit_R": take_profit_R,
    }


# ---------- Position sizing ----------

def position_size(
    equity: float,
    risk_pct: float,
    risk_per_share: float,
    *,
    max_position_pct: float = 0.0,
    entry_price: float | None = None,
) -> int:
    """Number of whole shares to risk `risk_pct` of equity per trade.

    Risk-based sizing:  qty_risk = (equity * risk_pct) / risk_per_share
    Optional notional cap:  qty_notional = (equity * max_position_pct) / entry_price

    When both `max_position_pct > 0` and `entry_price > 0` are provided,
    returns min(qty_risk, qty_notional). Otherwise risk-only.

    Returns 0 if the math doesn't yield at least 1 share.
    """
    if equity <= 0 or risk_per_share <= 0:
        return 0
    qty_risk = int((equity * risk_pct) // risk_per_share)
    if max_position_pct > 0 and entry_price is not None and entry_price > 0:
        qty_notional = int((equity * max_position_pct) // entry_price)
        return max(min(qty_risk, qty_notional), 0)
    return max(qty_risk, 0)


# ---------- Spread check ----------

def spread_ok(bid: float, ask: float, max_cents: float) -> bool:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False
    return (ask - bid) * 100 <= max_cents
