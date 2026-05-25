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
- `tray_status.py` — Windows system-tray icon for the ingest pipeline. Polls `data/ingest_log.jsonl` every 30s, animates a heartbeat pulse when actively writing (green), idle (yellow), stopped (red), or unknown (gray). Right-click menu exposes Show Status / Refresh Now / Reset Milestones / Open Log File / Open Watcher Log / Quit. Fires Windows toast notifications when count milestones (50/100/250/500/1000 symbols) or letter-group completions are crossed. Run with `py -3.12 dashboard/tray_status.py`. Independent of `server.py` — runs anywhere with a desktop session (laptop or Hermes RDP).

## URLs

- `http://localhost:8000/` — main dashboard
- `http://localhost:8000/snapshot` — JSON snapshot (state + health)
- `http://localhost:8000/bot/status` — bot status + gating maps
- `http://localhost:8000/bot/enable` / `/bot/arm` — GET to inspect, POST to toggle

## Changelog

### 2026-05-25 — Market Sentiment: compact graphic view + 3-mode toggle

- User rule: *"the market sentiment are too big i need a concise one and adjustable for the watchlist below to be pull up. I need it to be shown by graphic rather than too wordy"*. The original panel could swell to 60vh with 4 labelled cell groups plus 6 breadth tiles — ~400px on a 1080p monitor, eating into the Active Lists watchlist below it.
- **New compact graphic view (default):**
  - **Sentiment gauge bar** — horizontal red→amber→green gradient with a white marker showing where the composite score sits on a -100 / +100 scale. Replaces the textual sub-score tooltip as the primary at-a-glance read.
  - **Colored dot rows** — one dot per ETF (indices / VIX / sectors), color = direction (`up` green, `down` red, `flat` grey, `strong-up`/`strong-down` glow when |pct| > 1.5%). Hover shows symbol + percent; click loads chart. ~9px dots, three group labels (IDX / VIX / SEC).
  - **Mini breadth row** — A/D ratio, NH/NL, %>50SMA in a single line of plain numbers (color-coded). Drops the 6-tile grid entirely.
  - Total compact height ~110px vs ~400px expanded — **~3.6× more room for the watchlist** without losing any signal.
- **3-mode toggle** in the panel header — cycles `compact → expanded → collapsed → compact`:
  - **compact** (default) — the new graphic view
  - **expanded** — the original labelled-cell view (kept intact for users who want exact numbers without hovering)
  - **collapsed** — 38px header pill only, lets the watchlist take the whole right column
  - Mode persisted in `localStorage.sent_mode` so the choice survives reloads.
- **CSS `max-height` cap reduced**: 60vh → 35vh for expanded mode. Compact mode caps at 130px. Collapsed at 38px. None of the three can dominate the right column anymore.
- `renderSentiment(payload)` continues to render the full expanded view; a new `renderSentimentCompact(payload)` paints the compact view from the SAME payload, so toggling between modes is pure CSS show/hide — no extra fetches.

- User follow-up: *"to in the tray icon to show me the total ingestion percentage done"* — the outer arc gave a visual sense of progress but no exact number until you hovered for the tooltip. Now the percentage is drawn **into the icon itself** so a glance at the tray tells you "47%" without any hover.
- **`_make_circle_icon()` gains percentage text rendering.** Centered, bold (Segoe UI Bold → Arial Bold → PIL default fallback chain), white with 1px black halo so it's legible against any state color. Auto-shrinks from 22px to 18px at 100% so "100%" fits without bleeding off the icon.
- **Below 1% (rounds to "0%") the text is suppressed** — showing "0%" when the run has actually done a few symbols would be misleading. The arc starts being visible around 1-2% anyway, so the icon never looks "empty + ingest running" at the same time.
- **Heartbeat moved from inner dot to background brightness pulse.** The inner dot occupied the icon center, but that's now reserved for the percentage text. Heartbeat now manifests as the colored fill lightening ~12% every other frame — same 1Hz cadence, same proof-of-life signal, just at the perimeter instead of the center. `heartbeat_phase` (0 or 1) replaces the old `inner_dot_radius` parameter.
- **Four signals composed in one icon now:**
  - Fill color = ingest state (green/yellow/red/gray)
  - Outer arc = progress (continuous visual)
  - Centered text = progress (exact number)
  - Background brightness pulse = tray-script-alive (running only)
- **`ImageFont` lazy-loaded** at icon-draw time; falls back gracefully if no TTF fonts are installed (would only happen on a stripped-down Windows install).

### 2026-05-25 — Tray icon: outer progress arc + N/target tooltip

- User clarified that "milestone" meant a **persistent visible progress indicator**, not just toasts at thresholds — *"what i mean milestone is like a status bar of the ingestion"*. Toasts only fire at 50/100/250/500/1000; between those there was no at-a-glance way to see how far through 1519 symbols the run was without right-click → Show Status or hovering for the tooltip.
- **Outer progress arc.** `_make_circle_icon()` gains a `progress: float` parameter (0.0-1.0). When > 0, draws a white 4px arc starting at 12 o'clock and sweeping clockwise inside the black outline. 50% progress = half ring, 100% = full ring. Visible alongside the color (state) and inner-dot pulse (heartbeat) — three signals composed in one 64×64 icon.
- **Dynamic icon generation.** Previously `FRAMES[state]` was a precomputed list of icons rebuilt once at module load. Progress changes every poll, so pre-rendering doesn't work. Replaced with `_icon_for(state, frame_index, progress) -> Image.Image` that composes the icon on demand. PIL renders ~1-2ms per 64×64, cheap at the 1Hz heartbeat tick.
- **`get_progress()` now returns `target` + `progress_fraction`.** Target = universe size via `bars_store.list_symbols('daily')` (1519 on the current laptop seed). Progress fraction clamped to [0, 1]. Both fed to the icon renderer and the tooltip.
- **Tooltip reframed as a progress bar:** `"BCC | 22/1519 (1%) | letter A (0 done) | +2731 bars"` instead of the old `"BCC | letter B (1 done) | 25 syms in run | +3185 bars"`. The denominator + percent make it read as progress, not just a counter.
- **Heartbeat + milestone toasts unchanged** — the arc is additive, not a replacement. Toasts still fire at 50/100/250/500/1000; the arc gives a continuous signal between thresholds.

### 2026-05-25 — Tray-icon milestone toasts + current-run gap detection

- User rules (chat 2026-05-25): *"the tray icon, i need it to be have heatbeat so that i would know the ingestion is working"* and *"i need the status bar to tell me a milestone"*. Heartbeat shipped earlier today (commit `7917c45` — a pulsing inner dot on the green "running" state, animation cycles at 1Hz, proves the tray script itself is alive independent of ingest state). Today's follow-up adds **milestone toasts** so the user gets explicit notifications as the 180-day re-seed progresses through the alphabet.
- **Count milestones** at 50 / 100 / 250 / 500 / 1000 symbols — Windows toast fires once per crossed threshold. *"Ingest milestone: 100 symbols — currently on BAC (letter B)."*
- **Letter-completion milestones** — each time a letter group fully clears (last symbol from that letter is past), a toast announces the completion: *"Letter A done. Now on B. ETA 60h."*
- **State persisted** in `state/tray_milestone.json` (gitignored) so each threshold fires exactly once across tray restarts. The right-click menu's new **Reset Milestones** item clears the file so the next thresholds re-fire — useful after a manual re-run or when smoke-testing.
- **Gap-detection for "current run"** — naïve symbol-counting in a 72-hour lookback window double-counted because the laptop's 14-day ingest yesterday + the Hermes 180-day re-seed today both landed inside the window (`symbols_done = 1514` was the pathological number). `get_progress()` now walks backward through the timestamps; the latest gap > 1 hour between writes marks the boundary between this run and whatever came before. Only entries after that boundary count toward `symbols_done`, `letters_done`, `rate_per_hour`, and `eta_hours`. Smoke test at landing: 5 syms in the current run (started 10:35 UTC, currently on BDX) vs the bogus 1514 before the fix.
- **Tooltip rewritten** to show the current-run window cleanly: `"BDX | letter B (0 done) | 5 syms in run | +3185 bars"` (running) / `"BDX | 5 syms in run | letter B | 3m idle"` (idle).
- New module constants: `LOOKBACK_HOURS = 72`, `COUNT_MILESTONES = [50, 100, 250, 500, 1000]`, `RUN_GAP_THRESHOLD_SEC = 3600`, `MILESTONE_STATE_PATH = state/tray_milestone.json`.
- The `_update_loop()` calls `_check_and_fire_milestones()` after every status poll; toast errors are swallowed so a glitch never kills the tray.

### 2026-05-24 — Chart overlays: DITP prior-day key levels (D / E / F) + confluence annotation

- Companion change to `strategy/DITP/scanner.py` v0.2-alpha1. `_gather_chart_overlays()` in `server.py` now consumes the new `yesterday_high` / `yesterday_low` / `yesterday_close` fields on each DITP P2 watchlist candidate and renders them as dotted overlays on the symbol's chart:
  - **E · Yest H** (orange `#e67e22`, dotted) — polarity-flip target when today gapped through it.
  - **F · Yest C** (purple `#9b59b6`, dotted) — fair-value anchor / gap pivot.
  - **D · Yest L** (muted gray `#7f8c8d`, dotted) — included for visual completeness; P2 breakouts don't act on it directly but it's a meaningful institutional-support level.
- New overlay kind `"annotation"` (no price field) carries the confluence tier + human-readable reasons, so the chart legend tells the trader WHY a candidate made the Tier-1+ cut (e.g. *"daily R near $200.00 (MAJOR round)"*).
- Solid daily R + the existing intraday A / B / C dotted refs are unchanged. The full key-level taxonomy (A=PM support, B=first pullback, C=round#, D=yest L, E=yest H, F=yest C) is now visible end-to-end on any DITP-watchlist symbol's chart panel.

### 2026-05-23 — Pin transfers across tabs + IBKR 100-line subscription cap
- User rules (chat 2026-05-23): *"if the side is pin, by clicking another tab, it should also be pinned"* and *"i think IBKR data feed is limited to 100 tickers"*.
- **Pin transfer.** `openDrawer(id)` reverses the prior "always unpin on switch" behaviour: now if any drawer was pinned when you click another sidebar tab, the new drawer inherits the pin. Layout stays consistent — you can rotate Analysis ↔ Gating ↔ Bot log while keeping the right-shifted main area. Still only one drawer open at a time. Explicit ✕ close still unpins; pin button toggle still works per-drawer.
- **IBKR subscription cap.** Standard IBKR accounts (paper + most live tiers) allow ~100 simultaneous market-data lines. The streamer now caps at **95 subscriptions** (5-slot headroom for ephemeral probes / one-shot `reqTickers` calls). Module constant `_STREAMER_MAX_SUBS = 95`.
- **Candidates-first prioritisation.** `_set_streamer_symbols` now takes an ordered list (was a set). `_build_lists_payload` builds it as `candidate_syms + watchlist_non_candidate_syms` — so when the universe outgrows the cap, the watch-only tail drops first; actively-traded names always keep live IBKR quotes.
- **Status fields exposed:** `streamer.requested_n` (how many symbols we asked for), `streamer.max_subs` (the cap), `streamer.capped` (true when over). `/lists/all` payload includes all three.
- **Visible cap warning** in the active-lists header next to the feed badge: `⚠ N over IBKR 100-line cap` (amber). Shows only when `streamer.capped` is true. Tooltip explains the prioritisation rule.
- The fall-back chain still operates correctly when subscriptions are capped: the un-subscribed tail falls through to Alpaca-IEX, then yfinance, exactly as it does when TWS is down for a given symbol.

### 2026-05-23 — In-dashboard chart with strategy overlays (lightweight-charts)
- User rule (chat 2026-05-23): *"i need you to draw the chart based on the ticker select, the workings had to be overlaid in the chart. such as in DITP, the resistance line and for the GUNS the Premarket High etc..."*. The previous TradingView widget renders fine but is a black box — no API to overlay our own levels. Swapping to **lightweight-charts** (TradingView's open-source library, same look) gets us a JS-controllable candle/volume chart with `createPriceLine` for arbitrary horizontal levels.
- **`<script src="https://unpkg.com/lightweight-charts@4.1.3/...">`** added next to the Tailwind CDN. Single file, ~80kb gzipped.
- **`/chart/data?symbol=X&timeframe=Y&days=N`** (new endpoint in `dashboard/server.py`):
  - Reads OHLCV bars via `bars_store.load_bars` (`daily` direct, `3m` aggregated from `1min` via `patterns.aggregate_to_n_min`).
  - Coerces ISO-string timestamps to `datetime` then to unix seconds (the shape lightweight-charts expects).
  - Caps the tail (200 daily bars, 5 days × `bars_per_day` for intraday).
  - Collects strategy overlays per `_gather_chart_overlays(symbol)` — see below.
- **DITP overlays today** (from `state/watchlist_ditp_<date>.json`):
  - `level` — resistance (range_high) solid orange, labelled `DITP R · tier X · P2-Y`
  - `level` — resistance_low dashed orange, labelled `DITP R low`
  - `level` — round-number snap dotted cyan if a psychological round number sits inside the zone (`$985`, `$170`, `$20` etc., grid scales with price tier)
- **GUNS / OS** overlays placeholder — reads any `state/shortlist_<family>_<date>.json` for a per-symbol `pmh` field and renders it as a `level`. The shortlist files don't carry PMH today; this is the wiring spot for when they do.
- **Frontend (`web/index.html`):**
  - `_ensureLwChart(host)` creates the chart once (dark theme, monospace font, volume histogram inset at 82-100% of height).
  - `loadChartSymbol(sym)` fetches `/chart/data`, calls `setData` on candles + volume, calls `createPriceLine` per overlay (with `axisLabelVisible: true` so the level label shows on the price scale), then `fitContent` on the time axis.
  - Old `loadTvScript` / `rebuildTvWidget` / TradingView widget code retired.
  - The active-lists row-click now calls `loadChartSymbol(sym)` directly (was calling a non-existent `loadChart`, which silently no-op'd).
- **Loss of features vs TV widget:** no drawing tools, no symbol-search-from-chart, no built-in MA studies. We gain: actual overlays of the bot's decisions, no popups / network dependency on TV.com, full control over chart styling, no 15-minute delay quirks.

### 2026-05-23 — Real-time heartbeat quotes (IBKR streaming) + whole-row chart navigation
- User rules (chat 2026-05-23): *"the watchlist and candidate quotes should be in realtime heartbeat"* and *"when the list of ticker any row (not just the ticker name) being clicked, the corresponding chart to change"*.
- **Backend — persistent IBKR streaming-quote thread:**
  - New module-level state in `dashboard/server.py`: `_STREAMER_THREAD`, `_LIVE_QUOTES`, `_STREAMER_SYMBOLS`, `_STREAMER_STATUS`. Started lazily on the first `/lists/all` call via `_start_streamer_once()`.
  - The thread owns one persistent `ib_insync.IB` connection (clientId 99, port from cfg), maintains streaming `reqMktData` subscriptions for every symbol that's currently in any watchlist, and snapshots tickers into `_LIVE_QUOTES` every ~2s. Auto-reconnect with backoff on disconnect.
  - `_set_streamer_symbols(syms)` lets the FastAPI handlers tell the streamer which symbols to track. The streamer reconciles (add/cancel) on its next tick.
  - `_streamer_get(symbols)` reads the cache instantly — no IBKR round-trip per request. The 20s cold-fetch latency of the old `reqTickers` path is gone.
- **`_fetch_quotes` priority chain reordered:**
  1. **IBKR streaming cache** (`_streamer_get`) — primary, real-time
  2. IBKR ephemeral `reqTickers` — only if the streamer isn't connected yet
  3. Alpaca IEX
  4. yfinance
- **`/lists/all` payload gains a `streamer` field**: `{connected, subscribed_n, last_update, error}` — exposed to the dashboard for status reporting.
- **Frontend:**
  - **Polling cadence dropped from 60s → 3s.** Heartbeat ♥ indicator in the panel header.
  - **Cell-level flash on value change.** New `_lastQuoteBySym` Map persists across re-renders; on each refresh we diff vs the previous snapshot and add `flash-up` (green) / `flash-dn` (red) class to the `last` cell for ~0.9s. Volume cell flashes up-only (volume is monotonically increasing intraday).
  - **Whole-row click loads chart.** Click handler moved from `td.sym` to `<tr>`. Cursor styling shifted to `tbody tr` so the affordance is obvious. Symbol cell still underlines on row-hover.
- **What the user sees during RTH now:** the table updates every 3s with each cell briefly flashing on tick. Click anywhere in a row → chart panel loads that symbol. Click a different row → chart switches. Off-hours: no ticks fire so flashes don't trigger; chart navigation still works.

### 2026-05-23 — Active Lists moved to fixed right column · chart centred
- User rule (chat 2026-05-23): *"the candidate and watchlist panel should be of the right taking 1/5 of the screen and the chart should be moved to the center"*.
- **Layout now has two fixed columns flanking the centre:**
  - LEFT — 44px sidebar (Analysis / Gating / Bot log) + optional pinned drawer (20vw)
  - RIGHT — Active Lists panel (20vw, min 320px)
  - CENTRE — status bar, today P&L tile, chart panel, orders/positions, trades
- `body { padding-left: 44px; padding-right: max(320px, 20vw); }` reserves both columns. Status bar + today tile now span the centre region rather than full width — they don't reach under the right Active Lists column.
- **`#active-lists` styling:** `position: fixed; top:0; right:0; bottom:0; width: max(320px, 20vw);` Flexbox column inside — sticky header + scrolling body that fills remaining height.
- **Active Lists header restacked vertically** to fit the narrower column:
  - Row 1: title + Candidates/Watchlist tabs
  - Row 2: feed badge + cache TTL + last-update timestamp
- **Table tightened:** font-size dropped to `10px × scale` (9px for the strategy pill), `padding: 3px 4px` per cell, `white-space: nowrap`, smaller letter-spacing on the strategy-pill column. Six columns now fit cleanly inside 320–384px wide.
- No backend changes — pure HTML/CSS restructure. `/lists/all` endpoint + 60s polling unchanged.

### 2026-05-23 — Drawer width capped at 1/5 screen + full-replace on tab switch
- User rules (chat 2026-05-23): *"the side bar should only take 1/5 of the screen by max"* and *"any side bar tab that i click should change the side bar entirely not overlap with the old sidebar"*.
- **Width cap:** `.drawer { width: 20vw; min-width: 320px; max-width: 20vw; }`. On a 1920×1080 monitor that's 384px; on a 1366 laptop it's 320px (the floor); on an ultrawide it stays at 20vw. Previously normal drawers were 420px and the wide (Analysis) drawer was 760px — both replaced with the single capped width.
- **Body shift while pinned:** `body.has-pinned-drawer { padding-left: max(calc(44px + 320px), calc(44px + 20vw)) !important; }`. Replaces the old two-class system (`has-pinned-drawer-normal` / `has-pinned-drawer-wide`) since all drawers now share the same width.
- `.drawer-wide` CSS rule retired (no-op now). The class is still on `drawer-analysis` for harmless back-compat.
- **No overlap on tab switch:** `openDrawer(id)` now force-unpins AND closes every drawer other than the target. Previously pinned drawers persisted alongside a newly-opened one — visually overlapping. New rule: clicking a sidebar tab is a "full replace" — the user explicitly re-pins via 📌 if they want the new drawer pinned too.

### 2026-05-23 — Events drawer merged into Analysis + readable timeline + no-pin default
- User rules (chat 2026-05-23): *"any pinned by default"*, *"the events are events of the strategies, they should be in the analysis"*, *"the events content to me are unreadable by human, it need to be in readable human language"*.
- **No drawer pinned by default.** Removed the localStorage save/restore for `pinned_drawer`. Pin state is now session-only — pinning still works during a session, but every fresh dashboard load starts clean.
- **Events drawer retired; folded into Analysis drawer as a sub-tab.** The standalone `Events` sidebar button + `drawer-events` aside are gone. The Analysis drawer now exposes two sub-tabs at the top:
  - **by symbol** — the existing family tabs (GUNS / DITP / OS) + per-symbol decision rows
  - **event timeline** — the readable chronological event stream
- **Human-readable event formatter.** New `formatEventReadable(ev)` turns the JSON event into a one-line English sentence per type. Examples:
  - `strategy.ditp_p2.monitoring` → *"ditp_p2 watching **GS** — tier **A** P2-A, resistance **$984.70**, **0.10 ATR** from level. caution: SINGLE_MOUNTAIN, WIDE_BASE"*
  - `strategy.ditp_p2.strategy_started` → *"ditp_p2 started — entry at 09:31, **ARMED**, cap 3"*
  - `strategy.os_breakout.planned` → *"os_breakout **planned BIYA** — buy-stop-limit $2.66, stop $2.61, target $2.76"*
  - `strategy.guns_setup1.rejected` → *"guns_setup1 **rejected MLGO** — **not_consolidating_near_pmh** (PMH=$5.42 gap=2.10%)"*
  - `orchestrator.data_provider_selected` → *"data provider: **ibkr** (ok)"*
  - `strategy.guns_setup1.strategy_off_skipped` → *"guns_setup1 **skipped** — strategy is OFF"*
- **Coverage:** 20+ event types explicitly mapped. Unknown types fall back to a compact `<event-name> <symbol> key=value · key=value` view (still readable, no raw JSON).
- **Visual treatment:** each row shows `[HH:MM:SS]` time + colour-coded family pill (GUNS amber / DITP cyan / OS purple / SYS grey) + the readable sentence. Numbers tabular-aligned. Symbols rendered in cyan. Reasons/errors in red. Success states (planned, submitted, filled, take_profit) in green.
- Sidebar collapses from 4 to 3 buttons: **Analysis** (new dual-view drawer) · **Gating** · **Bot log**.

### 2026-05-23 — Sidebar drawers are pinnable
- User rule (chat 2026-05-23): *"dashboard side bar need to be able to pin"*. The four sidebar drawers (Analysis, Gating, Events, Bot log) slide out as overlays by default and close on outside-click / ESC; that's good for quick peeks but bad when you want one of them visible alongside the main view.
- Each drawer header gains a **📌 pin** button between the title and the ✕ close button. Click to pin / unpin.
- **Pin behavior:**
  - Pinned drawers stay open regardless of outside-click or ESC
  - Main content shifts right by the drawer width (420px for Gating/Events/Bot log, 760px for Analysis) — chart panel and active lists reflow into the remaining space
  - Only one drawer can be pinned at a time; pinning a second one unpins the first
  - Sidebar-button click on a pinned drawer is a no-op (it's already open)
  - Close button (✕) is the explicit "go away" — unpins AND closes
- **Persistence:** pin state survives reloads via `localStorage` key `pinned_drawer = "<drawer-id>"`. On dashboard load, the previously-pinned drawer auto-restores.
- **CSS additions:** `.drawer.pinned` (force translateX(0)), `.drawer-pin` button styling (highlighted in IBKR-red when active), `body.has-pinned-drawer-normal` (padding-left: 464px), `body.has-pinned-drawer-wide` (padding-left: 804px).
- **JS additions:** `togglePin(id)` and `_setBodyPinnedClass(drawer)`; `openDrawer` + `closeAllDrawers` modified to skip pinned drawers; init reads localStorage and restores.

### 2026-05-23 — Active-lists feed badge (LIVE / DELAYED indicator)
- User rule (chat 2026-05-23): *"the quote panel should state which data feed it source from"*. Earlier turn already exposed source per-row + a small muted summary; this turn promotes the summary to a prominent labeled badge in the panel header so the LIVE-vs-DELAYED status is unmistakable at a glance.
- New `.feed-badge` CSS class with four colour states: `feed-ibkr` (green, all-IBKR), `feed-alpaca` (amber, Alpaca-IEX), `feed-yf` (red, delayed), `feed-mixed` (amber, mixed sources), `feed-none` (red, no feed available). Round-dot prefix mirrors the existing status-bar pill aesthetic.
- Badge labels: `IBKR · LIVE` / `Alpaca IEX · LIVE (IEX vol only)` / `yfinance · DELAYED 15m` / `MIXED · <primary> primary` / `no feed available`. Tooltip on hover carries the full per-source row counts + a 1-line definition of each feed + the fallback order.
- Driven by the existing `quote_sources` field in `/lists/all`; pure frontend change.

### 2026-05-23 — Strategy Analysis → sidebar drawer + IBKR live quote feed
- User rules (chat 2026-05-23): *"the analysis of the ticker should be placed in the side bar"* and *"the quote should be from the live IBKR datafeed"*.
- **Strategy Analysis panel moved out of the main grid into the left sidebar.** New 4th sidebar button "Analysis" opens a wide (760px) drawer carrying the existing `#strategy-family-tabs` + `#strategy-analysis-list` markup verbatim — all of the family-tab + journal-event-stream JS continues to work unchanged because the DOM IDs moved with the markup.
- Main grid simplified: the chart panel now spans full width (was 50/50 with strategy analysis). Chart-empty message updated to point at "the active lists above, or open the Analysis drawer (left)".
- **`server.py`**: `_fetch_quotes` reworked as a 3-tier fallback chain — **IBKR snapshot → Alpaca IEX → yfinance**. Per-row `source` field exposes which feed served each row.
  - **IBKR** (`_fetch_quotes_ibkr`): connects via `ib_insync` on the dashboard's reserved clientId 99, qualifies contracts (resolves SMART/USD + primary exchange), uses **`ib.reqTickers`** for synchronous snapshots (more reliable than `reqMktData` + `ib.sleep`, which silently times out when batching 5+ subscriptions). Real-time during RTH, yesterday's `close` off-hours. Volume reported in shares (IBKR delivers 100-share lots; multiplied accordingly).
  - **Alpaca IEX** (`_fetch_quotes_alpaca`): one `trades/latest` + one `bars` round-trip for the whole batch. Real-time price, IEX-only volume. Free with paper account; credentials resolved via `_common.load_vendor_env("alpaca")`.
  - **yfinance** (`_fetch_quotes_yfinance`): last-resort fallback, 15-min delayed.
- Quote cache bumped from 30s → 60s; frontend polling cadence matched. `reqTickers` is ~340ms per symbol so a 60-symbol cold fetch is ~20s; 60s TTL means at most one cold fetch per minute even with multiple dashboard tabs open.
- **`web/index.html`** active-lists panel:
  - Header now shows quote-source breakdown — e.g. `quotes: 50 IBKR · 11 Alpaca-IEX · 60s cache`
  - Per-row badge next to volume: `IEX` (Alpaca-IEX feed, volume is partial), `yf` (yfinance, delayed), `no quote` (all three sources failed)
- New `.drawer-wide` CSS class (760px) for the analysis drawer.
- End-to-end smoke at the moment of landing: 61/61 watchlist symbols served by IBKR, 20.7s cold fetch wall-clock.

### 2026-05-23 — Active Lists panel (Candidates / Watchlist tabs)
- User rule (chat 2026-05-23): *"In the dashboard i want 2 tabs - candidate and watchlist. Each list will contain column symbol, last, chg, chg%, vol, strategy. The candidate folder is those ticker that passed the gate and ready for trade execution, the watchlist are those still under radar but has not pass the gate."*
- New full-width panel at the top of the page (above the Strategy Analysis + Chart grid), two tabs:
  - **Candidates** — symbols whose owning strategy family is currently ON + ARMED. These are the ones the bot will fire on when a valid trigger lands. Drives the operational "what am I about to trade?" view.
  - **Watchlist** — every symbol on every `state/watchlist_*_*.{txt,json}` file. The raw under-radar pool across families.
- Each row shows: `SYMBOL | LAST | CHG | CHG% | VOL | STRATEGY`. Strategy column renders as a colour-coded pill (GUNS amber / DITP cyan / OS purple). Symbol cell is click-to-load-chart, reusing the existing embedded TradingView panel.
- **`server.py`**: new `GET /lists/all` endpoint. Sync builder runs in an executor thread; aggregates rows from JSON + TXT watchlist files (JSON wins when both exist for the same symbol+family), batch-fetches quote data via yfinance (`period=2d, interval=1d`) with a 30s in-process cache, and tags each row with the armed state of its strategy family (via `scripts/_gating.is_enabled` + `is_armed`).
- **`web/index.html`**: new `.list-tab` styling (uppercase pill row, distinct from per-family `.fam-tab`), `table.active-list` styling (sticky header, right-aligned numerics, sym/strategy left-aligned). Polls `/lists/all` every 30s.
- End-to-end smoke at the moment of landing: **61 watchlist symbols** (50 OS + 6 DITP + 5 GUNS), **50 candidates** (only OS armed in paper).

### 2026-05-23 — Ticker profile health pill + refresh-scope modal
- User rule (chat 2026-05-23): *"ok you can start profiling, the dashboard also need to start profile status"*. Per the Dashboard Visibility Rule, the ticker-profile pipeline (added this session) needs an observable surface so the user can see coverage + freshness and trigger refreshes without dropping to CLI.
- **`server.py`** — two new endpoints:
  - `GET /profile/health?details=true` — coverage + freshness summary from `ticker_profile.profile_health()`. Returns `n_total / n_fresh / n_stale / n_full / n_partial / n_no_daily / oldest_ts / newest_ts / symbols_3m / overall`. The `overall` field is bucketed `ok | warn | critical` using the same thresholds as the data-health pill (≥80% full+fresh → ok; ≥50% or any stale → warn; else critical).
  - `POST /profile/refresh?scope=watchlist|ingested|universe` — bulk-refresh trigger. `watchlist` aggregates today's `state/watchlist_*_*.json` symbols (fast, ~10-30 tickers); `ingested` uses every symbol with local 1m parquet (~500 currently); `universe` uses every symbol with daily parquet (~1500). Runs in an executor thread so the UI stays responsive; yfinance calls paced at 0.4s.
- **`web/index.html`** — new status-bar pill **"Profile health"** sitting next to "Historical price data health". Polls `/profile/health` every 60s; colour follows the same `ok/warn/critical` palette. Click opens a modal mirroring the data-health modal: three KPI panels (coverage / freshness / timestamps), a list of symbols with full 3m profile, and three refresh buttons (watchlist / ingested / universe) labelled with their expected scope size.
- The substrate this exposes: `data/ticker_profile/<TICKER>.json` files produced by `resources/ticker_profile.py` — per-ticker behavioral baselines (`stats_daily` + `stats_1m_rth` + `stats_3m_rth`). The 3m section is what the upcoming candlestick anti-pattern detectors will read for ticker-relative thresholds.

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
