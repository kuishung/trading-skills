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

### 2026-05-21 — Embedded chart defaults to extended hours
- `web/index.html`: TradingView widget constructor now passes `session_id: 'extended'`, `extended_hours: true`, and `details: true`. Pre-market + after-hours bars render by default — RTH-only would hide the exact PM bars GUNS Setup 1 evaluates (PMH, consol_high) and Setup 5 evaluates (first 1-min RTH candle is fine but PM-bar median range is computed from PM data too).
- The free Advanced Charts widget has limited control vs the registered Charting Library, so it may show the "Extended Hours" toggle in the toolbar even after these flags — passing all three increases the odds the chart starts in extended mode across widget versions. User can also toggle in-chart (right-click → Time → Extended Hours, or the `E` button when visible).

### 2026-05-21 — Strategy-family tabs in the analysis panel
- `web/index.html`: new `.fam-tabs` bar inside the Strategy Analysis panel, between the `<h2>` header and the per-symbol list. One tab per strategy family (derived from `name.split('_')[0].toUpperCase()` — `guns_setup1` → `GUNS`, future `orb_setup1` → `ORB`). Each tab shows a small badge with the count of setups in that family.
- Active tab uses an IBKR-red underline; clicking switches `activeFamily` and re-renders the list filtered to only that family's setups.
- New families appear automatically as soon as they emit any journal event — no code change needed when adding ORB / future strategies.
- Selection persists across reloads via `localStorage` (`intraday_bot:familyTab`); falls back to the first family if the saved value isn't valid anymore.
- Analysis list `max-height` dropped from 540px → 504px to keep the panel the same total height as the chart panel now that the tab bar adds ~36px of chrome.

### 2026-05-21 — Analysis + chart side-by-side (50/50 split)
- `web/index.html`: Strategy Analysis and the embedded TradingView chart panel are now wrapped in a single `grid grid-cols-2 gap-3 items-start` row, so each takes half the dashboard width.
- Analysis list scrolls inside its half (`max-height: 540px; overflow-y: auto`) so a long shortlist doesn't push the chart down.
- Per-symbol row regridded to `60px 130px 1fr` (ticker · verdict · viz+details) with the details column truncated by ellipsis and full text in a hover tooltip — keeps each row to one clean line in the narrower column.
- Chart panel keeps its 560px body height; sits flush to the analysis on the right, updates on every ticker click as before.

### 2026-05-21 — TradingView chart embedded inline (in-flow panel, no popup/modal)
- `web/index.html`: replaced the modal overlay with an **always-visible chart panel** in the dashboard's normal flow, just after the Strategy Analysis section. 560px tall, full-width, dark candles, EMA + SMA + Volume studies pre-loaded. The panel is part of the page — no overlay, no extra window.
- Ticker clicks update the panel's symbol and gently scroll it into view (`scrollIntoView({behavior: 'smooth', block: 'nearest'})`). Initial state shows "click any ticker to load chart" placeholder.
- Timeframe switcher in the panel header: 1m / 5m / 15m / 1h / D. Click rebuilds the widget at the new resolution (the basic embed has no symbol/TF update hook — destroy + recreate is the supported path).
- `show_popup_button: false` so the widget's own popout button is hidden. No new tabs, no new windows from our code.
- `tv.js` is lazy-loaded on first ticker click — initial page load doesn't pay the cost.
- Caveat unchanged: this is the free Advanced Charts WIDGET (anonymous). It cannot authenticate to a user TradingView account; saved layouts / drawings / indicators from TV Desktop won't apply. The TV Desktop CDP bridge is still an option for "your real chart" — separate path, still deferred.

### 2026-05-21 — Click any ticker → TradingView Web
- `web/index.html`: every ticker cell in the Strategy Analysis panel, Pending Orders, Open Positions, and Today's Trades is now a clickable `.ticker-link`. Click opens `https://www.tradingview.com/chart/?symbol=<SYM>` in a new tab (`noopener,noreferrer`). User's logged-in TradingView session loads their saved layout for that symbol.
- New bottom-right toast stack — slides in / fades out with a 2.2s dwell. Confirms each click with "→ AAPL on TradingView".
- Delegated click handler at document level so dynamically-rendered ticker spans always work without per-render rebinding.
- **TV Desktop bridge deferred** — would require a `/tv/open` server endpoint that subprocesses `node resources/tradingview-mcp/src/cli/index.js symbol <SYM>` (CDP via vendored MCP, port 9222). Implementing it needs a dashboard server restart while the live orchestrator is running; orphan-tracking would cosmetically show "bot stopped" until reattached. User chose to defer; TV Web ships now.

### 2026-05-21 — Graphical layer: day timeline ribbon + per-symbol gap-viz + verdict animations
- `web/index.html`: animated **day timeline ribbon** between the status bar and the today-P&L strip. Spans 04:00 → 16:00 ET. Premarket + RTH bands tinted; vertical markers for shortlist (09:00), s1 entry (09:28), open (09:30), s5 entry (09:31), EOD (15:58). White cursor diamond drifts right every second (1s CSS transition for smooth motion). Header line shows "next: <phase> in MM:SS" updated every tick.
- `web/index.html`: each symbol row in the Strategy Analysis panel now carries an inline **SVG mini-viz**:
  - **Setup 1 gap-viz** — horizontal price-range bar with green tolerance band around PMH; green dot if consol_high cleared, red dot if it didn't. You SEE why each rejection happened.
  - **Setup 5 candle-viz** — horizontal bar of `candle_range_ratio` vs `candle_size_mult` ceiling; same pass/fail color logic.
- Verdict pills (`submitted` / `planned` / `rejected`) carry CSS keyframe pulse animations (green/blue/red). New rows slide in with a 350ms `row-slide-in` keyframe when fresh events arrive (flagged via `analysis[strat].recent[sym]`).
- Per-strategy live countdown chip in each analysis header ("entry in 03:14", "EOD in 4h 32m"). Updates every second via a separate `tickStrategyCountdowns()` loop so the full panel doesn't re-render every tick.
- Pure frontend — no server restart needed.

### 2026-05-21 — Strategy Analysis panel (per-symbol live decision pipeline)
- `web/index.html`: new "strategy analysis" panel between the gating panel and the orders/positions row. For each strategy: shortlist time + candidate count, header counters (submitted / planned / rejected / pending), one row per shortlisted symbol with a verdict pill (○ pending · ● planned · ▸ submitted · ✗ rejected) and the diagnostic values that drove the verdict (`pmh`, `consol_high`, `gap_pct`, `candle_range_ratio`, `ema9/20`, `sma50`, `pm_vol`, etc.).
- JS state: `analysis = { strat: { version, shortlistTs, candidates, decisions: { sym: {verdict, plan|reason|values, submitted} } } }`. Fed by `ingestJournalEvent()` on `strategy.<name>.<event>` envelopes; rebuilt from `state.events` on each snapshot via `rebuildAnalysisFromEvents()`.
- Existing event-log + gating panel + orders + positions panels untouched. The new panel is additive — frontend failures inside `ingestJournalEvent` are caught so the rest of the UI keeps working.
- Static file — no server restart needed. Hard-refresh the browser (Ctrl+Shift+R) to load the new bundle.
- Depends on `journal/writer.py` bridging to `events.emit()` so the dashboard sees the `strategy.*` envelopes; see that folder's changelog for the bridge.

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
