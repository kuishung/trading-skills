@echo off
REM TradeHunter IBKR connector - run this on YOUR PC, with YOUR TWS logged in.
REM The browser talks to the connector on 127.0.0.1:9224; the connector reads
REM your TWS read-only. Settings: http://127.0.0.1:9224/ once it is running.
REM
REM py -3.12 is REQUIRED: ib_insync imports eventkit, which calls
REM asyncio.get_event_loop() at import time - removed in Python 3.14.
REM Extra arguments are passed through, e.g.  start_ibkr_bridge.bat --port 4002
setlocal
cd /d "%~dp0"
title TradeHunter IBKR connector
echo Starting the TradeHunter IBKR connector...
py -3.12 ibkr_bridge.py %*
if errorlevel 1 (
  echo.
  echo The connector stopped with an error. If Python 3.12 or ib_insync is missing,
  echo right-click install_bridge.ps1 and choose "Run with PowerShell", or run:
  echo     py -3.12 -m pip install --user ib_insync
  pause
)
endlocal
