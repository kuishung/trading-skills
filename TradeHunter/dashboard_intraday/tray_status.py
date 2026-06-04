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
import re
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# --- TradeHunter bootstrap ---
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
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except ImportError as exc:
    sys.exit(
        f"Missing dependency: {exc}\n"
        "Install with: py -3.12 -m pip install pystray Pillow"
    )


def _load_bold_font(size_px: int):
    """Find a bold TrueType font for the percentage label.

    Tries Segoe UI Bold first (default on every modern Windows including
    Server 2019), then Arial Bold, falls back to PIL's bitmap font.
    The bitmap fallback won't scale, but at least the icon still renders."""
    for name in ("seguibd.ttf", "arialbd.ttf", "tahomabd.ttf"):
        try:
            return ImageFont.truetype(name, size_px)
        except Exception:
            continue
    return ImageFont.load_default()

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

def _load_milestone_state(iteration_key: str | None = None) -> dict:
    """Read which milestones have already been notified for the current
    iteration. State is keyed by `iteration_key` (the watcher log filename)
    so a supervisor restart -> new _ingest_*.log -> milestone state resets
    automatically and toasts re-fire as the new iteration crosses 50/100/...
    again.

    Schema on disk:
      {'iteration_key': '<filename>',
       'counts_fired': [50, 100, ...],
       'letters_done': ['A', 'B', ...]}

    If the on-disk key doesn't match the live iteration_key, treat as fresh
    state. Returns empty state if file missing/corrupt or key mismatch.
    """
    if MILESTONE_STATE_PATH.exists():
        try:
            state = json.loads(MILESTONE_STATE_PATH.read_text(encoding="utf-8"))
            if iteration_key is None or state.get("iteration_key") == iteration_key:
                return state
        except Exception:
            pass
    return {"iteration_key": iteration_key, "counts_fired": [], "letters_done": []}


def _save_milestone_state(state: dict) -> None:
    try:
        MILESTONE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MILESTONE_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


RUN_GAP_THRESHOLD_SEC = 3600   # legacy fallback: 1-hour gap = a different run.
                               # Only used when no _ingest_*.log is present to
                               # tell us the exact iteration boundary.

UNIVERSE_FALLBACK_PATH = SKILL_DIR / "resources" / "universe_full.txt"

# Filename format: _ingest_<tf_label>_<days>d_<YYYYMMDD>_<HHMMSS>.log
# (see scripts/wait_and_ingest.py — the watcher writes one of these per
# iteration, in local time per strftime).
_INGEST_LOG_TS_RE = re.compile(r'_(\d{8})_(\d{6})\.log$')


def _latest_iteration() -> tuple[str, datetime] | None:
    """Find the most-recent watcher iteration's identity + start time.

    Each iteration of `wait_and_ingest.py` creates a fresh `_ingest_*.log`
    file. We glob those, sort by the YYYYMMDD_HHMMSS embedded in the
    filename, return (filename, iteration_start_utc) for the latest.

    Why this matters: when the supervisor restarts (em-dash crash, OOM,
    Ctrl+C, etc.), the new iteration's progress should start from zero --
    NOT inherit the dead iteration's 90+ syms still sitting in the
    72-hour `ingest_log.jsonl` lookback window. The watcher log file is
    the cleanest signal of "this iteration began at T".

    Returns None if no _ingest_*.log files exist, in which case the
    caller falls back to the legacy 1-hour-gap rule.
    """
    try:
        candidates: list[tuple[str, str]] = []
        for p in get_data_root().glob("_ingest_*.log"):
            m = _INGEST_LOG_TS_RE.search(p.name)
            if m:
                candidates.append((m.group(1) + m.group(2), p.name))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        stamp, fname = candidates[0]
        # Filename strftime is local time per wait_and_ingest.py; convert to
        # UTC via Python's naive->aware->UTC chain.
        local_naive = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        local_aware = local_naive.astimezone()       # interpret as local tz
        utc_start = local_aware.astimezone(timezone.utc)
        return fname, utc_start
    except Exception:
        return None


def _target_universe_size() -> int:
    """Best estimate of the in-progress ingest's total universe size.

    Returns max(daily_parquet_count, universe_full_txt_line_count), so:
      * Fresh seed (daily=0..N still growing)         -> universe_full count wins
      * Post-complete (daily fully populated)         -> daily count wins
      * Partial-sync (daily=1 from Resilio crumb)     -> universe_full count wins
      * Both unavailable                              -> 1519 legacy floor

    Trade-off: a narrow `--universe journal` run (~50 syms) still gets the
    universe_full denominator, which under-reports % for that case. The
    proper fix is to have wait_and_ingest.py publish the exact target to
    state/ -- left for a follow-up if the narrow-universe view matters.
    """
    daily_n = 0
    try:
        import bars_store  # type: ignore
        daily_n = len(bars_store.list_symbols("daily"))
    except Exception:
        pass
    file_n = 0
    try:
        if UNIVERSE_FALLBACK_PATH.exists():
            file_n = sum(
                1 for line in UNIVERSE_FALLBACK_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            )
    except Exception:
        pass
    return max(daily_n, file_n) or 1519


# Pre-flight denominator parser. The watcher log file emits a line like
#     [pre-flight] 47 unique symbols need work
# at the top of every iteration when bulk_update runs with skip_up_to_date.
# That count is the right "X / N" denominator for this iteration — much
# better than the full universe size, which makes "1 / 1518 (0.1%)" look
# like nothing has happened even when the pre-flight skipped 1471 already-
# deep symbols.
#
# Matched against the watcher log inside its data_root, NOT the supervisor
# log (which doesn't capture the watcher's stdout since 2026-05-26's
# log_callback refactor routed it back through the watcher's own log()).
_PREFLIGHT_SYMS_RE = re.compile(r"\[pre-flight\]\s+(\d+)\s+unique symbols need work")
_preflight_cache: dict[str, tuple[float, int | None]] = {}


def _work_symbols_from_iteration_log(log_name: str) -> int | None:
    """Read the watcher log file `log_name` (basename, inside data_root)
    and return the pre-flight `unique symbols need work` count, or None
    if the line isn't present yet (older log format, or pre-flight still
    running).

    Cached by (path, mtime) — only re-parses when the file has actually
    changed. The pre-flight summary lands once at the very start of an
    iteration, so a cache hit after first parse is the common case.
    """
    if not log_name:
        return None
    try:
        log_path = get_data_root() / log_name
        if not log_path.exists():
            return None
        mtime = log_path.stat().st_mtime
    except OSError:
        return None
    cached = _preflight_cache.get(log_name)
    if cached and cached[0] == mtime:
        return cached[1]
    # Parse first ~80 lines — pre-flight summary lands within the first few
    # lines of a new iteration's log; cap is a defensive bound.
    n_work_symbols: int | None = None
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 80:
                    break
                m = _PREFLIGHT_SYMS_RE.search(line)
                if m:
                    n_work_symbols = int(m.group(1))
                    break
    except OSError:
        pass
    _preflight_cache[log_name] = (mtime, n_work_symbols)
    return n_work_symbols


def get_progress() -> dict:
    """Count symbols touched in the CURRENT iteration, identify the current
    letter, and compute approximate rate / ETA.

    Iteration boundary (preferred): the start timestamp of the latest
    `_ingest_*.log` file in data_root. Each call to wait_and_ingest.py
    creates a fresh one, so when the supervisor relaunches the watcher,
    the iteration boundary moves with it. Progress, rate, and ETA all
    reset cleanly per supervisor iteration.

    Iteration boundary (fallback): if no `_ingest_*.log` exists (e.g.,
    user ran ibkr_history.py directly without the supervisor), fall back
    to the legacy 1-hour-gap detection over a LOOKBACK_HOURS window.

    Returns:
      symbols_done   : int  — unique symbols touched in current iteration
      letters_done   : list[str]  — first-letter groups we've moved past
      current_letter : str | None
      latest_symbol  : str | None — actual chronologically-last ingest entry
      rate_per_hour  : float | None
      eta_hours      : float | None
      run_started_at : str | None — ISO UTC start of current iteration
      iteration_key  : str | None — filename of the watcher log (used to
                       key milestone state so restarts re-fire toasts)
      target         : int  — denominator (see _target_universe_size)
      progress_fraction : float in [0, 1]
      overshoot      : bool — True iff symbols_done > target (denominator
                       is wrong, UI shouldn't render 100% with confidence)
      last_write_at  : str | None — ISO UTC of chronologically-last ingest
                       entry, so the Tk window can show a live "Ns ago"
                       counter between refreshes
    """
    EMPTY = {
        "symbols_done": 0, "letters_done": [],
        "current_letter": None, "latest_symbol": None,
        "rate_per_hour": None, "eta_hours": None,
        "run_started_at": None, "iteration_key": None,
        "target": 0, "target_source": "universe",
        "progress_fraction": 0.0, "overshoot": False,
        "last_write_at": None,
    }
    log_path = get_data_root() / "ingest_log.jsonl"
    if not log_path.exists():
        return EMPTY

    iteration = _latest_iteration()
    if iteration is not None:
        iteration_key, iteration_start_utc = iteration
        cutoff = iteration_start_utc.timestamp()
    else:
        iteration_key = None
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

    # Bound to the current iteration:
    #   - watcher log found  -> raw is already filtered to entries since the
    #     iteration start; use it as-is
    #   - no watcher log     -> apply legacy 1-hour-gap rule as a fallback
    if iteration is None:
        run_start_idx = 0
        for i in range(len(raw) - 1, 0, -1):
            gap_sec = (raw[i][0] - raw[i - 1][0]).total_seconds()
            if gap_sec > RUN_GAP_THRESHOLD_SEC:
                run_start_idx = i
                break
        current_run = raw[run_start_idx:]
    else:
        current_run = raw

    # First-appearance order within the current iteration only
    seen: set[str] = set()
    syms_in_order: list[tuple[str, datetime]] = []
    for ts, sym in current_run:
        if sym not in seen:
            seen.add(sym)
            syms_in_order.append((sym, ts))

    if not syms_in_order:
        return EMPTY

    # Latest = chronologically-last ingest entry, NOT syms_in_order[-1]
    # (which would freeze whenever the latest entries are repeats of
    # already-seen symbols).
    latest_ts, latest_symbol = current_run[-1]
    current_letter = latest_symbol[0].upper() if latest_symbol else None
    letters_seen = sorted({s[0].upper() for s, _ in syms_in_order})
    letters_done = [L for L in letters_seen if current_letter and L < current_letter]

    first_ts = syms_in_order[0][1]
    elapsed_hours = (latest_ts - first_ts).total_seconds() / 3600
    rate_per_hour = (len(syms_in_order) / elapsed_hours) if elapsed_hours > 0.1 else None
    # Prefer the iteration's pre-flight work-symbol count as the denominator.
    # When skip_up_to_date is in effect (the watcher's default since
    # 2026-05-26), most symbols are skipped and the right denominator is
    # "unique symbols that still need a fetch", not the full universe size.
    # Falls back to the universe size when the pre-flight line isn't found
    # in the log (legacy iteration logs, or pre-flight still computing).
    target_source = "universe"
    target = None
    if iteration_key:
        n_work_syms = _work_symbols_from_iteration_log(iteration_key)
        if n_work_syms is not None and n_work_syms > 0:
            target = n_work_syms
            target_source = "pre-flight"
    if target is None:
        target = _target_universe_size()
    eta_hours = None
    if rate_per_hour and rate_per_hour > 0:
        remaining = max(0, target - len(syms_in_order))
        eta_hours = remaining / rate_per_hour

    raw_fraction = (len(syms_in_order) / target) if target > 0 else 0.0
    overshoot = len(syms_in_order) > target > 0
    progress_fraction = max(0.0, min(1.0, raw_fraction))

    return {
        "symbols_done": len(syms_in_order),
        "letters_done": letters_done,
        "current_letter": current_letter,
        "latest_symbol": latest_symbol,
        "rate_per_hour": rate_per_hour,
        "eta_hours": eta_hours,
        "run_started_at": first_ts.isoformat(),
        "iteration_key": iteration_key,
        "target": target,
        "target_source": target_source,
        "progress_fraction": progress_fraction,
        "overshoot": overshoot,
        "last_write_at": latest_ts.isoformat(),
    }


def _check_and_fire_milestones(icon, prog: dict) -> None:
    """Compare current progress against persisted milestone state.
    Fire ONE Windows notification per crossed milestone, persist what fired.

    Milestone state is keyed by iteration -- when the supervisor restarts
    and a new _ingest_*.log appears, prog['iteration_key'] changes,
    _load_milestone_state returns fresh state, and milestones re-fire as
    the new iteration crosses each threshold."""
    iteration_key = prog.get("iteration_key")
    state = _load_milestone_state(iteration_key)
    state["iteration_key"] = iteration_key   # persist the key even if no new fires
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


def get_deepcheck_status() -> dict:
    """Parse the latest `_deepcheck_<ts>.txt` integrity report in data_root.

    The supervisor (ingest_supervisor.py) writes one after every top-up. The
    tray MUST surface this (tray-sync rule, 2026-06-03): a clean audit is
    healthy; any corruption / empty / schema problem / Tier-2 `flagged files`
    is a critical data-quality alert the user needs to SEE at a glance.

    Returns: {status: clean|issues|partial|none|unknown, summary, ts,
              issues, corrupt, flagged, stale, age_min}.
    """
    import re
    try:
        root = get_data_root()
        reports = sorted(root.glob("_deepcheck_*.txt"))
    except Exception as exc:
        return {"status": "unknown", "summary": f"read failed: {exc}", "ts": None}
    if not reports:
        return {"status": "none", "summary": "no deep-check yet", "ts": None}
    latest = reports[-1]   # filename ts sorts chronologically
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"status": "unknown", "summary": f"read failed: {exc}", "ts": None}

    ts = None; age_min = None
    m = re.search(r"_deepcheck_(\d{8})_(\d{6})", latest.name)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            ts = dt.strftime("%m-%d %H:%M")
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        except ValueError:
            pass

    def _sum(pat):
        return sum(int(x) for x in re.findall(pat, text))
    corrupt = _sum(r"corrupt=(\d+)")
    empty   = _sum(r"empty=(\d+)")
    schema  = _sum(r"bad_schema=(\d+)")
    flagged = _sum(r"flagged files:\s*(\d+)")
    stale   = _sum(r"stale=(\d+)")
    issues = corrupt + empty + schema + flagged
    done = "# done" in text

    if not done:
        status, summary = "partial", "deep-check incomplete / running"
    elif issues > 0:
        status = "issues"
        summary = f"corrupt={corrupt} empty={empty} schema={schema} flagged={flagged}"
    else:
        status, summary = "clean", f"clean, stale={stale}"
    return {"status": status, "summary": summary, "ts": ts, "age_min": age_min,
            "issues": issues, "corrupt": corrupt, "flagged": flagged, "stale": stale}


def get_completed_through() -> dict:
    """Date through which the daily ingest has completed — shown in the progress
    window + tooltip so the user knows the data's currency at a glance.

    Primary source: the supervisor's last fully-topped-up session
    (`state/ingest_supervisor_state.json` -> last_success_session). Fallback
    (pre-supervisor): the most recent `last_bar` recorded in ingest_log.jsonl.
    Returns {date: 'YYYY-MM-DD'|None, source: supervisor|data|none}.
    """
    import json
    try:
        sp = Path(__file__).resolve().parent.parent / "state" / "ingest_supervisor_state.json"
        if sp.exists():
            s = json.loads(sp.read_text(encoding="utf-8")).get("last_success_session")
            if s:
                return {"date": str(s)[:10], "source": "supervisor"}
    except Exception:
        pass
    try:
        log = get_data_root() / "ingest_log.jsonl"
        last_line = None
        if log.exists():
            with log.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last_line = line
        if last_line:
            lb = json.loads(last_line).get("last_bar")
            if lb:
                return {"date": str(lb)[:10], "source": "data"}
    except Exception:
        pass
    return {"date": None, "source": "none"}


# ---- Icon generation (no .ico files needed — drawn at startup) ----

def _make_circle_icon(color: tuple, size: int = 64,
                      progress: float = 0.0,
                      heartbeat_phase: int = 0,
                      alert: bool = False) -> Image.Image:
    """64×64 PNG circle in the given RGBA color. Black 2px outline.

    Composes up to FOUR visible signals into one icon:

    - **Fill color** = ingest state (green/yellow/red/gray)
    - **Outer white arc** = progress fraction (0.0 → 1.0 fills the ring
      clockwise from 12 o'clock). Sits just inside the black outline.
    - **Centered percentage text** ("22%" / "100%") = numeric progress, drawn
      bold white with black outline for legibility on any background.
    - **Subtle fill brightening on heartbeat_phase=1** = proof-of-life pulse.
      Alternating phases create a visible breath; the colored circle is the
      pulse surface, since the percentage text replaced the old inner dot."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 4

    # Heartbeat pulse: lighten the fill ~12% on the "beat" phase.
    fill = color
    if heartbeat_phase > 0:
        r, g, b, a = color
        fill = (min(255, r + 30), min(255, g + 30), min(255, b + 30), a)

    # Alert outline: a thick RED ring when the latest deep check found data
    # integrity issues (corruption / empty / schema / Tier-2 flagged). This
    # rides ON TOP of the ingest-state fill colour so the user sees BOTH the
    # ingest state and a clear "data is bad" alarm at a glance (tray-sync rule).
    _outline = (239, 68, 68, 255) if alert else (0, 0, 0, 255)
    draw.ellipse(
        [pad, pad, size - pad, size - pad],
        fill=fill,
        outline=_outline,
        width=5 if alert else 2,
    )

    # Outer progress arc — sits inside the black outline so it reads as a
    # "ring around the icon". White for max contrast against any state color.
    if progress > 0.0:
        progress = max(0.0, min(1.0, progress))
        arc_inset = pad + 3
        # PIL angles: 0° = 3 o'clock, clockwise. We want 12 o'clock start.
        start = -90
        end = -90 + (360 * progress)
        draw.arc(
            [arc_inset, arc_inset, size - arc_inset, size - arc_inset],
            start=start, end=end,
            fill=(255, 255, 255, 255),
            width=4,
        )

    # Percentage text — only draw when progress rounds to >= 1%. Below that,
    # showing "0%" would be misleading (it's NOT zero, just very small).
    if progress >= 0.005:
        pct = int(round(progress * 100))
        text = f"{pct}%"
        # Auto-shrink font for 100% (4 chars) so it doesn't bleed off the
        # icon. 22px works for 1-99%; bumps down to 18px once we hit 100%.
        font = _load_bold_font(18 if pct >= 100 else 22)

        # Centre using textbbox (Pillow ≥ 10.0)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = (size - text_w) // 2 - bbox[0]
            y = (size - text_h) // 2 - bbox[1]
        except AttributeError:
            # Pillow < 10 fallback — coarse centering
            text_w, text_h = draw.textsize(text, font=font)  # type: ignore[attr-defined]
            x = (size - text_w) // 2
            y = (size - text_h) // 2

        # Black halo for legibility against any state color, then white fill
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

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


def _icon_for(state: str, frame_index: int, progress: float,
              alert: bool = False) -> Image.Image:
    """Build the tray icon for the current (state, heartbeat phase, progress).

    Four visible signals composed in one icon:
      - **Color** = ingest state (green/yellow/red/gray)
      - **Centered % text** = numeric progress (1% – 100%)
      - **Outer arc** = same progress as a visual gauge
      - **Background pulse** (running only) = tray script is alive (heartbeat)
        — the fill brightens slightly every other frame

    Why compose dynamically instead of pre-rendering: progress changes every
    poll, so we can't cache the full icon set. PIL renders a 64×64 image in
    ~1-2ms — cheap enough to redo on every heartbeat tick (1Hz) or status
    poll (every 30s for static states)."""
    color = _STATE_COLORS.get(state, _GRAY)
    # Background brightness pulse: only when running. Other states are static
    # — no point pulsing when nothing's being written.
    heartbeat_phase = (frame_index % 2) if state == "running" else 0
    return _make_circle_icon(color, progress=progress,
                             heartbeat_phase=heartbeat_phase, alert=alert)


# ---- Menu actions ----
#
# Architecture (2026-05-27, after the subprocess approach proved to kill the
# parent tray process on Windows):
#
#   - The Tk root + progress window are created ONCE by main() on the
#     interpreter's real main thread, then immediately withdrawn (hidden).
#   - pystray runs in a daemon thread (its Show Status / Quit callbacks
#     fire on that thread).
#   - Cross-thread communication uses a Queue + a periodic Tk-side poll
#     (root.after(100, ...)). Tk widgets are NEVER touched from the
#     pystray thread -- only the queue is.
#   - "Show Status" enqueues 'show'; the Tk poll deiconifies + lifts.
#   - "Close" / Esc / WM_DELETE withdraws (hides) the window, NOT
#     destroys -- so subsequent Show Status clicks reuse the same
#     persistent window.
#   - "Quit" enqueues 'quit'; the Tk poll calls icon.stop() then
#     root.quit() which ends mainloop and exits cleanly.
#
# Why this replaces the previous subprocess approach:
#   The previous code (3a7bcf2) spawned a fresh Python process per click
#   to dodge the "main thread is not in main loop" tkinter error. That
#   worked the first click but on Windows the spawn somehow caused the
#   PARENT tray process to die, so the tray icon disappeared after the
#   first window-open. Confirmed 2026-05-27 via process queries:
#   Get-CimInstance returned no python.exe with tray_status.py after the
#   first click. The proper fix is the Tk-on-main-thread pattern --
#   single process, persistent window, no spawn churn.

_command_queue: "queue.Queue[str]" = queue.Queue()


def _build_progress_window(root):
    """Build the progress-window widgets on `root` and wire up the periodic
    refresh + animation callbacks. The window starts hidden (root.withdraw
    is called by main BEFORE this builds); `_command_queue` 'show' commands
    deiconify it.

    Called ONCE at startup. The widgets, refresh_data + animate scheduling,
    and close-handler are all permanent for the process lifetime. Closing
    the window via the X / Close button / Escape calls root.withdraw() so
    the next Show Status click can re-show without rebuilding anything.
    """
    import tkinter as tk
    from tkinter import ttk

    win = root   # use the root window directly as the progress window
    win.title("Ingest Progress")
    win.geometry("420x330")
    win.resizable(False, False)
    win.attributes('-topmost', True)
    win.configure(bg='#1a1a1a')

    # Dark theme for ttk widgets (defaults look like Windows 95)
    style = ttk.Style(win)
    try:
        style.theme_use('clam')   # 'clam' respects custom colors better than 'vista'
    except Exception:
        pass
    style.configure(
        'Ingest.Horizontal.TProgressbar',
        background='#5fd97a',
        troughcolor='#262626',
        bordercolor='#1a1a1a',
        lightcolor='#5fd97a',
        darkcolor='#3fb55c',
        thickness=22,
    )

    # Big centered percentage
    pct_var = tk.StringVar(value='—')
    pct_lbl = tk.Label(
        win, textvariable=pct_var,
        font=('Segoe UI', 44, 'bold'),
        bg='#1a1a1a', fg='#5fd97a',
    )
    pct_lbl.pack(pady=(18, 6))

    # Visual progress bar
    pb_var = tk.DoubleVar(value=0)
    ttk.Progressbar(
        win, length=380, mode='determinate', maximum=100,
        variable=pb_var,
        style='Ingest.Horizontal.TProgressbar',
    ).pack(padx=20, pady=(0, 12))

    # "X / N symbols" line
    count_var = tk.StringVar(value='-')
    tk.Label(
        win, textvariable=count_var, font=('Segoe UI', 12, 'bold'),
        bg='#1a1a1a', fg='#cccccc',
    ).pack(pady=(0, 4))

    # "Data complete through: YYYY-MM-DD" — the ingest currency the user asked for
    through_var = tk.StringVar(value='Completed through: -')
    tk.Label(
        win, textvariable=through_var, font=('Segoe UI', 12, 'bold'),
        bg='#1a1a1a', fg='#5fd97a',
    ).pack(pady=(0, 4))

    # Latest deep-check / integrity result (tray-sync rule)
    deepcheck_var = tk.StringVar(value='Deep check: -')
    deepcheck_lbl = tk.Label(
        win, textvariable=deepcheck_var, font=('Segoe UI', 10),
        bg='#1a1a1a', fg='#888888',
    )
    deepcheck_lbl.pack(pady=(0, 4))

    # Live indicator: spinner glyph + colored status dot + "Ns ago" counter.
    # Updates every 150ms (animate()) independently of the 3s data refresh,
    # so the user sees continuous motion as long as the script is alive --
    # AND a green-vs-red dot for whether the watcher itself is alive.
    live_var = tk.StringVar(value='- waiting...')
    live_lbl = tk.Label(
        win, textvariable=live_var, font=('Consolas', 10),
        bg='#1a1a1a', fg='#888888',
    )
    live_lbl.pack(pady=(2, 4))

    # Current letter + latest symbol
    detail_var = tk.StringVar(value='-')
    tk.Label(
        win, textvariable=detail_var, font=('Segoe UI', 10),
        bg='#1a1a1a', fg='#888888',
    ).pack(pady=2)

    # Rate + ETA
    eta_var = tk.StringVar(value='-')
    tk.Label(
        win, textvariable=eta_var, font=('Segoe UI', 10),
        bg='#1a1a1a', fg='#888888',
    ).pack(pady=2)

    # Close button
    # HIDE (withdraw), don't DESTROY -- destroy would kill the Tk root which
    # holds the whole tray process together. Subsequent Show Status clicks
    # just deiconify this same persistent window.
    def close():
        try:
            win.withdraw()
        except Exception:
            pass

    tk.Button(
        win, text='Close', command=close,
        bg='#333333', fg='#cccccc',
        activebackground='#555555', activeforeground='#ffffff',
        relief='flat', font=('Segoe UI', 9),
        padx=14, pady=2, borderwidth=0,
    ).pack(pady=(10, 0))

    # Shared state between the slow data refresh and the fast animation tick.
    # Closure over a single dict so animate() can read fields that
    # refresh_data() writes (last_write_dt, state) without re-reading the log.
    live_state: dict = {
        "spinner_idx": 0,
        "last_write_dt": None,    # datetime | None — when the watcher last wrote
        "have_data": False,       # bool — have we ever populated yet?
    }

    # Braille spinner glyphs — 10 frames, animate by cycling the index every
    # 150ms. Renders fine in Consolas/Segoe UI on Windows 10+ / Server 2019.
    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def refresh_data():
        """Heavy refresh: re-reads ingest_log via get_progress(). Every 3s."""
        try:
            if not win.winfo_exists():
                return
        except Exception:
            return
        try:
            p = get_progress()
            pct = (p.get('progress_fraction') or 0.0) * 100
            target = p.get('target') or 0
            done = p.get('symbols_done', 0)
            overshoot = p.get('overshoot', False)
            if overshoot:
                # Denominator is wrong (numerator > target). Don't pretend
                # we're at 100% -- show "?" + amber tint so the user knows
                # the % is suspect.
                pct_var.set("?")
                pb_var.set(0)
                pct_lbl.configure(fg='#facc15')   # amber
            else:
                pct_var.set(f"{pct:.1f}%")
                pb_var.set(pct)
                pct_lbl.configure(fg='#888' if pct < 0.5 else '#5fd97a')
            # Label reflects the denominator source: "X / N to fetch" when
            # the pre-flight gave us the count of unique symbols that
            # actually need work (skip-up-to-date watcher), "X / N symbols"
            # when we're using the universe size as a fallback.
            tsrc = p.get('target_source') or 'universe'
            label = "to fetch" if tsrc == 'pre-flight' else "symbols"
            count_var.set(f"{done} / {target} {label}")
            cur_letter = p.get('current_letter') or '?'
            n_done = len(p.get('letters_done') or [])
            latest = p.get('latest_symbol') or '-'
            detail_var.set(
                f"Letter {cur_letter} ({n_done} groups past)   -   Latest: {latest}"
            )
            eta_h = p.get('eta_hours')
            rate = p.get('rate_per_hour')
            if eta_h is not None and rate:
                eta_var.set(
                    f"ETA: {eta_h:.1f}h ({eta_h/24:.1f}d)   -   "
                    f"Rate: {rate:.0f} syms/hr"
                )
            else:
                eta_var.set('ETA: gathering data...')

            # Cache last-write timestamp for the live indicator
            last_iso = p.get('last_write_at')
            if last_iso:
                try:
                    live_state['last_write_dt'] = datetime.fromisoformat(last_iso)
                except Exception:
                    pass
            live_state['have_data'] = done > 0

            # "Completed through" date (ingest currency)
            try:
                ct = get_completed_through()
                if ct.get('date'):
                    through_var.set(f"Completed through: {ct['date']}  ({ct['source']})")
                else:
                    through_var.set("Completed through: (no data yet)")
            except Exception:
                through_var.set("Completed through: -")

            # Deep-check result line (clean / issues / partial)
            try:
                dc = get_deepcheck_status()
                st = dc.get('status', '?')
                ts = f"  {dc['ts']}" if dc.get('ts') else ""
                deepcheck_var.set(f"Deep check: {st.upper()} - {dc.get('summary','')}{ts}")
                deepcheck_lbl.configure(fg='#ef4444' if st == 'issues'
                                        else ('#5fd97a' if st == 'clean' else '#888888'))
            except Exception:
                deepcheck_var.set("Deep check: -")
        except Exception as exc:
            pct_var.set('error')
            count_var.set(str(exc)[:60])

        try:
            win.after(3000, refresh_data)
        except Exception:
            pass

    def animate():
        """Light tick: advances spinner + recomputes 'Ns ago' from cached
        last_write_dt. Every 150ms. No file I/O."""
        try:
            if not win.winfo_exists():
                return
        except Exception:
            return
        try:
            i = live_state['spinner_idx'] % len(SPINNER)
            glyph = SPINNER[i]
            live_state['spinner_idx'] = i + 1

            last_dt = live_state['last_write_dt']
            if last_dt is None:
                if live_state['have_data']:
                    live_var.set(f"{glyph}  waiting on next ingest entry...")
                    live_lbl.configure(fg='#888888')
                else:
                    live_var.set(f"{glyph}  waiting for first ingest entry...")
                    live_lbl.configure(fg='#888888')
            else:
                age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if age < 0:
                    age = 0
                # Color the dot by liveness: matches the tray-icon thresholds
                # in get_ingest_status() for consistency
                if age < RUNNING_THRESHOLD_SEC:
                    dot, dot_fg, state_word = '●', '#5fd97a', 'live'  # green
                elif age < IDLE_THRESHOLD_SEC:
                    dot, dot_fg, state_word = '●', '#facc15', 'idle'  # amber
                else:
                    dot, dot_fg, state_word = '●', '#ef4444', 'stalled'  # red
                if age < 60:
                    age_str = f"{age:.0f}s ago"
                elif age < 3600:
                    age_str = f"{age/60:.0f}m ago"
                else:
                    age_str = f"{age/3600:.1f}h ago"
                live_var.set(f"{glyph}  {dot} {state_word}  -  last write {age_str}")
                live_lbl.configure(fg=dot_fg)
        except Exception:
            pass

        try:
            win.after(150, animate)
        except Exception:
            pass

    refresh_data()
    animate()
    win.bind('<Escape>', lambda e: close())
    # WM_DELETE_WINDOW = the X button. Hide instead of destroy (see close()).
    win.protocol("WM_DELETE_WINDOW", close)
    # NB: mainloop is started by main() on the root, not here. _build is
    # purely setup; the actual event loop runs once for the whole process.


def _on_show_status(icon, item):
    """Default tray action (left-click) -- enqueue a 'show' command.

    Called from pystray's daemon thread. We MUST NOT touch Tk widgets
    from this thread (see the architecture comment at the top of the
    'Menu actions' section). The main thread polls _command_queue every
    100ms via root.after(...) and handles the actual deiconify.
    """
    _command_queue.put('show')


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
    """Enqueue 'quit'; main thread will call icon.stop() + root.quit()."""
    _command_queue.put('quit')


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
    current_alert = False
    frame_index = 0
    last_poll = 0.0

    while True:
        try:
            now = time.monotonic()
            # Periodic status refresh (less frequent than animation)
            if now - last_poll >= POLL_INTERVAL_SEC or _force_refresh.is_set():
                status = get_ingest_status()
                new_state = status.get("state", "unknown")
                # Deep-check result — surfaced on the tray per the tray-sync rule.
                # A red alert ring + tooltip line whenever the latest integrity
                # audit found issues (corruption / schema / Tier-2 flagged).
                try:
                    dc = get_deepcheck_status()
                except Exception:
                    dc = {"status": "unknown", "summary": "deep-check read error", "ts": None}
                current_alert = (dc.get("status") == "issues")
                dc_ts = f" {dc['ts']}" if dc.get("ts") else ""
                dc_str = f"deepcheck: {dc.get('status', '?').upper()} ({dc.get('summary', '')}){dc_ts}"
                try:
                    ct = get_completed_through()
                    through_str = f"through {ct['date']}" if ct.get("date") else "through -"
                except Exception:
                    through_str = "through -"
                icon.title = f"Ingest: {new_state} — {status['tooltip']} | {through_str} | {dc_str}"
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
            icon.icon = _icon_for(current_state, frame_index, current_progress,
                                  alert=current_alert)
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
    # ---- Tk root on the actual main thread ----
    # tkinter REQUIRES its operations to happen on the interpreter's main
    # thread (this is exactly the constraint that broke the previous
    # subprocess-based attempt). The root is created here, the progress
    # window is built on it, then it's withdrawn (hidden) until the user
    # clicks Show Status.
    try:
        import tkinter as tk
    except ImportError:
        sys.stderr.write("[tray_status] tkinter not available; cannot show progress window.\n")
        return 1

    root = tk.Tk()
    root.withdraw()   # start hidden — Show Status click will deiconify
    try:
        _build_progress_window(root)
    except Exception as exc:
        import traceback
        sys.stderr.write(
            f"[tray_status] _build_progress_window failed: "
            f"{type(exc).__name__}: {exc}\n"
        )
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return 1

    # ---- pystray on a daemon thread ----
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

    # Icon + update loop both go on daemon threads so they don't block the
    # Tk mainloop on the main thread.
    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=_update_loop, args=(icon,), daemon=True).start()

    # ---- Cross-thread command poll ----
    # pystray callbacks (Show Status / Quit) put strings here; the Tk-side
    # poller reads + acts. 100ms latency between click and window-show is
    # imperceptible.
    def process_commands():
        try:
            while True:
                cmd = _command_queue.get_nowait()
                if cmd == 'show':
                    try:
                        root.deiconify()
                        root.lift()
                        # Windows anti-focus-stealing: re-assert topmost
                        # briefly so the window actually pops to front,
                        # then leave it set (the window's permanent
                        # behaviour is topmost anyway).
                        root.attributes('-topmost', True)
                        root.focus_force()
                    except Exception as exc:
                        sys.stderr.write(
                            f"[tray_status] deiconify failed: {type(exc).__name__}: {exc}\n"
                        )
                elif cmd == 'quit':
                    try:
                        icon.stop()
                    except Exception:
                        pass
                    root.quit()
                    return   # stop polling — mainloop is about to exit
        except queue.Empty:
            pass
        root.after(100, process_commands)

    root.after(100, process_commands)

    # ---- Run Tk mainloop on the main thread (blocks until root.quit()) ----
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            icon.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
