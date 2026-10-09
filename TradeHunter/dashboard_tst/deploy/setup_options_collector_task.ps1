# setup_options_collector_task.ps1 - register the Hermes options collector
# (deploy\options_collector.py --forever) as a Windows Scheduled Task on Hermes.
#
# The collector is ALWAYS ON (OPTIONS_V2_DESIGN.md section 4): first-time history for
# new basket tickers, chain cycles in US market hours, one end-of-day pass after
# 16:15 ET. It connects to IB Gateway on 127.0.0.1 (TST_IBKR_PORT, else 4002, 4001,
# 7497, 7496) as clientId 89 and keeps retrying while the Gateway is down.
#
# Triggers: at startup (1 min delay) and daily 07:00 local (Malaysia) - the daily one
# only revives a collector that died (MultipleInstances IgnoreNew). Restart on
# failure 3 times, 5 min apart. No execution time limit.
#
# Run ONCE on Hermes, from an elevated PowerShell:
#   cd C:\trading-skills\TradeHunter\dashboard_tst
#   powershell -ExecutionPolicy Bypass -File deploy\setup_options_collector_task.ps1 -StartNow
#
# Re-run with -StartNow after a git pull that changes the collector: it stops the
# running copy (and any orphaned python child) and starts the new code. Re-running
# also replaces an older registration (a cmd.exe wrapper appending stdout to the log).
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

if (-not (Test-Path $Python)) {
    Write-Error "venv python not found at $Python. Run deploy\run_app.ps1 first to build the venv."
}
if (-not (Test-Path $Script)) {
    Write-Error "collector script not found at $Script"
}
if (-not (Test-Path $LogDir)) { $null = New-Item -ItemType Directory -Path $LogDir }
$LogFile = Join-Path $LogDir "options_collector.log"

# python runs directly (no cmd.exe wrapper, no shell redirect): the collector writes
# and rotates its own log file.
$CollectorArgs = "`"$Script`" --forever --log-file `"$LogFile`""
$action = New-ScheduledTaskAction -Execute $Python -Argument $CollectorArgs -WorkingDirectory $DashRoot

$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = "PT1M"          # let the network and IB Gateway come up first
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
    # Stop a running copy first: IgnoreNew would otherwise keep the old code, and two
    # collectors would collide on clientId 89. Ending an older registration's task can
    # leave the python child running (its cmd.exe wrapper dies, the child does not) -
    # kill any collector process too.
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
Write-Host "Remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
