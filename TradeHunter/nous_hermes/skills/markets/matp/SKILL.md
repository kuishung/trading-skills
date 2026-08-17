---
name: matp
version: 1.9.0
description: Compute the faithful Median Analyst Target Price (MATP) + Max Buy Price (MBP) for TradeHunter and push the results to the platform. Use when asked to "refresh MATP", "run MATP", "update target prices", or on the scheduled cron. Reads the active Finviz screener filters from TradeHunter's API, expands them to a ticker universe, and for each ticker looks up the latest earnings date + analyst price targets on MarketBeat, keeps only targets issued AFTER the latest earnings, computes the median (MATP) and MBP = MATP/1.15, then POSTs the rows to TradeHunter's /api/matp endpoint. Does NOT write CSV / Google Sheets / Telegram -- output is the API push only.
---

# MATP — faithful Median Analyst Target Price for TradeHunter

This is the LLM/web-research half of TradeHunter's MATP feature. The platform
(deterministic Python on the Windows "Hermes" box) does NOT compute MATP — it
only stores + displays what this skill pushes. This skill does the fuzzy work:
browse Finviz + MarketBeat, apply the post-earnings filter, compute the median.

## Why this lives here (not in TradeHunter)
The faithful method needs per-analyst price targets WITH dates (to drop stale
pre-earnings targets). That requires reading MarketBeat's forecast pages —
messy HTML best handled by an agent with a browser, not a brittle scraper. So
TradeHunter offloads it to this agent (DeepSeek-backed), which pushes results
back over an authenticated API.

## Config — READ THESE FROM `~/.hermes/.env` (do NOT ask the user)
**Before any API call, get the two values by reading the agent's secrets file
`~/.hermes/.env`** (the file `hermes config` lists under "Secrets" — NOT a
project/repo `.env`). Run exactly:

```bash
grep -E '^(TRADEHUNTER_URL|TST_INGEST_API_KEY)=' ~/.hermes/.env
```

Use those exact values for every request below. **Never prompt the user for
them and never search the repo for a `.env`** — they live only in
`~/.hermes/.env`. If the grep returns nothing, stop and report that the two
keys are missing from `~/.hermes/.env` (don't guess).
- `TRADEHUNTER_URL` — e.g. `https://app.tradehunter.net`
- `TST_INGEST_API_KEY` — sent as the `X-API-Key` header (identical to
  `TST_INGEST_API_KEY` in TradeHunter's `app/.env` on the Windows Hermes box).

## What to ask the user
Nothing on the scheduled run. If invoked ad-hoc for specific tickers
("refresh MATP for NVDA, MSFT"), use those tickers and skip Stage 1.

## Two run modes
1. **Scheduled full refresh** (monthly cron) — Stage 1→5 over all active
   filters, pushing per-filter with `prune:true` (drift tracking).
2. **Queue poll** (frequent cron, ~10 min) — drain the collaborator-triggered
   ad-hoc requests. See "Ad-hoc refresh queue" below. This is what makes the
   "Request refresh" buttons on TradeHunter's MATP page actually do something.

---

## Heartbeat (every poll — do this FIRST, before draining the queue)
TradeHunter can't reach into this Linux box, so it can't tell whether the agent
is alive or what crons are installed. Tell it: **on every poll, before anything
else, POST a heartbeat** with the agent's version and a STRUCTURED list of its
crons — **including the FULL prompt each cron runs** — so the dashboard's
**/agent** page can show what every job actually does (not a truncated name).

**This agent schedules via Hermes's OWN scheduler (`hermes cron`), NOT the system
crontab** (`crontab -l` is empty here).

**Build `cron_jobs` — one object per cron with its FULL prompt.** `hermes cron
list` truncates the Name and has no `--json`/`show`, so read the cron-store file
**`~/.hermes/cron/jobs.json`** directly — it holds every job's full definition
(id, schedule, skills, and the complete prompt). Map each entry to
`{id, schedule, skills, prompt, next_run, active}` where **`prompt` is the
complete, untruncated instruction**. Map fields sensibly to the observed
jobs.json shape: `schedule` is an **object** `{"kind","expr","display"}` → send
its `display` (or `expr`) **string**, not the object; `skills` may be a list →
join with commas; `active` may be null (treat as active) or come from inverting
`paused`. Also send the raw `hermes cron list` text as `crons` (a fallback the
server uses if `cron_jobs` is absent).

Post it (example with two fields shown; include every cron you have):
```bash
curl -s -X POST "{TRADEHUNTER_URL}/api/agent/heartbeat" \
  -H "X-API-Key: {TST_INGEST_API_KEY}" -H "Content-Type: application/json" \
  -d '{
    "agent": "nous_hermes",
    "version": "matp 1.7.0",
    "host": "<hostname>",
    "polled_at": "<UTC ISO-8601>",
    "crons": "<raw `hermes cron list` text>",
    "cron_jobs": [
      {"id": "8be925747007", "schedule": "*/10 * * * *", "skills": "matp",
       "prompt": "Run the matp skill in queue-poll mode: GET /api/refresh-queue and action any pending requests.",
       "next_run": "2026-06-01T00:20:00+08:00", "active": true}
    ]
  }'
```
(`agent` defaults to `nous_hermes`. Soft-fail: if the heartbeat POST errors, log
it and carry on with the poll — it must never block the real work.)
Expect `{"ok":true,"agent":"nous_hermes","received_at":"..."}`.

## Ad-hoc refresh queue (frequent poll)
Collaborators (moderators/admins) click "Request refresh" on TradeHunter's MATP
page; that enqueues a request. TradeHunter can't fetch and can't reach this
agent — so the agent **polls** and drains the queue.

```
GET {TRADEHUNTER_URL}/api/refresh-queue     header: X-API-Key: {TST_INGEST_API_KEY}
-> {"requests":[
     {"id":7,"scope":"ticker","symbol":"NVDA","filter_id":null},
     {"id":8,"scope":"filter","filter_id":1,"filter_url":"https://finviz.com/...","filter_description":"growth screen"}
   ]}
```
**Report status with the `matp_status.sh` helper — NOT a hand-built curl.**
Progress drives the moving progress bar on TradeHunter's **/agent** page. Build
the status POST by hand and you WILL mangle the shell quoting (it happened
repeatedly); instead call the deterministic wrapper installed at
`~/.hermes/matp_status.sh`:
```bash
~/.hermes/matp_status.sh <rid> <status> [done] [total] [note]
#   status = running | done | failed   (empty done/total are simply not sent)
~/.hermes/matp_status.sh 7 running 0 25 "starting growth screen"   # claim + total
~/.hermes/matp_status.sh 7 running 10                              # 10/25
~/.hermes/matp_status.sh 7 done "" "" "refreshed 25 tickers"       # bar -> 100%
~/.hermes/matp_status.sh 7 failed "" "" "marketbeat blocked"
```
It reads `~/.hermes/.env` itself, builds the JSON with `jq`, POSTs to
`/api/refresh-queue/{rid}/status`, and logs to `~/.hermes/logs/matp_status.log`.
(If the helper is somehow absent, fall back to the raw POST below — but the
helper is the supported path.)

For each request:
1. **Mark it running, with the total** so the UI can draw a real progress bar:
   `~/.hermes/matp_status.sh {id} running 0 N "<what you're starting>"`
   (N = number of tickers this run will process — 1 for a ticker request, the
   universe size for a filter request once you've screened it.) Raw equivalent:
   `POST /api/refresh-queue/{id}/status` body
   `{"status":"running","progress_total":N,"progress_done":0}`.
2. **Do the work, reporting progress as you go:**
   - `scope:"ticker"` → run Stages 2-5 for that one `symbol`. Push via
     `/api/matp` as a single item, **no `filter_id`, no `prune`** (a single
     ticker is not a universe — pruning would wrongly drop everything else).
   - `scope:"filter"` → run Stages 1-5 for that filter's `filter_url` (the full
     universe). **Push incrementally so the table fills live:** as each ticker
     (or small batch) is computed, `POST /api/matp` with that batch +
     `filter_id` and **`final:false`** (NO `prune`) — these upsert the processed
     tickers immediately (they appear on the board mid-run) and are NOT archived.
     When the whole universe is done, send ONE closing push with the **full**
     item list + `filter_id` + **`prune:true`** + **`final:true`** — that one
     prunes the fallen-out tickers AND is saved as the run's archive file.
   - **Every few tickers**, update progress AND narrate what you're doing:
     `~/.hermes/matp_status.sh {id} running K "" "processing <SYM> (K/N)"`
     (K = tickers finished so far; leave total empty to keep the original). The
     `note` shows on the dashboard so the user sees what's happening live; the
     count drives the moving progress bar. Don't post on every single ticker —
     every ~5 is plenty.
3. **Mark it done/failed:**
   `~/.hermes/matp_status.sh {id} done "" "" "refreshed 12 tickers"`
   — on error, `~/.hermes/matp_status.sh {id} failed "" "" "<short reason>"`. The
   note shows on the ticker's detail page. (On `done`, the server snaps the bar
   to 100%.)

Idempotent: if the queue is empty, do nothing. TradeHunter de-dupes requests, so
you'll never see two open requests for the same target.

### Scheduled work — the DEDUPLICATED queue (use this; it is the token-saver)
On the SAME poll, ask TradeHunter for the deduplicated list of tickers to compute:
```
GET {TRADEHUNTER_URL}/api/matp-queue      header: X-API-Key: {TST_INGEST_API_KEY}
-> {"tickers":[{"symbol":"NVDA","filter_ids":[1,3],"manual":false,
                "exchange":"NASDAQ","as_of":null,"never_computed":true,"stale":true}, ...],
    "count": 240, "duplicates_avoided": 63,
    "advance_filter_ids":[1,3], "selective":{"due":true,...},
    "filter_errors":[...]}
```

**WORK THE LIST IN THE ORDER GIVEN — top to bottom.** It is returned
neediest-first: `never_computed` tickers, then the stalest, then alphabetical.
That order IS the scheduling policy. When your call budget runs out mid-list you
will have spent it on the tickers that actually lack a value; restarting at the
top next poll then continues down the same priority. (Working it in any other
order — alphabetically, or "whatever looks interesting" — recreates the bug this
ordering fixed: five tickers went a full day uncomputed while 76 already-fresh
ones were recomputed around them.)

Use each entry's **`exchange`** for the MarketBeat URL when it is non-null,
instead of guessing NASDAQ vs NYSE. A wrong guess fails the same way on every
poll, so a mis-guessed ticker never completes.
**Screener filters are only CONTAINERS.** TradeHunter resolves each active
filter's Finviz URL itself, unions the memberships, and returns **one row per
unique ticker**. Compute MATP **exactly once per entry in `tickers`** — never once
per filter. A ticker held by three filters appears once, with all three ids in
`filter_ids`; push it back once, attributed to those ids. This is the whole point
of the endpoint: it is what stops the same ticker burning three MarketBeat browse
sessions and three model passes.

Do **not** expand the Finviz URLs yourself when using this endpoint — the
membership has already been resolved for you.

When you finish, send the closing push with `final:true` and
`advance_filter_ids` echoed back as the filters to advance (or push per
`filter_id` with `prune:true` as before for drift tracking). **Check the
response's `advanced_filters`** — it lists the schedules that actually moved. If
it comes back empty when you sent ids, the cycle did NOT close: those filters
stay due, you will be handed the same universe on the next poll, and the
dashboard will keep saying "Recalculation in progress" forever. Say so in your
run note rather than reporting a clean finish. (That silent failure was real
until 2026-08-17: the server had no `advance_filter_ids` field, so the ids were
dropped and nothing ever advanced.)

`filter_errors` lists filters whose URL could not be resolved this cycle —
mention them in your run note; they contributed no tickers.

*Legacy path (still supported, do not prefer):* `GET /api/due-filters` returns the
per-filter list and requires you to expand each URL and dedupe by hand.

### Selective tickers (own schedule, same poll)
The `/api/due-filters` response now also carries a `selective` object — the ad-hoc
"Selective tickers" set (names with no source filter) has its **own** schedule,
independent of any filter:
```
-> {"filters":[...], "manual_tickers":[...],
    "selective": {"due": true, "interval": "weekly", "tickers": ["AAPL","NVDA", ...]}}
```
When `selective.due` is **true**, refresh `selective.tickers` (Stages 2-5 each, as a
plain multi-ticker run) and send a closing push to `/api/matp` with the full item
list, **`selective:true` + `final:true`, and NO `filter_id` and NO `prune`** (these
stay ungrouped "Selective" names — pruning/`filter_id` would wrongly re-bucket them).
That closing push **advances the selective schedule** (TradeHunter sets its
`last_run_at`/`next_run_at` from `interval`), exactly like a filter. If
`selective.due` is false, leave them alone. (`manual_tickers` is still returned for
back-compat — it's the same list, populated when a filter is due or the selective
schedule is due.)

---

## Procedure

### Stage 1 — Get the universe

**Preferred — the deduplicated queue (one browse + one model pass per ticker):**
```
GET {TRADEHUNTER_URL}/api/matp-queue     header: X-API-Key: {TST_INGEST_API_KEY}
```
Returns one entry per **unique** ticker, each carrying the `filter_ids` of every
container that holds it. Work straight down that list — no Finviz paging, no
manual dedupe, no ticker computed twice. Carry `filter_ids` through to Stage 5 so
per-filter attribution and drift tracking still work.

**Legacy — raw filters (only if the queue endpoint is unavailable):**
```
GET {TRADEHUNTER_URL}/api/filters     header: X-API-Key: {TST_INGEST_API_KEY}
```
Returns `{"filters":[{"id","description","url"}, ...]}`. For each filter `url`,
open it in the browser and extract every ticker (with exchange). Finviz
paginates 20/page via `&r=21`, `&r=41`, ... — page through to the reported
total. **Keep the per-filter ticker set** (which `id` each ticker came from).
On this path you MUST dedupe across filters yourself before computing — compute
MATP once per unique ticker and reuse it for every filter that holds it.

(Ad-hoc mode: skip this; use the tickers the user named. No `filter_id`, no
`prune` — see Stage 5.)

### Stage 2 — Latest earnings date per ticker
For each ticker, open:
```
https://www.marketbeat.com/stocks/<EXCHANGE>/<TICKER>/earnings/
```
Take the most recent **past** earnings date (YYYY-MM-DD) from the history
table — not a future scheduled date. (MarketBeat tolerates NASDAQ or NYSE in
the path; both resolve.)

### Stage 3 — Analyst price targets per ticker
```
https://www.marketbeat.com/stocks/<EXCHANGE>/<TICKER>/forecast/
```
From the price-target history table, collect every row with a NUMERIC target:
`date | brokerage | $target`. For a "boost" like `$440 -> $480`, use the NEW
value ($480). Skip rows with no numeric target (reiterations, initiations with
no number). All targets are on one page — no pagination.

### Stage 4 — Filter, median, MBP, distribution
For each ticker:
1. Keep only targets with `target_date > latest_earnings_date` (**strictly**
   greater — same-day-as-earnings excluded). Call these the *post-earnings* set.
2. Sort the post-earnings set numerically; compute the **median** (MATP):
   odd n = middle value; even n = average of the two middle; n=0 = skip the
   ticker (no post-earnings coverage); n=1 = that single value.
3. `MBP = MATP / 1.15`, rounded to 2 dp. MATP rounded to 2 dp.
4. **Distribution** of the post-earnings set: `target_high` (max), `target_low`
   (min), `target_mean` (average) — the spread of analyst disagreement.
5. **Keep the full target list** — ALL targets found (post- AND pre-earnings),
   each `{brokerage, target_date, target_price}` — to push as evidence in
   Stage 5. TradeHunter recomputes post/pre from the date on display, so send
   both.

Do NOT use the mean for MATP — MATP is a median (the mean is only a
distribution stat).

### Stage 5 — Push to TradeHunter (the only output)
**One POST per filter** (not one merged universe). Each POST carries that
filter's `id` as `filter_id` and `prune: true`, plus its COMPLETE current
ticker set. That lets TradeHunter mark tickers previously tied to that filter
but absent now as `dropped` (data retained, hidden from the live board).
```
POST {TRADEHUNTER_URL}/api/matp     header: X-API-Key: {TST_INGEST_API_KEY}
Content-Type: application/json
{
  "source": "nous_hermes",
  "filter_id": 1,
  "prune": true,
  "items": [
    {
      "symbol": "NVDA", "exchange": "NASDAQ", "last_earnings_date": "2026-05-21",
      "matp": 175.50, "mbp": 152.61, "n_targets": 12, "trend": "Uptrend",
      "target_high": 220, "target_low": 140, "target_mean": 178.3,
      "targets": [
        {"brokerage": "Morgan Stanley", "target_date": "2026-05-22", "target_price": 200},
        {"brokerage": "Goldman Sachs",  "target_date": "2026-05-23", "target_price": 185},
        {"brokerage": "Citi",           "target_date": "2026-04-30", "target_price": 160}
      ]
    },
    ...
  ]
}
```
- **`filter_id` + `prune: true`** on a full-universe run = enable drift tracking
  for that filter. To fill the board live, push processed tickers incrementally
  with **`final:false`** (no `prune`) as you go, then send ONE closing push with
  the COMPLETE ticker set + `prune:true` + **`final:true`** (only that closing
  push prunes fallen-out names AND is saved as the archive file). Ad-hoc /
  per-ticker runs: omit `filter_id`/`prune` and leave `final:true` (default).
- `mbp` optional (server recomputes MATP/1.15 if omitted) — but send it.
- `trend` optional: `Uptrend` / `Sideways` / `Downtrend` if you classify it
  (else omit; a separate daily job may fill it).
- `target_high/low/mean` = distribution of the **post-earnings** set (Stage 4.4).
- `targets` = the **full** list (post + pre); server de-dupes on
  `(symbol, brokerage, target_date, target_price)`, so re-pushing the same list
  each run adds nothing new. Include `target_date` on every target — it's how
  post/pre is decided.
- Omit tickers with n=0 post-earnings targets (don't push a null MATP).
- Expect `{"ok":true,"upserted":N,"history_appended":H,"targets_added":T,"dropped":D}`.
  401 = wrong key; 503 = TradeHunter has no `TST_INGEST_API_KEY` set yet.

**Out of scope for this skill:** the `signal*` fields (HOT/WARM/WATCHING bounce
setup — entry/stop/target/rr). Those need daily price bars (EMA20/EMA50 bounce)
and are filled by a separate daily job. This skill leaves them unset; the server
preserves any signal already on the row when a payload omits it.

**No CSV, no Google Sheets, no Pine, no Telegram.** The API push is the output.

---

## Quality checks before declaring done
- Every ticker pushed has `n_targets >= 1` and a sane MATP (positive, within an
  order of magnitude of the current price).
- The post-earnings filter was applied (you used `target_date > earnings_date`,
  strictly).
- Report a short summary: universe size, how many pushed, how many skipped for
  n=0, and any tickers whose earnings looked stale (>~3 months).

## Schedule (cron on the Linux agent)
Monthly full refresh (1st of month, 06:00 — pick a quiet hour):
```
hermes cron create "0 6 1 * *" \
  "Run the matp skill: refresh MATP for all active TradeHunter filters and POST the results." \
  --skill matp
```
For a more frequent earnings-aware pass, add a daily cron that runs matp only
for tickers whose latest earnings was in the last 1-2 days.

Queue poll (every 10 min — drains collaborator "Request refresh" clicks):
```
hermes cron create "*/10 * * * *" \
  "Run the matp skill in queue-poll mode: GET /api/refresh-queue and action any pending requests." \
  --skill matp
```

## Source / method
Mirrors the canonical MATP methodology in TradeHunter's
`resources/MATP/SKILL.md` (median of post-earnings analyst targets, MBP =
MATP/1.15) — but output is the API push, not CSV/Sheets/Pine.
