# Vendored upstream

This folder is a **vendored copy** of:

    https://github.com/tradesdontlie/tradingview-mcp

Vendored at: **commit `4795784a19dd64ff4e2649d2499a536b01bd2d68`**
(Merge: fix/cdp-injection-sanitization, 2026-04-03)

The original `.git` was stripped at vendoring time. Treat this as a
read-only third-party library copied in tree — don't make local
edits unless absolutely necessary; instead update the upstream commit
when needed.

## Why vendored (not git-cloned or git-submodule)

Per the day-one rule in `TradeHunter/CLAUDE.md`:

> **Every dependency lives inside TradeHunter/.**
> NO sibling-folder reads. Don't break the cross-PC sync invariant.

A clone outside the folder (e.g. `~/mcp-servers/`) doesn't sync via
Dropbox. A git submodule requires `git submodule update --init` on
every fresh sync. Vendoring is the simplest answer: the code travels
with TradeHunter, the user only needs to `npm install` once per PC.

## How to update

```bash
# In a scratch location (NOT inside TradeHunter/), clone fresh:
git clone https://github.com/tradesdontlie/tradingview-mcp.git /tmp/tv-mcp
cd /tmp/tv-mcp
git log -1 --format="%H %ci %s"     # record the new commit hash

# Replace in tree:
rm -rf TradeHunter/resources/tradingview-mcp
mv /tmp/tv-mcp TradeHunter/resources/tradingview-mcp
rm -rf TradeHunter/resources/tradingview-mcp/.git

# Update the commit line above in this file.
# Then re-run npm install per PC:
cd TradeHunter/resources/tradingview-mcp
npm install
```

## Per-PC install steps

`node_modules/` is gitignored (and Dropbox-sync friendly to skip since
it's per-OS). After a fresh sync to a new PC:

```bash
cd TradeHunter/resources/tradingview-mcp
npm install
```

The Claude Code MCP registration in `~/.claude/.mcp.json` is also
per-PC (absolute path); see `TradeHunter/CLAUDE.md`.
