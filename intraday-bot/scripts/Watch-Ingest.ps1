# Watch-Ingest.ps1 — supervisor that keeps wait_and_ingest.py alive forever.
#
# If the Python process exits for ANY reason (clean finish, crash, kill,
# OOM, IBKR auth pop-up that closes Gateway), this loop waits RestartDelay
# seconds and relaunches it. Stops cleanly on Ctrl+C (or when the PS host
# window closes).
#
# Per user 2026-05-26: *"i prefer to run it without interruption"*. The
# Python script itself handles in-process resilience (socket reconnect,
# per-symbol error skip — see resources/ibkr_history.py); this supervisor
# handles process-level resilience.
#
# Typical use (Hermes, after Gateway is up):
#     cd C:\ClaudeSkills\trading-skills\intraday-bot
#     powershell -ExecutionPolicy Bypass -File .\scripts\Watch-Ingest.ps1
#
# Or as a Task Scheduler "Run whether user is logged on or not" task for
# fully autonomous operation across Hermes reboots.
#
# Parameters mirror wait_and_ingest.py — defaults match the 180d re-seed
# we've been running.

param(
    [string] $Timeframes   = "3min",
    [int]    $SeedDays     = 180,
    [switch] $ForceSeed    = $true,
    [string] $Universe     = "daily",
    [int]    $RestartDelay = 30,
    [int]    $MaxIterations = 0   # 0 = forever
)

# Resolve paths relative to the script — works from any CWD
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotRoot   = Split-Path -Parent $ScriptDir
$CfgPath   = Join-Path $BotRoot "config.json"

# Pull data_root from config.json so the supervisor log lands in the same
# tree as the watcher's own log
$DataRoot = $BotRoot   # fallback
if (Test-Path $CfgPath) {
    try {
        $cfg = Get-Content $CfgPath -Raw | ConvertFrom-Json
        if ($cfg.data_root) { $DataRoot = $cfg.data_root }
    } catch {
        Write-Warning "could not parse config.json — using bot root for log"
    }
}

$LogFile = Join-Path $DataRoot ("_supervisor_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log($msg) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line  = "[$stamp] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# Build python argv
$pyArgs = @(
    "-3.12",
    (Join-Path $BotRoot "scripts\wait_and_ingest.py"),
    "--timeframes", $Timeframes,
    "--seed-days",  $SeedDays,
    "--universe",   $Universe
)
if ($ForceSeed) { $pyArgs += "--force-seed" }

Write-Log "supervisor starting"
Write-Log "  bot_root  : $BotRoot"
Write-Log "  data_root : $DataRoot"
Write-Log "  py_args   : $($pyArgs -join ' ')"
Write-Log "  log_file  : $LogFile"
Write-Log "  restart   : ${RestartDelay}s after each exit"
Write-Log "press Ctrl+C in this window to stop the supervisor"

$iter = 0
while ($true) {
    $iter++
    if ($MaxIterations -gt 0 -and $iter -gt $MaxIterations) {
        Write-Log "reached MaxIterations=$MaxIterations — exiting supervisor"
        break
    }

    Write-Log "iteration #$iter — launching watcher"
    $startTime = Get-Date

    # Foreground call so $LASTEXITCODE is populated when it exits
    & py @pyArgs
    $exitCode = $LASTEXITCODE

    $elapsed = (Get-Date) - $startTime
    $mins = $elapsed.TotalMinutes.ToString("F1")
    Write-Log "watcher exited (code=$exitCode) after ${mins}m"

    if ($exitCode -eq 0) {
        Write-Log "clean exit — full universe completed. Sleeping ${RestartDelay}s before next pass (incremental update)..."
    } else {
        Write-Log "non-zero exit — likely crash/disconnect. Restarting in ${RestartDelay}s"
    }

    Start-Sleep -Seconds $RestartDelay
}

Write-Log "supervisor stopping"
