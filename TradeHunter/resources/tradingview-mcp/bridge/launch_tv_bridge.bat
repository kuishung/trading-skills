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
REM  DEFAULT = a DEDICATED "TradeHunterTV" profile that runs ALONGSIDE your normal
REM  Chrome and ALWAYS opens the debug port (a normal-profile launch fails to open
REM  the port when Chrome is already running / has a background process). Log into
REM  TradingView ONCE in this window; the login persists in this profile, and your
REM  layouts are account-side so they show up in your normal Chrome too.
REM
REM  Want to use your EVERYDAY Chrome profile/login instead (no second window)?
REM  Comment the line below AND add --remote-debugging-port=9222 to your everyday
REM  Chrome shortcut (and turn OFF chrome://settings/system "continue running
REM  background apps" so a full close actually frees the port).
set "TH_USER_DATA=--user-data-dir=%LocalAppData%\TradeHunterTV"

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
