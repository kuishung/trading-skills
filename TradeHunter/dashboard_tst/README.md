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
