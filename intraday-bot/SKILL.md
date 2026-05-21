---
name: intraday_bot
version: 0.15.0
description: Self-contained intraday paper-trading framework. Seven top-level folders (Resources, Strategy, Execution, Journal, Review, Data, Dashboard) + scripts/ for operational glue. Strategies are organised by FAMILY — strategy/<FAMILY>/<setup_name>/ — with the GUNS family (Adam Khoo Gap Up News Scalp) wired today. Each strategy carries its own impl.py + __init__.py + changelog.md (per-strategy versioning). Each family has its own pre-market scanner. Per-strategy gating with TWO independent live flags — ON/OFF (pipeline runs?) and ARMED (plans submit?). Three states: OFF, ON+DISARMED (paper-eval), ON+ARMED (live). Strict risk discipline (1% NLV per trade, notional cap, EOD close-all). Fully self-contained — every dependency inside intraday-bot/, designed for Dropbox sync across PCs. Read intraday-bot/CLAUDE.md first in every session. Trigger phrases: "run intraday bot", "start intraday bot", "wire intraday strategy".
---

# Intraday Bot — Six-Layer Self-Contained Paper-Trading Framework

**Version:** 0.15.0 — `data/` artifact folder (the cream) + Parquet bars I/O + S&P 500 yfinance seed + IBKR EOD ingest hook

> **Read [`CLAUDE.md`](CLAUDE.md) first** — it carries the strict rules and the cross-PC sync workflow Claude consults on every session. Mirrored from `~/.claude/.../memory/project_intraday_bot_rules.md`. Keep them in sync.

## Changelog

- **0.14.1** (2026-05-21) — Moved the remaining dashboard files into `dashboard/`. `start_dashboard.bat`, `stop_dashboard.bat`, `_supervise_dashboard.bat` all left intraday-bot/ root; `scripts/setup_dashboard_launcher.py` → `dashboard/setup_launcher.py`. The supervisor now `cd`s into `dashboard/` and runs `py server.py` (no path prefix needed since it's in the same folder). `setup_launcher.py` updates its START_BAT/STOP_BAT paths to `dashboard/`. Smoke-tested: `cd dashboard && py server.py` brings up the UI exactly like before.
- **0.14.0** (2026-05-21) — Strategy family folders + dedicated dashboard folder.
  - `strategy/_guns_common.py` → `strategy/GUNS/_helpers.py`
  - `strategy/_guns_scanner.py` → `strategy/GUNS/scanner.py` (no underscore — CLI entry point)
  - `strategy/guns_setup1/` → `strategy/GUNS/guns_setup1/`
  - `strategy/guns_setup5/` → `strategy/GUNS/guns_setup5/`
  - `scripts/dashboard.py` → `dashboard/server.py` (dashboard gets its own top-level folder)
  - `web/` → `dashboard/web/`
  - `strategy/__init__.py` gains `_STRATEGY_IMPORT_PATHS` — leaf-name → dotted package path mapping. Leaf names (`guns_setup1`) stay everywhere user-facing (state flags, journal events, dashboard pills); the dotted path (`GUNS.guns_setup1`) is only used by importlib.
  - Files inside `strategy/GUNS/guns_setupN/impl.py` switched to relative imports (`from .._helpers import ...`) for family-internal references; cross-layer imports stay bare-name.
  - `dashboard` added to the path-bootstrap tuple in every layer module.
  - New file `intraday-bot/CLAUDE.md` — the in-folder copy of the strict rules. Auto-loaded by Claude Code when cwd touches intraday-bot/. First file consulted in each session.
  - `_supervise_dashboard.bat` points to `dashboard\server.py`.
- **0.13.0** (2026-05-21) — Big restructure to the five-layer architecture the user committed to. New top-level folders: `resources/`, `strategy/`, `execution/`, `journal/`, `review/`. Files moved:
  - `scripts/_ibkr_data.py` → `resources/ibkr_data.py`
  - `scripts/_smoke_ibkr.py`, `_dryrun_ibkr.py` → `resources/ibkr_smoke.py`, `ibkr_dryrun.py`
  - `scripts/guns_float_lookup.py` → `resources/yfinance_float.py` (renamed — it was always generic)
  - `scripts/guns_catalyst_classifier.py` → `resources/yfinance_news.py` (same)
  - `scripts/guns_scanner.py` → `strategy/_guns_scanner.py` (GUNS-family-shared)
  - `scripts/signals.py` → `strategy/signals.py`
  - `scripts/strategies/{base,_guns_common}.py` → `strategy/{base,_guns_common}.py`
  - `scripts/strategies/guns_setup1.py` → `strategy/guns_setup1/impl.py` (+ `__init__.py` + `changelog.md`)
  - `scripts/strategies/guns_setup5.py` → `strategy/guns_setup5/impl.py` (+ siblings)
  - `scripts/_journal.py` → `journal/writer.py`
  - `scripts/_events.py` → `journal/events.py`
  - `scripts/trade_day.py` → `execution/orchestrator.py`
  - `scripts/` keeps the operational glue: `_common.py`, `_gating.py`, `dashboard.py`, `setup_*.py`.
  - `review/` is a placeholder for the self-improvement loop.
  Each layer module starts with a small sys.path bootstrap that walks up to find SKILL.md and adds every layer folder + intraday-bot root, so bare-name imports (`from base import Strategy`) and package-style imports (`from strategy import KNOWN_STRATEGIES`) both work regardless of how a file is invoked.

  Portability hardening (the second half of the user's request): `_common.py` no longer reads `../alpaca-trader-paper/.env` or `../MATP/.env`. All credential lookup now goes VAULT-first via the central Dropbox folder, with an in-folder `.env` as final fallback. The bot is now fully self-contained — syncing intraday-bot/ to another machine and `pip install -r requirements.txt` is all it takes.

  Per-strategy versioning scaffolding: each `strategy/<name>/impl.py` declares `__version__`. A `changelog.md` next to it records every rule edit. Future trade-journal events will carry this version so post-trade analytics can attribute outcomes to a specific rule-set.

  Launchers updated: dashboard `BOT_SCRIPT` → `execution/orchestrator.py`, `setup_schedule.py` Task Scheduler command updated to match.
- **0.12.0** (2026-05-21) — Split per-strategy arming into TWO independent live gates: ON/OFF and ARM. Renamed `scripts/_arming.py` → `scripts/_gating.py`. Each strategy now has two flag files: `state/enabled_<name>.flag` (does the analysis pipeline run?) and `state/armed_<name>.flag` (do plans submit?). Three states per strategy: OFF / ON+DISARMED (paper-eval — scanner + analysis + journal, no orders) / ON+ARMED (live submission). The bot reads ON/OFF at the TOP of `_fire_strategy_entries` (cheap skip — no scanner, no resource calls) and ARM at the submit site. New `strategy_off_skipped` journal event records every scheduled fire that hit an OFF strategy. New endpoints: `GET/POST /bot/enable` mirroring the existing `/bot/arm`. `cfg.strategies.<name>.enabled` is now a FIRST-RUN-DEFAULT seed only; once the user touches the ON/OFF flag from the dashboard, the flag wins. The "no strategies enabled" strict-rule was removed — the bot now happily starts with everything OFF (useful for holidays / staging). UI: "strategy arming" panel became "strategy gating" with one row per strategy showing TWO pills (ON/OFF + ARM/DISARM), bulk shortcuts for enable-all/disable-all/arm-all/disarm-all, and a header summary "N ON · K ARMED". ARM pill is dimmed when strategy is OFF (state remembered, just won't fire).
- **0.11.0** (2026-05-21) — Per-strategy arming replaces the old global ARMED/DISARMED flag. New `scripts/_arming.py` manages one `state/armed_<strategy>.flag` per known strategy. The bot reads each flag LIVE at the entry-submit site, so toggling in the dashboard takes effect on the next entry attempt — no restart needed. Disarmed strategies still pick a universe, evaluate, and journal `entry_disarmed` events with the full plan they would have submitted. The legacy `state/armed.flag` is auto-migrated once on dashboard boot ("global armed" → every known strategy armed), then deleted. Dashboard UI gains a "strategy arming" panel with one pill per known strategy (click to toggle), plus arm-all / disarm-all shortcuts and an "N/M armed" summary in the header. Old `/bot/arm` body `{armed: bool}` replaced by `{strategy, armed}` or `{all: bool}`. `--dry-run` CLI flag still wins as the global operator override.
- **0.10.0** (2026-05-21) — Removed `scripts/scanner_observe.py` (the old 7-parallel IBKR ambient scanner) and all related dashboard plumbing. The model is now strictly **one scanner per strategy family**: each family owns its own pre-market scanner (`guns_scanner.py` today; future `orb_scanner.py`, `ditp_scanner.py`) that writes its own `state/watchlist_<family>_<date>.txt`. Dashboard simplifications: no more scanner-pill, no more "live top movers" panel, `BotManager` now manages only `trade_day.py`. The event log still surfaces every event the bot emits; the `scanner.*` event class is gone. `web/index.html` lost ~200 lines of scanner-specific rendering. The dashboard's auto-start path still fires at 09:00 ET to launch the bot — pre-market scanners will be wired into the launch sequence in a separate change.
- **0.9.0** (2026-05-21) — GUNS scanner is now end-to-end self-contained. Added `scripts/guns_float_lookup.py` (yfinance `floatShares` with a 7-day disk cache, drops float > 100M per the PDF) and `scripts/guns_catalyst_classifier.py` (yfinance `Ticker.news` + keyword classifier, drops M&A targets, secondary offerings, dilution, going-concern, SEC actions, FDA rejections; flags AI / earnings / FDA approvals as good). Both modules are GUNS-specific by design and wired only into `guns_scanner.py`. The scanner's output watchlist is now ready-to-trade with no manual pruning — `# UPSTREAM TODO` header line removed. New CLI flags: `--no-float`, `--no-catalyst`, `--strict-float`, `--strict-catalyst`, `--keep-mna`, `--float-cap N`. Bot still defensively re-checks price ≥ $1.50 and PM volume ≥ 30K inside `evaluate()`.
- **0.8.0** (2026-05-21) — Wire GUNS (Gap Up News Scalp) Setups 1 and 5 as MVP, plus `scripts/guns_scanner.py` to build the daily watchlist. Source: Adam Khoo Piranha Profits Lesson 8. Setup 1 = break of pre-market high at 09:30 ET; Setup 5 = break of first 1-min RTH candle at 09:31 ET. Shared universe via `state/watchlist_guns_<date>.txt` (per-family path so future ORB / DITP strategies get their own scanner + watchlist). The scanner pulls candidates from (a) a GUNS-tuned IBKR `ScannerSubscription` matching the PDF filter recipe (price 1.50-500, change% ≥ 5, avg-vol 20K-70M, today vol > 30K) and (b) `thestockmarketwatch.com/markets/today.aspx` top-gainers HTML scrape — union by symbol with per-source provenance comments. Price-tier SL table (10-50¢ by price bracket), 2R default TP, framework's existing BE-at-1R polling. Defensive double-check on price>=$1.50 and PM-volume>=30K inside each evaluate(). Both setups ship `enabled: false` in config.example.json — flip to true after curating the watchlist. Setups 2 (PM pivot break), 3 (PM bull flag), and 4 (post-open bull flag M1/M2/M5) are out of scope for this MVP; Setup 4 in particular needs a rolling watch window (09:30-10:30) that doesn't yet exist in the framework.
- **0.7.0** — Clear wired strategies + rename internals to intraday_bot

This is the **framework**. It handles everything that's strategy-agnostic:

- Alpaca paper-order plumbing: stop-limit entry → OCO bracket on fill → breakeven move at 1R → TP/SL completion polling
- Strict risk discipline at startup (refuses to launch if violated)
- EOD safety sweep at 15:58 ET (`close_all_positions(cancel_orders=True)`)
- Structured decision journal at `state/journal_<date>.jsonl` with per-strategy roll-up at end of day
- Local dashboard at `http://localhost:8000`
- Auto-start at 08:30 ET (T-60 BMO) on weekdays (configurable)

**Strategies live in [`scripts/strategies/`](scripts/strategies/)**. With zero strategies wired the bot starts up, validates rules, then exits with `STRICT RULE VIOLATION: no strategies enabled`. That's the correct sentinel state.

**Paper-only.** Trades route through the `alpaca-trader-paper` sibling skill, which hard-refuses any non-paper base URL. Going live requires a deliberate code change there — not a config flip.

**Two independent live gates per strategy.** Each wired strategy has its own ON/OFF flag (`state/enabled_<name>.flag`) and its own ARM flag (`state/armed_<name>.flag`), both managed by `scripts/_gating.py`. The bot reads them live: ON/OFF at the top of `_fire_strategy_entries` (cheap skip — no scanner, no resource calls), ARM at the submit site. Three meaningful states per strategy:

- **OFF** — pipeline doesn't run at all; one `strategy_off_skipped` journal event per scheduled fire.
- **ON + DISARMED** — full pipeline runs (scanner, resources, analysis), journal accumulates everything (universe, rejections, plans), but no submission. This is the "live paper-eval" mode for vetting.
- **ON + ARMED** — full pipeline runs AND plans flow to Alpaca.

Toggle individual strategies (or use enable-all / disable-all / arm-all / disarm-all) from the dashboard's "strategy gating" panel. Changes take effect on the next scheduled fire (ON/OFF) or the next submit attempt (ARM) — no bot restart. `cfg.strategies.<name>.enabled` is a first-run default only; after that, the flag wins. `--dry-run` CLI flag remains a global override for replays / smoke tests.

## Strict risk rules (enforced at startup)

```
- risk_per_trade_pct ≤ 1% of NLV     (global, never override)
- max_position_pct  = 10% of NLV     (global notional cap)
- At least one strategy WIRED        (config block present + module importable)
- Each wired strategy: take_profit_R > 0 and max_concurrent > 0
```

The bot is allowed to start with every strategy OFF — the orchestrator just journals `strategy_off_skipped` for each scheduled fire. Each strategy declares its own `take_profit_R`; the framework enforces the global rules.

## File layout — six-layer architecture

```
intraday-bot/
├── SKILL.md                          # this file
├── CLAUDE.md                         # rules + cross-PC workflow (READ FIRST)
├── requirements.txt                  # alpaca-py, ib_insync, yfinance, fastapi, ...
├── config.example.json
├── config.json                       # (gitignored)
├── .gitignore
│   (dashboard launchers live under dashboard/ — see below)
│
├── resources/                        # === LAYER 1: stateless data sources ===
│   ├── ibkr_data.py                  # IBKR bars/quotes/trades adapter
│   ├── ibkr_smoke.py                 # IBKR TWS handshake smoke test
│   ├── ibkr_dryrun.py                # IBKR data adapter dry-run
│   ├── yfinance_float.py             # free-float lookup, 7-day disk cache
│   └── yfinance_news.py              # news-catalyst classifier
│
├── strategy/                         # === LAYER 2: analysis units ===
│   ├── __init__.py                   # KNOWN_STRATEGIES + load_known + import-path map
│   ├── base.py                       # Strategy dataclass + interface
│   ├── signals.py                    # math helpers (EMA, position_size, ...)
│   └── GUNS/                         # strategy family folder
│       ├── __init__.py
│       ├── _helpers.py               # GUNS-family-shared helpers
│       ├── scanner.py                # GUNS pre-market watchlist builder (CLI)
│       ├── guns_setup1/
│       │   ├── __init__.py           # `from .impl import build, __version__`
│       │   ├── impl.py               # pick_universe / fetch_bars / evaluate / build
│       │   └── changelog.md          # per-strategy version history
│       └── guns_setup5/
│           ├── __init__.py
│           ├── impl.py
│           └── changelog.md
│   (future families: ORB/, DITP/, etc.)
│
├── execution/                        # === LAYER 3: Alpaca + position mgmt ===
│   └── orchestrator.py               # the bot — entry / OCO / BE / EOD
│
├── journal/                          # === LAYER 4: structured logs ===
│   ├── writer.py                     # journal(...) → state/journal_<date>.jsonl
│   └── events.py                     # emit(...) → state/events_<date>.jsonl
│
├── review/                           # === LAYER 5: self-improvement loop ===
│   └── (placeholder — TODO)
│
├── dashboard/                        # === operational UI (all in one folder) ===
│   ├── server.py                     # FastAPI + child-process manager
│   ├── start_dashboard.bat           # idempotent Windows launcher
│   ├── stop_dashboard.bat            # graceful POST /shutdown then port-kill
│   ├── _supervise_dashboard.bat      # respawn-on-exit-100 supervisor
│   ├── setup_launcher.py             # one-time Desktop shortcut installer
│   └── web/
│       └── index.html
│
├── scripts/                          # operational glue (cross-cutting)
│   ├── _common.py                    # config + ET clock + VAULT env + Telegram +
│   │                                 #   data-provider dispatch (alpaca|ibkr)
│   ├── _gating.py                    # per-strategy ON/OFF + ARM flags
│   └── setup_*.py                    # IBKR + Task Scheduler installers
│
├── ibc/                              # IBC bundle (TWS auto-login)
└── state/                            # (gitignored runtime artifacts)
    ├── enabled_<name>.flag           # per-strategy ON/OFF
    ├── armed_<name>.flag             # per-strategy ARM
    ├── journal_<date>.jsonl
    ├── events_<date>.jsonl
    ├── fills_<date>.jsonl
    ├── equity_<date>.json
    ├── bot_<date>.log
    └── cache/                        # yfinance float + news caches
```

Each layer module starts with a small `sys.path` bootstrap that walks up to find `SKILL.md`, then adds every layer folder + intraday-bot root. This means bare-name imports (`from base import Strategy`) and package-style imports (`from strategy import KNOWN_STRATEGIES`) both work, regardless of how the file is invoked.

## Self-contained — no sibling-skill dependencies

Every dependency lives inside `intraday-bot/`. Syncing this folder to another machine and running `pip install -r requirements.txt` is all it takes. Credential resolution walks this priority order:

1. `$INTRADAY_ENV_DIR/<vendor>.env` (manual override)
2. `intraday-bot/.env` (in-folder fallback)
3. `<Dropbox>/VAULT/Claude Credential/<vendor>.env` (central, shared across PCs)

`_common.py` no longer reads `../alpaca-trader-paper/.env` or `../MATP/.env` — those sibling-folder reads were removed in 0.13.0.

## Wiring a strategy

```
1. (New family?) mkdir strategy/<FAMILY>/. Drop in:
   - __init__.py        (package marker, light docstring)
   - _helpers.py        (family-shared helpers)
   - scanner.py         (if the family needs its own universe builder)

2. mkdir strategy/<FAMILY>/<setup_name>/. Create:
   - impl.py with:
     - __version__ = "1.0.0"   (bumped on every rule edit; see changelog.md)
     - def pick_universe(date_iso, cfg, strategy) -> list[str]
     - def fetch_bars(symbols, cfg, strategy) -> dict[str, list[bar]]
     - def evaluate(symbol, bars, strategy) -> plan_dict | None
     - def build(cfg) -> Strategy   (the factory)
   - __init__.py:  `from .impl import build, __version__`
   - changelog.md: seed with v1.0.0 entry

3. Inside impl.py, family-internal imports use relative form:
     from .._helpers import MIN_PRICE, build_long_buy_stop_limit_plan, ...
   Cross-layer imports stay bare-name:
     from _common import get_pm_bars         # scripts/_common.py
     from writer import journal              # journal/writer.py
     from base import Strategy               # strategy/base.py
     from signals import ema_series          # strategy/signals.py

4. In strategy/__init__.py, add the LEAF NAME to KNOWN_STRATEGIES AND
   add the leaf → "FAMILY.<setup_name>" mapping in
   _STRATEGY_IMPORT_PATHS.

5. Add a block under cfg.strategies.<leaf_name> in config.json:
     {
       "enabled": true,           # FIRST-RUN seed only; live ON/OFF lives in state/
       "entry_et": "09:35",
       "entry_cutoff_et": "15:00",
       "max_concurrent": 3,
       "take_profit_R": 10.0,
       "<your-strategy-specific-knobs>": ...
     }

6. (Re)start the bot. The orchestrator picks it up, schedules entry_et,
   journals every decision tagged with the strategy name and version.
   Toggle the strategy from the dashboard's "strategy gating" panel.
```

## Plan dict contract

`evaluate()` returns a plan dict in this shape (consumed by `submit_setup_entry`):

```python
{
    "strategy":            str,          # your strategy name
    "symbol":              str,
    "side":                "long" | "short",
    "entry_stop_trigger":  float,        # stop trigger price
    "entry_limit":         float,        # limit price after trigger
    "stop_loss":           float,        # SL leg of the OCO bracket
    "take_profit":         float,        # TP leg of the OCO bracket
    "risk_per_share":      float,        # used for sizing + R-multiple
    "take_profit_R":       float,        # informational
    # ... any strategy-specific evidence fields
}
```

The framework sizes the order from `risk_per_share` (clamped by `max_position_pct`), submits the stop-limit, attaches the OCO on fill, moves stop to breakeven at 1R, and journals every transition. Your strategy code only computes the plan.

## Operational quick reference

```
# First-time setup (one machine)
py -m pip install -r requirements.txt
py scripts/setup_ibkr.py            # IBKR creds + IBC bundle wiring
py scripts/setup_dashboard_launcher.py  # Windows desktop shortcuts

# Smoke tests (no orders submitted)
py resources/ibkr_smoke.py             # bare-socket TWS handshake
py resources/ibkr_dryrun.py            # exercise the data adapter
py execution/orchestrator.py --dry-run --fake-now 09:36   # full pipeline dry-run

# Run the dashboard (spawns bot + scanner children)
.\start_dashboard.bat               # or double-click the desktop shortcut

# Stop the dashboard (graceful)
.\stop_dashboard.bat
```

## What the bot does NOT do

- **No live trading.** Paper only, hard-enforced.
- **No multi-day backtesting.** This is a forward-test paper bot. Backtests belong in a separate tool, fed from `state/journal_*.jsonl`.
- **No auto-tuning / "learning".** Strategy parameters are deliberate. The journal exists so a human (or a future analytics layer) can decide what to change.
- **No regime detection.** Every strategy fires the same way regardless of market context (FOMC days, half-days, etc.). Add a regime gate inside the strategy if you want one.
- **No level-2 / order-book signals.** Alpaca paper doesn't expose useful depth; IEX-only L2 from the free IBKR feed is too thin for most names.

## GUNS (Gap Up News Scalp) — wired strategies

GUNS is ONE universe (gap-up + news catalyst + low float + price≥$1.50 + PM-volume≥30K) with FIVE entry setups. This release wires the two most mechanical of them. The remaining three are deliberately deferred:

| Setup | Trigger | Status |
|---|---|---|
| 1 — Break of PM high (M5) | 09:30 ET | ✅ wired (`guns_setup1`) |
| 2 — Break of PM pivot (M5) | 09:30 ET | ❌ deferred (needs pivot detection) |
| 3 — Break of PM bull flag (M5) | 09:30 ET | ❌ deferred (needs flag-pattern detection) |
| 4 — First post-open bull flag (M1/M2/M5) | 09:30-10:30 ET | ❌ deferred (needs rolling watch window, framework gap) |
| 5 — Break of first 1-min RTH candle | 09:31 ET | ✅ wired (`guns_setup5`) |

### Watchlist (shared universe input)

Both setups read `state/watchlist_guns_<date>.txt` — one ticker per line, `#` comments allowed. The `guns_` prefix is deliberate: each strategy family (GUNS today; ORB, DITP etc. in future) gets its own scanner with its own filter criteria, writing to its own watchlist file.

**`strategy/_guns_scanner.py` is a self-contained pipeline** (no sibling-skill dependencies) — it gathers candidates, filters by float, classifies catalysts, and writes a ready-to-trade file:

1. **IBKR GUNS-tuned scanner.** Submits a `ScannerSubscription` (`STK.US.MAJOR` — NYSE + AMEX + ARCA + NQ.NM + NQ.SC + BATS, `stockTypeFilter="CORP"` — no ADR/ETF/REIT/CEF, `TOP_PERC_GAIN`) with the filter recipe — price 1.50-500, change% ≥ 5, avg-vol ≥ 20K, today's vol > 30K. Uses `clientId=82` (distinct from bot 71 / observer 80 / dashboard 99).
2. **`https://thestockmarketwatch.com/markets/today.aspx`.** Scrapes the "Top Gainers" table (best-effort HTML parsing; logs a warning rather than crashing if the page restructures). Applies the same price + change% filter at the source.
3. **`scripts/guns_float_lookup.py`** — yfinance `Ticker.info["floatShares"]` with a 7-day disk cache. Drops anything with float > 100M (PDF rule). Symbols whose float can't be determined are kept with a `CAUTION: float=?` flag unless `--strict-float` is passed.
4. **`scripts/guns_catalyst_classifier.py`** — yfinance `Ticker.news` filtered to the last 36 hours, then keyword-matched. Drops BAD catalysts (M&A target / acquirer, secondary offering, dilution, PIPE, reverse split, going-concern, bankruptcy, SEC/DOJ action, FDA rejection / CRL, guidance cut). Tags GOOD catalysts (earnings beat, guidance raise, FDA approval, contract win, partnership, analyst upgrade, AI sympathy). Unknown-catalyst names kept with `CAUTION: no-fresh-news` unless `--strict-catalyst`. M&A names dropped by default; `--keep-mna` retains them with caution.

The script then merges by symbol (union, dedupe), ranks IBKR-then-SMW with overlap weighted highest, caps to `--top N` (default 20), and writes the file with per-symbol provenance + filter details:

```
HCWB      # IBKR rank=1; SMW; chg=+12.5%; px=$3.45; float=8.2M; cat=earnings_beat
JDZG      # IBKR rank=2; float=44.1M; cat=fda_good
MTVA      # SMW; chg=+9.1%; px=$2.89; float=?; cat=?; CAUTION:float=?,no-fresh-news
```

Run it pre-market:

```
py strategy/_guns_scanner.py                       # both sources, all filters, top 20
py strategy/_guns_scanner.py --source smw          # SMW only (no IBKR connection)
py strategy/_guns_scanner.py --strict-catalyst     # drop unknown-news names too
py strategy/_guns_scanner.py --float-cap 50000000  # tighter 50M float cap
py strategy/_guns_scanner.py --no-float            # skip float filter (debug)
py strategy/_guns_scanner.py --no-catalyst         # skip catalyst filter (debug)
py strategy/_guns_scanner.py --no-write            # preview to stdout
```

The bot defensively re-checks price ≥ $1.50 and PM volume ≥ 30K inside each setup's `evaluate()` so a bad row in the file doesn't fire an entry.

### Setup 1 — Break of Pre-Market High (order placed pre-market)

Fires once at `entry_et=09:28` ET — **before market open** — so the buy-stop-limit is already resting in Alpaca's book when RTH opens at 09:30. Reads PM 1-min bars (04:00 → 09:28), computes PMH and the last-15-min consolidation high (09:13 → 09:28). If the consolidation high is within `consol_band_pct` (default 1.5%) of PMH: submits a buy-stop-limit at PMH + 1¢ with limit = trigger + 5¢. Stop = price-tier table (12/17/25/40¢ by price bracket). TP = 2R (configurable). `time_in_force=DAY` keeps the order dormant until RTH opens, so PM ticks won't fire it. Unfilled at `entry_cutoff_et=09:35` → canceled. Per-strategy concurrency cap = 2.

### Setup 5 — Break of First 1-Minute Candle (09:31)

Fires at `entry_et=09:31` so the 09:30:00-09:30:59 candle has closed. Eligibility: bullish first candle, closes above EMA9/EMA20/SMA50 (toggleable via `require_above_*`), candle range ≤ `candle_size_mult` × median PM bar range. Stop = `min(price-tier, 1¢ below candle low)` — whichever is tighter. TP = 2R. Unfilled at `entry_cutoff_et=09:33` → canceled. Per-strategy concurrency cap = 2.

### Enabling

```jsonc
// config.json
"strategies": {
  "guns_setup1": { "enabled": true, ... },
  "guns_setup5": { "enabled": true, ... }
}
```

Both ship `enabled: false` in `config.example.json`. The framework's strict-rule check refuses to start with zero strategies enabled.

## Known sharp edges

- **`split_pm_rth` has no upper bound on RTH.** Bars after 16:00 ET get bucketed as "RTH" — historical replays through extended hours can mis-fire detection on after-hours bars. Live runs are time-gated to 09:30-15:58, so this only matters for diagnostic replays.
- **Per-family scanners are not yet auto-launched.** The dashboard auto-starts `execution/orchestrator.py` at **08:30 ET (1 hour before market open)**, but the strategy-family scanners (`strategy/GUNS/scanner.py`, etc.) still need to run separately before the open or the strategies will log `watchlist_missing` and skip. Wiring scanner-runs into the auto-start path is a planned change. *(Note: GUNS Setup 1's shortlist phase still fetches movers from SMW + the GUNS scanner output at 09:00 ET — the manual scanner run only matters if you want a fresh IBKR-side scan; the SMW pre-market scrape happens automatically.)*
