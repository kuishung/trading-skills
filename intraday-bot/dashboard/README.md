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

### 2026-05-22 — DITP universe filter pills
- `strategy/DITP/scanner.py`: each `P2Candidate` now carries a `universes: list[str]` field (e.g. `["sp500"]`, `["sp600","nasdaq100"]`). Populated by `build_universe_map()` which lazy-imports `sp500`, `sp_midcap400`, `sp_smallcap600`, `nasdaq100`, `djia` and inverts each into `{symbol: [universe_names...]}`. Module-level cache so `detect_p2()` doesn't rebuild per symbol.
- `web/index.html`: DITP tab now has a row of filter pills above the table: **All · S&P 500 · MidCap · SmallCap · NDX-100 · DJIA**. Each pill shows its candidate count; click to filter the table to just that universe. Active pill highlighted in IBKR red. Empty universes are dimmed and unclickable.
- Filter state lives in `ditpUniverseFilter` (default `"all"`); selecting a pill re-renders via `renderAnalysis()`. Tier counters (A/B/C) update with the filter — e.g., filtering to S&P 500 shows the tier breakdown of just S&P 500 candidates.
- Row tooltip shows the symbol's universe membership(s) so multi-index names (e.g. AAPL = S&P 500 + NDX-100 + DJIA) are visible.
- Watchlist on 2026-05-22 snapshot: 51 candidates total — 33 SmallCap, 9 MidCap, 9 S&P 500, 2 NDX-100, 2 DJIA (overlap because most NDX-100 / DJIA names are S&P 500 too).

### 2026-05-22 — Console layout: left sidebar + slide-out drawers
- New 44px fixed left sidebar with three vertical text labels: **Gating · Events · Bot log**. Click any label to slide out the matching drawer (one open at a time). Click outside / ESC / drawer's ✕ close.
- Moved out of the main scroll flow into drawers:
  - **Strategy gating** (the ON/OFF + ARM/DISARM controls + bulk shortcuts) — previously took ~140 px of vertical space below the today-P&L strip; now hidden until you need it.
  - **Event log** + **Bot log** — were a two-column row at the bottom of the page; both now live in their own drawers.
- Element IDs preserved (`#strategy-gate-list`, `#events`, `#botlog`, `#enable-all-btn` etc.) so all the existing JS hooks (`renderGatePanel`, `renderEvents`, `renderBotLog`) work without changes — just paint into the same IDs which now live inside the drawer panels.
- Main viewport now shows: status bar → day timeline → today P&L → Strategy Analysis + Chart (50/50) → orders + positions → today's trades. Much cleaner; the secondary controls are one click away from the sidebar.
- Body has `padding-left: 44px` so main content never collides with the sidebar.
- Drawer width 420 px (max-92vw on mobile), slides in via CSS `transform: translateX(-110% → 0)` with 0.18s easing.

### 2026-05-22 — DITP tab in Strategy Analysis (watchlist view)
- `server.py`: new endpoint `GET /strategy/ditp/watchlist` reads the highest-dated `state/watchlist_ditp_<date>.json` and returns its full payload (target_date, n_candidates, scanner_run_at_utc, candidates list). Source-of-truth filename returned as `_file` so the UI can show provenance.
- `web/index.html`: DITP is now a tab in the Strategy Analysis panel alongside GUNS. The tab appears automatically when `ditpWatchlist.candidates.length > 0` (it does the moment the scanner has run for the upcoming session). `renderAnalysis()` dispatches by family: GUNS → existing journal-event-driven per-symbol view; DITP → `renderDitpWatchlist()` table.
- DITP watchlist UI: target-date header + counts per tier (A/B/C/D), then a table of `tier · sym · variant · score · last · resistance zone (low → high) · distATR · t/m/rM · cautions`. Tier badge uses new `.tier-A/B/C/D` CSS classes (green / amber / dim / muted). Symbol cell is a `.ticker-link` so clicking opens the chart panel.
- DITP and GUNS share NOTHING about their content model — GUNS is live decisions (journal-event stream), DITP is end-of-day scanner output (file). The family-tabs scaffolding handles both transparently.
- Initial fetch on page load + 5-min refresh (scanner is EOD-driven; no need for faster polling).
- Required server restart (new endpoint).

### 2026-05-22 — Data-health pill in status bar + per-symbol modal
- New `Data` pill in the status bar (between Alpaca and bot status). Polls `/data/health` every 60s. Color follows `overall`: green `N fresh`, amber `N stale[ · M invalid]`, red `N ancient[ · M invalid]`.
- Click the pill → opens an in-page modal (no popup) showing the three-panel breakdown — **FRESHNESS** (fresh / stale / ancient / missing counts), **CONSISTENCY** (sorted + no-dupes + no-big-gaps check), **VALIDITY** (OHLCV sanity per bar). Plus per-symbol lists of the offenders (`stale_symbols`, `ancient_symbols`, `invalid_symbols`) and the last 30 `data/ingest_log.jsonl` entries with timestamp / source / symbol / bars_added / error.
- New server endpoints (`dashboard/server.py`): `/data/health[?timeframe=daily&details=true]` and `/data/ingest-log[?tail=30]`. Both delegate to `resources/data_integrity.py`. Wrapped in try/except so a broken parquet never takes down the dashboard.
- Required server restart (new endpoints added). Existing orchestrator was already exited post-EOD, so the restart was clean.

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
