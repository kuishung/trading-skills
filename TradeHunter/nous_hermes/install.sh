#!/usr/bin/env bash
# install.sh — deploy this repo's Hermes skills into the agent's skills dir.
#
# Run ON THE LINUX SERVER where the Nous Hermes agent is installed.
# It copies every skill under ./skills/ into ~/.hermes/skills/, preserving the
# category/skill-name folder layout Hermes expects.
#
# Usage:
#   bash nous_hermes/install.sh      # copy (overwrites matching files)
#   HERMES_HOME=/custom/.hermes bash nous_hermes/install.sh
#
# Idempotent: re-run after every update (git pull / scp) to refresh.

set -euo pipefail

# Resolve the directory this script lives in (the repo's nous_hermes/ folder),
# so it works regardless of the current working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/skills"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_HOME/skills"

if [ ! -d "$SRC" ]; then
  echo "ERROR: no skills/ dir found at $SRC" >&2
  exit 1
fi

if [ ! -d "$HERMES_HOME" ]; then
  echo "WARNING: $HERMES_HOME does not exist yet." >&2
  echo "Run 'hermes setup' first so the agent home is initialised." >&2
fi

mkdir -p "$DEST"

echo "Deploying skills:"
# Copy each category/skill-name tree. -a preserves structure; this overwrites
# files that changed but leaves agent-created skills in other folders intact.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --info=NAME "$SRC"/ "$DEST"/
else
  cp -av "$SRC"/. "$DEST"/
fi

echo
echo "Done. Installed into: $DEST"
echo "Verify in the agent with:  /skills   (or)  hermes skills list"

# --- Standalone bash helpers (deterministic, no LLM shell-mangling) ----------
# Deploy the ops helper scripts next to the agent home and make them executable.
#   heartbeat.sh   — liveness ping, run from SYSTEM CRON (not the LLM)
#   matp_status.sh — progress reporter, called BY the agent with positional args
# See each script's header for why they live outside the skill/LLM curl path.
for helper in heartbeat.sh matp_status.sh; do
  src="$SCRIPT_DIR/$helper"
  dest="$HERMES_HOME/$helper"
  if [ -f "$src" ]; then
    cp "$src" "$dest"
    chmod +x "$dest"
    echo "Installed helper: $dest"
  fi
done

# --- research-runner shim (LAN-only agent-grounded chat for the Research page) -
# Copy server.py + the systemd unit template into ~/.hermes/research_runner/.
# Starting the service is a one-time manual step (see research_runner/README.md)
# because it needs a token in ~/.hermes/.env first.
RR_SRC="$SCRIPT_DIR/research_runner"
RR_DEST="$HERMES_HOME/research_runner"
if [ -d "$RR_SRC" ]; then
  mkdir -p "$RR_DEST"
  cp "$RR_SRC/server.py" "$RR_DEST/server.py"
  cp "$RR_SRC/research-runner.service" "$RR_DEST/research-runner.service"
  echo "Installed research-runner shim: $RR_DEST/server.py"
  if grep -q '^TST_RESEARCH_TOKEN=' "$HERMES_HOME/.env" 2>/dev/null; then
    echo "  research-runner token: present in ~/.hermes/.env."
    echo "  (Re)start it with:  systemctl --user restart research-runner"
  else
    echo "  research-runner token: NOT set. See research_runner/README.md to set"
    echo "  TST_RESEARCH_TOKEN and start the systemd user service."
  fi
fi

# Schedule the liveness heartbeat (every 3 min) if not already in the crontab.
HB_DEST="$HERMES_HOME/heartbeat.sh"
if [ -f "$HB_DEST" ]; then
  CRON_LINE="*/3 * * * * $HB_DEST >/dev/null 2>&1"
  if crontab -l 2>/dev/null | grep -qF "$HB_DEST"; then
    echo "  heartbeat cron: already scheduled."
  else
    echo "  heartbeat cron: NOT scheduled yet. Install the every-3-min job with:"
    echo "    ( crontab -l 2>/dev/null; echo '$CRON_LINE' ) | crontab -"
    echo "  Then verify:  crontab -l   and   tail ~/.hermes/logs/heartbeat.log"
  fi
fi

echo
echo "Next: schedule the weekday 09:00 ET briefing (see nous_hermes/README.md)."
