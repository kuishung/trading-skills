# setup_hermes_ingest_supervisor_task.ps1 - register ingest_supervisor.py as a
# Windows Scheduled Task that auto-starts at Hermes boot and survives RDP
# disconnects. The supervisor itself decides (in ET) when to open the Gateway,
# top up, deep-check, and shut the Gateway down -- so a single boot trigger is
# all that's needed (no DST-fragile fixed-time triggers).
#
# This REPLACES the old IntradayBot-Gateway keep-alive + Hermes-IBC-Start-
# PostMarket + IntradayBot-Watcher tasks (disable those first; see DEPLOY).
#
# Idempotent: safe to re-run; overwrites the existing task definition.
#
# Typical use on Hermes (one-shot):
#     cd C:\trading-skills\TradeHunter
#     powershell -ExecutionPolicy Bypass -File .\scripts\setup_hermes_ingest_supervisor_task.ps1 -StartNow
#
# ASCII-only file: NO em-dashes / smart quotes (PS 5.1 Windows-1252 tokenizer trap).

param(
    [string] $TaskName = 'TradeHunter-IngestSupervisor',
    [string] $UserId   = 'Administrator',
    [switch] $Interactive = $false,   # run in the logged-on DESKTOP session so it
                                      # can launch the IB Gateway GUI (S4U/background
                                      # cannot). Requires the user to be logged on
                                      # (use auto-logon so it survives reboots).
    [switch] $StartNow = $false
)

# --- Resolve paths relative to this script ---
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotRoot   = Split-Path -Parent $ScriptDir
$Supervisor = Join-Path $ScriptDir 'ingest_supervisor.py'

if (-not (Test-Path $Supervisor)) {
    Write-Error "ingest_supervisor.py not found at $Supervisor"
    exit 1
}

# --- Resolve the py launcher (3.12 is mandatory for ib_insync) ---
$Py = 'C:\Windows\py.exe'
if (-not (Test-Path $Py)) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source } else {
        Write-Error "py launcher not found (expected C:\Windows\py.exe). Install Python launcher."
        exit 1
    }
}

# --- Detect an already-running supervisor (avoid duplicate -> Gateway/clientId fights) ---
$existing = $null
try {
    $existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*ingest_supervisor*' }
} catch {}
if ($existing) {
    $pidsList = ($existing | ForEach-Object { $_.ProcessId }) -join ', '
    Write-Warning "An ingest_supervisor already appears to be running (PID(s): $pidsList)."
    Write-Warning "-StartNow will be ignored to avoid a duplicate."
}

# --- Build + register the task ---
$arguments = "-3.12 `"$Supervisor`""

$action = New-ScheduledTaskAction -Execute $Py -Argument $arguments -WorkingDirectory $BotRoot
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 = no limit; runs forever
# MultipleInstances=IgnoreNew so the 5-min keepalive repetition (added to the
# trigger below) NEVER starts a duplicate supervisor while one is alive -- it
# relaunches ONLY when the process is actually gone (crash / kill / clean exit /
# RestartCount exhausted). This resurrects the SUPERVISOR itself within ~5 min.

if ($Interactive) {
    # Run in the user's logged-on DESKTOP session so child IB Gateway GUI
    # launches have a desktop. Trigger at logon (pair with auto-logon so a
    # console session always exists after a reboot). RunLevel Highest.
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $UserId
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Highest
    $TrigDesc  = "AtLogOn ($UserId), Interactive"
} else {
    # Background (S4U) at boot. Works for everything EXCEPT launching the GUI
    # Gateway (no desktop) -- use -Interactive on hosts that must auto-launch it.
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Highest
    $TrigDesc  = "AtStartup, S4U"
}

# Keepalive: repeat the trigger every 5 min indefinitely. Combined with
# MultipleInstances=IgnoreNew, Task Scheduler re-launches the supervisor whenever
# it's NOT running (and skips the fire when it IS) -- so a crashed/killed/exited
# supervisor self-resurrects within ~5 min, no separate watchdog needed. Built
# via the standard dummy -Once trigger trick (AtLogOn/AtStartup triggers don't
# take -RepetitionInterval directly in PS 5.1).
try {
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
    $TrigDesc += " + repeat 5m (keepalive)"
} catch {
    Write-Warning "Could not attach 5-min keepalive repetition: $($_.Exception.Message)"
}

try {
    $null = Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description 'TradeHunter ingest supervisor: daily post-extended-close top-up + deep check, Gateway lifecycle, ET-windowed (20:10->08:00 ET), blackout 08:00->20:10 ET for manual trading. Auto-starts at boot.' `
        -Force
} catch {
    Write-Error "Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Error "Common causes: not Administrator; UserId '$UserId' missing; S4U requires that user to have logged in once."
    exit 1
}

$verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $verify) { Write-Error "Task '$TaskName' did not register."; exit 1 }

Write-Host ""
Write-Host "Task registered: $TaskName  (State: $($verify.State))" -ForegroundColor Green
Write-Host "  Execute      : $Py $arguments"
Write-Host "  Trigger      : $TrigDesc"
Write-Host "  Working dir  : $BotRoot"
Write-Host "  Principal    : $UserId (S4U, RunLevel Highest)"
Write-Host ""

if ($StartNow) {
    if ($existing) {
        Write-Warning "-StartNow ignored: a supervisor is already running."
    } else {
        try {
            Start-ScheduledTask -TaskName $TaskName
            Start-Sleep -Seconds 3
            $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
            Write-Host "Started. LastTaskResult: $($info.LastTaskResult) (267009 = running)" -ForegroundColor Green
            Write-Host "Tail the supervisor log: Get-Content <data_root>\_supervisor.log -Wait"
        } catch {
            Write-Error "Start-ScheduledTask failed: $($_.Exception.Message)"; exit 1
        }
    }
} else {
    Write-Host "Next: fires at next boot, or start now with:" -ForegroundColor Yellow
    Write-Host "      Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  Remove with:"
    Write-Host "      Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
}
