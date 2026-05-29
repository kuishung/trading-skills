# setup_hermes_webapp_task.ps1 - register the TST-Dashboard-Web scheduled
# task so the dashboard_tst collaboration app stays up across reboots and
# RDP disconnects (mirrors the setup_hermes_*.ps1 pattern used by the bot).
#
# The task runs deploy\run_app.ps1, which creates/refreshes the venv,
# installs deps, and launches uvicorn. Bound to 0.0.0.0 so it is reachable
# over the Hamachi VPN -- LOCK IT DOWN with a firewall rule that only allows
# the Hamachi subnet (see DEPLOY.md, "Firewall").
#
# Run once on the server (elevated):
#   powershell -ExecutionPolicy Bypass -File dashboard_tst\deploy\setup_hermes_webapp_task.ps1 -StartNow
#
# ASCII-only per the PS 5.1 em-dash lesson.

[CmdletBinding()]
param(
    [string] $TaskName = 'TST-Dashboard-Web',
    [int]    $Port     = 8000,
    [string] $BindHost = '0.0.0.0',
    [switch] $StartNow
)

$ErrorActionPreference = 'Stop'

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $DeployDir 'run_app.ps1'
if (-not (Test-Path $RunScript)) {
    Write-Error "run_app.ps1 not found at $RunScript -- run this from inside the dashboard_tst checkout."
    exit 1
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`" -BindHost $BindHost -Port $Port"

$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'Administrator' -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

try {
    $null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force
} catch {
    Write-Error "Register-ScheduledTask failed: $($_.Exception.Message)"
    exit 1
}

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $verify) { Write-Error "Task '$TaskName' did not register."; exit 1 }
Write-Host "Task registered: $TaskName  (State: $($verify.State))" -ForegroundColor Green

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
    Write-Host "Started. LastTaskResult: $($info.LastTaskResult)" -ForegroundColor Green
    Write-Host "Health check: open http://localhost:$Port/health" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "Reminders:" -ForegroundColor Yellow
Write-Host "  - Lock the firewall to the Hamachi subnet (see DEPLOY.md)."
Write-Host "  - After 'git pull' on the server, restart:  Restart-ScheduledTask -TaskName '$TaskName'"
Write-Host "  - Remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
