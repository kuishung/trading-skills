# setup_hermes_supervisor_task.ps1 - register Watch-Ingest.ps1 as a Windows
# Scheduled Task so it survives RDP disconnects + Hermes reboots.
#
# Why this exists: launching Watch-Ingest.ps1 inside an interactive RDP
# PowerShell window means the supervisor + watcher die when the RDP session
# disconnects. We learned this the hard way on 2026-05-26 (twice). The fix
# is to register the supervisor as a Scheduled Task that runs under the
# Administrator principal in a session decoupled from any RDP session.
#
# This script:
#   1. Builds the task definition (action, trigger, settings, principal)
#   2. Registers it (or updates an existing one with -Force)
#   3. Verifies the task is in Windows's task store after registration
#   4. Optionally starts it immediately (only safe when no supervisor
#      is already running -- competing watchers collide on IBKR clientId)
#
# Idempotent: safe to re-run; it overwrites the existing task definition.
#
# Typical use on a fresh Hermes (one-shot):
#     cd C:\ClaudeSkills\trading-skills\intraday-bot
#     powershell -ExecutionPolicy Bypass -File .\scripts\setup_hermes_supervisor_task.ps1 -StartNow
#
# After it runs, the supervisor will auto-start on every Hermes boot. If you
# need to launch it without a reboot (and no other supervisor is alive):
#     Start-ScheduledTask -TaskName 'IntradayBot-Watcher'

param(
    [string] $TaskName    = 'IntradayBot-Watcher',
    [string] $Timeframes  = '3min:180,5min:180,daily:730',
    [string] $SymbolsFile = 'resources\universe_full.txt',
    [string] $UserId      = 'Administrator',
    [switch] $StartNow    = $false
)

# ASCII-only file: do NOT add em-dashes or smart quotes. Windows PowerShell
# 5.1 reads .ps1 as Windows-1252 without a UTF-8 BOM, and the em-dash byte
# sequence decodes to a curly close-quote that PS 5.1's tokenizer treats as
# a string delimiter, mid-file. See scripts/Watch-Ingest.ps1's history.

# --- Resolve paths relative to this script ---
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotRoot    = Split-Path -Parent $ScriptDir
$WatcherPs1 = Join-Path $ScriptDir 'Watch-Ingest.ps1'

if (-not (Test-Path $WatcherPs1)) {
    Write-Error "Watch-Ingest.ps1 not found at $WatcherPs1 -- run this script from inside an intaday-bot checkout."
    exit 1
}

# --- Detect existing supervisor (so we don't accidentally spawn a duplicate) ---
$existing = $null
try {
    $existing = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*Watch-Ingest*' }
} catch {
    # CIM might not be available in restricted environments -- fall back to Get-Process
    $existing = Get-Process powershell -ErrorAction SilentlyContinue
}
if ($existing) {
    # PS 5.1 has no null-coalescing -- explicit if/else. CIM rows expose
    # ProcessId; Get-Process rows expose Id.
    $pidsList = ($existing | ForEach-Object {
        if ($null -ne $_.ProcessId) { $_.ProcessId } else { $_.Id }
    }) -join ', '
    Write-Warning "A Watch-Ingest supervisor appears to be running (PID(s): $pidsList)."
    Write-Warning "-StartNow will be ignored if set, to avoid IBKR clientId collisions."
}

# --- Build the task ---
$arguments = "-ExecutionPolicy Bypass -File `"$WatcherPs1`" -Timeframes `"$Timeframes`" -SymbolsFile `"$SymbolsFile`""

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $arguments `
    -WorkingDirectory $BotRoot

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 = no time limit; ingest may run for days

$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId -LogonType S4U -RunLevel Highest

# --- Register (or update existing) ---
try {
    $null = Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description 'Forever-loop supervisor for the IBKR ingest watcher (S&P + MidCap + SmallCap + NDX + DJIA pure-IBKR seed). Auto-starts at Hermes boot; survives RDP disconnects.' `
        -Force
} catch {
    Write-Error "Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Error "Common causes: not running PowerShell as Administrator; UserId '$UserId' does not exist on this machine; LogonType S4U requires the user to have logged in at least once."
    exit 1
}

# --- Verify ---
$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $verify) {
    Write-Error "Task '$TaskName' did not register. Check the previous error or run with -Verbose."
    exit 1
}

Write-Host ""
Write-Host "Task registered: $TaskName  (State: $($verify.State))" -ForegroundColor Green
Write-Host "  Trigger      : AtStartup"
Write-Host "  Working dir  : $BotRoot"
Write-Host "  Watcher args : -Timeframes $Timeframes  -SymbolsFile $SymbolsFile"
Write-Host "  Principal    : $UserId (S4U, RunLevel Highest)"
Write-Host ""

# --- Optionally start now (only safe if no supervisor running) ---
if ($StartNow) {
    if ($existing) {
        Write-Warning "-StartNow ignored: a supervisor is already running. Stop it first if you want the scheduled task to take over."
    } else {
        try {
            Start-ScheduledTask -TaskName $TaskName
            Start-Sleep -Seconds 3
            $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
            Write-Host "Task started. LastTaskResult: $($info.LastTaskResult) (expect 267009 = currently running)" -ForegroundColor Green
        } catch {
            Write-Error "Start-ScheduledTask failed: $($_.Exception.Message)"
            exit 1
        }
    }
} else {
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "  - Task will auto-fire at the next Hermes boot."
    Write-Host "  - To launch the supervisor NOW (only if none is running):"
    Write-Host "      Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  - To inspect status later:"
    Write-Host "      Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
    Write-Host "  - To remove:"
    Write-Host "      Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
}
