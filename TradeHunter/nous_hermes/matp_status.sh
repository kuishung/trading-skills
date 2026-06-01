#!/usr/bin/env bash
# matp_status.sh — report MATP refresh-request progress to TradeHunter.
#
# WHY THIS EXISTS: the agent drives the /agent page's moving progress bar by
# POSTing /api/refresh-queue/{rid}/status as it works. Letting the LLM hand-build
# that curl+JSON every few tickers is exactly what kept failing (DeepSeek mangled
# the shell quoting; see heartbeat.sh's header). This wrapper takes positional
# args and builds the JSON with `jq -n`, so the agent just runs a fixed command
# it can't garble:
#
#   matp_status.sh <rid> <status> [done] [total] [note]
#     status = running | done | failed
#
# Examples:
#   matp_status.sh 7 running 0 25 "starting growth screen"   # claim + set total
#   matp_status.sh 7 running 10                              # progress -> 10/25
#   matp_status.sh 7 done "" "" "refreshed 25 tickers"       # finish (bar -> 100%)
#   matp_status.sh 7 failed "" "" "marketbeat blocked"       # error
#
# Omitted/empty numeric args are simply not sent (partial update). Unlike the
# liveness heartbeat this CANNOT run from system cron — only the agent knows its
# own progress mid-run — so it still goes through the agent's terminal tool; if
# the box gates outbound POSTs as pending_approval, allowlist THIS script's path
# (one fixed path is far easier to allow than arbitrary curls).
#
# Server contract: POST {TRADEHUNTER_URL}/api/refresh-queue/{rid}/status, header
#   X-API-Key, body {status, note?, progress_done?, progress_total?}. See
#   dashboard_tst/app/routes/api.py::update_refresh_status.

set -u

ENV_FILE="${HERMES_ENV:-$HOME/.hermes/.env}"
LOG_FILE="${HERMES_HB_LOG:-$HOME/.hermes/logs/matp_status.log}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG_FILE" 2>/dev/null; }

usage() { echo "usage: matp_status.sh <rid> <status:running|done|failed> [done] [total] [note]" >&2; exit 2; }

RID="${1:-}"; STATUS="${2:-}"
DONE_ARG="${3:-}"; TOTAL_ARG="${4:-}"; NOTE="${5:-}"
[ -n "$RID" ] && [ -n "$STATUS" ] || usage
case "$STATUS" in running|done|failed) ;; *) echo "bad status: $STATUS" >&2; usage ;; esac

# Load only the two values we need from the agent secrets file (OS-level read,
# bypassing the agent-tool credential-read block). Tolerate `export `/quotes.
if [ -r "$ENV_FILE" ]; then
  eval "$(grep -E '^[[:space:]]*(export[[:space:]]+)?(TRADEHUNTER_URL|TST_INGEST_API_KEY)=' "$ENV_FILE" \
          | sed -E 's/^[[:space:]]*export[[:space:]]+//')"
fi
if [ -z "${TRADEHUNTER_URL:-}" ] || [ -z "${TST_INGEST_API_KEY:-}" ]; then
  log "ABORT rid=$RID: TRADEHUNTER_URL or TST_INGEST_API_KEY missing in $ENV_FILE"; exit 1
fi

# Only send numeric progress fields when given a clean integer (else omit).
is_int() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
DONE='null';  is_int "$DONE_ARG"  && DONE="$DONE_ARG"
TOTAL='null'; is_int "$TOTAL_ARG" && TOTAL="$TOTAL_ARG"

PAYLOAD="$(jq -n \
  --arg status "$STATUS" --arg note "$NOTE" \
  --argjson done "$DONE" --argjson total "$TOTAL" \
  '{status:$status}
   + (if $note  != ""   then {note:$note}            else {} end)
   + (if $done  != null then {progress_done:$done}   else {} end)
   + (if $total != null then {progress_total:$total} else {} end)')"
[ -z "$PAYLOAD" ] && { log "ABORT rid=$RID: failed to build JSON (jq?)"; exit 1; }

RESP_FILE="$(mktemp /tmp/matp_status.XXXXXX 2>/dev/null || echo /tmp/matp_status.$$)"
HTTP="$(curl -sS -o "$RESP_FILE" -w '%{http_code}' \
  -X POST "$TRADEHUNTER_URL/api/refresh-queue/$RID/status" \
  -H "X-API-Key: $TST_INGEST_API_KEY" -H "Content-Type: application/json" \
  --max-time 20 -d "$PAYLOAD" 2>>"$LOG_FILE")"
RESP="$(cat "$RESP_FILE" 2>/dev/null)"; rm -f "$RESP_FILE"

if [ "$HTTP" = "200" ]; then
  log "OK rid=$RID $STATUS done=$DONE total=$TOTAL"
  echo "$RESP"
else
  log "FAIL rid=$RID $STATUS http=${HTTP:-none} resp=${RESP:-<empty>}"
  echo "$RESP" >&2
  exit 1
fi
