# setup_screener_task.ps1 - register the Hermes Options Screener collector
# (deploy\screener_collector.py --forever) as a Windows Scheduled Task on Hermes.
#
# The collector is ALWAYS ON (OPTIONS_SCREENER_DESIGN.md section 4): the universe of every
# optionable US underlying daily from 07:30 ET, a market pass over the whole universe every
# 30 min (TST_SCREENER_CYCLE_MIN) from 09:45 to 16:00 ET, one end-of-day pass after
# 16:20 ET, 2 years of grouped daily stock bars + the technicals, and the IV history in the
# gaps. Its data source is Massive (formerly Polygon.io) over HTTPS - Options Starter +
# Stocks Basic - with the same key as the options collector: TST_MASSIVE_API_KEY in
# app\.env. Without it the collector runs, reports "TST_MASSIVE_API_KEY is not set on this
# PC" (the Options page + the tray) and looks for the key again every 5 min. This script
# only checks that the line is there - it never prints the key.
#
# It runs BESIDE the basket collector (TST-Options-Collector, unchanged): two processes,
# one key. It writes its own database (screener.db, TST_SCREENER_DATABASE_URL) and its
# heartbeat state\screener_collector.json (the Hermes tray reads it).
#
# Triggers: at startup (1 min delay) and daily 07:00 local (Malaysia) - the daily one
# only revives a collector that died (MultipleInstances IgnoreNew). Restart on failure 3
# times, 5 min apart. No execution time limit.
#
# Run ONCE on Hermes, from an elevated PowerShell (after pip install -r app\requirements.txt):
#   cd C:\trading-skills\TradeHunter\dashboard_tst
#   powershell -ExecutionPolicy Bypass -File deploy\setup_screener_task.ps1 -StartNow
#
# Re-run with -StartNow after a git pull that changes the collector
# (app\services\scr_collector.py, scr_store.py, massive.py, opt_massive.py,
# app\screener_models.py, alembic_screener\, deploy\screener_collector.py): it stops the
# running copy (and any orphaned python child) and starts the new code. Re-running also
# replaces an older registration of the same task.
# The log is logs\screener_collector.log, written by the collector itself (--log-file):
# rotated at 5 MB, 5 old files kept (screener_collector.log.1 ... .5), so it never grows
# past ~30 MB. PS 5.1 compatible, ASCII only.

[CmdletBinding()]
param(
    [string] $TaskName = "TST-Options-Screener",
    [string] $At       = "07:00",
    [string] $User     = "Administrator",
    [switch] $StartNow
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir
$Python = Join-Path $DashRoot ".venv\Scripts\python.exe"
$Script = Join-Path $DeployDir "screener_collector.py"
$LogDir = Join-Path $DashRoot "logs"
$EnvFile = Join-Path $DashRoot "app\.env"

if (-not (Test-Path $Python)) {
    Write-Error "venv python not found at $Python. Run deploy\run_app.ps1 first to build the venv."
}
if (-not (Test-Path $Script)) {
    Write-Error "collector script not found at $Script"
}
if (-not (Test-Path $LogDir)) { $null = New-Item -ItemType Directory -Path $LogDir }
$LogFile = Join-Path $LogDir "screener_collector.log"

# The key line must be in app\.env. -Quiet returns only True / False: the key itself is
# never read into this script or shown.
$HasKey = $false
if (Test-Path $EnvFile) {
    $HasKey = [bool](Select-String -Path $EnvFile -Pattern '^\s*TST_MASSIVE_API_KEY\s*=\s*\S' -Quiet)
}
if (-not $HasKey) {
    Write-Warning "TST_MASSIVE_API_KEY is not set in $EnvFile. The screener collector will run and report the missing key until you add the line TST_MASSIVE_API_KEY=<your key> there (it looks again every 5 min)."
}

# python runs directly (no cmd.exe wrapper, no shell redirect): the collector writes
# and rotates its own log file.
$CollectorArgs = "`"$Script`" --forever --log-file `"$LogFile`""
$action = New-ScheduledTaskAction -Execute $Python -Argument $CollectorArgs -WorkingDirectory $DashRoot

$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = "PT1M"          # let the network come up first
$daily = New-ScheduledTaskTrigger -Daily -At $At

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
# ExecutionTimeLimit zero = no limit: the collector runs until the box stops.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atStartup, $daily) `
    -Principal $principal -Settings $settings -Force

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $verify) { Write-Error "Task '$TaskName' did not register."; exit 1 }
Write-Host "Task registered: $TaskName at startup + daily at $At  (State: $($verify.State))" -ForegroundColor Green

if ($StartNow) {
    # Stop a running copy first: IgnoreNew would otherwise keep the old code running.
    # Ending the task can leave the python child running - kill any screener collector
    # process too (the options collector, options_collector.py, is left alone).
    # schtasks writes to stderr when nothing is running; under "Stop" PS 5.1 would turn
    # that into a terminating error, so relax the preference for this one call.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & schtasks.exe /End /TN $TaskName 2>&1 | Out-Null } catch { }
    $ErrorActionPreference = $prevEap
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'screener_collector\.py' }
    foreach ($p in $procs) {
        Write-Host "Stopping running screener collector PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 3
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Started: $TaskName (State: $state)" -ForegroundColor Green
}

Write-Host "Log:     $LogFile  (rotated at 5 MB: .1 ... .5 are the older ones)"
Write-Host "Status:  Get-Content `"$DashRoot\state\screener_collector.json`""
Write-Host "Tail:    Get-Content `"$LogFile`" -Tail 40 -Wait"
Write-Host "Run now: Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
