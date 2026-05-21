@echo off
REM Launcher for IBC (IB Gateway, paper mode). Reads IBKR credentials from
REM the Dropbox VAULT, sets env vars, then invokes IBC's start script. No
REM secrets in this file. Path-portable via %~dp0 — works wherever the
REM repo is cloned, no edits needed per PC.

setlocal EnableDelayedExpansion

set "IBC_PATH=%~dp0"
if "%IBC_PATH:~-1%"=="\" set "IBC_PATH=%IBC_PATH:~0,-1%"

set "CRED_FILE=D:\Dropbox\VAULT\Claude Credential\credentials.txt"
if not exist "%CRED_FILE%" (
    echo ERROR: credentials file not found at %CRED_FILE%
    echo If your vault drive isn't mounted, mount it first then re-run.
    exit /b 1
)

for /f "usebackq tokens=1,2 delims==" %%a in ("%CRED_FILE%") do (
    if /i "%%a"=="IbLoginId" set "TWSUSERID=%%b"
    if /i "%%a"=="IbPassword" set "TWSPASSWORD=%%b"
)

if "%TWSUSERID%"=="" (
    echo ERROR: credentials file %CRED_FILE% did not yield IbLoginId.
    exit /b 1
)

set "TWS_PATH=C:\Jts\ibgateway\1045"
set "IBC_INI=%IBC_PATH%\config.ini"
set "TRADING_MODE=paper"

REM Locate the IBC start script. Modern IBC (3.21+) has separate
REM Start{TWS,Gateway}.bat scripts per app. Older IBC (3.20 and earlier)
REM has a single StartIBC.bat that takes --gateway / --mode flags.
set "IBC_START="
set "IBC_STYLE="
if exist "%IBC_PATH%\StartGateway.bat" (
    set "IBC_START=%IBC_PATH%\StartGateway.bat"
    set "IBC_STYLE=modern"
)
if not defined IBC_START if exist "%IBC_PATH%\scripts\StartGateway.bat" (
    set "IBC_START=%IBC_PATH%\scripts\StartGateway.bat"
    set "IBC_STYLE=modern"
)
if not defined IBC_START if exist "%IBC_PATH%\StartIBC.bat" (
    set "IBC_START=%IBC_PATH%\StartIBC.bat"
    set "IBC_STYLE=legacy"
)
if not defined IBC_START if exist "%IBC_PATH%\scripts\StartIBC.bat" (
    set "IBC_START=%IBC_PATH%\scripts\StartIBC.bat"
    set "IBC_STYLE=legacy"
)
if not defined IBC_START (
    echo ERROR: No IBC start script found in %IBC_PATH% or %IBC_PATH%\scripts.
    echo Expected one of: StartGateway.bat, StartIBC.bat
    echo Did you extract the IBC zip into %IBC_PATH%?
    exit /b 1
)

echo Using IBC: %IBC_START%  ^(style: %IBC_STYLE%^)
if "%IBC_STYLE%"=="modern" (
    call "%IBC_START%" paper
) else (
    call "%IBC_START%" --gateway --mode paper --tws-path "C:\Jts\ibgateway\1045"
)

endlocal
