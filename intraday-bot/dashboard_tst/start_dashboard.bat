@echo off
REM Launch the TST (trend & swing) dashboard on port 8001. Idempotent --
REM if it's already running, just opens the browser. Path-portable via %~dp0.
REM
REM Behaviour:
REM   1. cd to the script's own dir (no manual path edits needed)
REM   2. If nothing is listening on port 8001, start the dashboard in a
REM      minimised cmd window (which you can close any time via the Exit
REM      button on the dashboard, or via stop_dashboard.bat).
REM   3. Open http://localhost:8001 in the default browser.
REM
REM Implementation note (2026-05-27): rewritten to use a SINGLE-LINE
REM PowerShell -Command with semicolons and no `^` continuations.
REM The previous multi-line `^`-continued form blew up on this PC with
REM `'M' is not recognized as an internal or external command` because the
REM file had LF-only line endings and cmd.exe's `^` continuation parser
REM does NOT work reliably without CRLF. Single-line avoids both pitfalls.
REM Also: ASCII-only (no em-dashes / Unicode) so the file stays in the
REM "DOS batch file, ASCII text" classification.

setlocal EnableDelayedExpansion
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $listening = $false; try { if (Get-NetTCPConnection -State Listen -LocalPort 8001 -ErrorAction SilentlyContinue) { $listening = $true } } catch {}; if (-not $listening) { Write-Host 'Starting TST dashboard (with restart supervisor)...'; Start-Process -FilePath '%~dp0_supervise_dashboard.bat' -WindowStyle Minimized; Start-Sleep -Seconds 3 } else { Write-Host 'TST dashboard already running.' }; Start-Process 'http://localhost:8001'"

endlocal
