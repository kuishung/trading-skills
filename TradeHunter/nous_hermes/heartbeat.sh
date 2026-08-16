#!/usr/bin/env bash
# heartbeat.sh — standalone liveness ping for TradeHunter's /agent page.
#
# WHY THIS EXISTS (read before "simplifying" it back into the matp skill):
# The online/stale signal on TradeHunter's /agent page is a trivial fixed HTTP
# POST. Routing it through the Hermes LLM (hermes-cron -> DeepSeek -> terminal
# tool -> approval gate) made it fragile: the model mangled the shell quoting,
# the agent's terminal tool gated outbound `curl` as `pending_approval` in
# unattended cron runs, and DeepSeek stream stalls dropped whole polls — so the
# beat often never landed and the dashboard flapped to "stale" even though the
# agent was perfectly alive (diagnosed 2026-06-01).
#
# This script is run by PLAIN SYSTEM CRON, not the agent. Running directly in
# bash it sidesteps ALL three failure modes at once:
#   - no LLM  -> no shell-mangling, no stream stalls
#   - no agent terminal tool -> no `pending_approval` gate
#   - reads ~/.hermes/.env at the OS level -> the "credential store cannot be
#     read directly" block is an agent-tool restriction, not a file permission
# The JSON body is built with `jq -n` (never string interpolation) so a quoting
# bug is structurally impossible.
#
# WHAT THIS PROVES — and what it does NOT (lesson from the 2026-08-06 outage):
# Because this script is deliberately decoupled from the agent, a successful
# beat originally proved only that THE BOX IS POWERED ON. The agent was dead for
# TEN DAYS (uv's managed Python was deleted by a disk-full cleanup, so
# venv/bin/python dangled and hermes-gateway crash-looped 170,904 times) while
# this script cheerfully POSTed "online" every 3 minutes and TradeHunter's
# /agent pill stayed green the whole time.
# So the beat now also reports MEASURED HEALTH in a `health` object:
#   agent_ok   - `hermes --version` actually ran (exit 0) -> the venv interpreter
#                and CLI are intact. This is the check that would have caught it.
#   gateway    - `systemctl --user is-active hermes-gateway.service`
#   disk_pct   - root filesystem usage; the disk filling is what started it
# Still no LLM involvement, so all the 2026-06-01 reliability properties hold.
#
# Server contract: POST {TRADEHUNTER_URL}/api/agent/heartbeat, header
#   X-API-Key: {TST_INGEST_API_KEY}. Body fields (all optional except via auth):
#   agent, version, host, polled_at (ISO-8601), crons (raw text), cron_jobs[]
#   {id, schedule, skills, prompt, next_run, active}, health{agent_ok,
#   agent_error, agent_version, gateway, disk_pct, disk_free, disk_path}. See
#   dashboard_tst/app/routes/api.py::agent_heartbeat + routes/agent.py.
#
# Install: bash nous_hermes/install.sh  (copies this to ~/.hermes/heartbeat.sh)
# Schedule (system cron, every 3 min):
#   */3 * * * * $HOME/.hermes/heartbeat.sh >/dev/null 2>&1
#
# Override paths via env: HERMES_ENV, HERMES_JOBS, HERMES_HB_LOG.

set -u

# CRON ENVIRONMENT — both of these are required for the health probes to MEASURE
# anything, and getting them wrong produces false alarms every 3 minutes (which
# is worse than the silence we're fixing: nobody trusts a pill that cries wolf).
#   PATH:            cron gives you /usr/bin:/bin only, but `hermes` lives in
#                    ~/.local/bin -> without this, `hermes --version` is "not
#                    found" and every beat would report agent_ok=false on a
#                    perfectly healthy box. (This also fixes `hermes cron list`
#                    below, which has silently returned empty under cron.)
#   XDG_RUNTIME_DIR: `systemctl --user` needs the user bus; a cron job has no
#                    session, so without this it fails with "Failed to connect
#                    to bus" and the gateway state is unmeasurable.
PATH="$HOME/.local/bin:$PATH"
export PATH
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

ENV_FILE="${HERMES_ENV:-$HOME/.hermes/.env}"
JOBS_FILE="${HERMES_JOBS:-$HOME/.hermes/cron/jobs.json}"
LOG_FILE="${HERMES_HB_LOG:-$HOME/.hermes/logs/heartbeat.log}"
VERSION="matp 1.8.0 (cron heartbeat)"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG_FILE" 2>/dev/null; }

# 1. Load only the two values we need from the agent secrets file. Tolerate an
#    optional `export ` prefix and surrounding quotes. We do NOT `source` the
#    whole file (it may contain other lines that a plain shell would choke on).
if [ -r "$ENV_FILE" ]; then
  eval "$(grep -E '^[[:space:]]*(export[[:space:]]+)?(TRADEHUNTER_URL|TST_INGEST_API_KEY)=' "$ENV_FILE" \
          | sed -E 's/^[[:space:]]*export[[:space:]]+//')"
fi

if [ -z "${TRADEHUNTER_URL:-}" ] || [ -z "${TST_INGEST_API_KEY:-}" ]; then
  log "ABORT: TRADEHUNTER_URL or TST_INGEST_API_KEY missing/unreadable in $ENV_FILE"
  exit 1
fi

# 2. Raw `hermes cron list` text — the server's fallback display if it gets no
#    structured cron_jobs. Never fatal.
CRONS_RAW="$(hermes cron list 2>/dev/null || true)"

# 3. Structured cron_jobs from the Hermes cron store, normalised with jq so the
#    output is always valid JSON. Handles the store being a bare array, a
#    {"jobs":[...]} wrapper, or an object keyed by id. Any failure => null
#    (liveness still posts; cron_jobs is a nice-to-have, not required).
CRON_JOBS='null'
if command -v jq >/dev/null 2>&1 && [ -r "$JOBS_FILE" ]; then
  CRON_JOBS="$(jq -c '
    ( .jobs // . )
    | ( if type=="object" then [.[]] else . end )
    | map({
        id:       ( (.id // .job_id) | if . == null then null else tostring end ),
        schedule: ( .schedule
                    | if   type=="object" then (.display // .expr // .cron // (.|tostring))
                      elif . == null       then null
                      else  tostring end ),
        skills:   ( (.skills // .skill)
                    | if   type=="array" then join(",")
                      elif . == null      then null
                      else  tostring end ),
        prompt:   (.prompt // .instruction // null),
        next_run: ( (.next_run // .next) | if . == null then null else tostring end ),
        active:   ( if   (.active // null) != null then .active
                    elif (.paused // null) != null then (.paused | not)
                    else true end )
      })
  ' "$JOBS_FILE" 2>/dev/null || echo 'null')"
  [ -z "$CRON_JOBS" ] && CRON_JOBS='null'
fi

# 3b. MEASURED HEALTH — the part that makes a green pill mean "the agent can
#     run", not just "the box is on". Each probe is bounded and soft-fails to a
#     safe value; a probe erroring must never stop the beat from landing.
#
#     agent_ok is the load-bearing one: `hermes --version` exercises the venv
#     interpreter + the CLI entry point, which is precisely what broke on
#     2026-08-06 and went unseen for ten days.
AGENT_OK=false
AGENT_ERR=""
AGENT_VER=""
if HV="$(timeout 25 hermes --version 2>&1)"; then
  AGENT_OK=true
  # first non-empty line, e.g. "Hermes Agent v0.15.1 (2026.5.29)"
  AGENT_VER="$(printf '%s\n' "$HV" | grep -m1 . || true)"
else
  # keep it short — this lands in a dashboard cell, not a log viewer
  AGENT_ERR="$(printf '%s\n' "$HV" | grep -m1 . | cut -c1-200 || true)"
  [ -z "$AGENT_ERR" ] && AGENT_ERR="hermes --version failed or timed out"
fi

# gateway unit state — it is a --user unit; "active" when healthy,
# "activating" while crash-looping, "inactive"/"failed" when down.
GATEWAY="$(systemctl --user is-active hermes-gateway.service 2>/dev/null || true)"
[ -z "$GATEWAY" ] && GATEWAY="unknown"

# root filesystem — the disk filling is what set off the whole 2026-08-06 chain.
DISK_PATH="/"
DISK_PCT="$(df -P "$DISK_PATH" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
DISK_FREE="$(df -Ph "$DISK_PATH" 2>/dev/null | awk 'NR==2 {print $4}')"
case "$DISK_PCT" in ''|*[!0-9]*) DISK_PCT=-1 ;; esac

HEALTH="$(jq -n \
  --argjson agent_ok "$AGENT_OK" \
  --arg agent_error   "$AGENT_ERR" \
  --arg agent_version "$AGENT_VER" \
  --arg gateway       "$GATEWAY" \
  --argjson disk_pct  "$DISK_PCT" \
  --arg disk_free     "${DISK_FREE:-}" \
  --arg disk_path     "$DISK_PATH" \
  '{agent_ok:$agent_ok, agent_error:$agent_error, agent_version:$agent_version,
    gateway:$gateway, disk_pct:$disk_pct, disk_free:$disk_free,
    disk_path:$disk_path}' 2>/dev/null || echo 'null')"
[ -z "$HEALTH" ] && HEALTH='null'

# 4. Build the payload with jq -n (bulletproof JSON — no manual quoting) & POST.
PAYLOAD="$(jq -n \
  --arg agent   "nous_hermes" \
  --arg version "$VERSION" \
  --arg host    "$(hostname)" \
  --arg polled  "$(date -u +%FT%TZ)" \
  --arg crons   "$CRONS_RAW" \
  --argjson jobs "$CRON_JOBS" \
  --argjson health "$HEALTH" \
  '{agent:$agent, version:$version, host:$host, polled_at:$polled,
    crons:$crons, cron_jobs:$jobs, health:$health}')"

if [ -z "$PAYLOAD" ]; then
  log "ABORT: failed to build JSON payload (is jq installed?)"
  exit 1
fi

RESP_FILE="$(mktemp /tmp/hb_resp.XXXXXX 2>/dev/null || echo /tmp/hb_resp.$$)"
HTTP="$(curl -sS -o "$RESP_FILE" -w '%{http_code}' \
  -X POST "$TRADEHUNTER_URL/api/agent/heartbeat" \
  -H "X-API-Key: $TST_INGEST_API_KEY" \
  -H "Content-Type: application/json" \
  --max-time 20 \
  -d "$PAYLOAD" 2>>"$LOG_FILE")"
RESP="$(cat "$RESP_FILE" 2>/dev/null)"
rm -f "$RESP_FILE"

if [ "$HTTP" = "200" ]; then
  log "OK $HTTP agent_ok=$AGENT_OK gateway=$GATEWAY disk=${DISK_PCT}% $RESP"
else
  log "FAIL http=${HTTP:-none} agent_ok=$AGENT_OK gateway=$GATEWAY resp=${RESP:-<empty>}"
fi
