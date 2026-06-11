# Manual ad-hoc ingest on HERMES, OUTSIDE the nightly run window.
#
# Use this when you are NOT trading and want to bring the IB Gateway up to
# top up the parquet store during the daytime (the weekday "blackout" when the
# supervisor normally keeps the Gateway OFF to protect your manual trading).
#
# It does the whole cycle safely:
#   1. ARMS the supervisor manual-override (so the supervisor won't kill the
#      Gateway you're about to start). The override auto-expires (-Hours, default 4).
#   2. Starts the IB Gateway via IBC (telnet-free path).
#   3. Waits for the API port 4002 to come up.
#   4. Runs the universe ingest (top-up) in the foreground so you see progress.
#   5. DISARMS the override -- the running supervisor then shuts the Gateway down
#      at its next blackout tick (no manual kill needed).
#
# Run on HERMES (PowerShell):   .\scripts\manual_ingest.ps1
# Keep the Gateway UP after ingest (for more manual work):  .\scripts\manual_ingest.ps1 -KeepUp
# Longer override window:        .\scripts\manual_ingest.ps1 -Hours 6
#
# NOTE: this runs in the foreground over your RDP session -- if RDP disconnects,
# the ingest dies. For a long unattended seed, prefer the nightly supervisor or
# a Task Scheduler job instead.

param(
    [double]$Hours = 4,
    [switch]$KeepUp
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # TradeHunter root (parent of scripts\)
Set-Location $root

Write-Host "== 1/4  Arming supervisor manual override ($Hours h) =="
& py -3.12 "scripts\ingest_supervisor.py" --override on --hours $Hours

Write-Host "== 2/4  Starting IB Gateway (IBC) =="
$bat = Join-Path $root "ibc\StartIBC-intraday.bat"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$bat`"" -WorkingDirectory (Join-Path $root "ibc")

Write-Host "== 3/4  Waiting for Gateway API port 4002 (up to ~150s) =="
$ok = $false
for ($i = 0; $i -lt 50; $i++) {
    Start-Sleep -Seconds 3
    $c = Get-NetTCPConnection -LocalPort 4002 -State Listen -ErrorAction SilentlyContinue
    if ($c) { $ok = $true; break }
    Write-Host ("   ...waiting ({0}s)" -f (($i + 1) * 3))
}
if (-not $ok) {
    Write-Host "Gateway port 4002 never came up - check the Gateway window / login." -ForegroundColor Yellow
    Write-Host "Override is still ARMED; once the Gateway is up, just run the ingest:" -ForegroundColor Yellow
    Write-Host "  py -3.12 resources\ibkr_history.py update --universe" -ForegroundColor Yellow
    exit 1
}
Write-Host "Gateway is LIVE on 4002." -ForegroundColor Green

Write-Host "== 4/4  Running universe top-up (daily -> 5min -> 3min, same as the nightly schedule) =="
# Pull the timeframes + symbols file from the SAME constants the supervisor uses
# (single source of truth) so the side-door can never drift from the schedule.
# TOPUP_TIMEFRAMES = "daily:730,5min:180,3min:180" -> daily candles first, then
# 5min, then 3min (timeframe-major), matching ingest_supervisor.run_topup.
$tf = & py -3.12 -c "import sys; sys.path.insert(0,'scripts'); import ingest_supervisor as s; print(s.TOPUP_TIMEFRAMES)"
$symf = & py -3.12 -c "import sys; sys.path.insert(0,'scripts'); import ingest_supervisor as s; print(s.SYMBOLS_FILE)"
if (-not $tf) { $tf = "daily:730,5min:180,3min:180" }
if (-not $symf) { $symf = "resources\universe_full.txt" }
$symPath = Join-Path $root $symf
& py -3.12 "scripts\wait_and_ingest.py" --symbols-file $symPath --timeframes $tf --topup

if ($KeepUp) {
    Write-Host ""
    Write-Host "Ingest done. -KeepUp set: Gateway left UP and override left ARMED (auto-expires in $Hours h)." -ForegroundColor Yellow
    Write-Host "Before you trade, disarm + let the Gateway go down:" -ForegroundColor Yellow
    Write-Host "  py -3.12 scripts\ingest_supervisor.py --override off" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "== Disarming override (supervisor will shut the Gateway at the next blackout tick) =="
    & py -3.12 "scripts\ingest_supervisor.py" --override off
    Write-Host "Done. If the supervisor task is NOT running, shut the Gateway yourself before trading." -ForegroundColor Green
}
