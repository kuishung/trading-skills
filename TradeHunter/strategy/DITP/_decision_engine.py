"""DITP decision-engine primitives — pure functions, no I/O, no globals.

Source: strategies-reference/DITP.md (P2 v0.2 spec from chat 2026-05-23).

This module is the SINGLE SOURCE OF TRUTH for the DITP family's entry,
stop, target, and exit math. Both the backtest harness (review/) and the
live strategy module (strategy/DITP/ditp_p2/impl.py v0.2.0 when wired)
import from here. There is no second implementation. Drift between
backtest and live = bug, by construction impossible.

Phase 1 (this module): the 4 mechanical primitives covering the bare-bracket
backtest — entry trigger, default stop, default target, tradeability filter.

Phases queued for later (in priority order):
  Phase 2: momentum_ok(), one_min_confirmation_ok(), ema_cancel_check()
  Phase 3: anti_pattern_detected()  — the 5 reversing-candle detectors
  Phase 4: update_trailing_stop(), early_exit_check(), add_to_winner_check()

Skipped permanently (art, not math):
  - sentiment_gate         — user judgment based on live composite + VXX
  - hammer_wick_stop       — discretionary stop placement override
  - flex_entry_at_lower_KL — "price may come back to A or D, hammer, then rally"

All thresholds are ticker-relative per CLAUDE.md "Normalized strategy
parameters (Option A)" — ATR multiples only. No absolute dollar / share /
percent values appear in this file.
"""
from __future__ import annotations

__version__ = "0.1.0"


# ---------- Phase 1: bare-bracket primitives ----------

def entry_signal(curr_close: float, prev_close: float, resistance: float) -> bool:
    """True iff the current 3-min bar closes above the daily resistance level
    AND the prior 3-min bar did NOT close above it.

    First-crossing convention: prevents the signal from re-firing on every
    subsequent bar that stays above R. Once the bot has entered, it tracks
    the position via the bracket — it doesn't keep checking the entry rule.

    Returns False if either close is NaN/None or resistance is non-positive
    (defensive against bad data; backtest skips such candidates gracefully).
    """
    if resistance is None or resistance <= 0:
        return False
    if curr_close is None or prev_close is None:
        return False
    try:
        return float(curr_close) > float(resistance) and float(prev_close) <= float(resistance)
    except (TypeError, ValueError):
        return False


def stop_price(entry: float, atr_daily: float, mult: float = 0.25) -> float:
    """Default stop: entry - mult × ATR(daily). Phase 1 uses mult=0.25.

    The hammer-wick stop override (3¢ below the wick when the 1-min
    confirmation candle prints a hammer) is ART, not math — it is NOT
    in this function. Live trading applies it discretionarily; backtest
    measures the math-only stop. Comparing live results to backtest
    expectancy quantifies the value of the human override.
    """
    return float(entry) - (float(mult) * float(atr_daily))


def target_price(entry: float, atr_daily: float, mult: float = 0.5) -> float:
    """Default target: entry + mult × ATR(daily). Phase 1 uses mult=0.5.

    Paired with stop_price(mult=0.25), this produces a 2:1 reward:risk
    bracket (0.5 ATR up / 0.25 ATR down = 2R target).
    """
    return float(entry) + (float(mult) * float(atr_daily))


def tradeability_ok(entry: float, target: float, atr_daily: float,
                    atr_mult_cap: float = 1.0) -> bool:
    """User rule (chat 2026-05-23): the trade is tradeable only if 2R distance
    (entry → target) fits within `atr_mult_cap` × daily ATR. With the default
    Phase 1 settings (stop 0.25 ATR, target 0.5 ATR), the (target - entry)
    distance IS the 2R distance, and the cap is 1 × ATR.

    Quote: "if it is more than 1 ATR, then the trade need to be careful
    because it may not be feasible. We need a tradable setup."

    Rejects setups where the target sits beyond a normal day's range —
    those targets are statistically unlikely to fill before EOD even when
    the breakout is real.
    """
    if atr_daily is None or atr_daily <= 0:
        return False
    return (float(target) - float(entry)) <= (float(atr_mult_cap) * float(atr_daily))


# ---------- Phase 2-4: stubs (will land when their phase ships) ----------
#
# Documented here so adapter authors can see the full eventual surface
# without grepping. Each phase adds one or two pure functions; the
# adapter then either calls them (if the strategy uses that phase) or
# omits the call (Phase 1 backtest adapter omits them).

# def momentum_ok(bars_3m, bars_1m, ema_periods=(6, 18, 50)) -> bool: ...
#     Phase 2: True iff EMA6 > EMA18 > EMA50 on BOTH 3m and 1m at current bar.

# def one_min_confirmation_ok(bars_1m_after_break, resistance) -> bool: ...
#     Phase 2: Loose version — True if any 1m bar within the next 2-3 bars
#     closes back above resistance after any wick-touch below.

# def ema_cancel_check(bars_3m_window) -> bool: ...
#     Phase 2: True iff EMA stack has inverted on 3m — cancel pending entry.

# def anti_pattern_detected(candle, key_levels) -> str | None: ...
#     Phase 3: Returns the name of any matching reversing pattern at a key
#     level — outside bar / inside bar / shooting star / failed sustain /
#     bearish engulfing — or None.

# def update_trailing_stop(current_stop, bars_since_entry, radius=3) -> float: ...
#     Phase 4: Ratchet stop up to just below the most recent confirmed
#     higher-low on 3m (radius bars on each side define a pivot).

# def early_exit_check(bars_since_entry, key_levels) -> bool: ...
#     Phase 4: True iff any anti-pattern just printed at a key level —
#     exit immediately (don't wait for trailing stop).

# def add_to_winner_check(entry, current_stop, bars_since_entry) -> dict | None: ...
#     Phase 4: Returns {"size_mult": 1.0, "shared_stop": <price>} when the
#     trailing stop has reached breakeven AND price action is still bullish.
#     Single shot — caller must track that the add already happened.
