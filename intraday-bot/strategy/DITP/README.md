# strategy/DITP/ — DITP family

Source: TBD (see `strategies-reference/DITP.md` in the worktree).
Reference doc: `strategies-reference/DITP.md`.

Skeleton folder. No setups are wired yet. This README + the `__init__.py`
+ `_helpers.py` reserve the family slot so the next step (adding a setup)
just drops a `strategy/DITP/<setup_name>/` subfolder.

## Contents

- `__init__.py` — Family package marker. Light docstring describing the family.
- `_helpers.py` — Family-shared helpers (skeleton). Add shared constants + plan builders here as setups are wired.
- `scanner.py` — DITP P2 Pattern scanner CLI. Reads daily parquet bars from `data/price_history/daily/`, applies the §6 eligibility filters (EMA stack + real-ceiling resistance + signal-candle anatomy + pending-breakout state), classifies sub-variant A / B / C, then runs the §6.5 ranking layer (5-component score + 5 caution flags + tier mapping). Writes `state/watchlist_ditp_<tomorrow>.txt` + `.json`. Source: `strategies-reference/DITP.md` §6 + §6.5.
- `tc_scanner.py` — DITP TC (Trend Continuation) **EOD Day-0** scanner CLI. Walks the most recent `state/watchlist_ditp_*.json`, filters to symbols whose Day-0 daily candle both (a) closed above the P2 candidate's `resistance` (= `range_high`) and (b) printed bullish (close > open AND close in upper half of range), and writes `state/watchlist_tc_<tomorrow>.txt` + `.json`. Source: `strategies-reference/DITP.md` §6 Setup 4 (Phase 1 — premarket validation + entry pipeline still TBD).
- `tc_breakout.py` — **TC (Trend Continuation) detector — SELF-CONTAINED variant** (vs `tc_scanner.py` which requires yesterday's P2 watchlist). Single-file detector for the dashboard's ad-hoc Finviz-universe scanning. Identifies Day 0 / Day +1 / Day +2 / ... continuation patterns from PRICE ACTION ALONE: prior swing high = `max(closes[-prior_swing_lookback : -prior_swing_exclude_recent])`, first close above that swing within `max_days_since_breakout`, today's close still above the broken level, optional anti-extension filter. Trend gate RELAXED (default `require_stack=False`, `require_ema20_above_ema50=True`, `require_close_above_ema20=True`) so early-recovery cases like ORCL (EMA20 marginally below EMA200 on a recovery rally) still qualify. `__version__ = "1.0.0"`. Public API: `detect_tc_breakout(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `TCBreakoutConfig` (prior_swing_lookback=20, prior_swing_exclude_recent=3, max_days_since_breakout=5, max_extension_atr=5.0). Wired to dashboard `POST /scanner/yf_scan?setup=tc_breakout` and bundled into `/scanner/yf_scan_all`. Returns `breakout_level`, `days_since_breakout`, `extension_atr` for UI badges.
- `ema_watch.py` — **EMA watch detector** (pre-confirmation pullback to EMA20 / EMA50 / EMA200). Looser counterpart to `ema_rebound.py`: same trend-stack + close-near-EMA + recent-touch gates, but NO bullish-close requirement and NO bounce-magnitude requirement. Surfaces tickers whose pullback is IN PROGRESS (today's low pierced an EMA but today's bar isn't a confirmation candle yet). `__version__ = "1.0.0"`. Public API: `detect_ema_watch(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `EMAWatchConfig` (lookback_bars=3, touch_tolerance_atr=0.3, max_distance_atr=1.0, require_stack=True, require_above_ema200=True). Returns `watch_state` (`"TESTING"` / `"PULLING_BACK"`), `pierced_today`, `pierce_depth_atr` for UI badges. Wired to dashboard `POST /scanner/yf_scan?setup=ema_watch` and bundled into `/scanner/yf_scan_all`.
- `ema_rebound.py` — **EMA rebound detector** (daily support bounce on EMA20 / EMA50 / EMA200). Single-file detector module; no CLI scanner / watchlist file (consumed only by the dashboard's `POST /scanner/yf_scan?setup=ema_rebound` for ad-hoc filtering of the Finviz universe). Returns the WHICH EMA acted as support + bounce magnitude + **rebound_type** (`"reclaim"` = close above EMA, `"pin_through"` = close 1-5 ticks below EMA, qualified as pin bar) + **prior_tests_count** (how many bars in the prior 60 days ALSO tested the same EMA via trade-through, used as a conviction score boost). Source: user 2026-05-27 EMA-rebound refinements. `__version__ = "1.3.0"`. Public API: `detect_ema_rebound(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `EMARebConfig` (lookback_bars=5, touch_tolerance_atr=0.3, max_distance_atr=1.0, min_bounce_atr=0.3, tick_size=0.01, close_below_tolerance_ticks=5, pin_max_body_ratio=0.40, pin_min_lower_tail_ratio=0.50, **prior_tests_lookback=60**, **prior_tests_score_per=5**, **prior_tests_score_cap=15**, require_stack=True, require_above_ema200=True, require_bullish_close=True, min_close_position=0.5).
- `p1_rebound.py` — **DITP P1 detector** -- rebound off a horizontal SUPPORT level. Trend gate (EMA20>EMA50>EMA200) + bullish reclaim candle + horizontal-support touch via `resources/sr_levels.horizontal_support_np` (immediate-nearest valley below current per user framework reintegration 2026-05-27) + **reaction-magnitude gate** (bounce from touch's low to today's close >= 0.3 * ATR — a real visible bounce is required). Single-file detector consumed by the dashboard's `POST /scanner/yf_scan?setup=p1_rebound`. Source: user framing 2026-05-27. `__version__ = "1.2.0"`. Public API: `detect_p1_rebound(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `P1RebConfig`.
- `p3_retest.py` — **DITP P3 detector** -- retest of a broken resistance level (polarity flip; resistance → support). Trend gate + bullish reclaim candle + broken-R candidate from `resources/sr_levels.find_broken_resistance_below` v1.5.0 (independent of `horizontal_resistance_np`, HIGHEST broken peak below current) + staleness window (breakout 3-252 days ago) + **reaction-magnitude gate** + P2-zone vs P3-zone midpoint discriminator (v1.5.0) + **upper-tail ATR-magnitude floor** (v1.6.0). `__version__ = "1.6.0"`. Public API: `detect_p3_retest(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `P3RetestConfig` (mountain_min_age_bars=5, mountain_pullback_atr=0.5, breakout_min_age=3, breakout_max_age=252, touch_lookback_bars=5, max_distance_atr=1.5 (v1.6.0 ↑ from 1.0), max_upper_tail_ratio=0.15, **upper_tail_atr_min=0.25** (v1.6.0 new), require_stack=True).
- `p1a_rejection.py` — **DITP P1a detector** (SHORT side). Bearish rejection at a horizontal resistance — *"a failed P2 setup will be P1a setup"* (user 2026-05-27). Uses `patterns.horizontal_resistance_np` for the level (same as P2). Signal: bearish close, close in lower half of bar range, upper tail ≥ 30% of range, reaction magnitude `(high - close) / atr ≥ 0.3`, today's high touched the resistance. **Downtrend stack required as of v1.1.0** (2026-05-29 user rule: short setups must be on EMA20<50<200 downtrend chart). `__version__ = "1.1.0"`. Public API: `detect_p1a_rejection(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `P1aRejectConfig` (require_stack=True new).
- `p2a_breakdown.py` — **DITP P2a detector** (SHORT side). Pending breakdown below horizontal support — *"a break below support will be a P2a setup"* (user 2026-05-27). Mirror of P2: trend gate `EMA20 < EMA50 < EMA200 + close < EMA200` (downtrend stack), uses `sr_levels.horizontal_support_np` for the level (immediate-nearest most-recent valley below). Signal: bearish close, close in lower half, lower-tail ≤ 15% (no rejection wick), close still ABOVE support (pending), close within 1.5×ATR of support. Recent-breakdown-rejection check: if support was breached within 2 days, symbol is past P2a (graduated to P3a). `__version__ = "1.0.0"`. Public API: `detect_p2a_breakdown(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `P2aBreakdownConfig`.
- `p3a_retest.py` — **DITP P3a detector** (SHORT side). Retest of broken support as resistance — *"a successful break below (P2a) which support become a resistance after the break below and price action come back to test the Support turn resistance is P3a setup"* (user 2026-05-27). Mirror of P3: downtrend EMA stack, uses new `sr_levels.find_broken_support_above` helper (immediate-nearest broken-S above current = lowest mountain valley above current that price has clearly broken below by > 3 ticks), staleness window (breakdown 3-45 days ago), bearish reclaim-from-below candle, retest-touch + reaction-magnitude gates. `__version__ = "1.0.0"`. Public API: `detect_p3a_retest(symbol, cfg)` / `scan_universe(symbols, cfg)`. Config: `P3aRetestConfig`.
- `ditp_p2/` — Setup 1 (P2 Pattern). See its own README.
- `ditp_tc/` — Setup 4 (TC — Trend Continuation). See its own README.
- `_decision_engine.py` — Family-shared decision math (entry/stop/target/tradeability for the live entry pipeline). Currently used by `ditp_p2/backtest_adapter.py`.

## Status of the DITP setups

| Setup | Trigger | Status |
|---|---|---|
| 1 (P2 — A/B/C variants) | Day of breakout (intraday tape watch) | scaffolded — `ditp_p2/` v0.1.0 watch-only + dashboard scan `p2_pattern` (via `scanner.py`) |
| 2 (P1) | Day of support reclaim / rebound | dashboard scan `p1_rebound.py` v1.2.0 — filter only |
| 3 (P3 — retest) | Day of polarity-flip retest reclaim | dashboard scan `p3_retest.py` v1.3.0 — filter only |
| 4 (TC — Trend Continuation) | Day +1 / Day +2 after a qualifying breakout / rebound | scaffolded — `ditp_tc/` v0.1.0 watch-only, EOD Day-0 scanner in `tc_scanner.py`; Phase 2 (premarket) + Phase 3 (entry) TBD |
| **P1a (SHORT)** | Day of rejection at resistance (failed P2) | dashboard scan `p1a_rejection.py` v1.1.0 — filter only (downtrend stack required as of 2026-05-29) |
| **P2a (SHORT)** | Day of pending breakdown of support | dashboard scan `p2a_breakdown.py` v1.0.0 — filter only |
| **P3a (SHORT)** | Day of polarity-flip retest rejection (broken-S now R) | dashboard scan `p3a_retest.py` v1.0.0 — filter only |

## Convention reminders (from CLAUDE.md)

- Strategy code MUST cite the source: `"""Source: strategies-reference/DITP.md §6 Setup N"""`.
- Never blend DITP rules into a GUNS setup file (or vice versa). Hybrid setups annotate each rule's origin.
- Every rule edit bumps `__version__` in the setup's `impl.py` and adds a dated entry to that setup's README changelog (which IS the version history — there's no separate `changelog.md` per CLAUDE.md).

## Changelog

### 2026-05-29 — All 9 detectors: ctx-aware (efficiency Pass 2 #1)

Dashboard audit identified that every DITP detector independently re-loaded bars + re-allocated 4 numpy arrays + recomputed EMA20/EMA50/EMA200 + ATR(14) on every call. At 30 symbols × 9 detectors that's 270 redundant numpy passes per scan. Fix: new `resources/symbol_ctx.py` builds the prelude ONCE per symbol, detectors accept an optional `ctx` kwarg.

Refactor summary:

| Detector | Before | After |
|---|---|---|
| `ema_watch.py` | v1.0.0 | v1.0.1 (ctx-aware) |
| `ema_rebound.py` | v1.4.0 | v1.4.1 (ctx-aware) |
| `tc_breakout.py` | v1.0.0 | v1.0.1 (ctx-aware) |
| `p1_rebound.py` | v1.2.0 | v1.2.1 (ctx-aware) |
| `p3_retest.py` | v1.6.0 | v1.6.1 (ctx-aware) |
| `p1a_rejection.py` | v1.1.0 | v1.1.1 (ctx-aware) |
| `p2a_breakdown.py` | v1.0.0 | v1.0.1 (ctx-aware) |
| `p3a_retest.py` | v1.0.0 | v1.0.1 (ctx-aware) |
| `scanner.py` (P2) | (no version) | ctx-aware via `detect_p2(symbol, cfg, as_of_date=None, ctx=None)` |

All detectors accept `ctx: SymbolContext | None = None` as an optional last kwarg. When None (CLI / backtest path), the detector calls `build_context(symbol)` itself — same behaviour as before. When provided (dashboard path), the detector reuses `ctx.closes`, `ctx.ema20`, etc. directly. PATCH-level bumps because behaviour is unchanged; the rule set is identical.

**Backward compatibility**: every `scan_universe()` wrapper, every CLI entry point, every backtest call site keeps working without modification. The ctx kwarg is purely additive.

End-to-end: dashboard scan portion dropped from 0.3-0.4s to **0.1s** (30-symbol Scanner 1 universe). See `dashboard/README.md` for the full audit + numbers.

### 2026-05-29 — Detector relaxations: TSLA/MRVL upper-tail floor, MSFT stack, MRVL distance, BE short-gate

User batch 2026-05-29 (10 tickers in two waves): *"AMD is a TC candidate / IONQ is TC / TSLA is P2 / MSFT is P2 / MRVL is P3 retesting $190 / OBIO is P2 / SMCI is TC / RGTI is P2 / RDW is TC"* + *"why BE is labelled as P1a, perhaps we have to set a fixed rule. the shorting strategy P1a, P2a, P3a has to be based on downtrend chart based on EMA20, 50, 200 for shorting"*.

**Four coordinated changes:**

**1. `p1a_rejection.py` v1.1.0 — require downtrend stack (BE fix)**

User rule: short setups must be on a downtrend chart. P1a v1.0.0 fired on ANY trend because the bar anatomy was treated as the dominant signal. BE 2026-05-28 has a clean uptrend stack (EMA20 $276 > EMA50 $237 > EMA200 $150) and a bearish rejection bar at $310 R — v1.0.0 wrongly tagged this as P1a. Per the user's framework, bearish rejection at R in an UPTREND = P2 candidate (pending continuation), not a P1a short. Added `require_stack: bool = True` (matches P2a / P3a which already had this gate). BE now correctly fires only `ditp` (P2), no P1a false positive.

**2. `scanner.py` (P2) — three gate relaxations**

a. **`upper_tail_atr_min: float = 0.20`** — TSLA 5/28 had range $7.66 (0.51 ATR), upper tail $1.86 (0.12 ATR) = 24% of range. The 15% percentage gate rejected this clean P2 even though the absolute wick is tiny — the ratio was inflated by the small denominator on tight-range consolidation bars. Fix: skip the percentage check when upper tail < `upper_tail_atr_min * ATR`. The wick is absolutely small so ratio doesn't matter.

b. **`require_full_stack: bool = False` (default)** — MSFT 5/28 is in early-recovery from a downtrend: EMA20 ($415) > EMA50 ($411) but EMA50 < EMA200 ($437). Strict full stack rejected MSFT despite close $426.99 sitting $2.93 below R $429.92 (textbook P2 pending breakout). Relaxed default requires `close > EMA20 > EMA50` only; full stack is now a scoring boost in `score_candidate`, not a hard gate. Strict mode opt-in via `require_full_stack=True` for the production bot path. Mirrors `tc_breakout.py`'s relaxed-stack default (the same early-recovery case that caught ORCL).

c. **`max_upper_tail_ratio_rejected: float = 0.50`** (already landed earlier today for QCOM) — relaxed upper-tail tolerance specifically for rejected-breakout candidates.

**3. `p3_retest.py` v1.6.0 — upper-tail floor + distance bump (MRVL)**

a. **`upper_tail_atr_min: float = 0.25`** — MRVL 5/28 had range $12.69 (1.0 ATR), upper tail $2.56 (0.21 ATR) = 20% of range. Same problem as TSLA: 15% ratio inflated by tight bar. Same fix: skip percentage gate when upper tail < `upper_tail_atr_min * ATR`.

b. **`max_distance_atr: 1.0 -> 1.5`** — MRVL 5/28 close ($204.83) is 1.02 ATR above broken-R $192.15 (9 days ago). The 1.0 cap rejected this even though the touch test passed (today's low $194.70 ≤ $192.15 + 0.3 ATR). A strong-bounce retest with the close 1+ ATR above the level is a valid P3 — the level held, price ran. Bumped to 1.5 ATR to match `scanner.py` P2's distance gate.

**4. Verification across user-named tickers (yfinance 2y, 2026-05-28):**

Earlier batch (10 tickers from prior message):

| Symbol | User said | Now fires |
|---|---|---|
| GLW | EMA20 | ema_watch ✓ |
| SNDK | TC + P3 | tc_breakout ✓ (P3 missing — no broken-R below, recent uptrend hasn't formed required mountain structure) |
| APLD | TC | p3_retest + tc_breakout ✓ (bonus P3 from $42 broken 83d ago) |
| QCOM | P2 | **ditp** ✓ + tc_breakout |
| LUNR | P2 | tc_breakout ✓ (making new highs, no R above) |
| ORCL | TC + P2 | **ditp** + tc_breakout ✓ (relaxed stack catches MSFT-like case) |

New batch (10 tickers from this message):

| Symbol | User said | Now fires |
|---|---|---|
| AMD | TC | tc_breakout ✓ |
| IONQ | TC | tc_breakout ✓ |
| TSLA | P2 | **ditp** ✓ + p3_retest (bonus) |
| MSFT | P2 | **ditp** ✓ |
| MRVL | P3 ($190) | **p3_retest** ✓ + tc_breakout |
| OBIO | P2 | (none) — close $3.98 < EMA20 $4.02; doesn't fit any framework setup |
| SMCI | TC | tc_breakout ✓ |
| RGTI | P2 | **ditp** ✓ + tc_breakout |
| RDW | TC | tc_breakout ✓ |
| BE | (was wrongly P1a) | **ditp** ✓ — P1a no longer fires (downtrend gate) |

Regression checks (previously-validated cases): USAR fires `ditp` only (no P3 false positive — v1.5.0 midpoint discriminator still holding). ASTS migrates to `p3_retest + tc_breakout` (P2 graduated). LYV fires `ditp + ema_rebound + ema_watch` (multi-fire overlapping setups). NVDA fires `p3_retest + ema_watch`. AAOI fires `ema_watch` only (strict EMA rebound still correctly skips today's bearish bar).

**OBIO note**: doesn't fit P2 because close is BELOW EMA20. The framework's P2 definition requires the stock to be in a short-term uptrend approaching overhead R. OBIO is in early-stage consolidation / downtrend. If the user wants a wider "anywhere near a level" surface, that would need a separate pre-confirmation detector (similar to `ema_watch.py`); ask if needed.

Per CLAUDE.md normalization rule: all new gates ATR-relative. Same code applies to AMD's $30 ATR and OBIO's $0.20 ATR.

### 2026-05-29 — `tc_breakout.py` v1.0.0: standalone TC detector (SNDK, APLD, ORCL, LUNR)

User batch 2026-05-29: *"SNDK should be trend continuation and P3 candidate / APLD should be a trend continuation candidate / ORCL is a Trend continuation candidate, P2 resistance broken / LUNR is a P2 candidate"*.

The existing `tc_scanner.py` is the PIPELINE-STRICT TC scanner — it requires yesterday's P2 watchlist to walk through. That's the right architecture for the live trading bot's Day-0 EOD scan. But the dashboard's ad-hoc Finviz-universe scanning has no yesterday-P2 watchlist (and shouldn't — the user picks fresh signal-based universes each session). So a separate, standalone TC detector was needed.

**`tc_breakout.py`** — self-contained, identifies the "recent breakout + still above" pattern from price action alone:
- Prior swing = `max(closes[-prior_swing_lookback : -prior_swing_exclude_recent])` — the close-based swing high of the pre-breakout window
- First breakout = first close above the prior swing within the recent action
- Filters: today's close still above the broken level, breakout within `max_days_since_breakout` (default 5), trend gates relaxed
- Returns `breakout_level`, `days_since_breakout`, `extension_atr` for UI badges

**Verification across user-named tickers (yfinance 2y daily, 2026-05-28):**

| Symbol | User said | Day | Breakout level | Close | Ext (ATR) | Score |
|---|---|---|---|---|---|---|
| **SNDK** | TC + P3 | +2 | $1562.34 | $1641.64 | +0.71 | 34 |
| **APLD** | TC | +1 | $48.02 | $49.65 | +0.40 | 32 |
| **ORCL** | TC | 0 | $195.95 | $203.70 | +0.90 | 30 |
| **LUNR** | P2 | +1 | $38.26 | $45.70 | +1.65 | 37 |
| **QCOM** | P2 | +2 (bonus) | $238.16 | $243.29 | +0.31 | 29 |

LUNR was called "P2" by the user but it's actually making new highs (no overhead R in the 252d lookback), so the strict P2 detector correctly returns None. The pattern the user is observing — recent strong breakout, still trending — is TC, not P2. The user's "P2" label is the wider colloquial sense ("pending more upside"); the framework's specific terminology pins it as TC.

SNDK called "P3" by the user — but `find_broken_resistance_below` returns no mountains (the recent move was too fast for valid pullback validation). P3 doesn't fire. The TC label IS firing, which is the dominant signal.

ORCL's EMA stack is technically BROKEN (EMA20 $187.43 marginally below EMA200 $187.72) — this is an early-recovery case. The relaxed `require_stack=False` default keeps it in.

NVDA, AAOI, GLW correctly DON'T fire TC (no recent breakout in window) — no false positives.

Per CLAUDE.md normalization rule: all thresholds ATR-relative. Same code applies to APLD's $4 ATR and SNDK's $112 ATR.

### 2026-05-29 — `scanner.py`: ATR-relative rejection override for P2 breach grace (QCOM fix)

User question 2026-05-29: *"QCOM is a P2 candidate 26.5.2026 is a outlier, it is retesting the 245 resistance"*.

QCOM 2026-05-26 closed $248.82 > R $247.90 (breach). 2026-05-27 closed $233.40 ($14.50 below R = -0.88 ATR). 2026-05-28 (today) closed $243.29 ($4.61 below R = -0.28 ATR). Per the user's framework, the 5/26 breach was an outlier; the strong 5/27 rejection nullified it and price is now retesting R from below = textbook P2.

The pre-fix scanner rejected QCOM because `days_since_breach (2) <= breach_rejection_grace_days (2)` — the grace window assumes we can't yet tell if the breach was real or rejected. But QCOM's 5/27 rejection IS visible — the close pulled back $14.50 below R, more than 0.88 ATR. The grace shouldn't apply when the market has already provided a clear answer.

**Fix:** new `breach_rejection_atr: float = 0.3` config. After finding `last_breach_idx`, scan post-breach closes: if any closed `<= R - 0.3 * ATR`, set `is_rejected_breakout=True` and bypass the grace return. Plus a relaxed `max_upper_tail_ratio_rejected: float = 0.50` for these candidates (vs 0.15 for clean pending P2) — QCOM's 34% upper tail reflects today's intraday re-probe of R, exactly the signal the user wants surfaced.

**Verification:**
| Symbol | Breach idx | days_since | Post-breach min close vs R | rejection_seen | Result |
|---|---|---|---|---|---|
| **QCOM** | 5/26 ($248.82) | 2 | 5/27 close $233.40 = -0.88 ATR | ✓ | P2 variant=U, tier=C, score=48 |

No version bump on `scanner.py` (additive config + tightened-in-one-direction grace logic; plan-dict shape unchanged).

### 2026-05-29 — `ema_watch.py` v1.0.0: new pre-confirmation surface (AAOI)

User note 2026-05-29: *"AAOI is also EMA20 candidate"*.

AAOI today (2026-05-28): O=$181.25, H=$183.24, L=$166.69, **C=$169.02**, EMA20=$173.05. The bar is unambiguously bearish — close < open by $12.23, body = 74% of range, close in lower 14% of range. The strict `ema_rebound` detector (v1.4.0) correctly skips this: neither the bullish-close path nor the pin-bar fallback fires on a bar with that anatomy. But the *setup* is clearly forming — the trend stack is intact (20>50>200), close is only -0.19 ATR from EMA20, and today's low pierced EMA20 by 0.30 ATR. Tomorrow's bar could easily be the confirmation.

The strict rebound detector is doing the right thing — it shouldn't loosen its anatomy gates and start firing on bearish-bodied bars (that would re-introduce false positives in the universe). The right fix is a *parallel* surface that surfaces these in-progress pullbacks without claiming confirmation.

**`ema_watch.py`** is that surface. Same trend gates + close-near-EMA + recent-touch logic, but:

- NO bullish-close requirement
- NO bounce-magnitude requirement
- Lookback window shorter (3 days vs 5) — only the freshest pullbacks
- Returns `watch_state` (`"TESTING"` when close within tick tolerance of EMA, `"PULLING_BACK"` otherwise) and `pierced_today` so the UI badge can read e.g. "EMA20w / pierced 0.30xATR today / testing"

**Verification across user-named tickers (yfinance 1y/1d, 2026-05-28):**

| Symbol | EMA anchor | Close vs EMA | Pierced today | watch_state | Score | Notes |
|---|---|---|---|---|---|---|
| AAOI | EMA20 | -0.19 ATR | yes (0.30 ATR depth) | TESTING | 30 | originally flagged |
| NVDA | EMA20 | -0.03 ATR | yes (0.45 ATR depth) | TESTING | 30 | also hits strict EMA rebound (pin_through) — overlapping setups |

Per CLAUDE.md "no absolute thresholds" rule: every gate is ATR-relative (touch tolerance, distance cap, close-below tolerance), so the same code applies to NVDA's $7.17 ATR and AAOI's $21.30 ATR without retuning.

Wired into `dashboard/server.py`'s `/scanner/yf_scan?setup=ema_watch` AND bundled into `/scanner/yf_scan_all` (the "Run All" path), and registered as a new SETUPS entry in `dashboard/web/index.html` (badge: `EMA20w` / `EMA50w` / `EMA200w` — the trailing "w" disambiguates from confirmed rebound badges).

### 2026-05-28 — `p3_retest.py` v1.5.0: P2-zone vs P3-zone midpoint discriminator (USAR fix)

User question 2026-05-28: *"for USAR why you indicate it as P3 setup?"*

USAR today: O=$26.45, H=$28.59, L=$25.80, **C=$28.19**.
- R above (immediate-nearest mountain): **$28.69** (10 days ago, just $0.50 above close)
- Broken-R polarity flip: **$26.36** (79 days ago, $1.83 below close)

Both levels pass individual P3 gates — broken_R candidate exists, touch + bullish close + close-above-level + 14.3% upper tail (just under the 15% filter) + 0.99-ATR bounce. The detector tagged USAR as P3 with score 36. But per the user's framework, **the level the price action is actually testing is $28.69 (P2 territory), NOT the 79-day-old polarity flip $26.36** — the same exact correction USAR triggered back in v1.0.0 (where $25.95 was the false-positive flip).

**Fix**: new gate in `detect_p3_retest`. Compute R-above via `patterns.horizontal_resistance_np` up front; if it exists, the polarity-flip candidate is only accepted when today's close is **closer to broken-R than to R-above** (i.e., below the midpoint of the two levels). USAR's close $28.19 > midpoint $27.525 → P2 zone → rejected.

**Verification across user's named cases:**

| Symbol | R above | Broken-R | Midpoint | Close | Zone | P3 fires? |
|---|---|---|---|---|---|---|
| **USAR today** | $28.69 (0.21 ATR away) | $26.36 (0.76 ATR away) | $27.525 | $28.19 | P2 (above midpoint) | ✓ now rejected |
| **NVDA** (yesterday) | $236.54 (3.24 ATR away) | $212.19 (0.06 ATR away) | $224.37 | $212.60 | P3 (below midpoint) | ✓ still fires |
| **AAOI** | $233.67 (2.59 ATR away) | $173.41 (0.21 ATR away) | $203.54 | $178.00 | P3 (below midpoint) | rejected by 46%-upper-tail filter (separate gate) |

**Edge case** — when `R_above` is `None` (price is at the top of the structure, no overhead mountain), the midpoint check has no anchor and is skipped. The other gates still apply.

Per CLAUDE.md bump rule: MINOR (new gating filter; plan-dict shape unchanged).

### 2026-05-28 — DITP P2 scanner: close-based "already broken" gate + variant `"U"` fallback (ASTS fix)

User question 2026-05-28: *"Why ASTS is not under the radar for P2?"* ASTS today (O=$124, **H=$131.20**, L=$118.04, **C=$129.60**) is at the textbook P2 setup — close at R=$129.89 with no confirmed close-above breakout. But two gates blocked it:

1. **`highs[-1] > resistance` gate**: the old logic rejected if today's HIGH exceeded R, even when close came back below. For ASTS, H=$131.20 > $129.89 → rejected, despite C=$129.60 = clean rejection-at-resistance = textbook P2 pending. Fix: switched to **`closes[-1] > resistance`** — only confirmed close-above breakouts graduate the symbol out of P2 (to P3 retest watch). Intraday wick-throughs that close back below are still pending P2.

2. **Variant `None` rejection**: ASTS's bar didn't fit A/B/C anatomy strict-ly (body 43% between markup's 60% threshold and rectangle's flatness — no fit). Old logic rejected `variant=None`. Fix: introduce **variant `"U"` (Unclassified)** as a fallback — still pending P2 territory but anatomy doesn't match A/B/C. Score's variant bonus already maps unknown variants to `5` so the existing scoring is unchanged. `both_slopes_falling` (price pulling AWAY from R) is the only `variant=None` case that still rejects.

After fix: ASTS fires as **P2 variant=U, tier=C, confluence=1, score=54**. Frontend tooltip will read `"tier C / variant U"`.

**No version bump** on `scanner.py` since the watchlist-generation contract is unchanged (additive variant + gate semantics tightened in one direction). Watchlist `.txt` rows for variant=U candidates can be filtered downstream if needed.

### 2026-05-28 — Overlapping setups: NVDA hits BOTH P3 + EMA20 (v1.4.0 / v1.5.0 framework)

User teaching 2026-05-28: *"the pattern for each ticker can be overlapped... NVDA, I would say it is a rebound of EMA20 trade through as the bullish hammer is formed. If we look back on 1y1d chart, the mountain top form on 29.10.2025 at $212.19 and the recent mountain top formed on 27.4.2026 constitute a horizontal support where a line can be drawn in that range, now the price action is retesting the support. So it is p3 setup."*

NVDA today: O=$214.12, H=$214.15, L=$208.78, C=$212.60 — bullish hammer (body 28%, lower wick 71%, upper wick 0.6%). The framework was failing to fire EITHER P3 or EMA20 on NVDA. Three coordinated changes fix this without re-introducing the AAOI / USAR false positives that earlier versions had:

**1. `ema_rebound.py` v1.4.0** — accept bullish-OR-pin-bar + ATR-relative close-below tolerance
- `require_bullish_close` relaxed: a clear pin bar / hammer (body ≤ 40% of range, lower wick ≥ 50% of range) is a bullish rebound signal even with close < open by a small amount. NVDA's body is 28% bearish but the hammer shape is unmistakable.
- New `close_below_tolerance_atr=0.30`. Effective close-below-EMA tolerance is `max(tick-based, ATR-based)`. For NVDA (ATR=$7.39): 5 ticks=$0.05 vs 0.3·ATR=$2.22 → uses $2.22; close was $1.85 below EMA20 → passes.
- Result: NVDA fires EMA20 with `rebound_type=pin_through, prior_tests=7, score=46`.

**2. `p3_retest.py` v1.4.0** — upper-tail rejection filter + pin-bar acceptance + extended staleness window
- New `max_upper_tail_ratio: float = 0.15` (mirrors P2). Differentiates clean P3 reclaim from a doji / rejection bar. AAOI 2026-05-27 had 46% upper tail = rejection at $187 high, NOT a clean retest of $173.41 — now correctly rejected. NVDA has 1% upper tail → passes.
- `require_bullish_close` relaxed same way as EMA rebound (pin-bar accepted).
- `breakout_max_age` extended 45 → **252 days** (full lookback). NVDA's $212.19 is 143 days old — was outside the prior 45-day window. The detector's touch / bounce / upper-tail gates ensure only actively-retested levels qualify; the breakout age itself doesn't need a tight upper bound.

**3. `resources/sr_levels.py` v1.5.0** — revert single-most-recent-peak coupling
- v1.4.0 had coupled `horizontal_resistance_np` and `find_broken_resistance_below` so only ONE fired per ticker (based on the side of the single most-recent peak in the lookback). For NVDA this blocked the $212.19 polarity flip because $236.54 (8d ago, above current) was the most-recent peak.
- v1.5.0: the two finders are independent again. `horizontal_resistance_np` returns the most-recent peak ABOVE current ($236.54 for NVDA). `find_broken_resistance_below` returns the HIGHEST broken peak BELOW current ($212.19 for NVDA). Both can fire — that's the "overlapping patterns" the user wants. Same change applied symmetrically to `horizontal_support_np` and `find_broken_support_above` (for P1 vs P3a).

**Verification (4 test cases):**

| Symbol | Hits | Notes |
|---|---|---|
| **NVDA** | P3 ($212.19, 143d) + EMA20 (pin_through) | ✓ overlapping patterns as user described |
| AAOI | EMA20 only | ✓ P3 correctly rejected (upper tail 46% > 15%) |
| USAR | (none) | ✓ correctly excluded |
| GOOGL | (none) | Today's bar weak (close in lower half) |

Per CLAUDE.md bump rule: MINOR (new gate filters added; plan-dict shape additively extended with `upper_tail_ratio`, `is_pin_bar`).

### 2026-05-27 — `ema_rebound.py` v1.3.0: prior trade-through tests = conviction score boost

User refinement 2026-05-27: *"the chart will give you more conviction if it previously tested the same EMAs by traded through. In the case of NVDA, it happened 4.5.2026 and 5.5.2026."* A current bullish hammer at an EMA is a setup; the same EMA having been tested via similar trade-through patterns BEFORE makes the setup stronger.

**New diagnostic + scoring**:
- After the current setup is identified, scan back `prior_tests_lookback` bars (default 60 trading days ≈ 3 months) from the current touch index.
- For each bar in that window, check whether it ALSO traded through the same EMA with rebound anatomy (helper `_is_prior_trade_through`):
  - Low pierced the EMA (`low < ema_at_that_time`)
  - Close came back within tick tolerance (`close >= ema - 5*tick_size`)
  - Bullish close (`close > open`)
  - Close in upper half of bar range
- Count = `prior_tests_count` field on the candidate.
- Score bonus: `min(15, prior_tests_count × 5)` = up to +15 points for 3+ prior tests. Surfaced as `prior_tests_bonus` field for inspection.

**Each prior bar counts independently.** Consecutive same-week tests (NVDA's 4.5 + 5.5 case) both contribute to the count — that's the user's intent (multiple distinct tests of the level).

**Smoke-tested** (505-symbol universe): 27 of 29 candidates have ≥1 prior trade-through at the same EMA. Top picks:
- BKH (EMA50, 11 prior tests, bonus +15, score 54)
- ARI (EMA20, 14 prior tests, bonus +15, score 45)
- BKU / CALY (EMA50, 9-10 prior tests each)
- CVI / ACA (EMA50, 3 prior tests each, score 58 / 55 — top scorers thanks to combined bounce + prior-tests)

**NVDA-specific note**: today's NVDA bar per yfinance data is technically bearish (O=$214.12, C=$212.60 — close < open by $1.52) so it doesn't qualify as a bullish hammer in the framework. The user's chart source may differ from yfinance — if the bar IS bullish in their data, the prior-tests counter would catch the 4.5 / 5.5 tests if they pass the same trade-through anatomy.

### 2026-05-27 — `ema_rebound.py` v1.2.0: accept bullish pin bar that closes 1-5 ticks BELOW the EMA

User refinement 2026-05-27: *"bounce off EMA does not mean that it has to close above the EMAs. If the last candle form bullish pin bar below the EMA 2-5 ticks below, it can still be qualified as rebound by way of trade through EMAs."*

The v1.1.0 close gate required `close > ema_now` strictly — a bullish pin bar that pierced the EMA and closed 3 ticks below (e.g., $99.97 close vs $100.00 EMA) was REJECTED even though the rebound character was clearly present. v1.2.0 admits this case under tighter anatomy.

**Two accepted rebound states** (mutually exclusive `rebound_type`):
- `"reclaim"` (v1.1.0 behaviour) — `close >= EMA`. No additional shape check; the bullish close + close-in-upper-half + bounce-magnitude gates already ensure clean reclaim.
- `"pin_through"` (NEW v1.2.0) — `EMA − tolerance ≤ close < EMA` AND the bar is a clear bullish pin bar:
  - `body / range ≤ pin_max_body_ratio` (default 0.40 — body is small)
  - `lower_wick / range ≥ pin_min_lower_tail_ratio` (default 0.50 — wick is long)

The pin-bar anatomy gate is required ONLY for the below-EMA case. A plain bullish bar that closed below the EMA without the pin-bar shape is still rejected (it's a *failed* reclaim, not a trade-through rebound).

**Tolerance is absolute, tick-based** (mirrors the cluster-tolerance convention from sr_levels): `close_below_tolerance_ticks × tick_size` = 5 × $0.01 = **$0.05** by default. Stays tight across stock prices ($30 stock or $400 stock), unlike a percentage which would scale with price.

**Trend gate `require_above_ema200`** relaxed by the same tick tolerance — a pin bar that pierced EMA200 still qualifies.

**New candidate dict fields**:
- `rebound_type` — `"reclaim"` or `"pin_through"`
- `is_pin_bar` — bool, the pin-bar anatomy result for today's bar

Per CLAUDE.md bump rule: MINOR (gate semantics extended + new config fields, plan-dict shape additive).

**Smoke-tested** (505-symbol universe): 29 candidates, all `reclaim` today (no pin-through cases in today's snapshot — rare scenario by nature). Synthetic verification: a $100 stock with bar O=$99.90, H=$100.10, L=$98.50, C=$99.97 (pin shape: 4% body, 87% lower wick) at EMA=$100.00 correctly qualifies as `pin_through`.

### 2026-05-27 — `ema_rebound.py` v1.1.0: reaction-magnitude gate (pin-bar / hammer + confirmation candle)

User refinement 2026-05-27: *"sometimes price action will trade through the EMAs and form a rebound with pin bar or a bullish hammer, those are considered rebound on EMA, we have to find the confirmation candle i.e. the candle formation of a bullish candle."*

The v1.0.0 detector caught most rebound cases (touch + bullish close + reclaim) but didn't measure the bounce STRENGTH from the deepest probe of the EMA. The new gate mirrors what P1/P3 v1.1.0 added: `bounce_magnitude_atr = (last_close - min(lows[touched_idx:])) / atr` must clear `min_bounce_atr` (default 0.3 ATR).

**Why this matters for the pin-bar / hammer case**:
- The touch window detects the day price PIERCED the EMA (low went well below)
- The bounce gate measures cumulative reaction up to today's close — captures both same-bar rebounds (today IS the hammer, bounce within the bar) and prior-day pin + today confirmation candle
- Hammers naturally pass: small body near the high, long lower wick = close_position ≈ 0.7-0.9 and bounce_magnitude ≈ 0.5-1.0 ATR

**Other changes**:
- Score gains a `reaction` component (`min(10, bounce_magnitude_atr * 10)`) so stronger bounces rank higher.
- Candidate dict adds `bounce_magnitude_atr` field — surfaced in the dashboard EMA-badge tooltip alongside the anchor name.
- Touch logic now explicitly documents that the tolerance allows BOTH small overshoots above the EMA AND deep pierces below (the bounce gate is the quality filter, not the touch tolerance).

**Smoke-tested** (parquet universe, 371 symbols):
- 26 candidates; bounce magnitudes 0.39–1.20 ATR
- Top: CVI (EMA50, 0.83 ATR, score 43), AME (EMA50, 0.76, 42), ACA (EMA50, 0.78, 40), AROC (EMA50 touch 1d ago + confirmation today, 1.11 ATR), COHR (EMA20, 1.00 ATR)
- AROC is a textbook pin+confirmation: touch 1 day ago, today's bullish candle adds the 1.11-ATR cumulative bounce.

Per CLAUDE.md bump rule: MINOR (new gating filter + plan-dict additively extended with `bounce_magnitude_atr`).

### 2026-05-27 — `sr_levels.py` v1.4.0: single-most-recent-peak rule fixes AAOI mis-P3

User correction 2026-05-27: AAOI was being tagged P3 by the framework when it shouldn't be. The most-recent mountain on AAOI's 1Y chart is $233.67 (May 13, 9d ago, above current $182). My algorithm picked $173.41 (Apr 21, 25d ago, below current) as a P3 polarity-flip candidate — but per the user, that's a stale level the market has moved past.

Underlying fix in `resources/sr_levels.py` v1.4.0: only the **single most-recent mountain peak** in the lookback is the active level. Its side relative to current determines whether `horizontal_resistance_np` (P2 territory) OR `find_broken_resistance_below` (P3 polarity-flip) fires — never both. Same coupling for valleys (P1 vs P3a). See `resources/README.md` for the full rationale.

**P1 / P3 / P3a detector impact** (no code changes — picked up via the call chain):
- AAOI: previously P3 (flip $173.41) → no longer a candidate ✓
- Universe-wide: P1 16→6, P3 19→14, P3a 11→7 candidates (tighter, fewer false positives).

### 2026-05-27 — P1a / P2a / P3a v1.0.0: short-side mirror framework

User teaching 2026-05-27: *"P1 and P3 inverse will be P1a and P3a -- which is shorting setup. A failed P2 setup will be P1a setup. A break below support will be a P2a setup and a successful break below (P2a) which support become a resistance after the break below and price action come back to rest the Support turn resistance is P3a setup."*

The short-side framework is the symmetric mirror of P1/P2/P3:

| Long | Short | Level | Position | Signal |
|---|---|---|---|---|
| P1 | **P1a** | Mountain top (resistance) | Above current | Bearish rejection (failed P2) |
| P2 | **P2a** | Mountain valley (support) | Below current | Bearish breakdown approach |
| P3 | **P3a** | Broken mountain valley | Above current | Bearish retest (S → R polarity flip) |

**Three new detector modules** following the same shape as the long-side counterparts (single-file, `@dataclass` config, `detect_*` returning dict-or-None, `scan_universe` returning sorted list):
- `p1a_rejection.py` — bearish rejection at horizontal resistance. No downtrend gate (allows early-reversal shorts on uptrends).
- `p2a_breakdown.py` — pending breakdown of horizontal support. Mirrors P2's strict candle anatomy (no lower-tail rejection, close in lower half).
- `p3a_retest.py` — retest of broken-S now acting as R. Uses new `resources/sr_levels.find_broken_support_above` helper.

**`resources/sr_levels.py` bumped to v1.3.0**: new function `find_broken_support_above` mirrors `find_broken_resistance_below` — returns the immediate-nearest broken mountain valley above current price (= lowest mountain valley above current that price has clearly broken below by > 3 ticks), or empty list.

**Dashboard wiring**:
- `_VALID_SETUPS` extended with `p1a_rejection`, `p2a_breakdown`, `p3a_retest`.
- `/scanner/yf_scan` dispatch branches added for all three.
- `SETUPS` registry in `web/index.html` adds three entries with `shortLabel` `P1a` / `P2a` / `P3a` and red-tone badge styling via `.tag.strategy.short` so bullish vs bearish setups are visually distinct in the watchlist.

**Smoke-tested** on the 252-symbol parquet universe:
- P1a: 28 candidates. Top: APP at $498.69 rejecting $512.69 (1.32-ATR fall from high, 55% upper tail, score 43).
- P2a: 9 candidates. Top: AWK at $123.85 about to break $123.55 support (3% lower tail = clean breakdown anatomy, score 37).
- P3a: 11 candidates. Top: BBWI at $17.73 rejected at broken-support $18.07 (broken 18 days ago, 0.73-ATR fall, score 41).

### 2026-05-27 — Support selection: most-recent-in-time (asymmetric to resistance)

User correction 2026-05-27 from USAR case: the active support is the MOST RECENT swing low ($19.36 from 19.5.2026), not the HIGHEST swing low below current ($21.46). Older swing lows above the most-recent one were bypassed when price dipped through them, so they're no longer load-bearing.

The asymmetry (resistance = lowest above, support = most-recent below, broken-R = highest below) is now codified in `resources/sr_levels.py` module docstring. See `resources/README.md` for full rationale.

**Impact on P1 detector**: P1 still requires close within `max_distance_atr=1.0` of the support level. USAR's $19.36 is 3.03 ATR below current — too far for P1. USAR is "structurally anchored to $19.36" but not "actively retesting it". The chart-pane S/R strip surfaces the level for visual context even when no scan trigger fires.

### 2026-05-27 — Cluster tolerance: percentage → absolute ticks (±3 ticks)

User rule 2026-05-27: *"the placeholder cannot be too wide... plus minus 3 tick."* The cluster band in `resources/patterns.horizontal_resistance_np` etc. switched from percentage (`cluster_band_pct=0.01` = 1%) to absolute ticks (`cluster_tolerance_ticks=3, tick_size=0.01` = ±$0.03). For a $400 stock the previous 1% meant ±$4 (400 ticks wide), wildly off the user's intent. Absolute ticks keeps the placeholder tight across the universe.

P1Config + P2 scanner config updated:
- `P1RebConfig` and `P2Config`: `cluster_band_pct: float = 0.01` REPLACED with `tick_size: float = 0.01` + `cluster_tolerance_ticks: int = 3`. Passed through to `horizontal_resistance_np` / `horizontal_support_np`. (P3 detector uses `find_broken_resistance_below` which already had tick-based tolerance for the breakout check.)

P1/P3 candidate counts unchanged (16 / 19), but `mountain_anchors` field is now tighter (touches within ±$0.03, not ±1%). Scoring downstream re-balances slightly — multi-touch level scores drop because the touches no longer cluster within the tight band. The candidate ORDER reshuffles but no setups are dropped.

See `resources/README.md` for the full rationale on the API change.

### 2026-05-27 — P1 + P3 mountain defaults relaxed (5 days / 0.5 ATR) for actively-tested levels

User teaching from GOOGL case 2026-05-27: GOOGL is BOTH an EMA20-rebound AND a P1 setup (per user's chart-reading). The support level $382.77 (May 12 pin bar valley) is only 9 trading days old, with a 2.68-ATR rally since — well within the "actively-tested level" framework, but blocked by the previous strict gates (15-day age, 2.0-ATR pullback). User's framework: the same level evolves P2 → P3 → P1 over time as price interacts with it, and recent active levels matter.

**P1RebConfig + P3RetestConfig overrides:**
- `support_min_age_bars` / `mountain_min_age_bars`: 15 → **5**
- `support_pullback_atr` / `mountain_pullback_atr`: 2.0 → **0.5**

The underlying `resources/patterns.horizontal_resistance_np`, `resources/sr_levels.horizontal_support_np`, and `resources/sr_levels.find_broken_resistance_below` module-level defaults were also relaxed in lockstep so `find_key_levels` (chart pane S/R strip) uses the same defaults. See `resources/README.md` for the full rationale.

**DITP P2 scanner (`scanner.py`) KEEPS strict tuning** (`mountain_min_age_bars=15, mountain_pullback_atr=2.0`) — P2's watchlist generation has been tuned against historical-quality benchmarks; loosening would regress watchlist quality. P2 + P1 + P3 now have DIFFERENT validation gates for their structural levels, deliberate.

**Smoke-tested impact**:
- GOOGL surfaces as P1: support $382.77, today's low $382.60 (touched), bounce 0.65 ATR, close $388.88, distance 0.63 ATR, score 30. The setup matches the user's read.
- Parquet universe (241 tickers): P1 candidates **10 → 16** (BFS, AVNS, CARR, BANR — additional multi-mountain-anchor supports). P3 candidates **10 → 19** (recent breakouts like AA 7d ago, AVT 7d, AMD 10d, AAPL 5d — previously blocked by 15-day age gate). All retain bounce ≥0.3 ATR.
- USAR remains correctly excluded from P3 (its broken-R candidate $26.36 is 77d old, well outside the 3-45 day staleness window — independent of the mountain-age gate change).

### 2026-05-27 — `p1_rebound.py` v1.2.0 + `p3_retest.py` v1.3.0 + DITP P2 scanner: framework reintegration

User framework reintegration 2026-05-27: *"the immediate mountain top nearest to the current price action is relevant... Higher mountains are FUTURE P2 setups, not currently relevant. So on and so forth..."*. Each mountain peak is an independent P2 → P3 lifecycle. The relevant level at any moment is the one closest in price to current.

**Underlying changes** in `resources/patterns.py` + `resources/sr_levels.py` (see those READMEs for the full story):
- Selection rule: LOWEST mountain above current for resistance, HIGHEST mountain valley below current for support, HIGHEST broken mountain below current for P3 polarity-flip — all "immediate nearest in price" semantics.
- Ceiling gate (`max_below_window_high_pct`) default raised from 0.02 → 1.0 (effectively disabled). Higher mountains above the chosen level are FUTURE setups, not disqualifiers.
- Cluster gate `min_touches` default lowered from 2 → 1. A single confirmed mountain top is a valid level.

**Per-detector impact:**
- `p1_rebound.py` v1.1.0 → **v1.2.0**: picks up the new `horizontal_support_np` semantics; config `support_min_touches` lowered 2 → 1 to match.
- `p3_retest.py` v1.2.0 → **v1.3.0**: picks up the new `find_broken_resistance_below` semantics; docstring updated; the previous "absolute highest mountain must be broken" gate (v1.2.0) is removed — it was over-restrictive and blocked legitimate P3 candidates whenever some old historical peak loomed unbroken.
- `scanner.py` (P2): config `min_touches` 2 → 1; config `max_below_window_high_pct` 0.02 → 1.0 (gate disabled). No version bump in this turn — the watchlist generation logic is unchanged, only the resistance-discovery defaults shifted to match the framework.

**Smoke-tested impact:**
- USAR: previously tagged P3 with flip $25.95 (v1.0.0) or completely blocked (v1.1.0). Now correctly: P3 returns nothing (broken-R candidate $26.36 is 77d old, outside the 3-45 day staleness window); chart-pane R-above = $32.07 (the next structural mountain). USAR remains "P2 territory in progress" per the user's reading, but is 1.79 ATR from $32.07 so not yet within the 1.5 ATR P2 trigger gate.
- Live Finviz scan: P1 2 candidates (TSLA, C), P3 0 candidates, P2 0 candidates — all clean.
- Parquet universe (241): P1 3 → **10** candidates (top scorer AME with 4 mountain anchors at $227.95, score 70), P3 4 → **10** candidates.

**Known tradeoff**: the user's visually-identified peaks (like USAR's $28.69) may not satisfy `mountain_pullback_atr=2.0` if the subsequent pullback was shallow (<2×ATR). The algorithm falls through to the next structurally-confirmed mountain. If the user wants shallower peaks to count as mountains, lower `mountain_pullback_atr` to 1.0-1.5. Documented as a config knob, not changed by default to avoid surfacing noise across the universe.

### 2026-05-27 — `p3_retest.py` v1.2.0: only the highest mountain counts, 3-tick breakout tolerance

User correction 2026-05-27 from the USAR case: USAR was tagged P3 with `flip=$25.95` (a lower mountain peak that price had crossed). But USAR's REAL structural ceiling was higher — close $28.62 was still BELOW the actual key resistance. The user explained: *"For a P3 setup, the breakout must have already happen to break above [the resistance] and stay above this level as a support and price coming back to test this new support. Allow a plus minus 3 tick of this level when interpreting this setup."*

**Fix** (v1.2.0): pulls through to `resources/sr_levels.find_broken_resistance_below` v1.1.0, which now refuses to return ANY level unless the **highest** mountain peak in the 1-year lookback has been clearly broken above (`current_price > highest_level + 3 * 0.01`). Lower crossed peaks no longer count — they're not the key resistance.

**Config gains** two tick-tolerance fields:
- `tick_size: float = 0.01`
- `breakout_ticks: int = 3`

**Removed**: `max_candidates: int = 5` (the broken-R helper now returns at most 1 level; per-candidate iteration is single-pass).

**Smoke-tested impact**:
- USAR: previously tagged P3 with flip $25.95 / bounce 0.99 ATR / score 41. Now correctly drops out.
- Full Finviz scan (44 tickers): P3 candidates 1 → 0 (USAR was the only false positive).
- Parquet universe (241 tickers): P3 candidates 11 → 4 (AOSL, BNL, ATEN, ALGM — all real breakout-retest setups).

Per CLAUDE.md bump rule: MINOR (gating filter tightened + new config fields; plan-dict shape unchanged).

### 2026-05-27 — `p1_rebound.py` v1.1.0 + `p3_retest.py` v1.1.0: bounce-magnitude reaction gate

User rule 2026-05-27: *"we want to see if price action is bouncing at the horizontal support... if price action react by [re]bouncing in the horizontal support, we have a potential P1 setup."* And for P3: *"price action shows reactions in the resistance turned support."* The key word in both is **REACTION** — a touch followed by sideways drift isn't a setup; a touch followed by a visible bounce is. The v1.0.0 detectors gated on touch + bullish close + reclaim, but never measured the size of the reaction itself.

**v1.1.0 adds a reaction-magnitude gate (P1 + P3 identical math):**
```
bounce_low           = min(lows[touched_idx:])           # lowest low since touch
bounce_magnitude_atr = (last_close - bounce_low) / atr
gate: bounce_magnitude_atr >= cfg.min_bounce_atr          # default 0.3
```

The "lowest low since touch" framing handles multi-bar touches where the deepest probe of the level might be earlier than the most-recent qualifying touch. The magnitude is in ATR units (per CLAUDE.md normalization rule), so the same 0.3 default applies across $4-ATR and $1.50-ATR tickers without retuning.

**Recency tightened to keep the reaction fresh:**
- P1: `touch_lookback_bars` 5 → 3 (a 5-day-old touch with no follow-through isn't a "reaction")
- P3: `touch_lookback_bars` 7 → 5 (P3 retests can take a bit longer to develop than P1 rebounds)

**Reaction strength now drives sort order.** Added a `reaction` score component (`min(10, int(bounce_magnitude_atr * 10))`) to both detectors. Stronger bounces score higher.

**Candidate dict** gains a `bounce_magnitude_atr` field for downstream consumers (the dashboard's Setup-column rendering picks it up automatically — the matchDetail formatter still shows the level price, but raw data is there if needed).

**Smoke-tested impact**:
- P1: same 3 symbols pass, reordered — AME (bounce 0.76 ATR) now ranks ahead of ABCB (bounce 0.40 ATR) despite equal mountain anchors. Reaction strength is the new tiebreaker.
- P3: 12 → 11 candidates (ADEA filtered for weak bounce). Top is now a 4-way tie at score 42: BRX (0.88 ATR), AOSL (0.86), BNL (1.00), AA (1.34) — all showing decisive polarity-flip reactions. Previous top BHF dropped to #5 (its 0.66-ATR bounce is middling despite proximity 0.06 ATR).

Per CLAUDE.md bump rule: MINOR (new gating filter + sort-order knob, plan-dict shape unchanged besides the additive field).

### 2026-05-27 — S/R lookback unified to 1 year (~252 trading days) across P1 / P2 / P3

User rule 2026-05-27: *"when you look at Support and Resistance on a daily chart, you will look at 1 year daily chart to look at valley and mountains."* The three S/R-anchored DITP detectors had different defaults from earlier tuning:

- `scanner.py` (P2): `resistance_lookback = 90` → **252**
- `p1_rebound.py` (P1): `support_lookback = 120` → **252**
- `p3_retest.py` (P3): `lookback = 180` → **252**

Minimum bar requirement in each detector raised correspondingly (from 220 to `cfg.lookback + 14` = 266). yFinance fetch in `dashboard/server.py` bumped from 400 → 500 calendar days so the histories arrive with margin (~355 trading days delivered vs 266 needed).

Smoke-tested impact: AAPL's support_below validation jumped from 2 touches / 2 mountains → 5 / 5 at the same level (older touches now in range); ABBV gained an extra historic P3 candidate; P1 / P3 scanners returned the same actionable candidate sets (staleness window and EMA gates dominate over the lookback for those).

This change is documented as a rule in the `sr_levels.py` module docstring as well, so the rationale doesn't get lost if a future caller wonders why the defaults look "round" rather than "tuned".

### 2026-05-27 — `p1_rebound.py` + `p3_retest.py` v1.0.0: DITP P1 and P3 dashboard scans

User: *"ok for front end me will also apply the P1, P2 and P3 setup"* — P2 was already wired via `scanner.py` (key `ditp` in the dashboard SETUPS registry); P1 and P3 needed parallel detectors so the same Finviz-universe + Setup-column workflow could cover all three structural-level setups.

Both detectors are single-file modules in the same shape as `ema_rebound.py` — `@dataclass` config + `detect_*` returning candidate dict | None + `scan_universe` returning sorted list. No CLI watchlist files; consumed only by the dashboard's `POST /scanner/yf_scan?setup={p1_rebound,p3_retest}` for ad-hoc filtering of the Finviz universe. The "no entry pipeline yet" status applies to both — they're filters/scanners, not live trading systems.

**P1 (rebound off horizontal support)** is the long-side mirror of P2. Uses `resources/sr_levels.horizontal_support_np` for level discovery (mountain-valley anchors instead of mountain peaks; no floor gate so closest-recent valley wins — see sr_levels.py for the asymmetry rationale). Bullish reclaim candle + recent low-touch within `touch_lookback_bars=5`. Smoke-test against the 211-symbol laptop daily set returned 3 candidates: ABCB (S=$83.75, 2 mountain anchors, today's touch), AME, ACA — all in clean uptrends bouncing tightly off a validated support.

**P3 (retest of broken resistance)** uses `resources/sr_levels.find_broken_resistance_below` to enumerate historic mountain peaks now below current price, then applies a STALENESS WINDOW (`breakout_min_age=3, breakout_max_age=45`) — too-fresh peaks are still on the rocket-up from the breakout, too-stale peaks are no longer load-bearing. Closest qualifying level wins (the helper already sorts highest-first). Smoke-test returned 12 candidates; top scorer BHF showed the textbook polarity flip: flip level $62.63, today's close $62.67 (+0.06 ATR — basically touching), breakout 26 days ago. Score components: staleness sweet-spot (peak 7-28 days), proximity, recency.

Default config knobs all ATR-relative per CLAUDE.md normalization rule. Both detectors require uptrend stack + close > EMA200 (no dead-cat-bounce / mid-downtrend false positives).

### 2026-05-27 — `ema_rebound.py` v1.0.0: daily support-bounce detector on EMA20 / EMA50 / EMA200

User: *"Add a setup that will find rebound on EMA20 or EMA50 or EMA200"* -- applied as a filter setup in the dashboard's Scanner view (one of the entries in the "Setup matches" panel that runs against the Finviz universe).

**Detection logic** (per symbol, daily bars; needs >= 210 bars for EMA200 stability):

1. **Trend gate**: EMA20 > EMA50 > EMA200 AND close > EMA200. Without uptrend the EMAs would act as resistance, not support; the setup is meaningless.
2. **Bullish-candle gate**: today's close > today's open AND close in upper half of bar range (rebound character, not a weak retest).
3. **EMA selection** (descending strength: EMA200 -> EMA50 -> EMA20):
   - Close above the EMA (rebound confirmed).
   - Close within `max_distance_atr * ATR` of the EMA (recent rebound, not a 5-ATR runner).
   - A bar in the last `lookback_bars` days where low was at or just below the EMA (within `touch_tolerance_atr * ATR`).
   - First EMA that satisfies all three is the anchor. EMA200 beats EMA50 beats EMA20 because the deeper pullback that held = stronger structural support.
4. **Score**: `_EMA_WEIGHTS[anchor] + proximity_bonus + recency_bonus`. Sort `(-score, distance_atr asc)`.

**Why a single-file module, not a `strategy/DITP/ema_rebound/` setup folder**: this detector is consumed only by the dashboard's ad-hoc scan endpoint, not the bot's execution pipeline. There's no `build(cfg)` or `evaluate()` to plug into the orchestrator; no journal events; no `__version__` per-rule-edit churn anticipated near term. If/when we wire it into the bot's live entry pipeline, promote to a proper setup folder following the convention (`ditp_p2/` is the template).

**Smoke test** (laptop, user's intraday Finviz URL):
- `POST /scanner/yf_scan?setup=ema_rebound` -> 4 matches against 43 Finviz tickers in ~3s total:
  - FCX -> EMA50, touched 1 day ago, dist 0.94 ATR, score 30
  - C    -> EMA20, touched today, dist 0.54 ATR, score 23
  - GOOG -> EMA20, touched today, dist 0.74 ATR, score 22
  - GOOGL-> EMA20, touched today, dist 0.79 ATR, score 22
- No EMA200 hits in this universe (would need a deeper pullback that recently held; the high-vol Finviz set hasn't seen those recently).

**Wiring notes:**
- Server: dispatch added in `dashboard/server.py::scanner_yf_scan` for `setup=ema_rebound` -- monkey-patches `bars_store.load_bars` to return the yFinance bar cache (identical pattern to the DITP P2 path) then calls `ema_rebound.scan_universe()`.
- Frontend: new entry in the `SETUPS` array with a custom column spec (Symbol / EMA / Last / EMA value / Dist ATR / Days since / ATR / Score). The dashboard renderer is now column-spec-driven so adding more setups with different shapes won't require renderer forks.

### 2026-05-26 — `scanner.py` + `tc_scanner.py`: holiday-aware target / source dates

User rule (chat 2026-05-26, immediately after the TC scaffold landed): *"when scanning for the tickers we will be looking at last trading day setup, skip the holiday"*. Both DITP scanners were using a Mon-Fri-only "skip weekends" date math, which produced bogus `target_date=2026-05-25` (Memorial Day) when the P2 scanner ran EOD Fri 2026-05-22, then the TC scanner consuming that file resolved its source date to a non-trading-day and found zero bars.

Fix lives in the new `resources/market_calendar.py` (NYSE full-closure list + `is_trading_day` / `last_trading_day` / `next_trading_day` helpers). Both scanners now import from there:

- **`scanner.py::next_trading_day_iso`** — was Mon-Fri-only weekday skip, now delegates to `resources.market_calendar.next_trading_day`. The function signature is unchanged, so no caller modification required. Effect: a P2 scan running EOD Friday-before-Memorial-Day correctly writes a Tuesday-targeted watchlist.
- **`tc_scanner.py`** —
  - Removed the local `_next_business_day` helper. All "next trading day" / "last trading day" calls now use `resources.market_calendar`.
  - When `--source-date` is omitted, the scanner reads the consumed P2 watchlist's `target_date`. If that date is **not** a trading day (legacy file pattern from before this fix), it walks back to the last actual trading day and prints a `# note:` to stdout explaining the walk-back. The TC candidate's bar lookup then happens on a date that genuinely has bars.
  - If the resolved source_date is still not a trading day after the walk (shouldn't be possible, defensive check), the scanner aborts with a stderr error and exit code 1 instead of silently producing 0 candidates.
  - `--source-date` (explicit) is taken literally — the user knows what they're doing.

Smoke-validated against the existing `watchlist_ditp_2026-05-25.json` on disk: TC scanner now correctly resolves source_date to 2026-05-22 (Fri), computes target_date=2026-05-26 (Tue, skipping Mon), and emits the walk-back note. The 0-candidates result is now a genuine "no Friday breakouts in this watchlist" rather than a "no bars on Memorial Day" data hole.

No `__version__` bumps on `ditp_p2/impl.py` or `ditp_tc/impl.py` — the candidate-dict shapes are unchanged; this is purely a date-math fix in the scanner layer.

### 2026-05-26 — `ditp_tc/` v0.1.0 + `tc_scanner.py` (Phase 1 of TC build)

First wire-up of DITP Setup 4 — TC (Trend Continuation). Mirrors the same phased-build pattern that DITP P2 used (commit history: 4a3f9c4 P2 v0.1 watch-only → d3d3be5 confluence-tier filter → c05ee47 backtest adapter). Source: `strategies-reference/DITP.md` §6 Setup 4 (capture began chat 2026-05-25).

Files added in this folder:
- `tc_scanner.py` — EOD Day-0 TC scanner (family-level CLI, sibling of `scanner.py`).
- `ditp_tc/__init__.py` — setup package marker.
- `ditp_tc/impl.py` — strategy module v0.1.0 (`pick_universe` / `fetch_bars` / `evaluate` / `do_shortlist` / `build`).
- `ditp_tc/README.md` — setup README with Status + TBDs + this same Changelog entry rephrased from the setup's perspective.

**Phase 1 scope:**

1. **`tc_scanner.py`** walks the most recent `state/watchlist_ditp_*.json` and applies the Day-0 filters captured so far in DITP.md §6 Setup 4:
   - Today's daily close > P2 candidate's `resistance` (= `range_high`). The breakout actually fired.
   - Today's daily candle is bullish: `close > open` AND `(close - low) / (high - low) >= 0.5` (close in upper half). Filters wicky/barely-green breakouts.
2. **TC candidate** carries forward enough P2 metadata (variant, tier, score, resistance, confluence, cautions, EMAs, ATR14, yesterday's D/E/F levels, universes) that downstream consumers don't need to re-read the P2 watchlist. Plus Day-0 specifics: `day0_close`, `day0_open/high/low`, `day0_close_position` (0–1 normalized), `breakout_strength_atr` ((close − R)/ATR — how cleanly the close cleared resistance).
3. **`watchlist_tc_<tomorrow>.txt`** mirrors P2's `.txt` format (drops D-tier + Tier-0 confluence) so the orchestrator's entry pipeline applies a consistent filter across DITP setups. `.json` keeps every candidate for review.
4. **`impl.py` v0.1.0** — watch-only. `evaluate()` returns None; `do_shortlist()` journals `watchlist_loaded` with per-tier + per-event counts. `source_event` field is `p2_breakout` today; ready for `p1_rebound` once Setup 2 (P1) is taught.
5. **Sort key** = `(-breakout_strength_atr, tier, -score)`. Cleanest breakouts first — those are the highest-conviction continuation candidates.

**Why phased build (matching P2's pattern):** the TC framework taught 2026-05-25 was incomplete — premarket strictness, entry trigger, stop, TP, and cautions are all TBD in DITP.md §6 Setup 4. Phase 1 ships the part that IS fully specified (Day-0 filter + scanner output) so the watchlist starts producing data immediately. Phase 2 (premarket validation) + Phase 3 (live entry pipeline) wait on the user filling the gaps.

**Cross-folder impact (recorded in those folders too per the per-folder README rule):**
- `strategy/__init__.py` — added `"ditp_tc"` to `KNOWN_STRATEGIES` + `"DITP.ditp_tc"` to `_STRATEGY_IMPORT_PATHS`. (No separate README for `strategy/`; SKILL.md serves as that level's manifest.)
- `config.example.json` — new `ditp_tc` config block alongside `ditp_p2`. First-run seed only; live state lives in `state/enabled_ditp_tc.flag` + `state/armed_ditp_tc.flag`.

**Dashboard visibility (per CLAUDE.md "Dashboard visibility rule"):** auto-surfaces handle v0.1 — the Gating drawer + Strategy Analysis drawer pick TC up automatically once `strategy.ditp_tc.*` events flow. A dedicated `/strategy/ditp/tc_watchlist` endpoint + frontend section showing the TC candidate table is the natural next turn's work per the rule's "UI catches up next turn" allowance.

**No live-bot risk** — `evaluate()` returns None regardless of ARM. Even if the user toggles ARM, no order is submitted.

### 2026-05-24 — `_decision_engine.py` v0.1.0 + `ditp_p2/backtest_adapter.py` (Phase 1 of backtest build)

User decision: build the backtester before continuing DITP P2 live execution. *"This was not planned for but I think this is very important before we put the strategy to work, we need to see how good is the strategy."*

Two new files in this folder; live `ditp_p2/impl.py` stays at v0.1.0 (watch-only — no change to live behaviour). The backtester (built in `review/`) plugs into DITP P2 via the new adapter; the adapter wraps the existing scanner + the new family-level decision engine.

`strategy/DITP/_decision_engine.py` v0.1.0
- Family-level module — will be shared with future DITP setups (P3 retest, etc.) as they come online.
- 4 pure functions for Phase 1 bare-bracket math: `entry_signal(curr_close, prev_close, resistance)` (first-crossing 3m close above R), `stop_price(entry, atr_daily, mult=0.25)`, `target_price(entry, atr_daily, mult=0.5)`, `tradeability_ok(entry, target, atr_daily, atr_mult_cap=1.0)` (rejects setups where 2R > 1×ATR per user's "we need a tradable setup" rule).
- Phase 2-4 primitives (`momentum_ok`, `one_min_confirmation_ok`, `anti_pattern_detected`, `update_trailing_stop`, `early_exit_check`, `add_to_winner_check`) documented as commented stubs so the eventual full surface is discoverable in one place.
- This is the SINGLE SOURCE OF TRUTH for the family's decision math. When `ditp_p2/impl.py` bumps to v0.2.0 for live wiring, it imports from here. Backtest and live cannot drift — same code path by construction.

`strategy/DITP/ditp_p2/backtest_adapter.py`
- Implements `review._strategy_adapter.BacktestAdapter` Protocol for DITP P2. ~120 LOC including bootstrap + docstrings; ~30 LOC of actual glue.
- `pick_candidates(as_of_date)` calls `scan_universe(..., as_of_date=as_of_date)` and applies the v0.2-alpha1 tradeability filter (`tier != "D" AND confluence_tier > 0` — same rule the live `.txt` watchlist uses, commit d3d3be5).
- `entry_signal` / `stop_price` / `target_price` / `tradeability_ok` are thin delegations to `_decision_engine`. Strategy-specific metadata (variant, confluence_tier, cautions, D/E/F levels) is preserved on the candidate dict so the backtest metrics can do `by_variant` / `by_confluence_tier` / `by_caution` cuts.
- Adapter registered in `review/_adapter_registry.py` as `"ditp_p2"` → `DITPP2BacktestAdapter`.

`strategy/DITP/scanner.py` — `detect_p2()` + `scan_universe()` gain `as_of_date=None` parameter
- When `as_of_date` is set, the daily bars passed to the detector are truncated to dates ≤ cutoff *before* any pattern math runs. This is THE look-ahead guard for the backtest — without it, the scanner would see future bars and produce dishonest candidates.
- New `_bar_date()` helper normalizes bar timestamps (int / datetime / pandas.Timestamp / ISO string) to a `date` for the cutoff comparison.
- Live scan behaviour unchanged when `as_of_date=None` (default).

Smoke-test confirms the cutoff works: `detect_p2('NVRI', cfg)` returns Tier A on today's snapshot but `None` when called with `as_of_date=2026-05-10` (NVRI hadn't yet developed the setup 14 days ago).

Next session: depending on Phase 1 backtest results, either advance to Phase 2 of the backtest build (momentum gate + 1m confirmation) OR begin Phase 2 of live execution (intraday continuous-monitor framework + Alpaca bracket submission). Decision driven by what the data shows.

### 2026-05-24 — Scanner v0.2-alpha1 (Phase 1 of DITP P2 v0.2): confluence Tier-0 hard filter + prior-day key levels (D/E/F)

**Scope of this slice.** The full DITP P2 v0.2 spec (full live entry, polarity-flip confirmation, anti-pattern detection, HH/HL trailing, hammer-wick stop, add-to-winner) was taught in chat 2026-05-23. This commit ships **Phase 1 only** — the scanner-side prefilter + chart visibility that the future entry pipeline depends on. The remaining phases (intraday monitor framework, decision engine, order submission, dashboard armed-state visibility) are out of scope for this turn and listed below as a queued spec.

**Phase 1 changes (this commit):**

1. **Confluence tier computed per candidate** (`scanner.py`).
   - New helpers: `_round_grid_for_price()` (price-tier grid: <$5 → 0.50/1.00, <$20 → 1/5, <$100 → 5/10, <$500 → 10/50, <$2000 → 25/100, else 100/250); `_is_near_round_number()` (±0.5% snap); `compute_confluence_tier(resistance, yesterday_high) → (tier, reasons)`.
   - Tier ladder:
     - **Tier 0** — plain breakout with no confluence anchor → DROPPED from `.txt` watchlist.
     - **Tier 1** — daily R coincides with a minor round number.
     - **Tier 2** — daily R coincides with a MAJOR round number, OR matches yesterday's high.
     - **Tier 3** — daily R = yesterday's high AND a major round number (triple confluence — rare).
   - User rule (chat 2026-05-23): *"P2 Breakout Scenarios that are tradeable with more probability — (1) Daily Resistance that coincide with Round Number (2) Major Round Number (3) Previous Day significant Round number Resistance & Previous Day High"*.
2. **Tier-0 hard filter in `write_watchlist()`**. The `.txt` watchlist (which the orchestrator's entry phase reads) now drops any candidate with `confluence_tier == 0`. The full `.json` retains every Tier-0 row for review so we can backtest "what would have happened if we relaxed the filter". The `.txt` line gained a `CONF{tier}` suffix column so downstream consumers can read the tier without re-loading the `.json`.
3. **Prior-day key levels stored on every candidate** (`P2Candidate.yesterday_high/yesterday_low/yesterday_close`). Codes from the user's intraday key-level taxonomy (chat 2026-05-23): **D** = yesterday's L (institutional support), **E** = yesterday's H (polarity-flip target if today gapped above), **F** = yesterday's C (fair-value anchor). Pulled from `bars[-2]` of the daily parquet at scanner time.
4. **CLI output gains a `cnf` column + per-candidate `conf_reasons` annotation** alongside the existing cautions list. Footer counts both the raw candidate count AND the tradeable-after-Tier-0 count so the operator can see at a glance how many setups survived the new filter.
5. **Dashboard chart overlays** (`dashboard/server.py::_gather_chart_overlays`).
   - Added dotted E (yest H, orange `#e67e22`), F (yest C, purple `#9b59b6`), and D (yest L, muted gray `#7f8c8d`). D is included for visual completeness even though P2 breakouts don't act on it.
   - Added a confluence annotation overlay (`kind: "annotation"`) that surfaces the tier + reasons in the chart legend so the trader knows WHY the candidate made the cut.
6. **No `__version__` bump on `ditp_p2/impl.py`** yet — `impl.py` is still v0.1.0 (watch-only). It will bump to **v0.2.0** when Phase 2 lands and the strategy starts emitting live entry plans. The scanner's behavioural change is captured here because `scanner.py` is family-level and has no `__version__` of its own.

**Queued for the next slice (DITP P2 v0.2.0 — multi-phase build, NOT in this commit):**

These were explicitly taught by the user in chat 2026-05-23 and need to be preserved verbatim so the next session can pick up without re-teaching. Source: chat 2026-05-23 (entry/exit/management deep dive).

- **Entry trigger (3-min chart).** Daily R = the scanner's `resistance` field (top of zone). Entry fires when a 3-min bar CLOSES above resistance.
- **1-min confirmation.** After the 3-min close-above, drop to the 1-min chart and confirm strong support at the resistance-turned-support price line — typically 1-min hammer / hold of the breakout level. One-bar confirmation is enough.
- **EMA stack as momentum gate.** On both 3-min AND 1-min chart: EMA6 > EMA18 > EMA50 stack indicates good momentum; otherwise stand down.
- **Sentiment gate.** Composite ≥ 0 AND VXX % change ≤ +5% (use the dashboard Market Sentiment panel as the source of truth — gate reads `/market/sentiment`).
- **Stop placement (the artful part).** Default = 0.25 × daily ATR(14) below entry. **Override:** if there's a hammer wick at the resistance-turned-support level, place stop 3¢ below the wick — the wick is the support being respected. User: *"This is an art not science, we have to see the volatility of the ticker and decide the stop loss placement"*. If no hammer wick exists on the 1-min confirmation candle, fall back to default 0.25 × ATR.
- **Target.** Default = 0.5 × daily ATR(14) above entry → that's the bracket's TP leg.
- **Tradeability filter.** If 2R distance (entry → TP) > 1 × daily ATR, the trade is unfeasible — skip. Ensures the 2R move stays within a single ATR of normal daily range. User: *"if it is more than 1 ATR, then the trade need to be careful because it may not be feasible. We need a tradable setup."*
- **Order type.** Bracket order on Alpaca: market parent + TP limit + SL stop, DAY TIF.
- **Cancellation conditions.** If the 3-min chart goes into downtrend (EMA stack inverts) before the trigger fires, cancel the working bracket. Time-based ceiling = EMA-driven, not a clock cutoff. **No re-arming** after cancellation today.
- **Anti-patterns at daily R that abort the entry** (5 reversing-candle patterns — must be interpreted AT a key level, not free-floating): outside bar, inside bar, shooting star, failed sustain, bearish engulfing. *"the candle formation is meaningful to be interpreted in a key price level, the support and resistance, the round number level"*.
- **Bullish confirmation threshold (flexibility).** Price doesn't have to react at the pre-determined daily R — it may come down to PM support (A) or yesterday's low (D), form a bullish hammer, then rally to break out. Watch for the reaction at A/D/E/F intraday dotted levels too, not just daily R.
- **Intraday key levels to draw** (already wired in `_gather_chart_overlays`): A = PM support, B = first pullback valley, C = round number, D = yesterday's L, E = yesterday's H, F = yesterday's C. Solid line = daily R; dotted = intraday refs.
- **Add-to-winner (single shot).** After entry: when the first higher-low forms on the 3-min chart and the trailing stop is moved UP to that higher-low (becoming a breakeven-or-better stop), open ONE additional position of the same size. New position shares the moved-up stop AND aims for 2R from the original entry. **Only one add per trade.** User: *"we will not add twice"*.
- **HH/HL trailing stop.** Trend is considered good while price prints higher-highs + higher-lows on 3-min. SL ratchets up to sit just below each new HL (3¢ buffer). If the position has a strong catalyst, switch to trailing only after the stop hits breakeven.
- **Early exit on warning.** If price action shows any of the 5 reversing patterns at a key level after entry, exit early (don't wait for the trailing stop).
- **EOD cleanup.** Any working bracket that never triggered gets cancelled at EOD. If no orders triggered today, cancel everything.

**Why a phased build:** the user's instruction was *"build and commit"* and the natural slice boundary is "scanner-side prefilter + visibility" (1 turn, no orchestrator-pattern changes) vs "live entry pipeline" (multi-turn: needs an intraday continuous-monitor framework, the 5-anti-pattern detectors, bracket extension in `execution/`, dashboard armed-state visibility). Shipping Phase 1 now gets the high-conviction filter live for tomorrow's open while the build queue stays clear-eyed about what's left.

**Validation results.** Re-running the scanner on the current S&P-500 + NASDAQ snapshot: candidates that previously made the watchlist now carry a `confluence_tier` field; Tier-0 plain breakouts have dropped from the `.txt`; the JSON retains everything for review. Dashboard's DITP-symbol chart panels now render E/F/D dotted overlays alongside the existing daily-R solid line + intraday A/B/C dotted refs.

### 2026-05-23 — `ditp_p2/impl.py` v0.1.0 — watch-only strategy wired into orchestrator
- User rule (chat 2026-05-23): *"the DITP strategy also hook into the dashboard"*. The scanner has been producing watchlists EOD for a while but DITP wasn't a tradeable strategy in the orchestrator's `KNOWN_STRATEGIES`, so it never appeared in the Gating drawer / Candidates tab / Strategy Analysis drawer.
- New `strategy/DITP/ditp_p2/impl.py` v0.1.0:
  - `pick_universe(date_iso, cfg)` reads `state/watchlist_ditp_<date>.json`, returns tier A/B/C symbols (D dropped, same as the .txt watchlist). Falls back to the most recent watchlist file when the exact-date file isn't on disk (handles late starts + cross-day dry-runs).
  - `fetch_bars` returns empty maps — no intraday data needed for watch-only.
  - `evaluate` journals `strategy.ditp_p2.monitoring` per symbol per fire (carries tier, variant, resistance, distance_atr, cautions, version) and returns **None**. No plan = no order, even when ARMED. This is deliberate: the user explicitly deferred trade execution + sizing — *"we execution of trade and size position we deal with it later. now we focus on the pattern first"*.
  - `do_shortlist` journals `watchlist_loaded` with per-tier counts.
- `strategy/__init__.py` — added `"ditp_p2"` to `KNOWN_STRATEGIES` + `"ditp_p2": "DITP.ditp_p2"` to `_STRATEGY_IMPORT_PATHS`.
- `config.example.json` — new `ditp_p2` block: `enabled: false, shortlist_et: "09:00", entry_et: "09:31", entry_cutoff_et: "09:35", max_concurrent: 3, take_profit_R: 2.0, params.min_tier: "C"`. Sits alongside the existing GUNS / OS blocks.
- **Smoke-tested end-to-end:** orchestrator now reports `4 strategies wired`; with DITP toggled ON+ARMED the dry run loads 6 universe symbols, journals one `monitoring` event per symbol, and submits zero orders (correct watch-only behaviour). 
- Next version (v0.2.0): wires the 5 anti-pattern detectors from Step 3 of the user's teaching + the actual breakout entry trigger.

### 2026-05-23 — Scanner: pattern primitives hoisted to `resources/patterns.py`
- User rule (chat 2026-05-23): *"this pattern recognition should be an independent module in the resources because all the strategy will be using it"*.
- Four inline functions deleted from `strategy/DITP/scanner.py`: `ema`, `atr14`, `find_flush_up_bar`, `find_resistance`. The slope-math inside `classify_variant` was also extracted.
- DITP now imports `atr_wilder_np`, `ema_np`, `thrust_bar_np`, `window_slopes_np`, `horizontal_resistance_np` from `patterns`. The new symbols are added to `resources/patterns.py` in a dedicated "Numpy-array primitives" section so they sit alongside the existing list-of-dict API rather than replacing it.
- A thin `_resistance()` adapter remains in the scanner — it binds `cfg.*` to the primitive's keyword args and reshapes the return into the legacy 6-tuple `detect_p2()` already consumed. Same for `thrust_bar_np` (called with `cfg.flush_up_*` kwargs).
- What stays in DITP: `P2Config`, `P2Candidate`, `classify_variant` (the variant taxonomy logic — A/B/C decision tree, bullish-markup test), `detect_p2` orchestration, `score_candidate` (scoring + cautions + tier), `scan_universe`, `write_watchlist`, CLI. The split is: primitives = geometry; DITP = semantics.
- Scan output is **byte-identical** before/after the refactor: same 10 candidates (GS, NVRI, PLD, CTRE, WSR, DOC, CENTA, LYV, FN, MCW), same tiers, same scores, same proximity-first order. 49/49 `patterns.py` tests still pass.
- Future strategies (ORB, future-DITP-setup-2, etc.) can now import the same primitives directly — no copy-paste, no drift risk.

### 2026-05-23 — Scanner: M&A news overlay REVERTED same session
- User reversal (chat 2026-05-23): *"nevermind we drop out the m&A"*. The M&A news overlay added moments earlier (entry below) was rolled back in the same session.
- Removed `filter_ma_targets()` from `strategy/DITP/scanner.py`. `scan_universe()` reverted to returning `list[P2Candidate]` (was `tuple[list, list]`). `write_watchlist()` no longer carries `ma_dropped`. CLI flag `--no-news-filter` deleted.
- `resources/yfinance_news.py` docstring restored to its original GUNS-only contract — the module is once again a GUNS-internal classifier with no cross-strategy consumers.
- DITP P2 scanner is now back to: technical filters only (mountain anchors, ATR distance, variant classifier, upper-tail) → proximity-first sort → watchlist write. No news lookup, no yfinance dependency on this path.
- M&A exclusion was zero-effect on today's shortlist anyway (all 10 names classified as `unknown` by yfinance), so reverting changes nothing about today's list — same 10 candidates, same order.

### 2026-05-23 — Scanner: M&A news overlay (exclude deal stocks) [SUPERSEDED, see entry above]
- User rule (chat 2026-05-23): *"DITP scanner also exclude merger and acquisition"*. Same rule already in force for GUNS per CLAUDE.md "Traps the user has flagged" — *"M&A names are dropped from intraday lists — an announced deal anchors price, no R:R left."*
- New `filter_ma_targets(candidates)` in `strategy/DITP/scanner.py` calls `resources/yfinance_news.classify()` on each technical candidate (NOT the 1500-symbol universe — only the ~10 that pass detect_p2). Drops names whose recent-headline `category` is `ma_target` or `ma_generic`.
- Other GUNS BAD categories (offerings, fraud, SEC actions, FDA rejections) are NOT applied here — they're tuned for the gap-up news scalp and may over-filter for DITP's longer-horizon setup. If we want them later, add via category list, not by classification == "bad".
- `scan_universe()` signature changed: now returns `(kept, ma_dropped)`. Sole external caller (`main()` in same file) updated. Wire in `--no-news-filter` CLI flag for debug.
- `write_watchlist()` JSON now carries `ma_dropped` array for audit (headline + url + category + confidence + pub_date). Empty array when nothing dropped.
- `resources/yfinance_news.py` docstring rewritten to legitimize cross-strategy use; module is no longer GUNS-exclusive. Only the M&A categories cross over; other categories stay GUNS-local.
- Soft-fail: if yfinance import / network fails, the filter prints a stderr warning and keeps all candidates. Technical filters are primary defence; news is belt-and-braces.
- Current run: 10 technical candidates, **0 M&A drops** — none of GS/NVRI/PLD/CTRE/WSR/DOC/CENTA/LYV/FN/MCW have an M&A headline (they're structural plays, not deal stocks). Filter is verified active.

### 2026-05-23 — Scanner: proximity to resistance is the primary ranking key
- User rule (chat 2026-05-23): *"if the current candle near the immediate preceding mountain top resistance, then rank the first for P2 setup as priority"*.
- `scan_universe()` sort key changed from `(tier, -score, distance_atr)` → `(distance_atr, tier, -score)`. Tier + score stay as tiebreakers so the user can still read structural conviction at a glance.
- Header label updated to `"sorted by distance > tier > score"`.
- Reorder on the live shortlist: **GS** (A/A, distATR 0.10) jumps from #5 → **#1**; **MCW** (A/C, distATR 1.35) drops from #2 → #10. The actionability story now matches what fires first in the tape — a 0.10-ATR setup will break out before a 1.35-ATR one regardless of validation score.
- D-tier candidates would still be excluded from the orchestrator's `.txt` watchlist via the existing `write_watchlist()` filter (`if c.tier != "D"`), so the proximity-first sort can't promote weak setups into the trade list.

### 2026-05-23 — Scanner: multi-mountain is a conviction modifier, NOT an eligibility gate
- User clarification (chat 2026-05-23): *"on DITP P2 variant A, the multiple mountain top requirement is only add more conviction to the setup"*. Reverses the hard `min_range_mountains = 2` gate added the prior turn alongside the A/B/C variant rewrite.
- `detect_p2()` no longer returns `None` when `n_range_mountains < 2`. The hard-gate block was deleted; `cfg.min_range_mountains` default lowered to `1` (effectively no gate — every candidate has ≥1 mountain anchor by construction). Field kept for back-compat / future re-tightening.
- Multi-mountain is still **rewarded** in `score_candidate()`: `range_conv` adds +6 (≥3 mountains) / +3 (2) / 0 (1) to the validation component. Single-mountain candidates also carry the `SINGLE_MOUNTAIN` caution which downgrades them out of A-tier when paired with another caution.
- Shortlist count: **4 → 10** at the same data snapshot. Restored: **PLD A/B-78** (single mountain — the canonical TSLA-vs-PLD ceiling example), **GS A/A-75** ("GS qualify" per earlier user judgment), **MCW A/C-82**, **WSR B/B-80** (fresh resistance), **FN B/A-65**, **DOC C/B-48** (with `FLUSH_UP` caution intact).
- No code-flow change for the 4 that survived under the old gate (NVRI, CTRE, CENTA, LYV) — same tiers, same scores.

### 2026-05-22 — Scanner v0.7: resistance range = MOUNTAIN CONSENSUS only
- User refinement (chat): "arguably 168 as a consensus resistance with one outlier false breakout to $173" — non-mountain wicks are NOT part of the resistance, even when they sit inside the band. The resistance zone is purely the mountain consensus.
- Behavioral change in `find_resistance`: `range_low` / `range_high` now derived from `range_mtns_only` (mountain-qualifying peaks in band), not from all swings. Non-mountain swings within band still count as cluster touches (1% band) but don't define the range.
- **LYV** range tightened **[167.56 → 169.91] → [167.56 → 168.55]** (the 169.91 swing is no longer part of the resistance; the 173.12 outlier was already outside the band). The breach check now compares closes to 168.55: -5d 169.99 above → 4 days ago → outside grace → still rejected breakout, still P2 ✓.
- **PLD** range tightened **[145.34 → 145.44] → [145.44 → 145.44]** (the 145.34 non-mountain swing dropped from range).
- Other candidates unchanged (TRV/DAL/SPG already had mountains-only consensus ranges; GS/PLD/MPWR single-mountain).
- Final shortlist unchanged: 9 candidates, same tiers.

### 2026-05-22 — Scanner v0.6: resistance is a RANGE; breach rejection grace
- User rule (chat): "resistance is not a strict price level but a range" — multiple mountain tops within a tight band form a resistance zone with more conviction. LYV is the canonical example: 167.56 / 168.54 / 168.55 mountains within 2% of each other.
- `find_resistance` now returns `(level, touches, mountains_in_cluster, range_low, range_high, n_range_mountains)`. The range is built from all swing highs (mountain or non-mountain) within ±2% of the preceding mountain. `range_low` drives the distance check; `range_high` is the breakout trigger.
- `P2Candidate` gained `resistance_low` + `resistance_range_mountains`. Output shows the full zone (`rng_low → rng_high`).
- Scoring component "validation" gains a `range_conv` term: +3 for 2 range mountains, +6 for ≥ 3. TRV (4/4/4) and LYV (4/3/3) benefit; DAL gets +6 from its 3 range mountains too.
- **Breach rejection grace period** — recent close above resistance now uses a 2-day grace window. LYV's -4d close at 169.99 (3 days ago, then 3 consecutive closes below) is a rejected breakout; setup stays valid. PM's -2d close at 191.57 (1 day ago) is active; PM remains excluded (graduated to P3).
- **Ceiling gate uses MAX MOUNTAIN** (not max swing): recent non-mountain wicks no longer falsely disqualify a valid mountain-anchored level. LYV passes now (max mountain in window IS 168.55).
- Behavioral changes vs v0.5:
  - **LYV in** at Tier B with score 64 — "under the radar" exactly as user specified.
  - **DAL** improved to Tier B (range_low 74.19 makes distATR 0.03, almost touching).
  - **TRV** moved A→A but score 71→75 (range conviction bonus).
  - **SPG** moved C→B (range conviction).
- Final shortlist (9): GS (A 75), PLD (A 75), TRV (A 75), DAL (B 67), SPG (B 67), LYV (B 64), MPWR (B 62), AMAT (C 51), DOC (C 45).

### 2026-05-22 — Scanner v0.5: resistance level = climax of preceding MOUNTAIN
- User rule (chat, rephrased): "immediate high above current means the climax of the **preceding** mountain." Walking backward from the current bar, the first mountain-qualifying peak above current price IS the resistance. "Preceding" = the mountain that immediately precedes the current price action in time. Recent unvalidated swing highs (e.g., a 7-day-old bump with no pullback) do NOT anchor a resistance line — only mountain-qualifying swings do (age ≥ 15 daily bars + price subsequently dropped ≥ 2 × ATR14 below the peak).
- **Fallback** when no mountain exists above current price: use the most recent swing high above current. Fires `FRESH_RESISTANCE` caution automatically since no mountain anchor is in the cluster. Covers DOC's pattern (flushed through prior peaks to fresh territory at 19.91, no mountain above 19.66 since the historical mountains are at 17–18).
- Behavioral changes from v0.4 on the 2026-05-21 S&P 500 snapshot:
  - **PLD** level **145.34 → 145.44** (the 23d-old mountain, not the 7d-old fresh swing). Score 81 → 75, still Tier A.
  - **DAL** **74.97 → 75.02**; **SPG** **206.46 → 208.28** (both move up to the older mountain).
  - **MPWR** back in the shortlist at Tier B (its mountain at 1661.79 is within distance cap).
  - **SNPS** dropped (immediate mountain 525.49 is 1.74 × ATR away — beyond max_distance_atr=1.5).
  - **DOC** stays at 19.91 via the fallback (FRESH_RESISTANCE caution still fires).
- Final shortlist (8): GS (A 75), PLD (A 75), TRV (B 68), MPWR (B 62), DAL (C 56), SPG (C 54), AMAT (C 51), DOC (C 45).

### 2026-05-22 — Scanner v0.4: resistance level = immediate high on the left
- User rule (chat): the P2 resistance value is the **IMMEDIATE swing high above current price** (most recent in time), not a clustered/averaged level. Cluster around that immediate level still counts for touches + mountain-anchor validation.
- Rewrote `find_resistance()`: instead of iterating per-anchor and picking the lowest-level cluster that passes all gates, now simply finds `max(swings_above_current, key=index)` and validates with the ceiling + cluster-touch + mountain-count checks.
- Behavioral changes from v0.3 on the 2026-05-21 S&P 500 snapshot:
  - **PLD** moves to Tier A score 81 (was 75): the 145.34 cluster now correctly includes 143.95 + 145.44, yielding 3 touches / 2 mountains.
  - **SPG** moves C→B: 4 touches / 2 mountains now.
  - **TRV** resistance changes 308.98 → 311.58 (the immediate above 306.96); distATR widens; tier stays B.
  - **LYV** dropped: immediate-above (173.12) is 1.72 × ATR away, beyond the max_distance_atr=1.5 gate.
  - **DAL** resistance changes 75.02 → 74.97.
  - **CB** stays rejected (immediate 334.97 fails ceiling gate vs 345.67 peak).
- Final shortlist (8): PLD (A 81), GS (A 75), TRV (B 68), SPG (B 65), DAL (C 56), SNPS (C 55), AMAT (C 51), DOC (C 45).

### 2026-05-22 — Scanner v0.3: P2 "already broken" gate; P3 pattern documented
- New eligibility gate in `detect_p2`: if any daily CLOSE in the last 15 bars exceeded `resistance + 0.1 × ATR14`, the symbol is excluded from P2 — it has already broken out and is now a candidate for **Setup 3 (P3 — retest of broken resistance)**. Uses closes (not intraday highs) so a tiny wick through resistance doesn't disqualify a still-pending setup. PM 2026-05-22 dropped as expected (closes 191.86/191.50/191.57 above 191.30 resistance); TRV and LYV both stayed in P2 (no closes above their resistances).
- New config knobs: `recent_breakout_lookback = 15`, `recent_breach_atr = 0.1`.
- **P3 pattern** documented in `strategies-reference/DITP.md` §4 + §6 with PM as the worked example. P3 scanner not yet built — design is sibling to `scanner.py` and reuses the P2 scanner's "already broken" criterion as its inclusion gate. Trigger / stop / TP / sizing TBD per user spec.

### 2026-05-22 — Scanner v0.2: cluster-level bug fix + flush-up bar caution
- **Bug fix in `find_resistance`**: the cluster level was being computed as `mean(cluster_prices)`, which dropped GS from the shortlist (cluster around 984.70 averaged to 980.18, below the current close 982.12). Now uses `max(cluster_prices)` — the actual ceiling. GS returns to Tier A with score 75.
- **`FLUSH_UP` caution redefined**: was naive "signal candle range > 1.5 × ATR" (which missed DOC entirely because DOC's signal candle is normal-sized). Now detects a **flush-up BAR** anywhere in the last 15 daily bars — bullish body > 1.5 × ATR AND close > max(prior 30 bars' highs). Captures DOC's -11d explosion (body 4.82 × ATR, broke prior high 17.43 by ~12%) — exactly the profit-taking-risk pattern the user flagged.
- New `find_flush_up_bar()` helper. Diag fields `flush_up_bar_offset_days` + `flush_up_bar_body_atr` populated when fires.
- Validation set on 2026-05-21 S&P 500 snapshot now matches user's hand-picked calls exactly: GS (A), PLD (A), TRV (B "under the radar"), DAL (C), DOC (C with FRESH_RESISTANCE + FLUSH_UP + BIG_TAIL). See `strategies-reference/DITP.md` §6.5.

### 2026-05-22 — P2 scanner v0.1 + DITP-specific ranking guideline
- New `scanner.py` (~300 LOC). Reads `data/price_history/daily/<SYM>.parquet`, applies §6 P2 eligibility (EMA stack, mountain-anchored resistance with real-ceiling check, no-upper-tail signal candle, pending breakout), classifies Setup A/B/C by 10-bar slope analysis. All thresholds are ticker-relative per CLAUDE.md normalization rule (ATR multiples + scale-free slopes).
- Added a documented ranking layer (`score_candidate`): 5-component weighted score 0–100 + 5 caution flags + 4-tier mapping. Source: `strategies-reference/DITP.md` §6.5.
- Validated against the user's hand-picked PLD / DAL / TRV / DOC examples on the 2026-05-21 S&P 500 daily snapshot: PLD = Tier A (86); TRV = Tier B (73, "under the radar"); DAL = Tier C (56); DOC = Tier C (45, FRESH_RESISTANCE + BIG_TAIL captures the "flush up" caution).
- Watchlist output: `state/watchlist_ditp_<tomorrow>.txt` (line per non-D-tier symbol with tier + variant + resistance) plus `state/watchlist_ditp_<tomorrow>.json` (full per-candidate metadata for dashboard / review).
- Designed for nightly run post-EOD ingest. Orchestrator hook not wired yet (manual CLI for v0.1).
- `bars_store.load_bars()` is the only data-access path; this scanner does not touch IBKR/Alpaca, so it can run safely while the live bot is going.

### 2026-05-22 — Family folder scaffolded
- Created `__init__.py` + `_helpers.py` + this README.
- Created sibling `../strategies-reference/DITP.md` as a 12-section skeleton (all TBD until source material is filled in).
- No setups wired yet. `KNOWN_STRATEGIES` in `strategy/__init__.py` is NOT touched until the first setup leaf folder exists.
- No `cfg.strategies.<name>` block in `config.example.json` yet — added when the first setup is wired.
- No `scanner.py` yet — added if DITP needs its own universe builder (decided per source material).
