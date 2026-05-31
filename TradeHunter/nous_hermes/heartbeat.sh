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
# Server contract: POST {TRADEHUNTER_URL}/api/agent/heartbeat, header
#   X-API-Key: {TST_INGEST_API_KEY}. Body fields (all optional except via auth):
#   agent, version, host, polled_at (ISO-8601), crons (raw text), cron_jobs[]
#   {id, schedule, skills, prompt, next_run, active}. See
#   dashboard_tst/app/routes/api.py::agent_heartbeat + routes/agent.py.
#
# Install: bash nous_hermes/install.sh  (copies this to ~/.hermes/heartbeat.sh)
# Schedule (system cron, every 3 min):
#   */3 * * * * $HOME/.hermes/heartbeat.sh >/dev/null 2>&1
#
# Override paths via env: HERMES_ENV, HERMES_JOBS, HERMES_HB_LOG.

set -u

ENV_FILE="${HERMES_ENV:-$HOME/.hermes/.env}"
JOBS_FILE="${HERMES_JOBS:-$HOME/.hermes/cron/jobs.json}"
LOG_FILE="${HERMES_HB_LOG:-$HOME/.hermes/logs/heartbeat.log}"
VERSION="matp 1.7.1 (cron heartbeat)"

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

# 4. Build the payload with jq -n (bulletproof JSON — no manual quoting) & POST.
PAYLOAD="$(jq -n \
  --arg agent   "nous_hermes" \
  --arg version "$VERSION" \
  --arg host    "$(hostname)" \
  --arg polled  "$(date -u +%FT%TZ)" \
  --arg crons   "$CRONS_RAW" \
  --argjson jobs "$CRON_JOBS" \
  '{agent:$agent, version:$version, host:$host, polled_at:$polled,
    crons:$crons, cron_jobs:$jobs}')"

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
  log "OK $HTTP $RESP"
else
  log "FAIL http=${HTTP:-none} resp=${RESP:-<empty>}"
fi
