# Intraday system — setup on a fresh machine

Everything you need to install + configure to run this system on a new Windows / Mac / Linux box.

## 🔁 If you sync this folder via Dropbox (the common case here)

The user's setup syncs the **whole `Intraday/` folder via Dropbox** across multiple PCs. That changes what you have to redo on the second PC:

| What syncs automatically (Dropbox) | What you still install per machine |
|---|---|
| All skill source code & `SKILL.md` | Python 3.10+ (interpreter binary) |
| All `requirements.txt`, `package.json` | Node.js 18+ (runtime) |
| The `.env` credentials in each skill ⚠️ | Claude Code CLI |
| `.mcp.json` (BUT see path caveat below) | TradingView Desktop |
| `MATP_table.csv`, snapshots, generated outputs | Per-machine `pip install -r requirements.txt` |
| | Per-machine `npm install` in `tradingview-mcp/` |

### Three Dropbox-specific things to handle on the **second** PC

1. **Re-run `pip install` and `npm install`.** Source code travels, but installed packages (`__pycache__/`, `node_modules/`, `.venv/`) are platform-specific binaries. Don't sync them — exclude via Dropbox Selective Sync. See §1b below.
2. **Fix the `.mcp.json` absolute path** if your Dropbox folder lives at a different drive letter or username on the second PC. E.g., `D:\Dropbox\...` on PC1 might be `C:\Users\you\Dropbox\...` on PC2. Edit `.mcp.json` to match.
3. **Don't sync `.git/` across PCs via Dropbox.** Git creates lock files during commits, and Dropbox sync can corrupt them mid-operation if you `git commit` on both PCs near-simultaneously. Two options:
   - Best: move the repo OUT of Dropbox, use Dropbox only for outputs, and rely on `git push`/`pull` for source sync.
   - OK: keep in Dropbox but only commit from one PC at a time.

### Dropbox vs git — when to use which

- **Source code, `SKILL.md`, scripts** → git (version-controlled, attribution)
- **Generated outputs (CSVs, snapshots, Pine files, watchlists)** → fine to let Dropbox sync; gitignored anyway
- **`.env` credentials** → Dropbox is convenient but **a security trade-off** since they sync in plaintext across all your devices and live in Dropbox's cloud. If you ever share a Dropbox folder, share it without these files via Selective Sync exclusion.

### 🗝️ Central env folder pattern (auto-detected via Dropbox)

Instead of one `.env` per skill scattered across the tree, this system uses a **single central folder** for all credentials, kept in Dropbox so it syncs across all PCs:

```
<dropbox-root>/VAULT/Claude Credential/    ← same Dropbox-relative path on every PC
├── matp.env                ← MATP credentials (Telegram + Google + Drive)
├── alpaca.env              ← Alpaca paper API key/secret
└── intraday-premarket.env  ← Finviz URL + Telegram for the brief
```

**How it works**: each script's `_envpath.py` helper resolves the .env location in three tiers, first hit wins:

1. **`INTRADAY_ENV_DIR` env var** — if set and points at a real dir, use `<INTRADAY_ENV_DIR>/<name>.env`. For overrides / non-Dropbox setups.
2. **Auto-detected Dropbox-VAULT** — reads Dropbox's local `info.json` (`%LOCALAPPDATA%\Dropbox\info.json` on Windows, `~/.dropbox/info.json` on Mac/Linux) to find the Dropbox root, then looks for `<dropbox-root>/VAULT/Claude Credential/<name>.env`.
3. **Legacy fallback** — `<skill>/.env`, the original per-skill location.

**Setup on a new PC**: zero config needed if you have Dropbox installed and synced. The helper finds your Dropbox automatically, even when it lives at a different drive letter or username path (`D:\Dropbox\...` vs `C:\Users\you\Dropbox\...` vs `~/Dropbox/`). All you need is to install Dropbox and let `VAULT/Claude Credential/` sync down.

**When you might want the env var explicitly**: testing, a non-Dropbox machine, or pointing at a different folder for a particular run. Then:

```powershell
# Persistent (Windows User env var)
[Environment]::SetEnvironmentVariable("INTRADAY_ENV_DIR", "<your path>", "User")
# Or just for this session
$env:INTRADAY_ENV_DIR = "<your path>"
```

### Memory does NOT sync via Dropbox

Claude Code's memory directory lives at `%USERPROFILE%\.claude\projects\<project>\memory\` — outside Dropbox. If you want memory to follow you to the second PC, manually copy `~/.claude/projects/D--Dropbox-Claude-claude-skills-trading-skills/memory/` to the same path on the new PC. (Or accept that memory is per-machine and lean on `SETUP.md` + the per-skill `SKILL.md` files which DO sync.)

---

## §1a. System-level dependencies (install once per machine)

## 1. System-level dependencies (install once per machine — Dropbox doesn't help here)

| Dep | Min version | Used by | Check |
| --- | --- | --- | --- |
| **Python** | 3.10+ (3.14 verified) | MATP, alpaca-trader-paper, intraday-premarket-brief | `py --version` (Windows) / `python3 --version` |
| **Node.js + npm** | Node 18+ (24.x verified) | tradingview-mcp | `node --version && npm --version` |
| **Claude Code CLI** | latest | the whole orchestration | `claude --version` |
| **TradingView Desktop** | 3.1+ | tradingview-mcp (only) | see §5 below |
| **Git** | any recent | clone + worktrees | `git --version` |

Install order doesn't matter; install whichever are missing.

### 1b. Dropbox Selective Sync — exclude these (so binaries don't sync across platforms)

These folders contain platform-specific compiled artifacts. Syncing Windows binaries to Mac (or vice versa) will produce broken installs. Open Dropbox → Preferences → Selective Sync → exclude:

- `Intraday/tradingview-mcp/node_modules/` (and any other `node_modules/` you create)
- `Intraday/**/__pycache__/` (Python bytecode)
- `Intraday/**/.venv/` and `Intraday/**/venv/` (Python virtualenvs)
- `Intraday/**/.pytest_cache/` (test caches)

After exclusion, re-run `pip install -r requirements.txt` and `npm install` per skill on each PC.

Alternative: keep these synced and just accept that they'll be slow + occasionally broken — works fine if you only ever use one OS family across your PCs (e.g. Windows everywhere).

## 2. Clone (skip if Dropbox already has the folder)

```powershell
cd <wherever you keep code>
git clone <repo-url> Intraday
cd Intraday
```

If you use git worktrees the way this user does, do `git worktree add` per worktree as usual. The skills, scripts, and `.mcp.json` all live inside the worktree path.

## 3. Per-skill Python deps

Each skill has its own `requirements.txt`. Run from the skill's root directory:

```powershell
# MATP — analyst-target + bounce-alert pipeline
cd MATP
py -m pip install -r requirements.txt
py scripts/setup_sheets.py        # Google service account + Sheet ID
py scripts/setup_drive.py          # (optional) Drive folder for Pine + watchlist
py scripts/setup_telegram.py       # (optional) bot for daily bounce alert
cd ..

# alpaca-trader-paper — paper trading execution
cd alpaca-trader-paper
py -m pip install -r requirements.txt
py scripts/setup.py                # Alpaca paper API key + secret
cd ..

# intraday-premarket-brief — twice-daily pre-open scan
cd intraday-premarket-brief
py -m pip install -r requirements.txt
py scripts/setup.py                # Finviz URL + (optional) Telegram
cd ..
```

Each setup script prompts interactively and writes a gitignored `.env` to its skill root. **None of the `.env` files transfer via git.** You'll re-enter credentials on each new machine. See §6 for what credentials you need to gather.

## 4. tradingview-mcp install (if you want chart MCP)

```powershell
# Inside the Intraday root
git clone https://github.com/tradesdontlie/tradingview-mcp.git
cd tradingview-mcp
npm install
cd ..
```

Then create a project-scoped `.mcp.json` at the worktree root:

```json
{
  "mcpServers": {
    "tradingview": {
      "command": "node",
      "args": ["<ABSOLUTE PATH>/tradingview-mcp/src/server.js"]
    }
  }
}
```

Replace `<ABSOLUTE PATH>` with the actual path on the new machine. Use double-backslashes on Windows, e.g. `D:\\path\\to\\tradingview-mcp\\src\\server.js`.

## 5. TradingView Desktop on Windows

- Install via Microsoft Store (or `winget install TradingView.Desktop`).
- Store installs land under `C:\Program Files\WindowsApps\TradingView.Desktop_<version>\TradingView.exe` — but that folder is ACL'd to SYSTEM so cmd's `dir` can't list it.
- The upstream `tradingview-mcp/scripts/launch_tv_debug.bat` fails on Store installs. **This repo carries a patched version** that adds a `Get-AppxPackage` PowerShell fallback. If you re-clone `tradingview-mcp` on the new machine, re-apply the patch — see the commit/diff in the worktree for reference.

To launch TV with CDP enabled:

```powershell
cd tradingview-mcp
scripts\launch_tv_debug.bat
```

Leave that terminal open while you work — the CDP endpoint at `localhost:9222` dies when TradingView closes. Reload Claude Code after the launcher reports "CDP ready" so the MCP tools become available.

**TradingView Desktop on Mac/Linux**: use `scripts/launch_tv_debug_mac.sh` / `scripts/launch_tv_debug_linux.sh` — those don't have the WindowsApps quirk.

## 6. Credentials you need to gather (won't transfer via git)

| Skill | `.env` key | Source |
| --- | --- | --- |
| MATP | `GOOGLE_SA_KEY_PATH` | Google Cloud → IAM → Service Accounts → JSON key file (path on disk) |
| MATP | `MATP_SHEET_ID` | Google Sheets URL: `/d/<SHEET_ID>/edit` |
| MATP | `MATP_DRIVE_FOLDER_ID` | Drive folder URL: `/folders/<FOLDER_ID>` |
| MATP | `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` |
| MATP | `TELEGRAM_CHAT_ID` | DM the bot once, then `getUpdates` API |
| alpaca-trader-paper | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | https://app.alpaca.markets/paper/dashboard/overview |
| intraday-premarket-brief | `INTRADAY_FINVIZ_URL` | Your saved Finviz screener URL |
| intraday-premarket-brief | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Same bot as MATP, or a separate one |

**Required sharing**: the Google Sheet and Drive folder must be shared with the service-account email as Editor. The service-account email is in the JSON key file (`client_email` field).

## 7. Windows-specific gotchas (avoid wasting time)

1. **pip not on PATH**: `py -m pip install ...` instead of `pip install ...` on fresh Python installs.
2. **cp1252 default codepage**: emoji output crashes Python `print()`. All scripts in this repo do `sys.stdout.reconfigure(encoding="utf-8")` at startup. If you write new scripts, include that.
3. **WindowsApps ACL**: see §5. Affects only TradingView Desktop auto-detection.
4. **yfinance pre-market volume is always 0**: yfinance limitation, not Windows-specific, but flagged here so you don't waste time hunting. `intraday-premarket-brief` accepts this and uses gap-quality alone for scoring.
5. **`/tmp/` doesn't exist**: use `%TEMP%` or write to the repo dir.

## 8. Verification (run these to confirm everything works)

```powershell
# Python skills
py MATP/scripts/classify_trend.py --help
py alpaca-trader-paper/scripts/account.py --json
py intraday-premarket-brief/scripts/premarket_brief.py --mode t60 --tickers NVDA --no-telegram

# tradingview-mcp (after launching TV with CDP)
curl http://localhost:9222/json/version          # should return JSON, not error
# Then in a fresh Claude Code session: "use tv_health_check"
```

If any step errors, see the per-skill `SKILL.md` for that skill's troubleshooting notes.

## 9. What does NOT need re-setup

- The skills' source code and `SKILL.md` files travel with git — no install needed beyond `pip install -r requirements.txt`.
- The Pine indicator files in MATP regenerate on each MATP run.
- Snapshots in `intraday-premarket-brief/snapshots/` are gitignored and regenerate.
- The TradingView watchlist file regenerates from `MATP_table.csv`.
