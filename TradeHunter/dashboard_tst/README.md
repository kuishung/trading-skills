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
  task state). All ASCII-only, parse-clean. Also holds the **push reporters**
  that run where data lives and report into the app: `report_ingest_health.py`
  (parquet freshness, on Hermes) and `report_edgar_health.py` (EDGAR filing-corpus
  completeness, on **AI-Hermes** — folder-scans
  `C:\HermesSync\MarketResearch\QuarterlyReport` for missing quarters / stub MDs,
  POSTs `/api/ingest/edgar`; scheduled task).
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

### 2026-07-18 — v3.46: delete a ticker from the MATP watchlist (moderators)
- Each watchlist row now has a **`×` delete button** (moderators/admins only) to
  remove that ticker from the MATP board. New `POST /matp/{symbol}/delete`
  (`require_moderator`) hard-deletes the `MATPLevel` row and returns JSON; the
  front-end drops just that row (no list reload). `MATPHistory` is kept.
- A Finviz-filter ticker can reappear on the next filter run (manual removal, not a
  permanent block); a Selective/individual ticker stays gone until re-added.
- Templates: `_wl_macros.html` `ticker_grid` gains a `can_delete` arg → a trailing
  delete column + per-row `.wl-del` button; `_watchlist.html` passes
  `user.can_moderate`; `matp.html` adds a capture-phase `.wl-del` handler (confirm →
  POST → remove row) that preempts the row's HTMX chart-load, mirroring the TV button.

### 2026-07-18 — v3.45: FIX — MATP page hard-reloaded every 20s forever
- Removed the blunt full-page `setTimeout(location.reload, 20000)` in `matp.html`
  (gated on `open_reqs`). It had **no staleness check**, so a MATP refresh request
  stuck in `pending` (agent not polling the queue) hard-reloaded the whole page
  every 20s indefinitely.
- Auto-refresh is now solely the runs panel (`_runs_panel.html` / `_runs_context`),
  which was already staleness-aware: it polls only while a run is genuinely active
  (5–10s), reloads **just the board** once a run completes, and sets `poll_in=0`
  (STOP) when a run goes stale. Net: the board still updates when a run finishes,
  but a stuck request no longer refreshes the page in a loop.

### 2026-07-18 — v3.44: "Plot on TV" — draw MATP/MBP on the user's own TradingView
- New per-user action on `/matp`: click a ticker's **⧉** (per watchlist row) or the
  **▧ Plot on TV** button in the chart-pane toolbar → the ticker's **MATP + MBP**
  levels are drawn as two labelled horizontal lines on that user's **own**
  TradingView (web, in Chrome).
- **How it works (no server access to TV):** each user runs a tiny local **TV bridge**
  (`resources/tradingview-mcp/bridge/tv_bridge.mjs`, zero-dependency Node) on their
  own machine. The browser `fetch`es `http://127.0.0.1:9223/plot?symbol=…&matp=…&mbp=…`;
  the bridge drives that user's Chrome over CDP (reusing the TradingView MCP's chart
  API). The Hermes server never touches a TV — every user only ever plots on their own
  chart. Portable: the bridge + `launch_tv_bridge.bat` travel with the repo; nothing to
  `npm install`.
- **Idempotent:** the bridge tracks the line ids it created per symbol and removes its
  own previous MATP/MBP lines before redrawing — clicking again **refreshes** the two
  lines in place, never stacks duplicates, and never touches the user's manual drawings.
- Templates: `_wl_macros.html` (per-row `.tv-plot` span + 7th grid column),
  `_price_chart.html` (chart-pane `Plot on TV` button, gated on `chart_tv_plot`),
  `_chart_pane.html` (sets `chart_tv_plot`), `matp.html` (`window.plotOnTV` + a
  capture-phase click handler that preempts the row's HTMX navigation, plus a toast).
- Bridge setup + the "Chrome already running" gotcha: see
  `resources/tradingview-mcp/bridge/README.md`.

### 2026-06-18 — v3.43: Today Overview Phases 2 & 3 — ETF leaders + correlation + RRG (page complete)
- The last three placeholder cards are now **live**, completing the Today Overview:
  - **ETF leaders** (`/today/etf-leaders`) — 11 US sector SPDRs ranked by 1-month
    relative strength vs SPY, with 1w/1m/3m returns + a leading/lagging marker.
  - **Correlation** (`/today/correlation`) — 60-day daily-return Pearson heatmap across
    SPY + all sectors (12×12, green=together / red=inverse).
  - **ETF rotation (RRG)** (`/today/rrg`) — JdK-style RS-Ratio / RS-Momentum quadrant
    graph (Leading/Weakening/Lagging/Improving) with per-sector weekly tails, drawn as an
    auto-scaled client-side SVG.
- New `services/etf.py`: one parallel, ~15-min-cached **aligned daily-close fetch**
  (SPY + 11 ETFs, live via Yahoo) feeds `etf_leaders()`, `correlation_matrix()`, `rrg()`.
  **Pure Python** (Pearson, rolling z-scores, weekly resample) — no numpy/pandas, so
  **no new Hermes dependency**. Soft-fail to friendly messages.
- Verified live: 501 aligned dates fetched in ~1.5s; XLK leading; SPY–XLE corr −0.46;
  11 ETFs plotted with quadrants. RRG SVG script passes `node --check`; full page renders;
  app imports.
- **Today Overview is now feature-complete** (calendar + sentiment + 2 news feeds + ETF
  leaders + correlation + RRG). All data live; nothing reads parquet.

### 2026-06-18 — v3.42: Today Overview Phase 1 — sentiment + market news + company news
- Three placeholder cards are now **live**, each lazy-loaded via HTMX into its own
  endpoint so a slow source never blocks the page:
  - **Market sentiment** — CNN Fear & Greed gauge (score + rating + zone-coloured bar)
    + the VIX level (`/today/sentiment`).
  - **Market news** — broad-market headlines via Yahoo RSS (^GSPC) (`/today/news`).
  - **Company news** — per-ticker Yahoo RSS headlines for the active watchlist
    (signals first), merged newest-first (`/today/company-news`).
- New `services/market.py`: `fear_greed()`, `market_news()`, `company_news()` — all
  **live** (httpx + stdlib XML/email parsing, no new deps), 10-min TTL cache, soft-fail
  to None/[] so a dead feed shows a friendly message instead of 500. Company news uses a
  bounded thread pool, cached per symbol. VIX reuses `prices.fetch_quote("^VIX")`.
- Verified live: Fear & Greed + VIX + 12 market + 12 company headlines fetched and
  rendered; full page renders; compiles; app imports.
- Remaining: Phase 2 (ETF service → correlation → leaders), Phase 3 (RRG).

### 2026-06-18 — v3.41: HOTFIX — Today Overview 500'd on login
- v3.39's new `today` route module has its own `Jinja2Templates`, but it was **missing
  from the loop in `main.py` that injects the `nav_for`/`version` Jinja globals** into
  each route's env. Rendering `base.html` (which calls `nav_for(user)`) therefore raised
  → every login 500'd, because `/today` is now the landing. Added `today_routes` to that
  loop.
- Why earlier checks missed it: offline `get_template()` only **parses**, and
  `import app.main` doesn't **render** — the missing global only surfaces at render time.
  Now verified by rendering `/today` through the route's real env (nav_for present,
  full page renders).

### 2026-06-18 — v3.40: Selective tickers are schedulable (own refresh cadence)
- The ad-hoc **Selective tickers** set (active MATPLevels with `filter_id` NULL) now has
  its **own** scheduled MATP refresh, independent of any Finviz filter being due.
- **Data:** new singleton `SelectiveSchedule` (`models.py`, id=1, `run_interval` /
  `last_run_at` / `next_run_at`) + Alembic migration `e7f8a9b0c1d2` (head was
  `d6e7f8a9b0c1`); `get_selective_schedule()` lazily creates the row. Portable ORM only.
- **UI:** the Selective set moved from a card at the bottom to a **pinned card at the top
  of the Finviz Filter panel** with the same interval dropdown (off/daily/weekly/monthly/
  quarterly) + next/last (`finviz.html`); new `POST /finviz/selective/interval` route sets
  the cadence (due-now on change, like a filter).
- **Agent contract:** `/api/due-filters` now returns a `selective` object
  `{due, interval, tickers}`; `manual_tickers` is returned when a filter **or** the
  selective schedule is due. `MatpIngest` gained a `selective` flag — the agent's closing
  push with `selective:true`+`final:true` (no `filter_id`/`prune`) **advances the selective
  schedule**, mirroring filter runs. Contract documented in the matp `SKILL.md`.
- **Follow-up (Nous box):** the matp skill must act on `selective.due` — that's a separate
  agent deploy; until then the schedule is stored/shown but not auto-run.
- Verified: models/api/finviz compile, migration imports, finviz.html parses, app imports
  (create_all adds the table), and a singleton lifecycle test (create → due → advance →
  still one row) passes.

### 2026-06-18 — v3.39: Today Overview landing page (Phase 0 scaffold)
- New **Today Overview** page (`/today`, `routes/today.py` + `today.html`) — now the
  **post-login landing for all approved users** (flipped the redirects in `auth.py` ×2
  and the root route in `main.py` from `/matp` to `/today`).
- Added as the **first** nav item (`menus.py`); it's **always granted** (special-cased
  in `allowed_keys`) so the per-user menu ACL can't revoke the landing.
- Responsive card grid with 7 highlight cards. **Live now:** the embedded TradingView
  **economic calendar** (US, high/medium impact, themed to the dashboard's light/dark).
  The other six — market sentiment, market news, company news, ETF rotation (RRG), ETF
  leaders, correlation — are styled placeholders; their lazy endpoints land in later
  phases. All data will be fetched LIVE (Yahoo/httpx), never parquet.
- Roadmap: Phase 1 = news + sentiment (Fear&Greed/VIX) + company news; Phase 2 = ETF
  service → correlation heatmap → ETF leaders; Phase 3 = RRG quadrant graph (sector
  SPDRs vs SPY).
- Verified: routes/menus/auth compile, today.html parses, its script passes
  `node --check`, `today` stays granted for a restricted member, app imports clean.

### 2026-06-17 — v3.38: Agent-page run time shows local too
- The /agent "working now" panel (`_agent_runs.html`) rendered the run's `created_at`
  as raw UTC `%H:%M` ("… · Admin · 14:23"); now uses the localtime `time_only` macro so
  it shows the viewer's local clock. (The "online · 3m ago" + `next_run` strings are
  relative / agent-supplied, so they're unchanged.)
- Verified: template parses, app imports clean.

### 2026-06-17 — v3.37: Active-runs panel time shows local (+ time-only localtime mode)
- The active-runs panel ("waiting for the agent … · Admin · 14:23") rendered
  `r.created_at` as raw UTC `%H:%M`. Now uses the localtime macro so it shows the
  viewer's local clock (`_runs_panel.html`).
- Added a **`time_only=True`** mode to `_time.html` `localtime()` (emits
  `data-localtime`); `base.html` formats those as `{hour, minute}` local — for compact
  "today" timestamps with no date. (Full + `date_only` modes unchanged.)
- Verified: templates parse, the macro renders all three modes, base.html scripts pass
  `node --check`.

### 2026-06-17 — v3.36: Watchlist defaults to All on every fresh visit
- A bare `/matp` previously **restored the last-used filter from localStorage** (an
  early-paint redirect). Removed that — the board now always opens on **All** (server
  default `sel_wl="all"`). Picking a filter still works (adds `?wl=` for that view) but
  is no longer remembered across fresh visits (`matp.html`).
- Dropped the now-unused `localStorage('matp_wl')` write from the selector.
- Verified: matp.html parses; app unchanged server-side.

### 2026-06-17 — v3.35: Downtrend tickers are disqualified too (not just Price > MBP)
- The watchlist's **Disqualified** bucket (`matp.py` `matp_watchlist`) now holds a ticker
  if `price > MBP` **OR** it's in a **downtrend** (EMA20<EMA50<EMA200, from the live
  EMA-stack trend, falling back to the stored trend). No long setup either way.
- Section header relabelled **"Disqualified · Price > MBP or downtrend"**
  (`_watchlist.html`). The existing trend badge (▼ down) / red price already show which
  reason applies per row.
- Verified: matp.py compiles, template parses, app imports clean.

### 2026-06-17 — v3.34: Live trend uses the EMA-stack rule (strong uptrend tier)
- The watchlist's **live** trend badge previously came from `patterns.trend()` (an
  EMA20-**slope** rule). It now uses the new `patterns.ema_stack_trend()` (`resources/`)
  per the user's rule:
  - **strong uptrend** — EMA20 > EMA50 > EMA200 (badge `▲▲`, tooltip)
  - **uptrend** — EMA20 > EMA50 (badge `▲ up`)
  - **downtrend** — EMA20 < EMA50 < EMA200 (badge `▼ down`)
  - **sideways** — otherwise (`— flat`); **unknown** if < 200 daily bars
- `_ticker_analysis` (`matp.py`) routes through the stack rule; the bounce signal
  (HOT/WARM/WATCHING) now fires for both `up` and `strong_up`. `trend_badge`
  (`_wl_macros.html`) renders the new tiers and still maps the agent's stored labels
  (Uptrend/Downtrend/Sideways). The dashboard fetches 2y of daily bars, so EMA200 is
  well seeded.
- Note: the agent's stored trend (MATP skill `classify_trend`, runs on the Nous box)
  still uses its stricter close+slope rule and is only the fallback when live data is
  unavailable — align it separately if exact agreement on the fallback is wanted.
- Verified: `patterns.py` + `matp.py` compile, `ema_stack_trend` returns the right tier
  on synthetic series, `trend_badge` renders all runtime/stored values, app imports clean.

### 2026-06-17 — v3.33: Clicking a watchlist ticker swaps only the chart (no full reload)
- **Problem:** clicking a ticker did a full-page navigation to `/matp?symbol=…`, which
  re-lazy-loaded the whole watchlist and re-ran `_watchlist_signals()` (live trend/signal
  detection, Yahoo fetch per name) for **every** ticker — so every click reloaded the
  whole watchlist.
- **Fix:** ticker rows now HTMX-swap **only** the chart pane. New `GET /matp/chart`
  endpoint (`matp.py`) renders the new `_chart_pane.html` partial for one symbol, and the
  shared `_chart_context()` helper recomputes only the selected ticker's band/patterns
  (matp_home reuses it). Rows carry `hx-get` → `#chartPane` + `hx-push-url` (URL still
  updates; href kept as no-JS / middle-click fallback). Route declared before `/{symbol}`
  so it isn't shadowed.
- The watchlist DOM is left intact, so a small delegated script (`matp.html`) moves the
  "selected" highlight to the clicked row by toggling the same Tailwind classes the server
  renders (keeps the light/dark remap working).
- Result: selecting a ticker is now a single small request for that ticker's chart, not a
  whole-watchlist recompute. `_wl_macros.html` `ticker_grid` is used only by the MATP
  watchlist, so the change is scoped there.
- Verified: matp.py compiles, templates parse, `_chart_pane.html` renders a full chart
  pane (+ no-ticker fallback), all matp.html scripts pass `node --check`, app imports clean.

### 2026-06-17 — v3.32: Dashboard timestamps render in the viewer's local time
- Server stamps are UTC, but every user now reads dashboard times in **their own
  local timezone**. New reusable macro `_time.html` `localtime(dt, date_only=False)`
  emits `<time class="localtime" datetime="<ISO-UTC>">…UTC</time>` (trailing `Z` forces
  UTC parsing even when SQLite returns the datetime naive); a global script in
  `base.html` rewrites those elements to `toLocaleString()` in the browser and re-runs
  after HTMX swaps (lazy-loaded panels). JS-off fallback = the UTC text.
- Applied to the MATP-area timestamps: screener watchlist **Refreshed** (`_watchlist.html`),
  chart **calculated** (`_price_chart.html`), targets **Extracted** (`_targets_modal.html`),
  and the detail page **Updated** + MATP-history table (`matp_detail.html`). Hover shows
  the full local time + timezone.
- Other pages' timestamps (agent, research, studies chat) can adopt the same macro on
  request — the mechanism is global.

### 2026-06-17 — v3.31: Studies compact chart is 3/4 of the panel (S/R dock takes 1/4)
- On the Studies / curate page, the scroll-compact chart now shrinks to **75%** of the
  middle panel (was 50%), so the chart stays large when reduced and the trade-levels /
  support-resistance panel docks into the remaining **1/4** (`studies.html`
  `#studyMain.compact #chartHost { width: 75% }`).
- The docked trade-levels grid drops from **3-up to 2-up** so the numbers stay readable
  in the narrower 1/4 column (`#sideCol .lv-grid`).

### 2026-06-17 — v3.30: Watchlist "Refreshed" date + reliable filter last-run stamp
- **Screener watchlist now shows a "Refreshed <date> UTC" line** at the top of the
  grid (`_watchlist.html`), so a watchlist's freshness is visible without opening a
  chart. Source (`matp.py` `matp_watchlist`): for a Finviz-filter watchlist it's the
  filter's `last_run_at` (advanced when the scheduled MATP run completes); for All /
  Selective it falls back to the newest ticker `as_of` in the set.
- **`last_run_at` decoupled from the schedule gate** (`api.py` `/matp` finalize): it
  used to advance only when `RUN_INTERVALS.get(interval)` was truthy, so a finished run
  on an `off`/manual filter never stamped a date. Now **every** completed filter run
  (`final=True` + `filter_id`) sets `last_run_at = now`; `next_run_at` still only moves
  when the filter is on a schedule. Makes the watchlist date trustworthy regardless of
  interval.
- **Data Ingest page** (`finviz.html`) now shows the `last …` date even for `off`
  filters (it updates now), not just scheduled ones.
- Verified: api.py/matp.py compile, templates parse, `_watchlist.html` renders the date,
  app imports clean.

### 2026-06-17 — v3.29: Dark / light theme toggle (persisted, contrast-safe both ways)
- The whole dashboard can now switch between **dark and light** via a sun/moon
  **toggle button** in the header (between the Nous Hermes pill and the user menu).
  Choice persists in `localStorage` (`theme`), default **light** (no regression for
  existing users). `base.html` sets `<html class="light">` from a tiny `<head>`
  bootstrap **before paint** (no flash-of-wrong-theme).
- Reworked the theme CSS in `base.html`: **dark is now the default** (the app's native
  Tailwind classes), and the whole light remap is **scoped under `html.light { … }`**
  (CSS nesting) so the toggle flips the entire block at once. Restored the dark body
  gradient as default + `html.light body` for the light gradient. `<body>` no longer
  hard-codes `bg-white text-slate-900` — theme colour comes from the scoped CSS.
  Both modes keep contrasted fonts (dark = light text on dark; light = dark text on
  light, the existing `!important` remaps incl. hover variants).
- **Price chart follows the theme** (`_price_chart.html`): chart background/text pick
  white/`#334155` in light vs `#0b1220`/`#94a3b8` in dark at creation (was hard white),
  so a dark dashboard no longer shows a white chart. (Re-colour applies on next chart
  load after a toggle.)
- Also folded in the two watchlist tweaks: **ticker name fully shown** (`_wl_macros.html`
  sym col `minmax(3.8rem,1fr)` + `whitespace-nowrap`) and the **"Dropped from filter"
  list removed** (`_watchlist.html`).
- Verified: templates parse (jinja), both `base.html` scripts + the chart script pass
  `node --check`, `app/__init__.py` compiles.

### 2026-06-17 — v3.28: Chart header is one slim line (ticker · name · price · change)
- Replaced the 2-row TradingView single-quote widget with a **custom single-line
  header**: `ticker` (links to TradingView) · `company name` · `price` · `change%`,
  all inline at 2.5rem. Name/price/change are pulled live from Yahoo's chart meta via
  new `prices.fetch_quote()` (cached ~1 min, soft-fail), returned from `/matp/{sym}/
  prices` as `quote`, and filled by the chart script (bars fallback if no quote).
  Verified live (AAPL → "Apple Inc." 299.24 +0.95%); JS passes `node --check`.

### 2026-06-17 — v3.27: Watchlist font up; slimmer light chart header
- **Watchlist font increased** (`_wl_macros.html` `ticker_grid`): rows `text-[11px]`→
  `text-[13px]`, header `[9px]`→`[10px]`, trend `[10px]`→`[12px]`; widened the
  numeric columns so larger numbers still fit.
- **Chart header slimmer + light**: the TradingView single-quote header dropped from
  5.5rem → 3.5rem and switched `colorTheme` dark→light to match the white theme.
  (A true single-line custom header w/ company name would need a name-lookup — left
  as a follow-up.)

### 2026-06-17 — v3.26: Selective (ad-hoc) tickers listed + in the routine refresh
- The Finviz Filter tab now lists the **Selective tickers** — the ad-hoc 'individual'
  watchlist names (active `MATPLevel`s with no source filter) the user keyed in from the
  screener — as chips with a count.
- Those tickers are exposed to the routine refresh: `GET /api/filters` and
  `/api/due-filters` now return a **`manual_tickers`** list so the agent refreshes them
  alongside the screen filters. (Agent follow-up: the matp skill must read
  `manual_tickers` and refresh them directly — they have no screen URL.)

### 2026-06-17 — v3.25: Analyst targets show when each was extracted
- The MATP analyst-targets pop-out (`_targets_modal.html`) gains an **Extracted**
  column = `MATPTarget.as_of` (when our system first recorded/extracted the target),
  distinct from **Issued** (the analyst's `target_date`). Shows the date, with the
  full `YYYY-MM-DD HH:MM UTC` on hover; "-" when unknown. `_ticker_targets` now carries
  `as_of`.

### 2026-06-17 — v3.24: Data Ingest split into tabs
- Restructured the Data Ingest page into three tabs: **Finviz Filter** (the saved
  screener filters), **Market Data** (Price Data History + Ticker Profile, kept
  together), and **EDGAR Filings**. Client-side tab switching (deep-linkable via
  `#finviz/#market/#edgar`). Removed the **"Universe health by category"** block from
  Price Data History per request. Verified empty + populated branches render and the
  tab JS passes `node --check`.

### 2026-06-17 — v3.23: Data Ingest moved to an admin-only Settings menu
- "Data Ingest" (the `/finviz` filter manager) is now **admin-only**: removed from the
  Intraday menu, placed under a new admin-only **Settings ▾** dropdown in the nav (next
  to Admin), and the route is locked with `require_admin` (members/moderators get the
  standard 403). Dropped the `screener` key from the member-grantable menu registry
  (Intraday now = Strategy only). Verified: members can't see or reach it; admins can.

### 2026-06-17 — v3.22: Price chart goes light (match the white theme)
- The Lightweight-Charts price chart (`_price_chart.html`, MATP/Studies) now uses a
  **white background** + slate text + lighter grid/axis borders, to match the v3.19
  light theme (was `#0f172a` dark). Candle/EMA/MATP-MBP colours unchanged (all read
  fine on white). Also flipped the hidden Pattern Trainer chart for consistency.

### 2026-06-17 — v3.21: Nav dropdown fixes (close-on-click-away + light-theme hover)
- **Dropdowns now close** when you open another or click away (and on Escape). Native
  `<details>` stay open otherwise — added a capture-phase document click handler that
  closes any other open `header details[open]`.
- **Light-theme hover fixed**: the `hover:` variants (`hover:bg-slate-800`,
  `hover:text-slate-100/300`, accent `-300` hovers) weren't in the v3.19 remap, so on
  hover the background went dark and text dark → "black, can't read the words". Added
  the hover-state overrides (bg → light, text → dark / deep accent).

### 2026-06-17 — v3.20: Per-user menu access (admin-controlled)
- New **per-page menu access control**. The admin grants each member which menus they
  can use; ungranted menus are **hidden from the nav AND blocked at the URL** (303
  redirect to their first allowed page). Admins/moderators always see everything.
- `app/menus.py` is the single registry (key/label/group/href) + helpers:
  `allowed_keys(user)` (admin/mod → all; member `menu_access=None` → all for
  back-compat; else the stored list), `user_can`, `nav_for(user)` (the access-filtered
  grouped nav, registered as a Jinja global), and `require_menu(*keys)` (route-guard
  dependency).
- `base.html` renders the nav from `nav_for(user)`. Guards applied centrally via
  `include_router(dependencies=[Depends(menus.require_menu(...))])` on matp/studies/
  finviz(screener)/research(macro|company)/strategy/portfolio.
- DB: `users.menu_access` JSON list (Alembic `d6e7f8a9b0c1`, nullable, default-open).
  Admin console gains a **Menus** column — a per-member checkbox dropdown (`POST
  /admin/users/{id}/menus`) showing `granted/total`. Verified: migration up, access
  logic, nav filtering, and admin form all render.
- Flipped the dashboard from dark to a **white/light theme** via a **centralized remap
  in `base.html`** — one `<style>` block `!important`-overrides the ~30 dark utility
  classes the templates use (surfaces dark→white, text light→dark, borders, and the
  colored accent `-300` text → deeper shades for contrast on white). Body background +
  `<body>` class flipped to light too. No per-template edits — fully reversible by
  deleting that block. First pass; some accent/contrast spots may need tuning once seen.

### 2026-06-17 — v3.18: Fix — nav dropdowns were clipped by the nav's overflow-x
- The grouped-nav dropdown panels were invisible because the `<nav>` had
  `overflow-x-auto`, which forces a clipping box (when one overflow axis is `auto`
  the other can't stay `visible`), cutting off the absolutely-positioned panels.
  Removed the overflow (added `flex-wrap` for safety on narrow screens) — the few
  grouped items fit without horizontal scrolling and the dropdowns now show.

### 2026-06-17 — v3.17: Navigation regrouped into categories
- The flat top-nav is now **grouped dropdowns** by trading horizon (user IA):
  **Investing** (Macro / Company → `/research?kind=…`), **Swing & Trend**
  (MATP / Studies), **Intraday** (Screener=`/finviz` renamed / Strategy), and a
  top-level **My Portfolio**. Each group is a `<details>` dropdown; the active group
  highlights from the current path.
- New placeholder pages + routes: **`/strategy`** (Intraday — the Finviz→levels→Alpaca
  pipeline home) and **`/portfolio`** (Alpaca account view) — both "under construction"
  shells (`strategy.html`). Registered in `main.py` (+ version-globals).
- `research` route takes `?kind=macro|company` to filter the list (drives Macro/Company).
- Profile moved into the user dropdown (kept reachable). "Data Ingest" label → "Screener".
- Per user request: removed the **Patterns** entry from the nav menu (`base.html`) and
  **disabled the patterns router** (`main.py` `include_router` commented out → routes
  no longer registered, `/patterns*` 404s). The code, templates, models, and the
  `strategy/patterns` detector engine are **kept intact** — re-enable by uncommenting
  the one `include_router` line and restoring the nav entry.

### 2026-06-17 — v3.15: Pattern Trainer — draw spawns in the visible range
- "Draw ascending triangle" now places its handles inside the chart's **currently
  visible range** (`getVisibleLogicalRange`, at ~20%/70% across it), not the last ~30
  bars — so the triangle appears where you're looking even after scrolling back.
  Falls back to the recent window if the view is empty/too narrow.

### 2026-06-17 — v3.14: Pattern Trainer — Reset (start over) button
- New **`POST /patterns/{id}/reset`** + a **Reset (start over)** button in the
  calibration card: deletes ALL saved examples, clears the fitted calibration (back
  to seed thresholds), and sets status to `learning` — a blank slate to re-teach.
  Confirms first; the code/spec (`detect.py`, `pattern.md`) is untouched. UI clears
  the gallery, readiness, and threshold chips on success.

### 2026-06-17 — v3.13: Pattern Trainer — measured-move target + H at the base
- Matches the user's reference diagram: **H** is now defined as resistance − the
  **rising support at the LEFT base** (`sup.p0`), not ceiling − lowest-low — both the
  `_filter_min_height` rule and the overlay use this.
- The overlay now draws the **measured-move target** as a dashed grey level at
  **breakout + H** (with the price on the axis), completing the anatomy: flat
  resistance, ascending support, break ↑ marker, and the projected target. Cleared
  with the rest of the overlay. Verified H on real parquet (AAPL 232.9−190.9 = 42.0);
  rendered JS passes `node --check`.

### 2026-06-17 — v3.12: Pattern Trainer — volume / time-of-day / touches rules (spec gaps)
- Four new tickable+adjustable detection rules closing the gaps vs the canonical
  ascending-triangle spec (`strategy/patterns/ascending_triangle/pattern.md`):
  - **Volume contracts into apex** (`_filter_vol_contraction`): last-third mean
    volume ≤ first-third (the spring winding up).
  - **Breakout volume** ≥ N×20-bar avg (`_filter_breakout_volume`, default 1.5):
    the expansion that separates a real break from the thin-volume fakeout; drops
    matches that never broke out.
  - **Skip open/lunch/close** (`_filter_time_of_day`, intraday): drop breakouts in
    09:30–10:00 / 12:00–13:00 / 15:45–16:00 ET.
  - **Min touches/line** ≥ N (`_filter_min_touches`, default 3, **off** by default —
    3 is demanding and zeroes most symbols on seed thresholds).
- `pattern_detect` takes `vol_contract/breakout_vol/time_filter/min_touches`;
  `_apply_rules` + shared `_breakout_idx`/`_et_minutes` helpers. Verified each filter
  discriminates on real parquet; rendered JS passes `node --check`.

### 2026-06-16 — v3.11: Pattern Trainer — uptrend = EMA stack; draw-tool robustness
- **Uptrend gate is now the bullish EMA stack**: `fast > mid > slow` (intraday
  **EMA6>18>50**, daily **EMA20>50>200**) — an ascending triangle forms obviously in
  that regime. `_filter_uptrend` rewritten + `_ema_periods` returns (fast, mid, slow);
  falls back to mid>slow when fast can't be seeded. (NVDA 3m 18→8, AAPL daily 3→2.)
- **Draw tool** renamed **"Draw ascending triangle"** (horizontal resistance +
  ascending support; Save teaches it as the pattern), and `startDraw` now **auto-loads
  the typed ticker** if no chart is loaded — the most common "can't draw" cause was an
  empty chart silently no-op'ing. Clear status hint once handles appear.

### 2026-06-16 — v3.10: Pattern Trainer — resistance sits on the top wick (never cuts a body)
- The detector's horizontal resistance is now a strict level at the window's **highest
  high**, so the line **touches the top candle wick** instead of the least-squares
  average (which floated below the wicks). Fixed in `strategy/patterns/_features.py`
  `window_lines` (resistance p0==p1==max high); the min-height rule uses that level.
- The **drawn overlay** computes its ceiling from the **displayed** candles
  (`_maxHighIn` = max high over all loaded bars in the window, incl. extended hours),
  not the RTH-only backend value — because high ≥ open/close for every candle, the line
  is guaranteed to **never cut through a candle body** even when an extended-hours bar
  in the window spikes above the RTH high. `findBreakout` uses the same ceiling.
  Support unchanged (fitted ascending line).

### 2026-06-16 — v3.09: Pattern Trainer — on-screen detection rules (tick + adjust)
- New **Rules panel** in the detector card: each detection rule has a **checkbox to
  engage/disengage** it and, where relevant, an **adjustable value** — applied on
  Find. Rules: **RTH only**, **Uptrend (EMA)**, **No support breach**, **Min height
  ≥ N ×ATR** (default 0.2, editable), **Min score ≥ N** (off by default, overrides
  the fire cutoff). The Find request passes them as query params; the backend
  `pattern_detect` accepts `rth/trend/valid/min_height_atr/score_min` and runs them
  through `_apply_rules`.
- New `_filter_min_height` rule (resistance ceiling − lowest low, over window ATR;
  ticker-relative). Note: 0.2 ×ATR filters little (real triangles run 1–3 ×ATR tall)
  — the value is adjustable precisely so you can raise it until it bites.
- Backend `_geometry` imported for ATR. Verified: detect→rules chain runs on real
  parquet; rendered JS passes `node --check`.

### 2026-06-16 — v3.08: Pattern Trainer — RTH-only detection, triangle validity, apex tip
- **Detection now runs on regular-hours bars only** (`_rth_bars` + `_is_rth`,
  zoneinfo/DST): intraday Find/scan filter out pre-market + after-hours before
  detecting. Extended hours remain purely for the vertical session-separation lines.
- **Triangle validity gate** (`_filter_valid_triangle`): a match is dropped if price
  CLOSES below the rising support line anywhere in the window (0.2% tol) — that's a
  support breach / breakdown, not a valid ascending triangle. Applied in /detect +
  /scan, after the geometry detector, before the uptrend gate. (NVDA 3m: 18 raw → 12
  valid → 7 uptrend.)
- **Overlay converges to the tip:** the ascending support now rises to meet the
  horizontal resistance at the **apex (tip)**, and the break ↑ marker is the first
  **RTH** close above resistance **near the tip** (search bounded to the apex region,
  extended-hours bars skipped). Frontend `findBreakout`/`drawAnatomy` reworked.

### 2026-06-16 — v3.07: Pattern Trainer — extended-hours vertical session lines
- Replaced the extended-hours candle dimming with **vertical session lines** drawn as
  DOM overlays (Lightweight Charts has no native vertical line): green at the 09:30 ET
  open, red at the 16:00 ET close, so pre-market sits left of green and after-hours
  right of red. Repositioned on pan/zoom/resize. This frees candle colour entirely for
  the pattern highlight (per the earlier request). Daily has no session lines.
- Extended hours are now drawn as **vertical session lines** (the user's ask),
  replacing the candle-dimming: a **green dashed line at the 09:30 ET open** and a
  **red dashed line at the 16:00 ET close** of each day, so pre-market sits left of a
  green line and after-hours right of a red one. Implemented as thin DOM overlays on
  the chart (Lightweight Charts has no native vertical line) — same technique as the
  draw handles — repositioned on pan/zoom/resize. Intraday only.
- Dropped the extended-hours candle-dimming so candle colour is reserved purely for
  the pattern highlight (as originally requested). Legend updated. Frontend-only;
  rendered JS passes `node --check`.
- Dropped the **Teach/Test toggle**. One layout: a **big chart** (600px, now 3/4 width
  on a 4-col grid) with the **Detector / Find pattern panel moved to the upper-right**;
  teaching controls (Draw triangle → Save positive/counter) live inline in the toolbar.
- **Teaching is now adjust-the-lines**: the resistance is a **strict horizontal** line
  you drag up/down to set the flat break level; the **ascending support** is two
  handles snapping to the rising lows (the tightening range). Saved geometry has
  `resistance.p0 == p1` (horizontal). The draw handles were reworked accordingly
  (`addResHandle` = vertical price drag via `coordinateToPrice`; `addSupHandle` = snap
  to bar low).
- The detection overlay (`drawAnatomy`) now redraws the same anatomy: horizontal
  resistance (break level) + ascending support + break ↑ marker, with the matched
  candles highlighted and the rest greyed. Uptrend-only EMA gate (v3.04) unchanged.
- Frontend-only; rendered JS passes `node --check`. (Needs live testing on deploy.)
- Reworked the detection overlay around the user's feedback. Selecting a match now
  **highlights the pattern's candles in full colour and greys out every other bar**
  (`renderCandles` + `hiRange`) — the clearest "which portion is the pattern" signal —
  and the chart **zooms to it**. The overlay is now a **single horizontal resistance
  line** at the break-out ceiling (start → break candle) plus the `break ↑` marker;
  the **support line was dropped** per request. Extended-hours dimming remains the
  resting-state shading (when no match is selected); when a match is selected the
  highlight takes over. Uptrend-only EMA gate (v3.04) unchanged and still applied.
- Note: true vertical session lines / TV-style ETH background bands aren't natively
  supported by Lightweight Charts, so ETH stays candle-shaded at rest — a custom
  chart primitive would be needed for literal vertical lines (flagged, not built).

### 2026-06-16 — v3.04: Pattern Trainer — Find pattern is uptrend-only (EMA gate)
- An ascending triangle is a bullish continuation, so Find pattern (and the universe
  scan) now keep only matches whose window ends in an **uptrend**, judged by the same
  EMAs drawn on the chart (`_filter_uptrend`): last close above the slow EMA, mid EMA
  above slow EMA, and the slow EMA rising (daily 50/200, intraday 18/50). Falls back
  to the mid EMA when there isn't enough history to seed the slow one. `/detect` takes
  `?trend=up|any` (default `up`); the results header shows "uptrend only (EMA)" and how
  many off-trend matches were hidden. Geometry detector stays unchanged — this is a
  context gate at the application layer.

### 2026-06-16 — v3.03: Pattern Trainer — extended hours shown on intraday charts
- The 3m/5m parquet already includes pre-market (04:00–09:30 ET) + after-hours
  (16:00–20:00 ET), but those candles were indistinguishable from the regular
  session. Now extended-hours candles are **dimmed to muted slate** (lighter=up,
  darker=down) while regular hours stay vivid green/red, with an "ext hrs" swatch
  in the toolbar legend. RTH classified per-bar by ET time via `Intl`
  (`America/New_York`, DST-correct: regular = 09:30–16:00 ET). Daily unaffected.
  Frontend-only. (Verified the split on a real day: 110 pre / 130 RTH / 80 AH.)
- EMAs now draw on **daily too** and share one colour scheme by speed —
  **fast = red, mid = green, slow = purple** — with timeframe-specific periods:
  **daily = EMA20/50/200**, **intraday (3m/5m) = EMA6/18/50**. (Recolours the
  intraday EMAs from the v3.01 blue/red/green to red/green/purple.) Toolbar legend
  shows the active set. Still client-side, SMA-seeded, recomputed per load.

### 2026-06-16 — v3.01: Pattern Trainer — EMA overlays on intraday charts
- 3-min / 5-min charts now draw **EMA6 (blue), EMA18 (red), EMA50 (green)** with a
  colour legend in the toolbar. Computed client-side from the loaded closes, seeded
  with the SMA of the first `period` bars (TradingView convention) so the early tail
  isn't skewed. Drawn only on the intraday timeframes; cleared on daily and
  recomputed on every chart load. Frontend-only.

### 2026-06-16 — v3.00: Pattern Trainer — full triangle anatomy on a match (+ breakout)
- Clicking a detection now draws the three things a trader reads: (1) the
  **resistance** (flat top, amber), (2) the **ascending support** that tightens the
  range toward the apex (cyan), and (3) a **break ↑ marker** on the first candle
  that closes above the resistance line — found by extending resistance forward over
  the loaded history and scanning the apex region only. Frontend-only (reuses the
  `lines` the detector already returns in v2.99); the breakout is a post-hoc visual
  annotation over stored bars, so the detector itself stays no-lookahead.
- Resistance recoloured amber (was cyan) so the flat top and the rising support read
  as two distinct lines. Breakout marker clears with the rest of the overlay (Find-
  again / clear / reload / leaving Test).

### 2026-06-16 — v2.99: Pattern Trainer — Find pattern draws WHERE it matched
- Fixed: `Find pattern` listed flagged windows + zoomed, but never showed *which
  portion* of the chart or *what shape* the detector saw — so a hit was an opaque
  score. Now each match carries the detector's fitted trend-line endpoints and the
  chart **draws them as cyan dashed resistance/support lines** over the matched
  window (same renderer as your drawn triangle). The strongest match draws + zooms
  automatically on Find; clicking any row redraws + zooms to it. Lines clear on
  Find-again, clear, chart reload, and leaving Test mode.
- Backend: `strategy/patterns/_features.window_lines()` returns the fitted
  resistance/support endpoint prices (first/last bar, extrapolated from the same
  least-squares fit), and `ascending_triangle/detect.py` attaches a `lines`
  ({side:{t0,p0,t1,p1}}) field to every kept match (computed post-NMS, negligible
  cost). `/detect` passes it through. Verified on real parquet (AAPL daily: flat
  resistance 224→237, rising support 191→226 — a clean ascending triangle).

### 2026-06-16 — v2.98: Pattern Trainer — rebuilt around the calibration loop
- Reorganised the whole detail page from a button-pile into the **Teach → Calibrate
  → Test → Promote** loop (the page is a calibration loop, not a form). New layout:
  a **chart workspace** (2/3) beside a **brain sidebar** (1/3).
- **One mode switch** (`Teach` ⇄ `Test`) replaces the stacked toolbar. Teach shows
  draw-triangle + save positive/counter; Test shows Find pattern. Dropped the legacy
  click-to-`Mark region` primitive — drawing is the only label (its markers code is
  retained only to redraw older non-geometry examples).
- **Readiness sidebar**: example counts (✓/✗), validation suite pass-rate + bar, and
  a single next-step hint, all gated — `Recalibrate` unlocks at ≥3 ✓ and ≥2 ✗;
  `Promote → live` unlocks only when the suite passes ≥80%; `Run across universe`
  unlocks only once promoted.
- **Correction loop**: each detector hit in Test mode carries `✓ correct` / `✗ wrong`
  — one tap saves it as a positive/counter example and bumps readiness, so misfires
  feed the next calibration (active learning made literal). No new backend — it posts
  the detected window to the existing `examples.json`.
- New routes (`patterns.py`): `POST /calibrate` (fit thresholds from examples via the
  D4 engine + run the validation suite, store pass-rate), `POST /promote` /
  `POST /reopen` (gated status flips), `GET /scan` (capped universe sweep with the
  calibrated detector — honest about the cap; full queued scan is a later build).
  `/detect` now runs with the pattern's **calibrated** thresholds once it has them.
- DB: `patterns` gains `detector_thresholds` (JSON) + `detector_version` +
  `calib_pass_rate` + `calib_at` (Alembic `c5d6e7f8a9b0`, ORM-portable, up/down
  verified base→head). Verified end-to-end via TestClient on real parquet: calibrate
  fits + scores a suite, promote is correctly blocked below 80%, scan is blocked
  until ready.

### 2026-06-16 — v2.97: Pattern Trainer — remove the teaching chat (drawing is the label)
- Deleted the **Teaching chat** entirely (user: "why is the teach chat still
  there?"). Once the drag-to-label triangle tool (v2.95) made the *drawn geometry*
  the authoritative label — the calibrator derives slope/R²/touches from the shape,
  not from prose — the free-text chat was redundant and confusing. Removed: the
  `#chatpanel` section + chat JS from `pattern_detail.html`, the unused
  marked/DOMPurify CDN scripts + markdown CSS, the `/patterns/{id}/chat` and
  `/chat.json` routes + `_do_chat`/`_marked_context` helpers, and the
  `pattern_llm`/`PatternLesson`/`lessons`/`chat_ready` wiring in `patterns.py`.
- `patterns.html` intro + the "chat not configured" banner rewritten to describe the
  draw-to-label + **Find pattern** flow. (DB table `pattern_lessons` is left in
  place — harmless, no migration needed; can be dropped later if desired.)
- Verified: both templates parse, route file compiles, app imports v2.97, chat
  routes gone, `/detect` still present.

### 2026-06-16 — v2.96: Pattern Trainer — "Find pattern" detector overlay (D5)
- The Trainer now **runs the real geometric detector against the chart and draws
  what it found** — closing the loop from "teach" to "test". New **Find pattern**
  button calls `GET /patterns/{id}/detect?symbol&timeframe`, which loads the
  parquet window and runs the implemented `strategy/patterns/<slug>/detect.py`
  (D1–D4 engine: swings → line-fit → soft-scored geometric mean → NMS). Each
  match comes back with a **score + explainable `notes`** (slopes, R², touches,
  apex, contraction) and Lightweight-Charts times (epoch for intraday, ISO date
  for daily).
- A **results panel** under the chart lists every flagged window (score + window
  span); clicking a row **zooms the chart to that match's range**
  (`timeScale().setVisibleRange`). This is the visual-evaluation surface from
  `DETECTOR_DESIGN.md` — you eyeball whether the detector's geometry agrees with
  your own labels before trusting it, and mis-fires become new negative examples
  for the D4 calibration set.
- Backend detector resolution is **slug-driven** (`_detector_for(slug)` →
  `strategy.patterns.<slug>.detect` via `import_module`, falling back to
  `ascending_triangle`). Import uses `importlib.import_module` deliberately: the
  pattern package re-exports `detect()`, which shadows the submodule attribute,
  so a plain `import …detect as x` would bind the function — `import_module`
  returns the true module with `.detect`/`.__version__`.
- Verified end-to-end against parquet: NVDA 3min → 29 flags, AAPL daily → 3
  flags, each with valid scores + LWC times; route compiles, template parses.

### 2026-06-16 — v2.95: Pattern Trainer — drag-to-label triangle tool (zero text)
- The canonical labelling UX is now **direct manipulation, never typing** (user:
  "I need to drag from the chart… inputting text is confusing"). **Draw triangle**
  puts two trendlines on the chart — **resistance** (red) and **support** (green)
  — each with **draggable handles that snap to a candle's high/low**. The shape you
  draw IS the label; the calibrator derives slope/R²/touches FROM the geometry —
  you never type a threshold. **Save positive** (it's the pattern) / **Reject
  (negative)** (a near-miss) — both polarities feed calibration.
- Saved examples now carry **`kind`** (positive/negative, shown as a green/rose
  dot) + the drawn **`geometry`**; clicking one reloads the ticker and **redraws
  its trendlines**. (Migration `b4c5d6e7f8a9` adds `kind` + `geometry` JSON to
  `pattern_examples`; up/down verified, ORM round-trip verified.)
- Drag layer is custom (lightweight-charts has no native draggable lines): HTML
  handles positioned via `timeToCoordinate`/`priceToCoordinate`, pointer-drag →
  `nearestBarByX` snap → live `setData` redraw, repositioned on zoom/pan. The old
  2-click "Mark region" is hidden (kept for the chat path); "Save example" is
  replaced by the draw flow. Files: `models.py`, migration, `routes/patterns.py`
  (`save_example` takes `kind`+`geometry`, `_parse_geometry` validates), and
  `templates/pattern_detail.html`. Backs `strategy/patterns/DETECTOR_DESIGN.md`'s
  drag-to-label model. (NOTE: real mouse-drag feel needs a human eye — headless
  preview can't perform true drag gestures.)

### 2026-06-16 — v2.94: Pattern Trainer — saved-examples gallery + ROOT-CAUSE fix for the empty-`p` bug
- **New: saved-examples gallery** (user request — "save every example; when I load
  it again it shows up"). Mark a ticker+region, hit **★ Save example**, and it's
  stored as a reloadable artifact under the pattern. The page lists examples in a
  gallery; **clicking one reloads that ticker+timeframe and redraws the marked
  box** (A/B markers restored, ready to teach again). Delete removes it. Builds a
  per-pattern training set the Phase-2 detector generator will cite.
  - `models.py` `PatternExample` (pattern_id, symbol, timeframe, start_t, end_t,
    n_bars, label, note) + cascade off `Pattern.examples`; migration
    `a3b4c5d6e7f8_pattern_examples` (off head `f2a3b4c5d6e7`; up/down round-trip
    verified). `routes/patterns.py`: `POST /{id}/examples.json` (validates the
    region resolves to ≥1 parquet bar before saving), `POST /{id}/examples/{ex}/delete`,
    and `examples` passed to the detail page. `pattern_detail.html`: gallery panel +
    Save button (enabled only when a region is marked) + JS `applyMarkedRegion`
    (re-applies an example's ISO bounds onto the loaded bars).
- **ROOT-CAUSE fix for the Pattern-detail "empty fields" bug** (the real cause the
  v2.93 JS band-aid only worked around): `base.html` did `{% set p = request.url.path %}`
  as a nav-highlight helper. `pattern_detail.html` passes a **Pattern object as `p`** —
  so inside `{% block content %}`, base's `p` (the URL string) **shadowed** it, and
  every `{{ p.name }}` / `{{ p.chart_symbol }}` / `{{ p.id }}` / `{{ p.status }}`
  silently resolved against a string → **blank**. That's why the title, the chart
  ticker (so the chart never auto-reopened), the status badge, AND `data-pattern`
  (the v2.93 "empty pattern-id → /patterns//bars 404 → no bars" symptom) were all
  empty. The list page escaped it only because it uses a loop-scoped `{% for p in
  patterns %}`. Fix: rename base.html's helper `p` → `nav_path` (it's used only
  inside base.html's nav). Verified: detail page now renders name/ticker/status,
  the chart **auto-reopens on the saved ticker**, and nav highlighting still works.
  This makes the chart half of "load it again and it shows up" work too.

### 2026-06-15 — v2.93: Pattern Trainer — fix empty pattern-id in chart fetch (the real "no bars" cause)
- **THE root cause of every "no bars" on the chart:** the chart JS read the
  pattern id from the `data-pattern` attribute, which resolved **empty** at
  runtime, so every request went to `/patterns//bars` (double slash) → **404** →
  the frontend rendered that as "no bars" for *every* symbol/timeframe. The
  backend was always returning data; the request never reached it. Fix: derive
  the id from the page URL (`/patterns/<id>`) with the attribute as fallback
  (`pattern_detail.html`).
- Also hardened caching (found while diagnosing): the chart `fetch()` uses
  `cache:'no-store'` + a `_=<ts>` buster, and `/bars` sends `Cache-Control:
  no-store` (`routes/patterns.py`) so a stale empty response can't stick.
- Removed the temporary v2.92 `/bars` debug block.

### 2026-06-15 — v2.92: Pattern Trainer — daily back in dropdown + /bars diagnostic
- Re-added **Daily** to the timeframe dropdown → now **Daily / 3-min / 5-min**
  (`_TIMEFRAMES`, default chart timeframe back to daily).
- When `/patterns/{id}/bars` returns no bars, the JSON now includes a `debug`
  block (`root`, `path`, `path_exists`, `n_symbols_tf`) so a data-root / path
  mismatch can be diagnosed from the response itself. (Temporary aid while
  tracking down intraday "no bars" on Hermes.)

### 2026-06-15 — v2.91: Pattern Trainer timeframes → 3-min / 5-min
- Switched the Pattern Trainer chart timeframes to **3-min and 5-min** (both fully
  seeded at ~1,509 symbols), dropping daily and the mostly-empty 1-min (only ~58
  symbols seeded). `_TIMEFRAMES`, window/cap maps, the no-match fallback, and the
  dropdown labels updated. daily/1min defaults remain in the maps so either can be
  re-added by listing it in `_TIMEFRAMES`.

### 2026-06-15 — v2.90: Pattern Trainer intraday bars fix (no-stats parquet)
- `/patterns` chart returned "no bars" for **3min/1min** even though the files
  exist (daily worked). Cause: `_load_window` used `bars_store.available_range_fast`
  to find the default window, but that returns `None` for parquet written without
  `t` column statistics (the intraday seeds), so the route bailed. Now it reads
  the file and slices from the actual last bar (windowed by `_WINDOW_DAYS` + capped
  by `_MAX_BARS`), independent of column stats. Marked-region loads (explicit
  start+end) are unchanged.

### 2026-06-15 — v2.89: Pattern Trainer chart fix (explicit sizing)
- The Lightweight Charts candles weren't visible on `/patterns` even though the
  `/bars` endpoint returned valid data: `autoSize: true` was resolving the canvas
  to **0 height** in this layout, so the chart painted into nothing. Switched to
  **explicit `width`/`height`** at `createChart` + a `ResizeObserver`/`resize`
  width-sync (`pattern_detail.html`). No backend change.

### 2026-06-15 — v2.88: Pattern Trainer (Phase 1 — teach a pattern on a parquet chart)
- New members page **/patterns** (`routes/patterns.py`, `patterns.html`,
  `pattern_detail.html`, nav item, `Pattern`/`PatternLesson` models + Alembic
  migration `f2a3b4c5d6e7`): create a pattern, load a real chart, mark the region
  that shows it, and teach the assistant in chat.
- **Chart loads FROM PARQUET** (daily/3min/1min) via `resources.bars_store` —
  the first UI to do so on purpose. This is an OFFLINE training tool, so parquet
  is the right source; recorded as a **CARVE-OUT** in CLAUDE.md's "parquet =
  backtesting only" rule (live views still fetch live). Uses TradingView
  Lightweight Charts 4.2.0 (same lib as the MATP chart). New `GET
  /patterns/{id}/bars`.
- **Region marking:** click "Mark region" then two points on the chart (start/end);
  those bars are re-loaded from parquet and **injected into the teaching prompt**
  (`services/pattern_llm.py`, DeepSeek-direct, ticker-relative-threshold persona).
- **Same chatbot UX as Research** (async, markdown, thinking timer) pointed at the
  pattern endpoint, with a per-message "⌖ SYM · tf · N bars" badge when a region
  is attached. No-JS form fallback kept.
- Phase 2 (generate `pattern.md` + `detect.py`, committed to
  `strategy/patterns/<slug>/`) and Phase 3 (the "Find pattern" universe scan) are
  designed in `PATTERN_TRAINER_DESIGN.md`, not yet built.

### 2026-06-15 — v2.87: Research chat — real chatbot UX (async, markdown, thinking)
- Replaced the form-reload planning chat with a proper **async chatbot**
  (`research_detail.html` + new `POST /research/{id}/chat.json`): sending a message
  no longer reloads the page — the user bubble appears instantly, an animated
  **"Nous Hermes is thinking… Ns"** indicator runs while the agent works (with a
  *"reading the filings — up to a minute"* note in agent mode), and the reply is
  appended in place. Essential now that agent-grounded replies take 15–60s.
- **Markdown rendering** of assistant replies (marked + DOMPurify via CDN, same
  pattern as base.html's Tailwind/HTMX) — headings, lists, tables, code, bold now
  render instead of showing raw `**asterisks**`. Scoped `.md-body` styles match the
  console theme; inherits the global invisible-until-hover scrollbar rule.
- **Graceful degradation:** the form keeps `action=/research/{id}/chat` (the
  original redirect route), so with JS off it still works via full-page POST.
- Enter sends / Shift+Enter newline; textarea auto-grows; "Draft from chat"
  enables after the first message without a reload.

### 2026-06-15 — v2.86: Research chat — agent-grounded mode (reads the 10-Q corpus)
- The planning chat can now be **relayed to the Nous agent** instead of calling
  DeepSeek blind. When `TST_RESEARCH_RUNNER_URL` (+ `TST_RESEARCH_TOKEN`) are set,
  `services/research_llm.chat()` POSTs the conversation to the LAN-only
  `research-runner` shim on the Linux box, which runs the real Hermes agent
  (`hermes chat -q … -s research-planning`). That agent reads the EDGAR 10-Q
  corpus at `/mnt/hermes_sync/QuarterlyReport/<TICKER>/…` **on demand**, so the
  chat is grounded in the actual filings. (Agent-side pieces live in
  `nous_hermes/research_runner/` + the `markets/research-planning` skill.)
- **DeepSeek-direct is now the fallback**, not the only path: used when no runner
  is configured OR when the runner is unreachable/errors — so the page never
  breaks. `research_llm` refactored into `_chat_runner()` + `_chat_deepseek()`,
  with `runner_enabled()` / `chat_mode()` helpers.
- **Dashboard-visibility:** the planning-chat header shows a mode pill —
  *agent-grounded* (emerald) vs *DeepSeek (direct)* (slate) — so the user can see
  which brain is answering (`research_detail.html`, driven by `chat_mode`).
- DeepSeek-direct error messages now surface the API's real reason (e.g.
  *HTTP 401: Authentication Fails* / *402: Insufficient Balance*) instead of a
  bare code, and the key is `.strip()`-ed before use (a trailing space in `.env`
  had masqueraded as a bad key).
- New config (`config.py`): `TST_RESEARCH_RUNNER_URL`, `TST_RESEARCH_TOKEN`,
  `TST_RESEARCH_RUNNER_TIMEOUT` (default 150s). All optional — unset = prior
  DeepSeek-direct behaviour. Note: agent-grounded replies take **15–60s** (the
  agent runs a tool-calling loop); async UX is a deferred follow-up.

### 2026-06-13 — v2.85: Research page (Phase 1 — chat → plan → queue)
- New members-facing **/research** page (`routes/research.py`, `research.html`,
  `research_detail.html`, nav item): create a macro/company research topic, chat
  with the assistant to shape it, save the agreed **plan** (Markdown), and queue a
  run / set a per-topic cron. Owner-scoped (mods/admin see all).
- **Planning chat calls DeepSeek DIRECTLY** from the dashboard
  (`services/research_llm.py`, OpenAI-compatible) — because the Nous agent is
  outbound-only and can't serve a real-time chat. New config:
  `TST_DEEPSEEK_API_KEY` / `TST_DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`)
  / `TST_DEEPSEEK_MODEL` (default `deepseek-chat`). Soft-fails with a clear notice
  if the key is unset. See `RESEARCH_DESIGN.md` (architecture correction).
- Models `ResearchTopic` / `ResearchMessage` / `ResearchRun` + Alembic migration
  `e1f2a3b4c5d6` (portable types, Postgres-ready). Migration auto-runs on startup.
- **Phase 2 (next):** the agent side — poll `/api/research/due`, execute against the
  corpus, write output md to AI-Hermes, POST results back; register the per-topic
  `hermes cron`. "Run now" currently queues a run for that agent loop.

### 2026-06-11 — v2.84: sticky header for the option strike analyser
- `_bs_calc.html`: the **Option strike analyser** heading + Call/Put toggle +
  parameter inputs (Spot/Target/Days/IV/Rate/Div) are now a **sticky header**
  (`lg:sticky lg:top-0`) pinned to the top of `#scrollZone` while the strike
  ladder scrolls underneath — so the inputs stay visible and editable as you read
  the table (user request). Negative margins span the card's `p-3` so the opaque
  band sits flush to the card edges + a bottom divider; lg-only because
  `#scrollZone` is only an overflow container on lg (mobile page-scrolls).

### 2026-06-11 — v2.83: status docks into the chart header + proportional ¼ compact
- User feedback on v2.82, same day. (1) The **status strip** (badge + status
  controls) is no longer a separate panel — on lg it **docks into the chart's
  top header next to the TradingView ticker quote** (`#chartStatusSlot` in
  `_price_chart.html`, opt-in via `chart_status_slot`; JS moves `#statusPanel`
  in). Mobile / no-chart keeps the card-in-flow style. (2) Compact mode now
  shrinks the chart **proportionally — half width × half height = ¼ area,
  keeping its shape** — instead of squashing it into a full-width 23vh ribbon
  (56vh full → 32vh half-width compact). The trade-levels panel docks into the
  freed width beside it; the TV quote header is no longer cropped (it carries
  the status now), the EMA legend row + status title/signal hide in compact so
  the half-width header fits without cropping. **Chart floor (user): never
  smaller than 12cm × 8cm** (`min-width: 12cm` on the compact chart,
  `min-height: 8cm` on the chart zone) — the 50%/32vh proportions apply only
  where they stay above the floor, so small laptop screens get 12×8cm and
  large monitors get the proportional ¼.
- `_bs_calc.html` footnote now states the **data sources** explicitly (user
  asked where the analyser's data comes from): spot + IV default from the live
  Yahoo daily feed (last close / 30-day realized vol), target from the trade
  plan, premiums/probabilities computed by Black-Scholes from the inputs — no
  live option-chain quotes; the strike list is a model ladder.

### 2026-06-11 — v2.82: Studies scroll-compact chart + Black-Scholes strike analyser
- **Studies middle column is now two zones**: the chart on top, a scrollable
  zone below (status panel, trade levels, and the new strike analyser).
  Scrolling the lower zone past ~48px animates the chart down to **~¼ height**
  (56vh → 23vh, with the TradingView quote header cropped away so the plot
  keeps its room) and **docks the status + trade-levels panels into a side
  column beside the chart** (JS reparents them; slide-in animation), freeing
  the column for analysis work. Scrolling back to the top restores the full
  chart. Hysteresis (enter 48px / exit 8px) + a compact-only spacer keep the
  toggle from oscillating; lg-only — mobile stacks and page-scrolls as before.
- **New: Black-Scholes option strike analyser** (`_bs_calc.html`) below the
  levels panel — finally wires the until-now-unused `services/black_scholes.py`
  (DESIGN.md phase 4) to a UI. Inputs: Call/Put, spot, target, DTE, IV, rate,
  dividend yield. Spot prefills from the chart's live price feed and **IV
  defaults to the ticker's 30-day realized volatility** (computed client-side
  from the same bars); Target seeds from the study's profit target. Renders a
  ±27.5% strike ladder (premium, delta, breakeven, risk-neutral P(ITM),
  P(profit beyond breakeven), P/L + ROI if the target is hit at expiry) with
  the ATM strike flagged and a **"best strike"** highlight ranked by
  ROI × P(profit) — so a lottery-ticket far-OTM strike can't win on ROI alone.
  The risk-neutral-vs-real-world caveat from the service docstring is surfaced
  in the UI as required.
- Files: `app/routes/studies.py` (`GET /studies/api/black-scholes` strike-grid
  endpoint + `_strike_step` spacing heuristic), `app/templates/studies.html`
  (zones + compact CSS/JS), `app/templates/_bs_calc.html` (new),
  `app/templates/_price_chart.html` (`tvq-header` class hook only — shared
  with MATP pages, no behaviour change there), `app/__init__.py` (v2.82).

### 2026-06-09 — Data Ingest §3: EDGAR filing-corpus completeness (folder-derived)
- New **§3 "EDGAR Earnings Filings"** card on the Data Ingest page (`/finviz`)
  showing whether each ticker's SEC 10-Q/10-K history is complete. The corpus is
  fetched by the Nous agent and stored on **AI-Hermes** (the Windows file server,
  192.168.1.162, `C:\HermesSync\MarketResearch\QuarterlyReport`). The web app
  can't read that box, so — like the parquet `IngestHealth` pattern — AI-Hermes
  **pushes** a per-ticker report.
- **Folder-derived, no DB of "what should exist"** (user, 2026-06-09: "we do not
  need a db to keep track what is not there… run the folder, you'll see which
  quarter is missing"). Each fiscal year has 3 ten-Q quarters + a ten-K, so the
  reporter reads the present quarters from the filenames and lists the **missing
  quarters** directly (handles non-calendar filers via each ticker's regular
  filing slots). Statuses: **COMPLETE / GAPS** (holes in the cadence — the
  missing `YYYY-Qn` are listed) **/ STUB** (filings present but no usable
  Markdown body — `.md` absent or an empty stub). Aggregate also surfaces "no
  10-K seeded".
- Files: `app/models.py` `EdgarIngestHealth` (+ migration
  `alembic/versions/d0e1f2a3b4c5_edgar_ingest_health.py`, off head `c9d0e1f2a3b4`),
  `app/routes/api.py` `POST /api/ingest/edgar` (API-key gated, upsert per host),
  `app/services/edgar_health.py` (COMPLETE/GAPS/STUB + aggregate; renders only
  the actionable rows so 700+ tickers stay usable), `app/routes/finviz.py`
  (fetch latest row → display), `app/templates/finviz.html` (the card; the old
  §3 Ticker Profile renumbered §4), and the AI-Hermes reporter
  `deploy/report_edgar_health.py` (stdlib-only folder scan — no network, no DB).
- Verified on the real corpus (735 tickers): **4 with genuine quarter gaps**
  (e.g. AMD missing 2017-Q1 & 2023-Q1 — confirmed absent on disk), **731 stub-MD**
  (the prototype's empty-`.md` bug), **735 with no 10-K** (the prototype fetched
  only 10-Qs). Classifier across all statuses; template renders (populated +
  empty); migration up/down/up round-trip; endpoint upsert + display integration.

### 2026-06-07 — Data Ingest §3 renamed "Nightly pipeline" → "Ticker Profile"
- The §3 section is now titled **Ticker Profile** (it builds the per-ticker
  swing/trend & intraday profiles), with a clearer subtitle and the action button
  relabelled **Run regen → Run update**. Empty-state + "started" banner reworded
  to "profile update". Pure copy/label change — the `/pipeline/regen` endpoint,
  manifests, and kinds (swing-first) are unchanged.

### 2026-06-07 — Price Data History: show the newest-bar DATE (not just "N ago")
- §2 "Last write" column → **"Last bar (date)"**, showing the actual date of the
  newest bar per timeframe (daily → `2026-06-05`; intraday → `2026-06-05 19:57
  ET`), with the relative age kept as a small secondary. Header relabelled
  "Last ingest write" → "Newest data". So freshness is read as a date, not a
  relative age the user has to translate.
- The date comes from the reporter's new per-timeframe `newest_bar` field
  (`report_ingest_health._newest_label`: daily uses the stored UTC date so a
  UTC-midnight daily stamp shows the right session day; intraday converts to ET).
  `report_to_display` passes it through; the file-mtime local read sends `None`
  (it can't know the bar date) and the column falls back to the age.

### 2026-06-06 — v2.81: Data Ingest freshness + universe health + swing-first pipeline
Release marker for today's Data Ingest batch (all detailed below): per-timeframe
DATA freshness via the Hermes reporter, the per-category Universe-health table,
correct 3min/5min/daily timeframes, and the swing-first nightly pipeline. Bumped
`app/__init__.py` 2.80 → 2.81 so `/status` confirms the deploy.

### 2026-06-06 — Price Data History: correct timeframes + true data freshness
- `services/ingest_health.py`: `_TIMEFRAMES` was `1min/5min/15min/daily` but the
  seed + supervisor only pull **3min/5min/daily**. So §2 showed alarming
  "1min never / 15min never" rows and hid the real 3min row. Fixed to
  `(3min, 5min, daily)`.
- The local read still only knows the **file write time** ("just now"), not how
  fresh the *data* is. The fix for that lives on the ingest side:
  `scripts/report_ingest_health.py` (new) reads the newest-bar epoch per
  timeframe on Hermes and POSTs it here; `report_to_display()` (already present)
  renders "newest <age> ago" per timeframe, and the Data Ingest route prefers
  that pushed row over the file-mtime read. No dashboard code change was needed
  for the freshness — only the reporter that feeds the existing pushed-report
  path. (Scope rule preserved: the dashboard never opens a parquet.)
- §2 now also renders a **Universe health by category** table — per index
  (S&P 500 / 400 / 600 / NASDAQ-100 / Other / All seeded): members, seeded,
  missing (completeness), and a per-timeframe fresh/stale cell (✓ or "N stale",
  tier-coloured, with worst-lag in the tooltip). Carried in the pushed report's
  new `universe_health` field (`report_to_display` passes it through; the local
  read sends `[]`). "Stale" is cohort-relative (lagging the freshest peer / no
  file) so a closed-market weekend flags nobody. Memberships overlap
  (NASDAQ-100 ⊂ S&P 500) — noted in the UI.
- §3 regen form now defaults to **swing** (this is the trend & swing site), and
  the supervisor's nightly regen runs `both` so manifests carry
  `profiles_swing` (not just `profiles_intraday`). `pipeline_kinds` reordered
  swing-first; `POST /pipeline/regen` default kind = swing.

### 2026-06-06 — pipeline report folded into the Data Ingest page

The standalone `/pipeline` page was removed; its report + regen triggers now live
as **§3 "Nightly pipeline"** on the **Data Ingest** (`/finviz`) page (the natural
home alongside the parquet-health §2). The `/finviz` route adds `pipeline_runs.
list_runs()` to its context; `POST /pipeline/regen` now redirects to `/finviz`;
`/api/pipeline-runs` + the `/profile` page/API are unchanged. `pipeline.html`
deleted, 'Pipeline' nav item removed.

### 2026-06-06 — `/profile` page: per-ticker Swing/Trend profile viewer

New **Profile** nav page — type a ticker, see its **swing/trend** profile (the
intraday one stays in `dashboard_intraday`). `services/profiles.py` reads
`<data_root>/swing_profile/<T>.json` (file-read, no resources import) and
`display_rows()` flattens it into cells so the template is a simple loop.
`routes/pipeline.py`: `GET /profile?ticker=` (page) + `GET /api/profile/{ticker}`
(JSON for the Nous agent). Shows daily+weekly trend badges, EMA structure, 52w
position, ATR, base/accum, pullback, momentum, RS percentile, + analyst/earnings
slots (from MATP later). NOTE: context var is `prof` not `p` — `base.html`
shadows `p`, which silently blanked every `p.*` access.

### 2026-06-06 — `/pipeline` page: nightly ingest→deep-check→profiles report + regen triggers

New **Pipeline** nav page surfacing the overnight work on Hermes so freshness is
checkable from the web (no Hermes login):
- `services/pipeline_runs.py` — reads the per-run manifests the ingest supervisor
  + regen runner write to `<data_root>/pipeline_runs/run_*.json` (file-read only,
  like `ingest_health`; data_root derived from `TST_PRICE_HISTORY_DIR`'s parent).
- `routes/pipeline.py` — `GET /pipeline` (page), `GET /api/pipeline-runs` (JSON for
  the Nous agent), `POST /pipeline/regen` (moderator). Each run renders as a card
  with per-phase badges (ingest bars/pairs, deep-check corrupt/flagged/stale,
  profiles +written/-skipped) coloured by status + freshness tier.
- **Regen triggers** (moderator-only panel): pick `intraday|swing|both`, blank
  tickers = full universe (~7 min / ~1 min) or list tickers for an **ad-hoc
  per-ticker** run. Spawns `scripts/regen_profiles.py … --manifest` DETACHED with
  the data-science `py -3.12` (not the uvicorn venv), so it never ties up the web
  worker; the run drops a manifest that appears on the same page.
- Wired in `main.py` (router + version global) + nav link in `base.html`.

### 2026-06-01 — v2.80: rename Data Ingest §2 → "Price Data History"

- Renamed the Data Ingest page's section 2 from "Parquet Ingest" to **"Price Data
  History"** (heading + subtitle); behaviour unchanged.

### 2026-06-01 — v2.79: delete is admin-only + study numbering surfaced

- **Delete study is now admin-only** (`require_admin`; moderators can't, button
  hidden for them) — confirm spells out it keeps the Discord thread.
- **Study number surfaced consistently**: the middle status panel leads with
  `SYMBOL #id` (e.g. `AMZN #5`); left cards already show `#id`; the Discord thread
  is `SYMBOL — study #id` — same number identifies a study everywhere.

### 2026-06-01 — v2.78: wider chatroom + smaller chat font

- Study chatroom (right panel) widened to **1/4 of the screen** (`lg:w-80` →
  `lg:w-1/4`); chat bubbles + composer dropped to **text-xs** for density.

### 2026-06-01 — v2.77: ingest health via a pushed report (reporter cron)

- The parquet-ingest health is now **pushed** by a Hermes-side reporter, not read
  over the network. New `deploy/report_ingest_health.py` (stdlib-only) stats the
  bars store + tails `ingest_log.jsonl` and POSTs a report to **`POST
  /api/ingest/health`** (X-API-Key); run it from a Windows scheduled task (the
  "cron"). New `IngestHealth` model + migration `c9d0e1f2a3b4`; the Data Ingest
  page shows the latest report (per-timeframe freshness + **"reported X ago"** so
  a dead reporter is obvious + a recent ingest-log tail). Falls back to the local
  filesystem read (v2.76) when no report has been received yet.

### 2026-06-01 — v2.76: "Finviz" → "Data Ingest" (filters + parquet health)

- Renamed the **Finviz** nav/page to **Data Ingest**, now two sections:
  **1 · Finviz Filters** (the existing saved-filter manager) and **2 · Parquet
  Ingest** — a read-only **health/freshness** view of the bars store (per
  timeframe: # symbols, last-write "X ago" with green/amber/red tiers, size; an
  overall freshness pill). The ingest itself still runs on Hermes (unchanged);
  this is just the daily eyeball. New `services/ingest_health.py` (stats **file
  metadata only** — no parquet content reads, per the backtest-only scope rule)
  + `TST_PRICE_HISTORY_DIR` config. (Route prefix stays `/finviz`.)

### 2026-06-01 — v2.75: members always see (read-only) trade levels

- The study trade-levels panel now **always renders for members** (was only shown
  when a level was set), as a read-only row (Support/Resist/Entry/Stop/Target/R:R,
  "—" for unset) with **no inputs** — members view the curator's plan but can't
  edit it. The editable form stays moderator-only.

### 2026-06-01 — v2.74: web → Discord (two-way chat bridge)

- Sending from the web chatroom now **posts into the study's Discord thread** via
  the bot (`Name: message`), so it shows in Discord / the phone app, and reads
  back into the web chat (no platform Comment stored → no duplicate). Falls back
  to a plain on-platform comment when there's no thread or the bot post fails, so
  nothing is lost. New `discord.post_thread_message()`. Requires the bot
  configured + a thread on the study + Send Messages in Threads.

### 2026-06-01 — v2.73: chat timestamps in the viewer's local time

- Chat times were shown in UTC (Discord + stored comments are UTC), so they read
  8h off in MYT. Each message now carries a UTC-marked ISO string and the browser
  renders it in the viewer's local time via `toLocaleTimeString` (falls back to
  the server UTC HH:MM with no JS).

### 2026-06-01 — v2.72: chatroom comment posts in-place (HTMX)

- A member's comment now appears in the chatroom **instantly**: the composer
  `hx-post`s and swaps the refreshed merged-chat fragment into `#chatbox`
  (input resets, auto-scrolls). Previously it did a full-page reload whose chat
  fragment could serve from browser cache, so the new comment didn't show.
  `add_comment` returns the chat fragment for HX requests (full-page redirect as
  the no-JS fallback); chat render refactored into `_render_chat`.

### 2026-06-01 — v2.71: Studies = 3-panel app (cards · chart+levels · chatroom)

- Redesigned Studies into a single-screen **3-panel** layout, replacing the
  separate list + detail pages: **left** = curated-ticker cards (symbol, #id,
  status dot, R:R, 💬 count; current highlighted; "+ Curate" inline), **middle**
  = chart (fills) + status controls + trade levels, **right** = a **chatroom**
  merging the study's **Discord thread + on-platform comments** (time-sorted
  bubbles, source-tagged, self-polling, auto-scroll, composer posts a web comment,
  "Reply in Discord ↗").
- **`/studies` now lands straight on a study's chart** (newest open one) — no
  list page. `/studies/{id}` selects a study; clicking a left card is a full page
  load (the chart re-inits cleanly). New `_page_ctx` helper + `GET
  /studies/{id}/chat` (merged feed). `study_detail.html` removed (its content
  folded into the 3-panel `studies.html`).

### 2026-06-01 — v2.70: crop the chart header gap (single-quote widget)

- The TradingView single-quote header rendered tall with empty space below the
  quote, leaving a big gap before the chart. Height-capped the header to ~5.5rem
  + overflow-hidden so the blank space is cropped, and trimmed the chart panel's
  top padding. Shared `_price_chart.html`, so it fixes the gap on **both** the
  MATP board chart and the Studies chart. (Height tunable if the price clips.)

### 2026-06-01 — v2.69: basket mini-card strip expands on hover

- The study page's curation-basket strip stays one compact row (extra cards
  clipped); **hovering expands the full list** as a wrapped floating overlay
  (absolute-positioned, so the chart below never shifts). Pure CSS (`group` +
  `group-hover`).

### 2026-06-01 — v2.68: study page — basket mini-cards, no title, half-screen split

- Added a **curation-basket mini-card strip** on top of the study page: small
  clickable cards of the other OPEN studies (symbol + Rn:R, current highlighted)
  for quick switching; route now passes a lightweight `basket`.
- **Removed the symbol/title header** (the chart's TradingView header already
  shows it); the study's descriptive title now sits small in the status panel.
- **Half-screen split:** chart (now ~26vh) → status → trade levels are the fixed
  top; the **Discord + platform discussion scroll in the bottom half**.

### 2026-06-01 — v2.67: Studies list = single-screen

- The Studies list page is now single-screen (desktop): header + the "Curate a
  ticker" form are a **fixed top**, and the **Curation basket + discussion cards
  scroll within one screen** (`main_class` override + `lg:overflow-hidden` outer
  / `lg:overflow-y-auto` inner). Mobile falls back to normal page scroll.

### 2026-06-01 — v2.66: study page = fixed chart/status/levels, scrolling messages

- Restructured the study detail page into a **single-screen layout** (desktop):
  a **fixed top zone** — slim header → **chart** → **status panel** (data summary
  + moderator status controls, merged) → **trade levels** — that does **not
  scroll**, with the **messages** (rationale + Discord discussion + on-platform
  discussion) in a **scrolling region** below. The chart is shortened
  (`lg:h-[32vh]`) so the fixed zone fits; mobile falls back to normal page scroll
  (`main_class` override + `lg:overflow-hidden` / inner `lg:overflow-y-auto`).

### 2026-06-01 — v2.65: Curation basket (watchlist) on the Studies page

- Added a **"Curation basket"** at the top of `/studies`: every OPEN (non-closed)
  study as a compact, scannable row — live **Price** (red if above MBP) + **Signal**,
  **MBP**, the trade plan (**Entry/Stop/Target**) and **R:R** (colour-coded), each
  row clicking through to the study. New `GET /studies/basket` HTMX fragment
  (`_studies_basket.html`), lazy-loaded so the live price fetch (reuses MATP's
  cached `_watchlist_signals`) never blocks the page. Declared before `/{sid}`
  so the literal route wins. The existing discussion cards remain below.

### 2026-06-01 — v2.64: render Discord embeds in the discussion panel

- The discussion panel showed "(embed / attachment)" for any message without
  plain-text content — including the bot's own opening study **embed**.
  `fetch_thread_messages` now flattens each message's embeds (title +
  description + fields) to text, and the panel renders that; genuine attachments
  show "(attachment)", and a truly-empty message hints to enable the Message
  Content intent (human text comes back blank without it).

### 2026-06-01 — v2.63: per-study Discord discussion shown on the study page

- **Discord discussion now appears on the study page.** Webhooks are write-only,
  so this adds a **bot** read path: on curate, a Discord **thread** is created per
  study (id stored on `Setup.discord_thread_id`, migration `b8c9d0e1f2a3`), and
  the study page reads that thread's messages via the bot API and renders them in
  a "Discord discussion" panel that self-polls (15s) so new replies appear. A
  "Reply in Discord ↗" deep-link shows when a guild id is set; moderators get a
  "Start Discord thread" button for studies curated before the bot existed.
- New `discord.bot_configured()` / `create_study_thread()` / `fetch_thread_messages()`
  / `thread_link()`; `GET /studies/{id}/discord` (fragment) + `POST
  /studies/{id}/discord-thread`. Config: `TST_DISCORD_BOT_TOKEN`,
  `TST_DISCORD_CHANNEL_ID`, optional `TST_DISCORD_GUILD_ID`. All soft-fail — with
  no bot set, the panel shows "not configured" and the webhook doorbell is
  unaffected.

### 2026-06-01 — v2.62: Curate from MATP + study trade levels (S/R, entry, stop, R:R)

- **"Curate" button on the MATP chart** (board + detail, moderators): one click
  on a tradable ticker creates (or reopens) a study and jumps to it. New
  `POST /studies/curate` (reuses any non-closed study for the symbol instead of
  duplicating). Gated by a `chart_curatable` flag so it shows on MATP but not on
  the study page itself.
- **Study = where the curator determines the trade.** Study detail now has an
  editable **Trade levels** form (moderators): **Support, Resistance, Entry,
  Stop, Target**, with **Risk:Reward** derived live = (target−entry)/(entry−stop),
  colour-coded (≥2 green / ≥1 amber / <1 red). Members see it read-only. New
  `support`/`resistance` columns on `Setup` (Alembic migration
  `a7b8c9d0e1f2`, auto-applied on startup) + `POST /studies/{id}/levels`.
- **Support/Resistance drawn on the study chart** as solid horizontal lines
  (S = sky, R = fuchsia), alongside the dashed MATP/MBP lines, and kept inside
  the autoscaled price range.

- The **/studies** page is now the **curate → discuss** surface. A moderator
  curates a ticker (symbol + title + rationale + optional entry/stop/target);
  it opens in **discussing** and members discuss via comments; status moves
  `draft → discussing → agreed → closed`. Reuses the existing `Setup`+`Comment`
  models — **no migration**. One study == one curated ticker.
- New routes: `GET /studies` (cards: symbol, MATP/MBP, status, comment count),
  `POST /studies` (curate, moderators), `GET /studies/{id}` (detail + the MATP
  price chart + curator thesis + discussion thread), `POST /studies/{id}/comment`
  (members), `POST /studies/{id}/status` + `/delete` (moderators).
- **Discord doorbell:** creating a study posts the rich ticker embed to the
  channel ("📋 New study · SYM", linked to `/studies/{id}`), soft-fail.
- Templates: rewrote `studies.html` (curate form + list), new `study_detail.html`
  (reuses `_price_chart.html` for the chart).

### 2026-06-01 — v2.60: Discord — rich embeds + manual "Share to Discord"

- **Richer Discord embeds.** New shared `discord.build_ticker_embed()` builds one
  consistent embed for a ticker — Price, MATP, MBP (with ✅ at/below or ⛔ above
  the live price), Signal, Next earnings (+ countdown), Last earnings — coloured
  by signal (HOT=rose, WARM=amber, else emerald), titled + linked to `/matp?symbol=`.
- **Manual "Share to Discord"** button on the ticker detail page (moderators,
  shown only when a webhook is configured): `POST /matp/{symbol}/share-discord`
  posts that pick on demand (HTMX result inline). Synchronous + soft-fail.
- **Auto MATP-refresh post upgraded** to the same rich embed, now run as a proper
  background task (`_notify_refresh_done`, own DB session) so the agent's status
  call returns immediately while price/earnings are fetched for the post.
- (Still queued: extra auto-triggers — agent stale/online, new signal.)

### 2026-06-01 — v2.59: earnings markers on the date axis + chart cleanup

- **Earnings "E" markers now anchor to the date axis.** Both past and upcoming
  earnings sit at the bottom (`belowBar`) as up-arrows pointing at their date on
  the time axis, instead of one floating `belowBar` circle + one `aboveBar`
  down-arrow in the price area.
- **Earnings legend wording** consolidated to `· earnings: last <date> · next
  <date> (… from now)` (was "last earnings …" + "next earnings …").
- **Removed the TradingView symbol-info header widget** from the chart panel —
  it forced an "Upcoming Earnings / EPS / Market Cap / P/E" row with no option to
  hide it; the chart panel now starts at the zoom toolbar. Dropped the now-unused
  `tv_symbol`.

### 2026-06-01 — v2.58: on-chart earnings "E" markers (overlay)

- Added earnings markers **on** the candle chart (lightweight-charts has no
  built-in earnings overlay like full TradingView): the **past** earnings shows
  a circle "E" below its bar; the **upcoming** earnings shows an "E" out in the
  right blank margin — the time axis is extended with whitespace points so the
  future date has a slot to pin the marker to. Wrapped in try/catch so a marker
  failure can never break the candles. `_price_chart.html` only.
- NOTE: needs an eyeball after deploy (the whitespace-extension + future marker
  can't be browser-verified from the laptop).

### 2026-06-01 — v2.57: next earnings on the chart (date + countdown)

- The price chart now shows the **next/upcoming earnings** in the legend:
  "· next earnings 2026-08-26 (~12 weeks from now)" (countdown is `today` /
  `tomorrow` / `N days from now` / `~M weeks from now`). The existing earnings
  label is relabelled **"last earnings"** for clarity.
- Source: **live Yahoo `calendarEvents`** via a new `fetch_next_earnings()` in
  `services/prices.py` — does Yahoo's cookie+crumb handshake (cached ~1h, reused
  across symbols), per-symbol result cached ~6h, and **soft-fails to None** so
  the chart just omits the line if unavailable. Returned from the existing
  `/matp/{symbol}/prices` endpoint (no extra round-trip) and rendered by the
  chart script. (On-chart vertical earnings marker is a possible follow-up.)

### 2026-06-01 — v2.56: /agent page — agent identity as heading, crons below

- Restructured the Nous Hermes `/agent` page: the **agent name + liveness is now
  the page heading** (with a blinking dot when online), the live "working now"
  panel sits right under it, and the **"Working crons" are listed below** —
  instead of a generic "Agent status" title with the agent buried in a card
  header. Single-agent-friendly; the working-now panel renders once (`loop.first`).

### 2026-06-01 — v2.55: Discord notifications (outbound webhook) — MATP refresh

- **First Discord integration: outbound webhook.** When a MATP refresh request
  completes (`POST /api/refresh-queue/{id}/status` → `done`), TradeHunter posts a
  summary embed to a Discord channel — ticker scope shows MATP/MBP + a link to
  `/matp?symbol=…`; filter scope shows the filter + names-updated count + a link
  to `/matp?wl=…`. Outbound-only (no bot, no inbound), fired via FastAPI
  `BackgroundTasks` so it never adds latency, and **soft-fail** (no webhook or a
  network error is logged, never breaks the request).
- New `app/services/discord.py` (`post_embed`, `configured`); config
  `TST_DISCORD_WEBHOOK_URL` + `TST_PUBLIC_URL` (for click-through links);
  documented in `app/.env.example`.
- **Admin → Integrations** card shows webhook status + a **"Send test post"**
  button (`POST /admin/discord-test`, HTMX) so an admin can verify the webhook
  without waiting for a real refresh.
- Off by default: with no `TST_DISCORD_WEBHOOK_URL` set, nothing is posted.

### 2026-06-01 — v2.54: remove redundant "open TradingView chart" link

- Removed the "· open TradingView chart →" link from the chart legend — clicking
  the ticker name in the TradingView header widget opens it anyway, so the extra
  link was redundant. `_price_chart.html` only.

### 2026-06-01 — v2.53: chart zoom presets 6M/1Y/1.5Y/2Y + right-third blank

- Price chart zoom replaced 7M/All with **6M · 1Y · 1.5Y · 2Y** presets (default
  **6M**). Each shows the lookback in the **left ~2/3** of the chart with the
  **right ~1/3 left blank** (room ahead of current price): we set the visible
  *logical* range from the lookback's first bar to `lastBar + dataBars/2`, so
  data = 2/3 and blank = 1/3 of the width (current candle lands at ~2/3).
  `_price_chart.html` only.

### 2026-06-01 — v2.52: watchlist gridview — aligned columns + smaller fonts

- **Header now aligns with the rows.** The watchlist header and every row share
  one inline `grid-template-columns` (fixed widths) in `ticker_grid`, so columns
  line up pixel-for-pixel — the old drift came from each being a separate `auto`
  grid that sized independently.
- **Smaller Sym/Trend/Sig fonts** (`text-[11px]` / `text-[10px]`; header
  `text-[9px]`; `signal_badge` shrunk to `text-[10px]` + tighter padding) to fit
  the six columns cleanly. Price/MATP/MBP stay at `text-[11px]`.
- **Watchlist sidebar widened** `w-72 → w-80` so the 6-column grid isn't cramped.

### 2026-06-01 — v2.51: members see Nous Hermes + watchlist MATP column

- **Nous Hermes is now visible to all approved members** (was moderator-only):
  the top-bar pill, the `/agent` page, and the live "working now" panel
  (`/agent`, `/agent/pill`, `/agent/active`) dropped from `require_moderator` to
  `require_user`, and the pill renders for every logged-in member. View-only —
  the matp-board retry control stays moderator-gated.
- **Watchlist shows Price · MATP · MBP, all 2-decimal.** Added a MATP column to
  `ticker_grid` (orange), and Price/MATP/MBP now format `%.2f` instead of whole
  numbers (MBP green, price white/red-if-above-MBP). "Dropped" list MBP also 2dp.

### 2026-06-01 — v2.50: exact MBP/MATP in the analyst band

- The analyst band's **MBP/MATP labels** now show the exact stored 2-decimal
  value (`%.0f` → `%.2f`). MBP is a max-buy threshold stored as
  `round(matp/1.15, 2)`; the old whole-number label rounded to nearest, so a true
  100.60 displayed as `101` — *above* the real cap. Now exact, consistent with
  the detail page + chart legend. (Watchlist grid/rail still show whole-number
  MBP — separate, not yet changed.)

### 2026-06-01 — v2.49: pill blink fix + band marker thickness

- **Pill now blinks on any live MATP run (queued OR running), not just
  `running`.** Keying on `status='running'` never lit up in practice — the
  agent's running-status POST is gated by the terminal-tool approval, so runs
  sit in `pending`. The pill now blinks as soon as a refresh is queued (and
  stops when it finishes or goes stale); idle poll trimmed 20s → 15s.
  `/agent/pill` reuses `_active_run_items` for the `working` flag.
- **Analyst-band MBP/MATP lines thickened to match the price line** (`w-px` →
  `w-0.5`), so all three markers read at the same weight.

### 2026-06-01 — v2.48: live "working now" panel + moving progress bar on /agent

- The **/agent** (Nous Hermes) page now shows what the agent is **processing
  right now**: each active MATP refresh request (pending/running) rendered with a
  **moving progress bar** (width = `progress_done/progress_total`, `transition-all`
  animates it as the count climbs). New `GET /agent/active` HTMX fragment +
  `_agent_runs.html`; `agent.html` loads it on open into `#agent-active`.
- **Refreshes periodically, never goes silent.** The fragment self-polls on an
  adaptive cadence — 4s while a run is *running* (so the bar visibly moves), 8s
  while merely *queued*, and a 15s idle heartbeat when nothing is running so a
  **newly-queued run appears without a manual page reload** (the prior matp-board
  panel went fully quiet when idle, which is why a fresh run looked frozen).
- Queued / not-yet-claimed runs show an indeterminate pulsing bar; stale runs
  (pending >15m, or running >8m with no progress) turn the bar red with a "the
  agent may be stuck/ not polling" note that links to the liveness above.
- Pairs with nous_hermes `matp_status.sh` (agent-side progress reporter) so the
  bar has real data to move with. Bump `app/__init__.py` 2.47 → 2.48.
- **MATP watchlist selection now persists.** Clicking a ticker used to drop the
  `?wl=` (the links were `/matp?symbol=X`), so picking "Selective Tickers" then a
  ticker bounced you back to "All". Ticker links (and the "dropped" link) now
  carry `wl`, the `ticker_grid`/`watchlist_rail` macros take a `wl` arg, and the
  selection is also saved to `localStorage` + restored on any `/matp` load that
  arrives without `?wl=` (redirects after a run, bare reloads). `matp_watchlist`
  now passes `sel_wl` to `_watchlist.html`.
- **Top-bar tweaks:** the **selected** nav item (MATP/Studies/Finviz/Admin) is
  now **orange** (`bg-orange-500/15 text-orange-300`) instead of green; removed
  the redundant **Agent** nav item (the Nous Hermes pill already links to
  `/agent`); the **"Nous Hermes"** pill label is now
  **white**, and its liveness dot **blinks while the agent has a live MATP run
  (queued OR running, not stale)**, static otherwise (`.th-blink` keyframe in
  base.html). Originally keyed on `status='running'` only, but that never lights
  up in practice — the agent's running-status POST is gated by the terminal-tool
  approval, so runs sit in `pending`; keying on any live open run makes the pill
  blink as soon as a refresh is requested. The pill self-polls adaptively — 5s
  working, 15s idle — replacing the old fixed 60s refresh; `/agent/pill` returns
  a `working` flag.
- **Analyst targets sorted relevant-and-high first.** `_ticker_targets` now
  orders the included (post-earnings, MATP-counting) targets first, then the
  dropped ones, each group by target price descending — so the targets table
  (modal + detail page) leads with the relevant, highest targets instead of
  newest-issue-date order.
- **Analyst band redesign — per-analyst lines instead of a heatmap.** The band
  bar is now ~half height, and each post-earnings analyst target is drawn as its
  own thin vertical line (positioned by price) rather than a coloured consensus
  heatmap — where lines bunch up you read the concentration directly. Each line
  has a wider transparent hover target; **mousing over shows the brokerage +
  price** (and brightens/thickens the line). `_build_band` now takes
  `analysts=[{brokerage, price}]` and emits `band["lines"]` (dropped `bins` /
  `consensus_*`); both callers pass the included targets with brokerage.
- **Price chart 7M / All zoom.** Added a **7M / All** toggle to the price chart.
  Default is **All** (`fitContent()`, full ~2y); **7M** zooms to the last ~7
  months so current price action fills the view — `setVisibleRange` computes a
  real calendar range off the `YYYY-MM-DD` bar times (clamped to the feed start).
  `_price_chart.html` only.

### 2026-06-01 — v2.47: "Nous Hermes" heartbeat pill in the top bar

- A **Nous Hermes** pill now sits in the top nav, just **left of the user
  icon** (moderators/admins): a liveness dot (green online / red stale / grey no
  heartbeat) + label, self-refreshing every 60s, click-through to `/agent`. New
  `GET /agent/pill` fragment + `_agent_pill.html`. base.html places it with
  `ml-auto` (user menu drops its own `ml-auto` when the pill is present).

### 2026-06-01 — v2.46: don't churn during a run; reload the board once when it finishes

- The runs panel no longer animates/flickers while a run is in progress. It
  shows a **calm static status** (no pulsing bar) and polls at a slower cadence
  (running 10s, pending 20s) **only to notice completion**.
- When a watched run **finishes**, the panel reloads the **watchlist board
  exactly once** (`#wlbox`) so the new MATP data appears — instead of the board
  never updating or the panel refreshing continuously. Mechanism: a `poll=1`
  flag distinguishes a self-poll from the initial load; an empty poll result =
  "a run just finished → reload the board", carrying `wl`/`sym` so the right
  watchlist is reloaded. Retry button forwards `wl`/`sym` too.

### 2026-06-01 — v2.45: active-runs panel moved onto the watchlist column

- The "Active MATP runs" panel no longer spans the full page width above the
  board. It now sits **on top of the watchlist column** (the narrow `w-72`
  aside), so it doesn't push into / overlap the chart section. Single `#runsbox`
  relocated inside the aside (desktop); self-poll/retry behaviour unchanged.

### 2026-06-01 — v2.44: /agent shows each cron's FULL prompt (not the truncated name)

- Heartbeat gains a structured `cron_jobs` array `[{id,schedule,skills,prompt,
  next_run,active}]` (new `AgentHeartbeat.cron_jobs` JSON column, migration
  `f6a7b8c9d0e1`). The `/agent` page now renders one card per cron — schedule +
  skill + next-run badges and the **full prompt the agent runs** — so you can see
  what each job actually does. Falls back to the raw `hermes cron list` text when
  the agent is on an older skill (no structured jobs).
- Pairs with agent skill **matp v1.7.0** (sends `cron_jobs`).

### 2026-06-01 — v2.43: adaptive run-panel polling (stop hammering when stale)

- The active-runs panel no longer polls `/matp/runs` every 5s **forever**. It
  now **self-polls adaptively**: 5s while a run is actively *running*, 10s while
  a fresh request is *pending*, and **stops entirely once everything is stale**
  (agent likely not polling — re-rendering forever is pointless and annoying).
  Header shows "· auto-refresh paused"; the manual ↻ refresh / ↻ ask-agent-to-retry
  buttons resume it (retry resets the wait clock, so polling restarts).
- Mechanism: outer `#runsbox` kicks off once (`hx-trigger="load"`); each returned
  fragment schedules the next poll via `hx-trigger="load delay:{poll_in}s"` only
  when `poll_in > 0`. Cadence computed in `_runs_context()`.

### 2026-06-01 — v2.42: "ask agent to retry" on stuck runs + price in band wording

- **Retry button** on the active-runs panel: when a request goes **stale**
  (pending >15m, or running >8m with no progress), moderators see "↻ ask agent
  to retry" → `POST /matp/runs/{rid}/retry` re-queues it (clears the claim +
  progress, resets the wait clock) so the agent re-claims it on its next poll.
  Honest by design — if *nothing* is polling, re-queueing can't help; the stale
  note now links to the **/agent** page to check the cron. (`_open_run_items`
  helper shared by the poll + retry routes.)
- **Band wording** now shows the actual values inline: "MBP 254.35 / MATP 292.50
  / price 268.10 shown to scale" (was just the labels).

### 2026-06-01 — v2.41: Agent status page (Nous Hermes liveness + crontab)

- New **Agent** top-nav item (moderators/admins) → `/agent`: shows whether the
  Nous Hermes agent is **online/stale**, its **version**, last check-in time,
  and the **literal crontab lines** it's running. Answers "what cron is the
  agent running?" from the web UI — no SSH.
- Architecture-preserving: the agent stays **outbound-only**. New machine
  endpoint `POST /api/agent/heartbeat` (X-API-Key) that the agent calls on each
  poll with `{agent, version, host, crons, polled_at}`; upserted one row per
  agent (`AgentHeartbeat` model, migration `e5f6a7b8c9d0`). The dashboard never
  reaches into the Linux box.
- New `routes/agent.py` (`require_moderator`), `templates/agent.html`; online if
  the last heartbeat is within 25 min (poll is ~10 min). Comment lines in the
  crontab are filtered from display.
- Pairs with agent skill **matp v1.6.0**, which now POSTs the heartbeat first
  thing on every poll (see `nous_hermes/`).

### 2026-05-31 — v2.40: brighten MBP chart line a shade

- MBP chart price line + legend swatch `#15803d` → `#16a34a` (green-600) — still
  dark green, more legible on the dark chart, distinct from the EMA50 green.

### 2026-05-31 — v2.39: colour tweaks (MATP orange, MBP dark green, white watchlist text)

- **MATP** is now **orange** (`#f97316`) — chart price line, band marker/label, legend.
- **MBP** is now **dark green** (`#15803d` / green-600) — chart price line, band marker/label, legend.
- **Watchlist text → white** (ticker symbol + price columns) for readability.

### 2026-05-31 — v2.38: watchlist All / Selective Tickers / Disqualified split; smaller chart heading

- **Watchlist selector** gains **All** (every ticker in the DB) and **Selective Tickers** (ad-hoc tickers with no Finviz filter), plus each filter. `?wl=all|individual|<id>`; default is **All**. The **Run** button only shows for a specific filter (not All/Selective).
- **Removed the watchlist group title** (the dropdown already names the selection); the list is the sorted ticker grid. Added a **Price** column (live).
- **Disqualified section** — tickers whose **live price > MBP** (you shouldn't buy above the max-buy price) drop into a collapsible **"Disqualified · Price > MBP"** section; the rest stay in the main (qualified) list.
- **Finviz schedule note:** clarified on the page that scheduling **never creates agent crons** — intervals live in the dashboard (updated in place), the agent runs one poll cron; shows the one-time cron command.
- **Chart heading (TradingView widget) scaled to ~60%** (fixed 5rem header) so the flex-fill price chart gets more height.

### 2026-05-31 — v2.37: runtime Trend/Signal (lazy, non-blocking) + band extends past range

- **Watchlist Trend/Signal are now RUNTIME-detected** (from `resources.patterns` on live daily bars), but **lazy-loaded so the dashboard never freezes**: the page renders instantly, then the watchlist grid loads via one HTMX request (`GET /matp/watchlist`) that computes signals in a **bounded thread pool (8) with a 15-min per-symbol cache**. Trend (up/down/sideways) + a bounce-style Signal (HOT/WARM/WATCHING) per ticker; falls back to the stored value if a live calc fails. Macros moved to `_wl_macros.html` + fragment `_watchlist.html`.
- **Analyst band extends past the analyst range** to include the current price: the bar's display range now spans `min(low,price,mbp) … max(high,price,matp)` (+pad), so an **out-of-range price is shown proportionately** (distance to scale) instead of clamped at the edge. The analyst low–high is a shaded sub-segment; bins/MBP/MATP/price are absolute-positioned within the display range. `_build_band` reworked; verified price-below / price-above / price-inside all position correctly.

### 2026-05-31 — v2.36: scale to 110% + hover-only scrollbars

- Dropped the global scale from 130% → **110%** (`html { font-size: 110% }`).
- **Scrollbars are now invisible by default and fade in on hover** of the scroll area (thin `scrollbar-width`, transparent thumb → visible on `:hover`, both WebKit and Firefox).

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
