"""System-tray icon showing ingest status.

Runs on either laptop or Hermes (same code; reads cfg.data_root). The icon
color tells you at-a-glance whether the ingest is actively writing, paused,
stopped, or unknown.

Color states:
  GREEN   actively writing      (last ingest_log entry within 60s)
  YELLOW  idle / between symbols (last entry 60s - 10 min ago)
  RED     stopped / stale       (last entry > 10 min ago, or no process)
  GRAY    unknown               (no log file, can't determine)

Right-click menu:
  - Show Status        : popup notification with current symbol + progress
  - Open Log File      : opens ingest_log.jsonl in default editor
  - Open Watcher Log   : opens the most recent _ingest_*.log
  - Refresh Now        : force immediate poll
  - Quit               : exit the tray app

Polls ingest_log.jsonl every 30s. No IBKR or network calls — just file reads.

Launch:
    py -3.12 dashboard/tray_status.py

To auto-start at Windows login:
    Create a shortcut to this script in:
    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\

Dependencies: pystray + Pillow (added to requirements.txt).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore
except ImportError as exc:
    sys.exit(
        f"Missing dependency: {exc}\n"
        "Install with: py -3.12 -m pip install pystray Pillow"
    )

from _common import get_data_root  # noqa: E402

# ---- Tuning ----

POLL_INTERVAL_SEC = 30
RUNNING_THRESHOLD_SEC = 60    # last entry < 60s ago → actively writing
IDLE_THRESHOLD_SEC = 600      # last entry < 10 min → idle (between symbols)
                              # last entry > 10 min → stopped


# ---- Status snapshot ----

def get_ingest_status() -> dict:
    """Read ingest_log.jsonl and return current ingest state."""
    log_path = get_data_root() / "ingest_log.jsonl"
    if not log_path.exists():
        return {
            "state": "unknown",
            "msg": "no ingest log",
            "tooltip": "no ingest log file yet",
        }

    try:
        last_line = None
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last_line = line
        if not last_line:
            return {
                "state": "unknown",
                "msg": "log empty",
                "tooltip": "ingest_log.jsonl is empty",
            }

        entry = json.loads(last_line)
        ts = datetime.fromisoformat(entry["ts"])
        age_sec = (datetime.now(timezone.utc) - ts).total_seconds()

        if age_sec < RUNNING_THRESHOLD_SEC:
            state = "running"
        elif age_sec < IDLE_THRESHOLD_SEC:
            state = "idle"
        else:
            state = "stopped"

        sym = entry.get("symbol", "?")
        bars = entry.get("bars_added", 0)
        err = entry.get("error", "")

        # Short tooltip (Windows truncates long ones)
        if state == "running":
            tooltip = f"{sym} (+{bars} bars, {age_sec:.0f}s ago)"
        elif state == "idle":
            tooltip = f"{sym} ({age_sec/60:.1f}min ago) — between symbols"
        else:
            tooltip = f"{sym} ({age_sec/60:.0f}min ago) — likely stopped"
        if err:
            tooltip = f"ERR: {err[:40]}"

        return {
            "state": state,
            "msg": tooltip,
            "tooltip": tooltip,
            "last_symbol": sym,
            "last_ts": ts.isoformat(),
            "age_sec": age_sec,
            "bars_added": bars,
            "error": err,
        }
    except Exception as exc:
        return {
            "state": "unknown",
            "msg": f"error: {exc}",
            "tooltip": f"status read failed: {exc}",
        }


def get_progress_summary() -> str:
    """Build a multi-line progress summary for the notification popup."""
    log_path = get_data_root() / "ingest_log.jsonl"
    if not log_path.exists():
        return "No ingest log yet."

    # Heuristic: count distinct symbols seen "today" (last 24h)
    cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
    syms = set()
    total_bars = 0
    last_entry = None
    n_writes = 0

    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    ts = datetime.fromisoformat(e["ts"])
                    if ts.timestamp() >= cutoff:
                        syms.add(e["symbol"])
                        total_bars += e.get("bars_added", 0)
                        n_writes += 1
                        last_entry = e
                except Exception:
                    continue
    except Exception as exc:
        return f"Couldn't read log: {exc}"

    if not last_entry:
        return "No ingest activity in the last 24 hours."

    ts = datetime.fromisoformat(last_entry["ts"])
    age = (datetime.now(timezone.utc) - ts).total_seconds()

    lines = [
        f"Last 24h:",
        f"  Symbols touched:  {len(syms)}",
        f"  Chunk writes:     {n_writes}",
        f"  Bars added:       {total_bars:,}",
        f"  Current symbol:   {last_entry.get('symbol', '?')}",
        f"  Last write:       {age:.0f}s ago",
    ]
    return "\n".join(lines)


# ---- Icon generation (no .ico files needed — drawn at startup) ----

def _make_circle_icon(color: tuple, size: int = 64) -> Image.Image:
    """64×64 PNG circle in the given RGBA color. Black 2px outline."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 4
    draw.ellipse(
        [pad, pad, size - pad, size - pad],
        fill=color,
        outline=(0, 0, 0, 255),
        width=2,
    )
    return img


ICONS = {
    "running": _make_circle_icon((34, 197, 94, 255)),    # green-500
    "idle":    _make_circle_icon((250, 204, 21, 255)),   # yellow-400
    "stopped": _make_circle_icon((239, 68, 68, 255)),    # red-500
    "unknown": _make_circle_icon((156, 163, 175, 255)),  # gray-400
}


# ---- Menu actions ----

def _on_show_status(icon, item):
    """Popup notification with the longer progress summary."""
    msg = get_progress_summary()
    # Windows truncates long notifications; trim if needed
    if len(msg) > 250:
        msg = msg[:247] + "..."
    icon.notify(msg, title="Ingest Status")


def _on_open_log(icon, item):
    """Open ingest_log.jsonl in the default app (usually Notepad on Win)."""
    log_path = get_data_root() / "ingest_log.jsonl"
    if log_path.exists():
        os.startfile(str(log_path))  # noqa: SIM115 (Windows-only)
    else:
        icon.notify(f"Log not found: {log_path}", title="Open Log Failed")


def _on_open_watcher_log(icon, item):
    """Open the most recent _ingest_*.log (watcher stdout)."""
    data_dir = get_data_root()
    candidates = sorted(
        data_dir.glob("_ingest_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        os.startfile(str(candidates[0]))
    else:
        icon.notify("No watcher log files found.", title="Open Log Failed")


_force_refresh = threading.Event()


def _on_refresh_now(icon, item):
    """Trigger an immediate poll cycle (vs waiting for next 30s tick)."""
    _force_refresh.set()


def _on_quit(icon, item):
    icon.stop()


# ---- Main loop ----

def _update_loop(icon: "pystray.Icon"):
    """Background thread: poll status, update icon + tooltip."""
    while True:
        try:
            status = get_ingest_status()
            icon.icon = ICONS.get(status["state"], ICONS["unknown"])
            icon.title = f"Ingest: {status['state']} — {status['tooltip']}"
        except Exception as exc:
            icon.title = f"Ingest tray error: {exc}"
        # Wait for either the poll interval OR a force-refresh signal
        _force_refresh.wait(timeout=POLL_INTERVAL_SEC)
        _force_refresh.clear()


def main() -> int:
    menu = pystray.Menu(
        pystray.MenuItem("Show Status", _on_show_status, default=True),
        pystray.MenuItem("Refresh Now", _on_refresh_now),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Log File", _on_open_log),
        pystray.MenuItem("Open Watcher Log", _on_open_watcher_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _on_quit),
    )

    icon = pystray.Icon(
        "ingest_status",
        ICONS["unknown"],
        title="Ingest status (loading...)",
        menu=menu,
    )

    # Background updater
    threading.Thread(target=_update_loop, args=(icon,), daemon=True).start()

    # Blocks until _on_quit is called
    icon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
