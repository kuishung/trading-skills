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
- `setup_hermes_supervisor_task.ps1` — **One-shot Windows Scheduled Task installer for the Watch-Ingest.ps1 supervisor.** Registers `IntradayBot-Watcher` to run at Hermes boot under the Administrator principal (LogonType S4U, RunLevel Highest), with `RestartCount 3` and no execution-time limit. Pairs with `Watch-Ingest.ps1` to give the full failure-recovery chain: watcher crashes -> supervisor restarts (30s); supervisor crashes -> Task Scheduler restarts (60s, up to 3x); Hermes reboots -> task auto-fires. Survives RDP disconnects (the trap that killed the supervisor twice on 2026-05-26). Idempotent (-Force). Detects already-running supervisors and refuses `-StartNow` to prevent IBKR clientId collisions. ASCII-only per the PS 5.1 em-dash lesson. Run once per Hermes rebuild: `powershell -ExecutionPolicy Bypass -File scripts\setup_hermes_supervisor_task.ps1 -StartNow`.
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

### 2026-05-26 - `wait_and_ingest.py`: also passes `log_callback=log` to bulk_update

Follow-up to today's earlier `skip_up_to_date=True` entry. On Hermes the supervisor runs under Task Scheduler, which discards the watcher's stdout — so the pre-flight summary lines and per-iteration progress that `bulk_update` writes to stdout were never landing in the watcher's `_ingest_*.log` file. Passing `log_callback=log` routes everything through the same logger that writes the log file, so the dashboard tray can parse the pre-flight summary and the user can `Get-Content $log -Tail N` to see real progress. See `resources/README.md` changelog for the matching `bulk_update` parameter addition.

### 2026-05-26 - `wait_and_ingest.py`: passes `skip_up_to_date=True` to bulk_update

User feedback: *"when the ingest restart it always start from the A, i want it to have a log to confirm the which has been done and which now so i can save a lot of time"*. The watcher's bulk_update call now skips (sym, tf) pairs already at full target depth, which is exactly the right semantics for the watcher (it's the backfill tool; today's incremental top-up is the orchestrator's post-EOD job, not this script's). See `resources/README.md` changelog for the matching `bulk_update` pre-flight + `skip_up_to_date` parameter change.

Net effect for the user: on watcher restart, the log now opens with a pre-flight summary telling you exactly how many pairs are done vs. remaining (e.g., `per-timeframe completion: 3min=1456/1518 5min=1462/1518 daily=1456/1518`), then iterates only the gap-fill work with `[i/N]` progress prefixes. No more "scanning from A to wherever you got" wasted pacing time.

### 2026-05-26 - `setup_hermes_supervisor_task.ps1`: scheduled-task installer

- Trigger: Hermes's supervisor died twice today. Once from the em-dash parser crash (fixed in `Watch-Ingest.ps1`), once from RDP disconnect killing the interactive PowerShell window. The second death was the bigger lesson: any supervisor launched inside an RDP PS window dies when the RDP session ends, taking the watcher with it. The fix isn't a code change to the supervisor -- it's running the supervisor under Windows Task Scheduler so it lives in a session decoupled from any RDP login.
- We worked out the right `Register-ScheduledTask` incantation interactively (`-AtStartup` trigger, `RestartCount 3 / RestartInterval 1m`, `-LogonType S4U`, `-RunLevel Highest`, `ExecutionTimeLimit 0` for multi-day runs, Administrator principal). That incantation now lives in a tracked script instead of only in chat history.
- **Idempotent**: re-running overwrites the existing task definition with `-Force`.
- **Collision-safe**: detects an already-running `Watch-Ingest.ps1` process (via CIM with Get-Process fallback) and refuses `-StartNow` to avoid spawning a duplicate supervisor that would fight over IBKR clientId 84. We hit exactly that collision today; the script now prevents the rerun version of it.
- **ASCII-only** per the em-dash lesson, plus belt-and-suspenders: PS 5.1 has no null-coalescing (`??`) -- discovered when the parser flagged my first draft. Explicit `if/else` instead. Both the ASCII rule and the `??` ban are now noted in the file's header.
- Verified end-to-end on Hermes today: registered (`State: Ready`), survives RDP disconnect (will be properly tested at next Hermes reboot).

### 2026-05-26 - `Watch-Ingest.ps1`: ASCII-only source (PS 5.1 em-dash parser bug)

- Hermes ran the freshly-pulled supervisor and hit `TerminatorExpectedAtEndOfString` at line 109 + "Missing closing '}'" at line 102 - both red herrings. Root cause: Windows PowerShell 5.1 reads `.ps1` files as the system ANSI codepage (Windows-1252) when there is no UTF-8 BOM, so the em-dashes (`-` U+2014, UTF-8 bytes `E2 80 94`) decode as three Win-1252 chars where the third byte (`0x94`) is U+201D RIGHT DOUBLE QUOTATION MARK. PS 5.1's tokenizer treats curly quotes as alternates for `"`, so it silently closed/reopened strings mid-file and the parser exploded much later in a way that pointed nowhere useful.
- Fix: replaced every em-dash in `Watch-Ingest.ps1` with an ASCII hyphen `-`. Verified zero non-ASCII bytes remain and `[System.Management.Automation.Language.Parser]::ParseFile` returns no errors.
- Hard rule going forward: **every `.ps1` in this repo stays pure ASCII**. PowerShell Core 7+ defaults to UTF-8 without BOM, but Hermes (Server 2019) ships Windows PowerShell 5.1, and we must keep both happy. If a future script genuinely needs Unicode, save it with a UTF-8 BOM AND test it under `powershell.exe` (not just `pwsh`).
- Hermes recovery: `git pull` then re-run the same `Watch-Ingest.ps1` invocation - no other changes needed.

### 2026-05-26 - `wait_and_ingest.py` + `Watch-Ingest.ps1`: `--symbols-file` for pure-IBKR runs

- User rule: *"make sure the code only seed from IBKR, nothing outside IBKR"*. Previously `--universe daily` required existing daily parquets to resolve the symbol list — on a fresh Hermes with all parquets wiped, we had to use yfinance to bootstrap that. Now there's a third option that needs neither parquets nor network.
- **`--symbols-file <path>`** (new flag in `wait_and_ingest.py`): reads the universe from a plain-text file (one symbol per line, `#` comments and blank lines skipped, Windows reserved names defensively filtered). Overrides `--universe` when set. Paired with `resources/universe_full.txt` (1518 syms, committed to git).
- **`-SymbolsFile <path>`** (new param in `Watch-Ingest.ps1`): forwards to the watcher's `--symbols-file`. When set, the supervisor doesn't pass `--universe` at all.
- **`-ForceSeed` default flipped from `$true` to `$false`** in the supervisor — smart-resume (added the same day in `ibkr_history.bulk_update`) makes force-seed unnecessary for the resume case. User opts in explicitly if they want to wipe.
- Now the full launch on Hermes is yfinance-free: `Watch-Ingest.ps1 -Timeframes "3min:180,5min:180,daily:730" -SymbolsFile resources\universe_full.txt`

### 2026-05-26 — `wait_and_ingest.py`: `--timeframes` accepts `TF:DAYS` per-timeframe depths

- User rule: *"I will need 1d (2 years), 3m and 5m for 180 days"*. Single `--seed-days` was too rigid — daily wants more history (EMA200 + 2-year backtest window) while intraday timeframes can stay tight at 180d.
- New syntax: `--timeframes "3min:180,5min:180,daily:730"`. Each comma-separated entry may carry `TF:DAYS`. Entries without `:DAYS` fall back to `--seed-days` (default 180).
- Parser is forgiving: whitespace tolerated, malformed depth values (non-int after the colon) trigger a clean argparse error.
- Filename of the per-run log file (`_ingest_<tf_label>_<days>d_<ts>.log`) now uses the LARGEST `:DAYS` value seen in the run, so a mixed 180d+730d run is logged as `_ingest_3min-5min-daily_730d_*.log` — captures the outermost lookback in the filename.
- Passes the parsed dict to `ibkr_history.bulk_update(lookback_days_by_tf=…)` so each per-(sym,tf) ingest uses the right depth.

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
