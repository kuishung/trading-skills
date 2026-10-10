# dashboard_tst — Deployment runbook (Hermes + Cloudflare Tunnel)

How to host the collaboration platform on **Hermes (Windows Server 2019)**
and give collaborators a plain **URL** (no client install) via **Cloudflare
Tunnel**.

## The model

```
collaborator browser
  https://study.<your-domain>  ──►  Cloudflare  ──(outbound tunnel)──►  cloudflared ─► uvicorn 127.0.0.1:8000
                                                          (on Hermes)        (on Hermes, the app)

laptop:  edit → commit → push          Hermes:  git pull + restart service  (auto-update task, every 5 min)
```

Everything collaborators touch runs on **Hermes**: uvicorn (the app) + the
cloudflared agent. The agent dials **out** to Cloudflare, so **no inbound
ports are opened** on your network. Auth stays password-mode (admin creates
accounts); the public URL just lands on the login page.

| | This deploy |
|---|---|
| Host | Hermes (Win Server 2019) |
| Public access | Cloudflare Tunnel (free, auto-HTTPS, no port-forward) |
| Auth | `TST_AUTH_MODE=password` (admin-created accounts) |
| TLS | terminated by Cloudflare; set `TST_HTTPS_ONLY=1` |
| Refresh-on-push | `git pull` + restart service (autopull task) |

Cost: Cloudflare Tunnel + account are **free**; the only cost is a domain
(~$10/yr) for a stable URL. (A throwaway `trycloudflare.com` URL needs no
domain but **changes every restart** — fine for a smoke test, not for
collaborators.)

---

## A. App service on Hermes

Requirements: **Python 3.12**, **git** (and later **cloudflared**). The web app
never touches IBKR, and since v4.134 neither does the **options collector** (section G —
it reads Massive over HTTPS from the same venv). Still build the venv with `py -3.12`
(`run_app.ps1` prefers it), never 3.14: `requirements.txt` keeps `ib_insync` for the
legacy by-hand seeder `deploy\iv_seed_ibkr.py`, and `ib_insync` cannot import on 3.14.

```powershell
# 1. Clone (first time)
git clone <repo-url> C:\TradeHunter-checkout
cd C:\TradeHunter-checkout\TradeHunter\dashboard_tst

# 2. Configure
copy app\.env.example app\.env
#   In app\.env set:
#     TST_SECRET_KEY  -> py -c "import secrets; print(secrets.token_hex(32))"
#     TST_AUTH_MODE=password
#     TST_ADMIN_EMAIL / TST_ADMIN_PASSWORD   (seeds your admin on first run)
#     TST_HTTPS_ONLY=1                        (served over Cloudflare HTTPS)
#   Options page (v4.134): the data comes from Massive (formerly Polygon.io) - section G.
#     TST_MASSIVE_API_KEY=<your key>   (Hermes only; app\.env is gitignored - never commit it)
#     TST_MASSIVE_QUOTES=0             (Options Starter has no bid/ask; 1 only on a plan with quotes)
#     TST_OPTIONS_CYCLE_MIN=15         (optional: minutes between the collector's passes in the session)
#   (TST_IBKR_PORT / TST_OPTIONS_COLLECTOR_CLIENT_ID / TST_OPTIONS_MAX_LINES are gone since v4.134,
#    TST_OPTIONS_SOURCE / _FALLBACK / TST_ALPACA_FEED / TST_IV_SEED_IBKR / TST_IBKR_PYTHON since
#    v4.133 - delete them from an older app\.env.)

# 3. First run (foreground sanity check) -> http://localhost:8000/health
powershell -ExecutionPolicy Bypass -File deploy\run_app.ps1

# 4. Run as a service (survives reboot/RDP); binds 127.0.0.1
powershell -ExecutionPolicy Bypass -File deploy\setup_hermes_webapp_task.ps1 -StartNow
```

`.env` and the SQLite DB are gitignored (per-PC). The app is bound to
**127.0.0.1** — only cloudflared (same host) reaches it.

---

## B. Public URL with Cloudflare Tunnel

Install cloudflared on Hermes (`winget install Cloudflare.cloudflared`, or
the `.msi` from Cloudflare's GitHub releases).

### Smoke test today (no domain, ephemeral URL)
```powershell
cloudflared tunnel --url http://localhost:8000
```
Prints a `https://<random>.trycloudflare.com` that proxies to the app. Open
it, confirm collaborators can reach the login page. The URL changes each run.

### Stable URL for collaborators (named tunnel — needs a domain on Cloudflare)
```powershell
cloudflared tunnel login                       # browser auth, one time
cloudflared tunnel create tst                  # creates tunnel + creds .json
cloudflared tunnel route dns tst study.<your-domain>
# create the config from the template:
#   copy deploy\cloudflared-config.example.yml  %USERPROFILE%\.cloudflared\config.yml
#   fill in <TUNNEL-ID>, creds path, hostname
cloudflared tunnel run tst                     # test in foreground
cloudflared service install                    # then run on boot as a service
```
Collaborators open `https://study.<your-domain>` — permanent, HTTPS, nothing
to install. Cloudflare's edge also absorbs bots/DDoS in front of the login.

---

## C. The "push from laptop → Hermes refreshes" loop

`uvicorn --reload` proved unreliable on synced/networked drives, so the
refresh step **restarts the service** instead.

```powershell
# Manual refresh after a push:
powershell -ExecutionPolicy Bypass -File deploy\update.ps1     # git pull --ff-only + restart

# Hands-off: poll + auto-refresh every 5 min
powershell -ExecutionPolicy Bypass -File deploy\setup_hermes_autopull_task.ps1 -StartNow
```
`update.ps1` pulls, reinstalls deps only if `requirements.txt` changed, and
restarts `TST-Dashboard-Web`. With the autopull task, you just push from the
laptop and Hermes reflects it within a few minutes.

> Polling, not a webhook: a tunnelled private server isn't reachable inbound
> by GitHub, so we poll `git pull`.

---

## D. Members

You (admin) log in at the URL and create accounts: **Admin → Create member**
(email + initial password). Share those; collaborators log in and use the
**Feedback** board to comment on the build as it goes.

---

## E. Checking status

`GET /status` (unauthenticated, non-sensitive): `{status, version, auth_mode,
db_ok, uptime_seconds}`.

```powershell
# on the server:
powershell -ExecutionPolicy Bypass -File deploy\status_check.ps1
# remotely, if reachable:
powershell -ExecutionPolicy Bypass -File deploy\status_check.ps1 -Target https://study.<your-domain>
```

---

## F. EDGAR earnings-filing reporter (on AI-Hermes, 192.168.1.162)

The EDGAR corpus (SEC 10-Q/10-K) is fetched by the Nous agent and stored on
**AI-Hermes** (the Windows file server), NOT on the Hermes web host. The web app
can't read that box, so AI-Hermes runs `deploy/report_edgar_health.py` (stdlib
only) which **folder-scans** the corpus — deriving each ticker's missing quarters
+ stub/absent MDs straight from the filenames (no DB, no network) — and POSTs to
`/api/ingest/edgar`. The Data Ingest page §3 then shows COMPLETE/GAPS/STUB.

Run ON **AI-Hermes** (PowerShell). The repo arrives via the same `git pull` /
Dropbox sync as the rest of TradeHunter.

```powershell
# 1. Configure — in app\.env (or as TST_ env vars) set:
#     TST_EDGAR_DIR=C:\HermesSync\MarketResearch\QuarterlyReport   (default if unset)
#     TST_INGEST_API_KEY=<same key the dashboard uses>
#     TST_DASHBOARD_URL=https://study.<your-domain>                (or the tunnel/LAN URL)

# 2. Smoke test (no POST) — confirms the folder scan works:
py dashboard_tst\deploy\report_edgar_health.py --dry-run --limit 20

# 3. One real push:
py dashboard_tst\deploy\report_edgar_health.py

# 4. Schedule it (a pure folder scan — run it after each seed/update run):
schtasks /Create /TN "TST-Edgar-Report" /TR ^
  "py C:\trading-skills\TradeHunter\dashboard_tst\deploy\report_edgar_health.py" ^
  /SC DAILY /ST 13:00 /F
```

The scan is local-only (no network, no DB, ~735 tickers in seconds) and soft-fail
throughout — an unreadable ticker folder is skipped, never breaks the push.

---

## G. Options data from Massive (on Hermes, v4.134 — Options v2)

Since v4.134 every option and stock figure on the Options page comes from **Massive**
(formerly Polygon.io) — the user's decision of 2026-10-10 (`OPTIONS_V2_DESIGN.md` §13).
TradeHunter only FINDS the trade; the live price is checked and the order entered in IBKR
TWS. The earnings date stays on the free source (Yahoo). Nothing in the Options data path
touches IBKR any more: no IB Gateway, no weekday blackout, no clientId (89 is retired).

| Massive plan | Cost | What it gives | Used for |
|---|---|---|---|
| **Options Starter** | $29 / month | the whole-chain snapshot with greeks, IV, open interest and the day bar; **15 min delayed; NO bid/ask**; unlimited requests; daily bars of option contracts (expired ones too), 2 years | every chain read (so prices are estimated from each contract's IV); the IV30 history behind IV rank, rebuilt from option daily bars |
| **Stocks Basic** | free | end-of-day daily stock bars, 2 years, ~5 requests a minute | ATR, HV, 20-day volume, the stock price when the chain cannot give one |

(The free Options Basic plan cannot run the screener: no chain snapshot, no greeks / IV,
5 requests a minute.)

Two things read Massive, both server-side with the one key:

- **`TST-Options-Collector`** — `deploy\options_collector.py --forever`, the dashboard venv
  python, HTTPS to `api.massive.com`. Each 15 s tick: the **first-time history** of a new
  basket ticker first (2 years of Stocks Basic bars + about a year of IV30 rebuilt from
  option daily bars — 150-300 requests per ticker); then **session passes** — 09:30-16:00 ET
  on trading days, every `TST_OPTIONS_CYCLE_MIN` minutes (default 15), every basket ticker's
  chain (most-held first); then **one end-of-day pass** after 16:20 ET (the chain, the day's
  record into `option_chain_snapshot`, the Stocks Basic bars, the earnings date, the
  retention prune — a missed evening is caught up before the next open). It writes
  `opt_quote` / `opt_underlying` / `opt_underlying_daily` / `opt_refresh_log`, a heartbeat
  row (`opt_collector_status`) and `state\options_collector.json` (the Hermes tray reads it).
- **"Refresh now"** on a basket row (and in an opened trade) — the web app reads that one
  ticker at once (a few seconds), at most once per ticker per member per minute.

### First install (Hermes, PowerShell, elevated)

1. Subscribe at massive.com to **Options Starter** and **Stocks Basic**, and copy the API
   key from the Massive dashboard.
2. Put the key in `app\.env` on Hermes — the whole line, no quotes:
   `TST_MASSIVE_API_KEY=<your key>`. `app\.env` is gitignored: the key lives only there —
   never in the repo, a URL, a script you paste elsewhere, or a chat. While there, delete
   the dead IBKR lines `TST_IBKR_PORT`, `TST_OPTIONS_COLLECTOR_CLIENT_ID`,
   `TST_OPTIONS_MAX_LINES` if present. `TST_MASSIVE_QUOTES=0` (the default) is right for
   Options Starter.
3. Run:

```powershell
cd C:\trading-skills\TradeHunter\dashboard_tst
notepad app\.env                                                     # add TST_MASSIVE_API_KEY=<your key>, save, close
.\.venv\Scripts\python.exe -m pip install -r app\requirements.txt    # nothing new for Massive (httpx is already in); keeps the venv in step
powershell -ExecutionPolicy Bypass -File deploy\setup_options_collector_task.ps1 -StartNow
Get-Content state\options_collector.json                             # state, pass, api_ok, history_pending, heartbeat
```

4. Restart the web app with the canonical deploy script (CLAUDE.md / section C): the web
   app reads `app\.env` only when it starts, so "Refresh now" and the admin's
   "TST_MASSIVE_API_KEY is not set on the server" badge see the key only after a restart.

The setup script warns (without printing it) when `TST_MASSIVE_API_KEY` is missing from
`app\.env`; the collector then runs, reports "TST_MASSIVE_API_KEY is not set on this PC"
on the strip and the tray, and looks again every 5 min. A key **replaced** in `app\.env`
needs the collector restarted (re-run the setup script with `-StartNow`). The task starts
at boot (1 min delay) and daily at 07:00 local (revives a dead copy only), restarts on
failure, no time limit; the collector writes `logs\options_collector.log` itself, rotated
at 5 MB with 5 old files kept. If the v4.127 Cboe nightly is still registered, disable it:
`schtasks /Change /TN TST-Options-Nightly /DISABLE`.

**The first run takes a while.** History comes first, so on the first deploy (~100 basket
tickers x 150-300 option-bar requests, plus Stocks Basic at 5 requests a minute) the
session passes wait roughly 30-50 min while the backfills run; the strip says "reading
history" meanwhile.

### After a pull

The canonical web-app restart (section C / CLAUDE.md) does NOT restart the collector — it is
a separate long-running task. **Re-run the setup script with `-StartNow` after any pull that
changes `app\services\opt_collector.py`, `opt_massive.py`, `massive.py`, `opt_store.py`,
`app\models.py` or `deploy\options_collector.py`**: it stops the running copy (and an
orphaned python child) and starts the new code.

```powershell
cd C:\trading-skills\TradeHunter\dashboard_tst
powershell -ExecutionPolicy Bypass -File deploy\setup_options_collector_task.ps1 -StartNow
```

### Watching it

**The Options page strip** (top of `/options`):

| Strip | Meaning |
|---|---|
| "Massive: running · pass 12 · 40/98 tickers · data 15 min delayed" (green) | a session pass is reading the baskets |
| "Massive: reading history · 3/98 tickers" (green) | first-time history of new tickers |
| "Massive: end-of-day pass · 60/98 tickers · data 15 min delayed" (green) | after 16:20 ET |
| "Massive: idle · pass 26 done · data 15 min delayed" (grey) | between passes, or the market is closed |
| "Collector error: TST_MASSIVE_API_KEY is not set on this PC" / "Collector error: Massive rejected the API key (HTTP 401)" / "Collector error: your Massive plan does not include ... (HTTP 403)" (red) | the collector cannot read Massive; the tray line also says what is paused and when it tries again |
| "Massive collector: no heartbeat for 9 min" (amber) | the task died or hangs |

Below it, always on Options Starter: *"prices are estimated from IV (no bid/ask on this
plan) - check live in TWS before entering"*; the two bid/ask rules are greyed out. An
administrator also sees "TST_MASSIVE_API_KEY is not set on the server" when the web app has
no key. The basket's Data column reads "16 min · Massive (delayed)".

**The Hermes tray** carries the same collector: e.g. `Options collector: running · pass 12 ·
40/98 · Massive delayed · EOD 2026-10-09 · hb 10s ago` — **green** running / idle /
history / eod; **amber** ERROR with the reason, stopped, or NO HEARTBEAT; tooltip `Opt p12`,
`Opt idle`, `Opt ERR`, `Opt stale 9m`.

**What the collector does with an error.** No key or a rejected key: everything waits,
looked at again every 5 min. A plan without an endpoint (HTTP 403): only that part pauses
(chain reads, history reads, or the end-of-day stock bars), the rest carries on, retried
every 5 min. Massive not reachable: everything waits, retried after 60 s doubling to 5 min.
One ticker failing: logged in `opt_refresh_log`, the loop moves on. A history with too
little data (a young listing, a ticker with no options) is retried after 30 min, doubling
to 6 h.

By hand (stop the task first, so two loops do not read the same chains) — the venv python,
no `py -3.12` needed: `.\.venv\Scripts\python.exe deploy\options_collector.py --once -v`,
`--history NVDA LRCX`, `--eod-now`. Exit code 2 = Massive not usable for a one-off run (no
key, the key rejected, the plan lacks an endpoint, or Massive not reachable).

**First checks once the key is in** (none of this could be run on the laptop — there is no
key there):
1. The strip goes "reading history" -> "running" (US session) or "idle"; the tray line is green.
2. "Refresh now" on one ticker says "read from Massive: N contracts".
3. In the session, the Data column reads about "15-30 min · Massive (delayed)": every row of
   a snapshot is stamped with the read time minus the 15-min delay (not the contract's own
   last-trade time), so thinly traded strikes do not look stale.
4. After 16:20 ET the tray line shows `EOD <today>`.

---

## Notes / guardrails

- **Isolation:** the app is genuinely public now (auth-gated). It holds no
  broker credentials and opens no IBKR session (its one vendor secret is the Massive
  data key in `app\.env`, v4.134), but running it on a **separate
  VM** from the trading "Hermes" VM is the cleaner choice (DESIGN.md).
- **Google login (Path A):** now that there's a real HTTPS domain, you could
  switch `TST_AUTH_MODE=google`. Password mode is fine; your call.

## Troubleshooting
- **`/health` works locally but the public URL 502s** — cloudflared can't
  reach the app: confirm the web-app task is Running and bound to `:8000`.
- **trycloudflare URL keeps changing** — expected; use a named tunnel.
- **Admin login fails** — admin is seeded only on first startup when
  `TST_ADMIN_EMAIL`+`TST_ADMIN_PASSWORD` are set and no such user exists; set
  them, delete an empty `tst.db`, restart.
