# setup_options_collector_task.ps1 - register the Hermes options collector
# (deploy\options_collector.py --forever) as a Windows Scheduled Task on Hermes.
#
# The collector is ALWAYS ON (OPTIONS_V2_DESIGN.md section 13.4): first-time history for
# new basket tickers, a pass over every basket ticker every 15 min (TST_OPTIONS_CYCLE_MIN)
# in US market hours, one end-of-day pass after 16:20 ET. Its data source is Massive
# (formerly Polygon.io) over HTTPS - Options Starter + Stocks Basic. The key is
# TST_MASSIVE_API_KEY in app\.env; without it the collector runs, reports
# "TST_MASSIVE_API_KEY is not set on this PC" (Options page strip + tray) and looks for
# the key again every 5 min. This script only checks that the line is there - it never
# prints the key.
#
# Triggers: at startup (1 min delay), daily 07:00 local (Malaysia) and every 15 min from
# the moment this script runs - the daily and the 15-min ones only revive a collector
# that died (MultipleInstances IgnoreNew makes them a no-op while it runs), so a dead
# collector is back within 15 min instead of at the next 07:00. Restart on failure 3
# times, 5 min apart. No execution time limit.
#
# Run ONCE on Hermes, from an elevated PowerShell:
#   cd C:\trading-skills\TradeHunter\dashboard_tst
#   powershell -ExecutionPolicy Bypass -File deploy\setup_options_collector_task.ps1 -StartNow
#
# Re-run with -StartNow after a git pull that changes the collector
# (app\services\opt_collector.py, opt_massive.py, massive.py, deploy\options_collector.py):
# it stops the running copy (and any orphaned python child) and starts the new code.
# Re-running also replaces an older registration of the same task.
# The log is logs\options_collector.log, written by the collector itself (--log-file):
# rotated at 5 MB, 5 old files kept (options_collector.log.1 ... .5), so it never
# grows past ~30 MB. PS 5.1 compatible, ASCII only.

[CmdletBinding()]
param(
    [string] $TaskName = "TST-Options-Collector",
    [string] $At       = "07:00",
    [string] $User     = "Administrator",
    [switch] $StartNow
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir
$Python = Join-Path $DashRoot ".venv\Scripts\python.exe"
$Script = Join-Path $DeployDir "options_collector.py"
$LogDir = Join-Path $DashRoot "logs"
$EnvFile = Join-Path $DashRoot "app\.env"

if (-not (Test-Path $Python)) {
    Write-Error "venv python not found at $Python. Run deploy\run_app.ps1 first to build the venv."
}
if (-not (Test-Path $Script)) {
    Write-Error "collector script not found at $Script"
}
if (-not (Test-Path $LogDir)) { $null = New-Item -ItemType Directory -Path $LogDir }
$LogFile = Join-Path $LogDir "options_collector.log"

# The key line must be in app\.env. -Quiet returns only True / False: the key itself is
# never read into this script or shown.
$HasKey = $false
if (Test-Path $EnvFile) {
    $HasKey = [bool](Select-String -Path $EnvFile -Pattern '^\s*TST_MASSIVE_API_KEY\s*=\s*\S' -Quiet)
}
if (-not $HasKey) {
    Write-Warning "TST_MASSIVE_API_KEY is not set in $EnvFile. The collector will run and report the missing key until you add the line TST_MASSIVE_API_KEY=<your key> there (it looks again every 5 min)."
}

# python runs directly (no cmd.exe wrapper, no shell redirect): the collector writes
# and rotates its own log file.
$CollectorArgs = "`"$Script`" --forever --log-file `"$LogFile`""
$action = New-ScheduledTaskAction -Execute $Python -Argument $CollectorArgs -WorkingDirectory $DashRoot

$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = "PT1M"          # let the network come up first
$daily = New-ScheduledTaskTrigger -Daily -At $At
# Every 15 min, for good (9999 days: [TimeSpan]::MaxValue is refused on Server 2016+).
# A run BY HAND must Disable the task first - this trigger restarts it otherwise (the
# collector's lock, state\options_collector.lock, then makes the second copy wait or exit).
$revive = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 9999)

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
# ExecutionTimeLimit zero = no limit: the collector runs until the box stops.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atStartup, $daily, $revive) `
    -Principal $principal -Settings $settings -Force

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $verify) { Write-Error "Task '$TaskName' did not register."; exit 1 }
Write-Host "Task registered: $TaskName at startup + daily at $At + every 15 min if it died  (State: $($verify.State))" -ForegroundColor Green

if ($StartNow) {
    # Stop a running copy first: IgnoreNew would otherwise keep the old code running.
    # Ending an older registration's task can leave the python child running - kill any
    # collector process too.
    # schtasks writes to stderr when nothing is running; under "Stop" PS 5.1 would turn
    # that into a terminating error, so relax the preference for this one call.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & schtasks.exe /End /TN $TaskName 2>&1 | Out-Null } catch { }
    $ErrorActionPreference = $prevEap
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'options_collector\.py' }
    foreach ($p in $procs) {
        Write-Host "Stopping running collector PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 3
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Started: $TaskName (State: $state)" -ForegroundColor Green
}

Write-Host "Log:     $LogFile  (rotated at 5 MB: .1 ... .5 are the older ones)"
Write-Host "Status:  Get-Content `"$DashRoot\state\options_collector.json`""
Write-Host "Tail:    Get-Content `"$LogFile`" -Tail 40 -Wait"
Write-Host "Run now: Start-ScheduledTask -TaskName $TaskName"
Write-Host "By hand: Disable-ScheduledTask -TaskName $TaskName; schtasks /End /TN $TaskName first (the 15-min trigger restarts it otherwise), then Enable-ScheduledTask -TaskName $TaskName; Start-ScheduledTask -TaskName $TaskName after"
Write-Host "Remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
