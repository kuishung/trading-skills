# setup_hermes_tray_task.ps1 - register IntradayBot-Tray scheduled task
# that auto-launches the ingest progress tray icon when the user logs in.
#
# Pairs with the existing IntradayBot-Watcher (seed) and IntradayBot-Gateway
# (Gateway keep-alive) tasks so the FULL stack -- including the tray icon --
# comes up automatically. User rule 2026-05-27: "when the seed process is
# fired, the tray icon should come along and show up, not separate".
#
# Why a separate task (not bundled with the supervisor):
#   - The seed supervisor runs under S4U (no desktop) -- correct, since it
#     doesn't need one.
#   - The tray REQUIRES a desktop session (system-tray icons live in the
#     logged-in user's session). It can't run from an S4U task.
#   - Separating concerns: AtStartup for headless services (supervisor,
#     Gateway keep-alive), AtLogon for desktop-dependent UI (tray).
#
# Behavior:
#   - You RDP into Hermes as Administrator -> AtLogon trigger fires
#     -> tray icon appears in the system tray immediately.
#   - You disconnect RDP -> the tray process may persist or terminate
#     depending on Windows's session policy; on next RDP login the task
#     fires again, restarting the tray if needed (MultipleInstances
#     IgnoreNew prevents duplicates if it's still alive).
#   - Hermes reboot with auto-login enabled -> AtLogon fires once boot
#     completes.
#
# Idempotent: safe to re-run; -Force overwrites the existing task.
#
# Typical use on Hermes (after the supervisor + Gateway tasks are set up):
#     cd C:\ClaudeSkills\trading-skills\intraday-bot
#     powershell -ExecutionPolicy Bypass -File .\scripts\setup_hermes_tray_task.ps1 -StartNow
#
# ASCII-only per the PS 5.1 em-dash lesson (Watch-Ingest.ps1's history).
# No null-coalescing -- PS 5.1 doesn't have it.

param(
    [string] $TaskName = 'IntradayBot-Tray',
    [string] $UserId   = 'Administrator',
    [switch] $StartNow = $false
)

# --- Resolve paths ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotRoot   = Split-Path -Parent $ScriptDir
$TrayPy    = Join-Path $BotRoot "dashboard\tray_status.py"

if (-not (Test-Path $TrayPy)) {
    Write-Error "tray_status.py not found at $TrayPy"
    exit 1
}

# --- Find pythonw.exe (console-less Python; required for tray apps so no cmd
#     window flashes at launch). Strategy: ask py launcher for the Python 3.12
#     install dir, then look for pythonw.exe alongside python.exe. Fall back
#     to common install locations.
$pyw = $null
try {
    $pyExe = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null)
    if ($pyExe) {
        $candidate = $pyExe -replace '\\python\.exe$', '\pythonw.exe'
        if (Test-Path $candidate) { $pyw = $candidate }
    }
} catch {
    # py launcher failed; fall through to manual search
}

if (-not $pyw) {
    foreach ($p in @(
        'C:\Python312\pythonw.exe',
        'C:\Python311\pythonw.exe',
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
        "$env:ProgramFiles\Python312\pythonw.exe",
        "$env:ProgramFiles\Python311\pythonw.exe"
    )) {
        if (Test-Path $p) { $pyw = $p; break }
    }
}

if (-not $pyw) {
    Write-Error "Could not find pythonw.exe automatically. Install Python 3.12+ from python.org or pass the path manually:"
    Write-Error "  Register-ScheduledTask with -Action (New-ScheduledTaskAction -Execute 'C:\path\to\pythonw.exe' ...)"
    exit 1
}

Write-Host "Found pythonw.exe at: $pyw" -ForegroundColor DarkGray

# --- Build the task ---
$action = New-ScheduledTaskAction `
    -Execute $pyw `
    -Argument "`"$TrayPy`"" `
    -WorkingDirectory $BotRoot

# AtLogon trigger fires whenever the specified user logs in interactively.
# RDP login counts. This is what makes the tray "come along" with each RDP.
$trigger = New-ScheduledTaskTrigger -AtLogon -User $UserId

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)
# ExecutionTimeLimit 0 = no time limit; the tray is meant to run for the
# lifetime of the user session.

# CRITICAL: LogonType Interactive (NOT S4U which the supervisor uses).
# Interactive means the task runs in the user's desktop session, which is
# REQUIRED for system-tray icons to be visible. RunLevel Limited is correct
# for a non-elevated UI app.
$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId -LogonType Interactive -RunLevel Limited

# --- Register (or update existing) ---
try {
    $null = Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "Auto-launch the ingest-progress tray icon when $UserId logs in interactively (including RDP). Pairs with IntradayBot-Watcher + IntradayBot-Gateway so the full Hermes stack -- supervisor + Gateway + tray UI -- comes up without manual intervention." `
        -Force
} catch {
    Write-Error "Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Error "Common cause: not running PowerShell as Administrator."
    exit 1
}

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $verify) {
    Write-Error "Task '$TaskName' did not register."
    exit 1
}

Write-Host ""
Write-Host "Task registered: $TaskName  (State: $($verify.State))" -ForegroundColor Green
Write-Host "  Trigger     : AtLogon (UserId=$UserId)"
Write-Host "  Action      : $pyw  `"$TrayPy`""
Write-Host "  Working dir : $BotRoot"
Write-Host "  Principal   : $UserId  (Interactive, RunLevel Limited)"
Write-Host ""

# --- Optionally start now (only if no tray is currently running) ---
if ($StartNow) {
    $existing = $null
    try {
        $existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
                ($_.CommandLine -like '*tray_status.py*')
            } | Select-Object -First 1
    } catch {}
    if ($existing) {
        Write-Warning "-StartNow ignored: tray is already running (PID $($existing.ProcessId))."
        Write-Warning "Stop it first if you want the scheduled task to take over:"
        Write-Warning "  Stop-Process -Id $($existing.ProcessId) -Force"
    } else {
        try {
            Start-ScheduledTask -TaskName $TaskName
            Start-Sleep -Seconds 3
            $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
            Write-Host "Task started. LastTaskResult: $($info.LastTaskResult) (expect 0 = success)" -ForegroundColor Green
            if ($info.LastTaskResult -eq 267009) {
                Write-Host "  (267009 = SCHED_S_TASK_RUNNING -- tray is in startup; should be visible in the tray within seconds)"
            }
        } catch {
            Write-Error "Start-ScheduledTask failed: $($_.Exception.Message)"
            exit 1
        }
    }
} else {
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "  - Task auto-fires on the next RDP / Hermes logon."
    Write-Host "  - To launch now (only if tray isn't already running):"
    Write-Host "      Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  - To inspect status:"
    Write-Host "      Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
    Write-Host "  - To remove:"
    Write-Host "      Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
}
