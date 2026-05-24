# scripts/ — operational glue (cross-cutting)

What doesn't fit cleanly into any single layer (resources / strategy /
execution / journal / review / dashboard). Helpers used by everything,
plus one-time installers.

## Contents

- `_common.py` — Config loader, ET clock, VAULT-first credential resolution (Alpaca, Telegram), state file paths, Telegram send, fill-event append, and the data-provider abstraction (`get_pm_bars` / `get_rth_minute_bars` / `get_latest_quote` / `get_latest_trade` — dispatches to IBKR or Alpaca by `cfg["data_provider"]`, with auto-fallback on IBKR failure). Underscore prefix marks it as an internal helper module.
- `_gating.py` — Per-strategy gating: ON/OFF flag (`state/enabled_<name>.flag`) and ARM flag (`state/armed_<name>.flag`). API: `is_enabled` / `set_enabled` / `is_armed` / `set_armed` plus bulk forms and a `bootstrap_from_config` helper that seeds the enable flags from `cfg.strategies.<name>.enabled` on first run. Read live by both the orchestrator and the dashboard.
- `setup_ibkr.py` — Interactive setup: writes IBKR creds + IBC bundle config. Asks for username / password / paper-or-live / IBC paths. Idempotent.
- `setup_gateway_autostart.py` — Optional Windows auto-start for IB Gateway (registry Run key).
- `setup_schedule.py` — Optional Windows Task Scheduler job that fires `execution/orchestrator.py` at 08:55 ET on weekdays.
- `wait_and_ingest.py` — One-shot operational glue: wait for a specified Windows PID to exit (poll via `tasklist` every 60s), then launch `resources.ibkr_history.bulk_update` on a universe of choice. Reusable for any "ingest A finishes → ingest B starts" sequence where the two would otherwise collide on IBKR clientId. CLI: `py scripts/wait_and_ingest.py --wait-pid 21732 --timeframes 3min --seed-days 180 --force-seed --universe daily`. The `--universe` option supports `daily | 1min | 3min | journal` so a backtest re-seed can target the full daily-parquet universe (~1500 symbols) rather than the narrower journal-derived live universe (~50).

## Why these are not in their own layer folder

`_common.py` and `_gating.py` are touched by every layer. Putting them
in any one layer would be wrong; putting them in a "lib/" or "core/"
would be another layer name to remember. Keeping them in `scripts/`
matches the historical convention.

The `setup_*.py` installers are one-shot tools, not part of the
runtime trading system. They could move to `bin/` or `tools/` but
they're rarely-touched and small, so `scripts/` is fine.

## Changelog

### 2026-05-24 — `wait_and_ingest.py`: launch-after-PID-exit watcher for ingest sequencing

Companion to the `--force-seed` flag added to `resources/ibkr_history.py` today. Use case: DITP P2 backtest needs 180 days of 3min depth, but the current 14-day ingest is still running and both would collide on IBKR clientId 83 if started simultaneously. This script polls the running ingest's Windows PID via `tasklist` (Git Bash `ps -p` doesn't see native Windows processes), and when it exits, immediately launches the 180-day re-seed.

Reusable beyond this one-off: any future "ingest A → ingest B" sequence (e.g., extending 1min depth after 3min completes) can use the same script with different `--wait-pid` / `--timeframes` / `--seed-days` args.

`--universe` flag supports four sources: `daily` / `1min` / `3min` (all symbols with a parquet in that timeframe — the broad backtest universe), `journal` (the narrow live universe derived from recent journal events + watchlist + `cfg.history_universe` — what `ibkr_history.py update --universe` defaults to).

Logs to `data/_ingest_<tf>_<lookback>d_<ts>.log` — both the watcher status messages AND the bulk_update stdout/stderr. Tail-able while running.

Launch pattern (one-shot, runs unattended):
```
nohup py scripts/wait_and_ingest.py --wait-pid 21732 \\
    --timeframes 3min --seed-days 180 --force-seed --universe daily \\
    > /dev/null 2>&1 &
disown
```

### 2026-05-22 — `_common.py`: parquet data-provider branch for replay
- `get_pm_bars()` and `get_rth_minute_bars()` now recognise a third `cfg["data_provider"]` value: `"parquet"`. When set, both route to `_parquet_minute_bars()` which reads from `bars_store.load_bars(sym, start=..., end=..., timeframe="1min")`.
- `_parquet_minute_bars(symbols, cfg, fake_now, pm_only)` reads `cfg["replay_date"]` (set by `execution/orchestrator.py --replay-date`), builds an ET datetime range [04:00, cutoff] for the replay day, converts to UTC ISO, and calls `bars_store.load_bars()` per symbol. `pm_only` controls the default cutoff (09:30 vs 16:00) when `fake_now` is None.
- The provider abstraction stays clean — strategy code is unchanged, just calls `get_pm_bars(symbols, cfg)` and gets the right bars for whichever provider is configured.
- See `review/README.md` → "Replay workflow" for the end-to-end CLI usage.

### 2026-05-21 — Slimmed down to operational glue only
- Removed `_journal.py`, `_events.py` (moved to `journal/`).
- Removed `_ibkr_data.py`, `_smoke_ibkr.py`, `_dryrun_ibkr.py`, `guns_float_lookup.py`, `guns_catalyst_classifier.py` (moved to `resources/`).
- Removed `guns_scanner.py` (moved to `strategy/GUNS/`).
- Removed `signals.py`, `strategies/` (moved to `strategy/`).
- Removed `trade_day.py` (moved to `execution/`).
- Removed `dashboard.py`, `setup_dashboard_launcher.py` (moved to `dashboard/`).
- Remaining: `_common.py`, `_gating.py`, `setup_ibkr.py`, `setup_gateway_autostart.py`, `setup_schedule.py`.

### 2026-05-21 — `_common.py` self-contained
- Dropped `../alpaca-trader-paper/.env` and `../MATP/.env` sibling-folder reads.
- New `_env_lookup` walks: `$INTRADAY_ENV_DIR` → `intraday-bot/.env` → `<Dropbox>/VAULT/Claude Credential/<vendor>.env`.
- `load_alpaca_env` and `telegram_env` use the new lookup. No more cross-folder dependencies.
- Added a sys.path bootstrap (adds intraday-bot/ root + every layer folder) so the lazy `from ibkr_data import ...` calls inside the data-provider dispatch functions resolve regardless of how the importer is invoked.

### 2026-05-21 — `_arming.py` → `_gating.py`
- Renamed to reflect the new ON/OFF gate added alongside the existing ARM gate.
- API now: `is_enabled` / `set_enabled` / `all_enabled_state` / `set_all_enabled` alongside the `_armed` siblings.
- Added `bootstrap_from_config(cfg, known)` — seeds enable flags from `cfg.strategies.<name>.enabled` on first run; after that the flag wins.
- Added `migrate_global_arm_flag(known)` — one-shot migration of the legacy single `state/armed.flag` to per-strategy `state/armed_<name>.flag`.

### 2026-05-21 — `_arming.py` added
- Per-strategy ARM flag (`state/armed_<name>.flag`).
- `is_armed(name)` is called from the bot's submit site so toggles take effect mid-session.
