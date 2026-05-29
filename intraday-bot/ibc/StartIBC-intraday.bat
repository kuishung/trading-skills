@echo off
REM Launcher for IBC (IB Gateway, paper mode). Path-portable across PCs:
REM   - Credentials path: reads cfg.ibkr_secrets_path from ../config.json
REM   - IB Gateway path:  reads cfg.ibkr_gateway_path from ../config.json
REM Both lookups go through PowerShell since cmd.exe can't parse JSON
REM natively. No secrets in this file. No per-PC edits needed — change
REM the paths in config.json (gitignored, per-PC) and this script picks
REM them up automatically.

setlocal EnableDelayedExpansion

set "IBC_PATH=%~dp0"
if "%IBC_PATH:~-1%"=="\" set "IBC_PATH=%IBC_PATH:~0,-1%"

REM TradeHunter root = parent of ibc/
set "BOT_ROOT=%IBC_PATH%\.."
set "CONFIG_JSON=%BOT_ROOT%\config.json"

if not exist "%CONFIG_JSON%" (
    echo ERROR: config.json not found at %CONFIG_JSON%
    echo Copy config.example.json to config.json and set ibkr_secrets_path + ibkr_gateway_path.
    exit /b 1
)

REM Pull ibkr_secrets_path from config.json. Python is more predictable than
REM PowerShell inside cmd's for/f — cmd parser mangles nested parens + pipes.
REM sys.stdout.write avoids the trailing newline that print() would add.
REM
REM IMPORTANT: every json.load() below uses encoding='utf-8-sig' (NOT 'utf-8')
REM so a UTF-8 BOM at byte 0 -- which Windows PowerShell 5.1's
REM `Set-Content -Encoding UTF8` silently inserts -- doesn't cause
REM `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`. We hit
REM this exact failure on Hermes 2026-05-27 after a PS-driven config tweak:
REM Gateway exited at the 8am daily-reset, keep-alive task fired the bat,
REM bat couldn't parse config.json, returned 1 without launching IBC,
REM keep-alive looped forever. utf-8-sig decodes both BOM'd and BOM-less
REM UTF-8 correctly so this is the safe encoding to use everywhere we read
REM config.json from PowerShell-edited environments.
for /f "usebackq tokens=*" %%i in (`py -3.12 -c "import json,sys; sys.stdout.write(json.load(open(r'%CONFIG_JSON%', encoding='utf-8-sig')).get('ibkr_secrets_path',''))"`) do (
    set "CRED_FILE=%%i"
)

if "%CRED_FILE%"=="" (
    echo ERROR: cfg.ibkr_secrets_path not set in %CONFIG_JSON%
    exit /b 1
)

if not exist "%CRED_FILE%" (
    echo ERROR: credentials file not found at %CRED_FILE%
    echo Check that Resilio has synced HermesSync\Vault\credentials.txt
    echo or that the configured ibkr_secrets_path is correct.
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

REM Derive TWS_PATH = dirname of cfg.ibkr_gateway_path. Python instead of
REM PowerShell to avoid the cmd-parser-vs-nested-parens issue.
for /f "usebackq tokens=*" %%i in (`py -3.12 -c "import json,sys,os; sys.stdout.write(os.path.dirname(json.load(open(r'%CONFIG_JSON%', encoding='utf-8-sig')).get('ibkr_gateway_path','')))"`) do (
    set "TWS_PATH=%%i"
)

if "%TWS_PATH%"=="" (
    echo ERROR: cfg.ibkr_gateway_path not set in %CONFIG_JSON%
    exit /b 1
)

REM Determine which IB app we're launching (gateway / tws) from config.
REM Used to select which IBC start script (StartGateway.bat vs StartTWS.bat).
for /f "usebackq tokens=*" %%i in (`py -3.12 -c "import json,sys; sys.stdout.write(json.load(open(r'%CONFIG_JSON%', encoding='utf-8-sig')).get('ibkr_app_type','gateway'))"`) do (
    set "APP_TYPE=%%i"
)
if "%APP_TYPE%"=="" set "APP_TYPE=gateway"
if /i "%APP_TYPE%"=="gateway" (
    set "MODERN_START=StartGateway.bat"
) else (
    set "MODERN_START=StartTWS.bat"
)

REM Derive the exe filename from ibkr_gateway_path's basename — IB's newer
REM installers may name the exe with a version suffix (e.g., ibgateway1.exe
REM on Gateway 10.45+ to support multi-version installs). Don't hardcode.
for /f "usebackq tokens=*" %%i in (`py -3.12 -c "import json,sys,os; sys.stdout.write(os.path.basename(json.load(open(r'%CONFIG_JSON%', encoding='utf-8-sig')).get('ibkr_gateway_path','')))"`) do (
    set "APP_EXE=%%i"
)

if not exist "%TWS_PATH%\%APP_EXE%" (
    echo ERROR: %APP_EXE% not found at %TWS_PATH%
    echo Install IB %APP_TYPE% and update cfg.ibkr_gateway_path in config.json
    echo to the actual installed exe path. Newer IB Gateway versions use
    echo ibgateway1.exe instead of ibgateway.exe.
    exit /b 1
)

set "IBC_INI=%IBC_PATH%\config.ini"
set "TRADING_MODE=paper"

REM Locate the IBC start script. Modern IBC (3.21+) has separate
REM Start{TWS,Gateway}.bat scripts per app. Older IBC (3.20 and earlier)
REM has a single StartIBC.bat that takes --gateway / --tws + --mode flags.
set "IBC_START="
set "IBC_STYLE="
if exist "%IBC_PATH%\%MODERN_START%" (
    set "IBC_START=%IBC_PATH%\%MODERN_START%"
    set "IBC_STYLE=modern"
)
if not defined IBC_START if exist "%IBC_PATH%\scripts\%MODERN_START%" (
    set "IBC_START=%IBC_PATH%\scripts\%MODERN_START%"
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

echo Using IBC:      %IBC_START%  ^(style: %IBC_STYLE%, app: %APP_TYPE%^)
echo IB %APP_TYPE% dir: %TWS_PATH%
echo Credentials:    %CRED_FILE%
if "%IBC_STYLE%"=="modern" (
    call "%IBC_START%" paper
) else (
    if /i "%APP_TYPE%"=="gateway" (
        call "%IBC_START%" --gateway --mode paper --tws-path "%TWS_PATH%"
    ) else (
        call "%IBC_START%" --tws --mode paper --tws-path "%TWS_PATH%"
    )
)

endlocal
