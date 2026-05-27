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

### 2026-05-27 - Chart pane: switch to iframe `widgetembed` URL with studies in URL (EMAs finally render)

User: *"the ema line are still not shown"* -- the JS widget's `chart.createStudy()` didn't actually add the EMAs on this browser/widget revision, despite multiple attempts. Conclusion: the free Advanced Charts widget's API surface is too inconsistent to reliably add studies post-construction. Switched to a different code path entirely:

**Iframe `widgetembed` URL with studies in URL params.** Well-documented, simple, known to work for adding studies. URL shape:

```
https://s.tradingview.com/widgetembed/
  ?symbol=<SYM>&interval=D&theme=dark&style=1
  &studies=[{"id":"MAExp@tv-basicstudies","inputs":{"length":20}},
            {"id":"MAExp@tv-basicstudies","inputs":{"length":50}},
            {"id":"MAExp@tv-basicstudies","inputs":{"length":200}}]
```

Studies are JSON-encoded then URL-encoded into the `studies` param.

**Flow:**
- First ticker click: build URL, create iframe, set src.
- Subsequent clicks: same iframe, just update `iframe.src`. ~1-2s reload per click (vs the widget's in-place setSymbol which would have been instant -- but the widget never actually rendered the EMAs, so this is the right trade).
- "Open in TradingView ↗" link in the header still goes to the full TV page in the named external tab.

**Trade-offs accepted:**
- No full TradingView UI (drawing tools / indicator menu / saved layouts). The widgetembed is the minimal chart embed.
- EMAs render in TradingView's default line colors (typically blue / orange / violet). User can right-click any line in the embed's reduced UI to recolor.
- Each symbol switch reloads the iframe (~1-2s). Acceptable for interactive use; would be a problem for high-frequency switching, which isn't our pattern.

**Removed** (~120 lines): `tvWidget`, `tvWidgetReady`, `tvScriptPromise`, `ensureTvScriptLoaded`, `addEmaStudies`, all the widget-API gymnastics.

**Kept**: backend `GET /chart/yf_bars` endpoint (currently unused; useful if we ever build a sidebar stats panel or revisit Lightweight Charts).

### 2026-05-27 - Chart pane: revert back to TradingView widget (default-colored EMAs, but widget UI intact)

User: *"can you look back where you manage to draw the EMA but just colour not changed"*. After the Lightweight Charts pivot, the user preferred the TradingView widget approach -- even with EMAs in TV's default colors -- over LWC. Reverting the chart pane back to the widget, with the simpler EMA-add path that was empirically working (3 EMAs visible) before I broke it trying to force red/green/purple colors.

**Current chart pane:**
- Loads `tv.js` from `s3.tradingview.com` lazily on first chart open.
- Constructs the widget WITHOUT any `studies` parameter (the constructor's object-form `studies` array crashed the widget on this revision -- chart wouldn't load at all).
- After `onChartReady`, calls `chart.createStudy('Moving Average Exponential', true /*forceOverlay*/, false /*lock*/, [length])` three times for lengths 20 / 50 / 200, staggered 150ms apart (back-to-back createStudy calls during initial render can be dropped).
- **No color overrides**. The free embed accepts the override args but doesn't apply them; trying to be clever about it broke the chart in previous iterations. EMAs render in TradingView's default line colors. Right-click any EMA line in the chart UI to recolor manually.
- Subsequent clicks call `widget.setSymbol(symbol, 'D')` -- chart re-loads data in place; studies persist.
- "Open in TradingView ↗" link in the header still goes to the full TV page in the named external tab.

**Removed:**
- All Lightweight Charts code: `lwcChart`, `lwcScriptPromise`, `ensureLwcScript`, `emaSeries`, `renderLwcChart`, `_lwcResizeAttached`. ~150 lines.
- The unpkg CDN dependency.

**Kept:**
- Backend `GET /chart/yf_bars` endpoint. Currently unused by the chart pane (the TV widget pulls its own data) but harmless and may be useful later (e.g. for a separate stats panel or for a future LWC variant if we change our minds).

**Trade-off acknowledged**: EMAs are TradingView's default colors (typically blue / orange / violet), not the red/green/purple the user specified. After three failed override attempts on the free embed, the practical conclusion is: those overrides are not reliably available, and forcing them via JS keeps breaking the chart. Default colors + working chart > exact colors + broken chart.

### 2026-05-27 - Chart pane: switch from TradingView widget to Lightweight Charts (full programmatic control)

User reported repeatedly that EMA lines wouldn't render in the right colors via the free Advanced Charts widget. After three iterations of fighting widget-API quirks (constructor `studies_overrides` with indexed keys: ignored; `chart.createStudy` 6-arg overrides: studies added in wrong colors; post-creation per-study `applyOverrides`: studies stopped showing at all), it became clear the free tv.js embed's API surface isn't reliable enough to land per-study colors consistently.

**Switched the chart pane to Lightweight Charts** -- TradingView's open-source charting library:
- ~50 KB pure JS, lazy-loaded from `unpkg.com/lightweight-charts@4.2.0/...`. No widget API to fight; we own every pixel.
- Renders candles (green up / red down) + three EMA lines in exact colors (EMA20 red, EMA50 green, EMA200 purple) computed in JS from the same yFinance bars used elsewhere in the dashboard.
- Re-creates the chart on each ticker click (LWC is cheap to instantiate; fresh-on-each-symbol avoids stale series state).

**New backend endpoint**: `GET /chart/yf_bars?symbol=<X>&lookback_days=<N>` (default 400). Returns `{ symbol, count, bars: [{t,o,h,l,c,v}] }`. Reuses `resources/yf_daily_bars.fetch_daily_single()` so the chart and the scan pipeline share the same fetch code + canonical bar shape. No parquet read.

**Frontend**:
- New helpers: `ensureLwcScript()` (lazy CDN load), `emaSeries(closes, period)` (standard EMA math), `renderLwcChart(symbol, bars)` (chart construction + series setup).
- `loadTvChart(symbol)` (name kept so the existing event-delegation click handler doesn't need rewiring) now: parallel-loads the library + bars, then calls `renderLwcChart`.
- Removed: all TradingView widget code -- `tvWidget`, `tvWidgetReady`, `tvScriptPromise`, `ensureTvScriptLoaded`, `addEmaStudies`, `EMA_COLORS`, the `studies_overrides` block. ~150 lines gone.
- Chart styling matches dashboard tokens: `#0d1117` background, `#c9d1d9` text, `#21262d` grid, etc.
- The "Open in TradingView &nearr;" link still works (full TV page in the named external tab).

**Trade-offs accepted:**
- No built-in TradingView UI tools (drawings, alerts, indicator picker) -- those need the paid Charting Library.
- Chart re-renders fully on each symbol click (~50-200ms; no perceived lag).
- Single network dependency on `unpkg.com` for the LWC script. If unpkg is blocked, the chart pane shows an inline error and the rest of the dashboard keeps working. Easy to vendor locally if reliability becomes an issue.

**Smoke verified:**
- `GET /chart/yf_bars?symbol=AAPL&lookback_days=30` -> HTTP 200, 30 bars, latest 2026-05-26 (today ET).
- Page serves 60.7 KB. No dangling `tvWidget` / `addEmaStudies` / `EMA_COLORS` references in the JS.

### 2026-05-27 - TradingView chart: move EMAs to constructor `studies` (more reliable than onChartReady + createStudy)

User: *"the EMA lines can be added to chart?"*. Previous code used `widget.onChartReady(() => chart.createStudy(...))` -- which works on the full Charting Library but is fragile on the free `tv.js` Advanced Charts embed (the `chart()` method's surface varies by widget version; `createStudy` sometimes silently fails or isn't exposed).

Switched to declaring the three EMA studies at widget-construction time via the `studies` array, which is documented for the free embed:

```js
studies: [
  { id: 'MAExp@tv-basicstudies', inputs: { length:  20 } },
  { id: 'MAExp@tv-basicstudies', inputs: { length:  50 } },
  { id: 'MAExp@tv-basicstudies', inputs: { length: 200 } },
],
studies_overrides: {
  'moving average exponential.plot.color.0':     '#f85149',  // EMA20 red
  'moving average exponential.plot.linewidth.0': 2,
  'moving average exponential.plot.color.1':     '#3fb950',  // EMA50 green
  'moving average exponential.plot.linewidth.1': 2,
  'moving average exponential.plot.color.2':     '#bc8cff',  // EMA200 purple
  'moving average exponential.plot.linewidth.2': 2,
},
```

The `.0`/`.1`/`.2` index suffix in `studies_overrides` targets the Nth instance of the named study in declaration order -- documented mechanism for per-instance overrides without a post-creation callback dance.

Dropped `addEmaStudies()` and the `onChartReady` callback that called it. Kept `onChartReady` only to flip the `tvWidgetReady` flag (used by `setSymbol()` to short-circuit a re-instantiate).

**Failure mode if the indexed-override pattern doesn't apply on a specific TV widget revision**: all three EMAs still render in their default TV colors (typically blue/orange/violet). The user can right-click each EMA in the chart and pick a custom color from TV's UI. Worst-case: chart works, EMAs visible, colors not exactly as specified -- still functional. Tell me if you see this; we can switch to the JS-widget-with-custom-tv.js-version path or pre-compute the EMAs server-side and draw them as overlay shapes.

### 2026-05-27 - Scrollbar: invisible by default, fades in on hover at the muted tone

User: *"the scroll bar make it invisible and only visible when mouse over and colour tone to match"*. Applied a subtle-scrollbar treatment to the two scrollable containers (`main` for non-scanner views; `#finviz-table-wrap` for the watchlist):

- **Default state**: scrollbar track and thumb are both `transparent` -> visually invisible.
- **Container hover**: thumb fades to `rgba(139, 148, 158, 0.35)` (the `--text-muted` colour at 35% opacity) with a 0.2s transition.
- **Direct thumb hover**: thumb darkens to `rgba(139, 148, 158, 0.6)` to signal you're hovering the drag target.
- Width is `8px` (WebKit) / `thin` (Firefox). Both engines covered.

Rules are bundled in one block right after `main { ... }` so they're easy to find. Adding another scrollable container = append its selector to the existing list (rather than re-declaring the rules elsewhere).

### 2026-05-27 - Watchlist header: show readable filter pills, not the raw Finviz URL

User: *"the finviz url do not show the url just show the criteria that applied to get the tickers"*. Replaced the raw-URL display under the Watchlist header with a row of compact pills, one per filter code from the Finviz `f=` param.

Each pill:
- Shows a human label (e.g. `Mid Cap+`, `Price > $20`, `ATR > $2`, `Beta > 1`, `Avg Vol ≥ 10K`, `US-listed`).
- Tooltip on hover shows the raw Finviz code (e.g. `cap_midover`, `sh_price_o20`) so the user can still see what's behind the label when debugging an unfamiliar filter.

**Mapping coverage:**
- Static codes (cap_midover, geo_usa, etc.) live in `FINVIZ_FILTER_LABELS`.
- Parametric codes (price/ATR/beta/volatility/avg-vol thresholds) are matched with regex and rendered with the numeric value extracted.
- Unknown codes fall through to the raw code as the label so we never silently drop information.

Backend response unchanged -- `/scanner/finviz_tickers` still returns the full URL; the dashboard just stops displaying it verbatim. If we ever want server-side parsing instead (to share between dashboard + CLI), promote `formatFinvizFilter` to a Python helper.

**File-size delta**: 52KB -> 57KB (the static FINVIZ_FILTER_LABELS map + regex parser are ~80 lines).

### 2026-05-27 - Scanner view: rename to "Watchlist", flush-edge layout, EMA chart studies

Three threaded requests:
1. *"the scanner just penal just call watchlist"* -> renamed the left-pane panel header from "Finviz scanner" to "Watchlist".
2. *"do it like a side bar with the edge"* -> the left pane now sits flush against the actual nav sidebar; the right chart pane goes edge-to-edge of the viewport. No gap between panes; no rounded outer corners; the only visible separator is the 1px right-border on the watchlist column.
3. *"I need the EMA20 red EMA50 green EMA200 purple line to be shown in the tradingview chart"* -> swapped the `widgetembed` iframe for the JS widget (`TradingView.widget`), which exposes the `createStudy()` API for per-study color overrides.

**Flush-edge layout:**
- `main > .view[data-view="scanner"]` now uses `margin: -24px` to cancel main's own padding so the panels go edge-to-edge.
- Both panels lose their `border-radius` + outer borders. Watchlist keeps a 1px right-border as the visual separator from the chart pane.

**EMA wiring:**
- Lazy-loads `https://s3.tradingview.com/tv.js` on first chart open (no startup cost; only pays when the user clicks a ticker).
- First click creates the widget and runs `onChartReady` -> three `chart.createStudy('Moving Average Exponential', false, false, [N], null, {Plot.color, Plot.linewidth})` calls with lengths 20/50/200 and colors `#f85149` / `#3fb950` / `#bc8cff` (matching dashboard's --err / --ok / a custom purple).
- Subsequent clicks call `widget.setSymbol(symbol, 'D')` -> chart re-loads data in-place; the three EMA studies persist. Much faster than the iframe-reload approach.
- Failure path: if tv.js fails to load (offline, CDN block, etc.) the chart pane shows an inline error; the watchlist + Finviz endpoints keep working.

**Why JS widget instead of iframe with `&studies=...`**: the iframe `widgetembed` URL accepts a `studies` parameter but it can't differentiate colors per study instance -- all three EMAs would render with the default TV blue. `createStudy()` per-instance overrides are the only way to get red/green/purple.

**File-size delta**: 47KB -> 52KB (the JS widget integration code is non-trivial).

### 2026-05-27 - Scanner view: scrollable watchlist + inline TradingView chart in the right pane

User: *"anything in the watchlist cannot fit the screen to be scrolled / the remaining right page add a tradingview chart that link to the watchlist"*. Made the watchlist internally scrollable (panel header stays put) and added a second panel filling the remaining viewport width with a TradingView iframe chart that updates when you click a watchlist row.

**Layout shape:**
```
+--------+------- 270px -------+--- rest of viewport ---+
|        | Finviz scanner      | Chart: NVDA  [Open ↗] |
| Side   |  43 tickers · 4 m.  |                       |
| bar    |  URL: ...           |  +--- TradingView   --+|
|        |  [Refresh]          |  |  iframe          ||
|180px   |  +-------------+    |  |  (D candles,     ||
|        |  | #5 FCX [EMA]|    |  |   dark theme)    ||
|        |  | $64.36 ...  |    |  |                  ||
|        |  +-------------+    |  +-----------------+|
|        |  +-------------+    |                       |
|        |  | #24 C [EMA] |    |                       |
|        |  | scrollable! |    |                       |
|        |  ...             v  |                       |
+--------+---------------------+-----------------------+
```

**Scrollable watchlist** — the Scanner view now uses `height: 100%` + `flex-direction: row` + `overflow: hidden`, and the inner `#finviz-table-wrap` gets `flex: 1; overflow-y: auto; min-height: 0`. Result: the panel header (title, count, URL, Refresh button) stays anchored at the top of the panel while the list scrolls underneath. The page itself doesn't scroll — main's overflow is contained by the per-pane scroll regions.

**Inline chart pane:**
- New `<section class="panel scanner-chart-panel">` next to the Finviz panel. Header reads `Chart: <SYMBOL> [Open in TradingView ↗]`. The iframe fills the rest of the panel; default placeholder text reads *"Click a ticker in the watchlist to load its chart"*.
- Click any watchlist row -> iframe loads `https://s.tradingview.com/widgetembed/?symbol=<SYM>&interval=D&theme=dark&style=1&...`. Daily candles, dark theme, no social ideas panel, toolbar tinted to match the dashboard background.
- The `Open in TradingView ↗` link in the chart header is a fallback to TradingView's full UI — uses the same named-tab target (`intraday_bot_tv`) so at most one external tab exists across the session.
- Symbol form stays canonical dotted (`BRK.B` not `BRK-B`); TV accepts both.

**Click-handler change** — the previous behaviour (open in named-tab via `window.open`) is replaced by `loadTvChart(symbol)` which updates the inline iframe. The named-tab path is still available via the `Open in TradingView ↗` link.

**Why iframe `widgetembed` instead of the JS widget API**: simpler to wire (just set `iframe.src` on click); no TradingView SDK to load, no widget lifecycle to manage. The reload-on-symbol-change cost (~1-2s) is acceptable for interactive use. If we ever want symbol-change-without-reload, swap to `new TradingView.widget(...)` + `widget.chart().setSymbol(...)`.

**File-size delta**: 40KB -> 47KB (chart panel CSS + JS).

### 2026-05-27 - Scanner view: narrow column (270px = 1.5x sidebar) with compact card list

User: *"the scanner i want it to be next to the side bar, 1.5 width of the side bar"*. Constrained the Scanner view to 270px (180px sidebar * 1.5) and converted the Finviz list from a horizontal table to a vertical stack of compact cards.

**Layout shape:**
```
+--------+------- 270px -------+ <empty rest of viewport for future panels>
|        | Finviz scanner      |
| Side   |   43 tickers · 4 m. |
| bar    |   [Refresh]         |
|        |  URL: ...           |
|180px   |  +--------+         |
|        |  | #5 FCX [EMA50]   |  <- each ticker = one card
|        |  | $64.36  50.12M   |
|        |  +--------+         |
|        |  ...                |
+--------+---------------------+
```

**Each card stacks two lines:**
- Line 1: `#<Finviz rank>` (muted, small) - `<SYMBOL>` (accent) - setup-match badges (right-aligned, wraps if multiple)
- Line 2: `$<price>` - `<volume>` (small, muted, indented to align with symbol)

Matched rows get a green-tinted border and faint green background; symbol turns green too. Hover effect intensifies the tint. Click anywhere on the card -> TradingView (same reused tab).

**Why not just narrow the table**: at 270px a 5-column table (`# · Symbol · Price · Volume · Setup`) would be cramped at best and unreadable with multiple setup badges. Card stacking preserves all the info AND lets setup badges wrap naturally onto a second line.

**Scoped to the Scanner view only**: other views (Monitor, Strategies, Trades, Data) keep their wide layout. The `main > .view[data-view="scanner"]` selector targets the constraint.

**Click delegation** widened: the TradingView open-on-click handler now matches either `.finviz-row[data-symbol]` (the new card form) OR `table.scanner tbody tr[data-symbol]` (kept for any future setup-results tables that might land in other views).

### 2026-05-27 - Scanner: sort Finviz rows by setup membership

User: *"the list will sort by setup"*. Matched rows now float to the top of the Finviz table, grouped by which setup matched (in SETUPS-registry order: DITP P2 first, then EMA rebound, ...). Unmatched rows fall to the bottom. Within each group, Finviz's original volume-desc order is preserved as a stable tiebreaker.

**The `#` column keeps its Finviz rank** (1-indexed position in the original volume-desc result) rather than the display position, so a row showing `#5 FCX [EMA EMA50]` tells you both *that FCX was the 5th-most-traded name in the Finviz screen* AND *that EMA-rebound matched it*. Useful intel: high-volume names tend to be the most liquid for entries.

**Sort key** (per row): `(matched ? 0 : 1, setupIdx_in_SETUPS, original_finviz_idx)`. Sort is stable, so multi-setup matches go to the first-matching setup's group (set by `SETUPS` declaration order: DITP P2 before EMA rebound).

**Why SETUPS-declaration order** rather than alphabetical: the registry order is the authoring intent for setup priority. If we ever want a user-tunable priority, that's a separate UI control; for now declaration-order is the simplest sensible default.

### 2026-05-27 - Scanner: consolidate to single Finviz panel with a "Setup matched" column

User: *"we use back the same panel to indicate whether there are any setup matched in a setup column"*. Reverted the grouped-by-setup second panel; now the Finviz table is the sole panel and has a new last column showing which setup(s) matched each ticker.

**Removed:**
- The entire `<section class="panel">` for "Watchlist by setup" (HTML).
- `renderSetupGroup()` and `renderSetupGroups()` (per-setup-card renderers).
- `candidateRowHtml()` and `renderCell()` -- the column-spec-driven row renderer used by the grouped panel.
- `ageBadge()` -- leftover from the disk-watchlist age UI.
- `setMatchesStatus()` and the `#matches-status` element.
- The `#run-all-btn` Refresh button (in the removed panel).

**Added/changed:**
- Each yf setup in `SETUPS` now carries:
  - `shortLabel`  -- compact name shown in the badge (e.g. `P2`, `EMA`)
  - `matchDetail(candidate)` -- function returning a short disambiguator suffix (e.g. `B/A` for DITP, `EMA50` for ema_rebound). Tooltip on the badge shows the full label.
- New helpers `candidateFor(setup, symbol)` and `setupBadgesHtml(symbol)` build the per-row Setup column. A non-match renders as `-`; a match renders as one or more `<span class="tag strategy">` badges.
- `renderFinvizTable()` adds the new `Setup matched` column and counts matched rows. Header meta line is rebuilt live: `<N> tickers · <K> matched · fetched HH:MM:SS` once setups have run; just `<N> tickers · fetched HH:MM:SS` before they do.
- `runAllSetups()` simplified: drives `renderFinvizTable()` after each setup completes (incremental column population), and uses the Finviz panel's `#finviz-meta` line for in-flight status (`<N> tickers · building EMA (2/2)... · 3s`).

**Stubs** (DITP TC, GUNS) still live in `SETUPS` so the registry remembers them, but they never produce badges in the column (no candidates). When/if wired, flipping `run: 'stub'` -> `run: 'yf'` is all the change needed.

**Confined to dashboard** per the user directive: no parquet read, no backend changes, no Hermes-facing state. The "shortlist" is whichever Finviz rows have a non-empty Setup column at this moment.

File size: 45KB -> 38KB (removed the grouped-panel renderer + its CSS scaffolding).

### 2026-05-27 - Scanner: reframe "Setup matches" as a watchlist + auto-populate on view open

User: *"we are focused on the dashboard, do not mix with the hermes ... I need the setup being applied to the tickers obtained from the Finviz criteria and listed in a watchlist grouped by each setup"*. The existing panel already did exactly this; it just felt like an on-demand "scan" rather than a watchlist because it required a button click. Reframed the UX without restructuring the data flow.

**Changes:**
- Panel renamed from "Setup matches" -> "Watchlist by setup".
- Subheader copy reframed in watchlist terms: *"Each Finviz ticker run through every wired setup; matches listed below grouped by setup. Auto-runs when the Finviz tickers load."*
- Auto-run: `loadFinvizTickers()` now fires `runAllSetups()` after a successful Finviz fetch (and after clearing the prior universe's results). Watchlist is populated by the time the user looks at it; no manual click required.
- Button label: "Run all setups" -> "Refresh". Still re-runs every wired setup against the current universe; useful after editing setup config or to see fresh intraday yFinance bars.
- Mid-run button text: "Running... Xs" -> "Building... Xs". Status line during the run: "building watchlist - DITP P2 (1/2)..." instead of generic "running DITP P2".
- Final status line: "<N> shortlist candidates from <M> tickers across <K> setups - Xs" reframes the result as a shortlist rather than a scan report.
- Empty-state copy in the groups container: "click Run all setups..." -> "loading watchlist...".

**No backend change** -- this is purely a UX reframing on the same data pipeline (Finviz universe -> `/scanner/yf_scan` per setup -> grouped render).

**Confined to dashboard** per user directive: nothing touches parquets, journal, or any Hermes-facing state. The shortlist lives entirely in the browser tab (`setupResultsCache` JS module state); refreshing the page re-runs from scratch.

### 2026-05-27 - Scanner: add EMA-rebound setup + column-spec-driven renderer

User: *"Add a setup that will find rebound on EMA20 or EMA50 or EMA200"* applied as a FILTER setup (not a separate universe / scanner). Slots into the existing "Setup matches" panel alongside DITP P2.

**Backend (dashboard/server.py):**
- New dispatch in `POST /scanner/yf_scan` for `setup=ema_rebound`. Same monkey-patch-bars_store pattern as DITP P2 -- the yFinance batch fetch is shared infra; only the detector function differs. Calls `strategy.DITP.ema_rebound.scan_universe()`.
- `_VALID_SETUPS = ("ditp", "ditp_tc", "ema_rebound")` constant centralizes the allowlist.
- `ditp_tc` now returns 501 explicitly (was previously falling through to the DITP P2 detector silently -- a latent bug; the frontend never let it through but a hand-crafted curl would have).
- New detector lives at `strategy/DITP/ema_rebound.py` (v1.0.0). See `strategy/DITP/README.md` changelog for the detection logic + smoke-test results.

**Frontend (web/index.html):**
- New `ema_rebound` entry in `SETUPS` array with its own `columns` spec (Symbol / EMA / Last / EMA value / Dist ATR / Days since / ATR / Score).
- Renderer is now **column-spec driven**: `candidateRowHtml(c, columns)` reads field names from the setup definition; `renderSetupGroup` builds the header row from the same spec. Adding new setups with different result shapes no longer requires a renderer fork. New helper: `renderCell(value, kind)` with kinds `sym | num | num-int | tier | tag-ema | cautions | muted`.

**Smoke test** on laptop with user's intraday Finviz URL:
- DITP P2 -> 0 matches (high-vol Finviz set is IPO-heavy, fails 220-bar EMA200 gate)
- EMA rebound -> 4 matches (FCX on EMA50; C, GOOG, GOOGL on EMA20). Real, actionable hits.

File-size delta: 41KB -> 44KB (new setup spec + renderCell helper).

### 2026-05-27 - Scanner step 2 rework: dedicated "Setup matches" panel grouped by setup

User: *"instead of apply the setup filter, we do another panel to go through these tickers on all the setup that we have group them by setup"*. Replaced the in-table filter (one setup at a time, match-tagged rows) with a second `<section class="panel">` below the Finviz table that runs every wired setup and groups the matches.

**Layout shape:**
```
+--- Finviz scanner ---------------------------------------+
| 43 tickers ... [Refresh]                                 |
| #  Symbol  Price   Volume                                |  (plain table)
+----------------------------------------------------------+

+--- Setup matches ----------------------------------------+
| <status>                              [Run all setups]   |
|                                                          |
| -- DITP P2 - Resistance breakout  N matches / N=universe |
|    <table: Symbol Tier Var Conf Last Resist Dist Score>  |
|                                                          |
| -- DITP TC ...                    stub (not wired)       |
| -- GUNS ...                       stub (intraday only)   |
+----------------------------------------------------------+
```

**Why a separate panel:** the Finviz table is the universe (what's tradeable today). The setup matches are the analysis (which of those fit which strategy). Conflating them in one match-tagged table mixed two concerns. Separate panels lets the user keep the Finviz view as a sortable / scrollable reference while the matches grow underneath.

**Data-driven setup registry** in JS (`const SETUPS`):
- Each entry is `{ key, label, run: 'yf' | 'stub', note?, level? }`.
- `'yf'` setups call `POST /scanner/yf_scan?setup=<key>` sequentially.
- `'stub'` setups render the explanatory note without a network call -- placeholder for future wiring (DITP TC, GUNS, etc).
- Adding a new setup is a one-line append to `SETUPS`; the renderer is fully data-driven.

**Sequential execution** (not parallel). Each `yf_scan` does its own yFinance batch fetch, so 3 wired setups would do 3x the network work. Acceptable while only DITP P2 is wired; when we get >1 wired setup, the right move is a backend `/scanner/yf_scan_all` that fetches yFinance once and runs all detectors on the shared bars cache. Tracked but out of scope here.

**Incremental rendering**: as each setup completes, its group flips from `not run yet` -> result. Users see progress instead of a single blocking wait.

**Removed (replaced by the new panel):**
- The `.setup-filter` row inside the Finviz panel (dropdown + Apply + matches-only checkbox + status line).
- The match-aware columns in `renderFinvizTable()` (Tier / Var / Conf / Dist / Score appearing inline).
- The `matched` row tint + green-dot prefix CSS.
- JS state: `matchesBySymbol`, `activeSetupLabel`, `applyInFlight`, `onApplySetup`, `SETUP_LABELS`, `setSetupStatus`.

**Universe-refresh consistency**: clicking Refresh on the Finviz panel now also clears `setupResultsCache` -- stale match groups would otherwise survive a universe change.

**File-size delta**: 38KB -> 41KB. The grouped renderer + data-driven SETUPS registry are slightly heavier than the inline filter approach, but the separation buys clarity for future setup additions.

### 2026-05-27 - Scanner step 2 lands: setup filter panel inside the Finviz view

User directive: *"with the finviz tickers, then i can select from a panel to filter from the ticker the setup that match the strategies"*. The Finviz table is the primary surface; the setup detector now runs on demand and annotates which rows matched, without moving them.

**Layout addition (inside the existing Finviz panel):**
```
Filter by setup:  [DITP P2 ▼]  [Apply]  ☐ Show only matches      <status line>
```
- Dropdown lists the wired setups; DITP TC and GUNS are present but disabled with explanatory labels (TC needs the prior-day P2 watchlist in memory; GUNS needs IBKR's live momentum scan -- neither is wired for the yFinance path yet).
- **Apply** -> `POST /scanner/yf_scan?setup=<X>`. Because both `/scanner/finviz_tickers` and `/scanner/yf_scan` resolve their universe via the same `cfg.finviz_screener_url`, candidates are guaranteed to be a SUBSET of the visible ticker table -- no symbol-list shuffling between the two endpoints.
- **Match highlight**: rows whose symbol is in the candidates set get a green dot prefix on the symbol cell, a faint green tint across the row, AND new columns appear (Match / Tier / Var / Conf / Dist (ATR) / Score). Non-matched rows show `-` in those columns.
- **Show only matches** checkbox: pure client-side filter using the cached candidate map -- no extra round-trip.
- **Status line** to the right of the controls reports last-applied summary: `DITP P2 -> 3 matches · fetch=2.6s scan=0.0s · universe: finviz (43 symbols)`. Green for matches > 0, red on failure.

**State management** (single-page, no framework):
- `finvizRowsCache`: rows from last `/scanner/finviz_tickers` call.
- `matchesBySymbol`: symbol -> candidate dict after last Apply.
- `activeSetupLabel`: short name shown in the Match column badge.
- Re-rendering the table reads from these three; toggling "show only matches" does NOT refetch -- saves bandwidth and keeps the UI snappy.
- Clicking **Refresh** on the Finviz panel invalidates `matchesBySymbol` (the candidates were computed against the OLD universe -- safer to drop them than risk showing stale match labels).

**Smoke test** (laptop, Finviz URL = user's intraday filter, today's market data):
- `POST /scanner/yf_scan?setup=ditp` returns `ok=true, universe_source="finviz (43 symbols)", n_candidates=0, fetch=2.6s, scan=0.0s`. Zero matches is expected for this universe (Finviz's high-volatility filter selects mostly recent IPOs that fail DITP's `len(bars) >= 220` gate). The integration is correct; the result is honest. To get matches with this URL we'd need to loosen DITP's bar-count gate or tighten the Finviz filter to exclude post-IPO names (add `ipodate_morethan10y` or similar).

**File-size delta**: index.html 30KB -> 38KB. The new filter panel + match-aware renderer + state management are non-trivial; reasonable cost for the UX gain.

### 2026-05-27 - Scanner view reduced to step 1: Finviz ticker list (setup application deferred)

User: *"we do it step by step, in the dashboard, you pull out the the FInviz Scanner ticker first. Then manually I can apply the setup that I am looking for"*. Scanner view is now Finviz-only -- it pulls the ticker list and that's it. Setup application (DITP P2 / DITP TC detection on the selected tickers) is a separate step that will be wired later.

**New backend endpoint**: `GET /scanner/finviz_tickers?force_refresh=<bool>` -> `{ url, count, rows: [{symbol, price, volume}, ...] }`. Reads the URL from `cfg["finviz_screener_url"]`, hits `resources/finviz_screener.fetch_screener_rows()` (cached 1h per URL). Returns 400 if the URL is empty (clear signal to the user to set config.json).

**`resources/finviz_screener.py`** gained `fetch_screener_rows(url, ...)` to complement the existing `fetch_screener_symbols()` -- same pagination + cache contract, returns dicts with price + volume parsed from Finviz's "TS" comment block (`<!-- TS\nSYM|PRICE|VOLUME\n... -->`) which is the cleanest hook on the page (independent of which `v=` view the URL requested).

**Frontend changes:**
- Scanner view now has ONE panel titled "Finviz scanner": header with row count + Refresh button, the source URL shown below, then a table of Symbol / Price / Volume rows.
- Auto-loads on view activation (cached, so view-switching is instant). Click Refresh to force-bypass the cache.
- Row click still opens TradingView in the named-target tab (existing event delegation).
- **Removed from the UI** (kept in JS as backend endpoints): the setup dropdown, Scan button, scan log pane, scan-stats line, results-table renderer, universe-label line. The DITP-via-yFinance flow + /scanner/yf_scan + /scanner/runs + /scanner/universe endpoints all stay alive for when step 2 lands.

**File size**: 36KB -> 30KB. The CSS for the removed UI bits (scan-controls / scan-log / runs-grid / age-badge / run-scanner) is still in the file unused -- I'll harvest it next time we touch the styles.

### 2026-05-27 - Scanner universe is now driven by `cfg["finviz_screener_url"]`

User directive: *"perhaps we store the finviz criteria in the scanner setting, if we want to change anything we can just change the URL"*. The dashboard scan universe now resolves in two tiers:
1. If `cfg["finviz_screener_url"]` is set, scrape the symbol list from that URL via `resources/finviz_screener.py` (cached 1h per URL).
2. Else fall back to S&P 500 from `resources/sp500.py`.

The user changes the URL in `config.json` to alter the scan filters (mid-cap+, ATR, beta, volatility, etc.) -- no code change needed.

**New backend endpoint**: `GET /scanner/universe?setup=ditp` -> `{ source, size, sample[:10] }`. Cheap probe so the Scanner panel can label the universe BEFORE the user clicks Scan (the source label appears in the panel header as "Universe: finviz (43 symbols)"). Same resolution logic as `/scanner/yf_scan` but skips the yFinance fetch.

**Updated**: `POST /scanner/yf_scan` response now includes `universe_source: "finviz (N symbols)" | "sp500"` so the scan results know which universe was actually used. The frontend echoes it in the stats line under the table.

**Config**: `config.example.json` now ships with the user's intraday-tradeable filter URL as the default example so a fresh clone behaves usefully out of the box.

**Frontend**:
- Panel header now reads `Universe: <source label>` (driven by `/scanner/universe` on view activation).
- Scan stats line includes the universe source.
- Sample symbols are shown as a tooltip when hovering the source label.

**Smoke test** (laptop, with `finviz_screener_url` pointing at the user's mid-cap+ intraday filter):
- `GET /scanner/universe?setup=ditp` -> `{"source": "finviz (43 symbols)", "size": 43, "sample": ["NVDA", "INTC", "RGTI", ...]}`
- `POST /scanner/yf_scan?setup=ditp` -> `ok=true, universe_source="finviz (43 symbols)", universe_size=43, fetched_n=43, n_candidates=0, errors=0, fetch=3.3s, scan=0.0s`. 0 candidates is plausible because Finviz's high-volatility set is dominated by recent IPOs (NVTS, LUNR, ASTS, QBTS) that fail DITP's `len(bars) >= 220` gate. The integration is correct; the strategy filter is just strict for this universe.

### 2026-05-27 - TradingView row-click: reuse the same browser tab across clicks

User: *"then the ticker is click, use back the same browser to view dont open new broswer"*.

Changed `window.open(url, '_blank', 'noopener,noreferrer')` to `window.open(url, 'intraday_bot_tv')`. The second argument is now a NAMED target instead of `'_blank'`:
- First row click -> opens TradingView in a new tab named `intraday_bot_tv`.
- Every subsequent row click -> the browser sees the existing tab with that name and NAVIGATES it to the new symbol's URL instead of spawning another tab.
- Net effect: at most ONE TradingView tab open across an entire scan session, no matter how many rows the user clicks.

Subtle trade-off worth recording: deliberately dropped the `'noopener,noreferrer'` features string. Those flags force a fresh top-level browsing context, which ignores the named-target reuse and reintroduces the new-tab-per-click behaviour we just fixed. Without `noopener`, TradingView could in theory call `window.opener` to navigate the dashboard tab; TV is a trusted first-party page so this risk is acceptable. If we ever embed an untrusted third-party in the click target, revisit.

### 2026-05-27 - Scanner view: removed legacy "Watchlist files on disk" panel

User: *"remove the old watchlist files on disk penal"*. The bottom panel showing aggregated `state/watchlist_*.json` content via `/lists/all` was the last vestige of the parquet-based scanning workflow on the dashboard. Per the architectural shift to yFinance-only dashboard scanning (earlier this session), it had become an inconsistent secondary surface -- nobody should be acting on those stale CLI outputs from the dashboard.

**Removed from `web/index.html`:**
- The `<section class="panel" style="opacity:0.7">...</section>` block containing the legacy table + its "kept for reference" placeholder text.
- `renderScanner()`, `loadScanner()`, `startScannerAutoRefresh()`, `stopScannerAutoRefresh()`, the `scannerTimer` module-level state, and the `scanner-refresh` button handler.
- The per-view lifecycle hook that auto-polled `/lists/all` every 60s when Scanner view was active.

**Kept:**
- `fmtNum()` helper (still used by `renderYfResults` for the live scan table).
- The TradingView row-click delegation (still applies to the yf-scan results table).
- The backend `GET /lists/all` endpoint -- no dashboard caller, but other consumers may exist (the bot's quote layer, future Monitor view), so retiring it is out of scope for this change.

**Page size**: ~36KB -> ~33KB. The Scanner view is now a single panel: dropdown + Scan button + log + results table. Nothing else.

### 2026-05-27 - Scanner tables: row click opens TradingView in a new tab

User feedback: *"when any row is click, open tradingview"*. Both Scanner tables (the new yFinance results table AND the legacy on-disk watchlist table) now respond to row clicks by opening `https://www.tradingview.com/chart/?symbol=<SYM>` in a new tab.

**Implementation choices:**
- **Event delegation** on `document` for `table.scanner tbody tr[data-symbol]` -- newly-rendered rows pick up the behaviour without needing to re-wire on every render.
- **Hover cues**: pointer cursor on rows, underline on the symbol cell, `??` arrow appended -- so it's discoverable, not a surprise.
- **`title` attribute** on each row spells out "Click to open NVDA in TradingView" for tooltip-on-hover.
- **Browser URL, not the TradingView MCP** (`resources/tradingview-mcp/`). The MCP would need TV Desktop running with `--remote-debugging-port=9222` -- works for some users, doesn't for others. The browser URL works for everyone, every session, no setup. If we ever want "open in Desktop" as an option, that becomes a second click target (e.g. a small button per row), not the default behaviour.
- **`window.open(url, '_blank', 'noopener,noreferrer')`** so the dashboard tab stays focused and there's no opener-window leakage.

Symbol form is the canonical dotted form (BRK.B not BRK-B). TradingView accepts both; we send dotted to match what the journal and the rest of the dashboard use.

### 2026-05-27 - Scanner: yFinance-direct scan path (parquet store decoupled from dashboard scanning)

User architectural directive: *"the parquet store i intend to use it for backtesting only, this scanning of daily setup through yfinance only"*.

Until now the dashboard's Scan button spawned the CLI scanner (`strategy/DITP/scanner.py`) which reads daily parquets via `bars_store.load_bars`. After this commit the dashboard scan path is fully yFinance-native -- no parquet read, no `state/watchlist_*.json` write.

**New backend endpoint**: `POST /scanner/yf_scan?setup=<ditp|ditp_tc>&limit=<N>`
1. Resolves the universe (currently SP500 via `resources/sp500.py`).
2. Batched yFinance fetch via `resources/yf_daily_bars.fetch_daily_batch()` -- ~2s for 5 symbols, ~30-60s for SP500. Returns bars in the canonical `[{t,o,h,l,c,v}]` shape.
3. Monkey-patches `bars_store.load_bars` to return the in-memory bars cache, then calls the existing DITP `scan_universe()` so the FULL P2 detection (EMAs, ATR, resistance discovery, flush-up filter, breach-rejection check, confluence tiering, scoring) runs unchanged. Patch is reverted in a `finally` block.
4. Returns candidates as JSON. **Nothing is written to disk**. **Parquets are never touched**.

**Frontend changes:**
- Dropdown now lists DITP P2 only (DITP TC option is greyed -- the TC scanner needs the prior day's P2 watchlist on disk which the yf path doesn't write; wiring DITP TC is a follow-up).
- Removed the `Refresh data (yFinance)` button (no longer needed -- the scan is self-contained).
- Removed the `scan-stats` line for stale-age display (the yf scan is always fresh by construction; no age to display).
- New results table renders the candidates inline: Symbol / Tier / Variant / Conf / Last / Resistance / Dist (ATR) / Score / Cautions. Sorted by `(distance_atr, tier, -score)` per `scan_universe()`.
- Kept the "On-disk watchlists (legacy)" panel below at 70% opacity with a clearly-labeled note. Useful for inspecting nightly CLI outputs (which still run on Hermes) without conflating them with the live yf scan.

**New module**: `resources/yf_daily_bars.py` -- batch yFinance adapter returning canonical bar dicts. Used by the dashboard endpoint; backtesting can keep using `yfinance_history.py` (which writes parquets).

**Why monkey-patch instead of refactoring `detect_p2`**: the DITP detection is deep production code with the full P2 spec embedded. Touching it risks regressing the nightly Hermes scanners. The monkey-patch is scoped to one request via `try/finally`, has no persistent side effects, and lets the SAME detection code path run on either parquet OR yFinance bars depending on caller.

**Out of scope this turn:**
- DITP TC via yFinance (TC needs the prior P2 watchlist; needs a small refactor to pass it in-memory).
- GUNS via yFinance (GUNS's live momentum scan is intraday and needs IBKR; not a daily-chart setup).
- Removing the legacy /scanner/run + /scanner/runs endpoints (kept for now; can be retired once the user has used the yf path enough to confirm they don't miss the CLI pathway).

**File-size delta**: `server.py` +145 lines (new endpoint), `web/index.html` ~+~30 lines (results table, simplified controls).

### 2026-05-27 - Scanner view: dropdown + Scan + yFinance refresh (laptop-friendly daily scan)

User feedback: *"i need you to construct the scanner page yFinance on daily chart setup. And I need the strategy on a dropdown box for me to select and click scan"*. Restructured the Scanner view from a 3-card grid (one card per family with its own Run button) to a single control row: setup dropdown + Refresh-data button + Scan button + log + stats line.

**Why this layout:**
- One decision at a time. The user picks a setup, then acts -- not "which of three buttons do I click first".
- Surfaces the data-source choice in the UI ("Data source: yFinance daily candles (via parquet store)"), making the laptop-friendly path obvious. The user explicitly wants the laptop to be self-sufficient for daily-chart setups without needing IBKR.
- GUNS is listed but disabled (greyed) with a tooltip explaining it needs IBKR for the live momentum scan. Honest signal: "this exists but won't work on this config" beats hiding it entirely.

**Two-step workflow (both optional/independent):**
1. **Refresh data (yFinance)** -> `POST /data/refresh-stale?timeframe=daily` -> pulls fresh daily bars from yFinance for any stale/missing parquets. Targeted -- if nothing is flagged, it's a no-op in seconds. Useful right before a scan if the user wants to be sure today's bars are in the parquet store.
2. **Scan** -> `POST /scanner/run?family=<setup>` -> spawns the selected scanner subprocess. DITP scanners read parquets and write a fresh watchlist.

**Both steps share state**: only one operation runs at a time (`scanInFlight` guard), other controls disable + show live elapsed counter. Log pane below shows stdout/stderr tail with green/red tint per exit code.

**Removed**: the `runs-grid` 3-card layout + its `run-card` CSS + the per-family `triggerScanner` JS. The /scanner/runs endpoint stays (drives the single-line stats display for the selected setup).

**File size**: 33KB -> 35KB.

**Backend**: no new endpoints required. Uses the existing /scanner/runs + /scanner/run + /data/refresh-stale.

### 2026-05-27 - `server.py` + `web/index.html`: watchlist age indicator + on-demand scanner run

User feedback: *"yes need a watchlist age and i should be able to run the scanner"*. The Scanner view was showing 15 symbols but the user couldn't tell that they were from a 1-7 day old scan, and there was no way to refresh other than dropping to a terminal.

**New endpoints in `server.py`:**

`GET /scanner/runs` -> per-family scanner metadata:
```json
{
  "today_et": "2026-05-26",
  "families": {
    "guns":    { "label": "GUNS", "latest_file": "watchlist_guns_2026-05-20.txt",
                 "target_date": "2026-05-20", "n_candidates": 5, "age_days": 6, "stale": true, ... },
    "ditp":    { "label": "DITP P2", "latest_file": "watchlist_ditp_2026-05-25.json",
                 "target_date": "2026-05-25", "scanner_run_at": "2026-05-24T08:24:39+00:00",
                 "n_candidates": 10, "age_days": 1, "stale": true, ... },
    "ditp_tc": { "label": "DITP TC", "latest_file": null, "n_candidates": 0, "stale": true, ... }
  }
}
```

`POST /scanner/run?family=<guns|ditp|ditp_tc>` -> spawns the scanner subprocess for that family, waits up to 300s, returns `{ ok, returncode, duration_s, stdout_tail, stderr_tail, meta }`.

**Safety**: family is validated against `_SCANNER_REGISTRY` whitelist (no shell injection possible); `sys.executable` is the same Python that's running the server (which under the supervisor is `py -3.12` per the `eventkit`/`ib_insync` rule in CLAUDE.md); `cwd` pinned to `SKILL_DIR` so the scanner's relative imports work; `subprocess.run` with `shell=False` (default for list-argv form). Runs in a thread-pool executor so the FastAPI event loop stays responsive for other polls while the scan is in flight.

**Frontend additions (Scanner view):**
- New top panel "Scanners" -- a responsive grid of cards, one per family. Each card shows:
  - Family label (GUNS / DITP P2 / DITP TC)
  - Age badge: `fresh` (green, age <= 0), `stale` (amber, 1-7 days), `ancient` (red, >7 days), `never scanned` (gray, no file)
  - Candidate count + filename + scanner-run timestamp
  - Run scanner button
- Click Run -> button switches to "Running... Xs" with a live elapsed counter; other Run buttons disable (one scanner at a time keeps subprocess load predictable); a collapsible log pane appears below the card showing stdout/stderr tail on completion. On success: tinted green border, log shown for review. On failure: tinted red border + non-zero exit code surfaced.
- After the run completes (or fails): both `/scanner/runs` and `/lists/all` are re-fetched so the cards + the watchlist table reflect the new state in one update.
- Second panel ("Today's watchlist") is the existing aggregated table, unchanged.

**Threshold choice for the age badge** (fresh / stale / ancient cutoffs at 0 / 1-7 / 8+) reflects how the watchlists are USED: a watchlist becomes operationally useless the moment trading-day rolls over. Anything 1 day old is already targeting yesterday's session and shouldn't be acted on. 7 days is the soft limit where the data is so stale even a glance-confirmation has no value. These are display thresholds only -- the bot doesn't act on them.

**File size**: 23KB -> 33KB.

### 2026-05-27 - `web/index.html`: sidebar + Scanner view (function-specific navigation lands)

User feedback: *"i want you to build a side bar with scanner first"* + earlier *"next want the dashboard to be function specific"*. Switched the layout from top-tab strip to left-sidebar function nav, with Scanner as the first (default-active) sidebar slot. The other four functions (Monitor, Strategies, Trades, Data) are stubs until each is explicitly built out.

**Layout shape:**
```
+------------------------------------------+
|  STATUS BAR (sticky, always visible)     |
+----------+-------------------------------+
| SIDEBAR  | ACTIVE VIEW                   |
| Scanner* |   - one panel per concern     |
| Monitor  |   - view-specific data only   |
| Strats   |                               |
| Trades   |                               |
| Data     |                               |
+----------+-------------------------------+
```

**Sidebar choice over top tabs**: vertical nav scales better as more functions accrete and leaves the full content width for tables (which is what most functions render). The active item gets a left-edge accent stripe + tinted background, which carries glance-recognizable state from anywhere on the page.

**Scanner view built:**
- Data source: `GET /lists/all` (already existed -- aggregates `state/watchlist_<family>_<date>.{txt,json}` across all strategy families via `_aggregate_watchlist_rows()` in `server.py`).
- Table columns: Symbol / Strategy / Tier (color-coded A=green, B=amber, C=muted) / Variant / Resistance / Last / Chg % / ARM (live ARMED/disarmed pill driven by `armed_map` in the same payload).
- Auto-refresh every 60s while view is active; manual Refresh button next to the timestamp. Auto-refresh stops when leaving the view (per-view lifecycle hooks in `activateView()`).
- Meta line: total symbol count + per-strategy breakdown (e.g. `15 symbols  ·  DITP=15  ·  as of 09:01:23`).
- Empty state when scanners haven't run yet.

**Per-view lifecycle hooks** are the design pattern future views should follow: each view registers `boot` + `teardown` actions in `activateView()` so per-view pollers / WebSocket subscriptions don't run when their view is hidden. Saves bandwidth + keeps event loop clean.

**File size**: 13KB -> 23KB. Still hand-readable end-to-end.

**Endpoints exercised**: `/snapshot`, `/data/health`, `/lists/all`. No new backend endpoints required.

### 2026-05-27 - `web/index.html`: status bar (panel 1 of rebuild) -- 6 pills

First panel of the rebuild lands: a top status bar with 6 pills covering "is the world OK right now".

**The six pills:**
| Pill | Source | Poll | Levels |
|---|---|---|---|
| Local | `new Date()` | 1s tick | (no color, just time) |
| ET    | `Intl.DateTimeFormat` America/New_York | 1s tick | (no color, just time) |
| Stage | computed client-side from ET hour/min/weekday | 1s tick | ok=RTH, warn=pre/after/closed |
| IBKR  | `/snapshot` -> `health.ibkr` | 5s | ok=up, err=down, warn=no_lib/other |
| Alpaca| `/snapshot` -> `health.alpaca` | 5s | ok=ok, warn=no_credentials, err=error |
| Price Data | `/data/health?timeframe=daily` -> `overall` + summary | 30s | ok / warn / err mapped from overall |

**Design choices worth keeping for future panels:**
- Time pills use monospace numerals (`font-variant-numeric: tabular-nums`) so the seconds digit changing doesn't jitter the row width.
- Stage classification is client-side from ET parts (NOT a call to `/market/clock`) because `/market/clock` requires Alpaca creds and would return `is_open: null` on the laptop dev config; we want the stage pill to work everywhere. Holiday awareness is deliberately out of scope for the pill -- the bot's scanner remains the source of truth for "is today a trading day".
- All five non-time pills go red on fetch failure rather than freezing on the previous value. Silent UI is worse than an honest red dot.
- Polling cadences differ by cost: 5s for snapshot (cheap, drives most state), 30s for data/health (slow-moving, scans the universe parquets), 1s for clocks (purely client).

**File size**: 5KB -> 13KB (vs the original 163KB). Still small enough to scan end-to-end.

**Endpoints exercised**: `/snapshot`, `/data/health`. No new backend endpoints required.

### 2026-05-27 - `web/index.html`: WIPE -- restart layout design from scratch

User feedback: *"the whole dashboard concept is too complicated. I want to clear all layout and restart the layout design"*. The previous index.html had grown to ~163KB / ~10 panels (status bar + sidebar drawers + GUNS/DITP/OS family tabs + watchlists + positions + orders + event log + bot log + gating drawer + ...) and the cognitive cost of scanning it had exceeded the value it provided as a control surface.

**Approach: "wipe and we build up"** (user pick from a 4-option question on 2026-05-27). Strip to a blank page + one "server alive" pill. Add panels back one at a time, each justified before it lands.

**Done:**
- `web/index.html` archived to `web/index.html.old` (gitignored via root `.gitignore`; kept on disk for snippet salvage during the rebuild only -- delete locally once rebuild is far enough along).
- New `web/index.html`: 5KB plain HTML + scoped CSS + 30 lines of vanilla JS. Polls `/snapshot` every 5s to drive the alive-check pill. No Tailwind, no framework, no build step.
- Layout conventions for the rebuild are documented in the new file's top CSS comment so future panels stay coherent: plain HTML/vanilla JS, one `<section class="panel">` per concern, status pills as `<span class="pill ok|warn|err">`, ET-labeled timestamps when ET.
- `server.py` endpoints untouched -- every old endpoint (`/snapshot`, `/data/health`, `/lists/all`, `/strategy/ditp/watchlist`, `/strategy/ditp/tc_watchlist`, `/bot/status`, `/bot/arm`, `/bot/enable`, `/market/sentiment`, `/market/clock`, `/chart/data`, `/data/ingest-log`, `/ws`, ...) still responds. They simply have no caller in the new UI until panels grow back to consume them.

**Why this is safe to do mid-stream**: the dashboard is observation + control. Nothing it shows is a source of truth. The journal, parquets, state flags are all that matter for correctness; the UI just renders them. Wiping the UI loses zero data and breaks no other component.

**Hard rule still in force** (CLAUDE.md "Dashboard visibility rule"): every observable feature must eventually be surfaced. The wipe doesn't repeal that rule -- it just means we re-add surfaces deliberately instead of accreting them. The growth-back conversation starts with: "what's the most important thing to see first?"

### 2026-05-27 - `start_dashboard.bat`: fixed silent failure on double-click ('M' is not recognized...)

User reported that double-clicking `start_dashboard.bat` produced "nothing happens" — no browser open, no dashboard, no visible window. Reproduction via `cmd /c start_dashboard.bat` exposed `'M' is not recognized as an internal or external command` on stderr; the dashboard launch logic was being silently corrupted.

Root cause (TWO compounding bugs):
1. The file had LF-only line endings (Unix-style), but Windows `cmd.exe`'s `^` line-continuation parser is unreliable without CRLF. The multi-line `powershell -Command "..." ^ "..." ^ ...` block was being mis-tokenized — cmd was treating `Minimized;"` from line 19 as the start of a new command, hence the `'M'` error.
2. Even when the `^`-continuation parsing worked, the multi-line argument-array form of `powershell -Command` is fragile across PowerShell versions.

Fix: rewrote `start_dashboard.bat` to use a SINGLE-LINE `powershell -Command "..."` with semicolons, no `^` continuations needed. Also forced CRLF line endings on `start_dashboard.bat`, `_supervise_dashboard.bat`, and `stop_dashboard.bat` (all three were LF-only) via a one-shot `[System.IO.File]::WriteAllText` rewrite. The single-line form is unambiguous regardless of whether the file is LF or CRLF, but CRLF is now the on-disk standard for these files going forward.

Verified end-to-end: bat now exits 0 with empty stderr, supervised dashboard launches cleanly, port 8000 listens, browser auto-opens to `http://localhost:8000`, dashboard responds with HTTP 200.

ASCII-only in the file body per the existing house style (no em-dashes).

### 2026-05-27 - `tray_status.py`: Tk-on-main-thread + pystray-on-daemon-thread (final architecture)

The subprocess approach (commit 3a7bcf2) had a Windows-specific failure: after the first "Show Status" click successfully opened a window subprocess, the PARENT tray process died. Confirmed 2026-05-27 by process queries — `Get-CimInstance` returned no python.exe with `tray_status.py` after the first click. Why exactly the spawn killed the parent on this user's setup is unclear (possible interaction between pystray's win32 NotifyIcon, the py.exe launcher chain, and `subprocess.Popen` with `CREATE_NO_WINDOW`), but the symptom — tray icon disappears after first window-open — was reproducible.

Proper fix: the standard Python GUI-tray pattern. Single process, no spawn churn.

**Architecture:**
- `main()` runs on the interpreter's real main thread; creates Tk root, builds the progress-window widgets ONCE, then `root.withdraw()` (hides it).
- pystray runs in a daemon thread (`threading.Thread(target=icon.run, daemon=True).start()`).
- "Show Status" / "Quit" callbacks fire on the pystray thread; they MUST NOT touch Tk widgets. Instead they enqueue strings onto `_command_queue`.
- A Tk-side poller (`root.after(100, process_commands)`) reads the queue every 100ms and acts on the main thread — `deiconify` + `lift` + `focus_force` for 'show', `icon.stop()` + `root.quit()` for 'quit'.
- The window's close button / X / Escape now calls `root.withdraw()` (hide), NOT `root.destroy()` (which would kill the whole Tk session). The window is persistent for the process lifetime — subsequent Show Status clicks just re-deiconify the same widgets.

**Removed:**
- `subprocess` import (no longer spawning anything)
- `--window-only` CLI mode (no longer needed; window lives in the tray process)
- `_progress_window_active` single-instance flag (no longer needed; the window IS singular by construction)
- `_show_progress_window` wrapper (was the try/finally lifecycle guard for the spawn approach; the new architecture has no spawn to guard)

**Why this is more robust than every previous attempt:**
- No thread-safety issue (Tk operations all happen on main thread)
- No subprocess (no parent-child interaction)
- No flag to get stuck (no single-instance guard)
- The progress window is created exactly once per process, hidden by default — even ttk style configuration only runs at startup, not on every click. Any future Tk gremlin shows up at process start, not at the first click after hours of uptime.

Smoke-tested structurally: imports clean, `subprocess` no longer imported, `queue` is, `_command_queue` is a `Queue`, `_on_show_status`/`_on_quit` both put on the queue, `main()` contains the `tk.Tk()` + `root.withdraw()` + `process_commands` + daemon `icon.run` + `root.mainloop()` pieces, and `--window-only` is gone. End-to-end UI verification happens when the user runs `py -3.12 dashboard\tray_status.py` and clicks Show Status repeatedly.

### 2026-05-26 - DITP TC (Trend Continuation) watchlist now visible in the DITP family tab

Closes the dashboard-visibility gap that the TC Phase 1 build (commit 2e00724) left open per CLAUDE.md's "UI catches up next turn" rule. TC was wired into `KNOWN_STRATEGIES` and journal events were auto-surfacing in the Strategy Analysis drawer, but the TC watchlist file itself (`state/watchlist_tc_<date>.json`, produced EOD by `strategy/DITP/tc_scanner.py`) had no dedicated UI.

**Backend**: new `GET /strategy/ditp/tc_watchlist` endpoint mirroring `/strategy/ditp/watchlist`. Returns the highest-dated `state/watchlist_tc_*.json` payload (or `{candidates: [], note: "no_watchlist_yet"}` when no file exists).

**Frontend**: the DITP family tab now renders TWO stacked tables. The TC table sits on top (since it shows TOMORROW's actionable list) with a green title bar (`#86efac`) and these columns:

| Column | Source field | Meaning |
|---|---|---|
| tier | `p2_tier` | Inherited from the originating P2 candidate's tier |
| sym | `symbol` | Clickable; opens the chart |
| variant | `p2_variant` | TC_A/B/C — inherited P2 sub-variant |
| D0 close | `day0_close` | The breakout day's close (today) |
| R cleared | `resistance` | The level the close cleared (= P2.range_high) |
| brkATR | `breakout_strength_atr` | (close − R)/ATR — how cleanly the close cleared R |
| closPos | `day0_close_position` | (close − low)/(high − low) — 1.0 = closed AT high, 0.5 = midpoint |
| conf | `confluence_tier` | Inherited confluence tier (0 plain → 3 triple) |
| cautions | `cautions` | Inherited from P2 |

Header line shows `target Day+1 date · from Day-0 date · N tradeable of M · A/B/C tier counts · scanner timestamp · file`. P2 table below is unchanged.

**`familiesFromAnalysis()` updated** to include DITP if EITHER P2 or TC has candidates (was only checking P2). When the P2 file is missing but TC has data (rare, but possible if user only ran tc_scanner.py), the TC section renders alone with a placeholder for the missing P2 file.

Both watchlists refresh on the same 5-minute cadence as P2 (`initDitpWatchlist`). TC scanner runs after P2's EOD pass, so the TC payload appears slightly later in the EOD window.

Smoke-tested end-to-end:
- empty state: `{candidates: [], note: "no_watchlist_yet"}` ✓
- synthetic payload (2 candidates with realistic AAON + AAPL data): endpoint returns 200 with `n_cands=2`, fields preserved (`brkATR=0.42`, `closPos=0.875`)
- orchestrator dry-run still passes (5 strategies wired)

### 2026-05-26 - `tray_status.py`: progress window spawned as subprocess (fixes "main thread is not in main loop")

Sequel to 7c00359 (which added the try/finally + traceback that surfaced the real cause). With the diagnostic in place, the next Hermes attempt revealed:

```
[tray_status] _show_progress_window failed: RuntimeError: main thread is not in main loop
  ... line 762 in _show_progress_window_inner
    pct_var = tk.StringVar(value='-')
```

Tkinter requires its operations to run on the actual main thread of the Python interpreter. pystray's "Show Status" callback runs on its own thread; any daemon thread we spawn from it is not the main thread either. On Python 3.12 this surfaces as `RuntimeError: main thread is not in main loop` from the very first `tk.StringVar(...)` call after `tk.Tk()`.

Fix: `_on_show_status` now spawns the window as a SUBPROCESS — `py -3.12 dashboard\tray_status.py --window-only` — instead of a daemon thread. Each subprocess gets its own fresh interpreter with its own main thread (which IS the thread that runs Tk), so the thread-safety issue disappears entirely. `main()` checks for `--window-only` in argv and short-circuits straight to `_show_progress_window_inner()` without starting pystray or the update loop.

Trade-off accepted: ~0.5–1s subprocess startup latency per click. That's well within tolerance for a tray-icon click, given the alternative was "window doesn't open at all." Bonus: each click is now fully isolated — a Tcl crash, font loading failure, or any other Tk gremlin in one window can't take down the tray or affect future windows.

On Windows the subprocess uses `CREATE_NO_WINDOW` so there's no cmd-shell flash at spawn time. stdin/stdout/stderr are routed to DEVNULL since the subprocess doesn't need a console — if it has its own internal error, it's still visible via the existing try/finally + traceback inside `_show_progress_window` (which is now the body of the subprocess's main).

The `_progress_window_active` single-instance flag is preserved (no harm) but is effectively unused — multiple clicks now spawn multiple subprocess windows, which is fine.

### 2026-05-26 - `tray_status.py`: fix "tray icon can't open the window" — flag stuck after a failed first-open

User report: clicking the tray icon stopped opening the progress window. Root cause: `_show_progress_window` set `_progress_window_active` BEFORE the Tk-construction code, but the lifecycle's try/finally only wrapped `win.mainloop()`. If anything between the .set() and the mainloop raised (`tk.Tk()` can fail when launched from a daemon thread; ttk style configuration can fail under certain Tcl builds; etc.), the function exited via exception with the flag stuck set forever. Every subsequent click hit the single-instance early-return at the top → silent no-op → window never opened again.

Fix: split into `_show_progress_window` (a thin wrapper that takes the flag, calls the inner, releases the flag in a finally clause, and logs any exception to stderr) and `_show_progress_window_inner` (the original window-setup + mainloop). The flag is now released regardless of what raised, AND a traceback hits stderr so a foreground-launched tray surfaces the real failure cause for diagnosis.

Smoke-tested by monkey-patching `_show_progress_window_inner` to raise a simulated exception — verified the flag is cleared on exit and the traceback prints. No behaviour change in the happy path (window opens normally → mainloop runs → close → flag released).

### 2026-05-26 - `tray_status.py`: pre-flight work-item count is now the progress denominator

User screenshot showed the Tk progress window stuck at "1 / 1518 symbols (0.1%)" with `Latest: ALLY` shortly after a Hermes watcher restart. The watcher had correctly skipped ~30 already-deep A-symbols (the new `skip_up_to_date=True` from earlier today), but the tray's denominator was still the universe size (1518), making the actual progress look trivial.

Fix: tray now parses the watcher log for the line emitted by `bulk_update`'s pre-flight pass:

    [pre-flight] 47 unique symbols need work

When that line is present in the current iteration's `_ingest_*.log`, the tray uses `47` as the denominator instead of the universe size. The Tk window label switches from `"X / N symbols"` to `"X / N to fetch"` to reflect the smaller, more meaningful number. Falls back to the universe-size denominator + "symbols" label when the pre-flight line isn't present (legacy log files, or pre-flight still running).

New helper `_work_symbols_from_iteration_log(log_name)` with a (path, mtime) cache so the parse runs once per iteration rather than on every 3-second refresh.

`get_progress()` returns a new `target_source` field (`"pre-flight"` vs `"universe"`) so the UI can adapt its label accordingly.

Paired with `resources/ibkr_history.py::bulk_update` gaining a `log_callback` parameter (so the pre-flight summary actually lands in the watcher log — under Task Scheduler on Hermes, the child stdout is discarded and the lines would otherwise be lost). See `resources/README.md` changelog for the matching backend change.

### 2026-05-26 - `tray_status.py`: full audit, four bugs + dead-code purge

Trigger was a Hermes screenshot showing `92 / 2 symbols (100%)` with `Latest: AMZN` while the supervisor log on the right showed iteration #1 had just launched and was actively writing A and AA. End-to-end audit of `tray_status.py` against real ingest_log + watcher logs surfaced four real bugs plus one dead function. All four fixed in one bundle; verified against the live Hermes data:

| Field | Before | After |
|---|---|---|
| `symbols_done` | 92 (carried over from crashed iteration) | 25 (just this iteration) |
| `target` | 2 (only A, AA seeded so far) | 1518 (universe_full.txt) |
| `progress_fraction` | 1.0 (clamped from 46.0) | 0.016 (real) |
| `latest_symbol` | "AMZN" (frozen on dead iteration) | "ACN" (actual current) |

**Bug 1 - Denominator was the daily-parquet count.**
- `bars_store.list_symbols("daily")` returns `[]` during fresh seed and small numbers during partial Resilio sync; the pre-existing 1519 fallback only fired on import/IO exceptions, not on an empty list.
- **Fix**: new `_target_universe_size()` returns `max(daily_parquet_count, universe_full_txt_line_count) or 1519`. Larger wins. Verified: `max(2, 1518) = 1518`.
- **Known limitation**: narrow `--universe journal` runs (~50 syms) still get denominator 1518, under-reporting % for that run. Proper fix is `wait_and_ingest.py` publishing the exact target to `state/_ingest_target.json` -- deferred until the narrow-universe view matters.

**Bug 2 - `latest_symbol` froze across supervisor restarts.**
- Read from `syms_in_order[-1]` -- the last *first-appearance*. After supervisor restart, the new iteration's A/AA/AAPL are already in the `seen` dedup set from the dead iteration, so `syms_in_order` doesn't grow even as fresh ingest_log entries arrive. `latest_symbol` stayed pinned to whichever symbol died last.
- **Fix**: `latest_ts, latest_symbol = current_run[-1]` -- the actual chronologically-last log entry.

**Bug 3 (the big one) - `symbols_done` and `rate_per_hour` spanned the dead iteration.**
- `get_progress()` used a 1-hour run-gap rule. Supervisor restarts in 30s, so a fresh iteration was lumped in with the previous crashed iteration's 90+ syms in the same "current run." The new iteration that has done 2 syms in 18 seconds was displayed as `92 / target, rate 18/hr` (a 5-hour-average rate that has nothing to do with what's happening right now).
- **Fix**: new `_latest_iteration()` parses the `_(\d{8})_(\d{6}).log$` timestamp out of the newest `_ingest_*.log` filename and uses that as a hard iteration boundary. ingest_log entries earlier than that cutoff are dropped. The 1-hour-gap rule survives only as a fallback for the no-watcher-log case (e.g., someone running `ibkr_history.py` directly).
- Side benefit: each iteration's `rate_per_hour` and `eta_hours` are real per-iteration numbers, not 5-hour cross-iteration averages.

**Bug 4 - `progress_fraction` saturated silently at 1.0.**
- `clamp(0, 1, n/d)` papered over the denominator-wrong case as `100%`. User sees a green progress bar with no signal anything's off.
- **Fix**: `get_progress()` now returns an `overshoot: bool` field (True iff `symbols_done > target`). The Tk window renders `?` in amber instead of 100% green when overshoot is True.

**Bug 5 (housekeeping) - `get_progress_summary()` was dead code.**
- 44 lines, no callers. Was the old toast-popup body that the 2026-05-26 Tk-window changelog already replaced. Deleted.

**Milestone state - keyed by iteration.**
- `_load_milestone_state(iteration_key)` returns fresh state when the on-disk key doesn't match the current iteration's filename. So a supervisor restart -> new `_ingest_*.log` -> milestone notifications re-fire cleanly as the new iteration crosses 50/100/250/... again. No "ghost" milestones surviving a restart, no missing toasts when the user wants to see them.

**Live activity indicator in the Tk window.**
- User: *"i need a progress or moving animation to show it is progressing"*. The Tk window was static between 3s data refreshes, and at ~18 syms/hr the % only ticks every ~3 minutes. No way to tell at a glance whether anything was happening.
- Added a live indicator row between the count line and the letter line:
  - **Spinner glyph** (braille `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) cycling every 150ms -> proves the tray UI is alive and the refresh loop is firing
  - **Status dot** colored green / amber / red mirroring the tray-icon liveness thresholds (`<60s` / `<10min` / older) -> proves the watcher itself is alive
  - **"last write Ns ago"** counter incrementing live -> proves the ingest pipeline is still producing entries
- Refactored `_show_progress_window()` to split two cadences: `refresh_data()` every 3s (re-reads `ingest_log.jsonl` via `get_progress()`), `animate()` every 150ms (spinner + age counter only, reads cached `last_write_dt` so no file I/O). 5+ ticks/sec on the spinner without hammering the ~2MB log file.
- New `last_write_at` field on `get_progress()` return so the animation tick has the latest entry's UTC timestamp to compute "age" against.
- Window grew 300 -> 330 px tall to fit the new row.

**What I did NOT touch.**
- The `"Letter X done"` notification text still fires when ANY symbol with first letter > X appears -- it means "we've moved past alphabetically," not "every X-symbol completed." The text overstates but the inaccuracy is mild and an iteration-boundary `letters_done` reset makes the case clearer.
- 7-second IBKR pacing, 60s `RUNNING_THRESHOLD_SEC`, 30s `POLL_INTERVAL_SEC` -- all left as-is, they match the actual cadence in the ingest_log. If the new amber-after-60s on the live indicator turns out to flicker too often during legitimate big-symbol chunks (3 timeframes × N chunks @ 7s pacing can naturally span 60-200s between log entries), bump the threshold then.

### 2026-05-26 — Tray click → Tk progress window (big % + visual progress bar)

- User rule: *"i want the percentage and a progress bar to show when i click the tray icon"*. The previous "Show Status" action fired a Windows toast with multi-line text — text-only, truncated at ~250 chars, no visual progress indicator.
- **Replaced with a Tkinter Toplevel window** that left-click on the tray icon now opens:
  - **Big 44px green percentage** centered at the top
  - **Visual `ttk.Progressbar`** (380px wide, 22px thick, green fill on dark trough)
  - **Symbol counter** (`X / 1519 symbols`)
  - **Current letter + latest symbol** line
  - **ETA + rate** line (gracefully shows "gathering data..." if rate not computable yet)
  - **Close button** + Escape key + window-X all dismiss
- **Self-refreshing every 3 seconds** via `win.after()` — the window updates in place as the ingest progresses without you having to close + reopen.
- **Always-on-top** (`-topmost`) so it doesn't get lost behind other windows.
- **Single-instance** via `_progress_window_active = threading.Event()` — clicking the tray icon while the window is already open is a no-op rather than stacking multiple windows.
- **Thread-isolated**: Tk's `mainloop()` blocks, so the menu callback launches it in a daemon thread to keep pystray's event loop responsive. Win is destroyed cleanly when closed, lock released, ready for next open.
- **Dark theme**: `ttk.Style('clam')` + custom colors — matches the tray's aesthetic rather than the Windows 95 default ttk look.
- The old text-toast notification path is gone for "Show Status". The toast remains for milestone notifications + transient one-offs.

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
