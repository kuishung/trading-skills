# setup_hermes_gateway_task.ps1 - register the IntradayBot-Gateway scheduled
# task that keeps IB Gateway alive on Hermes. Pairs with the existing
# IntradayBot-Watcher task and scripts/keep_gateway_alive.ps1.
#
# Why this exists: IBC supervises Gateway dialogs but does NOT restart
# Gateway after a crash. Without external supervision, today's failure
# modes (IBKR daily-reset interaction, Gateway OOM, manual quit, OS
# update interruption) leave the watcher in a tight crash-loop on
# failed connect. This task closes that gap.
#
# Recovery chain after both tasks are registered:
#   watcher crash         -> Watch-Ingest.ps1 restarts (30s)
#   watcher supervisor    -> IntradayBot-Watcher task restarts (60s, 3x)
#   Gateway crash         -> IntradayBot-Gateway task relaunches IBC (<=5 min)
#   IBC crash             -> IntradayBot-Gateway task relaunches it
#   Hermes reboot         -> AtStartup triggers both tasks
#
# This script:
#   1. Builds the task definition (action, two triggers, settings, principal)
#   2. Registers or updates with -Force
#   3. Verifies the task is in Windows's task store
#   4. Optionally fires it once with -StartNow
#
# Idempotent: safe to re-run; overwrites the existing definition.
#
# Typical use on Hermes (one-shot):
#     cd C:\ClaudeSkills\trading-skills\intraday-bot
#     powershell -ExecutionPolicy Bypass -File .\scripts\setup_hermes_gateway_task.ps1 -StartNow
#
# ASCII-only per the PS 5.1 em-dash lesson (Watch-Ingest.ps1's history).
# No null-coalescing (??) -- PS 5.1 doesn't have it.

param(
    [string] $TaskName        = 'IntradayBot-Gateway',
    [string] $UserId          = 'Administrator',
    [int]    $IntervalMinutes = 5,
    [switch] $StartNow        = $false
)

# --- Resolve paths relative to this script ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotRoot   = Split-Path -Parent $ScriptDir
$ScriptPs1 = Join-Path $ScriptDir "keep_gateway_alive.ps1"

if (-not (Test-Path $ScriptPs1)) {
    Write-Error "keep_gateway_alive.ps1 not found at $ScriptPs1 -- run this from inside an intraday-bot checkout."
    exit 1
}

# --- Build the task ---
$arguments = "-NonInteractive -ExecutionPolicy Bypass -File `"$ScriptPs1`""

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $arguments `
    -WorkingDirectory $BotRoot

# Two triggers:
#   1. AtStartup    -- bring Gateway up immediately on Hermes boot
#   2. Once + repeat every $IntervalMinutes minutes -- crash-recovery within 5 min
#
# PS 5.1 quirk: RepetitionDuration must be a real TimeSpan (not infinite). Use
# 9999 days (~27 years) which is effectively forever for our purposes.
$tStartup = New-ScheduledTaskTrigger -AtStartup
$tRepeat  = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
# Each check is fast (port-probe + maybe a Start-Process). 5-min ceiling prevents
# a stuck check from holding the slot when the next interval fires.

$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId -LogonType S4U -RunLevel Highest

# --- Register (or update existing) ---
try {
    $null = Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger @($tStartup, $tRepeat) `
        -Settings $settings -Principal $principal `
        -Description "Gateway keep-alive: ensures IB Gateway is running on Hermes. Triggers on boot AND every $IntervalMinutes minutes. Idempotent (no-op if Gateway is up or starting). Pairs with IntradayBot-Watcher for the full Gateway+watcher recovery chain." `
        -Force
} catch {
    Write-Error "Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Error "Common causes: not running PowerShell as Administrator; UserId '$UserId' does not exist; LogonType S4U requires the user to have logged in at least once."
    exit 1
}

# --- Verify ---
$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $verify) {
    Write-Error "Task '$TaskName' did not register."
    exit 1
}

Write-Host ""
Write-Host "Task registered: $TaskName  (State: $($verify.State))" -ForegroundColor Green
Write-Host "  Triggers     : AtStartup + every $IntervalMinutes minutes"
Write-Host "  Action       : powershell.exe -File $ScriptPs1"
Write-Host "  Working dir  : $BotRoot"
Write-Host "  Principal    : $UserId (S4U, RunLevel Highest)"
Write-Host ""

# --- Optionally start now ---
if ($StartNow) {
    try {
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 3
        $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
        Write-Host "Task started. LastTaskResult: $($info.LastTaskResult) (expect 0 = success)" -ForegroundColor Green
        if ($info.LastTaskResult -eq 267009) {
            Write-Host "  (267009 = SCHED_S_TASK_RUNNING -- the check is still in progress; should finish in a few seconds.)"
        }
    } catch {
        Write-Error "Start-ScheduledTask failed: $($_.Exception.Message)"
        exit 1
    }
} else {
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "  - Task auto-fires at Hermes boot AND every $IntervalMinutes minutes."
    Write-Host "  - To launch now:  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  - To inspect:     Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
    Write-Host "  - To tail log:    Get-Content (Join-Path (cfg.data_root) '_gateway_keepalive.log') -Tail 20"
    Write-Host "  - To remove:      Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
}
