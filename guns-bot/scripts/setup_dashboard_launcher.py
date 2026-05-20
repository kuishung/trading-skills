"""One-time installer for the intraday_bot dashboard launchers on Windows.

Drops two shortcuts on the user's Desktop:

  Intraday Bot Dashboard.lnk        -> start_dashboard.bat
  Intraday Bot Dashboard (stop).lnk -> stop_dashboard.bat

Idempotent: re-running overwrites the shortcuts so paths stay correct if
you move the repo. The .bat files themselves are committed to the repo and
sync via Dropbox; only the per-user .lnk files live on the local desktop.

Run:
    py scripts/setup_dashboard_launcher.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
START_BAT = SKILL_DIR / "start_dashboard.bat"
STOP_BAT = SKILL_DIR / "stop_dashboard.bat"


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
            f"Did you `git pull` the latest intraday_bot?"
        )
    desktop = _desktop_dir()
    print(f"Desktop : {desktop}")
    print(f"Repo    : {SKILL_DIR}")

    start_lnk = desktop / "Intraday Bot Dashboard.lnk"
    stop_lnk = desktop / "Intraday Bot Dashboard (stop).lnk"

    try:
        _create_shortcut(START_BAT, start_lnk,
                         "Start the intraday_bot dashboard and open it in the browser.")
        _create_shortcut(STOP_BAT, stop_lnk,
                         "Stop the running intraday_bot dashboard.")
    except subprocess.CalledProcessError as e:
        sys.exit(
            f"PowerShell failed:\n  STDOUT: {e.stdout}\n  STDERR: {e.stderr}"
        )

    print()
    print("Created shortcuts:")
    print(f"  {start_lnk}")
    print(f"  {stop_lnk}")
    print()
    print("Double-click 'Intraday Bot Dashboard' to launch. The dashboard runs in a")
    print("minimised cmd window and opens http://localhost:8000 automatically.")
    print("Re-run this installer if you move the repo to a different folder.")


if __name__ == "__main__":
    main()
