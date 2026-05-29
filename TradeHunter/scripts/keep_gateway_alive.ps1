# keep_gateway_alive.ps1 - idempotent "is Gateway running? if not, launch it" check
# Called by the IntradayBot-Gateway scheduled task every N minutes (default 5).
#
# Why this exists: IBC supervises Gateway (dismisses dialogs, handles 2FA, etc.)
# but does NOT restart Gateway after a crash unless AutoRestartTime is configured
# (which has constraints around TwsSettingsPath that we don't currently meet).
# Without an external keep-alive, the failure modes we observed on Hermes today
# (Gateway shutdown at IBKR daily reset, OS update interruption, manual quit,
# OOM, etc.) would leave the watcher in a tight crash-loop on failed connect.
#
# Recovery target: detect a dead Gateway within ~5 min and relaunch via IBC.
# The watcher's _ensure_connected backoff (5..60s, up to 20 attempts) easily
# bridges the gap so no manual intervention is needed.
#
# ASCII-only per the PS 5.1 em-dash lesson (Watch-Ingest.ps1's history).

param(
    [int]    $Port        = 4002,
    [string] $LauncherBat = ""    # default: <bot_root>\ibc\StartIBC-intraday.bat
)

# --- Resolve paths from this script's location (path-portable) ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotRoot   = Split-Path -Parent $ScriptDir
if (-not $LauncherBat) {
    $LauncherBat = Join-Path $BotRoot "ibc\StartIBC-intraday.bat"
}

# --- Log to data_root so the entry tracks alongside other ingest logs ---
$DataRoot = $BotRoot
$CfgPath  = Join-Path $BotRoot "config.json"
if (Test-Path $CfgPath) {
    try {
        $cfg = Get-Content $CfgPath -Raw | ConvertFrom-Json
        if ($cfg.data_root) { $DataRoot = $cfg.data_root }
    } catch {
        # config malformed -- fall back to bot root, no log spam
    }
}
$LogFile = Join-Path $DataRoot "_gateway_keepalive.log"

function Write-Log($msg) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    try {
        Add-Content -Path $LogFile -Value "[$stamp] $msg" -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {}
}

# --- Check 1: is something listening on the Gateway API port? ---
# If yes, Gateway is operationally usable; the watcher can connect. Done.
# Don't log this case -- it's the common path and would spam the log every 5 min.
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    exit 0
}

# --- Check 2: is IBC/Gateway in the middle of starting up? ---
# Login takes ~30-60s. Without this check, a 5-min trigger could launch a second
# IBC just as the first one is logging in -- two Gateway sessions on the same
# account, IBKR kicks one, we're worse off than before.
# Match: java/javaw with IBC.jar in cmdline, OR ibgateway*.exe processes.
$starting = $null
try {
    $starting = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.Name -eq 'java.exe' -or
             $_.Name -eq 'javaw.exe' -or
             $_.Name -like 'ibgateway*') -and
            ($_.CommandLine -like '*IBC*' -or
             $_.CommandLine -like '*ibgateway*' -or
             $_.CommandLine -like '*ibcalpha*')
        } | Select-Object -First 1
} catch {
    # CIM failure -- treat as "no process detected" and proceed to launch.
    # Better to risk a duplicate launch than to never recover from a crash.
}

if ($starting) {
    Write-Log ("Port {0} not listening yet, but IBC/Gateway process is alive (PID {1}, name {2}). Assuming startup in progress; no action." -f $Port, $starting.ProcessId, $starting.Name)
    exit 0
}

# --- Check 3: launch IBC + Gateway ---
if (-not (Test-Path $LauncherBat)) {
    Write-Log "FATAL: launcher not found at $LauncherBat"
    exit 1
}

Write-Log "Gateway DOWN: port $Port not listening, no IBC/Gateway process detected. Launching $LauncherBat"
try {
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", $LauncherBat `
        -WorkingDirectory (Split-Path -Parent $LauncherBat) `
        -WindowStyle Minimized
    Write-Log "Launched. Next check (in ~5 min) will verify port $Port is listening."
    exit 0
} catch {
    Write-Log "FATAL: Start-Process failed: $($_.Exception.Message)"
    exit 1
}
