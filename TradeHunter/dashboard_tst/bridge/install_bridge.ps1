<#
  TradeHunter IBKR connector installer -- run ONCE per PC. No admin rights needed.

  Run on: YOUR PC (the one with TWS). Easiest: right-click this file >
  "Run with PowerShell". Or, in PowerShell in this folder:

      powershell -ExecutionPolicy Bypass -File install_bridge.ps1

  What it does, in order:

  1. PYTHON 3.12 -- the connector needs it (ib_insync cannot load on 3.14).
     If "py -3.12" does not answer, it asks before running
         winget install -e --id Python.Python.3.12

  2. LIBRARY -- py -3.12 -m pip install --user -r requirements.txt (ib_insync).

  3. AUTO-START -- a shortcut in your Startup folder, so the connector runs
     whenever Windows does.

  4. A "Start" BUTTON in the web app -- a web page cannot launch a local
     program (browsers forbid it), so this registers a custom URL protocol,
     tradehunter://start-bridge, the same mechanism Zoom and Teams links use.
     Chrome asks for confirmation the first time, which is the point: it is
     your machine deciding, not the web page.

  5. Starts the connector and opens its settings page, http://127.0.0.1:9224/,
     where you pick your TWS port.

  SECURITY: the registered command is FIXED and the URL argument (%1) is
  deliberately NOT passed to the shell. If it were, any website could put
  arbitrary text after "tradehunter://" and have it reach a command line. The
  handler can therefore only ever start this one script, with no arguments.

  Undo:  powershell -ExecutionPolicy Bypass -File install_bridge.ps1 -Uninstall
  (This file is ASCII only on purpose: Windows PowerShell 5.1 reads a file
  without a BOM in the ANSI code page.)
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$SkipPython,
    [switch]$SkipStartup,
    [switch]$SkipProtocol,
    [switch]$NoStart,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$bat  = Join-Path $here 'start_ibkr_bridge.bat'
$req  = Join-Path $here 'requirements.txt'
$startupDir = [Environment]::GetFolderPath('Startup')
$lnk  = Join-Path $startupDir 'TradeHunter IBKR Bridge.lnk'
$regRoot = 'HKCU:\Software\Classes\tradehunter'
$settingsUrl = 'http://127.0.0.1:9224/'

function Wait-Close {
    # "Run with PowerShell" closes the window when the script ends; keep the result readable.
    if (-not $NoPause) { [void](Read-Host "`nPress Enter to close this window") }
}

function Get-Py312 {
    # The version text of "py -3.12", or $null. Native stderr must not become a
    # terminating error here, so the preference is relaxed around the call.
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $null }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & py -3.12 -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
        return $null
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $old
    }
}

function Update-PathFromRegistry {
    # A fresh install changes PATH in the registry, not in this session.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Test-Connector {
    try {
        $h = Invoke-RestMethod -Uri ($settingsUrl + 'health') -TimeoutSec 2
        return [bool]$h.ok
    } catch {
        return $false
    }
}

if ($Uninstall) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed startup shortcut" }
    if (Test-Path $regRoot) { Remove-Item $regRoot -Recurse -Force; Write-Host "removed tradehunter:// handler" }
    Write-Host "`nUninstalled. The connector files and your settings"
    Write-Host "($env:APPDATA\TradeHunter\connector.json) are untouched; delete them by hand if you like."
    Wait-Close
    return
}

try {
    if (-not (Test-Path $bat)) { throw "start_ibkr_bridge.bat not found next to this script ($bat)" }
    Write-Host "TradeHunter IBKR connector installer"
    Write-Host "  folder: $here`n"

    # Files unzipped from a download carry the internet zone mark; clear it so the
    # launcher starts without a security prompt every time Windows boots. Only the
    # files the connector ships - never anything else that shares this folder.
    foreach ($f in @('ibkr_bridge.py', 'th_ibkr.py', 'start_ibkr_bridge.bat', 'install_bridge.ps1', 'requirements.txt', 'README.txt')) {
        $p = Join-Path $here $f
        if (Test-Path $p) { Unblock-File -Path $p -ErrorAction SilentlyContinue }
    }

    # ---- 1. Python 3.12 --------------------------------------------------------
    if (-not $SkipPython) {
        $ver = Get-Py312
        if (-not $ver) {
            Write-Host "[1/5] Python 3.12 was not found (the connector needs exactly 3.12)."
            if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
                throw ("winget is not available on this PC. Install Python 3.12 from " +
                       "https://www.python.org/downloads/ (tick 'py launcher'), then run this again.")
            }
            $answer = Read-Host "      Install Python 3.12 now with winget? (y/n)"
            if ($answer -notmatch '^(y|yes)$') {
                throw "Python 3.12 is required. Install it, then run this again."
            }
            & winget install -e --id Python.Python.3.12
            Update-PathFromRegistry
            $ver = Get-Py312
            if (-not $ver) {
                throw ("Python 3.12 is still not answering to 'py -3.12'. Close this window, " +
                       "open a new one and run this installer again.")
            }
        }
        Write-Host "[1/5] Python $ver found (py -3.12).`n"

        # ---- 2. ib_insync ------------------------------------------------------
        Write-Host "[2/5] Installing the connector's library (ib_insync)..."
        $old = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & py -3.12 -m pip install --user --disable-pip-version-check -r $req
        $code = $LASTEXITCODE
        $ErrorActionPreference = $old
        if ($code -ne 0) { throw "pip could not install ib_insync (exit code $code). See the messages above." }
        Write-Host "[2/5] Library installed.`n"
    } else {
        Write-Host "[1/5] Python check skipped.`n[2/5] Library install skipped.`n"
    }

    # ---- 3. auto-start ---------------------------------------------------------
    if (-not $SkipStartup) {
        $ws = New-Object -ComObject WScript.Shell
        $s = $ws.CreateShortcut($lnk)
        $s.TargetPath = $bat
        $s.WorkingDirectory = $here
        $s.WindowStyle = 7
        $s.Description = 'TradeHunter IBKR connector (option data from your own TWS)'
        $s.Save()
        Write-Host "[3/5] Auto-start installed:"
        Write-Host "      $lnk"
        Write-Host "      The connector will start (minimised) with Windows from now on.`n"
    } else {
        Write-Host "[3/5] Auto-start skipped.`n"
    }

    # ---- 4. tradehunter:// protocol -------------------------------------------
    if (-not $SkipProtocol) {
        # NOTE: no %1 anywhere in this command -- see the SECURITY note above.
        $cmd = 'cmd.exe /c start "" "{0}"' -f $bat

        New-Item -Path $regRoot -Force | Out-Null
        Set-ItemProperty -Path $regRoot -Name '(default)'   -Value 'URL:TradeHunter Bridge'
        Set-ItemProperty -Path $regRoot -Name 'URL Protocol' -Value ''
        New-Item -Path "$regRoot\shell\open\command" -Force | Out-Null
        Set-ItemProperty -Path "$regRoot\shell\open\command" -Name '(default)' -Value $cmd

        Write-Host "[4/5] URL handler installed:  tradehunter://start-bridge"
        Write-Host "      -> $cmd"
        Write-Host "      The Options page's 'Start' button now works. Chrome asks"
        Write-Host "      permission the first time -- that prompt is meant to be there.`n"
    } else {
        Write-Host "[4/5] URL handler skipped.`n"
    }

    # ---- 5. start it and open the settings page -------------------------------
    if (-not $NoStart) {
        if (-not (Test-Connector)) {
            Start-Process -FilePath $bat -WorkingDirectory $here
            Write-Host "[5/5] Starting the connector..."
            $up = $false
            for ($i = 0; $i -lt 30 -and -not $up; $i++) {
                Start-Sleep -Seconds 1
                $up = Test-Connector
            }
            if (-not $up) {
                Write-Host "      It has not answered yet -- check its window for a message."
            }
        } else {
            Write-Host "[5/5] The connector is already running."
        }
        Write-Host "      Opening $settingsUrl -- pick your TWS port there and press Save & reconnect."
        Start-Process $settingsUrl
    } else {
        Write-Host "[5/5] Not started (-NoStart). Start it with: $bat"
    }

    Write-Host "`nDone. In TWS: File > Global Configuration > API > Settings > tick"
    Write-Host "'Enable ActiveX and Socket Clients' (Read-Only API can stay ticked)."
} catch {
    Write-Host "`nInstall stopped: $($_.Exception.Message)" -ForegroundColor Red
    Wait-Close
    exit 1
}
Wait-Close
