"""Strategy: gap-and-go breakout.

Premise: when a name in the morning consensus list breaks above its
premarket high during regular hours with confirming volume, and the move
isn't already extended, take a long position. Stop under the signal bar's
low (capped at 0.3 × ATR for ticker-relative sizing). Target = 2 × stop
distance (2R).

All thresholds are NORMALIZED to ticker-relative measures so the same
rules work on NVDA (ATR ~$4) and HIMS (ATR ~$1.50) without per-ticker
hand-tuning. See memory/reference_normalized_strategy_parameters.md for
the design principle.

The function returns either an order spec dict (the dispatcher places it)
or None (no trade this bar). The function never knows whether it's
running in live, dry-run, or replay mode — that's the testability
invariant. Same code, same decisions, three handlers for "what to do on
YES."
"""
from __future__ import annotations

from typing import Any

NAME = "gap_and_go"
VERSION = "0.1.0"

# ============================================================
#  Normalized thresholds (ALL ticker-relative — no absolute values)
# ============================================================

# How far past the premkt high counts as a "real" break (filters tick noise).
# 0.05 × ATR ≈ 5% of a typical day's range.
BREAKOUT_BUFFER_ATR = 0.05

# How extended is "too late" — past this point we're chasing. Captures
# the user's "don't be the last one in" trap awareness.
MAX_EXTENSION_ATR = 0.5

# Volume z-score required for confirmation. 2σ above this ticker's own
# baseline = "unusual activity for THIS name", scales automatically across
# liquidity regimes (NVDA's "unusual" looks nothing like HIMS's "unusual").
MIN_VOL_ZSCORE = 2.0

# Stop placement: tighter of (signal-bar low) or (entry - STOP_ATR × ATR).
# 0.3 × ATR ≈ a third of a day's range — typical for breakout follow-through.
STOP_ATR = 0.3

# Target as R-multiple. 2R is the conservative starting point; can be
# raised once we have data on follow-through.
TARGET_R_MULT = 2.0

# ============================================================
#  Time-of-day gates (universal, not ticker-relative)
# ============================================================

# Skip the first N minutes of RTH — opening auction noise + initial
# volatility spike often produces false breakouts.
SKIP_OPEN_MINUTES = 5

# Don't take new positions in the last N minutes — not enough time for
# the move to develop, exit risk dominates.
SKIP_CLOSE_MINUTES = 15


def evaluate_setup(bar: Any, profile: dict, state: Any) -> dict | None:
    """Evaluate one 1-min bar against the gap-and-go setup.

    Args:
        bar: object with attributes open/high/low/close/volume/timestamp/symbol.
             For Alpaca live mode this is an alpaca-py Bar; for replay it's
             our internal Bar DTO. Both expose the same attribute names.
        profile: dict loaded from profiles/<TICKER>.json — must have
                 atr_14d, avg_minute_vol_rth, minute_vol_stddev, daily_trend,
                 prev_close, premkt_range_avg.
        state: dispatcher state object with attributes:
               premkt_high[symbol], minutes_to_close, in_position(symbol),
               equity, day_pnl_pct, trade_count_today,
               max_trades_per_day, max_concurrent_positions,
               daily_loss_pct_limit, risk_pct_per_trade,
               bars_in_session_so_far[symbol].

    Returns:
        Order spec dict or None.

        Order spec format (handed to dispatcher; dispatcher decides what
        to do with it based on mode):
            {
                "strategy": "gap_and_go",
                "symbol": "NVDA",
                "side": "buy",
                "qty": 50,
                "entry_ref": 232.10,           # price at signal time
                "stop_loss": 230.74,
                "take_profit": 234.82,
                "risk_per_share": 1.36,
                "r_multiple_target": 2.0,
                "reason_codes": ["pmh_break", "vol_z=2.4", "trend=Uptrend"],
            }
    """
    sym = bar.symbol

    # ------------------------------------------------------------
    # Day-level / portfolio gates (cheap checks first)
    # ------------------------------------------------------------
    if state.trade_count_today >= state.max_trades_per_day:
        return _reject("max_trades_hit")
    if state.day_pnl_pct <= state.daily_loss_pct_limit:
        return _reject("daily_loss_limit_hit")
    if state.in_position(sym):
        return _reject("already_in_position")
    if state.open_position_count() >= state.max_concurrent_positions:
        return _reject("max_concurrent_hit")
    if state.minutes_to_close < SKIP_CLOSE_MINUTES:
        return _reject("too_close_to_end")
    if state.bars_in_session_so_far(sym) < SKIP_OPEN_MINUTES:
        return _reject("opening_auction_window")

    # ------------------------------------------------------------
    # Trend filter — don't fight the daily picture
    # ------------------------------------------------------------
    daily_trend = profile.get("daily_trend", "Unknown")
    if daily_trend == "Downtrend":
        return _reject("daily_trend_down")

    # ------------------------------------------------------------
    # Profile must be usable
    # ------------------------------------------------------------
    atr = float(profile.get("atr_14d") or 0)
    vol_mean = float(profile.get("avg_minute_vol_rth") or 0)
    vol_std = float(profile.get("minute_vol_stddev") or 0)
    if atr <= 0 or vol_mean <= 0 or vol_std <= 0:
        return _reject("incomplete_profile")

    # ------------------------------------------------------------
    # Premkt high must be known for this ticker (state built during premkt)
    # ------------------------------------------------------------
    pmh = state.premkt_high(sym)
    if pmh is None:
        return _reject("no_premkt_high")

    # ------------------------------------------------------------
    # Setup condition: bar closed above PMH + buffer
    # ------------------------------------------------------------
    breakout_threshold = pmh + BREAKOUT_BUFFER_ATR * atr
    if bar.close <= breakout_threshold:
        return None  # silent skip — this is the dominant non-trade reason

    # ------------------------------------------------------------
    # Anti-chase: not already extended past PMH
    # ------------------------------------------------------------
    extension = bar.close - pmh
    if extension > MAX_EXTENSION_ATR * atr:
        return _reject(f"extended_{extension/atr:.2f}atr")

    # ------------------------------------------------------------
    # Volume confirmation: z-score against this ticker's own baseline
    # ------------------------------------------------------------
    vol_zscore = (float(bar.volume) - vol_mean) / vol_std
    if vol_zscore < MIN_VOL_ZSCORE:
        return _reject(f"vol_z_low_{vol_zscore:.2f}")

    # ------------------------------------------------------------
    # All checks passed — build the order
    # ------------------------------------------------------------
    entry = float(bar.close)

    # Stop = tighter of (bar low) or (entry - 0.3 × ATR)
    atr_stop = entry - STOP_ATR * atr
    bar_stop = float(bar.low) - 0.01  # 1 cent below signal bar
    stop = max(atr_stop, bar_stop)  # the LESS aggressive (higher) of the two
    risk_per_share = entry - stop

    if risk_per_share <= 0:
        # Defensive: shouldn't happen if breakout was real, but guard
        return _reject("invalid_risk_per_share")

    target = entry + TARGET_R_MULT * risk_per_share

    # R-based position sizing — same risk_pct_per_trade across all tickers
    dollar_risk = state.equity * (state.risk_pct_per_trade / 100.0)
    qty = int(dollar_risk / risk_per_share)
    if qty <= 0:
        return _reject("size_zero")

    return {
        "strategy": NAME,
        "version": VERSION,
        "symbol": sym,
        "side": "buy",
        "qty": qty,
        "entry_ref": round(entry, 4),
        "stop_loss": round(stop, 4),
        "take_profit": round(target, 4),
        "risk_per_share": round(risk_per_share, 4),
        "r_multiple_target": TARGET_R_MULT,
        "atr_used": round(atr, 4),
        "vol_zscore": round(vol_zscore, 2),
        "premkt_high_ref": round(pmh, 4),
        "extension_atr": round(extension / atr, 3),
        "reason_codes": [
            f"pmh_break_{(bar.close - pmh) / atr:.2f}atr",
            f"vol_z_{vol_zscore:.2f}",
            f"trend_{daily_trend}",
            f"stop_{(entry - stop) / atr:.2f}atr",
        ],
    }


def _reject(reason: str) -> None:
    """Helper to make rejection reasons greppable in decision logs.

    Currently just returns None. The dispatcher's logger captures the reason
    by inspecting which gate fired, OR strategies can yield-style emit
    intermediate signals if we ever need more granular tracking.

    For now the dispatcher logs only the "YES" decisions (orders) + the
    fact that this bar was evaluated. Rejection reasons are reconstructable
    later by re-running the brain in replay mode on the same bars — same
    code, same outcome. (Determinism is one benefit of normalized params.)
    """
    return None
