# setup_hermes_autopull_task.ps1 - register TST-Dashboard-Autopull, a
# scheduled task that runs update.ps1 every few minutes so the server picks
# up pushes automatically (the "push from laptop -> Hermes refreshes" loop).
#
# Polling, NOT a webhook: a private server behind Cloudflare Tunnel isn't
# reachable inbound by GitHub, so we poll `git pull` on an interval.
#
# Run once on the server (elevated):
#   powershell -ExecutionPolicy Bypass -File deploy\setup_hermes_autopull_task.ps1 -StartNow
#
# ASCII-only per the PS 5.1 em-dash lesson.

[CmdletBinding()]
param(
    [string] $TaskName       = 'TST-Dashboard-Autopull',
    [int]    $IntervalMinutes = 5,
    [switch] $StartNow
)

$ErrorActionPreference = 'Stop'

$DeployDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$UpdateScript = Join-Path $DeployDir 'update.ps1'
if (-not (Test-Path $UpdateScript)) {
    Write-Error "update.ps1 not found at $UpdateScript -- run this from inside the dashboard_tst checkout."
    exit 1
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$UpdateScript`""
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments

# Repeat forever at the chosen interval (PS 5.1 "effectively forever" pattern).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

$principal = New-ScheduledTaskPrincipal -UserId 'Administrator' -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

try {
    $null = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force
} catch {
    Write-Error "Register-ScheduledTask failed: $($_.Exception.Message)"
    exit 1
}

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $verify) { Write-Error "Task '$TaskName' did not register."; exit 1 }
Write-Host "Task registered: $TaskName  (every $IntervalMinutes min, State: $($verify.State))" -ForegroundColor Green

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started a first pull now." -ForegroundColor Green
}

Write-Host ""
Write-Host "Remove with:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor Yellow
