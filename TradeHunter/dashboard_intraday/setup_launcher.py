"""One-time installer for the TradeHunter dashboard launchers on Windows.

Drops shortcuts in TWO locations:

  1. Desktop  -- for quick double-click launch from anywhere
     Intraday Bot Dashboard.lnk         -> start_dashboard.bat
     Intraday Bot Dashboard (stop).lnk  -> stop_dashboard.bat

  2. dashboard/ folder -- so the shortcut sits next to its target,
     visible when the user navigates into the synced TradeHunter/
     folder on any PC
     Intraday Bot Dashboard.lnk
     Intraday Bot Dashboard (stop).lnk

Both sets are idempotent: re-running overwrites them. Both contain
absolute paths so they are PER-PC -- the in-folder .lnks are gitignored
to keep them out of git, and Dropbox-sync may or may not carry them
depending on the user's `.dropboxignore`. **Re-run this installer once
per PC** after a fresh sync.

The .bat files themselves are committed and sync via Dropbox.

Run:
    py dashboard/setup_launcher.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent   # TradeHunter/
DASHBOARD_DIR = Path(__file__).resolve().parent      # TradeHunter/dashboard/
START_BAT = DASHBOARD_DIR / "start_dashboard.bat"
STOP_BAT = DASHBOARD_DIR / "stop_dashboard.bat"


def _desktop_dir() -> Path:
    r"""Locate the user's real Desktop. Handles:
      - Standard local Desktop
      - OneDrive-redirected Desktop
      - Active Directory roaming/UNC-redirected Desktop (\\server\share\...)

    Uses Windows' Special Folder API via PowerShell — the only reliable way
    on AD-managed machines.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, check=True,
        )
        path = Path(result.stdout.strip())
        if path.is_dir():
            return path
        sys.exit(
            f"Windows reports Desktop = {path}, but that path doesn't exist.\n"
            f"If your Desktop is on a network share, make sure you're online."
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"PowerShell failed locating Desktop: {e.stderr}")


def _create_shortcut(target: Path, shortcut_path: Path, description: str) -> None:
    """Create a Windows .lnk using PowerShell COM (no pywin32 needed)."""
    # PowerShell handles single-quoted literals robustly, but paths can
    # contain single quotes. Escape any single quotes by doubling them.
    def esc(s: str) -> str:
        return s.replace("'", "''")

    ps = (
        f"$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{esc(str(shortcut_path))}'); "
        f"$sc.TargetPath = '{esc(str(target))}'; "
        f"$sc.WorkingDirectory = '{esc(str(target.parent))}'; "
        f"$sc.Description = '{esc(description)}'; "
        f"$sc.IconLocation = 'cmd.exe,0'; "
        f"$sc.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> None:
    if os.name != "nt":
        sys.exit("This installer is Windows-only.")
    if not START_BAT.exists() or not STOP_BAT.exists():
        sys.exit(
            f"Launcher .bat files missing. Expected:\n"
            f"  {START_BAT}\n  {STOP_BAT}\n"
            f"Did you `git pull` the latest TradeHunter?"
        )
    desktop = _desktop_dir()
    print(f"Desktop   : {desktop}")
    print(f"Dashboard : {DASHBOARD_DIR}")
    print(f"Repo      : {SKILL_DIR}")

    # Two install sites: Desktop (global launcher) + in-folder (sync-portable
    # visibility when the user navigates into TradeHunter/dashboard/).
    sites: list[tuple[Path, str]] = [
        (desktop,       "Desktop"),
        (DASHBOARD_DIR, "dashboard/ folder"),
    ]

    created: list[Path] = []
    try:
        for parent, label in sites:
            start_lnk = parent / "Intraday Bot Dashboard.lnk"
            stop_lnk  = parent / "Intraday Bot Dashboard (stop).lnk"
            _create_shortcut(START_BAT, start_lnk,
                             "Start the TradeHunter dashboard and open it in the browser.")
            _create_shortcut(STOP_BAT, stop_lnk,
                             "Stop the running TradeHunter dashboard.")
            created.append(start_lnk)
            created.append(stop_lnk)
            print(f"  [{label:<18}] {start_lnk}")
            print(f"  [{label:<18}] {stop_lnk}")
    except subprocess.CalledProcessError as e:
        sys.exit(
            f"PowerShell failed:\n  STDOUT: {e.stdout}\n  STDERR: {e.stderr}"
        )

    print()
    print(f"Created {len(created)} shortcuts.")
    print()
    print("Double-click 'Intraday Bot Dashboard' (Desktop OR dashboard/) to launch.")
    print("The dashboard runs in a minimised cmd window and opens")
    print("http://localhost:8000 automatically.")
    print()
    print("Re-run this installer:")
    print("  - on each new PC after a fresh Dropbox sync")
    print("  - if you move the repo to a different folder")


if __name__ == "__main__":
    main()
