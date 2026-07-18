@echo off
setlocal enabledelayedexpansion
title TradeHunter TV Bridge

REM ===========================================================================
REM  TradeHunter - TV Bridge launcher (portable, per-PC double-click)
REM  Runs on: YOUR local PC (the one with your logged-in TradingView / Chrome).
REM  What it does:
REM    1. Launches Chrome with the CDP debug port (9222) + opens TradingView.
REM    2. Starts the zero-dependency Node bridge (127.0.0.1:9223).
REM  Then the TradeHunter /matp page's "Plot on TV" buttons draw MATP/MBP on
REM  YOUR chart. See bridge/README.md for the how + the "Chrome already running"
REM  gotcha.
REM ===========================================================================

REM --- locate Chrome ---------------------------------------------------------
set "CHROME="
for %%P in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do (
  if exist "%%~P" if not defined CHROME set "CHROME=%%~P"
)
if not defined CHROME (
  echo [tv-bridge] Chrome not found in the usual locations.
  echo             Launch Chrome yourself with:  --remote-debugging-port=9222
  echo             then re-run this script ^(it will still start the bridge^).
) else (
  echo [tv-bridge] Chrome: "!CHROME!"
)

REM --- profile choice --------------------------------------------------------
REM  DEFAULT below = your NORMAL Chrome profile (reuses your existing TV login),
REM  which is what you picked ("based on the user who logs into TradeHunter").
REM  IMPORTANT gotcha: if Chrome is ALREADY running on this profile, Windows
REM  just forwards the new tab to the existing process and the debug port is
REM  NOT opened. If "Plot on TV" says it can't reach the port: fully quit Chrome
REM  first, then re-run this .bat  (or add --remote-debugging-port=9222 to your
REM  everyday Chrome shortcut so it's always on).
REM
REM  Prefer a dedicated, always-works profile that runs ALONGSIDE your normal
REM  Chrome (log into TV once in it)? Uncomment the next line:
REM  set "TH_USER_DATA=--user-data-dir=%LocalAppData%\TradeHunterTV"
set "TH_USER_DATA=%TH_USER_DATA%"

if defined CHROME (
  echo [tv-bridge] Launching Chrome with remote debugging on port 9222...
  start "" "!CHROME!" --remote-debugging-port=9222 !TH_USER_DATA! "https://www.tradingview.com/chart/"
)

REM --- start the bridge ------------------------------------------------------
where node >nul 2>nul
if errorlevel 1 (
  echo [tv-bridge] ERROR: Node.js not found on PATH. Install Node ^>= 22 and re-run.
  pause
  exit /b 1
)

echo [tv-bridge] Starting bridge (Ctrl+C to stop)...
node "%~dp0tv_bridge.mjs"

pause
