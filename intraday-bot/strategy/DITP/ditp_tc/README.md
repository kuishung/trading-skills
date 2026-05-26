# strategy/DITP/ditp_tc/ — TC (Trend Continuation) setup

Source: `strategies-reference/DITP.md` §6 Setup 4 (capture began chat 2026-05-25).

DITP Setup 4 — trades the **1–2 daily candles AFTER** a confirmed P2 breakout (or, once Setup 2 lands, a strong P1 rebound). You're not catching the breakout itself — you're riding the follow-through that typically prints on Day +1 / Day +2.

## Contents

- `__init__.py` — re-exports `build` + `__version__`
- `impl.py` — strategy module (`pick_universe` / `fetch_bars` / `evaluate` / `do_shortlist` / `build`)

The EOD Day-0 TC scanner lives one level up at `strategy/DITP/tc_scanner.py` — family-level sibling of `scanner.py` (the P2 scanner), since both produce DITP watchlists.

## Status

**v0.1.0 — WATCH-ONLY.** Wires TC into `KNOWN_STRATEGIES` so it appears in:
- the Gating drawer (ON/OFF + ARM toggles)
- the Active Lists Candidates tab when ARMED
- the Strategy Analysis drawer (via `strategy.ditp_tc.*` journal events)

`evaluate()` returns **None** — no plan submitted, ARM is a no-op until Phase 3 lands. By design: trigger / stop / TP / cautions for Day +1 entry are still TBD in DITP.md §6 Setup 4 and need user teaching before any code can be written for them.

## What's wired (Phase 1 — this version)

| Piece | Status | Where |
|---|---|---|
| EOD Day-0 scan: filter yesterday's P2 watchlist to actual breakouts that closed bullish | ✅ | `strategy/DITP/tc_scanner.py` |
| Watch-only orchestrator wiring (journal events, no plan) | ✅ | `impl.py` v0.1.0 |
| KNOWN_STRATEGIES registration + import-path mapping | ✅ | `strategy/__init__.py` |
| Config block `cfg.strategies.ditp_tc` (first-run seed) | ✅ | `config.example.json` |
| Gating drawer ON/OFF + ARM controls | ✅ (auto) | dashboard's existing auto-surface |
| Strategy Analysis drawer DITP tab picks up TC events | ✅ (auto) | dashboard's existing auto-surface (event name → family tab) |

## What's deferred (TBDs that need user teaching)

| Gap | Where to fill in |
|---|---|
| **P1 rebound as alternative Day-0 qualifying event** | requires Setup 2 (P1) to be taught — DITP.md §4 lists Setup 2 = TBD |
| **Premarket Day+1 "holds above Day-0 high" strictness** — every print? VWAP? PM low? last print before 09:30? | DITP.md §6 Setup 4 Eligibility 3 |
| **Day +1 entry trigger** — buy-stop above Day-0 high? close above intraday VWAP? something else? | DITP.md §6 Setup 4 "Entry trigger" |
| **Stop placement** | DITP.md §6 Setup 4 "Stop placement" |
| **Take profit / exit** — fixed R-multiple? measured move? trailing? | DITP.md §6 Setup 4 "Take profit / exit" |
| **Caution flags** — what de-rates a TC candidate? | DITP.md §6 Setup 4 "Caution flags" |
| **Day +2 carryover** — re-fire if Day +1 didn't trigger? | DITP.md §6 Setup 4 "Trade window: Day +1 and Day +2" — currently Phase 1 only emits Day +1 |

## Changelog

### 2026-05-26 — v0.1.0 — TC scaffolded + EOD Day-0 scanner wired

First wire-up of the TC setup. Mirrors the same phased-build pattern that DITP P2 used (commit history: 4a3f9c4 P2 v0.1 watch-only → d3d3be5 confluence-tier filter → c05ee47 backtest adapter).

**Phase 1 scope of this commit:**

1. **`tc_scanner.py` (family-level, sibling of `scanner.py`).** Walks the most recent `state/watchlist_ditp_*.json` and applies the Day-0 filters captured so far in DITP.md §6 Setup 4:
   - Today's daily close > P2 candidate's `resistance` (= `range_high`, the mountain-consensus top). The breakout actually fired.
   - Today's daily candle is bullish: `close > open` AND `(close - low) / (high - low) >= 0.5` (close in upper half of range). Filters wicky/barely-green breakouts.
2. **TC candidate output** carries forward enough P2 metadata (variant, tier, score, resistance, confluence, cautions, EMAs, ATR14, yesterday's D/E/F levels, universes) that downstream consumers don't need to re-read the P2 watchlist. Plus Day-0 specifics: `day0_close`, `day0_open/high/low`, `day0_close_position` (0-1 normalized), `breakout_strength_atr` ((close − R)/ATR — how cleanly the close cleared resistance).
3. **`watchlist_tc_<tomorrow>.txt`** mirrors the P2 `.txt` format (drops D-tier + Tier-0 confluence) so the orchestrator's entry pipeline applies a consistent filter across DITP setups. `.json` keeps every candidate for review.
4. **`impl.py` v0.1.0** — watch-only. `evaluate()` returns None; `do_shortlist()` journals `watchlist_loaded` with per-tier + per-event counts. Source-event field is `p2_breakout` today; ready for `p1_rebound` once Setup 2 lands.
5. **Sort key** = `(-breakout_strength_atr, tier, -score)`. Cleanest breakouts first — those are the highest-conviction continuation candidates. Tier + score are tiebreakers.
6. **`config.example.json`** adds the `ditp_tc` block alongside `ditp_p2`. `enabled: false` first-run seed (live state lives in the flag file).
7. **`strategy/__init__.py`** registers `ditp_tc` in `KNOWN_STRATEGIES` + the import-path mapping.

**Why phased build (matching P2's pattern):** the TC framework taught 2026-05-25 was incomplete — premarket strictness, entry trigger, stop, TP, and cautions are all TBD. Phase 1 ships the part that IS fully specified (the Day-0 filter + scanner output) so the watchlist starts producing data immediately. The Phase 2 (premarket validation) and Phase 3 (live entry) builds wait on the user filling in the gaps.

**Dashboard visibility (per CLAUDE.md "Dashboard visibility rule"):** auto-surfaces are sufficient for v0.1 — the Gating drawer + Strategy Analysis drawer pick TC up automatically once `strategy.ditp_tc.*` events flow. A dedicated `/strategy/ditp/tc_watchlist` endpoint + frontend section showing the TC candidate table (Day-0 breakout strength, P2-variant inheritance, the tomorrow-trade indication) is the natural next turn's work per the rule's "UI catches up next turn" allowance.

**No live-bot risk** — `evaluate()` returns None regardless of ARM. Even if the user toggles ARM, no order is submitted. Safe to run with the live bot.
