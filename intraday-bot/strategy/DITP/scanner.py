"""DITP P2 Pattern scanner — Beyond Insights "Higher Probability Breakout".

Source: strategies-reference/DITP.md §6 Setup 1 (sub-setups A / B / C).

Scans the daily-parquet universe (default: all symbols in
data/price_history/daily/) for P2 breakout candidates. Runs end-of-day so
NEXT day's intraday breakout watch is ready before market open. Writes:

    state/watchlist_ditp_<tomorrow>.txt    line per symbol (with variant tag)
    state/watchlist_ditp_<tomorrow>.json   full classification + metadata

Per-family scanner pattern: mirrors strategy/GUNS/scanner.py shape but
runs against daily bars instead of intraday tape, and produces TOMORROW's
watchlist instead of TODAY's. The orchestrator's post-EOD hook should
invoke this (out-of-scope for v0.1; manual CLI for now).

CLI:
    py strategy/DITP/scanner.py
    py strategy/DITP/scanner.py --symbols NVDA,AAPL,MSFT
    py strategy/DITP/scanner.py --variants AC          # skip B
    py strategy/DITP/scanner.py --no-write             # print only

All thresholds are ticker-relative per CLAUDE.md "Normalized strategy
parameters (Option A)" — ATR multiples and scale-free slopes only.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import numpy as np  # type: ignore  # noqa: E402

# Layer-1 pattern primitives — every numeric helper this scanner needs
# lives in resources/patterns.py so other strategies can share them.
from patterns import (  # noqa: E402
    atr_wilder_np,
    ema_np,
    thrust_bar_np,
    window_slopes_np,
    horizontal_resistance_np,
)

import bars_store  # noqa: E402  (resources/bars_store.py)
from symbol_ctx import SymbolContext, build_context  # noqa: E402  (resources/symbol_ctx.py)
from market_calendar import next_trading_day  # noqa: E402  (resources/market_calendar.py)

STATE_DIR = SKILL_DIR / "state"


# ---------- Config ----------

@dataclass
class P2Config:
    """All thresholds are ticker-relative (ATR multiples + scale-free slopes)."""
    # Resistance discovery. User rule 2026-05-27: daily-chart S/R is a
    # 1-year (~252 trading day) read for valleys and mountains. Same
    # window used by P1 / P3 / find_key_levels.
    resistance_lookback: int = 252      # daily bars to scan for swing highs
    swing_radius: int = 3               # bars on each side for swing-high local-max
    # Cluster tolerance switched to absolute ticks 2026-05-27 per user rule
    # "the placeholder cannot be too wide... plus minus 3 tick". Previous
    # 1% percentage scaled badly with price (±$4 for a $400 stock).
    tick_size: float = 0.01             # US equity tick = 1 cent
    cluster_tolerance_ticks: int = 3    # cluster band = 3 × $0.01 = ±$0.03
    min_touches: int = 1                # ≥ N swing highs in cluster (any kind); lowered 2026-05-27 per user reintegration -- single mountain top is a valid resistance
    # Flush-up bar detector — looks BACK over recent bars (not just today's
    # signal candle) for a strong upward bar that broke prior range. Risk =
    # profit-taking after such a move. The flush-up may be days back (DOC
    # pattern, the bar that established the current resistance) or today.
    flush_up_lookback: int = 15          # scan last N daily bars for a flush-up bar
    flush_up_body_atr: float = 1.5       # bar body > N × ATR14
    flush_up_prior_high_window: int = 30 # bar must close above max of prior M bars

    # P2 "already broken" check — a P2 setup is by definition PENDING. A
    # daily CLOSE above resistance counts as a breach. BUT: a breach that
    # was *immediately rejected* (price fell back below resistance for ≥
    # `breach_rejection_grace_days` consecutive days) is treated as a failed
    # breakout — the setup is still valid P2, just under the radar. LYV
    # 2026-05-22: close 169.99 at -5d, then closes -4d 168.87, -3d 167.49,
    # -2d 163.01, -1d 164.44 — failed breakout, P2 stays valid.
    # PM 2026-05-22: closes above at -5d/-3d/-2d, last close above was -2d
    # (1 day ago < 2-day grace) — real breakout in progress, P2 invalidated.
    recent_breakout_lookback: int = 15      # how far back to scan for prior closing breach
    breach_rejection_grace_days: int = 2    # days_since_last_breach > N => rejected, still P2
    # ATR-relative rejection override (added 2026-05-29 per user QCOM case).
    # If any bar AFTER the most-recent breach closed `breach_rejection_atr` *
    # ATR or more BELOW resistance, the breakout has been visibly rejected
    # even when days_since_breach is still inside the grace window. Bypasses
    # the grace return-None so QCOM (breach 5/26, rejection 5/27 at -0.88
    # ATR vs R, today's close 0.28 ATR below R) flows through as P2.
    breach_rejection_atr: float = 0.3
    # Relaxed upper-tail tolerance for rejected-breakout candidates.
    # In the standard pending-P2 case, max_upper_tail_ratio = 15% filters
    # out rejection bars that aren't yet pending breakouts. But in the
    # rejected-breakout case (visible market rejection of a prior close
    # above R), today's bar OFTEN has a substantial upper tail because
    # price re-probed R from below -- and that's exactly the signal the
    # user wants to surface. QCOM 5/28: upper tail = 34% (H probed R
    # again, close pulled back below). Allow up to 50% in this case;
    # anything more would be a heavy bearish bar that's no longer P2.
    max_upper_tail_ratio_rejected: float = 0.50
    # Absolute ATR-magnitude floor for the upper-tail gate (added
    # 2026-05-29 per user TSLA case). TSLA today: range $7.66 (0.51
    # ATR), upper tail $1.86 (0.12 ATR) = 24% of range. The 15%
    # percentage gate rejected this even though the absolute wick is
    # tiny -- the percentage is amplified by the small denominator on
    # tight-range consolidation bars. Skip the percentage gate when
    # upper tail is smaller than `upper_tail_atr_min` ATR -- the wick
    # is absolutely small, so its ratio-of-range doesn't matter.
    upper_tail_atr_min: float = 0.20
    # Trend stack mode (added 2026-05-29 per user MSFT case). MSFT
    # 2026-05-28 is in early-recovery from a downtrend: EMA20 ($415)
    # > EMA50 ($411) but EMA50 < EMA200 ($437). The short-term trend
    # is up but EMA200 is overhead. Strict 20>50>200 rejects MSFT.
    # User calls it P2 because price is at R ($429.92, $2.93 above
    # close). Relaxed mode: require close > EMA20 > EMA50 only --
    # full stack EMA50 > EMA200 becomes a scoring boost (handled in
    # score_candidate), not a hard gate. Strict mode (require_full_stack
    # = True) preserves the v1.0.0 behaviour for the production bot
    # path if needed.
    require_full_stack: bool = False

    # Resistance as a RANGE — user rule (chat 2026-05-22): multiple mountain
    # tops within a tight band form a resistance zone with more conviction.
    # Mountains within ±resistance_range_pct of the preceding mountain count
    # as the resistance range. Per user 2026-05-23 clarification: multi-
    # mountain is a CONVICTION MODIFIER, not an eligibility gate — a valid
    # P2 (any variant) may have a single preceding mountain. The bonus is
    # applied in score_candidate(). LYV's 167.56 / 168.54 / 168.55 mountains
    # within 2% form a 3-mountain zone — high conviction.
    resistance_range_pct: float = 0.02

    # Mountain-top filter — a resistance MUST be anchored to a true left-side peak.
    # A swing high counts as a "mountain top" only if it (a) sits far enough in
    # the past, and (b) price has subsequently fallen away from it by at least
    # `mountain_pullback_atr × ATR14`. Without this filter, recent swing highs
    # inside the current consolidation can falsely anchor a resistance line
    # (the TSLA-vs-PLD discrimination from the 2026-05-22 chat refinement).
    mountain_min_age_bars: int = 15     # ≥ N daily bars from the current bar
    mountain_pullback_atr: float = 2.0  # price must have dropped ≥ N × ATR14 below the peak
    # "Real ceiling" filter — DISABLED by user reintegration 2026-05-27.
    # Original purpose: discriminate TSLA-style mid-downtrend bounces
    # ($418 cluster has $452 peak above = bounce on the way down, not
    # structural R) from PLD-style true ceilings ($145 cluster IS the
    # highest). Replaced by: the EMA-stack trend gate already filters
    # out downtrends, AND the user's framework treats each mountain
    # peak as an independent P2 setup -- higher mountains above the
    # chosen level are FUTURE P2s, not disqualifiers. Set this to
    # 0.02 to restore the legacy strict behavior if needed.
    max_below_window_high_pct: float = 1.0

    # Signal candle eligibility
    max_distance_atr: float = 1.5       # last close within N × ATR14 below resistance
    max_upper_tail_ratio: float = 0.15  # upper_tail / candle_range threshold

    # Consolidation classifier
    consolidation_lookback: int = 10        # daily bars window
    slope_flat_threshold: float = 0.002     # |slope/price| < N => "flat" (0.2%/day)
    slope_rising_threshold: float = 0.001   # slope/price > N => "rising" (0.1%/day) — loosened 2026-05-23 to match real ascending triangles
    tight_rect_height_atr: float = 2.0      # rectangle height < N × ATR14 => "tight" — loosened 2026-05-23 to match real daily-chart rectangles
    # Variant A signal-candle: bullish markup bar = solid green body closing
    # near its high. Per user definition 2026-05-23: "a bullish mark up bar
    # seen at the range".
    markup_body_ratio_min: float = 0.6      # body / range > N
    markup_upper_tail_max: float = 0.10     # (high - close) / range < N
    # Multi-mountain requirement: per user 2026-05-23 REVISION, this is a
    # conviction modifier (scored in score_candidate), NOT an eligibility
    # gate. Field kept for back-compat / future re-tightening but the
    # detector no longer rejects on it. Default 1 = effectively no gate
    # (every candidate already has ≥1 mountain anchor by construction).
    min_range_mountains: int = 1


# ---------- Indicator primitives ----------
#
# Hoisted to resources/patterns.py as of 2026-05-23 so other strategy
# families can share them. The DITP scanner now imports:
#   - atr_wilder_np            (was inline atr14)
#   - ema_np                   (was inline ema)
#   - thrust_bar_np            (was inline find_flush_up_bar)
#   - window_slopes_np         (slope math previously inside classify_variant)
#   - horizontal_resistance_np (was inline find_resistance)
# from resources.patterns. The functions below are the DITP-specific
# orchestration layer — variant taxonomy, scoring, cautions, tier.


def _resistance(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                cfg: P2Config, current_close: float, atr: float
                ) -> tuple[float, int, int, float, float, int] | None:
    """Thin adapter over patterns.horizontal_resistance_np with DITP
    parameter binding. Preserves the legacy tuple shape that
    detect_p2() expects: (level, touches, mountain_anchors, range_low,
    range_high, n_range_mountains).
    """
    r = horizontal_resistance_np(
        highs, lows, closes, current_close, atr,
        lookback=cfg.resistance_lookback,
        swing_radius=cfg.swing_radius,
        min_touches=cfg.min_touches,
        tick_size=cfg.tick_size,
        cluster_tolerance_ticks=cfg.cluster_tolerance_ticks,
        range_pct=cfg.resistance_range_pct,
        mountain_min_age_bars=cfg.mountain_min_age_bars,
        mountain_pullback_atr=cfg.mountain_pullback_atr,
        max_below_window_high_pct=cfg.max_below_window_high_pct,
    )
    if r is None:
        return None
    return (r["level"], r["cluster_touches"], r["mountain_anchors"],
            r["range_low"], r["range_high"], r["range_mountains"])


def classify_variant(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                     closes: np.ndarray, resistance: float, atr: float,
                     cfg: P2Config) -> tuple[str | None, dict]:
    """Classify the consolidation shape as A / B / C. Returns (None, diag)
    if the price action doesn't match any P2 variant — caller drops the
    candidate.

    Mutually-exclusive variant definitions (per user 2026-05-23):

      A = Direct approach — recent slopes BOTH rising AND signal candle is
          a bullish markup bar (solid green, closes near high, no upper
          tail). Price action is approaching the resistance range with
          momentum, no consolidation phase.

      B = Tight rectangle — flat top + flat bottom + rectangle height <
          tight_rect_height_atr × ATR. Price consolidating horizontally
          at the resistance.

      C = Ascending triangle — flat top + rising lows. Price compressing
          into the apex at the resistance.

    Reject (return None):
      - Both slopes clearly falling (price is PULLING AWAY from
        resistance, not approaching — not a P2 setup, could be P3)
      - Doesn't match any of A / B / C
    """
    # Window geometry from the shared primitive
    w = window_slopes_np(
        opens, highs, lows, closes,
        resistance=resistance, atr=atr,
        lookback_bars=cfg.consolidation_lookback,
        tick_size=cfg.tick_size,
        cluster_tolerance_ticks=cfg.cluster_tolerance_ticks,
    )
    slope_h_pct = w["slope_highs_pct_per_bar"]
    slope_l_pct = w["slope_lows_pct_per_bar"]
    rect_height_atr = w["rect_height_atr"]
    near = w["highs_near_resistance"]

    flat_top    = abs(slope_h_pct) < cfg.slope_flat_threshold and near >= 2
    flat_bottom = abs(slope_l_pct) < cfg.slope_flat_threshold
    rising_bot  = slope_l_pct > cfg.slope_rising_threshold
    rising_top  = slope_h_pct > cfg.slope_rising_threshold
    tight       = rect_height_atr < cfg.tight_rect_height_atr
    both_falling = (slope_h_pct < -cfg.slope_flat_threshold
                    and slope_l_pct < -cfg.slope_flat_threshold)

    # Signal-candle test for Variant A — bullish markup bar. Anatomy
    # numbers come from the primitive; the body/tail thresholds and
    # the "is this a markup?" decision are DITP-specific so they stay
    # here.
    rng = w["last_high"] - w["last_low"]
    is_bullish_markup = False
    if rng > 0 and w["last_close"] > w["last_open"]:
        body_ratio = (w["last_close"] - w["last_open"]) / rng
        upper_tail_ratio = (w["last_high"] - w["last_close"]) / rng
        is_bullish_markup = (body_ratio >= cfg.markup_body_ratio_min
                              and upper_tail_ratio <= cfg.markup_upper_tail_max)

    diag = {
        "slope_highs_pct_per_day": round(slope_h_pct, 5),
        "slope_lows_pct_per_day": round(slope_l_pct, 5),
        "rect_height_atr": round(rect_height_atr, 2),
        "highs_near_resistance": near,
        "consolidation_lookback": w["bars_inspected"],
        "is_bullish_markup": bool(is_bullish_markup),
        "both_slopes_falling": bool(both_falling),
    }

    # Reject: pulling away from resistance (both falling)
    if both_falling:
        return None, diag

    # B: tight rectangle at resistance (flat top + flat bottom + tight band)
    if flat_top and flat_bottom and tight:
        return "B", diag

    # C: ascending triangle (flat top + rising lows)
    if flat_top and rising_bot:
        return "C", diag

    # A: direct approach (both rising + bullish markup signal candle)
    if rising_top and rising_bot and is_bullish_markup:
        return "A", diag

    return None, diag


# ---------- Universe membership ----------
#
# Each candidate carries the list of index universes it belongs to so the
# dashboard's DITP tab can filter by universe (S&P 500 / MidCap / SmallCap /
# NASDAQ-100 / DJIA). A symbol can be in multiple universes (e.g. AAPL is in
# S&P 500 AND NASDAQ-100 AND DJIA).

UNIVERSE_LOADERS = [
    ("sp500",     "sp500",          "get_sp500_symbols"),
    ("sp400",     "sp_midcap400",   "get_sp400_symbols"),
    ("sp600",     "sp_smallcap600", "get_sp600_symbols"),
    ("nasdaq100", "nasdaq100",      "get_nasdaq100_symbols"),
    ("djia",      "djia",           "get_djia_symbols"),
]


def build_universe_map() -> dict[str, list[str]]:
    """Return {symbol: [universe_name, ...]} across all known indexes.
    Best-effort: a missing module / scrape failure for one index doesn't kill
    the others — that universe is just omitted from the map."""
    out: dict[str, list[str]] = {}
    for label, mod_name, fn_name in UNIVERSE_LOADERS:
        try:
            mod = __import__(mod_name)
            fn = getattr(mod, fn_name)
            for s in fn():
                out.setdefault(s.upper(), []).append(label)
        except Exception as exc:
            sys.stderr.write(f"[universes] {label}: {exc}\n")
    return out


# Lazy module-level cache so detect_p2() doesn't rebuild it per symbol.
_UNIVERSE_MAP: dict[str, list[str]] | None = None


def _get_universe_map() -> dict[str, list[str]]:
    global _UNIVERSE_MAP
    if _UNIVERSE_MAP is None:
        _UNIVERSE_MAP = build_universe_map()
    return _UNIVERSE_MAP


# ---------- Bar-date helper (backtest look-ahead cutoff) ----------

def _bar_date(ts) -> date:
    """Normalize a bar's timestamp field to a `date` for as_of_date comparison.
    bars_store may yield int (unix s), datetime, or pandas.Timestamp depending
    on source/path; we accept all three."""
    if isinstance(ts, date) and not isinstance(ts, datetime):
        return ts
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    # pandas.Timestamp or anything with .date()
    if hasattr(ts, "date"):
        try:
            return ts.date()
        except Exception:
            pass
    # ISO string fallback
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    raise TypeError(f"_bar_date: unsupported timestamp type {type(ts)!r}")


# ---------- Per-symbol detector ----------

# ---------- Confluence tier (DITP P2 v0.2 hard filter) ----------
#
# Per user teaching 2026-05-23: "P2 Breakout Scenarios that are tradeable
# with more probability — (1) Daily R coincides with round number,
# (2) Major round number, (3) Yesterday's H + significant round number."
# Confluence tier promotes the structural breakout to a high-conviction
# setup; Tier 0 (no confluence) is dropped from the live entry pipeline.

def _round_grid_for_price(price: float) -> tuple[float, float]:
    """Return (minor_grid, major_grid) for a price tier. Minor = the
    regular round-number snap; major = the 'humans say it out loud'
    larger increment."""
    if price < 5:    return (0.50, 1.00)
    if price < 20:   return (1.00, 5.00)
    if price < 100:  return (5.00, 10.00)
    if price < 500:  return (10.0, 50.00)
    if price < 2000: return (25.0, 100.0)
    return (100.0, 250.0)


def _is_near_round_number(level: float, grid: float, tolerance_pct: float = 0.005) -> bool:
    """Is `level` within ±tolerance_pct of a multiple of `grid`?
    Default tolerance 0.5%. A $985 level is within 0.5% of $980/$990 too,
    so we measure to the NEAREST multiple."""
    if level <= 0 or grid <= 0:
        return False
    nearest = round(level / grid) * grid
    return abs(level - nearest) <= level * tolerance_pct


def compute_confluence_tier(resistance: float, yesterday_high: float
                            ) -> tuple[int, list[str]]:
    """Score a P2 setup's confluence quality.

    Returns (tier, reasons):
      tier = 0  → no special confluence (Tier 0 — dropped by hard filter)
      tier = 1  → daily R aligns with a minor round number
      tier = 2  → daily R aligns with a MAJOR round number
      tier = 3  → daily R = yesterday's high AND aligns with a major round
                  (triple confluence — rare, highest conviction)
    """
    if resistance is None or resistance <= 0:
        return 0, []
    minor_grid, major_grid = _round_grid_for_price(resistance)
    reasons: list[str] = []
    tier = 0

    near_major = _is_near_round_number(resistance, major_grid)
    near_minor = _is_near_round_number(resistance, minor_grid)
    near_yesterday_high = (
        yesterday_high > 0
        and abs(resistance - yesterday_high) / resistance <= 0.005   # within 0.5%
    )

    if near_minor:
        tier = max(tier, 1)
        nearest_minor = round(resistance / minor_grid) * minor_grid
        reasons.append(f"daily R near ${nearest_minor:.2f} (minor round)")
    if near_major:
        tier = max(tier, 2)
        nearest_major = round(resistance / major_grid) * major_grid
        reasons.append(f"daily R near ${nearest_major:.2f} (MAJOR round)")
    if near_yesterday_high and near_major:
        tier = 3
        reasons.append(f"daily R ~ yesterday's high ${yesterday_high:.2f} (triple confluence)")
    elif near_yesterday_high:
        reasons.append(f"daily R ~ yesterday's high ${yesterday_high:.2f}")
        # Yesterday-high alone (no major round) is a meaningful signal — bump to Tier 2
        tier = max(tier, 2)

    return tier, reasons


@dataclass
class P2Candidate:
    symbol: str
    variant: str
    universes: list[str]        # which index lists this symbol belongs to (sp500, sp400, sp600, nasdaq100, djia)
    resistance: float           # = range_high (trigger level the bot watches)
    resistance_low: float       # bottom of the resistance zone; distance check uses this
    resistance_touches: int
    resistance_mountains: int   # mountain-qualifying touches in 1% cluster
    resistance_range_mountains: int  # mountain-qualifying peaks in the ±2% range (conviction signal)
    last_close: float
    last_high: float
    last_low: float
    last_range_atr: float       # signal-candle range / ATR14 — drives FLUSH_UP caution
    distance_atr: float         # = (range_low - current) / ATR14
    upper_tail_ratio: float
    ema20: float
    ema50: float
    ema200: float
    atr14: float
    diag: dict
    score: int = 0              # 0–100, populated by score_candidate()
    tier: str = ""              # "A" / "B" / "C" / "D", populated by score_candidate()
    cautions: list[str] = None  # ["FLUSH_UP", "FRESH_RESISTANCE", ...]
    # Confluence + prior-day key levels (per DITP P2 v0.2 spec).
    # confluence_tier (0/1/2/3) drives the hard tradeability filter that
    # ditp_p2's evaluate() enforces — Tier 0 setups are dropped before
    # they ever reach the entry pipeline.
    confluence_tier: int = 0         # 0=plain, 1=at round#, 2=at major round#, 3=triple (R = yH = major round#)
    confluence_reasons: list[str] = None   # human-readable list of which factors contributed
    # Yesterday's daily-bar levels — used as additional intraday support
    # references during the trade. The chart panel draws them as dotted
    # lines (codes D, E, F in the user's key-level taxonomy).
    yesterday_low: float = 0.0       # D — strong support, often where institutions step in
    yesterday_high: float = 0.0      # E — potential polarity-flip target (if today gapped through it)
    yesterday_close: float = 0.0     # F — fair-value anchor / gap pivot


def detect_p2(symbol: str, cfg: P2Config,
              as_of_date: date | None = None,
              ctx: SymbolContext | None = None) -> P2Candidate | None:
    """Apply all P2 rules to one symbol's daily bars. Returns candidate or None.

    `as_of_date` controls look-ahead: when set, only daily bars with
    timestamp on or before as_of_date are visible to the detector. This is
    THE critical knob for backtesting — it lets the scanner simulate what
    it would have produced at the end of day `as_of_date`, with no
    knowledge of any future bars. When None (default), the full parquet
    history is visible (live / EOD-scanner use case).

    `ctx` (added 2026-05-29 efficiency Pass 2 #1): optional pre-built
    SymbolContext for the live / scanner path. IGNORED when
    `as_of_date` is set -- the backtest path needs to filter bars,
    which would invalidate any pre-built context. Live dashboard path
    uses ctx to skip the bars load + numpy arrays + EMA × 3 + ATR
    that this detector would otherwise recompute.
    """
    if as_of_date is not None or ctx is None:
        # Backtest path OR live path without a ctx -- do the load +
        # filter + compute here.
        bars = bars_store.load_bars(symbol, timeframe="daily")
        if as_of_date is not None:
            cutoff = as_of_date
            bars = [b for b in bars if _bar_date(b["t"]) <= cutoff]
        if len(bars) < cfg.resistance_lookback + 14:
            return None
        closes = np.array([b["c"] for b in bars], dtype=float)
        highs  = np.array([b["h"] for b in bars], dtype=float)
        lows   = np.array([b["l"] for b in bars], dtype=float)
        opens  = np.array([b["o"] for b in bars], dtype=float)
        e20  = ema_np(closes, 20)
        e50  = ema_np(closes, 50)
        e200 = ema_np(closes, 200)
        atr  = atr_wilder_np(highs, lows, closes, period=14)
        if atr <= 0:
            return None
    else:
        # ctx-supplied path (dashboard batched scan).
        if len(ctx.bars) < cfg.resistance_lookback + 14:
            return None
        bars   = ctx.bars
        closes = ctx.closes
        highs  = ctx.highs
        lows   = ctx.lows
        opens  = ctx.opens
        e20    = ctx.ema20
        e50    = ctx.ema50
        e200   = ctx.ema200
        atr    = ctx.atr14

    # 1. Bullish EMA stack + price > EMA20.
    # Strict mode (require_full_stack=True): full 20>50>200 stack.
    # Relaxed mode (default): close > EMA20 > EMA50 only -- accepts
    # early-recovery cases like MSFT 2026-05-28 where short-term trend
    # is up but EMA200 is still overhead.
    if cfg.require_full_stack:
        if not (e20[-1] > e50[-1] > e200[-1] and closes[-1] > e20[-1]):
            return None
    else:
        if not (closes[-1] > e20[-1] > e50[-1]):
            return None

    # 3. Horizontal resistance above current, anchored to a left-side mountain top
    r = _resistance(highs, lows, closes, cfg, float(closes[-1]), atr)
    if r is None:
        return None
    resistance, touches, mountains, range_low, range_high, n_range_mountains = r

    # Note (user clarification 2026-05-23): multi-mountain resistance is a
    # CONVICTION BOOST, not an eligibility gate. A single preceding mountain
    # is still a valid P2 anchor — it just earns SINGLE_MOUNTAIN caution +
    # lower validation score downstream. The legacy hard gate
    #     if n_range_mountains < cfg.min_range_mountains: return None
    # has been removed so single-mountain candidates flow through and get
    # tier'd by score_candidate() like everything else.

    # 4. Pending breakout — distance check uses range_low (the closest mountain
    #    in the resistance zone), so a wide range (LYV: zone [167.56, 173.12])
    #    qualifies as long as the BOTTOM of the zone is within reach. The
    #    trigger level the bot watches for breakout is range_high.
    distance_atr = (range_low - float(closes[-1])) / atr
    if distance_atr < 0 or distance_atr > cfg.max_distance_atr:
        return None
    # User correction 2026-05-28 (ASTS case): the old `highs[-1] >
    # resistance` gate rejected pending-breakout pin bars where price
    # spiked through R intraday then closed back below. That's EXACTLY
    # the textbook P2 setup -- testing the level, not yet a confirmed
    # close-above breakout. Switched to a close-based check: if close
    # > resistance the breakout has CONFIRMED (graduated to P3 retest
    # watch), so the symbol is no longer a pending-P2 candidate.
    if float(closes[-1]) > resistance:
        return None  # already closed above today; graduated to P3
    # Find the most recent close ABOVE resistance (no buffer — even a tiny
    # close-above counts as a breach event for this check). If days_since_breach
    # ≤ grace period, the breakout is still active → graduated to P3. If days
    # > grace period AND we're below resistance now, the breakout was REJECTED
    # and the symbol is still a valid P2 (LYV's pattern). If no breach at all
    # in lookback, the setup is plain pending P2.
    lb_breach = min(cfg.recent_breakout_lookback, len(closes))
    last_breach_idx: int | None = None
    for j in range(len(closes) - 1, len(closes) - lb_breach - 1, -1):
        if j < 0:
            break
        if float(closes[j]) > resistance:
            last_breach_idx = j
            break
    is_rejected_breakout = False
    if last_breach_idx is not None:
        days_since_breach = (len(closes) - 1) - last_breach_idx
        # ATR-relative rejection check (added 2026-05-29 per user QCOM
        # case). Any post-breach close <= R - breach_rejection_atr * ATR
        # is a visible rejection -- we've SEEN the market reject the
        # breakout, regardless of grace window.
        rejection_threshold = resistance - cfg.breach_rejection_atr * atr
        rejection_seen = False
        for k in range(last_breach_idx + 1, len(closes)):
            if float(closes[k]) <= rejection_threshold:
                rejection_seen = True
                break
        if rejection_seen:
            # Mark the candidate so downstream gates (the upper-tail
            # filter below) can relax for this "failed breakout, now
            # pending again" pattern.
            is_rejected_breakout = True
        elif days_since_breach <= cfg.breach_rejection_grace_days:
            # No visible rejection AND still in grace window -> can't
            # yet tell whether the breach is a real breakout. Treat as
            # graduated to P3 watch (current behaviour pre-fix).
            return None

    # 5. Last candle has NO UPPER TAIL. Three modes:
    #   - Absolute-magnitude floor (TSLA case): if the upper tail is
    #     smaller than `upper_tail_atr_min * ATR` in absolute terms,
    #     skip the percentage gate -- the wick is too small to be
    #     a meaningful rejection regardless of its ratio-of-range.
    #   - Rejected-breakout (QCOM case): relaxed limit because today's
    #     high probing R from below is the signal, not a bear flag.
    #   - Standard: tight 15% ratio cap.
    body_top = max(float(opens[-1]), float(closes[-1]))
    rng = float(highs[-1] - lows[-1])
    upper_tail = float(highs[-1]) - body_top
    upper_tail_atr = upper_tail / atr if atr > 0 else 0.0
    ratio = (upper_tail / rng) if rng > 0 else 0.0  # always defined for journaling
    if upper_tail_atr >= cfg.upper_tail_atr_min:
        upper_tail_limit = (cfg.max_upper_tail_ratio_rejected
                           if is_rejected_breakout
                           else cfg.max_upper_tail_ratio)
        if ratio > upper_tail_limit:
            return None

    # 6. Classify Setup A / B / C. User correction 2026-05-28 (ASTS):
    # variant=None previously rejected the candidate (e.g., ASTS's
    # ~43% body falls between markup threshold and rectangle thresholds
    # so no variant fits). For the dashboard's P2 surfacing this is
    # too strict. Fallback to "U" (unclassified P2) when no A/B/C
    # pattern fits but the symbol is still pending breakout. Downstream
    # scoring penalises tier via variant bonus (U=5 like fallback).
    variant, diag = classify_variant(opens, highs, lows, closes, resistance, atr, cfg)
    if variant is None:
        if diag.get("both_slopes_falling"):
            return None     # PULLING AWAY -- definitively not P2
        variant = "U"       # pending breakout, no clean A/B/C anatomy

    last_range_atr = rng / atr if atr > 0 else 0.0
    flush_offset = thrust_bar_np(
        opens, highs, lows, closes, atr,
        body_atr_min=cfg.flush_up_body_atr,
        prior_high_window=cfg.flush_up_prior_high_window,
        lookback=cfg.flush_up_lookback,
    )
    if flush_offset is not None:
        diag["flush_up_bar_offset_days"] = int(flush_offset)
        diag["flush_up_bar_body_atr"] = round(
            abs(float(closes[flush_offset]) - float(opens[flush_offset])) / atr, 2
        )

    # Prior-day key levels (D / E / F per user's key-level taxonomy).
    # `closes[-1]` is "today" in the daily-parquet sense; index -2 is the
    # most recent completed prior session.
    if len(bars) >= 2:
        prev = bars[-2]
        yesterday_high  = round(float(prev["h"]), 2)
        yesterday_low   = round(float(prev["l"]), 2)
        yesterday_close = round(float(prev["c"]), 2)
    else:
        yesterday_high = yesterday_low = yesterday_close = 0.0

    # Confluence tier — Tier 0 candidates pass the daily scan but are
    # silently filtered out by the orchestrator's DITP P2 evaluate()
    # before they reach the entry pipeline (hard filter per user 2026-05-23).
    conf_tier, conf_reasons = compute_confluence_tier(
        resistance=round(range_high, 2),
        yesterday_high=yesterday_high,
    )

    cand = P2Candidate(
        symbol=symbol,
        variant=variant,
        universes=_get_universe_map().get(symbol.upper(), []),
        resistance=round(range_high, 2),         # trigger = top of zone
        resistance_low=round(range_low, 2),       # bottom of zone (drives distance)
        resistance_touches=touches,
        resistance_mountains=mountains,
        resistance_range_mountains=n_range_mountains,
        last_close=round(float(closes[-1]), 2),
        last_high=round(float(highs[-1]), 2),
        last_low=round(float(lows[-1]), 2),
        last_range_atr=round(last_range_atr, 2),
        distance_atr=round(distance_atr, 2),
        upper_tail_ratio=round(ratio, 3),
        ema20=round(float(e20[-1]), 2),
        ema50=round(float(e50[-1]), 2),
        ema200=round(float(e200[-1]), 2),
        atr14=round(atr, 2),
        diag=diag,
        confluence_tier=conf_tier,
        confluence_reasons=conf_reasons,
        yesterday_high=yesterday_high,
        yesterday_low=yesterday_low,
        yesterday_close=yesterday_close,
    )
    score_candidate(cand, atr)
    return cand


# ---------- Shortlist ranking guideline (DITP-specific) ----------
#
# Scoring layer that turns eligibility-passing candidates into a ranked
# shortlist. Documented in strategies-reference/DITP.md §6.5. Five weighted
# components sum to 100; cautions are SEPARATE annotations that demote tier
# but don't subtract from the raw score.
#
# Components (weighted sum = 100):
#   1. Proximity to resistance      (0–25)  closer = better
#   2. Resistance validation        (0–25)  touches × mountains
#   3. Signal candle anatomy        (0–20)  smaller upper tail = better
#   4. Trend strength (EMA spread)  (0–20)  EMA20 well above EMA50/200 = stronger
#   5. Variant tightness bonus      (0–10)  B (tight rect) > C (ascending) > A (direct)
#
# Cautions (annotations; influence tier mapping):
#   FRESH_RESISTANCE   — 0 mountain anchors (resistance is recent, not historically tested)
#   FLUSH_UP           — signal candle range > 1.5 × ATR14 (rapid push, exhaustion risk)
#   BIG_TAIL           — upper_tail_ratio in (0.10, 0.15] (push showed rejection)
#   SINGLE_MOUNTAIN    — exactly 1 mountain anchor (weaker validation)
#   WIDE_BASE          — 10-bar rectangle height > 2 × ATR (loose consolidation)
#
# Tier mapping (final shortlist sort key, A first):
#   A — score ≥ 75 AND no major cautions (FLUSH_UP / FRESH_RESISTANCE)
#   B — score ≥ 60 OR (≥ 75 with one major caution)
#   C — score ≥ 45 OR has ≤ 2 cautions
#   D — anything else (kept in the JSON for review, omitted from .txt watchlist)

def score_candidate(c: P2Candidate, atr: float) -> None:
    """Compute score / tier / cautions in-place on the candidate."""
    comp: dict[str, int] = {}
    cautions: list[str] = []

    # 1. Proximity to resistance (0–25)
    if c.distance_atr <= 0.2:    comp["proximity"] = 25
    elif c.distance_atr <= 0.5:  comp["proximity"] = 20
    elif c.distance_atr <= 1.0:  comp["proximity"] = 15
    else:                        comp["proximity"] = 10

    # 2. Resistance validation (0–25) — touches × mountain anchors in the 1%
    #    cluster + conviction bonus for multi-mountain resistance ZONE (2%
    #    range). LYV's 3 mountains in zone gets the full bonus.
    base_touches = 8 if c.resistance_touches >= 4 else (
                   6 if c.resistance_touches == 3 else 4)
    base_mtns    = 15 if c.resistance_mountains >= 3 else (
                   12 if c.resistance_mountains == 2 else (
                    8 if c.resistance_mountains == 1 else 2))
    range_conv   = 6 if c.resistance_range_mountains >= 3 else (
                   3 if c.resistance_range_mountains == 2 else 0)
    comp["validation"] = min(25, base_touches + base_mtns + range_conv)

    # 3. Signal candle anatomy (0–20)
    if c.upper_tail_ratio <= 0.02:   comp["anatomy"] = 20
    elif c.upper_tail_ratio <= 0.05: comp["anatomy"] = 17
    elif c.upper_tail_ratio <= 0.10: comp["anatomy"] = 12
    else:                            comp["anatomy"] = 6

    # 4. Trend strength via EMA20/EMA50/EMA200 spread (0–20)
    if atr > 0:
        gap_20_50 = (c.ema20 - c.ema50) / atr
        gap_50_200 = (c.ema50 - c.ema200) / atr
    else:
        gap_20_50 = gap_50_200 = 0.0
    spread = (gap_20_50 + gap_50_200) / 2
    if spread >= 3.0:   comp["trend"] = 20
    elif spread >= 2.0: comp["trend"] = 16
    elif spread >= 1.0: comp["trend"] = 11
    elif spread >= 0.3: comp["trend"] = 6
    else:               comp["trend"] = 2

    # 5. Variant tightness bonus (0–10)
    comp["variant"] = {"B": 10, "C": 9, "A": 7}.get(c.variant, 5)

    c.score = sum(comp.values())
    c.diag["score_components"] = comp

    # ---- Cautions ----
    if c.resistance_mountains == 0:
        cautions.append("FRESH_RESISTANCE")
    elif c.resistance_mountains == 1:
        cautions.append("SINGLE_MOUNTAIN")
    # FLUSH_UP: a strong upward bar within the last N days that punched
    # through prior range. Profit-taking risk on retest of the resulting
    # resistance. See patterns.thrust_bar_np() for the precise rule. The flush
    # may be days ago (DOC pattern) or today.
    if "flush_up_bar_offset_days" in c.diag:
        cautions.append("FLUSH_UP")
    if 0.10 < c.upper_tail_ratio <= 0.15:
        cautions.append("BIG_TAIL")
    if c.diag.get("rect_height_atr", 0) > 2.0:
        cautions.append("WIDE_BASE")
    c.cautions = cautions

    # ---- Tier ----
    has_major = any(x in cautions for x in ("FRESH_RESISTANCE", "FLUSH_UP"))
    n_cautions = len(cautions)
    if c.score >= 75 and not has_major:
        c.tier = "A"
    elif c.score >= 75 and n_cautions <= 1:
        c.tier = "B"
    elif c.score >= 60:
        c.tier = "B"
    elif c.score >= 45 or n_cautions <= 2:
        c.tier = "C"
    else:
        c.tier = "D"


# ---------- Universe + output ----------

def next_trading_day_iso(today: date | None = None) -> str:
    """Next NYSE trading day (Mon-Fri AND skipping US market holidays).

    Holiday-aware via resources/market_calendar.next_trading_day. Prior to
    2026-05-26 this function only skipped Sat/Sun, which produced a holiday
    target_date when the scanner ran the trading day immediately before a
    holiday (e.g., EOD Fri 2026-05-22 wrote a watchlist targeted at Memorial
    Day Mon 2026-05-25, which then had no daily bars to evaluate).
    """
    today = today or date.today()
    return next_trading_day(today).strftime("%Y-%m-%d")


def scan_universe(symbols: Iterable[str], cfg: P2Config,
                  variants_allowed: set[str],
                  as_of_date: date | None = None) -> list[P2Candidate]:
    """`as_of_date` is propagated to detect_p2() — see its docstring for the
    backtest look-ahead-cutoff semantics. Default None = live scan."""
    out: list[P2Candidate] = []
    for sym in symbols:
        try:
            c = detect_p2(sym, cfg, as_of_date=as_of_date)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_p2 failed: {exc}\n")
            continue
        if c is None:
            continue
        if c.variant not in variants_allowed:
            continue
        out.append(c)
    # Final shortlist sort: distance (close first) → tier (A first) → score (high first).
    # Per user rule 2026-05-23: "if the current candle near the immediate
    # preceding mountain top resistance, then rank the first for P2 setup
    # as priority". Proximity is the primary actionability signal — a
    # candidate 0.10 ATR from breakout will trigger TODAY before one 1.0
    # ATR away regardless of validation quality. Tier + score remain as
    # tiebreakers so user can still see the structural conviction.
    tier_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    out.sort(key=lambda c: (c.distance_atr, tier_rank.get(c.tier, 9), -c.score))
    return out


def write_watchlist(candidates: list[P2Candidate], target_date_iso: str) -> tuple[Path, Path]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = STATE_DIR / f"watchlist_ditp_{target_date_iso}.txt"
    json_path = STATE_DIR / f"watchlist_ditp_{target_date_iso}.json"
    # txt: SYM\ttier\tvariant\tresistance\tconfluence_tier — D-tier AND Tier-0
    # confluence omitted from the trade-watch list (kept in .json for review).
    # The orchestrator's entry phase reads .txt.
    #
    # Confluence Tier 0 hard filter (DITP P2 v0.2): per user 2026-05-23, only
    # P2 breakouts that coincide with a round number, major round number, OR
    # yesterday's high are tradeable with sufficient probability. Plain
    # breakouts with no confluence anchor are dropped from .txt to keep the
    # live entry pipeline focused on high-conviction setups.
    lines = [
        f"{c.symbol}\t{c.tier}\tP2_{c.variant}\t{c.resistance}\tCONF{c.confluence_tier}"
        for c in candidates
        if c.tier != "D" and c.confluence_tier > 0
    ]
    txt_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    # json: full metadata
    payload = {
        "target_date": target_date_iso,
        "scanner_run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_candidates": len(candidates),
        "candidates": [asdict(c) for c in candidates],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return txt_path, json_path


# ---------- CLI ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated symbols; default = all daily-parquet symbols")
    ap.add_argument("--variants", default="ABC",
                    help="which sub-setups to keep (A, B, C or any combination). Default ABC.")
    ap.add_argument("--no-write", action="store_true",
                    help="print only; do not write state/watchlist_ditp_*.{txt,json}")
    ap.add_argument("--target-date",
                    help="YYYY-MM-DD watchlist target date (default = next business day)")
    args = ap.parse_args()

    cfg = P2Config()
    variants_allowed = set(args.variants.upper())
    if not variants_allowed.issubset({"A", "B", "C"}):
        sys.stderr.write(f"--variants must be a subset of A/B/C, got {args.variants!r}\n")
        return 2

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = bars_store.list_symbols("daily")
    if not symbols:
        print("(no daily-parquet symbols on disk)")
        return 1
    sys.stdout.write(f"# DITP P2 scan: {len(symbols)} symbols, variants={sorted(variants_allowed)}\n")
    sys.stdout.flush()

    candidates = scan_universe(symbols, cfg, variants_allowed)

    n_tradeable = sum(1 for c in candidates if c.tier != "D" and c.confluence_tier > 0)
    sys.stdout.write(f"# {len(candidates)} candidates ({n_tradeable} tradeable after Tier-0 confluence filter), "
                     f"sorted by distance > tier > score:\n")
    sys.stdout.write(f"{'SYM':<6} {'T':<2} {'V':<2} {'cnf':>3} {'score':>5} {'last':>8} "
                     f"{'rng_low':>8} {'rng_high':>8} {'distATR':>8} {'upTail':>7} "
                     f"{'t/m/rM':>8}  conf_reasons / cautions\n")
    for c in candidates:
        cau = ",".join(c.cautions) if c.cautions else "-"
        cnf = (c.confluence_reasons[0] if c.confluence_reasons else "-")
        annot = f"{cnf} | {cau}"
        sys.stdout.write(
            f"{c.symbol:<6} {c.tier:<2} {c.variant:<2} {c.confluence_tier:>3d} {c.score:>5d} "
            f"{c.last_close:>8.2f} {c.resistance_low:>8.2f} {c.resistance:>8.2f} "
            f"{c.distance_atr:>8.2f} {c.upper_tail_ratio:>7.3f} "
            f"{c.resistance_touches:>2d}/{c.resistance_mountains:<1d}/{c.resistance_range_mountains:<2d}  {annot}\n"
        )

    if not args.no_write:
        target = args.target_date or next_trading_day_iso()
        txt_path, json_path = write_watchlist(candidates, target)
        rel_txt = txt_path.relative_to(SKILL_DIR)
        rel_json = json_path.relative_to(SKILL_DIR)
        sys.stdout.write(f"\n# wrote {rel_txt}\n# wrote {rel_json}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
