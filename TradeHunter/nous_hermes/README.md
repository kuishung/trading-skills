# nous_hermes/ — Nous Hermes agent project

All work for the **Nous Research Hermes agent** (https://hermes-agent.nousresearch.com/)
lives here. The agent itself runs on a separate **Linux server** (clean install,
CLI + SSH); this folder is the source of truth that gets deployed there.

> **Naming convention:** this folder is `nous_hermes` (not `hermes`) on purpose,
> to disambiguate from the R720 Windows Server **"Hermes" Hyper-V VM** documented
> in the repo-root `CLAUDE.md`. That VM is an unrelated TradeHunter data-ingest
> worker and **cannot** run the Nous Hermes agent. Anything Nous-agent-related
> lives under `nous_hermes/` and deploys to the Linux box.

## Contents
- `skills/` — Hermes skills (each `<category>/<name>/SKILL.md`), mirrors the
  layout the agent expects at `~/.hermes/skills/`.
  - `markets/premarket-briefing/` — the pre-market briefing skill (v1.0.0).
    - `SKILL.md` — procedure + rules.
    - `templates/briefing.md` — Telegram output format.
    - `references/sources.md` — trusted data sources per section.
  - `markets/matp/` — the **MATP skill** (v1.0.0). Computes the faithful
    Median Analyst Target Price + Max Buy Price for **TradeHunter** and POSTs
    the results to the platform's `/api/matp` (no CSV/Sheets/Telegram). Reads
    the active Finviz filters from `GET {TRADEHUNTER_URL}/api/filters`, expands
    to a ticker universe, looks up each ticker's latest earnings + post-earnings
    analyst targets on MarketBeat, computes the median (MATP) + MBP=MATP/1.15,
    and pushes via `X-API-Key`. Scheduled by `hermes cron` (monthly + optional
    daily earnings-aware). Needs `TRADEHUNTER_URL` + `TST_INGEST_API_KEY` set on
    the box.
  - `markets/research-planning/` — the **research-planning skill** (v1.0.0). The
    agent-grounded planning chat behind TradeHunter's **Research** page. Co-designs
    one research topic (macro/company) into a runnable PLAN; reads the EDGAR 10-Q
    corpus at `/mnt/hermes_sync/QuarterlyReport/<TICKER>/…` **on demand** + web.
    Invoked via the research-runner shim (`hermes chat -q … -s research-planning`).
- `research_runner/` — the **LAN-only HTTP shim** that lets the (outbound-only)
  dashboard get an agent-grounded chat reply over the LAN. `server.py` (stdlib
  `http.server`, token-auth, `POST /chat`) + `research-runner.service` (systemd
  user unit) + its own README. See that README for the one-time token + systemd
  setup. Pairs with dashboard_tst v2.86's runner-mode chat relay.
- `install.sh` — copies `skills/` into `~/.hermes/skills/` on the server,
  deploys `heartbeat.sh` + `matp_status.sh` to `~/.hermes/`, and copies the
  `research_runner/` shim to `~/.hermes/research_runner/` (+ prints its cron/setup
  lines).
- `heartbeat.sh` — standalone liveness ping for TradeHunter's `/agent` page.
  Runs from **system cron** (every 3 min), independent of the Hermes LLM, so the
  online/stale signal can't be derailed by model shell-mangling or the agent's
  terminal-tool approval gate. Builds the JSON with `jq -n`; POSTs to
  `{TRADEHUNTER_URL}/api/agent/heartbeat`.
- `matp_status.sh` — deterministic progress reporter the **agent** calls while
  draining the MATP refresh queue (`matp_status.sh <rid> <status> [done] [total]
  [note]`). Drives the moving progress bar on `/agent`; POSTs to
  `{TRADEHUNTER_URL}/api/refresh-queue/{rid}/status`. Replaces the hand-built
  curl the LLM kept mangling.

## The pre-market briefing skill

Runs every US trading weekday at **09:00 America/New_York** (30 min before the
09:30 ET cash open) and pushes a briefing to **Telegram** covering:
1. Economic calendar (today, US-focused)
2. Pre-market sentiment (futures, VIX, DXY, yields, commodities, overnight Asia/Europe)
3. Macro & micro insights of the day
4. Large/mid-cap catalysts likely to drive significant intraday moves

Data approach: **web-research-first** — uses Hermes' native web search + browser.
The catalyst scan can later be backed by a finviz/yfinance screener script once
the server has those (see `references/sources.md` upgrade note). The catalyst
section tags trap names (`[M&A-anchored]`, `[consumed-gapper]`, `[dilution]`)
per the trader's GUNS methodology so they can be skipped fast.

Open in MYT: **21:30** during US EDT (Mar–Nov) / **22:30** during US EST (Nov–Mar);
briefing fires 30 min earlier. Anchored to ET so it auto-tracks DST.

## Accessing the server (SSH)

The agent runs on a Linux box on the LAN. First login from any dev PC:

```bash
ssh administrator@192.168.1.163
```

- Host `192.168.1.163` · user `administrator` · port 22 · **password-based**
  (it prompts; password is not stored in the repo).

## Deploy (on the Linux server)

Prereq: `hermes setup` has been run and the Telegram gateway is configured
(`hermes config` should show Telegram credentials).

1. Get this folder onto the server. Until git is set up there, scp it:
   ```bash
   # from the dev PC:
   scp -r nous_hermes user@server:/path/to/TradeHunter/nous_hermes
   ```
   (Later, once the server has the repo: `git pull` then re-run install.sh.)

2. Install the skill(s):
   ```bash
   bash /path/to/TradeHunter/nous_hermes/install.sh
   ```
   Verify: in the agent chat run `/skills` and confirm `premarket-briefing`
   appears; or test on demand with `/premarket-briefing`.

3. Schedule the weekday 09:00 ET job, delivered to Telegram:
   ```bash
   hermes cron create "0 9 * * 1-5" \
     "Run the pre-market briefing for today and deliver it." \
     --skill premarket-briefing \
     --deliver telegram
   ```
   - `--deliver telegram` = your Telegram home channel. For a specific chat use
     `--deliver telegram:<chat_id>`; for a forum topic `telegram:-100<chat>:<thread>`.
   - **Timezone:** confirm the schedule resolves to America/New_York, not server
     local time. If your server clock isn't ET, either set the agent timezone in
     `~/.hermes/config.yaml` or adjust the cron hour to the ET-equivalent in the
     server's zone. Verify with `hermes cron list`.
   - Holidays: the skill self-checks and sends a one-line "market closed" note on
     US holidays, so the weekday cron is safe to leave running year-round.

4. List / manage jobs:
   ```bash
   hermes cron list
   hermes cron remove <job_id>
   ```

## Updating the skill
Edit files here, redeploy (scp / git pull → `bash install.sh`), then re-test with
`/premarket-briefing`. Skill changes are picked up on next invocation; no need to
recreate the cron job unless the schedule or delivery target changes.

## Changelog
- **2026-08-16** — `markets/matp` skill → **v1.8.0**: Stage 1 now reads the new
  **deduplicated queue** at `GET /api/matp-queue` instead of expanding each due
  filter's Finviz URL itself. Screener filters overlap, so the old per-filter walk
  recomputed a ticker once per filter that held it — three containers holding NVDA
  meant three MarketBeat browse sessions and three model passes for one answer.
  TradeHunter now resolves the memberships, unions them, and hands the agent **one
  row per unique ticker** (each carrying `filter_ids`, so per-filter attribution and
  `prune` drift tracking still work). **Action needed on next agent run:** use
  `/api/matp-queue`; do NOT expand the filter URLs yourself. `/api/due-filters` still
  works for back-compat. Pairs with dashboard_tst v3.85 (the "All Tickers" tab).
- **2026-06-18** — Documented the **Selective-tickers schedule contract** in
  `skills/markets/matp/SKILL.md`: `/api/due-filters` now returns a `selective`
  `{due, interval, tickers}` object, and a closing `/api/matp` push with
  `selective:true`+`final:true` (no `filter_id`/`prune`) advances that schedule.
  **Action needed on next agent run:** the matp skill must refresh `selective.tickers`
  when `selective.due` is true (dashboard side shipped in dashboard_tst v3.40).
- **2026-06-15** — Added the **`markets/research-planning` skill (v1.0.0)** + the
  **`research_runner/` LAN shim** — the agent-grounded chat behind TradeHunter's
  Research page. The dashboard is outbound-only, so for the planning chat it now
  POSTs the conversation to `research_runner/server.py` (stdlib `http.server`,
  `X-Research-Token` auth, LAN-bound, systemd user service) which runs
  `hermes chat -q "<conversation>" -s research-planning -Q --max-turns 12 --yolo`.
  The agent reads the EDGAR 10-Q corpus on the mount **on demand**, so planning is
  grounded in the real filings. `install.sh` now also deploys the shim to
  `~/.hermes/research_runner/`. One-time setup (token + `systemctl --user enable
  --now research-runner`) is in `research_runner/README.md`. Pairs with
  dashboard_tst **v2.86** (`research_llm` runner-mode relay + DeepSeek-direct
  fallback + the chat-mode pill). MVP is synchronous (replies take 15–60s) and
  stateless per turn; per-topic session memory + async UX are noted follow-ups.
- **2026-06-01** — Added **`matp_status.sh`** + `markets/matp` skill → **v1.7.1**.
  The agent drives TradeHunter's `/agent`-page progress bar by POSTing
  `/api/refresh-queue/{id}/status` as it works, but letting DeepSeek hand-build
  that curl+JSON kept failing the same way the heartbeat did (mangled quoting).
  `matp_status.sh` is a deterministic positional-arg wrapper
  (`matp_status.sh <rid> <status> [done] [total] [note]`) — reads `~/.hermes/.env`
  itself, builds JSON with `jq`, logs to `~/.hermes/logs/matp_status.log`. SKILL.md
  now instructs the agent to call the helper instead of raw curl at every
  status step (claim/total, per-~5-tickers progress, done/failed). `install.sh`
  deploys it to `~/.hermes/matp_status.sh` alongside `heartbeat.sh`. NOTE: unlike
  the heartbeat it can't run from system cron (only the agent knows its own
  progress), so it still goes through the agent terminal tool — if the box gates
  outbound POSTs as `pending_approval`, allowlist this one script path. Pairs
  with dashboard_tst v2.48 (the moving progress bar on /agent).
- **2026-06-01** — Added **`heartbeat.sh`** — a standalone liveness ping run by
  **system cron** (every 3 min), decoupled from the Hermes LLM. Diagnosed the
  agent going "stale" on TradeHunter's /agent page while the gateway was
  perfectly alive (2.7-day uptime): the in-skill heartbeat ran the POST through
  the LLM, where DeepSeek mangled the shell quoting, the agent terminal tool
  gated outbound `curl` as `pending_approval` in unattended cron, and DeepSeek
  stream stalls dropped whole polls — so the beat rarely landed. The new script
  builds the JSON with `jq -n` (no string interpolation), reads `~/.hermes/.env`
  at the OS level (bypassing the agent-tool credential-read block), and runs in
  plain bash (no approval gate). `install.sh` now copies it to
  `~/.hermes/heartbeat.sh`, `chmod +x`'s it, and prints the crontab line.
  Liveness is now mechanical; the LLM is left to do only the queue-drain work it
  actually needs. (Pairs with dashboard_tst `/api/agent/heartbeat`.)
- **2026-06-01** — `markets/matp` skill → **v1.7.0**: heartbeat now sends a
  STRUCTURED `cron_jobs` array — one object per cron with its **full prompt**
  (`hermes cron list` truncates the Name) — so TradeHunter's /agent page shows
  what each cron actually does. Full prompt sourced from `hermes cron list
  --json` / `hermes cron show <id>` / the `~/.hermes` cron store. Pairs with
  dashboard_tst v2.44.
- **2026-06-01** — `markets/matp` skill → **v1.6.1**: heartbeat now reports
  `hermes cron list` (this agent schedules via Hermes's own scheduler; the system
  `crontab -l` is empty here, so v1.6.0 would have reported no crons). Same
  heartbeat endpoint/cadence otherwise.
- **2026-06-01** — `markets/matp` skill → **v1.6.0**: on every poll the agent now
  POSTs a heartbeat to TradeHunter's `POST /api/agent/heartbeat` (X-API-Key)
  first thing — `{agent, version, host, crons (raw `crontab -l`), polled_at}` —
  so TradeHunter's **Agent** page can show the agent online/stale + the literal
  crons it's running (the agent stays outbound-only; no inbound access). Pairs
  with TradeHunter dashboard_tst v2.41.
- **2026-05-30** — Added `markets/matp` skill v1.0.0 — the LLM/web-research
  half of TradeHunter's MATP feature. Browses Finviz + MarketBeat (DeepSeek +
  browser), applies the post-earnings filter, computes median (MATP) + MBP, and
  POSTs to TradeHunter's authenticated `/api/matp` (no CSV/Sheets/Telegram).
  Pairs with TradeHunter v1.5's `routes/api.py`. Config: `TRADEHUNTER_URL` +
  `TST_INGEST_API_KEY` on the box; schedule via `hermes cron`.
- **2026-05-29** — Added "Accessing the server (SSH)" section with the LAN
  first-login command (`ssh administrator@192.168.1.163`, password-based).
- **2026-05-29** — Added RRG (Relative Rotation Graph) check to the Step 3 Micro
  sector-rotation read (StockCharts/TradingView RRG) + new sources.md section.
- **2026-05-29** — Added MarketWatch + CNBC as major-market-news sources for the
  macro narrative (Step 3 note + `references/sources.md` macro-tape section).
- **2026-05-29** — Added StreetInsider + TradingView as catalyst sources for the
  stock/industry catalyst scan (Step 4 source list + `references/sources.md`).
- **2026-05-29** — Folder created. Added `markets/premarket-briefing` skill
  v1.0.0 (economic calendar + pre-market sentiment + macro/micro + large/mid-cap
  catalyst watch, web-research-first, Telegram delivery, weekday 09:00 ET cron).
  Added `install.sh` deploy script, output template, and sources reference.
