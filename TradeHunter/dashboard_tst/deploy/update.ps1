# update.ps1 - pull the latest code and restart the web-app service so the
# site reflects the new commit. The deploy loop's "refresh" step.
#
# Deliberately does NOT rely on uvicorn --reload (unreliable on synced /
# networked drives, as seen during development) -- it restarts the process
# explicitly, which always picks up new code.
#
# Run on the server, manually or via the autopull scheduled task:
#   powershell -ExecutionPolicy Bypass -File deploy\update.ps1
#
# ASCII-only per the PS 5.1 em-dash lesson.

[CmdletBinding()]
param(
    [string] $TaskName = "TST-Dashboard-Web"
)

$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir
$RepoRoot  = git -C $DashRoot rev-parse --show-toplevel
Set-Location $RepoRoot

$before = git rev-parse HEAD
git pull --ff-only
$after = git rev-parse HEAD

if ($before -eq $after) {
    Write-Host "No new commits. Nothing to do." -ForegroundColor Gray
    exit 0
}
Write-Host "Updated $before -> $after" -ForegroundColor Green

# Refresh deps only if requirements changed.
$changed = git diff --name-only $before $after
if ($changed -match "dashboard_tst/app/requirements.txt") {
    Write-Host "requirements.txt changed -> installing deps" -ForegroundColor Cyan
    $pip = Join-Path $DashRoot ".venv\Scripts\pip.exe"
    if (Test-Path $pip) { & $pip install -r (Join-Path $DashRoot "app\requirements.txt") }
}

Write-Host "Restarting $TaskName ..." -ForegroundColor Cyan
Restart-ScheduledTask -TaskName $TaskName
Write-Host "Done. Site updated." -ForegroundColor Green
