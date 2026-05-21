---
name: intraday_bot
version: 0.9.0
description: Strategy-agnostic intraday paper-trading framework. Provides the infrastructure (IBKR scanner pipeline, Alpaca paper-order execution, OCO brackets, breakeven moves, EOD safety sweep, structured journaling, dashboard) and a strict risk discipline (1% NLV per trade, notional cap, EOD close-all). Strategies are drop-in modules under scripts/strategies/ — wire one in by adding a file that exposes build(cfg) -> Strategy, registering it in KNOWN_STRATEGIES, and enabling it under cfg.strategies.<name> in config.json. Ships with GUNS Setup 1 (PM-high break at 09:30) and GUNS Setup 5 (first 1-min candle break at 09:31), both disabled by default. With no strategies enabled, the bot refuses to start. Fully self-contained — float filter (yfinance) and catalyst classifier (yfinance news) run inside this folder; no sibling-skill dependency. Trigger phrases include "run intraday bot", "start intraday bot", "wire intraday strategy".
---

# Intraday Bot — Strategy-Agnostic Paper-Trading Framework

**Version:** 0.9.0 — GUNS scanner is now self-contained: float + catalyst filters live in this folder

## Changelog

- **0.9.0** (2026-05-21) — GUNS scanner is now end-to-end self-contained. Added `scripts/guns_float_lookup.py` (yfinance `floatShares` with a 7-day disk cache, drops float > 100M per the PDF) and `scripts/guns_catalyst_classifier.py` (yfinance `Ticker.news` + keyword classifier, drops M&A targets, secondary offerings, dilution, going-concern, SEC actions, FDA rejections; flags AI / earnings / FDA approvals as good). Both modules are GUNS-specific by design and wired only into `guns_scanner.py`. The scanner's output watchlist is now ready-to-trade with no manual pruning — `# UPSTREAM TODO` header line removed. New CLI flags: `--no-float`, `--no-catalyst`, `--strict-float`, `--strict-catalyst`, `--keep-mna`, `--float-cap N`. Bot still defensively re-checks price ≥ $1.50 and PM volume ≥ 30K inside `evaluate()`.
- **0.8.0** (2026-05-21) — Wire GUNS (Gap Up News Scalp) Setups 1 and 5 as MVP, plus `scripts/guns_scanner.py` to build the daily watchlist. Source: Adam Khoo Piranha Profits Lesson 8. Setup 1 = break of pre-market high at 09:30 ET; Setup 5 = break of first 1-min RTH candle at 09:31 ET. Shared universe via `state/watchlist_guns_<date>.txt` (per-family path so future ORB / DITP strategies get their own scanner + watchlist). The scanner pulls candidates from (a) a GUNS-tuned IBKR `ScannerSubscription` matching the PDF filter recipe (price 1.50-500, change% ≥ 5, avg-vol 20K-70M, today vol > 30K) and (b) `thestockmarketwatch.com/markets/today.aspx` top-gainers HTML scrape — union by symbol with per-source provenance comments. Price-tier SL table (10-50¢ by price bracket), 2R default TP, framework's existing BE-at-1R polling. Defensive double-check on price>=$1.50 and PM-volume>=30K inside each evaluate(). Both setups ship `enabled: false` in config.example.json — flip to true after curating the watchlist. Setups 2 (PM pivot break), 3 (PM bull flag), and 4 (post-open bull flag M1/M2/M5) are out of scope for this MVP; Setup 4 in particular needs a rolling watch window (09:30-10:30) that doesn't yet exist in the framework.
- **0.7.0** — Clear wired strategies + rename internals to intraday_bot

This is the **framework**. It handles everything that's strategy-agnostic:

- IBKR live scanner subscriptions (7 parallel scan codes) with mktData enrichment
- Alpaca paper-order plumbing: stop-limit entry → OCO bracket on fill → breakeven move at 1R → TP/SL completion polling
- Strict risk discipline at startup (refuses to launch if violated)
- EOD safety sweep at 15:58 ET (`close_all_positions(cancel_orders=True)`)
- Structured decision journal at `state/journal_<date>.jsonl` with per-strategy roll-up at end of day
- Local dashboard at `http://localhost:8000`
- Auto-start at 09:00 ET on weekdays (configurable)

**Strategies live in [`scripts/strategies/`](scripts/strategies/)**. With zero strategies wired the bot starts up, validates rules, then exits with `STRICT RULE VIOLATION: no strategies enabled`. That's the correct sentinel state.

**Paper-only.** Trades route through the `alpaca-trader-paper` sibling skill, which hard-refuses any non-paper base URL. Going live requires a deliberate code change there — not a config flip.

**Armed by default = NO.** The bot launches in dry-run mode unless you've clicked the **Arm** pill on the dashboard. Disarmed = strategies evaluate + journal, no Alpaca submissions.

## Strict risk rules (enforced at startup)

```
- risk_per_trade_pct ≤ 1% of NLV     (global, never override)
- max_position_pct  = 10% of NLV     (global notional cap)
- At least one strategy enabled
- Each enabled strategy: take_profit_R > 0 and max_concurrent > 0
```

Each strategy declares its own `take_profit_R`. The framework enforces the global rules; per-strategy R is up to the strategy.

## File layout

```
intraday_bot/
├── SKILL.md                          # this file
├── requirements.txt                  # alpaca-py, ib_insync, fastapi, uvicorn, ...
├── config.example.json               # copy to config.json to set IBKR/Alpaca paths
├── config.json                       # (gitignored — your local paths + strategies block)
├── .gitignore
├── start_dashboard.bat               # Windows launcher
├── stop_dashboard.bat                # graceful shutdown
├── _supervise_dashboard.bat          # respawn-on-exit supervisor
├── scripts/
│   ├── _common.py                    # env, ET clock, data abstraction, Telegram
│   ├── _ibkr_data.py                 # IBKR data adapter (bars, quotes, trades)
│   ├── _events.py                    # event emitter → state/events_*.jsonl
│   ├── _journal.py                   # structured decision log → state/journal_*.jsonl
│   ├── _smoke_ibkr.py                # IBKR TWS handshake test
│   ├── _dryrun_ibkr.py               # IBKR adapter dry-run
│   ├── signals.py                    # SHARED utilities (EMA, split_pm_rth,
│   │                                 #   position_size, spread_ok)
│   ├── scanner_observe.py            # 7-scanner subscriber + quote enrichment
│   ├── trade_day.py                  # strategy-agnostic orchestrator (the bot)
│   ├── dashboard.py                  # local server, child-process manager
│   ├── strategies/
│   │   ├── __init__.py               # registry + load_enabled(cfg)
│   │   ├── base.py                   # Strategy dataclass — the interface
│   │   └── (your strategies here)
│   └── setup_*.py                    # one-time installers (Windows shortcuts,
│                                     #   IBKR config wizard, Task Scheduler)
├── ibc/                              # IBC bundle (TWS auto-login). credentials.txt
│                                     #   sourced from the secrets file at runtime.
├── state/                            # (gitignored runtime artifacts)
│   ├── events_<date>.jsonl           # bot + scanner event stream
│   ├── journal_<date>.jsonl          # per-decision structured log
│   ├── fills_<date>.jsonl            # entry/fill/exit timeline
│   ├── equity_<date>.json            # opening / closing equity
│   ├── bot_<date>.log                # bot stdout/stderr
│   ├── scanner_<date>.log            # scanner stdout/stderr
│   └── ibkr_scanner_parameters.xml   # cached scanner metadata
└── web/
    └── index.html                    # dashboard frontend
```

## Wiring a strategy

```
1. Create scripts/strategies/<name>.py with:
   - def pick_universe(date_iso, cfg, strategy) -> list[str]
   - def fetch_bars(symbols, cfg, strategy) -> dict[str, list[bar]]
   - def evaluate(symbol, bars, strategy) -> plan_dict | None
   - def build(cfg) -> Strategy   (the factory)

2. Add the module name to KNOWN_STRATEGIES in scripts/strategies/__init__.py

3. Add a block under cfg.strategies.<name> in config.json:
     {
       "enabled": true,
       "entry_et": "09:35",
       "entry_cutoff_et": "15:00",
       "max_concurrent": 3,
       "take_profit_R": 10.0,
       "<your-strategy-specific-knobs>": ...
     }

4. (Re)start the bot. Orchestrator picks it up, schedules entry_et,
   journals every decision tagged with the strategy name.
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
py scripts/_smoke_ibkr.py           # bare-socket TWS handshake
py scripts/_dryrun_ibkr.py          # exercise the data adapter
py scripts/trade_day.py --dry-run --fake-now 09:36   # full pipeline dry-run

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

**`scripts/guns_scanner.py` is a self-contained pipeline** (no sibling-skill dependencies) — it gathers candidates, filters by float, classifies catalysts, and writes a ready-to-trade file:

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
py scripts/guns_scanner.py                       # both sources, all filters, top 20
py scripts/guns_scanner.py --source smw          # SMW only (no IBKR connection)
py scripts/guns_scanner.py --strict-catalyst     # drop unknown-news names too
py scripts/guns_scanner.py --float-cap 50000000  # tighter 50M float cap
py scripts/guns_scanner.py --no-float            # skip float filter (debug)
py scripts/guns_scanner.py --no-catalyst         # skip catalyst filter (debug)
py scripts/guns_scanner.py --no-write            # preview to stdout
```

The bot defensively re-checks price ≥ $1.50 and PM volume ≥ 30K inside each setup's `evaluate()` so a bad row in the file doesn't fire an entry.

### Setup 1 — Break of Pre-Market High (09:30)

Fires once at `entry_et=09:30`. Reads PM 1-min bars, computes PMH and the last-15-min consolidation high, fires a buy-stop-limit at PMH+1¢ if the consolidation high is within `consol_band_pct` (default 1.5%) of PMH. Stop = price-tier table (12/17/25/40¢ by price bracket). TP = 2R (configurable). Unfilled at `entry_cutoff_et=09:35` → canceled. Per-strategy concurrency cap = 2.

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
- **IBKR volume in lot-units.** Scanner enrichment multiplies by 100 for display; if you see volume off by 100×, flip the multiplier in `web/index.html` `fmtCompactVol`.
- **HOT_BY_OPT_VOLUME requires an options data subscription.** Without it, the scanner subscribes silently but emits empty rows. Drop it from `DEFAULT_PARALLEL_SCANNERS` if it stays empty.
