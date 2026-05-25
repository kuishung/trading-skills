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

POLL_INTERVAL_SEC = 30        # how often to re-check ingest_log.jsonl
HEARTBEAT_INTERVAL_SEC = 1.0  # how fast the icon pulses when state == 'running'
RUNNING_THRESHOLD_SEC = 60    # last entry < 60s ago → actively writing
IDLE_THRESHOLD_SEC = 600      # last entry < 10 min → idle (between symbols)
                              # last entry > 10 min → stopped

# Milestone tracking — fire a Windows toast notification when these thresholds
# are crossed (only once per threshold, persisted in state/tray_milestone.json).
LOOKBACK_HOURS = 72           # window for counting "current run" symbols
COUNT_MILESTONES = [50, 100, 250, 500, 1000]   # symbols-done thresholds
MILESTONE_STATE_PATH = SKILL_DIR / "state" / "tray_milestone.json"


# ---- Progress + milestone tracking ----

def _load_milestone_state() -> dict:
    """Read which milestones have already been notified for the current run.
    File schema: {'counts_fired': [50, 100, ...], 'letters_done': ['A', 'B', ...]}.
    Returns empty state if file missing/corrupt."""
    if MILESTONE_STATE_PATH.exists():
        try:
            return json.loads(MILESTONE_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"counts_fired": [], "letters_done": []}


def _save_milestone_state(state: dict) -> None:
    try:
        MILESTONE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MILESTONE_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


RUN_GAP_THRESHOLD_SEC = 3600   # 1-hour gap = a different run; bound the count to "current run" only


def get_progress() -> dict:
    """Count symbols touched in the CURRENT run, identify the current letter,
    and compute approximate rate / ETA.

    "Current run" detection: read all ingest_log entries in the last
    LOOKBACK_HOURS, then find the most-recent gap > RUN_GAP_THRESHOLD_SEC
    (default 1 hour). Everything AFTER that gap counts as the current run;
    earlier entries are treated as a different previous run.

    This way a yesterday-14d-run + today-180d-rerun-after-restart doesn't
    double-count — the user sees only the current session's progress.

    Returns:
      symbols_done : int  — unique symbols touched in current run
      letters_done : list[str]  — first-letter groups fully past
      current_letter : str | None
      latest_symbol  : str | None
      rate_per_hour  : float | None
      eta_hours      : float | None
      run_started_at : str | None — ISO timestamp of the current run's first entry
    """
    EMPTY = {
        "symbols_done": 0, "letters_done": [],
        "current_letter": None, "latest_symbol": None,
        "rate_per_hour": None, "eta_hours": None,
        "run_started_at": None,
    }
    log_path = get_data_root() / "ingest_log.jsonl"
    if not log_path.exists():
        return EMPTY

    cutoff = datetime.now(timezone.utc).timestamp() - LOOKBACK_HOURS * 3600
    raw: list[tuple[datetime, str]] = []   # (ts, symbol) chronological
    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    ts = datetime.fromisoformat(e["ts"])
                    if ts.timestamp() < cutoff:
                        continue
                    raw.append((ts, e["symbol"]))
                except Exception:
                    continue
    except Exception:
        pass

    if not raw:
        return EMPTY

    # Find the latest run-boundary (gap > threshold) — walk backward from
    # the end, the first gap we hit is the boundary between this run and
    # whatever came before.
    run_start_idx = 0
    for i in range(len(raw) - 1, 0, -1):
        gap_sec = (raw[i][0] - raw[i - 1][0]).total_seconds()
        if gap_sec > RUN_GAP_THRESHOLD_SEC:
            run_start_idx = i
            break

    current_run = raw[run_start_idx:]

    # First-appearance order WITHIN the current run only
    seen: set[str] = set()
    syms_in_order: list[tuple[str, datetime]] = []
    for ts, sym in current_run:
        if sym not in seen:
            seen.add(sym)
            syms_in_order.append((sym, ts))

    if not syms_in_order:
        return EMPTY

    latest_symbol, latest_ts = syms_in_order[-1]
    current_letter = latest_symbol[0].upper() if latest_symbol else None
    letters_seen = sorted({s[0].upper() for s, _ in syms_in_order})
    letters_done = [L for L in letters_seen if current_letter and L < current_letter]

    first_ts = syms_in_order[0][1]
    elapsed_hours = (latest_ts - first_ts).total_seconds() / 3600
    rate_per_hour = (len(syms_in_order) / elapsed_hours) if elapsed_hours > 0.1 else None
    eta_hours = None
    if rate_per_hour and rate_per_hour > 0:
        try:
            import bars_store  # type: ignore
            target = len(bars_store.list_symbols("daily"))
        except Exception:
            target = 1519
        remaining = max(0, target - len(syms_in_order))
        eta_hours = remaining / rate_per_hour

    # Target denominator for the progress bar — universe size (daily parquet
    # count). Cached above for ETA; reuse here so the arc/tooltip have the
    # same N as the ETA math.
    try:
        import bars_store  # type: ignore
        target = len(bars_store.list_symbols("daily"))
    except Exception:
        target = 1519
    progress_fraction = (len(syms_in_order) / target) if target > 0 else 0.0
    progress_fraction = max(0.0, min(1.0, progress_fraction))

    return {
        "symbols_done": len(syms_in_order),
        "letters_done": letters_done,
        "current_letter": current_letter,
        "latest_symbol": latest_symbol,
        "rate_per_hour": rate_per_hour,
        "eta_hours": eta_hours,
        "run_started_at": first_ts.isoformat(),
        "target": target,
        "progress_fraction": progress_fraction,
    }


def _check_and_fire_milestones(icon, prog: dict) -> None:
    """Compare current progress against persisted milestone state.
    Fire ONE Windows notification per crossed milestone, persist what fired."""
    state = _load_milestone_state()
    fired_any = False

    # Count-based milestones
    counts_fired = set(state.get("counts_fired", []))
    for threshold in COUNT_MILESTONES:
        if prog["symbols_done"] >= threshold and threshold not in counts_fired:
            icon.notify(
                f"{prog['symbols_done']} symbols done — currently on "
                f"{prog['latest_symbol']} (letter {prog['current_letter']}).",
                title=f"Ingest milestone: {threshold} symbols",
            )
            counts_fired.add(threshold)
            fired_any = True
    state["counts_fired"] = sorted(counts_fired)

    # Letter-completion milestones
    letters_done_now = set(prog["letters_done"])
    letters_already_announced = set(state.get("letters_done", []))
    newly_done = letters_done_now - letters_already_announced
    for letter in sorted(newly_done):
        eta_msg = ""
        if prog.get("eta_hours") is not None:
            eta_msg = f" ETA {prog['eta_hours']:.0f}h."
        icon.notify(
            f"Letter {letter} done. Now on {prog['current_letter']}.{eta_msg}",
            title=f"Ingest milestone: letter {letter} complete",
        )
        fired_any = True
    state["letters_done"] = sorted(letters_done_now | letters_already_announced)

    if fired_any:
        _save_milestone_state(state)


def reset_milestone_state() -> None:
    """Clear persisted milestone state. Called via right-click menu when the
    user wants to re-announce milestones on the next thresholds crossed
    (e.g., after a manual rerun)."""
    try:
        if MILESTONE_STATE_PATH.exists():
            MILESTONE_STATE_PATH.unlink()
    except Exception:
        pass


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

        # Pull current-run progress (count + current letter) for the tooltip.
        # Cheap-ish — re-reads the same log we just scanned. Wrapped in try
        # so a glitch here doesn't break the basic status display.
        try:
            prog = get_progress()
            done = prog["symbols_done"]
            cur_letter = prog["current_letter"] or "?"
            n_letters_done = len(prog["letters_done"])
        except Exception:
            prog = None
            done = 0
            cur_letter = "?"
            n_letters_done = 0

        # Progress fraction for the tooltip — "X / N (Y%)" framing makes it
        # read as a progress bar rather than a raw counter.
        target = (prog or {}).get("target", 0)
        pct = int(100 * (prog or {}).get("progress_fraction", 0.0)) if prog else 0
        prog_str = f"{done}/{target} ({pct}%)" if target > 0 else f"{done} syms"

        # Tooltip — Windows truncates ~127 chars, keep compact
        if state == "running":
            tooltip = (
                f"{sym} | {prog_str} | letter {cur_letter} "
                f"({n_letters_done} done) | +{bars} bars"
            )
        elif state == "idle":
            tooltip = (
                f"{sym} | {prog_str} | letter {cur_letter} "
                f"| {age_sec/60:.0f}m idle"
            )
        else:
            tooltip = (
                f"{sym} | {prog_str} | stalled {age_sec/60:.0f}m"
            )
        if err:
            tooltip = f"ERR: {err[:50]}"

        return {
            "state": state,
            "msg": tooltip,
            "tooltip": tooltip,
            "last_symbol": sym,
            "last_ts": ts.isoformat(),
            "age_sec": age_sec,
            "bars_added": bars,
            "error": err,
            "progress": prog,
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

def _make_circle_icon(color: tuple, size: int = 64,
                      inner_dot_radius: int = 0,
                      progress: float = 0.0) -> Image.Image:
    """64×64 PNG circle in the given RGBA color. Black 2px outline.

    If `inner_dot_radius > 0`, draws a white inner dot of that radius — used
    for the heartbeat animation (cycling between small/large dot creates a
    visible pulse so the user can confirm the tray script itself is alive).

    If `progress > 0.0`, draws a white outer arc starting at 12 o'clock and
    sweeping clockwise — fills as a visible progress indicator. 0.5 = half
    ring, 1.0 = full ring. The arc sits just inside the black outline so it
    reads as a "status bar wrapped around the icon"."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 4
    draw.ellipse(
        [pad, pad, size - pad, size - pad],
        fill=color,
        outline=(0, 0, 0, 255),
        width=2,
    )
    # Progress arc — drawn ON TOP of the colored circle, just inside its
    # black outline. White for max contrast against any state color.
    if progress > 0.0:
        progress = max(0.0, min(1.0, progress))
        arc_inset = pad + 3   # 7px from edge = sits inside the outline ring
        # PIL angles: 0° = 3 o'clock, clockwise. We want 12 o'clock start,
        # so subtract 90°. End angle sweeps clockwise by `progress * 360`.
        start = -90
        end = -90 + (360 * progress)
        draw.arc(
            [arc_inset, arc_inset, size - arc_inset, size - arc_inset],
            start=start, end=end,
            fill=(255, 255, 255, 255),
            width=4,
        )
    if inner_dot_radius > 0:
        cx, cy = size // 2, size // 2
        r = inner_dot_radius
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(255, 255, 255, 230),
        )
    return img


# State colors (RGBA)
_GREEN  = (34, 197, 94, 255)    # green-500
_YELLOW = (250, 204, 21, 255)   # yellow-400
_RED    = (239, 68, 68, 255)    # red-500
_GRAY   = (156, 163, 175, 255)  # gray-400

_STATE_COLORS = {
    "running": _GREEN,
    "idle":    _YELLOW,
    "stopped": _RED,
    "unknown": _GRAY,
}


def _icon_for(state: str, frame_index: int, progress: float) -> Image.Image:
    """Build the tray icon for the current (state, heartbeat phase, progress).

    Three visible signals composed in one icon:
      - **Color** = ingest state (green/yellow/red/gray)
      - **Inner dot pulse** (running only) = tray script is alive (heartbeat)
      - **Outer arc** = % of universe symbols processed in the current run

    Why compose dynamically instead of pre-rendering: progress changes every
    poll, so we can't cache the full icon set. PIL renders a 64×64 image in
    ~1-2ms — cheap enough to redo on every heartbeat tick (1Hz) or status
    poll (every 30s for static states)."""
    color = _STATE_COLORS.get(state, _GRAY)
    inner_dot = 0
    if state == "running":
        # 2-phase heartbeat: small dot ↔ large dot
        inner_dot = 4 if (frame_index % 2 == 0) else 11
    return _make_circle_icon(color, inner_dot_radius=inner_dot, progress=progress)


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


def _on_reset_milestones(icon, item):
    """Clear which milestones have already fired. Useful after a manual run
    or when you want to re-test the toast notifications."""
    reset_milestone_state()
    icon.notify("Milestone history cleared — next thresholds will fire fresh.",
                title="Milestones reset")


def _on_quit(icon, item):
    icon.stop()


# ---- Main loop ----

def _update_loop(icon: "pystray.Icon"):
    """Background thread: poll status periodically, animate icon continuously.

    Two cadences in one loop:
      - Status poll every POLL_INTERVAL_SEC (~30s): re-reads ingest_log.jsonl,
        recomputes progress fraction
      - Heartbeat tick every HEARTBEAT_INTERVAL_SEC (~1s): advances frame
        index and redraws icon (heartbeat phase changes)

    The icon redraws each tick with the latest (state, frame_index, progress)
    so the outer progress arc reflects the most recent symbols_done/target
    ratio. Arc grows monotonically over the course of a run."""
    current_state = "unknown"
    current_progress = 0.0
    frame_index = 0
    last_poll = 0.0

    while True:
        try:
            now = time.monotonic()
            # Periodic status refresh (less frequent than animation)
            if now - last_poll >= POLL_INTERVAL_SEC or _force_refresh.is_set():
                status = get_ingest_status()
                new_state = status.get("state", "unknown")
                icon.title = f"Ingest: {new_state} — {status['tooltip']}"
                if new_state != current_state:
                    current_state = new_state
                    frame_index = 0   # reset animation phase on state change
                # Progress fraction drives the outer arc
                prog = status.get("progress")
                if prog:
                    current_progress = prog.get("progress_fraction", 0.0)
                # Fire milestone toast(s) if we just crossed a threshold
                if prog and prog.get("symbols_done", 0) > 0:
                    try:
                        _check_and_fire_milestones(icon, prog)
                    except Exception:
                        pass  # never let milestone errors kill the tray
                last_poll = now
                _force_refresh.clear()

            # Render current icon — incorporates state color, heartbeat phase,
            # AND progress arc in one composed image
            icon.icon = _icon_for(current_state, frame_index, current_progress)
            frame_index += 1

            # Static states sleep longer (no heartbeat to animate) — only
            # re-tick when the next status poll is due. Running state ticks
            # at the heartbeat cadence so the pulse stays visible.
            if current_state == "running":
                time.sleep(HEARTBEAT_INTERVAL_SEC)
            else:
                _force_refresh.wait(timeout=POLL_INTERVAL_SEC)
        except Exception as exc:
            icon.title = f"Ingest tray error: {exc}"
            time.sleep(POLL_INTERVAL_SEC)


def main() -> int:
    menu = pystray.Menu(
        pystray.MenuItem("Show Status", _on_show_status, default=True),
        pystray.MenuItem("Refresh Now", _on_refresh_now),
        pystray.MenuItem("Reset Milestones", _on_reset_milestones),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Log File", _on_open_log),
        pystray.MenuItem("Open Watcher Log", _on_open_watcher_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _on_quit),
    )

    icon = pystray.Icon(
        "ingest_status",
        _icon_for("unknown", 0, 0.0),
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
