@echo off
REM Launch the intraday_bot dashboard. Idempotent — if it's already running,
REM just opens the browser. Path-portable via %~dp0.
REM
REM Behaviour:
REM   1. cd to the script's own dir (no manual path edits needed)
REM   2. If nothing is listening on port 8000, start the dashboard in a
REM      minimised cmd window (which you can close any time via the Exit
REM      button on the dashboard, or via stop_dashboard.bat).
REM   3. Open http://localhost:8000 in the default browser.

setlocal EnableDelayedExpansion
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$listening = $false; try { if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) { $listening = $true } } catch {};" ^
  "if (-not $listening) {" ^
  "  Write-Host 'Starting dashboard (with restart supervisor)...';" ^
  "  Start-Process -FilePath '%~dp0_supervise_dashboard.bat' -WindowStyle Minimized;" ^
  "  Start-Sleep -Seconds 3" ^
  "} else { Write-Host 'Dashboard already running.' };" ^
  "Start-Process 'http://localhost:8000'"

endlocal
