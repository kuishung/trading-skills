# check_rename_hermes.ps1 - read-only post-rename health check for Hermes.
#
# Context: the bot folder was renamed intraday-bot -> TradeHunter. Hermes
# has NO Dropbox - it receives the rename via `git pull` (data + creds come
# over Resilio). Two things break:
#
#   1. git pull renames only TRACKED files. Your per-PC GITIGNORED files
#      (config.json, .env, ibc/credentials.txt, state/*.flag, the MCP's
#      node_modules) are left behind in the old intraday-bot/ folder. The
#      big one is config.json: without it in TradeHunter/, get_data_root()
#      falls back to SKILL_DIR\data and seeding writes INSIDE the bot folder
#      instead of C:\HermesSync\MarketData.
#   2. Scheduled tasks / IBC launchers baked in the OLD absolute path at
#      registration time, so they still point at intraday-bot.
#
# Parquet DATA itself is safe - it lives at cfg.data_root outside the folder.
#
# This script ONLY reads + reports. It makes no changes. Run it on Hermes
# AFTER `git pull`:
#
#     cd C:\<your-clone>\trading-skills\TradeHunter
#     powershell -ExecutionPolicy Bypass -File .\scripts\check_rename_hermes.ps1
#
# ASCII-only on purpose (PS 5.1 reads .ps1 as Windows-1252 without a UTF-8
# BOM; an em-dash byte can terminate a string mid-file - see Watch-Ingest.ps1).

[CmdletBinding()]
param(
    [string] $BotRoot = ""
)

$ErrorActionPreference = 'Continue'

# --- locate the bot root (this script lives in <BotRoot>\scripts\) ----------
if ([string]::IsNullOrWhiteSpace($BotRoot)) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $BotRoot   = Split-Path -Parent $ScriptDir
}
$BotRoot = (Resolve-Path -LiteralPath $BotRoot -ErrorAction SilentlyContinue).Path
if ([string]::IsNullOrWhiteSpace($BotRoot)) {
    Write-Host "FATAL: could not resolve BotRoot. Pass -BotRoot 'C:\path\to\TradeHunter'." -ForegroundColor Red
    exit 2
}

# derive the presumed OLD path by swapping the leaf name
$BotParent = Split-Path -Parent $BotRoot
$BotLeaf   = Split-Path -Leaf   $BotRoot
$OldPath   = Join-Path $BotParent 'intraday-bot'

# --- result accounting ------------------------------------------------------
$script:nPass = 0; $script:nWarn = 0; $script:nFail = 0
function Note($level, $msg) {
    switch ($level) {
        'PASS' { $c = 'Green';  $script:nPass++ }
        'WARN' { $c = 'Yellow'; $script:nWarn++ }
        'FAIL' { $c = 'Red';    $script:nFail++ }
        default { $c = 'Gray' }
    }
    Write-Host ("  [{0}] {1}" -f $level, $msg) -ForegroundColor $c
}
function Section($t) { Write-Host ""; Write-Host ("=== {0} ===" -f $t) -ForegroundColor Cyan }

Write-Host ""
Write-Host "Post-rename health check (read-only)" -ForegroundColor White
Write-Host ("  bot root (new) : {0}" -f $BotRoot)
Write-Host ("  presumed old   : {0}" -f $OldPath)

# --- 1. new-folder integrity ------------------------------------------------
Section "1. New TradeHunter folder integrity"
if ($BotLeaf -ne 'TradeHunter') {
    Note 'WARN' ("bot root leaf is '{0}', expected 'TradeHunter'. Running from an unexpected location?" -f $BotLeaf)
} else {
    Note 'PASS' "running from a folder named TradeHunter"
}
$keyFiles = @(
    'resources\ibkr_history.py',
    'scripts\wait_and_ingest.py',
    'scripts\Watch-Ingest.ps1',
    'scripts\keep_gateway_alive.ps1',
    'ibc\StartIBC-intraday.bat',
    'config.json'
)
foreach ($rel in $keyFiles) {
    $full = Join-Path $BotRoot $rel
    if (Test-Path -LiteralPath $full) { Note 'PASS' ("present: {0}" -f $rel) }
    else                              { Note 'FAIL' ("MISSING: {0}" -f $rel) }
}

# --- 2. old folder + stranded gitignored per-PC files -----------------------
Section "2. Old intraday-bot folder + stranded per-PC files"
# git pull renames tracked files but leaves untracked/gitignored files in the
# old folder. These per-PC files must be moved by hand into TradeHunter/.
$perPc = @('config.json', '.env', 'ibc\credentials.txt')
if (Test-Path -LiteralPath $OldPath) {
    Note 'WARN' ("old folder still exists: {0}" -f $OldPath)
    foreach ($rel in $perPc) {
        $old = Join-Path $OldPath $rel
        $new = Join-Path $BotRoot $rel
        $inOld = Test-Path -LiteralPath $old
        $inNew = Test-Path -LiteralPath $new
        if ($inOld -and -not $inNew) {
            Note 'FAIL' ("STRANDED: {0} is in the OLD folder but NOT the new one. Move it: Move-Item '{1}' '{2}'" -f $rel, $old, $new)
        } elseif ($inOld -and $inNew) {
            Note 'WARN' ("{0} exists in BOTH folders - confirm the new one is the live copy, then delete the old." -f $rel)
        }
    }
    # state flags + MCP node_modules are per-PC too (lower stakes)
    if (Test-Path -LiteralPath (Join-Path $OldPath 'state')) {
        Note 'INFO' "old\state\ still present (per-PC flags/logs) - harmless; regenerated on next run if not moved."
    }
    if (Test-Path -LiteralPath (Join-Path $OldPath 'resources\tradingview-mcp\node_modules')) {
        Note 'INFO' "old MCP node_modules present - re-run 'npm install' in the new folder rather than moving it."
    }
    Note 'WARN' "  -> Once per-PC files are moved and nothing below points at the old path, the old folder is safe to delete."
} else {
    Note 'PASS' "no leftover intraday-bot folder at the sibling path (per-PC files presumably already moved)"
}

# --- 3. config.json: data_root / vault_dir / ibkr paths ---------------------
Section "3. config.json paths"
$cfgPath = Join-Path $BotRoot 'config.json'
$cfg = $null
if (Test-Path -LiteralPath $cfgPath) {
    try { $cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json }
    catch { Note 'FAIL' ("config.json exists but failed to parse: {0}" -f $_.Exception.Message) }
}
if ($null -eq $cfg) {
    Note 'FAIL' "config.json missing or unreadable - data_root cannot be confirmed"
} else {
    # data_root: the parquet location. THE important one for seeding.
    if ([string]::IsNullOrWhiteSpace($cfg.data_root)) {
        Note 'WARN' "data_root is EMPTY -> code falls back to TradeHunter\data (inside the renamed folder)."
        Note 'WARN' "  -> If Hermes was meant to use C:\HermesSync\MarketData (Resilio), set data_root, or seeding/journal will write INSIDE the bot folder and your existing parquets are orphaned."
    } else {
        Note 'PASS' ("data_root set: {0}" -f $cfg.data_root)
        if (Test-Path -LiteralPath $cfg.data_root) {
            Note 'PASS' "  -> data_root path exists on disk"
            $ph = Join-Path $cfg.data_root 'price_history'
            if (Test-Path -LiteralPath $ph) {
                $pq = @(Get-ChildItem -LiteralPath $ph -Recurse -Filter *.parquet -ErrorAction SilentlyContinue)
                Note 'PASS' ("  -> price_history reachable: {0} parquet file(s)" -f $pq.Count)
            } else {
                Note 'WARN' "  -> price_history\ not found under data_root yet (fine on a fresh seed; suspicious otherwise)"
            }
        } else {
            Note 'FAIL' "  -> data_root path does NOT exist (Resilio not mounted / wrong drive letter?)"
        }
        if ($cfg.data_root -like '*intraday-bot*') {
            Note 'FAIL' "  -> data_root still contains 'intraday-bot' - repoint it"
        }
    }
    # vault_dir (credentials)
    if (-not [string]::IsNullOrWhiteSpace($cfg.vault_dir)) {
        if (Test-Path -LiteralPath $cfg.vault_dir) { Note 'PASS' ("vault_dir exists: {0}" -f $cfg.vault_dir) }
        else { Note 'WARN' ("vault_dir set but missing: {0}" -f $cfg.vault_dir) }
    }
    # any config value that still references the old folder name
    $hit = ($cfg.PSObject.Properties | Where-Object {
        ($_.Value -is [string]) -and ($_.Value -like '*intraday-bot*')
    })
    foreach ($h in $hit) {
        Note 'FAIL' ("config key '{0}' still references intraday-bot: {1}" -f $h.Name, $h.Value)
    }
    if ($null -eq $hit) { Note 'PASS' "no config value references the old intraday-bot path" }
}

# --- 4. scheduled tasks -----------------------------------------------------
Section "4. Scheduled tasks (Task Scheduler)"
$knownTasks = @('IntradayBot-Gateway','IntradayBot-Watcher','IntradayBot-Tray')
foreach ($tn in $knownTasks) {
    $t = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
    if ($null -eq $t) {
        Note 'WARN' ("task not registered: {0} (expected on Hermes if you set it up)" -f $tn)
        continue
    }
    Note 'INFO' ("task {0} - State: {1}" -f $tn, $t.State)
    $bad = $false
    foreach ($a in $t.Actions) {
        $line = ("{0} {1} (cwd: {2})" -f $a.Execute, $a.Arguments, $a.WorkingDirectory)
        if ($line -like '*intraday-bot*') {
            Note 'FAIL' ("  action points at OLD path: {0}" -f $line.Trim())
            $bad = $true
        }
        if ($line -like '*\dashboard\*') {
            Note 'FAIL' ("  action points at OLD 'dashboard\' (now dashboard_intraday\): {0}" -f $line.Trim())
            $bad = $true
        }
    }
    if (-not $bad) { Note 'PASS' ("  {0} actions do not reference the old path" -f $tn) }
}

# sweep EVERY scheduled task for the old folder name (catches anything ad-hoc)
Section "4b. Full Task Scheduler sweep for 'intraday-bot'"
$allHits = @()
foreach ($t in (Get-ScheduledTask -ErrorAction SilentlyContinue)) {
    foreach ($a in $t.Actions) {
        $line = ("{0} {1} {2}" -f $a.Execute, $a.Arguments, $a.WorkingDirectory)
        if ($line -like '*intraday-bot*') {
            $allHits += ("{0}{1} -> {2}" -f $t.TaskPath, $t.TaskName, $line.Trim())
        }
    }
}
if ($allHits.Count -eq 0) { Note 'PASS' "no scheduled task anywhere references intraday-bot" }
else { foreach ($h in $allHits) { Note 'FAIL' $h } }

# --- 5. IBC launcher + config.ini for hardcoded old paths -------------------
Section "5. IBC launcher / config paths"
$ibcFiles = @('ibc\StartIBC-intraday.bat','ibc\config.ini','ibc\StartGateway.bat','ibc\StartTWS.bat')
$ibcHit = $false
foreach ($rel in $ibcFiles) {
    $full = Join-Path $BotRoot $rel
    if (-not (Test-Path -LiteralPath $full)) { continue }
    $m = Select-String -LiteralPath $full -Pattern 'intraday-bot' -SimpleMatch -ErrorAction SilentlyContinue
    if ($null -ne $m) {
        $ibcHit = $true
        foreach ($line in $m) { Note 'FAIL' ("{0}:{1} -> {2}" -f $rel, $line.LineNumber, $line.Line.Trim()) }
    }
}
if (-not $ibcHit) { Note 'PASS' "no IBC file hardcodes the old intraday-bot path" }
Note 'INFO' "Note: IBC files may still hardcode TWS/Gateway install paths or a secrets path - verify those exist separately."
if (-not [string]::IsNullOrWhiteSpace($cfg.ibkr_secrets_path)) {
    if (Test-Path -LiteralPath $cfg.ibkr_secrets_path) { Note 'PASS' ("ibkr_secrets_path exists: {0}" -f $cfg.ibkr_secrets_path) }
    else { Note 'WARN' ("ibkr_secrets_path set but missing: {0}" -f $cfg.ibkr_secrets_path) }
}

# --- 6. Python 3.12 + ib_insync probe (hard rule for IBKR workloads) --------
Section "6. Python 3.12 / ib_insync (IBKR hard rule)"
$py312 = $null
try { $py312 = & py -3.12 -c "import sys; print(sys.version.split()[0])" 2>$null } catch {}
if ([string]::IsNullOrWhiteSpace($py312)) {
    Note 'FAIL' "py -3.12 not available - IBKR seeding/ingest will fail (eventkit needs 3.12, not 3.14). Install Python 3.12."
} else {
    Note 'PASS' ("py -3.12 present: {0}" -f $py312)
    $probe = & py -3.12 -c "import ib_insync; print(ib_insync.__version__)" 2>$null
    if ([string]::IsNullOrWhiteSpace($probe)) {
        Note 'WARN' "py -3.12 cannot import ib_insync - run: py -3.12 -m pip install ib_insync"
    } else {
        Note 'PASS' ("ib_insync importable under 3.12: v{0}" -f $probe)
    }
}

# --- summary ----------------------------------------------------------------
Section "Summary"
Write-Host ("  PASS: {0}   WARN: {1}   FAIL: {2}" -f $script:nPass, $script:nWarn, $script:nFail) -ForegroundColor White
if ($script:nFail -gt 0) {
    Write-Host "  -> Action required: re-register the affected scheduled tasks from the new folder," -ForegroundColor Red
    Write-Host "     and/or fix the config/IBC paths flagged above. Re-run the matching setup_hermes_*.ps1" -ForegroundColor Red
    Write-Host "     from inside TradeHunter to rewrite each task's baked-in path." -ForegroundColor Red
    exit 1
} elseif ($script:nWarn -gt 0) {
    Write-Host "  -> Review the warnings above; seeding will likely run but confirm data_root + sync state." -ForegroundColor Yellow
    exit 0
} else {
    Write-Host "  -> All clear. The rename did not break Hermes seeding." -ForegroundColor Green
    exit 0
}
