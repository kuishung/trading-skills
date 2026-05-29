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
echo
echo "Next: schedule the weekday 09:00 ET briefing (see hermes/README.md)."
