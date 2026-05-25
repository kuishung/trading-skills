# DITP — reference

> **STATUS: in progress.** Setup 1 (P2 Pattern) eligibility filled in
> from chat 2026-05-22. Trigger / stop / TP / sizing still TBD. Setups 2
> and 3 not yet taught.
>
> Every change to a `strategy/DITP/<setup>/impl.py` must cite the section
> here it implements (e.g.
> `"""Source: strategies-reference/DITP.md §6 Setup 1 — P2 Pattern"""`).

---

## 1. Source attribution

- **Author / course:** **Beyond Insights** (watermark on the Setup A reference chart). Specific instructor / lesson TBD.
- **Source path / URL:** TBD
- **Date taught:** 2026-05-22 (Setup 1 eligibility + Setup A image; rest pending)
- **Vendored material:** Setup A reference chart was pasted inline in chat on 2026-05-22. Once a copy is saved to disk, vendor it to `intraday-bot/strategy/DITP/Materials/P2_setup_A.png` (gitignored, Dropbox-only — same convention as `strategy/GUNS/Materials/`).

## 2. Methodology type

- **Type:** Hybrid — mechanical screening (EMA stack, candle anatomy) + discretionary pattern recognition (range count, ascending triangle quality).
- **Time horizon:** TBD (likely day-trading off DAILY chart context — explicitly mentioned EMA20/50/200 daily)
- **Instruments:** TBD (implied US equities)
- **Direction:** Long-only confirmed for Setup 1 (breakout above resistance)

## 3. Top-level rules

TBD. Per-framework discipline statements once all 3 setups are taught.

## 4. Pattern / setup catalog

The DITP framework teaches THREE setups; **Setup 1 (P2 Pattern)** itself
has three sub-variants (A / B / C) that share eligibility but differ in
consolidation shape.

| # | Name | Trigger time | Pattern (one-line) |
|---|------|--------------|-------------------|
| 1A | **P2 Pattern — Setup A** (Direct approach) | Day of breakout (intraday tape watch) | Daily uptrend + horizontal resistance + last daily candle pushes cleanly into resistance with *no upper tail*, no prior consolidation needed |
| 1B | **P2 Pattern — Setup B** (Tight range) | Day of breakout (intraday tape watch) | Daily uptrend + horizontal resistance + ~8–15 recent daily candles form a *tight horizontal rectangle* just below resistance, last candle has no upper tail |
| 1C | **P2 Pattern — Setup C** (Tightening / ascending triangle) | Day of breakout (intraday tape watch) | Daily uptrend + horizontal resistance + ~8–15 recent daily candles form an *ascending triangle* (flat top at resistance + rising lows), last candle has no upper tail |
| 3 | **P3 Pattern** (Retest of broken resistance) | Day of retest support hold | Symbol previously had a P2 setup; breakout above resistance HAPPENED; now pulling back to retest the broken resistance from above. Entry on confirmation the level is holding as support. |
| 2  | **P1 Pattern** (TBD) | TBD | Rebound-type setup. Not yet taught in detail; reserved as Setup 2 per user 2026-05-25. |
| 4  | **TC** (Trend Continuation) | Day +1 / Day +2 after a P2 breakout or strong P1 rebound | React-don't-anticipate. The 1-2 daily candles after a confirmed breakout / rebound typically continue the trend. TC scanner surfaces yesterday's qualifying events; entry the following morning. |

## 5. Key level hierarchy

- **EMA20 (daily)** — short-term trend
- **EMA50 (daily)** — intermediate trend
- **EMA200 (daily)** — long-term trend
- **Horizontal resistance (daily)** — the level the consolidation forms below; setup fires on breakout above it

## 6. Entry / exit rules per setup

### Setup 1 — P2 Pattern (Breakout)

**Source: chat 2026-05-22 (user dictation) + three Beyond Insights chart images for sub-setups A / B / C.**

**Shared eligibility (all three sub-setups must satisfy):**

1. **EMA stack on daily chart.** EMA20 (red), EMA50 (green), EMA200 (magenta) plotted. Mechanical interpretation: **EMA20 > EMA50 > EMA200** AND price > EMA20. The three Beyond Insights charts all show this stack clearly across the visible range.
2. **Daily chart in overall uptrend.** Visually obvious across the reference charts (price ascends from lower-left to upper-right). Mechanical proxy = the EMA stack above + price slope; may add an explicit higher-highs-higher-lows check.
3. **Horizontal resistance line on the daily chart, anchored to a true left-side mountain top.** A price level touched ≥ 2 times in the recent past without closing through.

   **Defining the resistance VALUE (user rule, 2026-05-22):** *"Immediate high above current means the climax of the preceding mountain."* Walking backward from the current bar, the **climax of the first MOUNTAIN-qualifying peak above current price** anchors the level. "Preceding" = the mountain that immediately precedes the current price action in time. Only mountain-qualifying peaks (age ≥ `mountain_min_age_bars` default 15 daily bars + price subsequently dropped ≥ `mountain_pullback_atr × ATR14` default 2.0 ATR) anchor a resistance line.

   **Fallback if no mountain above current** — DOC pattern. When price has broken through all prior mountains and is in fresh territory, the level falls back to the most recent swing high above current. Triggers `FRESH_RESISTANCE` caution downstream.

   **Resistance is a RANGE, not a strict price (user refinement, 2026-05-22):** *"there are multiple mountain tops that form a range of resistance. that is a resistance with more conviction."* Once the preceding mountain anchors the level, the resistance ZONE is the **consensus of mountain tops within `±resistance_range_pct` (default 2 %) of the anchor**:
   - `range_low` = lowest **mountain** in zone → drives the **distance-to-current** eligibility check (so a wide consensus qualifies as long as its bottom is in reach).
   - `range_high` = highest **mountain** in zone → the **breakout trigger** (price has to clear the top of the mountain consensus for the breakout to count).
   - `n_range_mountains` = number of mountain peaks in the zone → drives a **conviction bonus** in §6.5 scoring. ≥ 3 in-zone mountains adds significant validation.

   **Non-mountain wicks are NOT part of the consensus (user refinement, 2026-05-22):** *"for LYV, arguably 168 as a consensus resistance with one outlier false breakout to $173."* Recent non-mountain swing highs that sit above or inside the consensus zone are NOT considered part of the resistance — they are *outlier excursions* representing failed breakouts (or pending validation that hasn't happened yet). LYV: the consensus is 167.56 / 168.54 / 168.55 (3 mountains). The 169.91 swing (age 10d) and the 173.12 spike (age 4d) are outliers above the consensus; both are wicks that failed to hold above the mountain top. The breach-check uses `range_high = 168.55`; the 169.99 close at -5d that exceeded it is then evaluated against the grace period (rejected if older than 2 days).

   **Real-ceiling gate uses max MOUNTAIN, not max swing.** A recent non-mountain wick above the chosen level does NOT disqualify — only a higher mountain-qualifying peak does. LYV: 173.12 wick (age 4d, not a mountain) doesn't fail the 168.55 mountain's ceiling check; 168.55 IS the highest mountain in window. TSLA: 452 mountain overhead does kill the 418 cluster.

   **User-validated 2026-05-22 against S&P 500 daily snapshot:**
   - **GS** zone = 984.70 (single mountain, well-defined ceiling). Tier A.
   - **PLD** zone = 145.34 → 145.44 (preceding mountain + minor non-mountain swing in band). Tier A.
   - **TRV** zone = 308.98 → 313.12 (4 mountains in zone — high conviction). Tier A.
   - **LYV** consensus zone = 167.56 → 168.55 (3 mountains). The 169.91 and 173.12 are outliers above the consensus, not part of the resistance. Tier B "under the radar". The -5d close at 169.99 (which exceeded 168.55) was the rejected breakout that fired the grace-period exception.
   - **DOC** zone = 19.91 (fallback, no mountain above). Tier C with FRESH_RESISTANCE + FLUSH_UP + BIG_TAIL.

   **Recent-breakout exclusion has a grace period** — a daily close above the resistance only disqualifies (graduates to P3) if it occurred within the last `breach_rejection_grace_days` (default 2) daily bars. Older breaches that have been rejected by subsequent closes back below count as failed breakouts; the setup stays valid P2. LYV's -4d close at 169.99 (4 days ago) = rejected. PM's -2d close at 191.57 (1 day ago) = active breakout, graduated to P3.

   **Qualifying that resistance as REAL** (refinement also from 2026-05-22):
   - (i) **The level must be the actual ceiling** — there must be no significantly higher swing in the lookback window. Mechanically: `level ≥ (1 - max_below_window_high_pct) × max_swing_high`, default 98 %. Filters out cases like TSLA where the immediate-above-current swing (453.40) sat between current price and a still-higher peak in a downtrend — the immediate isn't the real ceiling there.
   - (ii) **The level must have ≥ `min_touches` (default 2) swing highs clustered within ±`cluster_band_pct` (default 1 %).** Single isolated swing highs aren't reliable resistance — they need validation by other touches in the same price band.
   - (iii) **The level must NOT have been broken recently** — see eligibility check below.

   **Mountain-anchor status drives ranking, not eligibility.** A swing high qualifies as a "mountain top" only if it (a) sits ≥ `mountain_min_age_bars` (default 15) daily bars in the past, AND (b) price subsequently dropped ≥ `mountain_pullback_atr × ATR14` (default 2.0 ATR) below it. Mountain anchors among the cluster touches drive the **scoring + caution flags** (§6.5) — more mountain anchors → higher tier; zero mountain anchors → FRESH_RESISTANCE caution.

   **User-validated against the 2026-05-21 daily snapshot of the S&P 500:** **PLD** resistance = 145.34 (immediate above current; cluster of 143.95 + 145.34 + 145.44, two of which are mountains); **GS** = 984.70 (immediate above 982.12, only one swing above current); **TSLA rejected** (immediate-above 453.40 is the chart's max swing so passes ceiling, but distance 2.1 × ATR > max_distance_atr disqualifies); **PM excluded** (closes 191.86 / 191.50 / 191.57 already broke 191.30 — P2 graduated, candidate for P3).
4. **Last daily candle has NO UPPER TAIL.** Hard rule — the candle pushes cleanly into resistance and closes at/near its high. The annotation in every Beyond Insights chart calls this out explicitly ("No tail above. (no hesitation near resistance)"). Ideal candle types: bullish markup bar, hammer, pin bar. **Shooting star is disqualifying.**
5. **Current state = pending breakout.** The signal candle is the one *before* the actual close-above-resistance. Setup IDENTIFIES the candidate; ENTRY is when price actually breaks above.

**The three sub-setups differ ONLY in the consolidation shape leading into the signal candle:**

#### Setup 1A — Direct approach

- **No consolidation required.** Price oscillates through prior swings then makes a clean, unhesitating final push into the resistance.
- Inset schematic: oscillating waves rising into the line, with the *final* wave going straight up without a pullback.
- Reference image: Beyond Insights "Higher Probability Breakout Setup: A".
- Mechanical detection: skip the consolidation check entirely; rely on the EMA-stack + uptrend + no-upper-tail rule + proximity-to-resistance.

#### Setup 1B — Tight range (rectangle)

- **~8–15 recent daily candles** (discretionary count) form a *narrow horizontal rectangle* immediately below resistance.
- High-low band of the rectangle is significantly smaller than the symbol's typical daily range (mechanical proxy: rectangle height < 0.5 × ATR14, or rectangle high − rectangle low < N% of price — TBD on the exact N).
- Inset schematic: oscillating waves of *decreasing* amplitude under the resistance line.
- Reference image: Beyond Insights "Higher Probability Breakout Setup: B" — large rectangle annotation on the right side of the chart.
- Mechanical detection: linear-regression slope of recent highs ≈ 0 (flat), slope of recent lows ≈ 0 (flat), range height small relative to ATR.

#### Setup 1C — Tightening range (ascending triangle)

- **~8–15 recent daily candles** (discretionary count) form an *ascending triangle*: flat top at the resistance, rising lows along an upward trendline, converging into an apex right at the signal candle.
- Inset schematic: oscillating waves with both decreasing amplitude AND rising lows.
- Reference image: Beyond Insights "Higher Probability Breakout Setup: C" — two converging trendlines drawn on the right side.
- Mechanical detection: linear-regression slope of recent highs ≈ 0 (flat top), slope of recent lows > 0 (rising), and the two converge.

**Trigger (all sub-setups):** TBD. Two plausible mechanisms:
  - (i) **Buy-stop above resistance** — order rests at `resistance + 1¢` (or some small offset), fills on the intraday break.
  - (ii) **Daily close confirmation** — wait for a daily close *above* resistance, then enter next morning. Slower / more conservative.
The reference charts strongly imply (i): the yellow-circled candle IS the breakout day (the candle pushes through), not the day after a confirmed close. Pending user confirmation.

**Stop loss:** TBD. Natural placements:
  - Below the consolidation low (the rectangle low for Setup 1B, the most recent rising-trendline touch for Setup 1C).
  - Below EMA20 / EMA50 (a trend-based stop).
  - A fixed % of price.

**Take profit:** TBD. Common P2-pattern targets:
  - **Measured move** — project the height of the consolidation upward from the resistance line.
  - Next prior horizontal resistance.
  - Fixed R-multiple (consistent with risk_per_trade_pct convention).

**Concurrency cap:** TBD.

### Setup 4 — TC (Trend Continuation)

**Status: IN PROGRESS — capture began chat 2026-05-25 (Memorial Day pause).**
**Source: user dictation, no chart materials yet.**

**Concept.** Unlike P2 (anticipate the breakout) and P3 (trade the retest of a broken level), TC trades the **continuation strength of the 1-2 daily candles AFTER a confirmed breakout / rebound already happened**. You're not catching the breakout — you're riding the follow-through that typically prints on Day +1 / Day +2.

**Two qualifying Day-0 prior events:**

1. **P2 breakout** — symbol cleared mountain-consensus `range_high` on Day 0 close.
2. **Strong P1 rebound** — robust bounce off the P1 level. Definition pending P1 framework being taught (Setup 2 — see TBD above).

**Trade window:** Day +1 and Day +2 (the first two daily candles following the qualifying event).

#### Eligibility (captured so far — incomplete)

1. **Day 0 = P2 breakout day** (`close > range_high` for the mountain-consensus zone) **OR strong P1 rebound day** (definition TBD).
2. **Day 0 daily candle must be a bullish formation.** Working definition (pending user confirmation): `close > open` AND `close` in upper half of daily range. Filters out wicky "barely green with a long upper tail" candles.
3. **Day +1 premarket price action stays above Day 0 daily high (YH).** Strictness pending confirmation ("every premarket print" vs "premarket VWAP" vs "premarket low" vs "last print before 09:30 ET"). Intent: gap-and-hold — buyers maintained control overnight, so the breakout has real follow-through.

#### Entry trigger

TBD — to be taught.

#### Stop placement

TBD — to be taught.

#### Take profit / exit

TBD — to be taught.

#### Caution flags

TBD — to be taught.

#### TC scanner (architecture sketch)

TC needs its **own scanner** that runs at TWO phases (per user 2026-05-25):

1. **EOD scan, Day 0** (~16:15 ET): Read yesterday's `state/watchlist_ditp_<date>.json` (P2 watchlist), filter to symbols where Day 0 close > `range_high` AND Day 0 candle is bullish. Output: `state/watchlist_tc_<date>.json` (TC candidates pending premarket validation).

2. **Premarket scan, Day +1** (~09:00 ET / T-30 BMO): For each TC candidate, check premarket price action satisfies the "stay above Day 0 high" rule. Output: `state/shortlist_tc_<date>.json` (names eligible to fire at Day +1 open).

3. **Day +1 entry phase** (~09:31 ET): Strategy module evaluates entry triggers on the shortlist. Specific trigger TBD.

The scanner does NOT re-screen the universe — it ingests yesterday's P2 hits (and, once P1 is taught, yesterday's P1 rebound hits).

---

### Setup 2 — P1 (TBD)

Not yet taught.

### Setup 3 — P3 Pattern (Retest of broken resistance)

**Source: chat 2026-05-22 (user dictation, sparked by PM's behavior in the 2026-05-21 daily snapshot).**

**Core idea:** Once a P2 resistance is broken, it doesn't disappear — it inverts and acts as **support**. The cleanest entry is not at the breakout itself (which is often a fast move, high slippage, and risk of immediate reversal) but at the **retest**: price pulling back to test the former resistance from above. If the level holds as support, the breakout is *confirmed* and the original P2 thesis is now playing out from a much better risk/reward entry.

**Eligibility (all must hold):**

1. **Bullish EMA stack on daily** (same as P2 §6: EMA20 > EMA50 > EMA200, price > EMA20).
2. **Identifiable broken-resistance level** — a price level that:
   - Was previously a valid P2 resistance (anchored to a left-side mountain top OR a fresh peak, see §6 P2 eligibility).
   - Was BROKEN with conviction: at least one daily close > `resistance + N × ATR14` within the last `breakout_lookback` daily bars. (Mechanical thresholds TBD; for v0.1, reuse the P2 scanner's `recent_breach_atr = 0.1` definition of "breakout".)
3. **Price is now pulling back toward the broken level.** Current close is within `retest_distance_atr` of the broken resistance line.
4. **Retest is from ABOVE** — price went above and is now coming back, not coming up from below for a first touch. (`max(closes after breakout) > broken_level + 0.5 × ATR14` or similar.)
5. **Support-hold confirmation** — TBD. Common confirmations:
   - Bullish candle close at/above the broken level (hammer, bullish engulfing).
   - Multiple consecutive lows respecting the level (≥ 2 touches without closing below).
   - Bullish reaction bar with low at/near the level and close well above it.

**Trigger:** TBD (likely a buy-stop above the confirmation bar's high, or buy-on-close once the confirmation bar prints).

**Stop loss:** TBD. Natural placement: below the confirmation bar's low, or `0.5 × ATR14` below the broken level (since a meaningful close below would invalidate the "broken resistance is now support" thesis).

**Take profit:** TBD. Common P3 targets:
- Measured move = height from breakout to most-recent high projected up from the retest entry.
- Next prior horizontal resistance above.
- Fixed R-multiple.

**Worked example — PM (2026-05-22 daily snapshot):**
- Mountain-anchored resistance at **191.30** (from age 67d / 59d swing highs).
- Breakout: closes **191.86 / 191.50 / 191.57** in the last 7d, all > 191.30 + 0.1 × ATR14 (5.03 → buffer = 0.50, breach level 191.80).
- Now pulled back to close **188.63** (below the broken 191.30 — deeper retest than usual; could still set up if the larger 188 zone holds).
- Per the scanner's new "already broken" gate: PM is correctly excluded from P2; it's a P3 candidate.

### P3 scanner

NOT YET BUILT. Will be a sibling to `strategy/DITP/scanner.py` (or a flag on the same scanner) that:
1. Identifies symbols where a P2 resistance was breached in the recent past (the P2 scanner's exclusion criterion = the P3 scanner's inclusion criterion).
2. Verifies price has come back near the broken level.
3. Waits for support-hold confirmation.
4. Writes its own watchlist file (`state/watchlist_ditp_p3_<tomorrow>.txt`).

Spec is still partial — `retest_distance_atr`, support-hold mechanics, trigger / stop / TP all TBD per user spec.

## 6.5 Shortlist ranking guideline (DITP-specific)

Eligibility is binary: a candidate either passes the mechanical filters in §6 or it doesn't. **Ranking** is a separate scoring + caution layer that turns the pool of eligible candidates into a tiered shortlist. The orchestrator's entry phase reads the tier-tagged watchlist file and decides whether to act on B/C tier or only A tier (a config knob — TBD per user preference).

Implemented in `intraday-bot/strategy/DITP/scanner.py` → `score_candidate()`. Validated by chat 2026-05-22 against the user's hand-picked PLD / DAL / TRV / DOC examples.

### Five scoring components (weighted sum = 100)

| # | Component | Range | What it rewards |
|---|---|---|---|
| 1 | **Proximity** | 0–25 | `distance_atr ≤ 0.2` → 25; tapers to 10 at `distance_atr ≤ 1.5` |
| 2 | **Resistance validation** | 0–25 | More touches + more mountain anchors. Capped at 25 = (8 touches-base) + (15 mountains-base for ≥ 3). 0 mountains → only 2 points from this dimension. |
| 3 | **Signal candle anatomy** | 0–20 | `upper_tail_ratio ≤ 0.02` → 20; tapers to 6 at the 0.15 cutoff |
| 4 | **Trend strength** | 0–20 | Mean of (EMA20–EMA50)/ATR + (EMA50–EMA200)/ATR. Spread ≥ 3 ATR → 20; spread < 0.3 ATR → 2. |
| 5 | **Variant bonus** | 0–10 | Setup B (tight rectangle) = 10; C (ascending triangle) = 9; A (direct approach) = 7. Tighter coiled-spring patterns rank higher. |

### Five caution flags (annotations; do NOT subtract from score)

| Flag | Trigger | Meaning |
|---|---|---|
| `FRESH_RESISTANCE` | 0 mountain anchors in cluster | Resistance is a recent peak (last 15 days), not historically tested. Pattern is still valid but less validated. *DOC's situation: 5 historical mountains at 17–18, but the current resistance at 19.89 was formed 4–8 days ago.* |
| `FLUSH_UP` | A **flush-up bar** within the last 15 daily bars: bullish body > 1.5 × ATR14 AND close > max(prior 30 bars' highs) | The resistance was established (or is being approached) by a single strong upward bar that broke prior range. **Profit-taking risk**: traders who got long during or before that bar are sitting on quick gains and are likely to sell on any retest of the resulting resistance level. The flush-up may be days back (e.g., DOC: bar at -11d created the new resistance) or it could be today's signal candle itself. |
| `BIG_TAIL` | `0.10 < upper_tail_ratio ≤ 0.15` | The push showed rejection at the top. Borderline pass — eligibility threshold is 0.15, but anything > 0.10 is worth a warning. |
| `SINGLE_MOUNTAIN` | Exactly 1 mountain anchor | Weaker validation than 2+ mountains. Often fine; flagged so the tier reflects it. |
| `WIDE_BASE` | 10-bar rectangle height > 2 × ATR14 | Loose consolidation. Less of a coiled spring; more room for the breakout to be a one-day pop that fades. |

`FRESH_RESISTANCE` and `FLUSH_UP` are **major** cautions — they downgrade the tier hard. The other three are **minor** and only matter in aggregate.

### Tier mapping (the final sort key)

| Tier | Condition | Action |
|---|---|---|
| **A** | `score ≥ 75` AND no major cautions | Headline shortlist — bot can trade these on auto-arm. |
| **B** | `score ≥ 60`, OR `score ≥ 75` with ≤ 1 major caution | Solid candidates, "under the radar" picks. Bot can trade with reduced size or require human ARM. |
| **C** | `score ≥ 45` OR ≤ 2 total cautions | Watchlist but disarmed by default. Surface in the dashboard for human review. |
| **D** | Anything else | Kept in `.json` for review; omitted from the `.txt` watchlist that the orchestrator reads. |

Final sort: `tier (A first) → score (high first) → distance_atr (close first)`.

### Validation cases (chat 2026-05-22, S&P 500 daily snapshot 2026-05-21)

| Sym | User call | Scanner output | Match |
|---|---|---|---|
| **GS** | "qualify" | **A**, 75, SINGLE_MOUNTAIN + WIDE_BASE | ✓ in the headline shortlist |
| **PLD** | "valid setup" | **A**, 75, SINGLE_MOUNTAIN | ✓ |
| **TRV** | "valid but **under the radar**" | **B**, 71, WIDE_BASE | ✓ B-tier = "under the radar" |
| **DAL** | "valid setup too" | **C**, 56, SINGLE_MOUNTAIN + BIG_TAIL + WIDE_BASE | ✓ in the shortlist with multiple cautions |
| **DOC** | "valid but **flush-up bar** must be cautioned (profit-taking risk)" | **C**, 45, **FRESH_RESISTANCE + FLUSH_UP + BIG_TAIL** | ✓ all three cautions fire — FLUSH_UP catches the -11d bar (body 4.82 × ATR, close broke prior 30d high of 17.43); FRESH_RESISTANCE catches that the resulting 19.89 ceiling has no historical mountain anchor |
| TSLA | (rejected during ceiling-filter step earlier) | filtered out at eligibility | ✓ (had 452 peak overhead of 418 cluster) |

### Bug fix on the same day

`find_resistance()` originally computed the cluster level as the **mean** of cluster prices. For GS, the cluster around 984.70 included 975.66 (an unfilled recent gap fill), and the mean fell to 980.18 — *below* the current close of 982.12, so GS was wrongly dropped from the shortlist. Fixed by changing the level to **`max(cluster_prices)`** — the actual ceiling that price has to break. Lower swings in the cluster are still counted as "touches" for validation strength but don't redefine the level.

## 7. Stock screening criteria

**For Setup 1 (P2 Pattern):**

- Liquid US equity (price/volume floor TBD — probably mid/large-cap given the daily-trend reliance).
- Daily timeframe must be visually clean enough for human-interpretable resistance lines.
- The bot needs **daily bars + daily EMAs** to evaluate eligibility — already in `data/price_history/daily/<SYM>.parquet` for the S&P 500.

**Universe builder:** TBD. Probably a daily pre-screen run after each close that flags candidates for the next day's intraday breakout. Mechanically:

1. From the universe (S&P 500 or wider), filter to symbols where EMA20 > EMA50 > EMA200 AND price > EMA20.
2. Identify horizontal resistance candidates (price level with ≥ 2 touches, no close-through, in last ~30–60 daily bars).
3. Check: is current price within X% below resistance? (X TBD)
4. Confirm the last candle anatomy (no upper tail, ideally bullish markup / hammer / pin bar).
5. Optionally classify consolidation shape (tight vs ascending triangle vs direct approach).
6. Output: ticker + resistance price + suggested buy-stop trigger.

The pre-screen output becomes the next day's intraday watchlist; the orchestrator's entry phase watches the live tape for the actual breakout.

## 8. Catalyst guidance

TBD. (Likely simpler than GUNS — DITP appears to be a chart-technicals method, not a news-catalyst method. To be confirmed.)

## 9. Risk + sizing

TBD. Standard global risk rules still apply: `risk_per_trade_pct ≤ 1%`, `max_position_pct = 10%`. Per-setup sizing rules to be filled in.

## 10. What's DISCRETIONARY (resists mechanization)

For Setup 1 (P2 Pattern):

- **Number of consolidation candles** ("5–15") — user explicitly said this is a discretionary view. Mechanization: pick a default (e.g. 7) and expose as a config knob.
- **"Tight" vs "tightening" classification** — visual judgment. Mechanization candidate: standard-deviation of recent highs vs lows, or linear-regression slopes of swing highs vs swing lows.
- **Ideal vs acceptable candle types** ("bullish markup bar, hammer, pin bar" are *good*; the rule is "no upper tail" minimum). Mechanization: hard rule = "no upper tail above 30% of body"; soft rule = bonus weight for the named patterns.
- **Resistance line drawing itself** — discretionary in a human chart. Mechanization candidate: cluster of swing highs within a small price band.

## 11. Implementation status

| Setup | Status | Code | Notes |
|------|--------|------|-------|
| 1A — P2 Direct approach | spec partial (eligibility + 3 ref images) | scanner v0.2 | needs trigger / stop / TP / sizing spec |
| 1B — P2 Tight range     | spec partial (eligibility + 3 ref images) | scanner v0.2 | needs trigger / stop / TP / sizing spec |
| 1C — P2 Ascending triangle | spec partial (eligibility + 3 ref images) | scanner v0.2 | needs trigger / stop / TP / sizing spec |
| 2 | not yet taught | — | — |
| 3 — P3 Retest of broken resistance | spec partial (eligibility sketched) | not scaffolded yet | shares "broken resistance" criterion with the P2 scanner's exclusion gate; needs retest mechanics + trigger / stop / TP spec |

## 12. Glossary

- **P2 Pattern** — DITP's name for the daily-chart breakout setup (Beyond Insights). Has three sub-variants A / B / C.
- **Setup A** — direct approach; no consolidation required, signal = clean push into resistance with no upper tail.
- **Setup B** — tight range; ~8–15 daily candles bunched in a narrow horizontal rectangle below resistance.
- **Setup C** — tightening range / ascending triangle; ~8–15 daily candles with flat top at resistance + rising lows.
- **EMA stack** — the relative ordering of EMA20 / EMA50 / EMA200. "Bullish stack" = EMA20 > EMA50 > EMA200, with price > EMA20.
- **Bullish markup bar** — strong-bodied bullish candle that closes near its high, no upper tail.
- **Hammer** — long lower wick, small body at top of range, no upper tail (or very small).
- **Pin bar** — single-bar reversal; for a bullish pin bar approaching resistance the relevant feature here is small body and minimal upper tail.
- **Shooting star** — bullish candle with a long upper tail (rejection at the high). **Disqualifying** for the P2 pattern: "the last daily candle formed without tail on top" rules this out.
- **Ascending triangle** — flat top + rising lows. Bullish continuation pattern when the flat top is the prior resistance.
- **Signal candle** — the LAST daily candle of the eligibility pattern, with no upper tail, sitting AT the resistance. This is the candle annotated in every Beyond Insights reference image; it precedes the actual breakout.

---

## Convention reminders (from CLAUDE.md)

- Strategy code MUST cite the source: `"""Source: strategies-reference/DITP.md §6 Setup N"""` in each `impl.py` top docstring.
- Never blend DITP rules into a GUNS file (or vice versa). Hybrid setups must annotate each rule's origin.
- Every rule edit bumps `__version__` in the setup's `impl.py` and adds a dated entry to that setup's README changelog.
