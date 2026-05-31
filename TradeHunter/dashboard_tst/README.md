# dashboard_tst/ — operational UI (trend & swing trading)

The **trend & swing** dashboard. FastAPI + WebSocket backend, static
HTML/Tailwind frontend, served on **port 8001**. Runs as a SEPARATE
process alongside the intraday dashboard (`dashboard_intraday/`, port
8000) — both can run at the same time.

Created 2026-05-29 as an **independent copy** of
`dashboard_intraday/server.py` (user request: *"I want to wire another
dashboard call dashboard_tst which is for trend and swing trading"*).
The copy is the working baseline; content is being trimmed / extended
to the trend-and-swing workflow per ongoing user direction.

## Relationship to dashboard_intraday/

| | dashboard_intraday | dashboard_tst |
|---|---|---|
| Port | 8000 | **8001** |
| Purpose | Intraday bot control + DITP/GUNS scanner | Trend & swing trading |
| Bot lifecycle (orchestrator) | **Owns it** — auto-start, ON/OFF, ARM | **None** — auto-start disabled |
| Code | canonical | independent copy, diverging |

**Hard rule (CLAUDE.md):** only ONE dashboard may control the live
orchestrator, because the per-strategy state flags don't coordinate
across processes. `dashboard_tst` therefore has `_auto_start_loop`
**omitted from its lifespan** — it never spawns `execution/orchestrator.py`.
It's a read-only observer + trend/swing scanner. The bot start/stop
endpoints still exist (inherited from the copy) but must not be used
from this dashboard.

## Contents

- `server.py` — FastAPI app on port 8001. Same endpoint surface as the
  intraday server MINUS the auto-start coroutine. sys.path bootstrap
  adds `dashboard_tst` (not `dashboard_intraday`). `title="dashboard_tst
  (trend & swing)"`.
- `web/index.html` — single-page UI (copied from intraday; branding
  updated to "TST · trend & swing"). Uses RELATIVE API URLs so it works
  on 8001 automatically.
- `start_dashboard.bat` / `stop_dashboard.bat` / `_supervise_dashboard.bat`
  — Windows launchers, port 8001, ASCII-only, `%~dp0`-relative.
- `setup_launcher.py` — one-time installer. Drops `TST Dashboard.lnk` /
  `TST Dashboard (stop).lnk` (distinct names from the intraday shortcuts
  so both can live on the Desktop). Gitignored, per-PC. Run once per PC:
  `py dashboard_tst/setup_launcher.py`.
- `app/` — **the collaboration platform** (FastAPI app, Phase 1 scaffold).
  Separate application from the legacy operational `server.py` fork. Run
  from this folder: `uvicorn app.main:app --reload`. Layout:
  `config.py` (env-driven settings, `TST_*`), `db.py` (SQLAlchemy;
  `TST_DATABASE_URL` → SQLite dev / Postgres prod), `models.py`
  (`User`/`MATPLevel`/`Setup`/`Comment`), `security.py` (PBKDF2 hashing +
  session auth + `require_user`/`require_admin`), `main.py` (app factory,
  admin bootstrap, `/health`, `/status`, `/`), `models.py` also has
  `Feedback`; `routes/` (`auth`, `studies`, `feedback` [the development
  comment board], admin-only `admin` with the **control-plane-only** swing-bot
  stubs), `services/` (`black_scholes.py` — working pure-math option
  pricing/prob-ITM; `resources_bridge.py` — import seam to the shared
  `resources/`/`review/` library, Phase-tagged stubs), `templates/` +
  `static/`, `requirements.txt`, `.env.example`. Deps are per-PC
  (gitignored); the SQLite db (`*.db`) and `.env` are gitignored.
- `deploy/` — **deployment kit for Hermes (Cloudflare Tunnel model).**
  `run_app.ps1` (create venv [prefers py 3.12], install deps, launch uvicorn
  bound to 127.0.0.1), `setup_hermes_webapp_task.ps1` (the `TST-Dashboard-Web`
  service task), `update.ps1` (the refresh step: `git pull --ff-only` +
  restart the service — does NOT rely on `--reload`), `setup_hermes_autopull_task.ps1`
  (`TST-Dashboard-Autopull` — polls `update.ps1` every 5 min so pushes
  auto-deploy), `cloudflared-config.example.yml` (named-tunnel ingress
  template → `localhost:8000`), and `status_check.ps1` (poll `/status` +
  task state). All ASCII-only, parse-clean.
- `DEPLOY.md` — **deployment runbook.** Active **Path B** (Hamachi VPN,
  `http://<server-hamachi-ip>:8000`, password auth, no domain/TLS — the VPN
  tunnel is encrypted) step-by-step: pull → `.env` → first run → firewall
  lock to the `25.0.0.0/8` Hamachi subnet → service install → members join
  → update loop. Plus the future **Path A** (public + Google) upgrade notes.
- `alembic/` + `alembic.ini` — **DB migrations.** `env.py` is wired to the
  app's `Base` + `TST_DATABASE_URL` (`render_as_batch=True` for SQLite ALTERs);
  `versions/` holds the migration scripts (baseline = `d555dc88d20b`). The app
  runs `alembic upgrade head` on startup (with safe onboarding for legacy DBs —
  see `app/db.py::init_db`). To add a schema change: edit the models, then
  `alembic revision --autogenerate -m "..."` from `dashboard_tst/`; it
  auto-applies on next start. **Never** add a column model-only — always a
  migration (per the CLAUDE.md data-handling rule).
- `DESIGN.md` — **product blueprint** (DRAFT, pre-implementation). The
  agreed vision: `dashboard_tst` as TradeHunter's trend & swing product +
  members-only, internet-facing collaboration platform (Finviz → MATP/MBP
  quarterly → pattern study → collaborate on entry/SL/PT → Black-Scholes
  option win-rate → parquet backtest), driving its own trend-swing bot
  that journals/reviews/self-improves via the shared Layer 4/5 code.
  Captures the roof model, the swing-bot gating/clientId/state plan, the
  public/trusted security split (control plane vs execution plane), the
  reuse map, a phased roadmap, and the still-OPEN decisions. Read this
  before building anything here.

## URLs

- `http://localhost:8001/` — main dashboard
- `http://localhost:8001/snapshot` — JSON snapshot (state + health)

## Run

```
py dashboard_tst/server.py
```
or `dashboard_tst\start_dashboard.bat` (with restart supervisor).

## Status / TODO

This is a fresh fork. The trend & swing content (which panels, which
scanner, which setups) is being defined by the user. Likely first
additions:
- The `resources/trend_state.py` 4-state classifier (UPTREND / DOWNTREND
  / CONSOLIDATION / SIDEWAYS) surfaced as a sortable universe board.
- Swing-trade setup family tab(s) once swing strategies are taught
  (none coded yet — DITP/GUNS are intraday).

Intraday-specific UI inherited from the copy (gating drawer, bot
controls, intraday scanner setups) will be trimmed as the trend/swing
surface takes shape.

## Changelog

### 2026-05-31 — v2.35: per-Finviz-filter run schedule (agent runs due filters)

Each saved Finviz filter now has a **run schedule** — `off / daily / weekly / monthly / quarterly` — set by moderators on the `/finviz` page (a Schedule column with a dropdown + "next/last run" info). The interval lives in the dashboard; the **agent's poll cron just runs whatever's due**, so you don't manage per-filter crons on the agent.
- **Model:** `finviz_filters` gains `run_interval` / `last_run_at` / `next_run_at` (migration `d4e5f6a7b8c9`). `RUN_INTERVALS` maps interval→days.
- **API:** `GET /api/due-filters` returns active filters with `next_run_at <= now` (and interval ≠ off). The **finalizing `/api/matp` push advances** that filter's `last_run_at`/`next_run_at` from its interval — so completing a run (scheduled or manual) reschedules the next one. Setting an interval makes it due immediately (`next_run_at` cleared).
- **Route:** `POST /finviz/filters/{id}/interval`.
- Nous Hermes `matp` skill → v1.5.0: on each poll it also `GET /api/due-filters` and runs each due filter full-universe (`prune` + `final`), which advances the schedule.
- Verified: off→none due; set weekly→due now→run→advances +7d→not due; UI dropdown renders.

### 2026-05-31 — v2.34: pro band + current price + patterns + Trend column + individual-ticker group

- **Analyst band redesigned** (cleaner: taller `h-9` bar with a ring, low/high inside, MBP/MATP labels below) **+ a live current-price marker** (amber ▼ + line, positioned by % within the range). `_build_band` takes `current`; the route fetches the live close (cached) for the selected ticker.
- **Pattern recognition hooked in** (shared `resources.patterns` on live daily bars): the chart panel shows badges for **Trend (up/down/sideways)**, **Consolidation**, and **Bull flag** for the selected ticker. New `_ticker_analysis()` (soft-fail). 
- **Watchlist gains a Trend column** (Sym · Trend · Signal · MBP) via the new `ticker_grid` macro (uses the stored `MATPLevel.trend`).
- **Individual-ticker group**: ad-hoc ticker runs (no Finviz filter) now appear under an **"Individual tickers"** group in the watchlist (answers "how does a single-ticker run show up?" — previously they were invisible).
- Verified live: NVDA → current 211.14, trend/bull-flag badges; trend column, individual group, current marker all render.

### 2026-05-31 — v2.33: chart fills the screen + ETFs in search

- **Chart fits the screen.** The chart card now flex-fills the right panel (`chart_fill`): the TradingView header and the analyst band are fixed (`shrink-0`), and the **price-chart canvas takes the remaining height** (`lg:flex-1`, min-h fallback on mobile). No more fixed `vh` height that overflowed at the 130% scale; the watchlist column keeps its own internal scroll.
- **ETFs included** in the ticker search/autocomplete (`quoteType` now `EQUITY` *or* `ETF`; suggestions tagged `[ETF]`). Verified SPY/VOO show up.

### 2026-05-31 — v2.32: neon zone-only heatmap + fix Analyst-targets button overlap

- The band heatmap now **only colours the concentrated zone** (the densest contiguous run of buckets) — scattered outlier targets stay neutral — using **neon** colours (`hsl(h,100%,60%)`, blue→red, peak=red) with a CSS **glow** (`box-shadow`). Per-bin colour is `transparent` outside the zone. (`_build_band` computes the zone before colouring.)
- Fixed the **"Analyst targets" button overlap**: it's no longer absolutely positioned over the band — it sits in its own right-aligned row above the band (board + detail).
- The band already recomputes per ticker (each selection is a full reload with the new `sel`/`sel_band`).

### 2026-05-31 — v2.31: scale the whole UI ~30% bigger

Set `html { font-size: 130% }` in `base.html` — since Tailwind's sizes are rem-based, fonts, padding, widths and most spacing scale up ~30% together, so the whole app reads bigger. Added `lg:overflow-y-auto` to the right chart panel so the larger content scrolls within the panel instead of clipping under the no-scroll desktop layout.

### 2026-05-31 — v2.30: analyst band as a concentration heatmap; targets table removed

- **Band reworked into a thick heatmap bar** (`h-8`): each price bucket is coloured by how many analyst targets fall in it — **blue→red, red = most concentrated** (`_build_band` adds an HSL `color` per bin, hue 240→0). The **low/high labels now sit inside the bar**; MBP/MATP markers + labels above/below.
- **Removed the inline analyst-targets table** from the right panel; the **"Analyst targets"** button on the band still opens the full list in the pop-out modal.
- Verified: heatmap colours (densest bucket → red), low/high inside, table gone, modal button intact.

### 2026-05-31 — v2.29: top nav bar + ticker autocomplete; Feedback removed

- **Nav relocated to a top bar.** The left sidebar is gone; `base.html` is now a top header (logo + horizontal nav + user menu) over full-width content (no new panels added — the MATP watchlist/chart reclaim the freed width). Removed the mobile hamburger (nav fits the top bar).
- **Deleted "Feedback"** from the nav.
- **Ad-hoc ticker autocomplete** — the ticker box is now a typeahead over **US tickers + company names**: typing a symbol or a name fetches suggestions from `GET /matp/ticker-search` (Yahoo search, US-equities only) into a `<datalist>`; picking one fills the symbol. `run-ticker` validation now allows `-` (e.g. BRK-B). Verified live: "nvid"→NVDA, "apple"→AAPL.

### 2026-05-31 — v2.28: watchlist dropdown filters the shown tickers

The Run-panel **watchlist dropdown is now a view selector**: changing it filters the middle ticker list to that watchlist (navigates `?wl=<filter_id>`), auto-selects that watchlist's first ticker (chart updates), and the **Run** button runs the selected watchlist. The dropdown is visible to everyone (members can switch which watchlist they view); only mod/admin see the Run button. Board route takes `?wl`, computes `sel_wl` (default = first active filter) and `shown_watchlists`; the middle panel + mobile dropdown render only the selected watchlist. Verified switching Growth↔Value filters the list and auto-selects.

### 2026-05-31 — v2.27: TV link in the legend line + opaque chart background

- Moved the **"open TradingView chart →"** link out of the widget overlay into the **description/legend line** below the chart.
- Made the **chart background opaque** (`layout.background` `#0f172a`, panel `bg-slate-900`) so the page's grid-pattern background no longer bleeds through and clashes with the chart's own gridlines.

### 2026-05-31 — v2.26: connect the TradingView header, chart, and analyst band

The TradingView ticker header, the price chart, and the analyst consensus band are now **one connected panel** — a single rounded border with thin `border-b` dividers between the three sections (no gaps/separate boxes). `_band.html` is now "bare" (caller supplies the box); `_price_chart.html` takes an optional `chart_band` and renders the band as the panel's third section with the "Analyst targets" button. Board passes `chart_band=sel_band`; the detail page wraps the bare band in its own box. Verified the connected panel + detail page.

### 2026-05-31 — v2.25: fold MATP/MBP/calculated/earnings into the chart legend

Merged the separate "MATP … · MBP … · calculated … · earnings …" line into the **EMA legend** below the chart, so it reads as one description: `EMA20 · EMA50 · EMA200 · MATP <v> · MBP <v> · calculated <date> · earnings <date>`. `_price_chart.html` takes `chart_as_of` + `chart_earnings` (passed from board `sel` and detail `level`); the standalone calc line is removed.

### 2026-05-31 — v2.24: chart/analyst/watchlist polish

- Removed the **"Price vs MATP / MBP"** chart heading.
- Moved the **"Analyst targets"** button onto the **consensus band** (opens the targets modal); removed the redundant **"open full detail"** link/button (board + detail).
- The **TradingView widget** now has an **"open TradingView chart →"** link that opens the full TV chart for that ticker (`tradingview.com/chart/?symbol=<EXCH:SYM>`).
- **Watchlist is now a grid** — columns **Sym · Signal · MBP** (signal badge per ticker) with a small header row.
- Removed the per-watchlist **"Run MATP"** button from the rail (the Run panel above handles running).
- Verified all six on a throwaway DB.

### 2026-05-31 — v2.23: TradingView ticker-info widget above the chart

Added TradingView's **Symbol-Info** embed widget above the price chart (in `_price_chart.html`, so board + detail both get it) — shows the ticker's price/change/key stats that our chart can't. **Kept the lightweight chart** with MATP/MBP/EMA overlays (user choice: "keep ours + add TV info panel"), so nothing is lost. Symbol resolves with the exchange prefix when known (`NASDAQ:NVDA`), else the bare symbol; dark/transparent theme. `chart_exchange` passed from board (`sel.exchange`) and detail (`level.exchange`).

### 2026-05-31 — v2.22: Run panel — watchlist dropdown + ad-hoc ticker run + progress

A dedicated **Run panel** at the top of the MATP page (separate, full-width, visible on mobile):
- **Watchlist dropdown + Run** (moderators/admins) — pick an active Finviz filter and run it (`POST /matp/run-filter`).
- **Ad-hoc ticker + Run** (any approved member) — type a US ticker and run just that one (`POST /matp/run-ticker`, validates A–Z/`.`/≤6, dedup, redirects to the ticker). Members can run single tickers; full-filter runs stay mod/admin.
- **Live progress** (`#runsbox`, polls `/matp/runs`) moved here into its own panel (hidden when idle via `empty:hidden`); removed from the chart panel.
- Verified: dropdown shows for mod only; ticker form for all; member ticker run enqueues + dedups; invalid ticker rejected.

### 2026-05-31 — v2.21: Mobile-responsive shell + band guard fix

Made the app usable on a phone:
- **Left sidebar hidden < `lg`**, replaced by a **hamburger menu in the header** (logo + nav links + version). On desktop the sidebar is unchanged.
- The fixed **no-scroll single-screen** layout is now **`lg`-only**; on mobile the body/containers use `min-h-screen` and the page **scrolls naturally**. The MATP three-pane stacks vertically (watchlist dropdown → chart panel), the analyst table caps at `max-h-[60vh]` with its own scroll, and `main` is `overflow-y-auto lg:overflow-hidden`.
- **Bug fix:** the consensus zone guard used `is not none` on a possibly-*undefined* key — a ticker with **no post-earnings targets** crashed the band (board + detail). Now guarded with `band.n`. Verified the no-targets case renders.

### 2026-05-31 — v2.20: MATP page cleanup (band concentration, calc date, trimmed chrome)

Six requested tweaks to the MATP workspace:
1. **Band concentration** — dropped the histogram; the consensus is now a **shaded green zone on the bar** marking where the majority of analyst targets cluster (densest contiguous run of buckets), with the low/high/consensus labels kept.
2. Removed the **"MATP board"** heading and the **"Watchlists"** label (decluttered chrome).
3/4. Removed the **"Analyst summary"** heading; the **analyst targets table + "Open full detail →"** button now live together in the analyst (band) section.
5. Added a **"MATP … · MBP … · calculated <date> · earnings <date>"** line so you can see **when the MATP was computed** (`MATPLevel.as_of`).
6. Removed the **"Other"** (unfiled) group from the watchlist.

`_band.html` reworked (shared by board + detail); `_build_band` now returns the consensus zone as start/end percentages. Verified all six on a throwaway DB.

### 2026-05-31 — v2.19: "Ask the progress" — refresh button + agent narration

The runs panel gains a **↻ refresh** button (HTMX re-fetch of `/matp/runs` into `#runsbox`) so you can pull the latest status on demand instead of waiting for the 5s auto-poll, and it now shows the **agent's narration `note`** ("processing MSFT (12/61)") so you see exactly what it's doing. (The dashboard can't call the Linux agent directly — outbound-only — so progress stays pull-based: the agent reports via `/api/refresh-queue/{id}/status`, the panel displays it.) Nous Hermes `matp` skill v1.4.1 now posts a `note` alongside each progress update.

### 2026-05-31 — v2.18: Run observability (elapsed + stale warning)

The runs panel now shows **how long** each run has been queued/running (`queued 12m` / `running 5m`) and flags a **stale** run in red with guidance: a `pending` request sitting >15m warns "the research agent may not be polling (check its `matp` cron)"; a `running` request with no progress >8m warns it may be stuck. `/matp/runs` computes elapsed minutes + the stale flag server-side. This makes "is it working?" answerable at a glance instead of guessing. (Paired with Nous Hermes `matp` skill v1.4.1 — reads creds from `~/.hermes/.env` explicitly so it stops prompting.)

### 2026-05-31 — v2.17: MATP three-panel workspace (menu · watchlist · chart)

Reworked the MATP page into the working layout: **side menu (left, base nav) · watchlist (middle) · chart panel (right)**, all in one non-scrolling screen.
- **Middle = watchlist** (`lg:w-72`), groups expanded, scrolls internally, highlights the selected ticker; Dropped names tuck into a collapsible at the bottom. The big multi-column board table is **removed** (its per-ticker detail now lives in the right panel).
- **Right = chart panel**, stacked top→bottom and fixed (no page scroll): **MATP run** (live progress, pinned on top) → **price chart** (EMAs + MATP/MBP) → **analyst low–high band with the consensus histogram** → **analyst summary list**, with a **"Full detail →"** button that opens the analyst-targets pop-out modal.
- **First ticker auto-selected** on load (`?symbol=` overrides) so a chart shows immediately. Chart height parameterized (`chart_height_class`) so the composite fits; band factored into shared `_band.html` (used by board + detail).
- Verified: default selection renders chart/band/summary; run pinned on top; watchlist highlight; modal button; no board table; no-scroll shell; explicit `?symbol` works.

### 2026-05-31 — v2.16: Single-screen app-shell (no page scroll) + consensus histogram on the band

- **No-scroll, single-screen layout.** Converted the shell to a fixed-height app layout: `base.html` body is `h-screen overflow-hidden`, the content column is `overflow-hidden`, and `main` is height-controlled via a new `main_class` block (default still scrolls; other pages unchanged). The MATP page is now `h-full flex flex-col` with **panes that scroll internally** — the watchlist rail, the board table, and the chart each scroll on their own, so the **main frame never scrolls**. Trimmed the header/intro to a compact one-liner to save vertical space. Watchlist rail kept (collapsed = tickers-only, hover-expand overlay, pin, click swaps chart) and made full-height.
- **Consensus concentration on the analyst band.** `_build_band` now bins the post-earnings target prices into a **histogram** drawn above the low→high band — taller/green bars = where targets cluster. The densest bucket is surfaced as a **consensus N–M (k/total)** label, so you can see where analysts actually agree vs the full range.
- Verified: no-scroll shell classes + internal-scroll panes on board/detail; band histogram + consensus label render from the target distribution.

### 2026-05-31 — v2.15: Analyst targets open in a pop-out modal

Analyst targets now open in a **pop-out modal** instead of rendering inline on the page. An **"Analyst targets"** button sits in the chart header (so it's available on both the MATP board when a ticker is selected, and the detail page); clicking it HTMX-loads the targets into a centered overlay (close via ×, click-outside, or Esc).
- New `GET /matp/{symbol}/targets` returns the `_targets_modal.html` fragment (brokerage / issued / target / post-vs-pre); `_ticker_targets()` helper shared with the detail route. Reusable modal shell `_at_modal.html` (included once per page).
- The detail page's inline "Analyst targets" table is **removed** (replaced by the modal button); the "Run archive" section stays inline.
- Verified: button + modal shell on board and detail; fragment renders post/pre correctly; inline evidence table gone.

### 2026-05-31 — v2.14: MATP/MBP always visible · live table population · collapsible watchlist rail

Three things:
- **Chart shows MATP + MBP by default.** Added an `autoscaleInfoProvider` to the candlestick series so the visible price range always includes MATP and MBP — they're no longer clipped off-screen when far from the current price.
- **Table populates mid-run.** `/api/matp` gains a `final` flag: the agent now pushes processed tickers incrementally with `final:false` (upsert only, no prune, **not archived**) so they appear on the board as they're computed, then sends ONE closing push (`final:true` + `prune:true`) that prunes fallen-out names and is the archived run file. While a queue run is active the board auto-refreshes (20s) so the rows appear without a manual reload. Nous Hermes `matp` skill → v1.4.0 (incremental + closing push).
- **Collapsible watchlist rail.** The left watchlist is now a narrow rail showing **tickers only** when retracted; it **expands on hover** (as an overlay, so the board doesn't reflow) and can be **pinned** open (persisted in `localStorage`). Clicking a ticker swaps the chart (`?symbol=`). Mobile keeps the "Watchlists ▾" dropdown.
- Verified: incremental push writes no archive, finalizing push archives one file; rail/pin/hover markup + autoscale + active-run auto-refresh all present.

### 2026-05-31 — v2.13: MATP run archive (one JSON file per run) + per-ticker view

Every `/api/matp` run is now archived as **one JSON file per run** in the MATP folder (`<data_root>/MATP/run_<UTC>[_f<filter_id>].json`) — on Hermes set `TST_MATP_DIR=C:\HermesSync\MarketData\MATP` (Resilio-synced); defaults to `<TradeHunter>/data/MATP`. The file holds the run's full raw extraction (every ticker + its analyst targets/distribution). This is the durable "cream" archive; the live UI still reads `tst.db` (Postgres-portable), so the data-handling rule is intact.
- New `app/services/matp_archive.py` (`save_run`, `runs_for_symbol`); `config.matp_dir` (`TST_MATP_DIR`). Archiving is soft-fail — it never breaks ingest (`/api/matp` returns `archived: <filename>`).
- The dashboard writes the file (only Hermes has the MarketData path; the Linux agent just POSTs).
- **Detail page** gains a **"Run archive"** section: each archived run that included the ticker, expandable to the analyst summary extracted *that day* (brokerage / issued / target / post-vs-pre), so you can see exactly what each run captured.
- Verified: ingest writes the file (`run_…_f1.json`) with full payload; detail renders the run archive with correct post/pre status.

### 2026-05-31 — v2.12: EMA20/50/200 overlays on the price chart

Added three EMA overlays to the price chart: **EMA20 (red)**, **EMA50 (green)**, **EMA200 (purple)**, all at lineWidth 2. Computed client-side from the daily closes (standard EMA, SMA-seeded) and drawn as lightweight-charts line series; a small legend identifies each line alongside MATP/MBP. Bumped the live fetch window `1y → 2y` so EMA200 is meaningful (≈300 points instead of ≈50). Verified: NVDA 2y → 501 bars; all three EMA series + legend render.

### 2026-05-31 — v2.11: MATP three-pane layout — expanded watchlist, full-width, chart on the right

Reworked the MATP page into a left-aligned, full-width three-pane layout: **watchlist (left, all groups expanded)** · **board table (middle)** · **price chart (right, sticky, ~72vh)** when a ticker is selected. Previously the chart stacked above the table in a centered, capped column.
- `base.html` `main` width is now overridable via a `main_wrap` block; MATP overrides it to full-width/left-aligned (other pages keep the centered `max-w-5xl`).
- Watchlist `<details>` now default to **open** (all groups expanded), not just the first.
- Middle board column is `lg:flex-1`; the selected chart sits in a `lg:flex-1 lg:sticky` right column, so board + chart split the space beside the watchlist. On mobile everything stacks (watchlist dropdown → board → chart).
- Verified: MATP full-width vs admin centered; watchlist expanded; no chart unselected; chart in sticky right pane when `?symbol=` set.

### 2026-05-31 — v2.10: Build version shown at the sidebar logo

The hand-maintained build version now shows under the TradeHunter logo in the sidebar ("trade collaboration · v2.10") on every page — quick at-a-glance confirmation of what's deployed. Implemented by exposing `__version__` as a Jinja **global** (`version`) on every route module's templates env, set in one place in `main.py` (no per-route plumbing). `base.html` reads `{{ version }}`. Verified across /matp, /finviz, /feedback, /admin.

### 2026-05-31 — v2.9: Price chart inline on the MATP page (click a watchlist ticker)

The price chart (MATP/MBP candlestick) now shows **on the MATP board itself**, not only the detail page. Clicking a ticker in the watchlist rail goes to `/matp?symbol=SYM#chart`, and the board renders that ticker's chart inline at the top of the main column (with an "open full detail →" link). The chart markup+script was factored into a shared partial **`_price_chart.html`** (params `chart_symbol`/`chart_matp`/`chart_mbp`), now included by both the board and the detail page — single source, no duplication.
- `matp_home` accepts `?symbol=` and passes the matched level as `sel`.
- Rail ticker links (watchlist + Other) point at `?symbol=…#chart`; the main board table's ticker links still go to the full detail page.
- Verified: board without `?symbol` has no chart; `?symbol=NVDA` renders it (lib + MATP injected + detail link + fetch URL); detail page still renders via the partial.

### 2026-05-31 — v2.8: Watchlist second sidebar (responsive — collapses on mobile)

Added a **watchlist rail** to the MATP page: each active Finviz watchlist, expandable to its tickers (click → detail), with a per-watchlist **↻ Run MATP** button (mods/admins) and MBP shown per ticker. Tickers with no source filter fall under an **"Other"** group.
- **Responsive (the mobile concern):** on `lg+` it's a sticky **second sidebar** (`lg:w-64`) beside the board; on phones it **collapses to a "Watchlists ▾" dropdown** above the board (`<details>`, `lg:hidden` / `hidden lg:block` swap) so three columns never squeeze a narrow screen. Same rail markup via a `watchlist_rail()` macro, rendered in both containers.
- The standalone v2.1 filter selector is **replaced** by the rail's per-watchlist Run buttons (route `POST /matp/run-filter` unchanged; `_run_filter.html` now unused).
- Route groups active tickers by `filter_id` into `watchlists` + `unfiled`.
- Verified: desktop aside + mobile dropdown both render, grouping correct, Run forms + "Other" group present, board table intact.

### 2026-05-31 — v2.7: Run-MATP selector lives on the MATP page only

Removed the "Run MATP" filter selector from the `/admin` console (reverted the admin route's filter context); it now appears only on the MATP board, which is its natural home. Admin stays focused on user management + bot control.

### 2026-05-31 — v2.6: Live MATP-run progress bar + visible dedup

The MATP board now shows a live **"Active MATP runs"** panel (HTMX-polled every 5s, no full reload) for every pending/running request: a **progress bar** + **who triggered it** + when. The bar is **determinate** (done/total tickers) once the agent reports counts, **indeterminate** (animated) while queued/before counts. Replaces the old static "N queued" banner.
- **Model:** `matp_refresh_requests` gains `progress_done` / `progress_total` (migration `c3d4e5f6a7b8`).
- **API:** `POST /api/refresh-queue/{id}/status` accepts optional `progress_done` / `progress_total`; on `done` it snaps the bar to 100%.
- **New fragment endpoint:** `GET /matp/runs` → `_runs_panel.html` (defined before `/{symbol}` so it doesn't collide).
- **Dedup made visible** (answers "avoid another run"): enqueue already blocks a second open request for the same filter/ticker; the panel now shows it's already in flight (status + triggerer), and the selector still labels it "— queued". Verified: two clicks → one run.
- Nous Hermes `matp` skill → v1.3.0: queue-poll mode now posts `progress_total` on start and `progress_done` every ~5 tickers, driving the bar.
- Verified: panel renders queued (indeterminate) → running 30/61 (~49% determinate) → done (drops off, snapped to 61/61); dedup holds.

### 2026-05-31 — v2.5: Price chart fetches live (Yahoo) instead of parquet

Switched the price chart's data source from the stored parquet to a **live Yahoo Finance fetch** — so it works for any symbol immediately, with no dependency on bars being seeded / Resilio-synced / `data_root`-configured on the host. New `app/services/prices.py::fetch_daily_ohlc()` calls the Yahoo v8 chart API via `httpx` (already a dep — no API key, query1/query2 failover, ~10-min in-process TTL cache, browser UA), returns lightweight-charts shape, and yields `[]` on any failure (chart shows "Couldn't load live price data — try again shortly"). `GET /matp/{symbol}/prices` now calls this; the parquet path (`resources_bridge.daily_bars`) and the `pyarrow` requirement added in v2.3 are **reverted**. Verified live: NVDA → 251 daily bars, bogus symbol → [].

### 2026-05-31 — v2.4: Layout — left sidebar nav + user dropdown menu

Restructured the chrome in `base.html`. Nav links (MATP / Studies / Finviz / Admin) moved from the top bar into a **left sidebar** (`<aside>`, sticky, with active-link highlighting via `request.url.path`). The **name + Logout** moved into an **icon-triggered dropdown menu** in the top-right (avatar/initial + chevron → menu with name, role, email, Logout) — JS-free using `<details>`/`<summary>` (marker hidden via CSS). Logged-out pages (login) render full-width with **no sidebar**; the `content` block is defined once and referenced via `self.content()` so there's no duplicate-block error. Verified both states render correctly.

### 2026-05-31 — v2.3: Interactive price chart with MATP/MBP level lines

The in-app version of the MATP Pine indicator. On a ticker's detail page, a **"Price vs MATP / MBP"** candlestick chart now renders with **MATP** (light) and **MBP** (green) drawn as horizontal price lines — so you see where price sits vs the median target and the max-buy line. Flow: select a watchlist → click a ticker → chart.
- **Tech:** TradingView's free **lightweight-charts** (v4.2.0, CDN) + new endpoint `GET /matp/{symbol}/prices` (session-auth) returning daily OHLC in chart shape. `resources_bridge.daily_bars()` reads the **shared parquet** store (`resources.bars_store`, `timeframe="daily"`, last ~400 bars) — deterministic, self-contained, no external calls.
- **Graceful degradation:** if a symbol has no parquet (or pyarrow/data_root isn't set up), the endpoint returns `bars:[]` and the chart shows "No daily price bars yet" instead of erroring.
- `pyarrow` added to `app/requirements.txt` (the bars reader needs it). **Deploy dependency:** the chart only draws real candles where the daily parquet is present on Hermes (Resilio `MarketData` + `config.json` `data_root`); otherwise it shows the empty state until price history is synced.
- Verified: shape conversion (stubbed bars), endpoint serialization, and the detail page injecting the chart container + lib + correct MATP/MBP price-line values + fetch URL.

### 2026-05-31 — v2.2: MATP board shows Last earnings column

Re-added a **Last earnings** column to the MATP board (it was dropped in the v1.9 redesign; the data — `MATPLevel.last_earnings_date` — was always stored, just not surfaced). Added to the shared `row()` macro (after `n`) and both table headers (active + dropped), so a collaborator sees the post-earnings reference date that the MATP is computed against without opening the detail page.

### 2026-05-31 — v2.1: Finviz-filter run selector (MATP board + admin console)

Replaced the per-filter refresh buttons with a **filter selector**: a dropdown of active Finviz filters + a "↻ Run" button, available on **both** the MATP board and the `/admin` console (admins run from there too). Scales cleanly past one filter and reads as an intentional "run this screen" action.
- New shared partial `templates/_run_filter.html` (select + Run + queued indicator), included on `matp.html` (`run_next=/matp`) and `admin.html` (`run_next=/admin`).
- New route `POST /matp/run-filter` (`require_moderator`, Form `filter_id` + `next`): only **active** filters are runnable (inactive = no-op), enqueues via the existing dedup path, redirects to the internal `next`. Path chosen to avoid colliding with `/{symbol}/refresh`.
- `admin.py` now passes `active_filters` + `open_filter_ids`. Gating unchanged — **moderators + admins only** (per decision); members see results but no control.
- Verified: selector renders for mod/admin and is hidden from members; admin run → 303→/admin; member run → 403; dedup holds across users; inactive filter not enqueued.

### 2026-05-31 — v2.0: Collaborator-triggered ad-hoc MATP refresh (request queue)

Moderators/admins can now trigger a MATP refresh from the page — without breaking the LLM-free / outbound-only-agent separation. TradeHunter can't fetch and can't reach the agent, so a click **enqueues** rather than fetches; the agent polls and drains the queue (near-real-time, ~10 min, not synchronous).
- **New model `matp_refresh_requests`** (migration `b2c3d4e5f6a7`): `scope` (`ticker`/`filter`), `symbol`, `filter_id`, `status` (pending/running/done/failed), `note`, `requested_by`, `created_at`/`claimed_at`/`completed_at`.
- **UI (moderators/admins only):** detail page gets a "↻ Request refresh" button + a status banner (queued / in progress / completed at / failed: reason); board gets per-active-filter "↻ Refresh a whole filter" buttons, an "N refresh requests queued" banner, and a ↻ badge on rows with an open request. Enqueue **de-dupes** — an identical open request won't double-up (button flips to "queued").
- **API (agent, X-API-Key):** `GET /api/refresh-queue` (pending requests; filter scope includes `filter_url`/`filter_description`), `POST /api/refresh-queue/{id}/status` (`running`/`done`/`failed` + optional `note`, stamps claimed/completed).
- **Routes (session, moderator-gated):** `POST /matp/{symbol}/refresh`, `POST /matp/filter/{id}/refresh`.
- Nous Hermes `matp` skill → v1.2.0: documents the two run modes (scheduled full vs **queue poll**), the queue endpoints, the ticker-scope = no-prune / filter-scope = prune rule, and a `*/10 * * * *` poll cron.
- Verified end-to-end on a throwaway DB (all 3 migrations apply): member enqueue → 403, moderator ticker+filter enqueue → 303, de-dup holds (2 rows, not 3), agent queue GET + running→done transitions, board + detail render the controls/banners.

### 2026-05-31 — v1.9: MATP board redesign — Finviz-drift tracking + bounce signal

Reworked the MATP pages around the question the method actually answers ("is this a buy now, and how much room is left?") instead of a flat data dump.
- **Universe-drift tracking.** The Finviz screen is dynamic — names fall out / qualify each run. Added to `matp_levels`: `status` (`active`/`dropped`), `filter_id` (FK → `finviz_filters`), `last_seen_at`. A ticker that leaves the screen is marked **dropped** (its MATP history is *retained*, never deleted). `/api/matp` gained `filter_id` + `prune`: a full-universe run for one filter marks same-filter tickers absent from the batch as dropped (returns `dropped` count). Per-ticker ad-hoc runs omit both (no pruning).
- **Actionable bounce signal** on `matp_levels`: `signal` (HOT/WARM/WATCHING) + `signal_entry`/`signal_stop`/`signal_target`(= MATP)/`signal_rr`, mirroring `resources/MATP`'s `daily_bounce_alert.py`. Ingest only touches these when a payload includes a signal, so a MATP-only run never wipes a signal set by the separate daily bounce job (and vice-versa). Also added `n_targets` to the level (board low-confidence ⚠ when n≤2).
- **Board (`/matp`)**: columns Ticker · MATP · MBP(max buy) · Trend · Signal · n · Filter · Updated; **active sorted HOT→WARM→WATCHING→rest**; dropped names tucked behind a collapsible "Dropped from filter (N)" section.
- **Detail (`/matp/{symbol}`)**: added a **levels band** (MBP + MATP markers along the analyst low→high range) and a **Bounce setup** card (entry/stop/target/R:R) above the existing history chart + analyst-evidence table.
- Migration `a1b2c3d4e5f6_matp_drift_and_signal` (batch mode for SQLite). Nous Hermes `matp` skill bumped to v1.1.0 — pushes **per-filter** with `filter_id`+`prune`, optional `trend`; bounce `signal*` declared out of scope (price-bar work). Verified end-to-end on a throwaway DB: both migrations apply, two-run drift (AAPL dropped, NVDA history appended, targets de-duped), board + detail render with signal/band/dropped section.

### 2026-05-30 — v1.8: Alembic migrations (DB schema is now upgrade-safe)

Closed the `create_all` ALTER gap (which 500'd when a new column was added to an existing table). Added **Alembic** wired to the app's `Base` + `TST_DATABASE_URL` (so migrations target SQLite dev / Postgres prod identically), with `render_as_batch=True` for SQLite ALTERs. `alembic/versions/d555dc88d20b_baseline_schema.py` is the baseline (all current tables). `db.init_db()` now runs migrations on startup with **safe onboarding** for all three states: fresh DB → run all migrations; already-managed DB → apply pending; **legacy `create_all` DB (Hermes) → add any missing tables via `create_all`, then `stamp head`** so it becomes migration-managed without losing data. Verified on a fresh DB (8 tables + `alembic_version` created) AND a simulated Hermes DB (new MATP tables added, existing users/filters preserved, stamped). Going forward, every schema change is a tiny reversible migration — satisfies the "upgrade to hosted Postgres without a rewrite" rule. **Workflow:** after editing models, run `alembic revision --autogenerate -m "..."` from `dashboard_tst/`; the new migration auto-applies on the next app start / deploy.

### 2026-05-30 — v1.7: MATP evidence (A+B+C) — distribution + per-analyst targets

Extended the MATP data model from output-only to the full three layers (user decision):
- **B — distribution summary** on each `matp_history` row: `target_high`/`target_low`/`target_mean` (the spread of analyst disagreement around the median). Shown as a Range column on the detail history table.
- **C — `matp_targets`** (new model): every individual analyst target (`brokerage`, `target_price`, `target_date`), unique on `(symbol, brokerage, target_date, target_price)` so re-pushing the same list each run inserts nothing new. `included` (post-earnings?) is **computed on display** from `target_date` vs the current earnings date, so it never goes stale.
- `/api/matp` payload extended (`TargetIn` + distribution fields); ingest returns `targets_added`. `/matp/{symbol}` detail now shows the **Analyst targets evidence table** with post-earnings ✅ / pre-earnings (dropped) status. Nous Hermes `matp` skill Stage 4-5 updated to compute the distribution + push the full target list.
- Verified on a clean DB: 3 targets stored, re-push de-dupes to 0, detail renders evidence + status. **Note:** this exposed the `create_all` ALTER gap (a stale local table lacked the new columns) — reinforces the Alembic need for future column adds; new tables on Hermes create fresh.

### 2026-05-30 — v1.6: MATP hookup — machine API + history + board + chart

Wired the full MATP data flow with **no LLM in TradeHunter**. The faithful, web-research-heavy computation runs on the **Nous Hermes agent** (Linux, DeepSeek + browser; skill at `nous_hermes/skills/markets/matp/`); TradeHunter receives, stores **with history**, and displays.

- **Machine API** (`routes/api.py`, auth via shared `TST_INGEST_API_KEY` / `X-API-Key`, constant-time; 503 if unset, 401 on bad key): `GET /api/filters` (active Finviz filter URLs the agent screens) + `POST /api/matp` (push computed levels). No user session.
- **History kept.** `POST /api/matp` upserts the current snapshot (`MATPLevel`, one row/symbol) AND appends a **de-duped** `MATPHistory` row (new model) — only when the MATP value changes — so we track how each ticker's analyst target moved over time. Carries `n_targets` + `source` (provenance).
- **Board built** (`/matp` was a placeholder): lists current MATP/MBP/trend/last-earnings/updated per ticker; each ticker links to its detail.
- **Per-ticker history view** (`/matp/{symbol}`): a dependency-free **server-rendered SVG line chart** of MATP over time + the full history table.
- `config.py` gains `ingest_api_key`; `.env.example` documents `TST_INGEST_API_KEY`. Verified live: 401 without key; 3 distinct values → 3 de-duped history rows; board + detail chart render (200).

**Still open:** per-ticker on-demand refresh trigger from the UI; a `tst.db` backup (the platform's accumulated data — users/filters/MATP/history/setups — is a single un-backed-up SQLite file on Hermes).

### 2026-05-30 — v1.4: Finviz list shows decoded criteria, URL hidden in link

The Finviz filters list showed the raw screener URL (meaningless on screen). Replaced the "URL" column with "Filter criteria": `routes/finviz.parse_finviz_criteria(url)` decodes the screener's `f=` parameter (tokens like `cap_largeover`, `sh_avgvol_o500`, `ta_sma200_pa`) into readable chips (Market Cap, Avg Volume > 500, SMA200, P/E < 30, etc.), with the URL **embedded in the link** behind the chips (opens Finviz in a new tab). URLs without an `f=` fall back to "Open in Finviz ↗". Decoder covers the common category prefixes + over/under/range values; unknown tokens render as-is.

### 2026-05-30 — v1.3: harden update.ps1 restart (no more deploy hangs)

`deploy/update.ps1` could hang on deploy: it called `Stop-ScheduledTask` **before** freeing port 8000, and `Stop-ScheduledTask` can block waiting on the orphaned uvicorn child — so it never reached the port-free step. Reordered the restart to **free the port first** (kill the listener on 8000 directly, which also lets the task wrapper exit), then a **non-blocking** stop via `schtasks /End`, then start. Deploy is now reliably one command (`update.ps1`) with no manual port-killing.

### 2026-05-30 — v1.2: straight-to-login root + hand-maintained version

- **Root goes straight to the login page.** `GET /` now redirects: unauthenticated → `/login` (no marketing/hero landing), approved members → `/matp`, pending/disabled → the awaiting-approval page.
- **Build label is now a hand-maintained version.** `app/__init__.py __version__` (currently **`1.2`**) shows as `build v1.2` in the login footer — easier to track than a git SHA. The exact commit SHA still lives in `/status` (`build` field) for precise deploy verification. Convention: bump `__version__` on each meaningful change and note it here.

### 2026-05-30 — Build indicator + callback error-surfacing

Added a running-build indicator so you can tell at a glance whether a machine (esp. Hermes) is on the latest code after a deploy: `app/_build.py` reads the git commit SHA from `.git` at process start (no `git` on PATH needed); shown as `build <sha>` in the login-page footer and in `/status`. Also made the Google OAuth callback **surface its real error**: the handler now wraps the whole flow, logs the exception (`log.exception`), and — when `TST_DEBUG=1` — returns the traceback on the `/auth/callback` page instead of silently redirecting (was hiding the cause of login failures). Diagnostic aid for the Hermes login issue (root cause was a dead IPv6 route to `oauth2.googleapis.com` — see CLAUDE.md/Hermes notes).

### 2026-05-30 — MATP landing page + post-login routing

Added a **MATP** page (`/matp`, nav link) — the Median Analyst Target Price board, currently a themed "Under construction" placeholder (the real MATP/MBP board, driven by active Finviz filters, comes later). Post-login routing now matches the intended flow: **approved** users (Google callback or password login) redirect to **`/matp`**; **pending** users go to `/` and see the awaiting-approval message (`require_user` also blocks pending from `/matp`). New files: `routes/matp.py`, `templates/matp.html`. Verified: approved login → `Location: /matp`, page renders.

### 2026-05-30 — update.ps1: free port 8000 on restart (fix orphaned uvicorn)

Recurring deploy issue: `Stop-ScheduledTask` terminates the `run_app.ps1` wrapper but **orphans the child `uvicorn` process still holding port 8000**, so the fresh start can't bind and the stale/hung process keeps serving old code (symptoms: "site froze", "new pages don't appear after git pull", `/status` uptime never resets). Fixed `update.ps1` to explicitly kill whatever listens on the port (new `-Port`, default 8000) between stop and start, so every pull reliably runs the new code. Manual one-off recovery is the same kill-port-then-start sequence.

### 2026-05-30 — Finviz tab: saved-filter manager (no scan)

Added a **Finviz** tab (`/finviz`, nav link) to **manage a list of saved screener filters** — this page only curates the list, it does not run any scan (scanning is a later step that consumes the active filters). New `FinvizFilter` model (id, description, url, is_active, created_by, created_at). **Moderators+** add a filter (description + URL + Active checkbox), toggle Active/Inactive, and delete; **all approved members** view the list read-only (URL links out to Finviz in a new tab). New files: `routes/finviz.py` (CRUD), `templates/finviz.html`. Verified end-to-end in a throwaway run: add active + inactive → list renders with statuses → toggle works; member add → 403, member view → 200. (The `resources_bridge.screen_universe()` + `finviz_screener.py` Accept-header fix from earlier remain, ready for the future scan step.)

### 2026-05-30 — New sign-ins: pending + admin approval (role defaults to Member)

A first-time Google sign-in lands **`pending` with role `member`** and must be **approved by an admin** in the console before getting access; the admin can also change the role afterward. Added `TST_AUTO_APPROVE` (default **`0`** = approval required); set `=1` to auto-approve new users as active members instead. `auth_callback` sets `status=pending` for new users unless auto-approve is on (the bootstrap admin email always becomes an approved admin). The who-can-sign-in gate remains Google's test-user list / `TST_ALLOWED_EMAIL_DOMAINS`; this adds the per-user approval step on top. Verified: default `auto_approve=False`.

### 2026-05-30 — Three user roles: Administrator / Moderator / Member

Extended the two-role model (member/admin) to three tiers. `models.py` gains role constants (`ROLE_ADMIN`/`ROLE_MODERATOR`/`ROLE_MEMBER`), labels, and `is_moderator`/`can_moderate`/`role_label` helpers; `security.py` gains a `require_moderator` dependency (admins + moderators). Permissions: **Administrator** = full (user management, roles, bot control); **Moderator** = member powers + content moderation (cannot manage users); **Member** = post/collaborate. Made the moderator tier tangible now by gating **feedback deletion** behind `require_moderator` (member can post; moderator/admin can remove any — with a `×` button shown only to moderators). Admin console role dropdowns (member-row + create-user) now list all three; `set_role` validates against the allowed set and blocks an admin from changing their own role (lockout guard). Verified in a throwaway run: all three options render, moderator delete → 303, member delete → 403.

### 2026-05-30 — Admin console: user counts, Joined column, re-enable

Built out the admin console (`/admin`, admin-only) for managing users. Added a counts strip (Total / Pending / Active / Disabled), a **Joined** date column in the members table, and — fixing a real gap — an **Enable** action so a disabled user can be brought back (previously `disable` was one-way in the UI; the Enable button reuses the approve endpoint, which sets status back to APPROVED). The pending-approval queue (google mode) and role/disable controls are unchanged. Verified end-to-end in a throwaway password-mode run: login → create users → `/admin` 200 with all new elements → disable shows Enable + "disabled" status → re-enable clears it.

### 2026-05-30 — Branding finalize, login redesign, /auth/google flow, faster restarts

- **Branding:** dropped the placeholder "TST" and "trend & swing" everywhere — browser-tab titles now "… · TradeHunter", header/landing taglines now "trade collaboration" / "Collaborative trade research" (the platform isn't limited to one methodology).
- **Login redesign:** `login.html` is now a polished centered card (logo, heading, labeled inputs, styled error) for both password and Google modes.
- **`/auth/google` flow:** split "render the login page" from "start Google OAuth". `GET /login` now always renders the card (in google mode it shows a *Sign in with Google* button → `GET /auth/google` → OAuth redirect), instead of auto-redirecting. Better UX and lets the login card render in google mode.
- **`run_app.ps1` faster restarts:** install deps only when the venv is new or `requirements.txt` changed (SHA-256 hash check in `.venv\.reqhash`), instead of reinstalling on every launch. A cold reinstall at boot was delaying uvicorn by minutes; now the app comes back in seconds after a reboot/restart. Also dropped the cosmetic `pip install --upgrade pip` step.

### 2026-05-30 — Branding: TradeHunter logo + themed background

Added `app/static/logo.svg` — a vector mark (emerald crosshair + rising trend arrow = "hunt the trade"), wired into the header (mark + "TradeHunter · trend & swing" wordmark), the landing-page hero, and the favicon. Rebranded the site from the placeholder "TST" to **TradeHunter** throughout. Added a site-wide themed background in `base.html` (a `<style>` block): an emerald radial glow top-center + a faint cyan glow, a subtle chart-style grid, and a vertical depth gradient; plus a sticky translucent blurred header. (A candlestick-chart graphic overlay was trialed and reverted per user preference — kept the glow/grid only.) Pure CSS/SVG, no images or extra requests.

### 2026-05-30 — Access via Cloudflare Tunnel (public URL); auto-deploy loop

Collaborators won't install a VPN client, so the access model changed from Hamachi to a **public URL via Cloudflare Tunnel** on Hermes. `cloudflared` runs alongside uvicorn and dials out to Cloudflare (no inbound ports, no router changes), giving a free auto-HTTPS URL that proxies to `localhost:8000`. Auth stays password-mode; `TST_HTTPS_ONLY=1`. Added the auto-deploy loop: `deploy/update.ps1` (`git pull --ff-only` + restart the service — explicit restart, since `--reload` proved unreliable on synced drives during dev) and `deploy/setup_hermes_autopull_task.ps1` (polls every 5 min; webhook can't reach a tunnelled private box). Added `deploy/cloudflared-config.example.yml`. `run_app.ps1` now prefers `py -3.12`; the web-app task binds `127.0.0.1` (cloudflared reaches it locally — the app isn't directly exposed). `DEPLOY.md` rewritten around the Hermes + Cloudflare Tunnel runbook; DESIGN.md networking decision updated; root `.gitignore` ignores per-PC `.claude/launch.json`. All deploy scripts parse-clean.

### 2026-05-30 — Live preview verified + feedback board + two blocking-bug fixes

Stood up a real local preview (uvicorn on a 3.12 venv outside Dropbox) and verified the app end-to-end — which caught two bugs that compile checks missed: (1) the installed Starlette requires `TemplateResponse(request, name, context)` — the old `(name, context)` calls 500'd every page; fixed across all routes; (2) the app never loaded `app/.env` (config read `os.environ` but nothing populated it) — added `python-dotenv` loading (a real deployment fix). Then built the **Feedback board** (`/feedback`, `Feedback` model + route + template + nav link): any approved member posts a comment (optional topic + body), shown newest-first with author + UTC timestamp — the "collaborators comment on the development as it goes" surface. Verified live: login → post → thread renders. Also confirmed `--reload` does NOT fire on the Dropbox drive (informed the deploy loop's pull-and-restart design).

### 2026-05-30 — Status monitoring: `/status` endpoint + `status_check.ps1`

Added a way to watch the deployment "along the way." New unauthenticated `GET /status` returns non-sensitive operational state — `status`, `version`, `auth_mode`, `db_ok` (a `SELECT 1` connectivity probe), `uptime_seconds` (monotonic since process start). New `deploy/status_check.ps1` polls `/status` and the `TST-Dashboard-Web` scheduled-task state, printing `[UP]/[DOWN]` + a `healthy/problem` result (exit 0/1). Runnable on the server (localhost) or from the laptop when both machines share the LAN or the Hamachi VPN (`-Target http://<server-hamachi-ip>:8000`) — which also lets Claude check status on demand from the laptop once the laptop joins the VPN. `main.py` byte-compiles clean; the script parses clean.

### 2026-05-30 — Path B: VPN deploy + mode-switchable auth (password active)

User chose **Path B** — host on the server, members reach it over a **Hamachi VPN** (`http://<server-hamachi-ip>:8000`), no public internet / domain / TLS (the VPN tunnel is already encrypted; the VPN membership is the access gate). This collided with the prior Google-only auth (Google rejects bare/private IPs as OAuth redirect URIs), so auth is now **mode-switchable via `TST_AUTH_MODE`**: `password` (active — admin creates accounts, PBKDF2-hashed, approved on creation) or `google` (Path A, fully built and parked for when a domain+TLS exist). Changes: `config.py` adds `auth_mode` + re-adds `admin_password`; `models.User` re-adds nullable `password_hash` alongside `google_sub`; `security.py` re-adds PBKDF2 hash/verify; `routes/auth.py` branches on mode (Authlib lazy-imported so password mode needs no OAuth dep); `routes/admin.py` shows a create-user form (password) or the pending queue (google); `main.py` seeds the admin in password mode; `login.html`/`admin.html` branch on mode. Added the **deploy kit**: `deploy/run_app.ps1`, `deploy/setup_hermes_webapp_task.ps1` (both parse-clean), and a `DEPLOY.md` Path-B runbook (incl. firewall lock to the Hamachi subnet + the Path-A upgrade notes). All `app/*.py` byte-compile clean; config logic verified (defaults to password, switches to google, domain gate case-insensitive). DESIGN.md auth + networking items updated.

### 2026-05-30 — Auth refactor: Google OAuth (OIDC) + admin approval

Replaced the placeholder password/invite-only auth with **Google sign-in + admin approval** (user decision). Google authenticates (no passwords stored anywhere); a first-time user is created `status=pending` and is blocked from member areas (`require_user` enforces approved + not-disabled) until an admin approves them on the new **pending queue** in the admin page. `TST_ADMIN_EMAIL` is auto-promoted to admin+approved on first sign-in (bootstrap); optional `TST_ALLOWED_EMAIL_DOMAINS` restricts who can even create a pending request. Changes: `models.User` swapped `password_hash`/`is_active` → `google_sub`/`picture`/`status`(pending|approved|disabled)/`approved_at`; `security.py` dropped all password code; `routes/auth.py` now does `/login → Google → /auth/callback` via Authlib; `routes/admin.py` gained approve/disable/role actions (replacing manual user creation); new `templates/pending.html` + Google-button login + admin queue; `main.py` dropped the password bootstrap and warns if Google creds are unset. New deps: `authlib`, `httpx`. Verified: all `app/*.py` byte-compile clean. Google sign-in for `openid`/`email`/`profile` is free (no CASA assessment). DESIGN.md auth item flipped `[PROVISIONAL]` → `[DECIDED]`. Setup needs a Google Cloud OAuth client (redirect `…/auth/callback`); placeholders are in `.env.example`.

### 2026-05-30 — Phase 1 scaffold: `app/` collaboration platform (FastAPI)

Stood up the platform skeleton per DESIGN.md, separate from the legacy operational `server.py` fork (which stays untouched for now). Stack chosen from the DESIGN recommendations (the user dismissed the stack questions, so these are provisional defaults, overridable): **FastAPI + SQLAlchemy + Jinja2/HTMX**, DB via `TST_DATABASE_URL` (SQLite dev → Postgres prod, no code change), **invite-only** session auth (admin seeds accounts; no public signup), role-based (`member`/`admin`). Security posture baked in from line one: admin-only control plane, and **zero order-execution in this process** — the swing-bot control endpoints are explicit stubs that relay to the trusted-side execution plane (not wired). Included a working pure-stdlib `services/black_scholes.py` (verified: call=10.4506 on the textbook inputs, put-call parity exact) and a Phase-tagged `services/resources_bridge.py` import seam to the shared `resources/`/`review/` library. All `app/*.py` byte-compile clean; web deps are per-PC and not yet installed here. Root `.gitignore` gained `*.db`/`*.sqlite3`. No trading logic yet — Phases 2-6 (MATP board, setups/collaboration, Black-Scholes UI, backtest, swing-bot wiring) build on this shell.

### 2026-05-30 — DESIGN.md: review loop is shared with collaborators

Corrected the data-visibility model. The earlier draft kept the automated review loop (Layer 5) admin-only and gave collaborators only study-level data. Per user direction, the **review loop is shared with collaborators** — it's the feedback core of the collaboration (members co-develop the swing setups, so they see how those setups performed and what the review proposes). The "two tiers" split collapses to one shared analytics surface with a single hard carve-out: **broker credentials + the live order-execution session stay trusted-side, never exposed**. Added an [OPEN] presentation choice — whether to show absolute dollar figures or normalised metrics (R-multiples, win-rate %) — defaulting to normalised. Updated §5, §6.2, §8 phase 5, and §10 accordingly.

### 2026-05-29 — Added DESIGN.md product blueprint

Captured the agreed vision for `dashboard_tst` as a **product** (not just an observer fork): TradeHunter's trend & swing methodology surfaced as a members-only, internet-facing collaboration platform. The blueprint records the roof model (TradeHunter = umbrella + shared `resources/`; two products diverging in methodology/UI/exposure), the user funnel (Finviz → MATP/MBP quarterly → pattern study → collaborate on entry/SL/PT → Black-Scholes option win-rate → parquet backtest), a dedicated trend-swing bot with its own family/gating/clientId/state that reuses the shared journal (Layer 4) + review (Layer 5) for journaling/review/self-improvement, the two-review-loops + data-sensitivity tiers (live P&L = admin-only; collaborators see study data), and the hard security split for public hosting on the R720 (admin-only control plane vs trusted-side execution plane, TLS reverse proxy, isolation from the trading Vault). Several decisions remain **[OPEN]** in the doc (host isolation, web stack, auth model, swing family name, clientId, Black-Scholes definition + options data source, backtest success definition). No code yet — this is the blueprint to design against.

### 2026-05-29 — Created as independent copy of dashboard_intraday/ on port 8001

Forked `dashboard_intraday/` (server.py + web/index.html + launchers +
setup_launcher.py). Surgical changes from the copy:
- `server.py`: `PORT 8000 → 8001`; sys.path bootstrap `dashboard_intraday
  → dashboard_tst`; docstring + WEB_DIR comment rebranded; FastAPI
  `title` → "dashboard_tst (trend & swing)"; **`_auto_start_loop`
  removed from lifespan** so it never spawns the orchestrator (only the
  intraday dashboard does).
- `web/index.html`: `<title>` + `<h1>` rebranded to TST. API URLs are
  relative so no port edits needed in the frontend.
- `.bat` launchers: port 8000 → 8001, headers rebranded.
- `setup_launcher.py`: shortcut names → "TST Dashboard" / "TST Dashboard
  (stop)"; folder paths → `dashboard_tst/`.
- Did NOT copy `tray_status.py` (it's an ingest-pipeline heartbeat tray,
  not dashboard-specific; the single copy in `dashboard_intraday/`
  serves both).
