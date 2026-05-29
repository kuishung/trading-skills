# GUNS Setup 5 — Break of First 1-Minute RTH Candle at 09:31 ET

Source: Adam Khoo Piranha Profits, Lesson 8 (Gap Up News Scalp).
Reference doc: `strategies-reference/GUNS.md`.

Fires at 09:31 ET (after the 09:30:00–09:30:59 candle closes). Eligible
when the first candle is bullish, closes above EMA9 / EMA20 / SMA50
(each toggleable), and its range is ≤ `candle_size_mult` × the median
PM bar range. Stop = the tighter of (price-tier) and (1¢ below candle
low). TP = 2R.

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
- **PATCH** — parameter retuning (candle_size_mult, EMA toggles, etc.), bug fixes, journal field additions.

## Changelog

### 1.0.0 (patch) — 2026-05-21 — Version-tag the plan dict
- `impl.py`: After `build_long_buy_stop_limit_plan()`, set `plan["strategy_version"] = __version__` so downstream journal events (`planned`, `entry_submitted`, etc.) carry the version. Enables `review/stats.py` to attribute outcomes to a specific rule-set when buckets are recomputed.
- No behavior change; pure metadata enrichment for the enrichment program.

### 1.0.0 — 2026-05-21 — Initial wired implementation
- Trigger: 09:31 ET (after the 09:30:00–09:30:59 candle closes). Cancel unfilled at 09:33.
- Entry: buy-stop @ first-candle high + $0.01, limit = trigger + 5¢.
- Eligibility:
  - first candle is bullish (close > open)
  - first candle closes above EMA9, EMA20, SMA50 (each toggleable via `require_above_*`)
  - first candle range ≤ `candle_size_mult` × median PM bar range (default 2.0)
- Stop: `min(price-tier-cents, 1¢ below candle low)` — whichever is tighter risk.
- Take profit: 2R (configurable via `take_profit_R`).
- Defensive double-check on price ≥ $1.50 and PM volume ≥ 30K inside `evaluate()`.
- Per-strategy concurrency cap: 2.
