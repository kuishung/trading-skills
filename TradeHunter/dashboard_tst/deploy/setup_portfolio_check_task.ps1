# setup_portfolio_check_task.ps1 - register the daily option-spread monitoring
# sweep as a Windows scheduled task on Hermes.
#
# The sweep grades every member's open bull put spreads against their exit lines
# (short-put delta, and percent of max loss) and files one SpreadCheck row per
# spread per trading day. It is what makes the Portfolio page a MONITOR rather
# than a report: without it, positions are only ever checked on days somebody
# happens to open the site.
#
# Default time is 06:00 LOCAL. Hermes sits in Malaysia (UTC+8), so 06:00 MYT is
# 18:00 ET during US EDT and 17:00 ET during EST -- comfortably after the 16:00
# close in both, and still the SAME ET calendar day, so the check files under the
# trading day that just closed rather than the one about to start. That is why
# the time is expressed locally and not as "after the close": one local time is
# correct on both sides of the US DST switch, and a converted one is not.
#
# Usage (on Hermes, elevated PowerShell):
#   powershell -ExecutionPolicy Bypass -File dashboard_tst\deploy\setup_portfolio_check_task.ps1
#   ...                                  ... setup_portfolio_check_task.ps1 -At "05:30"
#
# ASCII-only per the PS 5.1 em-dash lesson.

[CmdletBinding()]
param(
    [string] $TaskName = "TST-Portfolio-Check",
    [string] $At       = "06:00",
    [string] $User     = "Administrator"
)

$ErrorActionPreference = "Stop"

# dashboard_tst/deploy/setup_portfolio_check_task.ps1 -> dashboard_tst/
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir

$Python = Join-Path $DashRoot ".venv\Scripts\python.exe"
$Script = Join-Path $DeployDir "portfolio_daily_check.py"
$LogDir = Join-Path $DashRoot "logs"

if (-not (Test-Path $Python)) {
    Write-Error "venv python not found at $Python. Run deploy\run_app.ps1 first to build the venv."
}
if (-not (Test-Path $Script)) {
    Write-Error "sweep script not found at $Script"
}
if (-not (Test-Path $LogDir)) { $null = New-Item -ItemType Directory -Path $LogDir }

# Redirect through cmd so the run leaves a dated log behind. A scheduled task that
# only reports an exit code is impossible to debug three weeks later, and "did the
# monitor run?" is exactly the question that gets asked three weeks later.
$LogFile = Join-Path $LogDir "portfolio_check.log"
$Cmd = "`"$Python`" `"$Script`" >> `"$LogFile`" 2>&1"

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c $Cmd" -WorkingDirectory $DashRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

try {
    $null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force
} catch {
    Write-Error "Register-ScheduledTask failed: $($_.Exception.Message)"
}

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($verify) {
    Write-Host "Registered '$TaskName' - runs daily at $At" -ForegroundColor Green
    Write-Host "  python : $Python"
    Write-Host "  script : $Script"
    Write-Host "  log    : $LogFile"
    Write-Host ""
    Write-Host "Run it once now to confirm:" -ForegroundColor Cyan
    Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  Get-Content '$LogFile' -Tail 30"
    Write-Host ""
    Write-Host "Remove with:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor Yellow
} else {
    Write-Error "Task '$TaskName' was not found after registration."
}
