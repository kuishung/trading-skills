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
