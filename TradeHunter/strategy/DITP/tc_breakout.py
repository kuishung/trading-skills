"""TC (Trend Continuation) detector -- standalone, yFinance-runnable.

User request 2026-05-29: SNDK, APLD, ORCL, LUNR flagged as TC candidates.
The existing `tc_scanner.py` is the PIPELINE-STRICT TC scanner -- it
requires yesterday's P2 watchlist to walk through. That's the right
architecture for the live trading bot (TC trades the Day +1 / Day +2
follow-through of a CONFIRMED Day-0 P2 breakout).

But for the dashboard's ad-hoc Finviz-universe scanning we don't have
a yesterday-P2 watchlist (and we shouldn't -- the user picks fresh
signal-based universes each session). So this file provides a
SELF-CONTAINED TC detector that identifies the "recent breakout +
continuation" pattern from price action alone:

  Day 0 (recent past): close broke above a prior swing high
  Day 0 / +1 / +2:     close is HOLDING above that broken level, AND
                       the price keeps trending up (or pulls back to
                       EMA20 and bounces -- the textbook continuation
                       entry)

Difference vs `tc_scanner.py`:

| Aspect              | tc_scanner.py (strict) | tc_breakout.py (this)  |
|---------------------|------------------------|-------------------------|
| Source              | Yesterday's P2 watchlist + today's bar | Today's bar + last N bars only |
| Required state file | state/watchlist_ditp_<yesterday>.json | None |
| Use case            | Live bot Day-0 EOD       | Dashboard ad-hoc Finviz scan |
| What counts as breakout | P2 candidate's `resistance` (from yesterday's scanner state) | max(closes) in days [-N : -K] of THIS bar series |
| Bullish gate        | strict (close > open AND close in upper half) | bullish-or-pin-bar (matches ema_rebound v1.4.0) |
| Day +2 carryover    | TBD (Phase 2)            | Built in -- last_breakout_idx within `max_days_since_breakout` of today |

User-named test cases (2026-05-29, yfinance 2y daily):

  SNDK: 5/26 was first close above prior 20d max close ($1562.34).
        Today is Day +2.
  APLD: 5/27 was first close above prior 20d max close ($48.02).
        Today is Day +1.
  ORCL: 5/28 (today) is Day 0 -- close $203.70 just broke prior 20d
        max close $195.95.
  LUNR: 5/27 was first close above prior 20d max close ($38.26).
        Today is Day +1.

All four should fire under a Day-0 through Day-+5 window.

Per CLAUDE.md normalization rule: all thresholds are ATR-relative
(distance from broken level, pullback depth to EMA20). Same code
applies to APLD's $4 ATR and SNDK's $112 ATR without retuning.

Public API:
  detect_tc_breakout(symbol: str, cfg: TCBreakoutConfig) -> dict | None
  scan_universe(symbols, cfg) -> list[dict]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# --- TradeHunter bootstrap (same pattern as scanner.py / ema_rebound.py)
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import numpy as np  # type: ignore  # noqa: E402

from patterns import ema_np, atr_wilder_np  # noqa: E402  (resources/patterns.py)
import bars_store  # noqa: E402  (resources/bars_store.py)
from symbol_ctx import SymbolContext, build_context  # noqa: E402  (resources/symbol_ctx.py)


__version__ = "1.0.1"


@dataclass
class TCBreakoutConfig:
    """All thresholds ATR-relative (CLAUDE.md normalization rule).

    The defaults are tuned to the user's 2026-05-29 named cases: SNDK
    (Day +2), APLD (Day +1), ORCL (Day 0), LUNR (Day +1). Tighten
    `max_days_since_breakout` to surface only freshly-broken-out
    tickers; loosen to keep symbols on the TC list longer after the
    initial breakout.
    """
    # Prior-swing window: max(closes) over days [-prior_swing_lookback :
    # -prior_swing_exclude_recent]. The "exclude recent" cutoff means
    # the prior swing is genuinely PRIOR to the breakout day, not
    # contaminated by the breakout itself.
    prior_swing_lookback:        int   = 20
    prior_swing_exclude_recent:  int   = 3
    # How far back (in trading days) to allow the FIRST breakout close
    # above the prior swing. 0 = today is Day 0. 5 = Day 0 through
    # Day +5. The user's named cases span Day 0 to Day +2; 5 leaves
    # headroom for slower continuations.
    max_days_since_breakout:     int   = 5
    # Today's close must still be ABOVE the broken level by at least
    # this many ATR (so a same-day pullback below the level disqualifies).
    # Set to 0 to allow "test the level from above" without rejecting.
    min_above_breakout_atr:      float = 0.0
    # Trend filter. Strict stack (EMA20>EMA50>EMA200) is the textbook
    # uptrend, but the ORCL case has EMA20 ($187.43) marginally below
    # EMA200 ($187.72) on a recovering downtrend -- still a valid TC
    # in the user's framework. Default RELAXED stack: require
    # close > EMA20 > EMA50 (close above the short trend, short above
    # the medium trend) but allow EMA50 < EMA200 (early-recovery case).
    require_stack:               bool  = False
    require_close_above_ema20:   bool  = True
    require_ema20_above_ema50:   bool  = True
    # Bullish-bias gate. Same flexibility as ema_rebound v1.4.0:
    # bullish close (close > open) OR pin-bar anatomy. The TC pattern
    # often has Day +1 / Day +2 with small bodies + upper wicks as
    # buyers / sellers wrestle -- a small bearish body is fine if the
    # close is still holding above the broken level.
    require_today_bullish:       bool  = False
    # Anti-extension filter: if today's close is >= max_extension_atr
    # above the broken level, the TC trade is already too far gone --
    # the easy continuation move is done, late entry = bad R:R. Set
    # to a large number (e.g., 20) to effectively disable.
    max_extension_atr:           float = 5.0


def detect_tc_breakout(
    symbol: str,
    cfg: TCBreakoutConfig,
    ctx: SymbolContext | None = None,
) -> dict | None:
    """Apply TC-breakout rules to one symbol's daily bars. Returns
    candidate dict or None.

    Candidate fields:
      symbol                : uppercase ticker
      breakout_level        : the prior swing high that was broken (close-based)
      breakout_first_day_idx: bar index of the FIRST close above breakout_level
      days_since_breakout   : trading days since that first breakout (0 = today)
      last_close            : today's close
      last_open, last_high, last_low
      extension_atr         : (last_close - breakout_level) / ATR
      atr14                 : Wilder ATR(14)
      ema20, ema50, ema200  : current EMA values
      score                 : composite -- closer to Day 0 + tighter pullback = higher
    """
    # v1.0.1: ctx-aware (Pass 2 #1). Shared prelude hoisted out.
    if ctx is None:
        ctx = build_context(symbol)
    if ctx is None or len(ctx.bars) < 210:
        return None

    bars   = ctx.bars
    closes = ctx.closes
    opens  = ctx.opens
    highs  = ctx.highs
    lows   = ctx.lows
    ema20  = ctx.ema20
    ema50  = ctx.ema50
    ema200 = ctx.ema200
    atr    = ctx.atr14

    last_close = float(closes[-1])
    last_open  = float(opens[-1])
    last_high  = float(highs[-1])
    last_low   = float(lows[-1])

    # ---- Trend gates ----
    if cfg.require_stack and not (ema20[-1] > ema50[-1] > ema200[-1]):
        return None
    if cfg.require_close_above_ema20 and last_close < ema20[-1]:
        return None
    if cfg.require_ema20_above_ema50 and not (ema20[-1] > ema50[-1]):
        return None

    # ---- Prior swing high ----
    # max(closes) over days [-prior_swing_lookback : -prior_swing_exclude_recent].
    # "exclude_recent" ensures the prior swing is the pre-breakout high,
    # not contaminated by the breakout day itself.
    exclude = max(1, cfg.prior_swing_exclude_recent)
    lookback = cfg.prior_swing_lookback
    if len(closes) < lookback + exclude:
        return None
    pre_window = closes[-(lookback + exclude) : -exclude]
    breakout_level = float(np.max(pre_window))

    # ---- First breakout close ----
    # Walk forward from the day AFTER pre_window's end. First close >
    # breakout_level = the breakout event.
    first_breakout_idx: int | None = None
    start = len(closes) - exclude
    for j in range(start, len(closes)):
        if float(closes[j]) > breakout_level:
            first_breakout_idx = j
            break
    if first_breakout_idx is None:
        return None

    days_since_breakout = (len(closes) - 1) - first_breakout_idx
    if days_since_breakout > cfg.max_days_since_breakout:
        return None

    # ---- Still above the level today ----
    if last_close < breakout_level + cfg.min_above_breakout_atr * atr:
        return None

    # ---- Extension filter ----
    extension_atr = (last_close - breakout_level) / atr
    if extension_atr > cfg.max_extension_atr:
        return None

    # ---- Bullish bias (optional) ----
    if cfg.require_today_bullish:
        rng = last_high - last_low
        if rng <= 0:
            return None
        body = abs(last_close - last_open)
        body_ratio = body / rng
        lower_tail = min(last_open, last_close) - last_low
        lower_tail_ratio = lower_tail / rng
        is_pin = body_ratio <= 0.40 and lower_tail_ratio >= 0.50
        if last_close <= last_open and not is_pin:
            return None

    # ---- Score ----
    # Day 0 = highest conviction (freshest signal). Day +5 = stale.
    recency_bonus = max(0, 20 - days_since_breakout * 3)
    # Closer to the broken level (smaller extension) = better R:R for entry.
    # Reward extension in [0.5, 2.0] ATR -- not at the level (no follow-
    # through yet), not extended (entry too late).
    if 0.5 <= extension_atr <= 2.0:
        extension_bonus = 10
    elif extension_atr < 0.5:
        extension_bonus = 5
    else:
        extension_bonus = max(0, 10 - int((extension_atr - 2.0) * 4))
    # Stack bonus: textbook 20>50>200 stack = stronger trend.
    stack_bonus = 10 if (ema20[-1] > ema50[-1] > ema200[-1]) else 0
    score = recency_bonus + extension_bonus + stack_bonus

    return {
        "symbol":                  symbol.upper(),
        "breakout_level":          breakout_level,
        "breakout_first_day_idx":  first_breakout_idx,
        "days_since_breakout":     days_since_breakout,
        "last_close":              last_close,
        "last_open":               last_open,
        "last_high":               last_high,
        "last_low":                last_low,
        "extension_atr":           float(extension_atr),
        "atr14":                   float(atr),
        "ema20":                   float(ema20[-1]),
        "ema50":                   float(ema50[-1]),
        "ema200":                  float(ema200[-1]),
        "score":                   int(score),
    }


def scan_universe(symbols: Iterable[str], cfg: TCBreakoutConfig) -> list[dict]:
    """Loop the universe, call detect_tc_breakout on each. Returns
    candidates sorted by score desc, then extension_atr asc (smaller
    extension = better entry).
    """
    out: list[dict] = []
    for sym in symbols:
        try:
            c = detect_tc_breakout(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_tc_breakout failed: {exc}\n")
            continue
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c["score"], c["extension_atr"]))
    return out
