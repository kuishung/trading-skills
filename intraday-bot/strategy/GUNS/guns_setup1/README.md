# GUNS Setup 1 — Break of Pre-Market High at 09:30 ET

Source: Adam Khoo Piranha Profits, Lesson 8 (Gap Up News Scalp).
Reference doc: `strategies-reference/GUNS.md`.

Single-shot entry at 09:30 ET. Fires a buy-stop-limit at PMH + $0.01
if the last 15 minutes of PM bars consolidated within `consol_band_pct`
(default 1.5%) of the PMH. Stop = price-tier table. TP = 2R.

## Contents

- `__init__.py` — Re-exports `build` and `__version__` from `impl.py`.
- `impl.py` — `__version__`, `build(cfg)`, `evaluate(symbol, bars, strat)`, `pick_universe`, `fetch_bars`.

## Versioning policy

Every change to the trigger rules, sizing, stop placement, take-profit
multiple, or eligibility filters bumps `__version__` in `impl.py` and
adds a Changelog entry below. Trade journal events carry this version
so post-trade analytics can attribute outcomes to a specific rule-set.

Bump rules:
- **MAJOR** — incompatible plan-dict shape change or fundamentally different entry/exit logic.
- **MINOR** — new gating filter, new entry condition added.
- **PATCH** — parameter retuning (consol_band_pct, lookback, etc.), bug fixes, journal field additions.

## Changelog

### 1.1.0 (patch) — 2026-05-21 — Version-tag the plan dict
- `impl.py`: After `build_long_buy_stop_limit_plan()`, set `plan["strategy_version"] = __version__` so downstream journal events (`planned`, `entry_submitted`, etc.) carry the version. Enables `review/stats.py` to attribute outcomes to a specific rule-set when buckets are recomputed.
- No behavior change; pure metadata enrichment for the enrichment program.

### 1.1.0 — 2026-05-21 — Shortlist phase 30 min BMO

**Why:** The user wants Setup 1 detection to START 30 minutes before market open so candidates are gathered + journaled across the morning, not just snapshot at the order-placement moment. This is also a foundational hook for future LLM-based conviction journaling on each shortlisted candidate.

**Change:** Setup 1 now has TWO phases:

1. **Shortlist phase @ `shortlist_et = 09:00 ET`** — `do_shortlist()` pulls candidates from two sources and writes `state/shortlist_guns_setup1_<date>.json`:
   - **GUNS scanner output** (`state/watchlist_guns_<date>.txt`) — the family pre-market scanner's filtered list.
   - **`resources/smw_premarket_movers.py`** — live scrape of `stockmarketwatch.com/movers/premarket`, gainers ≥ 5% within $1.50-$500.
   - Union by symbol, preserves order (scanner first, then SMW-only). Journals `shortlist_built` with per-source counts + overlap.

2. **Entry phase @ `entry_et = 09:28 ET`** (unchanged): `pick_universe()` reads the shortlist artifact instead of re-loading the raw scanner watchlist. Falls back to the raw watchlist if the shortlist file is missing (bot started late, OFF at 09:00, etc.). Eligibility evaluation + order submission logic unchanged from 1.0.1.

**Files touched in `guns_setup1/`:**
- `impl.py`: `__version__` 1.0.1 → 1.1.0. New `do_shortlist(date_iso, cfg, strat)` function. New `shortlist_path(date_iso)` helper returning `STATE_DIR / "shortlist_guns_setup1_<date>.json"`. `pick_universe()` now reads the shortlist file with fallback to `load_guns_watchlist()`. `build()` wires `shortlist_et="09:00"` (config-overridable) + `shortlist=do_shortlist`.

**Files touched elsewhere** (each gets its own changelog entry in that folder's README):
- `strategy/base.py` — added `shortlist_et: str | None` field and `shortlist: Shortlist | None` callable to the `Strategy` dataclass.
- `execution/orchestrator.py` — new `_fire_strategy_shortlist()` scheduler; main loop fires `shortlist_et` once per strategy per day before `entry_et`.
- `resources/smw_premarket_movers.py` — NEW module.
- `config.example.json` — added `"shortlist_et": "09:00"` to the `guns_setup1` block.

**New journal events:**
- `shortlist_built` — emitted by `do_shortlist()` with `n_scanner`, `n_smw`, `n_merged`, `sources_overlap`, `symbols`.
- `shortlist_loaded` — emitted by `pick_universe()` when it successfully reads the shortlist file.
- `shortlist_load_failed` — same site, on JSON/IO error.
- `shortlist_failed` — emitted by the orchestrator if `do_shortlist()` raises.

### 1.0.1 — 2026-05-21 — Place the order pre-market, not at open

**Why:** Per the PDF + the user's read of the rule, the buy-stop-limit must be **resting in Alpaca's book before market open**, not submitted at the open. Previously `entry_et=09:30` meant we fired at the same moment the market opened — a race against HFTs reacting to PMH breaks at 09:30:00.000.

**Change:** `entry_et` 09:30 → **09:28**. That's more than 1 minute before open, giving the bot's 5-second tick + Alpaca round-trip plenty of headroom to have the order resting by 09:29:00.

**Why 09:28 specifically:** the bound is "before 09:29:00 ET". 09:28 leaves ~60s margin for the 5-second orchestrator tick + IBKR bar fetch (~5-15s for the watchlist's worth of symbols) + Alpaca submit round-trips. If the orchestrator fires at the next tick (say 09:28:03), there's still time to evaluate every symbol and submit every order well before 09:29:00.

**No risk of firing pre-market:** `time_in_force=DAY` keeps stop-limit orders dormant until 09:30:00 RTH opens. PM ticks won't trigger them.

**Eligibility evaluation now uses bars 04:00 → 09:28** (15-min consolidation window = 09:13 → 09:28). PMH might move higher between 09:28 and 09:30; that just makes our trigger slightly conservative — acceptable trade-off for guaranteed pre-open placement.

**Files touched in `guns_setup1/`:**
- `impl.py`: `__version__` 1.0.0 → 1.0.1, docstring "Timing" section added, `build()` hardcoded fallback for `entry_et` changed from `"09:30"` to `"09:28"` (so the new default holds even when `config.json` doesn't override).

**Files touched elsewhere (see those folders' READMEs for their own entries):**
- `config.example.json` at intraday-bot/ root: `guns_setup1.entry_et` 09:30 → 09:28.

### 1.0.0 — 2026-05-21 — Initial wired implementation
- Trigger: 09:30 ET, single shot. Cancel unfilled at 09:35.
- Entry: buy-stop @ PMH + $0.01, limit = trigger + 5¢.
- Eligibility: last 15 min of PM bars consolidating within `consol_band_pct` (default 1.5%) of PMH.
- Stop: price-tier table (12¢/17¢/25¢/40¢/50¢ by price bracket).
- Take profit: 2R (configurable via `take_profit_R`).
- Defensive double-check on price ≥ $1.50 and PM volume ≥ 30K inside `evaluate()`.
- Per-strategy concurrency cap: 2.
