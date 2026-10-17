# setup_option_calc_task.ps1 - register the Hermes Option Calculator lane
# (deploy\option_calc_worker.py --forever) as a Windows Scheduled Task on Hermes.
#
# The lane is ALWAYS ON (OPTION_CALCULATOR_DESIGN.md steps 2-3): once a minute it looks at
# the screener collector's newest finished Massive market pass and recomputes every
# member's saved option-calculator setups (the /calc page) on it, writing the results to
# tst.db. It reads only what the screener collector stored (screener.db) - no API key.
# It runs BESIDE the screener collector (TST-Options-Screener, unchanged): a separate
# process. It writes its status to state\option_calc_worker.json (the Hermes tray and
# Settings -> Data Ingest -> Option Calculator read it).
#
# Triggers: at startup (1 min delay), daily 07:00 local (Malaysia) and every 15 min from
# the moment this script runs - the daily and the 15-min ones only revive a lane that
# died (MultipleInstances IgnoreNew makes them a no-op while it runs), so a dead lane is
# back within 15 min. Restart on failure 3 times, 5 min apart. No execution time limit.
#
# Run ONCE on Hermes, from an elevated PowerShell (after pip install -r app\requirements.txt):
#   cd C:\trading-skills\TradeHunter\dashboard_tst
#   powershell -ExecutionPolicy Bypass -File deploy\setup_option_calc_task.ps1 -StartNow
#
# Re-run with -StartNow after a git pull that changes the lane (app\services\calc_*.py,
# app\services\option_calc.py, deploy\option_calc_worker.py): it stops the running copy
# (and any orphaned python child) and starts the new code. Restart the web app FIRST when
# the pull also brought alembic\ migrations. Re-running also replaces an older
# registration of the same task.
# The log is logs\option_calc_worker.log, written by the lane itself (--log-file):
# rotated at 5 MB, 5 old files kept (option_calc_worker.log.1 ... .5).
# PS 5.1 compatible, ASCII only.

[CmdletBinding()]
param(
    [string] $TaskName = "TST-Option-Calc",
    [string] $At       = "07:00",
    [string] $User     = "Administrator",
    [switch] $StartNow
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir
$Python = Join-Path $DashRoot ".venv\Scripts\python.exe"
$Script = Join-Path $DeployDir "option_calc_worker.py"
$LogDir = Join-Path $DashRoot "logs"

if (-not (Test-Path $Python)) {
    Write-Error "venv python not found at $Python. Run deploy\run_app.ps1 first to build the venv."
}
if (-not (Test-Path $Script)) {
    Write-Error "lane script not found at $Script"
}
if (-not (Test-Path $LogDir)) { $null = New-Item -ItemType Directory -Path $LogDir }
$LogFile = Join-Path $LogDir "option_calc_worker.log"

# python runs directly (no cmd.exe wrapper, no shell redirect): the lane writes and
# rotates its own log file.
$WorkerArgs = "`"$Script`" --forever --log-file `"$LogFile`""
$action = New-ScheduledTaskAction -Execute $Python -Argument $WorkerArgs -WorkingDirectory $DashRoot

$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = "PT1M"          # let the network and the databases come up first
$daily = New-ScheduledTaskTrigger -Daily -At $At
# Every 15 min, for good (9999 days: [TimeSpan]::MaxValue is refused on Server 2016+).
# A run BY HAND must Disable the task first - this trigger restarts it otherwise (the
# lane's lock, state\option_calc_worker.lock, then makes the second copy wait or exit).
$revive = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 9999)

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
# ExecutionTimeLimit zero = no limit: the lane runs until the box stops.
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
    # Ending the task can leave the python child running - kill any option calculator
    # lane process too (the two options collectors are left alone).
    # schtasks writes to stderr when nothing is running; under "Stop" PS 5.1 would turn
    # that into a terminating error, so relax the preference for this one call.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & schtasks.exe /End /TN $TaskName 2>&1 | Out-Null } catch { }
    $ErrorActionPreference = $prevEap
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'option_calc_worker\.py' }
    foreach ($p in $procs) {
        Write-Host "Stopping running option calculator lane PID $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 3
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Started: $TaskName (State: $state)" -ForegroundColor Green
}

Write-Host "Log:     $LogFile  (rotated at 5 MB: .1 ... .5 are the older ones)"
Write-Host "Status:  Get-Content `"$DashRoot\state\option_calc_worker.json`""
Write-Host "Tail:    Get-Content `"$LogFile`" -Tail 40 -Wait"
Write-Host "Run now: Start-ScheduledTask -TaskName $TaskName"
Write-Host "By hand: Disable-ScheduledTask -TaskName $TaskName; schtasks /End /TN $TaskName first (the 15-min trigger restarts it otherwise), then Enable-ScheduledTask -TaskName $TaskName; Start-ScheduledTask -TaskName $TaskName after"
Write-Host "Tray:    a pull that changed dashboard_intraday\tray_status.py needs the IntradayBot-Tray task restarted in the RDP session (DEPLOY.md section I)" -ForegroundColor Yellow
Write-Host "Remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
