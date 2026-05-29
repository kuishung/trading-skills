# OS Breakout — Break of Pre-Market High at 09:28 ET (pre-rest)

Source: Adam Khoo / GUNS Setup 1 (mechanics) + internal automation
relaxation (no catalyst / float gates). Reference doc:
`strategies-reference/OS.md` §6.

Two-phase flow, same shape as GUNS Setup 1:

1. **Shortlist phase** at `shortlist_et = 09:00 ET`. `do_shortlist()` runs
   `strategy/OS/scanner.py::build_os_watchlist()` (IBKR `TOP_PERC_GAIN`
   scan, filtered to $1.50-$50, ≥ 3% change, ≥ 200K avg volume) and
   persists `state/shortlist_os_breakout_<date>.json` for the entry phase.

2. **Entry phase** at `entry_et = 09:28 ET`. `pick_universe()` reads the
   shortlist, `fetch_bars()` pulls today's PM 1-min bars, `evaluate()`
   checks PMH consolidation and submits a buy-stop-limit at PMH+1¢ that
   rests in Alpaca's book before 09:30:00.

Unfilled orders cancel at `entry_cutoff_et = 09:35 ET`.

## Contents

- `__init__.py` — Re-exports `build` + `__version__`.
- `impl.py` — `__version__`, `build(cfg)`, `evaluate(symbol, bars, strat)`, `pick_universe`, `fetch_bars`, `do_shortlist`.

## Versioning policy

Every change to trigger / sizing / stop / TP / eligibility bumps
`__version__` in `impl.py` and adds a Changelog entry below. Journal
events carry this version (`plan["strategy_version"]`) so the review
layer can attribute outcomes to a specific rule-set.

- MAJOR — plan-dict shape change or fundamentally different entry/exit
- MINOR — new gating filter / entry condition
- PATCH — parameter retuning, bug fix, journal field addition

## Changelog

### 1.0.0 — 2026-05-23 — Initial wired implementation

**Eligibility (all must hold):**
- Symbol on the OS watchlist (IBKR `TOP_PERC_GAIN`, $1.50–$50, ≥3% chg, ≥200K avg vol)
- PM volume ≥ 100,000 shares
- PMH in `[$1.50, $50]` (defensive re-check; scanner already pre-filters)
- Last 15 min of PM bars consolidating within `consol_band_pct` (default 1.5%) of the PMH

**Trigger:** Buy-stop-limit at `PMH + 0.01`, limit at `trigger + 5¢`,
TIF=DAY, submitted at 09:28 ET.

**Stop:** Price-tier table (12 / 17 / 25 / 40 / 50 cents by price bracket).

**Take profit:** 2.0R default; configurable via `take_profit_R`.

**Concurrency cap:** 3.

**Default config** (in `config.example.json`):
```
"os_breakout": {
  "enabled": true,
  "shortlist_et": "09:00",
  "entry_et": "09:28",
  "entry_cutoff_et": "09:35",
  "max_concurrent": 3,
  "take_profit_R": 2.0,
  "params": {
    "consol_band_pct": 1.5,
    "consol_lookback_min": 15,
    "limit_cents_above_stop": 5,
    "scanner_rows": 50,
    "scanner_min_change_pct": 3.0,
    "scanner_min_avg_volume": 200000
  }
}
```

**Default state:** ON + ARMED in paper mode. Real-money authorization
requires 30 calendar days of paper-eval journal data clearing an
expectancy check (`review/stats.py` per-setup avg-R analysis).
