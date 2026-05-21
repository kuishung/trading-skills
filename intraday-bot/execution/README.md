# execution/ — Layer 3: Alpaca bridge + position management

Receives plans from `strategy/` and:
- Places stop-limit entries
- Attaches OCO bracket on fill
- Actively manages positions:
  - Moves stop to breakeven at the strategy's declared R-multiple
  - (Future) trailing stop when the position runs in our favor
  - (Future) reversal-close when the thesis breaks
- EOD safety sweep at 15:58 ET (`close_all_positions`, `cancel_orders=True`)

The Execution layer is NOT fully strategy-agnostic in its exit policy.
Each strategy declares how it wants positions managed; Execution
honors that contract. Today the policy is hard-coded (OCO + BE@1R);
strategy-aware exits are a planned change.

## Contents

- `orchestrator.py` — The bot. `main()` validates strict risk rules, loads strategies via `strategy.load_known(cfg)`, schedules each strategy's `entry_et`, runs the continuous management loop (`poll_entry_fills` / `poll_breakeven_moves` / `poll_exit_completion`), and force-closes everything at `eod_close_all_et`. CLI: `py execution/orchestrator.py [--dry-run] [--fake-now HH:MM]`.

## Strict risk rules (enforced at startup)

```
- risk_per_trade_pct ≤ 1% of NLV     (global, never override)
- max_position_pct  = 10% of NLV     (global notional cap)
- At least one strategy WIRED        (config block + importable module)
- Each wired strategy: take_profit_R > 0 and max_concurrent > 0
```

All-OFF runtime state is allowed — bot starts and journals
`strategy_off_skipped` per scheduled fire.

## Changelog

### 2026-05-21 — `orchestrator.py`: IBC auto-launch at T-60
- Extended the startup IBKR sequence: if the initial probe fails AND `cfg.ibkr_autolaunch_enabled` is true (default), spawn the IBC launcher .bat (`cfg.ibkr_launcher_bat`) as a DETACHED process (so TWS survives the bot's exit — lets the user keep TWS open for manual trading after the bot finishes the day).
- Retries the probe with a backoff schedule (10/10/10/15/15/20/20s ≈ 100s total) up to `cfg.ibkr_autolaunch_timeout_s` (default 90s). TWS typically logs in within 30-60s.
- New journal events: `ibc_autolaunch_started` (with launcher path + reason for triggering), enriched `data_provider_selected` with `attempt` field (`"initial"` vs `"after_autolaunch"`) and `autolaunch_wait_s`.
- Helper functions: `_spawn_ibc_launcher()`, `_resolve_ibkr_or_fallback()`.
- Use case: user practices manual trading in TWS, leaves PC overnight. At T-60 BMO (08:30 ET) the dashboard auto-starts the bot. The bot probes IBKR, finds TWS not yet logged in (overnight idle), launches IBC, waits, then uses IBKR for the day. No manual login required.

### 2026-05-21 — `orchestrator.py`: startup IBKR probe with Alpaca fallback
- `main()` now calls `resources/ibkr_data.probe_ibkr_reachable()` when `cfg.data_provider=="ibkr"` and `--dry-run` is not set.
- On probe failure: flips `cfg["data_provider"]="alpaca"` for the rest of the session, journals `data_provider_fallback` with the reason, logs a warning. Bot keeps running on Alpaca IEX bars instead of crashing on the first bar fetch.
- On probe success: journals `data_provider_selected`.
- Order routing is unaffected (Alpaca paper either way); only the bar/quote feed source changes.
- Use case: bot auto-starts at 08:30 ET (T-60 BMO) before the user has logged into IB Gateway -- previously this would lock the session to "IBKR unavailable" with no graceful fallback at startup. Now there's a clean decision logged at startup.

### 2026-05-21 — `orchestrator.py` forwards strategy_version + take_profit_R in entry_submitted journal
- `entry_submitted` event payload now includes `take_profit_R=plan.get("take_profit_R")` and `strategy_version=plan.get("strategy_version")`. Lets `review/stats.py` attribute fill/exit outcomes back to a specific strategy rule-set version, which is the foundation of the enrichment loop (Layer 5).
- Pure metadata enrichment; no behavior change. `take_profit_R` is also useful downstream for sanity-checking that planned-R matches achieved-R after a TP fill.

### 2026-05-21 — `orchestrator.py` schedules optional shortlist phase
- New `_fire_strategy_shortlist(cfg, strat, date_iso)` function: gated by `is_enabled()`, calls `strat.shortlist(date_iso, cfg, strat)` if defined, traps + journals exceptions as `shortlist_failed`.
- `phase_run_strategies` main loop now tracks a `shortlisted: set[str]` parallel to the existing `fired` set, fires each strategy's shortlist phase once per day when `now >= shortlist_et`. Strategies without a `shortlist_et` are unaffected.
- `--fake-now` replay path also fires the shortlist phase before the entry phase so replays mirror live behavior.

### 2026-05-21 — Folder established
- Moved `scripts/trade_day.py` → `orchestrator.py`.
- Bootstrap snippet added; `from strategy import ...` uses the package import path now that intraday-bot/ root is on sys.path.
- ON/OFF gate added: `_fire_strategy_entries` skips at top if `is_enabled(strat.name)` is false and emits one `strategy_off_skipped` event.
- ARM gate already in place: skip submit + emit `entry_disarmed` if `not is_armed(strat.name)`.
- Dropped the "at least one strategy enabled" strict rule — runtime gating handles it.
