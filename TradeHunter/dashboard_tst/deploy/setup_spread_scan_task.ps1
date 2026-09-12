# setup_spread_scan_task.ps1 - register the nightly bull-put-spread scan
# (deploy\spread_scan.py) as a Windows Scheduled Task on Hermes.
#
# Runs daily at 06:30 local (Malaysia) = after the US close and after the
# 06:00 Portfolio check, so both jobs read the same closing chains and the
# Spread page is fresh before the user's morning.
#
# Run ONCE on Hermes, from an elevated PowerShell:
#   cd C:\trading-skills\TradeHunter\dashboard_tst
#   powershell -ExecutionPolicy Bypass -File deploy\setup_spread_scan_task.ps1
#
# Output goes to logs\spread_scan.log (appended). PS 5.1 compatible, ASCII only.

[CmdletBinding()]
param(
    [string] $TaskName = "TST-Spread-Scan",
    [string] $At       = "06:30",
    [string] $User     = "Administrator"
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir
$Python = Join-Path $DashRoot ".venv\Scripts\python.exe"
$Script = Join-Path $DeployDir "spread_scan.py"
$LogDir = Join-Path $DashRoot "logs"

if (-not (Test-Path $Python)) {
    Write-Error "venv python not found at $Python. Run deploy\run_app.ps1 first to build the venv."
}
if (-not (Test-Path $Script)) {
    Write-Error "scan script not found at $Script"
}
if (-not (Test-Path $LogDir)) { $null = New-Item -ItemType Directory -Path $LogDir }
$LogFile = Join-Path $LogDir "spread_scan.log"

$Cmd = "`"$Python`" `"$Script`" >> `"$LogFile`" 2>&1"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c $Cmd" -WorkingDirectory $DashRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
# Cboe rate-limits bursts, so the scan runs one chain every 1.5 s: ~550 symbols
# is ~30 minutes (measured 60 symbols in 197 s, 2026-09-12). 90 min ceiling.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90)

$null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $verify) { Write-Error "Task '$TaskName' did not register."; exit 1 }
Write-Host "Task registered: $TaskName daily at $At  (State: $($verify.State))" -ForegroundColor Green
Write-Host "Log: $LogFile"
Write-Host "Run it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove:      Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
