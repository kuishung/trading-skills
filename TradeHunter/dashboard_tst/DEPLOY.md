# dashboard_tst — Deployment runbook

How to stand up the collaboration platform on the server and let members
reach it. Two paths exist; **Path B is the current/active one.**

| | Path B (active) | Path A (future) |
|---|---|---|
| Reach | Hamachi VPN, `http://<server-hamachi-ip>:8000` | public internet, `https://<domain>` |
| Auth | `password` (admin seeds accounts) | `google` (OAuth + admin approval) |
| TLS / domain | none (VPN tunnel is encrypted) | Caddy + Let's Encrypt + a real domain |
| `TST_AUTH_MODE` | `password` | `google` |

The app code supports both — switching is just `TST_AUTH_MODE` in `.env`
(plus the Path-A infrastructure). This runbook covers Path B.

---

## Why HTTP-over-Hamachi is acceptable

Hamachi encrypts the peer-to-peer tunnel (AES). Traffic between a member's
machine and the server never traverses the open internet in cleartext, so
plain `http://` inside the VPN is fine for this stage. Keep
`TST_HTTPS_ONLY=0`. Do **not** also expose port 8000 to the public internet.

---

## 0. Where it runs

Per `DESIGN.md`, the long-term home for an internet-facing deployment is a
**separate VM** on the R720, isolated from the trading "Hermes" VM and its
broker credentials. For a VPN-only Path B preview the exposure is much
lower (no public surface), but a separate VM is still the cleaner choice.
**This app holds no broker credentials and opens no IBKR session** — the
swing-bot control endpoints are stubs that will relay to a trusted-side
execution plane later.

Requirements on the server: **Python 3.10+**, **git**, and **Hamachi**
(installed + a network created). Python 3.12 is fine; the `py -3.12`
IBKR rule does NOT apply (this is a non-IBKR web app).

---

## 1. Get the code

The server pulls via git (no Dropbox there):

```powershell
# first time
git clone <repo-url> C:\TradeHunter-checkout
cd C:\TradeHunter-checkout\TradeHunter\dashboard_tst
# subsequently
git pull --ff-only
```

## 2. Configure

```powershell
copy app\.env.example app\.env
```
Edit `app\.env`:
- `TST_SECRET_KEY` — generate: `py -c "import secrets; print(secrets.token_hex(32))"`
- `TST_AUTH_MODE=password`
- `TST_ADMIN_EMAIL` / `TST_ADMIN_PASSWORD` — seeds your approved admin on first run.
- `TST_HTTPS_ONLY=0`

`.env` is gitignored (per-PC). The SQLite DB (`tst.db`) is gitignored too.

## 3. First run (foreground sanity check)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\run_app.ps1 -BindHost 0.0.0.0
```
This creates `.venv`, installs `app\requirements.txt`, and launches uvicorn.
Verify locally on the server: open `http://localhost:8000/health` -> `{"status":"ok"}`.
Ctrl+C to stop, then make it persistent (step 5).

## 4. Firewall — lock port 8000 to the VPN

Allow inbound 8000 ONLY from the Hamachi subnet (`25.0.0.0/8`), so the LAN
and any public interface can't reach it:

```powershell
New-NetFirewallRule -DisplayName "TST Dashboard (Hamachi only)" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 `
  -RemoteAddress 25.0.0.0/8
```

## 5. Run it as a service (survives reboot / RDP disconnect)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\setup_hermes_webapp_task.ps1 -StartNow
```
Registers `TST-Dashboard-Web` (AtStartup, S4U, auto-restart). Mirrors the
bot's `setup_hermes_*` task pattern.

## 6. Members connect

1. Install Hamachi, join your network (invite them; free Hamachi caps a
   network at **5 machines** — server + 4 members — paid plan or Tailscale
   for more).
2. Browse `http://<server-hamachi-ip>:8000` (find the server's Hamachi IP
   in the Hamachi window, `25.x.x.x`).
3. You (admin) log in with the seeded creds, open **Admin**, and create
   member accounts (email + initial password). Share those with members.

## 7. Update loop (as we build)

```
laptop:  edit -> commit -> push
server:  git pull --ff-only
         Restart-ScheduledTask -TaskName 'TST-Dashboard-Web'
```
(`run_app.ps1` re-installs deps on each start, so new requirements are
picked up automatically.)

---

## Upgrading to Path A (public + Google) later

When you want public access with Google login: get a domain, set
`TST_AUTH_MODE=google` + the `TST_GOOGLE_*` vars, put Caddy in front for
TLS, and register the OAuth client redirect `https://<domain>/auth/callback`.
A domain A-record can even point at the Hamachi IP with Caddy using the
DNS-01 challenge if you want it private-but-with-real-Google-login. That
infra (Caddyfile + service) is not built yet — flagged as the Path-A step.

## Troubleshooting

- **Can't reach it from a member machine** — confirm both are "online" (green)
  in Hamachi; check the firewall rule; confirm the task is Running
  (`Get-ScheduledTask -TaskName TST-Dashboard-Web`).
- **`/health` works locally but not over Hamachi** — the app is bound to
  127.0.0.1; ensure the task/run used `-BindHost 0.0.0.0`.
- **Login fails for the admin** — the admin is only seeded on first startup
  when `TST_ADMIN_EMAIL`+`TST_ADMIN_PASSWORD` are set AND no such user
  exists yet. Set them, delete `tst.db` if it was created empty, restart.
