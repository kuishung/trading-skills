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
- `Watch-Ingest.ps1` — **Process supervisor for `wait_and_ingest.py`.** PowerShell forever-loop that relaunches the watcher every time it exits (clean finish, crash, OOM, killed). Pairs with the in-Python resilience in `resources/ibkr_history.py` (socket reconnect + per-symbol error skip). Run from a foreground PS window: `powershell -ExecutionPolicy Bypass -File .\scripts\Watch-Ingest.ps1`. Stops on Ctrl+C. Logs to `<data_root>/_supervisor_<timestamp>.log`. Parameters mirror `wait_and_ingest.py` (`-Timeframes`, `-SeedDays`, `-ForceSeed`, `-Universe`, `-RestartDelay`).
- `hermes_health.py` — **Hermes VM pre-flight check.** Run on the Hermes VM after Phase 1+2 of `HERMES_SETUP.md` is complete, BEFORE kicking off the multi-day 180-day re-seed. Verifies Python version, required packages importable, bars_store readable, disk space adequate, `ibc/credentials.txt` present, `config.json` clientId matches the host (warns if Hermes still has laptop's 71), IBKR Gateway socket reachable (port 4002 → paper Gateway, 7497 → paper TWS fallback). Safe to run on any PC; flags Python 3.14 + ib_insync asyncio incompatibility with a clear remedy ("use `py -3.12` for IBKR workloads"). CLI: `py scripts/hermes_health.py` (or `--skip-ibkr` for the no-Gateway-yet case, `--json` for machine-readable).

## Why these are not in their own layer folder

`_common.py` and `_gating.py` are touched by every layer. Putting them
in any one layer would be wrong; putting them in a "lib/" or "core/"
would be another layer name to remember. Keeping them in `scripts/`
matches the historical convention.

The `setup_*.py` installers are one-shot tools, not part of the
runtime trading system. They could move to `bin/` or `tools/` but
they're rarely-touched and small, so `scripts/` is fine.

## Changelog

### 2026-05-26 — `Watch-Ingest.ps1`: process-level supervisor for the watcher

- User rule: *"i prefer to run it without interruption"*. The existing in-Python resilience covers IBKR socket reconnects (up to 20 attempts in `ibkr_history._ensure_connected`) — but if the Python process itself dies (crash, OOM, killed by an errant `Stop-Process`), nothing relaunches it. The supervisor closes that gap.
- **Forever-loop**: launches `py -3.12 scripts/wait_and_ingest.py ...` and waits for exit; on any exit code, sleeps `-RestartDelay` (default 30s) then relaunches. Continues until Ctrl+C or PS window close.
- **Clean exit ≠ done forever**: when the run finishes the full universe (exit 0), the supervisor still relaunches — that's intentional, the next pass will do incremental updates only (existing parquets get top-up bars from the last stored timestamp), which is the right behaviour for an "always-fresh" data warehouse.
- **Per-iteration logging** to `<data_root>/_supervisor_<timestamp>.log`: launch time, exit code, runtime per iteration. Useful for forensic analysis if the watcher is dying often.
- **Pairs with the `ibkr_history.bulk_update` per-symbol try/except** added in the same commit — together they cover the two main interruption sources: per-symbol failures (in-process) and process-level death (out-of-process).
- Not used by default — the user still launches via `Start-Process … -WindowStyle Hidden` for single-shot runs. The supervisor is for "really truly never stop" scenarios (e.g., overnight Hermes runs that span multiple days).

### 2026-05-24 — `hermes_health.py`: pre-flight check for the Hermes VM

Companion to `HERMES_SETUP.md` (at intraday-bot/ root) and the CLAUDE.md "Hermes — autonomous worker VM" section added today. User is standing up a Hyper-V VM on their office Dell R720 (Windows Server 2019 host, Server 2019 guest VM named "Hermes") to take over multi-day ingest jobs from the laptop.

The script verifies everything that needs to be in place BEFORE kicking off the 180-day full-universe 3min re-seed (which runs unattended for ~4 days):

| Check | What it catches |
|---|---|
| Python version + required packages | Missing `pip install -r requirements.txt`, wrong Python interpreter |
| `bars_store.list_symbols("daily"/"3min")` | Dropbox sync incomplete, parquet dir wrong path |
| Disk space | <50 GB free = re-seed will fail mid-run |
| `ibc/credentials.txt` existence (without printing) | IBC not configured — IB Gateway won't auto-launch |
| `config.json` ibkr_client_id | Hermes still has laptop's clientId 71/83 (will collide with running laptop sessions) |
| IBKR Gateway port 4002 reachable (TCP probe, no IB slot consumed) | IBC not running / Gateway crashed / wrong port |
| `strategy.DITP.scanner.detect_p2` importable | Code sync broken |
| `strategy.DITP._decision_engine` v0.1.0 functions present | Today's backtest scaffold not synced yet |
| `review._adapter_registry.known()` includes `ditp_p2` | Adapter not registered |

Exit code 1 if any FAIL — operator must fix before continuing. WARNs are non-blocking but worth reviewing.

Special handling: ib_insync on Python 3.14 fails at module load (eventkit's `asyncio.get_event_loop()` raises since 3.14 removed implicit loop creation). The check catches the specific RuntimeError and recommends using `py -3.12` for IBKR workloads — without that diagnostic, the user would see a cryptic asyncio traceback.

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
