# Hermes setup — Hyper-V VM on the R720

**Hermes** is a dedicated Hyper-V VM running on the user's office Dell R720 server (Windows Server 2019 host with Hyper-V role). It's the autonomous worker for **data ingest** and **backtest runs** — long-running jobs that shouldn't tie up the laptop.

This file is the one-shot install guide. Follow top-to-bottom on a freshly-installed Server 2019 VM. Total time: ~30 min after Windows finishes installing.

---

## What Hermes does vs what the laptop does

| Job | Hermes | Laptop |
|---|---|---|
| Continuous data ingest (3min / 1min / daily) | ✅ | ❌ (frees laptop) |
| Long backtest runs (parameter sweeps, multi-day windows) | ✅ | ❌ |
| Quick backtest iterations | Either | ✅ Preferred (faster iteration) |
| Strategy code editing | ❌ | ✅ |
| Dashboard during US market hours | ✅ (recommended) or laptop | Either |
| Live trading bot (when DITP P2 goes live) | ✅ (future — out of scope for v1 setup) | ❌ |
| TradingView Desktop chart review | ❌ | ✅ |
| Git commits / pushes | Either | Either |

**Mental model:** Hermes = autonomous worker. Laptop = developer cockpit.

---

## VM specs (set in Hyper-V Manager BEFORE installing Windows)

| Setting | Value | Why |
|---|---|---|
| Generation | **Gen 2** | UEFI boot, faster, modern features. Always Gen 2 for any 2019+ guest. |
| RAM (startup) | **16 GB static** (NOT dynamic) | Static = predictable; backtests don't tolerate balloon thrashing. Real working set ~2 GB; 16 GB gives big headroom. |
| vCPU | **4 cores** | Single-threaded workloads; 4 is comfortable. |
| Hard disk | **200 GB VHDX, dynamic** | Initial: ~20 GB OS + ~10 GB parquets. Grows as we add timeframes. Dynamic = only consumes what's used. |
| Network adapter | **External switch** (bridged to physical NIC) | Hermes gets own LAN IP. Reachable from laptop via RDP. Direct internet to IBKR / Alpaca / Dropbox. |
| Integration Services | All enabled | Standard. |
| Automatic start action | **Always start automatically, delay 60 sec** | If R720 reboots, Hermes comes back online. 60 sec delay so host finishes booting first. |
| Automatic stop action | **Save state** | Clean suspend on host shutdown. |
| Secure Boot | On, template = "Microsoft Windows" | Default for Gen 2 Windows. |
| Checkpoint type | Production checkpoint | App-consistent snapshots. |
| Smart paging file | Default | Rarely triggers with 16 GB static. |

## Windows installation choices

- **Edition:** Windows Server 2019 **Standard (Desktop Experience)** — NOT Core. We need GUI for Dropbox, IB Gateway, and IBC.
- **Computer name:** `HERMES`
- **Timezone:** Match your laptop (MYT / UTC+8). The bot does ET conversion internally.
- **Administrator password:** Strong, save to your password manager
- **License key:** Skip if doing eval-mode for now (180 days to validate the architecture before licensing)

After first boot:
1. **Disable Server Manager auto-start** (it pops up every login, annoying): `Server Manager → Manage → Server Manager Properties → "Do not start Server Manager automatically at logon"`
2. **Enable Remote Desktop:** `System → Remote Desktop → Enable Remote Desktop → Allow connections`. Verify firewall rule was created automatically.
3. **Test RDP from laptop:** `mstsc → HERMES.local` (or its LAN IP). Confirm you can connect.
4. **Set up auto-login** (so the VM logs in automatically after host reboot — needed for IBC + Dropbox to start):
   - Win+R → `control userpasswords2`
   - Untick "Users must enter a user name and password to use this computer"
   - Enter administrator password twice to confirm
5. **Disable sleep / hibernate:** `Power Options → Change plan settings → Never sleep, never turn off display`. Critical — sleep would kill the ingest.

---

## Software install order (Phase 1: base runtime)

Open PowerShell **as Administrator** for all of these.

### 1. Install Chocolatey (package manager)
```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

### 2. Install Python 3.12 (NOT 3.13 or 3.14), Git, Notepad++ (optional)

**CRITICAL: Install Python 3.12 specifically. NOT 3.13, 3.14, or newer.**

```powershell
choco install -y python312 git notepadplusplus
```

Why this matters: `ib_insync` (our IBKR client) depends on `eventkit`, which calls `asyncio.get_event_loop()` at module-import time. Python 3.14 removed the implicit event-loop creation (it now raises `RuntimeError: There is no current event loop in thread 'MainThread'`), which means **ib_insync cannot even be imported on Python 3.14** without a workaround. We discovered this on 2026-05-24 when the 180-day re-seed launcher crashed on a Python 3.14 interpreter.

If you have multiple Python versions installed (common on dev machines), always invoke IBKR-related scripts with `py -3.12` explicitly:

```powershell
py -3.12 resources/ibkr_history.py update --universe --timeframes 3min --seed-days 180 --force-seed
py -3.12 scripts/wait_and_ingest.py --timeframes 3min --seed-days 180 --force-seed --universe daily
```

Non-IBKR work (scanner, backtest, dashboard) runs fine on either version.

Restart PowerShell so PATH picks up the new installs. Verify:
```powershell
py -3.12 --version       # should show Python 3.12.x
git --version
```

### 3. Install Dropbox + sign in (CODE ONLY — data syncs via Resilio, see step 5)
- Download from <https://www.dropbox.com/install>
- Install, sign in with your Dropbox account
- **Selective Sync:** in Dropbox preferences, sync ONLY the `Claude/claude-skills/trading-skills/intraday-bot/` folder tree. **Crucially, EXCLUDE `intraday-bot/data/`** — the bulk data does NOT go through Dropbox (Resilio handles it in step 5). Don't sync your personal Dropbox stuff to a server VM either.
- Wait for sync. Should complete in ~2-5 min since we excluded the heavy data folder.
- Confirm: `Test-Path C:\<dropbox-root>\intraday-bot\SKILL.md` → True

### 4. Install intraday-bot Python deps (Python 3.12 explicitly)
```powershell
cd C:\<dropbox-root>\intraday-bot
py -3.12 -m pip install --upgrade pip
py -3.12 -m pip install -r requirements.txt
```

### 5. Install Resilio Sync + connect to laptop's HermesSync share

Resilio Sync is the peer-to-peer sync mechanism for the bulk data (parquets, journals, reviews, ingest logs). The laptop is already running it; this step adds Hermes as a second peer.

1. Download Resilio Sync from <https://www.resilio.com/individuals/>
2. Install. On first launch, choose "Standard install" (no need to create an account)
3. On the **laptop**, generate a Read & Write key for the laptop's `D:\HermesSync\` folder:
   - Right-click `D:\HermesSync` → Resilio Sync → Share
   - Choose **Read & Write** access
   - Copy the generated key (or QR code, or one-time link)
4. On Hermes, in Resilio:
   - Click "Add folder" → "Enter a key or link"
   - Paste the laptop's key
   - Set local path to `C:\HermesSync` (create this folder if needed)
5. Initial sync begins — ~664 MB or more of MarketData transfers over LAN (~5-10 min on Gbps LAN)
6. Verify after sync:
   ```powershell
   Test-Path "C:\HermesSync\MarketData\price_history\daily\AAPL.parquet"   # → True
   ```

### 6. Configure data_root in Hermes's config.json

The bot code reads `cfg["data_root"]` and uses it everywhere for data I/O. Each PC sets its own local Resilio path.

```powershell
# Copy config.example.json to config.json (per-PC, gitignored)
cd "C:\<dropbox-root>\intraday-bot"
Copy-Item config.example.json config.json
```

Then edit `config.json` (Notepad++ recommended) and set:

```jsonc
{
  // ... other settings ...
  "data_root": "C:\\HermesSync\\MarketData",
  // ... other settings ...
  "ibkr_client_id": 84,        // Hermes-specific (laptop uses 71/80/83/99)
  // ... other settings ...
}
```

Verify the override is picked up:
```powershell
py -3.12 -c "
import sys; sys.path.insert(0, 'scripts')
from _common import get_data_root
print('data_root resolves to:', get_data_root())
"
# Expected: data_root resolves to: C:\HermesSync\MarketData
```

---

## Software install order (Phase 2: IBKR)

### 7. Install IB Gateway (not TWS)
- Download IB Gateway from <https://www.interactivebrokers.com/en/trading/ibgateway-stable.php> (paper trading version)
- Install with defaults
- **Do NOT launch it manually** — IBC will manage it

### 8. Configure IBC (Interactive Brokers Controller)
IBC lives in `intraday-bot/ibc/` — already configured. Adapt config for Hermes:

1. The IBKR credentials file is at `C:\HermesSync\Vault\credentials.txt` (synced from laptop via Resilio in step 5). Format:
   ```
   IbLoginId=<your paper username>
   IbPassword=<your paper password>
   TradingMode=paper
   ```
   You do NOT need to create or copy this file manually — Resilio sync delivers it. Just verify it exists:
   ```powershell
   Test-Path "C:\HermesSync\Vault\credentials.txt"   # → True
   ```

2. Open `C:\Code\trading-skills\intraday-bot\ibc\config.ini` and verify:
   ```
   IbDir=C:\Jts\ibgateway\<version>     # adjust to where IB Gateway was installed
   FIX=no
   TradingMode=paper
   IbLoginId=                            # leave blank, picked up from credentials file
   IbPassword=                           # leave blank, picked up from credentials file
   ```

3. Update `intraday-bot/config.json` (gitignored, per-PC). For Hermes, the key fields:
   ```json
   {
     "data_root": "C:\\HermesSync\\MarketData",
     "vault_dir": "C:\\HermesSync\\Vault",                                       // ← lets bot find alpaca.env, etc.
     "ibkr_host": "127.0.0.1",
     "ibkr_port": 4002,
     "ibkr_client_id": 84,                                                       // ← Hermes uses 84 (laptop uses 71)
     "ibkr_secrets_path": "C:\\HermesSync\\Vault\\credentials.txt",              // ← IBC reads from Resilio-synced location
     "ibkr_ibc_dir": "C:\\Code\\trading-skills\\intraday-bot\\ibc",
     "ibkr_app_type": "gateway",
     ...rest copied from laptop config...
   }
   ```

4. Create a Windows scheduled task to launch IBC at logon:
   - Task Scheduler → Create Basic Task → "IBC IB Gateway"
   - Trigger: "When I log on"
   - Action: Start a program → `C:\<dropbox-root>\intraday-bot\ibc\StartGateway.bat`
   - In task properties → "Run with highest privileges"
   - Save

5. Reboot Hermes. After auto-login, IBC should auto-launch IB Gateway and log in. Verify by:
   - RDP in
   - IB Gateway window visible
   - Bottom-right shows "Connected" + green light

### 9. Smoke-test the IBKR connection
```powershell
cd C:\<dropbox-root>\intraday-bot
py resources/ibkr_smoke.py
```
Expected: prints account ID, no errors.

---

## Software install order (Phase 3: bot verification)

### 10. Pre-flight check
```powershell
py scripts/hermes_health.py
```
This CLI (see `scripts/hermes_health.py`) verifies:
- Python version OK
- Required packages importable
- Dropbox sync paths resolve
- bars_store readable (sees the existing parquets)
- IBKR handshake (via clientId 85 probe, not 84)
- Sufficient disk space (>50 GB free)
- IBC's `credentials.txt` exists (without printing it)

All checks should pass. If any fail, fix before continuing.

### 11. Scanner smoke test
```powershell
py strategy/DITP/scanner.py --no-write
```
Expected: prints ~10 candidates, no errors. Verifies the daily parquets are readable on Hermes.

### 12. Backtest smoke test (no IBKR needed)
```powershell
py review/backtest.py --list-strategies
py review/backtest.py --strategy ditp_p2 --start 2026-05-12 --end 2026-05-22 --no-write
```
Expected: ~2 trades (same as laptop's earlier run). Verifies the backtest harness works on Hermes.

### 13. Tiny IBKR ingest test
```powershell
py resources/ibkr_history.py ingest AAPL MSFT --timeframes 3min --days 5
```
Expected: pulls ~390 bars per symbol (5 days × 78 RTH bars + extended hours). Verifies Hermes can talk to IBKR end-to-end.

---

## Kick off the real work: 180-day full-universe re-seed

Once all smoke tests pass, this is the command to run for the actual backtest data preparation:

```powershell
# In an Administrator PowerShell, from intraday-bot/ root:
py scripts/wait_and_ingest.py --timeframes 3min --seed-days 180 --force-seed --universe daily
```

- Runs ~80-100 hours (3-4 days continuous)
- Logs to `data/_ingest_3min_180d_<ts>.log` (tailable)
- Auto-reconnect handles TWS hiccups
- Parquets flow into Dropbox → laptop sees them in real-time
- DO NOT close the PowerShell window during the run. Use Windows scheduled task or `nohup`-equivalent if you want to log out / disconnect RDP without killing it.

**Better: launch as a Windows scheduled task** so it survives RDP disconnects + Hermes restarts:
- Task Scheduler → Create Task → "DITP P2 180d ingest"
- Trigger: One time, today
- Action: `py.exe` with arguments `C:\<dropbox-root>\intraday-bot\scripts\wait_and_ingest.py --timeframes 3min --seed-days 180 --force-seed --universe daily`
- Start in: `C:\<dropbox-root>\intraday-bot`
- "Run whether user is logged on or not"
- "Run with highest privileges"

The watcher + ingest continue even if you RDP out. Re-RDP in any time to check progress via the log file.

---

## Daily operations (after the 180-day re-seed completes)

### Incremental updates (every market day)
Hermes runs the daily 3min top-up after US market close (16:00 ET). Set up via Windows scheduled task or the bot's own EOD hook:

```powershell
py resources/ibkr_history.py update --universe --timeframes 3min --pacing 7
```

This is incremental (fills the gap from last stored bar to now) — fast, ~10 min for full universe.

### Backtest runs
From either machine:
```powershell
py review/backtest.py --strategy ditp_p2 --start 2026-05-01 --end 2026-05-31
```
Backtests are minutes; run anywhere.

### Tail the running ingest
From Hermes RDP session:
```powershell
Get-Content data\_ingest_3min_180d_*.log -Tail 50 -Wait
```

### Check IBC / IB Gateway is healthy
- RDP in
- Look for IB Gateway window: green light bottom-right = connected
- Or programmatically: `py resources/ibkr_smoke.py`

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Cannot connect to IB Gateway" | IBC didn't auto-launch | Check Task Scheduler "IBC IB Gateway" task is enabled; manually run `ibc/StartGateway.bat` |
| "clientId 84 already in use" | Previous ingest didn't disconnect cleanly | Kill any orphan `python.exe` processes; restart IB Gateway via IBC |
| Dropbox parquets not appearing on laptop | Dropbox not syncing | Right-click Dropbox icon → "Pause syncing" then "Resume syncing"; check selective-sync includes `intraday-bot/` |
| `pyarrow` not installed | requirements.txt missed | `py -m pip install pyarrow` |
| Hermes time drift from real time | NTP not configured | `w32tm /resync /force`; ensure VM time sync via Hyper-V Integration Services is enabled |
| Ingest dies after RDP disconnect | Running interactively, not as scheduled task | Re-launch via Task Scheduler "Run whether user is logged on or not" |
| Hermes appears down from laptop | VM saved-state by host | Hyper-V Manager → Hermes → Start |

---

## Architecture diagrams

### Data flow
```
                IBKR API (paper account)
                    │
                    │ clientId 84 (ingest)
                    │ clientId 85 (smoke probes)
                    │
            ┌───────▼────────┐
            │  Hermes VM     │
            │  Win Srv 2019  │
            │                │
            │  IB Gateway    │
            │  + IBC         │
            │  + Python 3.12 │
            │  + bot code    │
            └───────┬────────┘
                    │ writes
                    │
            ┌───────▼────────┐
            │ Dropbox sync   │
            │ data/price_    │
            │  history/      │
            └───────┬────────┘
                    │
       ┌────────────┼────────────┐
       │            │            │
    Laptop      Other PC     Hermes (re-read)
```

### IBKR clientId allocation (across all machines)

| ClientId | Used by | Where |
|---|---|---|
| 71 | Live trading bot | Laptop (current) → Hermes (future) |
| 80 | Observer / dashboard streamer | Laptop |
| 83 | Ingest (legacy/current) | Laptop |
| **84** | **Ingest (production)** | **Hermes** |
| **85** | **Health-check probes** | **Hermes** |
| 98 | Probe / handshake test | Either |
| 99 | Dashboard probe | Laptop |

Don't reuse these in new code — append next available number for any future component.

---

## What's NOT in this setup yet (deferred)

- **Live trading bot on Hermes** — when DITP P2 goes live, we add the orchestrator as a scheduled task. Out of scope for v1 setup.
- **Dashboard on Hermes** — could run here, but launching from laptop while you're at it is fine. Optional later add.
- **Telegram credentials** — only needed when live trading; ingest + backtest don't send alerts.
- **Alpaca credentials** — only needed when live trading; backtest doesn't submit orders.
- **Backtest LLM critique loop** — `review/propose.py` runs anywhere; can add Hermes cron job later.

When any of these become needed, add a new phase to this doc rather than scattering setup steps elsewhere.

---

## Changelog

### 2026-05-24 — Vendor credentials (alpaca.env, intraday-premarket.env, credentials.txt) also move to HermesSync/Vault/

Same-day follow-up to the data-folder relocation: the laptop's Dropbox device-limit blocks installing Dropbox on Hermes, so credentials need a different sync mechanism. Migrated all 3 credential files used by intraday-bot (and the sibling intraday-premarket skill) from `D:\Dropbox\VAULT\Claude Credential\` to `D:\HermesSync\Vault\`. Resilio handles laptop ↔ Hermes sync. Hermes never needs Dropbox.

**Code change:** `scripts/_common.py::_vault_root()` now honours `cfg["vault_dir"]` (new config option), mirroring the `data_root` pattern. Default behaviour (no override) still auto-discovers via the legacy walk-up. Adapter chain in `_env_lookup()` unchanged — `INTRADAY_ENV_DIR` env var still takes priority, then in-folder `.env`, then the (now configurable) vault dir.

**Files migrated:**
- `credentials.txt` (43 bytes) — IBKR paper creds, consumed by IBC
- `alpaca.env` (164 bytes) — Alpaca API keys, consumed by `load_alpaca_env()`
- `intraday-premarket.env` (207 bytes) — sibling skill (not used by intraday-bot itself, but moved for unified location)

**Config additions** on each PC:
- Laptop `config.json`: `"vault_dir": "D:\\HermesSync\\Vault"`
- Hermes `config.json`: `"vault_dir": "C:\\HermesSync\\Vault"`

Verified: orchestrator dry-run boots cleanly, Alpaca creds resolve via new path, `_vault_root()` returns the configured location.

### 2026-05-24 — Data folder moves out of Dropbox to Resilio-synced HermesSync

Architectural change made same-day as initial setup: the data folder (parquets, journals, reviews, ticker profiles, ingest logs — ~10GB and growing) is too heavy for Dropbox sync. Moved to a peer-to-peer sync folder using Resilio Sync.

**New layout:**
- Laptop: `D:\HermesSync\MarketData\` (was `intraday-bot\data\`)
- Hermes: `C:\HermesSync\MarketData\` (junction-free direct location)
- Sync: Resilio P2P over LAN between the two

**Code change:** all path constants (`bars_store.PRICE_HISTORY_ROOT`, `journal.writer.JOURNAL_DIR`, `review.stats.JOURNAL_DIR + REVIEW_DIR`, `ticker_profile.profile_path()`, `bars_store.INGEST_LOG_PATH`, `data_integrity.INGEST_LOG_PATH`, `review/backtest.py` output, `scripts/wait_and_ingest.py` log dir) now route through `scripts._common.get_data_root()`, which reads `cfg["data_root"]` from per-PC config.json. Default behaviour (no override) still resolves to `SKILL_DIR/data` — preserves single-PC simplicity.

**Setup steps reflect this**: Phase 1 now has steps 5 (install Resilio + connect) and 6 (configure data_root in config.json). Step 3 (Dropbox) now explicitly excludes `data/` from Selective Sync.

### 2026-05-24 — Initial Hermes setup guide
Created as part of the DITP P2 backtest infrastructure build. Goal: spin up an autonomous worker VM so the laptop is freed from multi-day ingest jobs. Server 2019 Standard (Desktop Experience), 16 GB RAM, 4 vCPU, 200 GB VHDX, external network switch. IB Gateway managed by IBC. ClientId 84 for ingest (vs laptop's 83 for live + 80 for observer + 99 for dashboard probes).
