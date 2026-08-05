# bridge/ — TradeHunter TV Bridge

**Role:** a tiny, zero-dependency local sidecar that lets the TradeHunter web app
draw **MATP / MBP** levels on the *user's own* TradingView (web, in Chrome). It is
the local half of the `/matp` "Plot on TV" feature.

## Why it exists

The TradingView MCP drives TV over Chrome DevTools Protocol (CDP), which is
**localhost-only**. TradeHunter is served from Hermes, so the **server can never
reach a user's local TV**. Instead, each user runs this bridge on their **own**
machine:

```
[TradeHunter /matp page in browser]          [the user's own PC]
  click ⧉ / "Plot on TV"  ──fetch──▶  http://127.0.0.1:9223/plot?symbol=NVDA&exchange=NASDAQ&matp=180.5&mbp=165.0
                                              │  (this bridge — Node, zero deps)
                                              ▼  CDP → the user's Chrome TV tab
                                        • setSymbol(NVDA)
                                        • draw MATP line @180.5   (orange)
                                        • draw MBP  line @165.0   (green)
```

Because the bridge is local to each user, **every user only ever plots on their
own chart** — the server is never in the loop. That's the multi-user safety
guarantee.

## Contents

- `tv_bridge.mjs` — the bridge. Zero dependencies: Node built-in `http` + global
  `fetch` + global `WebSocket` (needs **Node ≥ 22**; no `npm install`). Reuses the
  MCP's exact chart calls (`window.TradingViewApi._activeChartWidgetWV.value()` →
  `setSymbol` / `createShape` / `getAllShapes` / `removeEntity`).
- `launch_tv_bridge.bat` — one double-click: launches Chrome with the CDP debug
  port + opens TradingView, then starts the bridge.

## Setup (per PC — the whole thing)

Runs on: **your local PC** (the one with your logged-in TradingView in Chrome).

1. Make sure **Node ≥ 22** is installed (`node --version`).
2. Double-click **`launch_tv_bridge.bat`**.
   - It opens Chrome with `--remote-debugging-port=9222` + a `tradingview.com/chart`
     tab, then starts the bridge on `127.0.0.1:9223`.
3. In TradeHunter → `/matp`, click a ticker's **⧉** (row) or **▧ Plot on TV** (chart
   toolbar). The MATP + MBP lines appear on your chart.

That's it. The bridge + launcher travel with the Dropbox-synced repo, so on a new
PC it's the same: sync → double-click.

### The one gotcha: "Chrome already running"

The debug port only opens on the **first** Chrome process for a given profile. If
your everyday Chrome is **already open** on the default profile, the launcher's new
tab is just handed to the existing process and the port stays closed — "Plot on TV"
then reports it can't reach the port.

Fixes (pick one):
- **Fully quit Chrome first**, then run the `.bat`.
- **Add the flag to your everyday Chrome shortcut** permanently: append
  `--remote-debugging-port=9222` to the shortcut's Target. Then your normal,
  logged-in Chrome is always CDP-ready and the `.bat` just needs the bridge.
- **Use a dedicated profile** that runs *alongside* your normal Chrome: uncomment the
  `TH_USER_DATA` line in the `.bat` (`--user-data-dir=%LocalAppData%\TradeHunterTV`)
  and log into TradingView once in it. Always works, never fights your main browser.

## Endpoints

- `GET /health` → `{ ok, bridge, tv_tab }` — is the bridge up and can it see a TV tab?
- `GET /plot?symbol=&exchange=&matp=&mbp=` → sets the symbol and draws/refreshes the
  two lines. `&nav=1` returns a self-closing HTML page (used by the browser's
  `window.open` fallback if a loopback `fetch` is ever blocked).

Env overrides: `TH_TV_BRIDGE_PORT` (9223), `TH_TV_CDP_PORT` (9222), `TH_TV_CDP_HOST`
(127.0.0.1).

## Idempotency (no duplicate lines)

The bridge records the shape ids it created **per symbol** on a page global
(`window.__TH_LINES`) and removes *its own* previous MATP/MBP lines before redrawing.
So clicking a ticker again **refreshes** the pair in place — never stacks duplicates,
never touches your manual drawings. (Edge case: if you fully reload the TV tab between
plots, the in-page tracking resets; a subsequent plot may leave the pre-reload pair
behind. Just clear those two lines by hand, or re-plot after they're gone.)

## Changelog

### 2026-07-18 — v1.1.0: auto-launch, locked lines, reliable de-dupe, PNA, autosave
- **Auto-launch Chrome on demand** — if no debug Chrome is found when you click "Plot
  on TV", the bridge launches one itself (dedicated `TradeHunterTV` profile, port 9222).
  So you can close Chrome freely; the next plot brings it back. New `bridge_only.bat` +
  Startup shortcut keep just the bridge running, Chrome opens when needed.
- **Locked lines** — MATP/MBP now draw with `disableSelection:true` + a post-create
  `setProperties({frozen:true})` (the create-time lock option is a no-op on TV), so they
  can't be dragged/edited. `removeEntity` still works on them, so redraw is unaffected.
- **De-dupe by chart label** — before drawing, the bridge scans the chart for existing
  `MATP …`/`MBP …` lines and removes them. This reads the chart itself, so it survives a
  TV close/reopen (the old in-memory id map didn't) → no more duplicate lines.
- **Private-Network-Access header** — `Access-Control-Allow-Private-Network: true` on the
  preflight, so the fetch from `https://app.tradehunter.net` to `http://127.0.0.1:9223`
  isn't blocked (no more window.open fallback popup).
- **autosave()** after drawing so lines persist to the account layout (when logged in on a
  named layout). Version stamped (`/health` + startup log report `v1.1.0`).

### 2026-07-18 — v1.0.0: initial TV bridge
- Added `tv_bridge.mjs` (zero-dep CDP sidecar) + `launch_tv_bridge.bat`.
- Powers the `dashboard_tst` v3.44 `/matp` "Plot on TV" feature. Draws MATP (orange)
  + MBP (green) horizontal lines on the user's own TradingView web chart; idempotent
  per-symbol redraw; `/health` + `/plot` endpoints; `window.open(&nav=1)` fallback for
  mixed-content-blocking browsers.
