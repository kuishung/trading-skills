# status_check.ps1 - check whether the dashboard_tst app is up and healthy.
#
# Polls the app's /status endpoint and (if run on the server) the
# TST-Dashboard-Web scheduled task. Run from anywhere that can reach the
# target: on the server itself (localhost), or from the laptop when both
# machines are on the same LAN / the Hamachi VPN.
#
#   # on the server:
#   powershell -ExecutionPolicy Bypass -File deploy\status_check.ps1
#   # from the laptop over Hamachi:
#   powershell -ExecutionPolicy Bypass -File deploy\status_check.ps1 -Target http://25.x.x.x:8000
#
# Exit 0 = healthy, 1 = problem. ASCII-only per the PS 5.1 em-dash lesson.

[CmdletBinding()]
param(
    [string] $Target   = "http://localhost:8000",
    [string] $TaskName = "TST-Dashboard-Web"
)

$ok = $true
Write-Host "Target: $Target" -ForegroundColor Cyan

try {
    $r = Invoke-RestMethod -Uri "$Target/status" -TimeoutSec 5
    Write-Host ("  [UP]   status={0} version={1} auth={2} db_ok={3} uptime={4}s" -f `
        $r.status, $r.version, $r.auth_mode, $r.db_ok, $r.uptime_seconds) -ForegroundColor Green
    if (-not $r.db_ok) {
        Write-Host "  [WARN] db_ok is false (database not reachable)" -ForegroundColor Yellow
        $ok = $false
    }
} catch {
    Write-Host "  [DOWN] /status unreachable: $($_.Exception.Message)" -ForegroundColor Red
    $ok = $false
}

# Scheduled-task state is only meaningful when run ON the server.
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $t) {
    $info = $t | Get-ScheduledTaskInfo
    Write-Host ("  task {0}: State={1} LastResult={2}" -f $TaskName, $t.State, $info.LastTaskResult) -ForegroundColor Gray
}

if ($ok) {
    Write-Host "RESULT: healthy" -ForegroundColor Green
    exit 0
} else {
    Write-Host "RESULT: problem" -ForegroundColor Red
    exit 1
}
