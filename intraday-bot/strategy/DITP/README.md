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

## Status of the DITP setups

| Setup | Trigger | Status |
|---|---|---|
| TBD | TBD | not scaffolded — awaiting strategies-reference/DITP.md §4 Setup catalog |

## Convention reminders (from CLAUDE.md)

- Strategy code MUST cite the source: `"""Source: strategies-reference/DITP.md §6 Setup N"""`.
- Never blend DITP rules into a GUNS setup file (or vice versa). Hybrid setups annotate each rule's origin.
- Every rule edit bumps `__version__` in the setup's `impl.py` and adds a dated entry to that setup's README changelog (which IS the version history — there's no separate `changelog.md` per CLAUDE.md).

## Changelog

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
