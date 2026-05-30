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

Requirements: **Python 3.12**, **git** (and later **cloudflared**). The
`py -3.12` IBKR rule does NOT apply — this is a non-IBKR web app.

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

## Notes / guardrails

- **Isolation:** the app is genuinely public now (auth-gated). It holds no
  broker credentials and opens no IBKR session, but running it on a **separate
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
