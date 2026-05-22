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
