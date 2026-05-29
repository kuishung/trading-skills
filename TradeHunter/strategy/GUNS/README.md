# strategy/GUNS/ — Gap Up News Scalp family

Source: Adam Khoo Piranha Profits, Lesson 8 (2017).
Reference doc: `strategies-reference/GUNS.md` (in the worktree, not the
TradeHunter folder).

ONE universe (gap-up + news catalyst + low float + price ≥ $1.50 + PM
volume ≥ 30K) with FIVE entry setups. This family wires setups 1 and 5.
Setups 2 / 3 / 4 are deferred (setup 4 in particular needs a rolling
09:30–10:30 watch window the current single-fire `entry_et` model does
not support).

## Contents

- `__init__.py` — Family package marker. Light docstring describing the family.
- `_helpers.py` — Family-shared helpers: watchlist path + loader (`load_guns_watchlist`), price-tier stop-loss table (`price_tier_stop_cents`), long buy-stop-limit plan builder, PM-high / consolidation extractor, eligibility constants (`MIN_PRICE`, `MIN_PM_VOLUME`).
- `scanner.py` — Family pre-market watchlist builder. CLI: `py strategy/GUNS/scanner.py`. Sources: IBKR `ScannerSubscription` (GUNS-tuned filters) + `thestockmarketwatch.com` top-gainers scrape. Filter stages: yfinance float (drops > 100M) → yfinance news catalyst (drops M&A / offering / dilution / fraud, flags unknown). Writes `state/watchlist_guns_<date>.txt` — ready to trade, no manual pruning needed.
- `guns_setup1/` — Break of Pre-Market High at 09:30 ET.
- `guns_setup5/` — Break of First 1-Minute RTH Candle at 09:31 ET.
- `Materials/` — Reference material (copyrighted PDFs). **Gitignored**; syncs via Dropbox only. Currently: `Lesson 8-Gap Up News Scalp Strategy.pdf` from Adam Khoo Piranha Profits.

Per-ticker behavioral baselines (atr, vol stats, 3m percentile distributions) **now live at `data/ticker_profile/<TICKER>.json`** — a universal cross-strategy product, not GUNS-specific. See `resources/ticker_profile.py`. The legacy `profiles/` subfolder was retired on 2026-05-23.

## Status of the five PDF setups

| Setup | Trigger | Status |
|---|---|---|
| 1 — Break of PM high (M5) | 09:30 ET | ✅ wired (`guns_setup1`) |
| 2 — Break of PM pivot (M5) | 09:30 ET | ❌ deferred (pivot detection not built) |
| 3 — Break of PM bull flag (M5) | 09:30 ET | ❌ deferred (flag pattern detection not built) |
| 4 — First post-open bull flag (M1/M2/M5) | 09:30–10:30 ET | ❌ deferred (needs rolling watch window — framework gap) |
| 5 — Break of first 1-min RTH candle | 09:31 ET | ✅ wired (`guns_setup5`) |

## Changelog

### 2026-05-23 — `profiles/` folder retired (moved to universal `data/ticker_profile/`)
- Per-ticker baselines are now a universal cross-strategy product per the user's rule (chat 2026-05-23: *"if it is a universal product then it is data output, i propose to put it into the data folder in ...data\ticker_profile"*). The single `POET.json` that lived here moved to `data/ticker_profile/POET.json`.
- The `strategy/GUNS/profiles/` directory + its README are gone — no callers in this folder ever read from it; the only files were the seed `POET.json` and a stub README.
- All ticker_profile API calls drop the `family` parameter — GUNS reads stay the same shape (`get_profile("NVDA")`).

### 2026-05-21 — `profiles/` folder added (per-ticker behavioral baselines)
- Created `profiles/` with its own README. Initial population by `resources/ticker_profile.py` (yfinance source). Each ticker that passes the shortlist phase will get a cached profile here over time.
- Files: `<TICKER>.json` — committed (small + useful as historical record).
- Sample: `POET.json` confirmed end-to-end (ATR(14)=$2.53, 17% of close, Uptrend).

### 2026-05-21 — `Materials/` folder added
- Moved the copyrighted PDF reference material into `Materials/` (was at TradeHunter/ root as `GUNS Materials/`). Stays gitignored; syncs via Dropbox.

### 2026-05-21 — Family folder established
- Created `_helpers.py` (the GUNS-family shared utilities).
- Created `scanner.py` (GUNS-family pre-market watchlist builder CLI). The underscore prefix was dropped from the previous `_guns_scanner` name because this file is a CLI entry point, not a private helper.
- Created `guns_setup1/` and `guns_setup5/` (the two wired setups).
- `scanner.py` reaches `_helpers.py` via the absolute package path `from strategy.GUNS._helpers import ...` so it works both as a CLI (`py strategy/GUNS/scanner.py`) and as a package member.

### 2026-05-21 — `scanner.py` is self-contained
- Wired in float + catalyst filter stages so the output watchlist is ready-to-trade with no manual pruning. The previous "UPSTREAM TODO: remove M&A names" header is gone.
- New CLI flags: `--no-float`, `--no-catalyst`, `--strict-float`, `--strict-catalyst`, `--keep-mna`, `--float-cap N`.
