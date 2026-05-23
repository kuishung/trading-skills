# strategy/OS/ — Opening Surge family

Source: Internal (Claude, 2026-05-23) — see `strategies-reference/OS.md`.

A fully-automated, no-human-in-the-loop opening-surge breakout play.
Modeled on GUNS Setup 1 but stripped of the catalyst classifier + float
cap, so it can run end-to-end without any qualitative gate. The
trade-off: wider universe + more trades + higher per-trade loss rate,
offset by smaller per-trade risk (0.5% vs GUNS's 1%) and a time-based
exit at 10:30 ET.

## Contents

- `__init__.py` — Family package marker.
- `_helpers.py` — Family-shared helpers: watchlist path + loader, price-tier stops, PMH/consolidation extractor, plan builder, eligibility constants. **Deliberately does NOT import from `strategy/GUNS/_helpers.py`** so edits in one family don't ripple to the other (per CLAUDE.md "Never blend rules from multiple frameworks").
- `scanner.py` — IBKR `TOP_PERC_GAIN` scanner CLI. Writes `state/watchlist_os_<date>.txt`.
- `os_breakout/` — Single setup wired today: 09:28 buy-stop-limit pre-rest at PMH+1¢.

## Setups

| Setup | Trigger | Status |
|---|---|---|
| `os_breakout` — Break of Pre-Market High | 09:28 ET buy-stop-limit pre-rest, fires at PMH+1¢ when RTH opens | ✅ wired, paper-eval mode |

## Status: paper-eval gate

OS is **fully automated** (default state ON+ARMED) but ALSO **paper-only**
until 30 calendar days of journal data clear an expectancy check in
`review/stats.py`. Real-money authorization is a separate explicit step
after that validation.

The "fully automated" promise means: no human approval on entries. The
"paper-only gate" is the safety net — bad rules can compound losses
faster than a daily review can catch them.

## Convention reminders (from CLAUDE.md)

- Strategy code MUST cite the source: `"""Source: strategies-reference/OS.md ..."""`.
- Never blend OS rules into a GUNS file or vice versa.
- Every rule edit bumps `__version__` in the setup's `impl.py` and adds a dated entry to that setup's README changelog.

## Changelog

### 2026-05-23 — Family scaffolded, `os_breakout` v1.0.0 wired
- Created `__init__.py`, `_helpers.py`, `scanner.py`, this README, and the `os_breakout/` setup folder with `impl.py` v1.0.0.
- `_helpers.py` mirrors GUNS primitives (price-tier stops, PMH consolidation) but lives in OS's namespace so the two families stay isolated.
- `scanner.py` calls `resources/ibkr_movers.get_movers(TOP_PERC_GAIN)` with OS-specific filters (price $1.50-$50, change% ≥ 3, avg volume ≥ 200K). Writes `state/watchlist_os_<date>.txt`.
- `os_breakout/impl.py`:
  - `do_shortlist()` at 09:00 ET → runs scanner, writes `state/shortlist_os_breakout_<date>.json`
  - `pick_universe()` at 09:28 ET → reads the shortlist (fallback to .txt watchlist)
  - `fetch_bars()` → PM bars via `get_pm_bars` (IBKR primary, Alpaca fallback)
  - `evaluate()` → PMH consolidation check, price band re-check, PM volume floor, plan builder
- Wired into `strategy/__init__.py` `KNOWN_STRATEGIES` + `_STRATEGY_IMPORT_PATHS`.
- Added `cfg.strategies.os_breakout` block to `config.example.json`.
- Default state: ON + ARMED (per the "fully automated" mandate). Live Alpaca account is paper so this is safe.
