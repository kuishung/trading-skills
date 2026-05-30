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

if (-not (Test-Path $Venv)) {
    Write-Host "Creating virtual env at $Venv ..." -ForegroundColor Cyan
    # Prefer Python 3.12 (reliable wheels for the web deps); fall back to default.
    if (py -3.12 --version 2>$null) { py -3.12 -m venv $Venv } else { py -m venv $Venv }
}

Write-Host "Installing/updating dependencies ..." -ForegroundColor Cyan
& $Pip install --upgrade pip
& $Pip install -r $ReqFile

$uargs = @("app.main:app", "--host", $BindHost, "--port", "$Port")
if ($Reload) { $uargs += "--reload" }

Write-Host "Launching uvicorn: $BindHost`:$Port (reload=$($Reload.IsPresent))" -ForegroundColor Green
& $Uvicorn @uargs
