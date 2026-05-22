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

import bars_store  # noqa: E402  (resources/bars_store.py)

STATE_DIR = SKILL_DIR / "state"


# ---------- Config ----------

@dataclass
class P2Config:
    """All thresholds are ticker-relative (ATR multiples + scale-free slopes)."""
    # Resistance discovery
    resistance_lookback: int = 90       # daily bars to scan for swing highs
    swing_radius: int = 3               # bars on each side for swing-high local-max
    cluster_band_pct: float = 0.01      # within 1% of each other forms a cluster
    min_touches: int = 2                # ≥ N swing highs in cluster (any kind)
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

    # Resistance as a RANGE — user rule (chat 2026-05-22): multiple mountain
    # tops within a tight band form a resistance zone with more conviction.
    # Mountains within ±resistance_range_pct of the preceding mountain count
    # as the resistance range. `n_range_mountains ≥ 2` adds a conviction
    # bonus in score_candidate(). LYV's 167.56 / 168.54 / 168.55 mountains
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
    # "Real ceiling" filter — the cluster must be the actual top of the window,
    # not a midway point in a downtrend. If a higher mountain top exists
    # significantly above the cluster level, the cluster is just a bounce on
    # the way down, not a structural resistance. Discriminates TSLA (418
    # cluster has a 452 peak above it) from PLD (145 cluster is the highest).
    max_below_window_high_pct: float = 0.02  # cluster level ≥ max_mountain × (1 - N)

    # Signal candle eligibility
    max_distance_atr: float = 1.5       # last close within N × ATR14 below resistance
    max_upper_tail_ratio: float = 0.15  # upper_tail / candle_range threshold

    # Consolidation classifier
    consolidation_lookback: int = 10    # daily bars window
    slope_flat_threshold: float = 0.002 # |slope/price| < N => "flat" (0.2%/day)
    slope_rising_threshold: float = 0.003  # slope/price > N => "rising"
    tight_rect_height_atr: float = 1.0  # rectangle height < N × ATR14 => "tight"


# ---------- Indicator primitives ----------

def ema(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros_like(arr, dtype=float)
    out[0] = arr[0]
    alpha = 2.0 / (n + 1)
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def find_flush_up_bar(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                      closes: np.ndarray, atr: float, cfg: P2Config) -> int | None:
    """Find the most recent flush-up bar in the last `flush_up_lookback`
    daily bars. A flush-up bar = body > flush_up_body_atr × ATR14 AND
    close > max(prior flush_up_prior_high_window bars' highs).

    Returns the bar's offset from the end (e.g., -11 if it was 11 days ago)
    or None if no flush-up bar exists in the window.

    Mechanics behind the user's "profit taking" caution: a single oversized
    bullish bar that punches through prior range traps quick-profit traders
    AT the breakout level, so any retest of the resulting resistance has
    elevated selling pressure from those holders.
    """
    if atr <= 0:
        return None
    n = len(closes)
    lookback = min(cfg.flush_up_lookback, n)
    prior_w = cfg.flush_up_prior_high_window
    most_recent_match: int | None = None
    for offset in range(-lookback, 0):
        if n + offset - prior_w < 0:
            continue
        body = abs(float(closes[offset]) - float(opens[offset]))
        if body <= cfg.flush_up_body_atr * atr:
            continue
        if float(closes[offset]) <= float(opens[offset]):
            continue  # body must be bullish (close > open)
        prior_high = highs[offset - prior_w:offset].max()
        if float(closes[offset]) > prior_high:
            most_recent_match = offset   # keep updating; latest match wins
    return most_recent_match


def atr14(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, n: int = 14) -> float:
    """Wilder ATR — returns last-bar value."""
    if len(highs) < n + 1:
        return 0.0
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    atr = np.zeros_like(tr)
    atr[:n] = tr[:n].mean()  # bootstrap
    for i in range(n, len(tr)):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return float(atr[-1])


def find_resistance(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                    cfg: P2Config, current_close: float, atr: float
                    ) -> tuple[float, int, int] | None:
    """Find lowest horizontal resistance above current price.

    Resistance must be anchored to a "mountain top" — a swing high that is
    both old enough (≥ mountain_min_age_bars from the current bar) AND has
    a real pullback (price dropped ≥ mountain_pullback_atr × ATR14 below it
    at some point after the peak).

    All swing highs in `resistance_lookback` are CANDIDATES; only the
    qualifying mountain tops can ANCHOR a resistance. Recent (non-mountain)
    swing highs may still count as touches inside the cluster band.

    Returns (level, n_touches, n_mountain_anchors) for the LOWEST resistance
    above current_close, or None.
    """
    lb = min(cfg.resistance_lookback, len(highs))
    window_h = highs[-lb:]
    window_l = lows[-lb:]
    if len(window_h) < cfg.swing_radius * 2 + 1:
        return None
    r = cfg.swing_radius

    # 1. All swing highs in the window.
    swings: list[tuple[int, float]] = []
    for i in range(r, len(window_h) - r):
        if window_h[i] == window_h[i - r:i + r + 1].max():
            swings.append((i, float(window_h[i])))
    if len(swings) < cfg.min_touches:
        return None

    # 2. Filter to "mountain tops" — old enough + with pullback.
    mountains: list[tuple[int, float]] = []
    last_idx = len(window_h) - 1
    for i, h in swings:
        if (last_idx - i) < cfg.mountain_min_age_bars:
            continue  # too recent — could be part of the current consolidation
        # Pullback check: any bar AFTER the peak (still inside the window)
        # with low ≤ h - mountain_pullback_atr × ATR14.
        if i + 1 >= len(window_l):
            continue
        post = window_l[i + 1:]
        if (post.min() if len(post) else float("inf")) <= h - cfg.mountain_pullback_atr * atr:
            mountains.append((i, h))

    # Mountain anchors are now a RANKING dimension, not a hard gate. A
    # resistance with no mountain anchors (DOC pattern — broke through prior
    # high, now testing a fresh recent peak) still qualifies, but gets
    # FRESH_RESISTANCE flagged downstream so the scorer demotes it.
    mountain_idxs = {i for i, _ in mountains}
    # Ceiling gate uses MAX MOUNTAIN (not max swing) so a recent non-mountain
    # wick above the mountain zone doesn't disqualify a valid setup.
    # LYV 2026-05-22: 173.12 wick at -4d would have falsely failed the
    # 168.55 mountain-anchored level vs max swing. Using max_mountain instead,
    # 168.55 IS the highest mountain → ceiling passes.
    max_mountain_high = max((h for _, h in mountains), default=0.0)

    # 3. The resistance LEVEL = the climax of the PRECEDING MOUNTAIN above
    # current price. User rule (chat 2026-05-22, rephrased): "immediate high
    # above current means the climax of the preceding mountain." "Preceding"
    # = the mountain that immediately precedes the current price action in
    # time. A "mountain" is a swing high old enough (≥ mountain_min_age_bars)
    # AND with subsequent pullback (≥ mountain_pullback_atr × ATR14). Recent
    # unvalidated bumps don't count.
    #
    # FALLBACK: if no mountain exists above current price, fall back to the
    # most recent swing high above current — this covers DOC's pattern
    # (broke through prior peaks to fresh territory; new resistance hasn't
    # been validated as a mountain yet). The downstream scorer will fire
    # FRESH_RESISTANCE caution because cluster_mountains will be 0.
    swings_above = [(i, h) for i, h in swings if h > current_close]
    if not swings_above:
        return None
    mountains_above = [(i, h) for i, h in swings_above if i in mountain_idxs]
    if mountains_above:
        # Preceding mountain (most recent in time above current)
        i_imm, h_imm = max(mountains_above, key=lambda x: x[0])
    else:
        # No mountain above → fallback to most recent swing high above
        i_imm, h_imm = max(swings_above, key=lambda x: x[0])
    level = float(h_imm)

    # Resistance RANGE — the CONSENSUS of mountain tops within
    # ±resistance_range_pct of the preceding mountain. Non-mountain swings
    # (recent wicks) are NOT part of the consensus; they're outlier
    # candidates that may represent failed breakouts (e.g., LYV's 173.12
    # spike at -4d, which broke above the 167–168 mountain consensus and
    # was rejected). User rule (chat 2026-05-22): "arguably 168 as a
    # consensus resistance with one outlier false breakout to $173" —
    # the outlier doesn't define the resistance; the mountain consensus
    # does.
    if mountains_above:
        range_mtns_only = [h for _, h in mountains_above
                           if abs(h - level) / level <= cfg.resistance_range_pct]
    else:
        range_mtns_only = [level]   # fallback: single non-mountain anchor
    range_low = min(range_mtns_only)
    range_high = max(range_mtns_only)
    n_range_mountains = len(range_mtns_only) if mountains_above else 0

    # "Real ceiling" gate — there must be NO HIGHER MOUNTAIN above the level
    # (a recent non-mountain wick doesn't disqualify — only validated peaks
    # constitute a higher resistance). TSLA's 418 cluster failed this because
    # its 452 mountain was overhead. LYV's 168.55 IS the highest mountain
    # in window → ceiling passes.
    if max_mountain_high > 0 and level < max_mountain_high * (1 - cfg.max_below_window_high_pct):
        return None

    # Count touches + mountain anchors WITHIN the cluster band around the
    # immediate level. Lower swings in the same price band count as touches
    # validating the level; mountain-qualifying touches drive the
    # FRESH_RESISTANCE / SINGLE_MOUNTAIN cautions in score_candidate().
    cluster = [(i, hh) for i, hh in swings
               if abs(hh - level) / level <= cfg.cluster_band_pct]
    if len(cluster) < cfg.min_touches:
        return None
    n_mountains_in_cluster = sum(1 for i, _ in cluster if i in mountain_idxs)
    return (level, len(cluster), n_mountains_in_cluster, range_low, range_high, n_range_mountains)


def classify_variant(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                     resistance: float, atr: float, cfg: P2Config) -> tuple[str, dict]:
    """Classify the consolidation shape as A / B / C.

    A = direct approach (no specific consolidation; falls through)
    B = tight horizontal rectangle just below resistance
    C = ascending triangle (flat top + rising lows)
    """
    N = min(cfg.consolidation_lookback, len(highs))
    recent_highs = highs[-N:]
    recent_lows = lows[-N:]

    x = np.arange(N, dtype=float)
    slope_h = float(np.polyfit(x, recent_highs, 1)[0])
    slope_l = float(np.polyfit(x, recent_lows, 1)[0])

    px = float(closes[-1])
    slope_h_pct = slope_h / px if px else 0.0
    slope_l_pct = slope_l / px if px else 0.0

    rect_height = float(recent_highs.max() - recent_lows.min())
    rect_height_atr = rect_height / atr if atr else float("inf")

    near = np.sum(np.abs(recent_highs - resistance) / resistance < cfg.cluster_band_pct)

    flat_top    = abs(slope_h_pct) < cfg.slope_flat_threshold and near >= 2
    flat_bottom = abs(slope_l_pct) < cfg.slope_flat_threshold
    rising_bot  = slope_l_pct > cfg.slope_rising_threshold
    tight       = rect_height_atr < cfg.tight_rect_height_atr

    diag = {
        "slope_highs_pct_per_day": round(slope_h_pct, 5),
        "slope_lows_pct_per_day": round(slope_l_pct, 5),
        "rect_height_atr": round(rect_height_atr, 2),
        "highs_near_resistance": int(near),
        "consolidation_lookback": N,
    }

    if flat_top and rising_bot:
        return "C", diag
    if flat_top and flat_bottom and tight:
        return "B", diag
    return "A", diag


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


# ---------- Per-symbol detector ----------

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


def detect_p2(symbol: str, cfg: P2Config) -> P2Candidate | None:
    """Apply all P2 rules to one symbol's daily bars. Returns candidate or None."""
    bars = bars_store.load_bars(symbol, timeframe="daily")
    if len(bars) < 220:
        return None  # need ≥ 200 for EMA200 to stabilize + headroom
    closes = np.array([b["c"] for b in bars], dtype=float)
    highs  = np.array([b["h"] for b in bars], dtype=float)
    lows   = np.array([b["l"] for b in bars], dtype=float)
    opens  = np.array([b["o"] for b in bars], dtype=float)

    # 1. Bullish EMA stack + price > EMA20
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    if not (e20[-1] > e50[-1] > e200[-1] and closes[-1] > e20[-1]):
        return None

    # 2. ATR14
    atr = atr14(highs, lows, closes)
    if atr <= 0:
        return None

    # 3. Horizontal resistance above current, anchored to a left-side mountain top
    r = find_resistance(highs, lows, closes, cfg, float(closes[-1]), atr)
    if r is None:
        return None
    resistance, touches, mountains, range_low, range_high, n_range_mountains = r

    # 4. Pending breakout — distance check uses range_low (the closest mountain
    #    in the resistance zone), so a wide range (LYV: zone [167.56, 173.12])
    #    qualifies as long as the BOTTOM of the zone is within reach. The
    #    trigger level the bot watches for breakout is range_high.
    distance_atr = (range_low - float(closes[-1])) / atr
    if distance_atr < 0 or distance_atr > cfg.max_distance_atr:
        return None
    if float(highs[-1]) > resistance:
        return None  # already broken intraday today
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
    if last_breach_idx is not None:
        days_since_breach = (len(closes) - 1) - last_breach_idx
        if days_since_breach <= cfg.breach_rejection_grace_days:
            return None  # recent breach within grace → graduated to P3, not P2

    # 5. Last candle has NO UPPER TAIL
    body_top = max(float(opens[-1]), float(closes[-1]))
    rng = float(highs[-1] - lows[-1])
    upper_tail = float(highs[-1]) - body_top
    ratio = (upper_tail / rng) if rng > 0 else 0.0
    if ratio > cfg.max_upper_tail_ratio:
        return None

    # 6. Classify Setup A / B / C
    variant, diag = classify_variant(highs, lows, closes, resistance, atr, cfg)

    last_range_atr = rng / atr if atr > 0 else 0.0
    flush_offset = find_flush_up_bar(opens, highs, lows, closes, atr, cfg)
    if flush_offset is not None:
        diag["flush_up_bar_offset_days"] = int(flush_offset)
        diag["flush_up_bar_body_atr"] = round(
            abs(float(closes[flush_offset]) - float(opens[flush_offset])) / atr, 2
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
    # resistance. See find_flush_up_bar() for the precise rule. The flush
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
    """Next business day (Mon-Fri only; ignores US holidays — best-effort)."""
    today = today or date.today()
    nxt = today + timedelta(days=1)
    while nxt.weekday() >= 5:   # 5=Sat 6=Sun
        nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d")


def scan_universe(symbols: Iterable[str], cfg: P2Config,
                  variants_allowed: set[str]) -> list[P2Candidate]:
    out: list[P2Candidate] = []
    for sym in symbols:
        try:
            c = detect_p2(sym, cfg)
        except Exception as exc:
            sys.stderr.write(f"[{sym}] detect_p2 failed: {exc}\n")
            continue
        if c is None:
            continue
        if c.variant not in variants_allowed:
            continue
        out.append(c)
    # Final shortlist sort: tier (A first) → score (high first) → distance (close first).
    tier_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    out.sort(key=lambda c: (tier_rank.get(c.tier, 9), -c.score, c.distance_atr))
    return out


def write_watchlist(candidates: list[P2Candidate], target_date_iso: str) -> tuple[Path, Path]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = STATE_DIR / f"watchlist_ditp_{target_date_iso}.txt"
    json_path = STATE_DIR / f"watchlist_ditp_{target_date_iso}.json"
    # txt: SYM\ttier\tvariant\tresistance — D-tier omitted from the trade-watch
    # list (kept in .json for review). The orchestrator's entry phase reads .txt.
    lines = [
        f"{c.symbol}\t{c.tier}\tP2_{c.variant}\t{c.resistance}"
        for c in candidates if c.tier != "D"
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

    sys.stdout.write(f"# {len(candidates)} candidates, sorted by tier > score > distance:\n")
    sys.stdout.write(f"{'SYM':<6} {'T':<2} {'V':<2} {'score':>5} {'last':>8} "
                     f"{'rng_low':>8} {'rng_high':>8} {'distATR':>8} {'upTail':>7} "
                     f"{'t/m/rM':>8}  cautions\n")
    for c in candidates:
        cau = ",".join(c.cautions) if c.cautions else "-"
        sys.stdout.write(
            f"{c.symbol:<6} {c.tier:<2} {c.variant:<2} {c.score:>5d} "
            f"{c.last_close:>8.2f} {c.resistance_low:>8.2f} {c.resistance:>8.2f} "
            f"{c.distance_atr:>8.2f} {c.upper_tail_ratio:>7.3f} "
            f"{c.resistance_touches:>2d}/{c.resistance_mountains:<1d}/{c.resistance_range_mountains:<2d}  {cau}\n"
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
