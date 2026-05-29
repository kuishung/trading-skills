# nous_hermes/ — Nous Hermes agent project

All work for the **Nous Research Hermes agent** (https://hermes-agent.nousresearch.com/)
lives here. The agent itself runs on a separate **Linux server** (clean install,
CLI + SSH); this folder is the source of truth that gets deployed there.

> **Naming convention:** this folder is `nous_hermes` (not `hermes`) on purpose,
> to disambiguate from the R720 Windows Server **"Hermes" Hyper-V VM** documented
> in the repo-root `CLAUDE.md`. That VM is an unrelated intraday-bot data-ingest
> worker and **cannot** run the Nous Hermes agent. Anything Nous-agent-related
> lives under `nous_hermes/` and deploys to the Linux box.

## Contents
- `skills/` — Hermes skills (each `<category>/<name>/SKILL.md`), mirrors the
  layout the agent expects at `~/.hermes/skills/`.
  - `markets/premarket-briefing/` — the pre-market briefing skill (v1.0.0).
    - `SKILL.md` — procedure + rules.
    - `templates/briefing.md` — Telegram output format.
    - `references/sources.md` — trusted data sources per section.
- `install.sh` — copies `skills/` into `~/.hermes/skills/` on the server.

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
   scp -r nous_hermes user@server:/path/to/intraday-bot/nous_hermes
   ```
   (Later, once the server has the repo: `git pull` then re-run install.sh.)

2. Install the skill(s):
   ```bash
   bash /path/to/intraday-bot/nous_hermes/install.sh
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
