@echo off
REM Supervisor loop for the intraday_bot dashboard.
REM
REM Runs server.py (sibling) and re-launches it if it exits with code 100
REM (the Restart signal). Any other exit code is a clean stop (Exit dashboard,
REM Shut down, Ctrl-C). Underscore-prefixed name reflects that users don't
REM invoke this directly — start_dashboard.bat spawns it minimized.

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Pin to Python 3.12 — ib_insync (used by /lists/all for live IBKR quotes)
REM doesn't support 3.14 yet. The `py` launcher otherwise picks the highest
REM installed version, which is currently 3.14 on this PC.
:loop
py -3.12 server.py
if "%errorlevel%"=="100" (
    echo.
    echo Dashboard requested a restart — relaunching in 1s...
    timeout /t 1 /nobreak >nul
    goto loop
)
echo Dashboard exited with code %errorlevel%.

endlocal
