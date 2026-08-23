@echo off
REM TradeHunter IBKR bridge — run this on YOUR PC, with YOUR TWS logged in.
REM The server never connects to your broker; the browser talks to this bridge
REM on 127.0.0.1 and only rule evaluation happens server-side.
REM
REM py -3.12 is REQUIRED: ib_insync imports eventkit, which calls
REM asyncio.get_event_loop() at import time — removed in Python 3.14.
setlocal
cd /d "%~dp0"
echo Starting TradeHunter IBKR bridge...
py -3.12 ibkr_bridge.py %*
if errorlevel 1 (
  echo.
  echo Bridge exited with an error. If ib_insync is missing:
  echo     py -3.12 -m pip install ib_insync
  pause
)
endlocal
