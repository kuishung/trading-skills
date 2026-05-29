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
  invite-only admin bootstrap, `/health`, `/`), `routes/` (`auth`,
  `studies`, admin-only `admin` with the **control-plane-only** swing-bot
  stubs), `services/` (`black_scholes.py` — working pure-math option
  pricing/prob-ITM; `resources_bridge.py` — import seam to the shared
  `resources/`/`review/` library, Phase-tagged stubs), `templates/` +
  `static/`, `requirements.txt`, `.env.example`. Deps are per-PC
  (gitignored); the SQLite db (`*.db`) and `.env` are gitignored.
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
