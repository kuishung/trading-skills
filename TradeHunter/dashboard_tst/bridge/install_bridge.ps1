<#
  TradeHunter bridge installer -- run ONCE per PC. No admin rights needed.

  Run on: YOUR laptop (the PC with TWS), in PowerShell:

      powershell -ExecutionPolicy Bypass -File install_bridge.ps1

  It sets up two things, both optional and independent:

  1. AUTO-START  -- a shortcut in your Startup folder so the bridge is running
     whenever Windows is, which is the real fix for "is the bridge up?".

  2. A "Start bridge" BUTTON in the web app -- a web page cannot launch a local
     program (browsers forbid it), so this registers a custom URL protocol,
     `tradehunter://start-bridge`, the same mechanism Zoom and Teams links use.
     The button then just opens that URL and Windows runs the launcher. Chrome
     asks for confirmation the first time, which is the point: it is your
     machine deciding, not the web page.

  SECURITY: the registered command is FIXED and the URL argument (%1) is
  deliberately NOT passed to the shell. If it were, any website could put
  arbitrary text after "tradehunter://" and have it reach a command line. The
  handler can therefore only ever start this one script, with no arguments.

  Undo:  .\install_bridge.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$SkipStartup,
    [switch]$SkipProtocol
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$bat  = Join-Path $here 'start_ibkr_bridge.bat'
$startupDir = [Environment]::GetFolderPath('Startup')
$lnk  = Join-Path $startupDir 'TradeHunter IBKR Bridge.lnk'
$regRoot = 'HKCU:\Software\Classes\tradehunter'

if ($Uninstall) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed startup shortcut" }
    if (Test-Path $regRoot) { Remove-Item $regRoot -Recurse -Force; Write-Host "removed tradehunter:// handler" }
    Write-Host "`nUninstalled. The bridge itself is untouched -- start it manually with start_ibkr_bridge.bat."
    return
}

if (-not (Test-Path $bat)) { throw "start_ibkr_bridge.bat not found next to this script ($bat)" }
Write-Host "TradeHunter bridge installer"
Write-Host "  launcher: $bat`n"

# ---- 1. auto-start -----------------------------------------------------------
if (-not $SkipStartup) {
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = $bat
    $s.WorkingDirectory = $here
    $s.Description = 'TradeHunter IBKR bridge (options data from your own TWS)'
    $s.Save()
    Write-Host "[1/2] Auto-start installed:"
    Write-Host "      $lnk"
    Write-Host "      The bridge will start with Windows from now on.`n"
} else {
    Write-Host "[1/2] Auto-start skipped.`n"
}

# ---- 2. tradehunter:// protocol ---------------------------------------------
if (-not $SkipProtocol) {
    # NOTE: no %1 anywhere in this command -- see the SECURITY note above.
    $cmd = 'cmd.exe /c start "" "{0}"' -f $bat

    New-Item -Path $regRoot -Force | Out-Null
    Set-ItemProperty -Path $regRoot -Name '(default)'   -Value 'URL:TradeHunter Bridge'
    Set-ItemProperty -Path $regRoot -Name 'URL Protocol' -Value ''
    New-Item -Path "$regRoot\shell\open\command" -Force | Out-Null
    Set-ItemProperty -Path "$regRoot\shell\open\command" -Name '(default)' -Value $cmd

    Write-Host "[2/2] URL handler installed:  tradehunter://start-bridge"
    Write-Host "      -> $cmd"
    Write-Host "      The web app's 'Start the bridge' button now works."
    Write-Host "      Chrome will ask permission the first time -- that prompt is your"
    Write-Host "      machine confirming, and it is meant to be there.`n"
} else {
    Write-Host "[2/2] URL handler skipped.`n"
}

Write-Host "Done. Start it now without rebooting:"
Write-Host "  $bat"
