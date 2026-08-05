@echo off
title TradeHunter TV Bridge
REM ===========================================================================
REM  Runs on: YOUR local PC. Starts ONLY the bridge (no Chrome).
REM  The bridge AUTO-LAUNCHES Chrome (dedicated TradeHunterTV profile) the first
REM  time you click "Plot on TV" and no debug Chrome is running — so you can close
REM  Chrome freely and plotting still brings TradingView up.
REM
REM  Put a shortcut to THIS file in shell:startup so the bridge is always running.
REM  First-time only: run launch_tv_bridge.bat once and log into TradingView in the
REM  window it opens (on a NAMED layout) so the profile remembers your login +
REM  drawings persist. After that, this bridge-only launcher is all you need.
REM ===========================================================================
where node >nul 2>nul
if errorlevel 1 (
  echo [tv-bridge] ERROR: Node.js not found on PATH. Install Node ^>= 22 and re-run.
  pause
  exit /b 1
)
node "%~dp0tv_bridge.mjs"
pause
