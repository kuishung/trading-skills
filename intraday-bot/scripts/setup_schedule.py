"""Register a Windows Task Scheduler job that runs trade_day.py at 08:55 ET
every weekday. On macOS/Linux, prints the equivalent cron line.

Usage:
    py scripts/setup_schedule.py            # interactive: prompts to confirm
    py scripts/setup_schedule.py --unregister
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

TASK_NAME = "intraday-bot-daily"
SKILL_DIR = Path(__file__).resolve().parent.parent
TRADE_DAY = SKILL_DIR / "scripts" / "trade_day.py"


def register_windows(start_time_local: str) -> int:
    """Register a daily task at `start_time_local` (HH:MM, *local* clock).

    The user's local clock is not necessarily ET — they need to know that and
    we surface a clear note.
    """
    python = sys.executable
    cmd = (
        f'schtasks /Create /TN "{TASK_NAME}" /SC WEEKLY '
        f'/D MON,TUE,WED,THU,FRI '
        f'/TR "\\"{python}\\" \\"{TRADE_DAY}\\"" '
        f'/ST {start_time_local} /F'
    )
    print(f"Running: {cmd}")
    return subprocess.call(cmd, shell=True)


def unregister_windows() -> int:
    cmd = f'schtasks /Delete /TN "{TASK_NAME}" /F'
    print(f"Running: {cmd}")
    return subprocess.call(cmd, shell=True)


def print_cron(start_time_local: str) -> None:
    python = shutil.which("python3") or sys.executable
    hh, mm = start_time_local.split(":")
    print(
        "\nAdd this line to your crontab (run `crontab -e`):\n"
        f"  {mm} {hh} * * 1-5 {python} {TRADE_DAY}\n\n"
        "Note: the time above is your local clock, NOT ET. Adjust accordingly "
        "or use a TZ= prefix:\n"
        f"  TZ=America/New_York {mm} {hh} * * 1-5 {python} {TRADE_DAY}\n"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--unregister", action="store_true",
                   help="Remove the scheduled task.")
    p.add_argument("--time", default="08:55",
                   help="LOCAL wall-clock start time (default 08:55). "
                        "If your machine isn't on ET, convert ET->local manually.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not TRADE_DAY.exists():
        sys.exit(f"trade_day.py not found at {TRADE_DAY}")

    if platform.system() == "Windows":
        if args.unregister:
            return unregister_windows()
        print(
            "About to register a Windows Task Scheduler job:\n"
            f"  Name : {TASK_NAME}\n"
            f"  Time : {args.time} (LOCAL clock)\n"
            f"  Days : MON-FRI\n"
            f"  Cmd  : {sys.executable} {TRADE_DAY}\n"
            "\nNote: if your local clock isn't ET, the bot's internal ET clock "
            "will still anchor phases correctly — it just means trade_day.py "
            "may start before or after 08:55 ET depending on your TZ.\n"
        )
        ans = input("Proceed? [y/N] ").strip().lower()
        if ans != "y":
            print("Cancelled.")
            return 1
        return register_windows(args.time)
    else:
        if args.unregister:
            print("No automatic unregister on non-Windows. Edit your crontab to remove.")
            return 0
        print_cron(args.time)
        return 0


if __name__ == "__main__":
    sys.exit(main())
