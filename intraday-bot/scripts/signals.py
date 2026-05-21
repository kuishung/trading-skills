"""Shared signal utilities.

Pure functions: given raw bar data, return decisions. No I/O. Strategy-
agnostic — the helpers here are usable by any strategy module under
scripts/strategies/, but no strategy logic lives here.

Vocabulary:
- "PM"  = pre-market session, 04:00-09:30 ET.
- "RTH" = regular trading hours, 09:30-16:00 ET.
- Bars are dicts with keys: t (datetime), o, h, l, c, v.
"""
from __future__ import annotations

from datetime import datetime, time as dtime


# ---------- EMAs ----------

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
