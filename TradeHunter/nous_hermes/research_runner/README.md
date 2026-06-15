# research_runner/ — LAN-only agent-grounded chat shim

**Role:** a tiny HTTP service on the Nous agent (Linux box) that lets
TradeHunter's **Research page** get a planning reply from the *real* Hermes
agent — the one with the EDGAR 10-Q corpus on its mount — instead of the blind
DeepSeek-direct chat.

The dashboard is outbound-only, so for the chat path it POSTs the conversation
here over the LAN; the shim runs `hermes chat -q "<conversation>" -s
research-planning -Q --max-turns N --yolo` and relays the agent's answer back.
The agent reads `/mnt/hermes_sync/QuarterlyReport/<TICKER>/…` **on demand**.

## Contents
- `server.py` — the shim. Stdlib `http.server` only (no pip installs). Token-auth
  (`X-Research-Token` vs `TST_RESEARCH_TOKEN` from `~/.hermes/.env`), LAN-bound.
  `POST /chat` body `{ "topic": {kind, subject, title}, "history": [{role,content}…] }`
  → `{ "ok": bool, "content": "<agent reply>" }`. `GET /health` for probes.
- `research-runner.service` — systemd **user** unit template.

## Endpoints
| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | liveness + config probe |
| POST | `/chat` | `X-Research-Token` | run the agent on a conversation, return the reply |

## Install / deploy (on the Linux box)
`nous_hermes/install.sh` copies `server.py` to `~/.hermes/research_runner/` and
the `research-planning` skill into `~/.hermes/skills/`. Then, one-time:

```bash
# 1) set a shared secret (same value goes in the dashboard's app/.env)
TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(24))")
grep -q '^TST_RESEARCH_TOKEN=' ~/.hermes/.env || echo "TST_RESEARCH_TOKEN=$TOKEN" >> ~/.hermes/.env
echo "TST_RESEARCH_TOKEN=$TOKEN"   # copy this into the dashboard

# 2) install + start the systemd user service
mkdir -p ~/.config/systemd/user
cp ~/.hermes/research_runner/research-runner.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now research-runner
loginctl enable-linger "$USER"     # survive logout

# 3) verify
curl -s http://127.0.0.1:8787/health
```

The dashboard (Windows Hermes box) then sets in `dashboard_tst/app/.env`:
```
TST_RESEARCH_RUNNER_URL=http://192.168.1.163:8787
TST_RESEARCH_TOKEN=<same token as above>
```

## Security
- **Token required** — `/chat` refuses calls without a matching `X-Research-Token`.
- **LAN-only** — keep this off the internet (never behind the Cloudflare tunnel).
  Optionally firewall the port to the dashboard host only:
  `sudo ufw allow from <dashboard-LAN-ip> to any port 8787 proto tcp`.
- No secrets in the unit or repo — the token lives in `~/.hermes/.env`.

## Follow-ups (not in the MVP)
- **Per-topic session memory:** use `hermes chat -c research-<topic_id>` and send
  only the latest message so the agent doesn't re-read filings every turn.
- **Async UX:** the dashboard currently blocks on the reply (15–60s). Add a
  queue + poll/SSE if the latency bites.
- **MATP/bars tools:** add a read endpoint on the dashboard, then teach the skill
  to pull them on demand.

## Changelog
- **2026-06-15** — v1.0.0. Created the shim + systemd unit. Pairs with the
  `markets/research-planning` skill and dashboard_tst's runner-mode chat relay
  (`research_llm.chat()` → this shim, DeepSeek-direct fallback).
