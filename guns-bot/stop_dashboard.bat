@echo off
REM Stop the running guns-bot dashboard. Sends a graceful POST /shutdown
REM first (which lets the server flush its log), then falls back to
REM killing the process that owns port 8000 if it didn't exit.

setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Invoke-WebRequest -Uri http://127.0.0.1:8000/shutdown -Method Post -TimeoutSec 2 ^| Out-Null; Write-Host 'Asked dashboard to shut down.' } catch { Write-Host 'Dashboard did not respond to /shutdown.' };" ^
  "Start-Sleep -Seconds 1;" ^
  "$conn = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue;" ^
  "if ($conn) {" ^
  "  foreach ($c in $conn) { try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop; Write-Host ('Force-stopped PID ' + $c.OwningProcess) } catch { Write-Host ('Could not stop PID ' + $c.OwningProcess + ': ' + $_) } }" ^
  "} else { Write-Host 'Dashboard is not running.' }"

endlocal
