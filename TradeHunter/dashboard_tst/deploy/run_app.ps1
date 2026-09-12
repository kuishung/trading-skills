# run_app.ps1 - create/refresh the venv, install deps, launch the
# dashboard_tst collaboration app.
#
# This is a NON-IBKR web app, so it runs on any Python 3.10+ (the py -3.12
# IBKR rule does not apply here). It binds 127.0.0.1 by default because in
# production Caddy terminates TLS on 443 and reverse-proxies to it. For a
# quick local test without Caddy, run with -BindHost 0.0.0.0.
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File dashboard_tst\deploy\run_app.ps1
#   ...                                  ... run_app.ps1 -Reload          (dev)
#   ...                                  ... run_app.ps1 -BindHost 0.0.0.0 -Port 8000
#
# ASCII-only per the PS 5.1 em-dash lesson.

[CmdletBinding()]
param(
    [string] $BindHost = "127.0.0.1",
    [int]    $Port     = 8000,
    [switch] $Reload
)

$ErrorActionPreference = "Stop"

# dashboard_tst/deploy/run_app.ps1 -> dashboard_tst/
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DashRoot  = Split-Path -Parent $DeployDir
Set-Location $DashRoot

$Venv    = Join-Path $DashRoot ".venv"
$Pip     = Join-Path $Venv "Scripts\pip.exe"
$Uvicorn = Join-Path $Venv "Scripts\uvicorn.exe"
$ReqFile = Join-Path $DashRoot "app\requirements.txt"
$EnvFile = Join-Path $DashRoot "app\.env"

if (-not (Test-Path $EnvFile)) {
    Write-Warning "app\.env not found. Copy app\.env.example -> app\.env and fill it in (secret, Google creds, admin email) before going live."
}

$created = $false
if (-not (Test-Path $Venv)) {
    Write-Host "Creating virtual env at $Venv ..." -ForegroundColor Cyan
    # Prefer Python 3.12 (reliable wheels for the web deps); fall back to default.
    if (py -3.12 --version 2>$null) { py -3.12 -m venv $Venv } else { py -m venv $Venv }
    $created = $true
}

# Install deps ONLY when the venv is new or requirements.txt changed. This
# skips the slow reinstall on every boot/restart, so the app comes back in
# seconds (a cold reinstall at startup was taking minutes and delaying uvicorn).
$ReqHashFile = Join-Path $Venv ".reqhash"
$ReqHash = (Get-FileHash -Path $ReqFile -Algorithm SHA256).Hash
$HaveHash = (Test-Path $ReqHashFile) -and ((Get-Content $ReqHashFile -Raw).Trim() -eq $ReqHash)
if ($created -or -not $HaveHash) {
    Write-Host "Installing/updating dependencies ..." -ForegroundColor Cyan
    & $Pip install -r $ReqFile
    Set-Content -Path $ReqHashFile -Value $ReqHash -Encoding ascii
} else {
    Write-Host "Dependencies up to date; skipping install." -ForegroundColor DarkGray
}

# Free the port before binding it (v4.65). On Windows a second uvicorn CAN bind
# a port that an orphaned one still holds (SO_REUSEADDR semantics differ from
# Linux), and connections are then split between the two at random. An orphan
# running old code but reading the new templates from disk answers some
# requests with a bare 500 that never reaches the live process's log - which is
# exactly how the Portfolio board sat on "Checking your positions" after the
# v4.62 deploy. Killing every listener here makes a restart mean a restart.
$stale = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $stale) {
    if ($procId -eq $PID) { continue }
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -ne $p) {
        Write-Host ("Killing stale listener on port {0}: {1} (pid {2}, started {3})" -f $Port, $p.ProcessName, $procId, $p.StartTime) -ForegroundColor Yellow
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}
if ($stale) { Start-Sleep -Seconds 2 }

$uargs = @("app.main:app", "--host", $BindHost, "--port", "$Port")
if ($Reload) { $uargs += "--reload" }

Write-Host "Launching uvicorn: $BindHost`:$Port (reload=$($Reload.IsPresent))" -ForegroundColor Green
& $Uvicorn @uargs
