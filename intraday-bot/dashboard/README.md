# dashboard/ — operational UI

Real-time observer + control surface for the bot. FastAPI + WebSocket
backend, static HTML/Tailwind frontend. Read-only with respect to
orders — the dashboard never sends orders to Alpaca. The bot
(`execution/orchestrator.py`) does that. The dashboard just observes
the files the bot writes and exposes the per-strategy gating controls.

Everything dashboard-related lives in this one folder (server, web,
Windows launchers, Desktop-shortcut installer).

## Contents

- `server.py` — FastAPI app. Endpoints: `/snapshot`, `/config`, `/bot/{start,stop,status,arm,enable}`, `/shutdown`, `/restart`, `/shutdown-all`, `/ws` (WebSocket). Spawns + monitors `execution/orchestrator.py` as a child process. Auto-launches the bot at `cfg.auto_start_et` on weekdays (default **08:30 ET** — 1 hour before market open). Probes IBKR + Alpaca for the health pills.
- `web/index.html` — single-page UI. Status bar, today P&L tile, per-strategy gating panel (ON/OFF + ARM pills, with bulk shortcuts), pending orders / open positions, today's trades, event log + bot log.
- `start_dashboard.bat` — idempotent Windows launcher. Opens the browser to `http://localhost:8000`. If the dashboard is already running, just opens the browser.
- `stop_dashboard.bat` — graceful `POST /shutdown`, then port-kill fallback.
- `_supervise_dashboard.bat` — supervisor loop. Re-launches `server.py` on exit code 100 (Restart signal). Underscore-prefixed name reflects that users don't invoke it directly — `start_dashboard.bat` spawns it minimised.
- `setup_launcher.py` — one-time launcher installer. Drops `.lnk` files in TWO places: (a) the user's real Desktop (handles OneDrive / AD-redirected Desktops) and (b) this `dashboard/` folder itself, so the shortcut sits next to its target and shows up the moment you navigate into the synced folder on any PC. The in-folder `.lnk` files are gitignored (absolute paths are per-PC). Run once per PC after a fresh Dropbox sync.
- `Intraday Bot Dashboard.lnk` / `Intraday Bot Dashboard (stop).lnk` — created by `setup_launcher.py`. Gitignored. Double-click to launch / stop the dashboard.

## URLs

- `http://localhost:8000/` — main dashboard
- `http://localhost:8000/snapshot` — JSON snapshot (state + health)
- `http://localhost:8000/bot/status` — bot status + gating maps
- `http://localhost:8000/bot/enable` / `/bot/arm` — GET to inspect, POST to toggle

## Changelog

### 2026-05-21 — `setup_launcher.py` also drops `.lnk` files in this folder
- `setup_launcher.py`: now creates the two shortcuts in TWO places — Desktop (as before) AND in this `dashboard/` folder itself. When the user navigates into the synced intraday-bot/dashboard/ folder via Windows Explorer on any PC, the launcher is right there.
- Both sets contain absolute paths so they're per-PC; the in-folder copy is gitignored (`dashboard/*.lnk` added to root `.gitignore`). Re-run the installer once per new PC after a fresh Dropbox sync.

### 2026-05-21 — Auto-start moved to 08:30 ET (T-60 BMO)
- `server.py`: fallback default for `auto_start_et` changed from `"09:15"` → `"08:30"`. The `/config` endpoint's fallback default also bumped to match.
- Rationale: 1 hour before market open gives the bot a 30-minute warm-up window before Setup 1's shortlist phase fires at 09:00 ET (T-30 BMO). Connectivity probes, IBKR handshake, Alpaca health check all complete well before any decision logic runs.
- `_auto_start_loop()` docstring updated to reflect the new timing.

### 2026-05-21 — Everything dashboard-related consolidated here
- `scripts/dashboard.py` → `server.py`
- `web/` → `dashboard/web/`
- `start_dashboard.bat`, `stop_dashboard.bat`, `_supervise_dashboard.bat` moved from intraday-bot/ root.
- `scripts/setup_dashboard_launcher.py` → `setup_launcher.py`.
- Supervisor updated: now `cd`s to this folder and runs `py server.py` (no `dashboard\` prefix needed since it's a sibling).
- `setup_launcher.py` `START_BAT` / `STOP_BAT` paths point to this folder.

### 2026-05-21 — Per-strategy ON/OFF + ARM gating UI
- Old single ARMED/DISARMED pill in the header replaced by a per-strategy gating panel under the Today P&L tile.
- Each strategy row has TWO pills (ON/OFF + ARM/DISARM). Bulk shortcuts: enable-all, disable-all, arm-all, disarm-all.
- Header summary pill shows "N ON · K ARMED".
- ARM pill is dimmed when the strategy is OFF (state remembered, just won't fire until ON).
- New `/bot/enable` endpoint pair mirroring `/bot/arm`.

### 2026-05-21 — Scanner UI removed from `web/index.html`
- Removed the "live top movers" panel + scanner-pill from the header, plus the JS state + render functions that fed them (`renderScanner`, `setScannerStatus`, `primaryScannerForNow`, `latestScannerFromEvents`, `fmtCompactVol`, the `scanner.snapshot` event handler) — ~200 lines.
- Removed the scanner subprocess management from `server.py` (`SCANNER_SCRIPT`, scanner spawn/stop logic, `scanner_status` / `scanner_pid` properties, scanner entries in `_snapshot` / `_poll_health` / auto-start broadcasts, `scanner_start_et` / `scanner_end_et` from `/config`).

### 2026-05-21 — Folder established
- Initial split from `scripts/dashboard.py` + `web/`.
