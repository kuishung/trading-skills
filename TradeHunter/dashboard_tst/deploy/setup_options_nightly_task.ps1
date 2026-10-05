# setup_options_nightly_task.ps1 - register the nightly Options job
# (deploy\options_nightly.py) as a Windows Scheduled Task on Hermes.
#
# Runs daily at 07:15 local (Malaysia) = 19:15 ET (EDT) / 18:15 ET (EST): the
# same ET day, after the close, AFTER the 06:00 Portfolio check and the 06:30
# Spread scan (~30 min for ~550 symbols), so two jobs never hit Cboe's CDN at
# once (the 429 was measured at ~24 requests in 10 s).
#
# Run ONCE on Hermes, from an elevated PowerShell:
#   cd C:\trading-skills\TradeHunter\dashboard_tst
#   powershell -ExecutionPolicy Bypass -File deploy\setup_options_nightly_task.ps1
#
# Output goes to logs\options_nightly.log (appended). PS 5.1 compatible, ASCII only.

[CmdletBinding()]
param(
    [string] $TaskName = "TST-Options-Nightly",
    [string] $At       = "07:15",
    [string] $User     = "Administrator"
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir
$Python = Join-Path $DashRoot ".venv\Scripts\python.exe"
$Script = Join-Path $DeployDir "options_nightly.py"
$LogDir = Join-Path $DashRoot "logs"

if (-not (Test-Path $Python)) {
    Write-Error "venv python not found at $Python. Run deploy\run_app.ps1 first to build the venv."
}
if (-not (Test-Path $Script)) {
    Write-Error "nightly script not found at $Script"
}
if (-not (Test-Path $LogDir)) { $null = New-Item -ItemType Directory -Path $LogDir }
$LogFile = Join-Path $LogDir "options_nightly.log"

$Cmd = "`"$Python`" `"$Script`" >> `"$LogFile`" 2>&1"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c $Cmd" -WorkingDirectory $DashRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
# One chain every 1.5 s plus ~1 s of metrics and engines per ticker: a 60-ticker
# basket is ~2.5 min, the union across members is what the job walks. The 30-min
# ceiling is the budget MAX_BASKET = 60 was set by (A4.1); a run that needs more
# is the signal to move the DB to Postgres, not to raise this.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $verify) { Write-Error "Task '$TaskName' did not register."; exit 1 }
Write-Host "Task registered: $TaskName daily at $At  (State: $($verify.State))" -ForegroundColor Green
Write-Host "Log: $LogFile"
Write-Host "Run it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove:      Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
