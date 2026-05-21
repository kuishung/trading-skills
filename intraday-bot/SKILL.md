---
name: intraday_bot
version: 0.7.0
description: Strategy-agnostic intraday paper-trading framework. Provides the infrastructure (IBKR scanner pipeline, Alpaca paper-order execution, OCO brackets, breakeven moves, EOD safety sweep, structured journaling, dashboard) and a strict risk discipline (1% NLV per trade, notional cap, EOD close-all). Strategies are drop-in modules under scripts/strategies/ — wire one in by adding a file that exposes build(cfg) -> Strategy, registering it in KNOWN_STRATEGIES, and enabling it under cfg.strategies.<name> in config.json. With no strategies enabled, the bot refuses to start. Trigger phrases include "run intraday bot", "start intraday bot", "wire intraday strategy".
---

# Intraday Bot — Strategy-Agnostic Paper-Trading Framework

**Version:** 0.7.0 — clean-slate infrastructure, no strategies wired

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

## Known sharp edges

- **`split_pm_rth` has no upper bound on RTH.** Bars after 16:00 ET get bucketed as "RTH" — historical replays through extended hours can mis-fire detection on after-hours bars. Live runs are time-gated to 09:30-15:58, so this only matters for diagnostic replays.
- **IBKR volume in lot-units.** Scanner enrichment multiplies by 100 for display; if you see volume off by 100×, flip the multiplier in `web/index.html` `fmtCompactVol`.
- **HOT_BY_OPT_VOLUME requires an options data subscription.** Without it, the scanner subscribes silently but emits empty rows. Drop it from `DEFAULT_PARALLEL_SCANNERS` if it stays empty.
