"""Register Windows Task Scheduler entries for IB Gateway auto-start.

Creates TWO scheduled tasks:

  1. "intraday-ibc-start"   — runs IBC weekdays at 08:30 local clock. IBC launches
                          IB Gateway and auto-fills the paper login.
  2. "intraday-ibc-stop"    — runs each weekday at 16:30 local clock to close
                          Gateway cleanly. Optional but recommended so the
                          process doesn't pile up if you forget.

Usage:
    py scripts/setup_gateway_autostart.py
    py scripts/setup_gateway_autostart.py --unregister
    py scripts/setup_gateway_autostart.py --start-time 08:30 --stop-time 16:30

Notes:
- The times you pass are LOCAL clock, not ET. If your machine isn't on
  America/New_York, convert ET to your local TZ before invoking.
- IBC must already be installed (download from
  https://github.com/IbcAlpha/IBC/releases) and configured via
  scripts/setup_ibkr.py.
- The bot script (trade_day.py) is NOT auto-scheduled by this — use
  scripts/setup_schedule.py for that. They're separate processes:
      Gateway autostart at 08:30 -> Gateway is up by 08:35
      Bot autostart at 08:55     -> Bot connects to Gateway, runs the day
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config  # noqa: E402

START_TASK = "intraday-ibc-start"
STOP_TASK = "intraday-ibc-stop"


def find_ibc_start_script(cfg: dict, ibc_dir: Path) -> Path:
    """We prefer the intraday_bot wrapper (StartIBC-intraday.bat) which sources
    credentials from the user's chosen path. Fall back to IBC's bundled
    StartIBC.bat only if the wrapper isn't present (and warn the user)."""
    # 1. The launcher we wrote (reads from cfg['ibkr_secrets_path'])
    launcher_str = cfg.get("ibkr_launcher_bat")
    if launcher_str:
        launcher = Path(launcher_str)
        if launcher.exists():
            return launcher
        sys.exit(
            f"Configured ibkr_launcher_bat ({launcher}) doesn't exist.\n"
            f"Re-run scripts/setup_ibkr.py to regenerate it."
        )

    # 2. Plain IBC entrypoint as fallback — only used if setup_ibkr.py never ran
    candidates = [
        ibc_dir / "StartIBC.bat",
        ibc_dir / "scripts" / "StartIBC.bat",
        ibc_dir / "IbcStart.bat",
    ]
    for c in candidates:
        if c.exists():
            print(f"WARN: using IBC's default {c} — credentials must be in")
            print(f"      ibc/config.ini's IbLoginId/IbPassword fields. For a")
            print(f"      vault-aware setup, run scripts/setup_ibkr.py first.")
            return c
    sys.exit(
        f"Couldn't find any IBC start script under {ibc_dir}. Make sure you've\n"
        f"downloaded IBC from https://github.com/IbcAlpha/IBC/releases and\n"
        f"extracted it there, then run scripts/setup_ibkr.py."
    )


def find_ibc_stop_script(ibc_dir: Path) -> Path | None:
    """IBC's Stop script — not always present in older releases."""
    candidates = [
        ibc_dir / "StopIBC.bat",
        ibc_dir / "scripts" / "StopIBC.bat",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def register_windows(start_script: Path, start_time: str,
                     stop_script: Path | None, stop_time: str) -> int:
    rc = 0
    cmd = (
        f'schtasks /Create /TN "{START_TASK}" /SC WEEKLY '
        f'/D MON,TUE,WED,THU,FRI '
        f'/TR "\\"{start_script}\\"" '
        f'/ST {start_time} /F'
    )
    print(f"Running: {cmd}")
    rc = subprocess.call(cmd, shell=True) or rc

    if stop_script:
        cmd2 = (
            f'schtasks /Create /TN "{STOP_TASK}" /SC WEEKLY '
            f'/D MON,TUE,WED,THU,FRI '
            f'/TR "\\"{stop_script}\\"" '
            f'/ST {stop_time} /F'
        )
        print(f"Running: {cmd2}")
        rc = subprocess.call(cmd2, shell=True) or rc
    else:
        print(f"(no StopIBC.bat found — Gateway will keep running until you "
              "manually close it or reboot. Not a problem; just an FYI.)")
    return rc


def unregister_windows() -> int:
    rc = 0
    for task in (START_TASK, STOP_TASK):
        cmd = f'schtasks /Delete /TN "{task}" /F'
        print(f"Running: {cmd}")
        rc = subprocess.call(cmd, shell=True) or rc
    return rc


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--unregister", action="store_true",
                   help="Remove both scheduled tasks.")
    p.add_argument("--start-time", default="08:30",
                   help="LOCAL clock to start IB Gateway (default 08:30).")
    p.add_argument("--stop-time", default="16:30",
                   help="LOCAL clock to stop IB Gateway (default 16:30).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if platform.system() != "Windows":
        print("This script is Windows-only. On macOS/Linux, use cron + a small")
        print("wrapper script to call IBC's start/stop entrypoints.")
        return 1

    if args.unregister:
        return unregister_windows()

    cfg = load_config()
    ibc_dir = cfg.get("ibkr_ibc_dir")
    if not ibc_dir:
        sys.exit("ibkr_ibc_dir not set in config.json. Run scripts/setup_ibkr.py first.")
    ibc_dir = Path(ibc_dir)
    if not ibc_dir.exists():
        sys.exit(f"ibkr_ibc_dir {ibc_dir} doesn't exist. Did IBC install complete?")

    start_script = find_ibc_start_script(cfg, ibc_dir)
    stop_script = find_ibc_stop_script(ibc_dir)

    print(f"""About to register:
  Task   : {START_TASK}
  Time   : {args.start_time} (LOCAL clock) MON-FRI
  Cmd    : {start_script}

  Task   : {STOP_TASK}
  Time   : {args.stop_time} (LOCAL clock) MON-FRI
  Cmd    : {stop_script if stop_script else '(not registered — no StopIBC.bat found)'}
""")
    ans = input("Proceed? [y/N] ").strip().lower()
    if ans != "y":
        print("Cancelled.")
        return 1
    return register_windows(start_script, args.start_time, stop_script, args.stop_time)


if __name__ == "__main__":
    sys.exit(main())
