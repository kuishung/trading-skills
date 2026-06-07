"""ingest_supervisor.py — autonomous daily post-close top-up + deep check (Hermes).

Single always-on service that OWNS the Gateway + ingest lifecycle, replacing the
old `IntradayBot-Gateway` keep-alive and `Hermes-IBC-Start-PostMarket` tasks.

ALL timing is computed in America/New_York (ET) via zoneinfo, so US DST never
breaks the schedule even though Hermes runs on MYT (UTC+8). A single boot-time
Task Scheduler job just launches this; this process decides *when* to act.

Daily cycle (per the user's spec, 2026-06-03):
  - RUN WINDOW: 20:10 ET (10 min after the 20:00 ET *extended* close) until
    08:00 ET (90 min before the 09:30 ET open).
  - On a weekday evening, ONCE per session:
        open Gateway -> top up the day's new bars (retry on crash/stall until
        success OR the 08:00 ET deadline) -> run a FULL deep check + write a
        timestamped report -> close Gateway.
    Then idle (Gateway OFF) until the next window. No overnight busy-loop —
    the market is closed, so there is nothing new to fetch.
  - WEEKDAY BLACKOUT: 08:00 ET -> 20:10 ET, Mon–Fri only. Gateway is forced OFF
    so it can NEVER collide with the user's manual IBKR trading (which begins
    90 min before the open).
  - WEEKEND SEEDING (set 2026-06-07): across the market-closed span Sat 00:00 ET
    -> Mon 08:00 ET the Gateway is kept ON (auto-revived) so a seed/backfill can
    run uninterrupted — no daytime blackout, because there's no manual trading to
    protect. The Monday 08:00 ET blackout resumes for the trading week. The seed
    itself is run on demand (tray "Run ingest" / a seed job); the supervisor's job
    here is to keep the Gateway available.

The "retry / auto-revive" requirement is satisfied as *retry-until-success*:
if the single nightly top-up crashes or the Gateway drops mid-fetch, the loop
re-runs it — but only while still inside the window, and it hard-stops + shuts
the Gateway at the 08:00 ET deadline no matter what.

Modes:
    (default)         run the real supervisor loop (Hermes)
    --self-test       assert the pure timing/window/session logic (no side effects)
    --dry-run         run the loop with MOCKED gateway/ingest/deepcheck so the
                      orchestration can be exercised on a laptop with no IBKR.
                      Combine with --fake-start "YYYY-MM-DD HH:MM" + --fake-step
                      + --fake-ticks to simulate a stretch of time in seconds.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time as _time
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --- bootstrap ---
_root = Path(__file__).resolve().parent.parent
for _p in [str(_root), str(_root / "scripts"), str(_root / "resources")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
SKILL_DIR = _root

ET = ZoneInfo("America/New_York")

# ---- Schedule (ET wall-clock) ----
MARKET_OPEN          = time(9, 30)
EXTENDED_CLOSE       = time(20, 0)
FETCH_AFTER_CLOSE_MIN = 10     # fetch 10 min after extended close -> 20:10 ET
STOP_BEFORE_OPEN_MIN  = 90     # stop 90 min before open          -> 08:00 ET

def _add_min(t: time, mins: int) -> time:
    return (datetime(2000, 1, 1, t.hour, t.minute) + timedelta(minutes=mins)).time()

RUN_START = _add_min(EXTENDED_CLOSE, FETCH_AFTER_CLOSE_MIN)   # 20:10 ET
RUN_END   = _add_min(MARKET_OPEN, -STOP_BEFORE_OPEN_MIN)      # 08:00 ET

GATEWAY_PORT = 4002
TICK_SEC = 60          # supervisor poll cadence in real mode
GATEWAY_UP_TIMEOUT_SEC = 180
GATEWAY_LOGIN_GRACE_SEC = 90   # wait this long for an in-flight (re)login before
                               # treating an alive-but-portless Gateway as a stale
                               # logged-out session (the daily auto-logout) and
                               # force-restarting it.
DEADLINE_MARGIN_MIN = 3   # stop heavy work + shut Gateway this many min BEFORE
                          # RUN_END (08:00 ET) so the Gateway is provably down
                          # before the user's manual-trade window begins.
TOPUP_TIMEFRAMES = "3min:180,5min:180,daily:730"
SYMBOLS_FILE = "resources/universe_full.txt"

# Absolute py launcher — bare "py" may not resolve under the Task Scheduler S4U
# session's PATH. C:\Windows\py.exe is the standard launcher location.
PY = shutil.which("py") or r"C:\Windows\py.exe"


# ======================================================================
# PURE TIMING LOGIC  (no I/O — this is what --self-test exercises)
# ======================================================================

def in_run_window(t: time) -> bool:
    """True if ET wall-clock `t` is inside the nightly run window
    (20:10 ET .. 08:00 ET, spanning midnight)."""
    return t >= RUN_START or t < RUN_END


def is_blackout(t: time) -> bool:
    """True during 08:00 ET .. 20:10 ET — Gateway must be OFF (manual-trade safe)."""
    return not in_run_window(t)


def session_date(now_et: datetime) -> date | None:
    """The trading session this moment belongs to, or None if in blackout.
       evening (>=20:10) -> today; early morning (<08:00) -> yesterday."""
    t = now_et.time()
    if t >= RUN_START:
        return now_et.date()
    if t < RUN_END:
        return (now_et - timedelta(days=1)).date()
    return None


def is_trading_session(d: date | None) -> bool:
    """Mon-Fri only (no equity session on weekends; holidays are a harmless no-op)."""
    return d is not None and d.weekday() < 5


def should_fetch(now_et: datetime, last_success_session: date | None) -> bool:
    """Initiate / continue a fetch iff: in window, a weekday session, and we
    haven't already succeeded for THAT session."""
    sess = session_date(now_et)
    if not is_trading_session(sess):
        return False
    return last_success_session != sess


def deadline_for(now_et: datetime) -> datetime:
    """The WORKING deadline for the current window = RUN_END (08:00 ET) minus a
    shutdown margin, so heavy work stops and the Gateway is down BEFORE 08:00.
    Morning leg (<08:00) -> today; evening (>=20:10) -> tomorrow."""
    if now_et.time() < RUN_END:
        d = now_et.date()
    else:
        d = now_et.date() + timedelta(days=1)
    return (datetime.combine(d, RUN_END, tzinfo=ET)
            - timedelta(minutes=DEADLINE_MARGIN_MIN))


def is_weekend_seeding(now_et: datetime) -> bool:
    """True across the continuous market-closed weekend span — **Saturday 00:00
    ET through Monday 08:00 ET (RUN_END)**. During this span the Gateway is kept
    UP for seeding/backfill instead of being forced off, because there is no
    manual trading to protect (set 2026-06-07 per user: "Gateway should be on for
    seeding in Hermes during weekends").

    Friday evening still runs the normal Friday-session top-up (NOT seeding);
    Monday 08:00 ET onward the weekday blackout resumes so the Gateway is provably
    down before the trading week."""
    wd = now_et.weekday()                       # Mon=0 .. Sun=6
    if wd in (5, 6):                            # all of Saturday and Sunday
        return True
    if wd == 0 and now_et.time() < RUN_END:     # Monday early morning (<08:00 ET)
        return True
    return False


# ======================================================================
# SIDE EFFECTS  (overridden by a mock in --dry-run)
# ======================================================================

class RealEffects:
    """Real Gateway / ingest / deep-check actions on Hermes."""

    def __init__(self, log):
        self.log = log

    # ---- Gateway ----
    def gateway_is_up(self) -> bool:
        # Cache ~8s: a single tick calls this several times (and the heartbeat
        # reads it too) — caching collapses those to ONE port check, cutting the
        # Gateway-log "client disconnected" probe noise.
        now = _time.monotonic()
        cached = getattr(self, "_gw_up_cache", None)
        if cached is not None and (now - cached[0]) < 8.0:
            return cached[1]
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {GATEWAY_PORT} -State Listen "
                 f"-ErrorAction SilentlyContinue | Measure-Object).Count"],
                capture_output=True, text=True, timeout=20)
            val = out.stdout.strip().isdigit() and int(out.stdout.strip()) > 0
        except Exception as exc:
            self.log(f"gateway_is_up check failed: {exc}")
            val = False
        self._gw_up_cache = (now, val)
        return val

    def gateway_up(self) -> bool:
        if self.gateway_is_up():
            return True
        # A process may already be alive: EITHER a clean (re)login in progress
        # (login takes ~30-60s before the port listens — don't launch a second
        # IBC, two sessions on one account get one kicked), OR a STALE session
        # left logged-out by the daily IBKR auto-logout (process alive, port
        # never comes up). Give an in-flight login a brief grace; if the port
        # still isn't up, treat it as stale -> kill it + relaunch fresh. This is
        # the everyday resurrection (esp. with auto-logout set to 16:00 ET) and
        # also fixes the "did NOT come up within 180s" relaunch loop.
        if self._gateway_proc_alive():
            self.log(f"IBC/Gateway process alive — waiting up to {GATEWAY_LOGIN_GRACE_SEC}s for login")
            if self._wait_for_port(GATEWAY_LOGIN_GRACE_SEC):
                self.log("Gateway login completed")
                return True
            self.log("login did not complete — stale/logged-out session; "
                     "killing it and relaunching (resurrection)")
            self.gateway_down()
        bat = SKILL_DIR / "ibc" / "StartIBC-intraday.bat"
        self.log(f"starting Gateway via {bat}")
        try:
            subprocess.Popen(["cmd.exe", "/c", str(bat)],
                             cwd=str(bat.parent),
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except Exception as exc:
            self.log(f"Gateway launch failed: {exc}")
            return False
        if self._wait_for_port(GATEWAY_UP_TIMEOUT_SEC):
            self.log("Gateway up")
            return True
        self.log(f"Gateway did NOT come up within {GATEWAY_UP_TIMEOUT_SEC}s")
        return False

    def _wait_for_port(self, timeout_s: int) -> bool:
        """Poll the Gateway port until it accepts connections or timeout."""
        waited = 0
        while waited < timeout_s:
            _time.sleep(10); waited += 10
            if self.gateway_is_up():
                return True
        return False

    def gateway_down(self) -> None:
        # Hang-proof + reliable. The earlier version let ibc/Stop.bat block for
        # 60s (IBC's STOP command stalls when IBC isn't tracking this Gateway —
        # e.g. it was started by an old/other IBC) and left the force-kill
        # unguarded; on Hermes 2026-06-04 this stalled the whole supervisor for
        # ~2 days. Now: SHORT Stop.bat budget, force-kill is the reliable path
        # and is guarded, and the whole routine is bounded (~60s worst case).
        if not self.gateway_is_up() and not self._gateway_proc_alive():
            return
        stop = SKILL_DIR / "ibc" / "Stop.bat"
        self.log("stopping Gateway (ibc/Stop.bat, 15s budget)")
        try:
            subprocess.run(["cmd.exe", "/c", str(stop)], cwd=str(stop.parent),
                           timeout=15)
        except Exception as exc:
            self.log(f"Stop.bat did not finish ({type(exc).__name__}) — going to force-kill")
        waited = 0
        while waited < 15 and self.gateway_is_up():
            _time.sleep(5); waited += 5
        if self.gateway_is_up() or self._gateway_proc_alive():
            self.log("force-killing ibgateway*/java(IBC)")
            try:
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_Process | "
                                "Where-Object { $_.Name -match 'ibgateway' -or "
                                "($_.Name -match 'java' -and $_.CommandLine -match 'IBC') } | "
                                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                                "-ErrorAction SilentlyContinue }"],
                               timeout=30)
            except Exception as exc:
                self.log(f"force-kill subprocess error: {exc}")
        if self.gateway_is_up():
            self.log("WARNING: Gateway STILL listening after Stop.bat + force-kill")

    def _gateway_proc_alive(self) -> bool:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
                 "'ibgateway' -or ($_.Name -match 'java' -and $_.CommandLine -match "
                 "'IBC') } | Measure-Object).Count"],
                capture_output=True, text=True, timeout=20)
            return out.stdout.strip().isdigit() and int(out.stdout.strip()) > 0
        except Exception:
            return False

    # ---- Ingest ----
    def run_topup(self, deadline: datetime, session) -> bool:
        """Launch the full-universe incremental top-up; monitor; kill at the
        08:00 ET deadline. Returns True on a clean exit before the deadline.
        `--fresh-through <session>` makes a retry skip already-fetched symbols."""
        cmd = [PY, "-3.12", str(SKILL_DIR / "scripts" / "wait_and_ingest.py"),
               "--symbols-file", str(SKILL_DIR / SYMBOLS_FILE),
               "--timeframes", TOPUP_TIMEFRAMES, "--topup",
               "--fresh-through", session.isoformat()]
        self.log(f"topup start (deadline {deadline.isoformat()}): {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, cwd=str(SKILL_DIR))
        except Exception as exc:
            self.log(f"topup launch failed: {exc}")
            return False
        while True:
            try:
                proc.wait(timeout=30)
                ok = (proc.returncode == 0)
                self.log(f"topup exited rc={proc.returncode}")
                return ok
            except subprocess.TimeoutExpired:
                if et_now() >= deadline:
                    self.log("DEADLINE (08:00 ET) reached mid-topup — terminating")
                    proc.terminate()
                    try: proc.wait(timeout=30)
                    except subprocess.TimeoutExpired: proc.kill()
                    return False

    def run_deep_check(self) -> dict:
        """Run the full deep check, write the report, and return a parsed
        summary {status, corrupt, flagged, stale, report}."""
        import re
        from _common import get_data_root
        ts = et_now().strftime("%Y%m%d_%H%M%S")
        report = get_data_root() / f"_deepcheck_{ts}.txt"
        self.log(f"deep check -> {report}")
        try:
            with report.open("w", encoding="utf-8") as fh:
                subprocess.run([PY, "-3.12",
                                str(SKILL_DIR / "scripts" / "check_bars_integrity.py"),
                                "--deep"], cwd=str(SKILL_DIR), stdout=fh, timeout=3600)
        except Exception as exc:
            self.log(f"deep check failed: {exc}")
            return {"status": "error", "report": report.name, "error": str(exc)[:120]}
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return {"status": "unknown", "report": report.name}
        def _s(p): return sum(int(x) for x in re.findall(p, text))
        corrupt = _s(r"corrupt=(\d+)") + _s(r"empty=(\d+)") + _s(r"bad_schema=(\d+)")
        flagged = _s(r"flagged files:\s*(\d+)")
        stale = _s(r"stale=(\d+)")
        status = ("issues" if (corrupt + flagged) > 0
                  else ("clean" if "# done" in text else "partial"))
        return {"status": status, "corrupt": corrupt, "flagged": flagged,
                "stale": stale, "report": report.name}

    def run_regen(self) -> dict:
        """Nightly profile regen from the parquet (local, no Gateway). Runs BOTH
        the intraday profile (consumed by dashboard_intraday/GUNS) and the
        swing/trend profile (consumed by dashboard_tst/MATP) — both are cheap and
        each dashboard surfaces its own. Returns the per-kind phases so the
        manifest carries profiles_intraday + profiles_swing."""
        self.log("regen profiles (intraday + swing)")
        try:
            sys.path.insert(0, str(SKILL_DIR / "scripts"))
            import regen_profiles  # noqa
            res = regen_profiles.regen("both", None, log=self.log)
            phases = res.get("phases", {})
            return {"status": "ok", "phases": phases, "duration_s": res.get("duration_s")}
        except Exception as exc:
            self.log(f"regen failed: {exc}")
            return {"status": "error", "error": str(exc)[:120]}

    def write_run_manifest(self, session, deep: dict, regen: dict) -> None:
        """Write the per-run pipeline manifest the dashboard_tst /pipeline page
        reads: ingest (parsed from the latest _ingest log) -> deep-check ->
        profiles, with status + metrics + log pointers."""
        import json
        from _common import get_data_root
        root = get_data_root()
        # ingest summary from the newest _ingest_*.log
        ingest = {"status": "unknown"}
        try:
            logs = sorted(root.glob("_ingest_*.log"), key=lambda p: p.stat().st_mtime)
            if logs:
                txt = logs[-1].read_text(encoding="utf-8", errors="replace")
                import re
                m = re.search(r"DONE:\s*(\d+)\s*bars written across\s*(\d+)", txt)
                w = re.search(r"(\d+)\s+unique symbols need work", txt)
                ingest = {"status": "ok" if "DONE:" in txt else "partial",
                          "bars": int(m.group(1)) if m else None,
                          "pairs": int(m.group(2)) if m else None,
                          "symbols_needed": int(w.group(1)) if w else None,
                          "log": logs[-1].name}
        except Exception as exc:
            ingest = {"status": "error", "error": str(exc)[:120]}
        overall = "success"
        if ingest.get("status") not in ("ok",) or deep.get("status") in ("issues", "error") \
                or regen.get("status") == "error":
            overall = "partial" if ingest.get("status") == "ok" else "failed"
        # Expand the per-kind profile phases (profiles_intraday + profiles_swing)
        # so each dashboard sees its own; fall back to a single "profiles" phase
        # if the regen errored before producing per-kind results.
        phases = {"ingest": ingest, "deepcheck": deep}
        rphases = regen.get("phases") if isinstance(regen, dict) else None
        if rphases:
            for k, v in rphases.items():
                phases[f"profiles_{k}"] = v
        else:
            phases["profiles"] = regen
        manifest = {
            "run_id": f"{session}_{et_now():%H%M%S}",
            "session": str(session),
            "finished_at": et_now().isoformat(),
            "overall": overall,
            "phases": phases,
        }
        d = root / "pipeline_runs"; d.mkdir(parents=True, exist_ok=True)
        (d / f"run_{session}_{et_now():%H%M%S}.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        self.log(f"manifest written: overall={overall}")

    def report_freshness(self) -> None:
        """Push per-timeframe DATA freshness (newest-bar epoch) to the
        dashboard's /api/ingest/health so the Data Ingest page shows how fresh
        the seeded data is — not just the file write time. Soft-fail: a
        reporting hiccup must never affect the ingest run itself."""
        try:
            sys.path.insert(0, str(SKILL_DIR / "scripts"))
            import report_ingest_health as rep  # noqa
            report = rep.build_report()
            key = rep._resolve_key(None)
            if not key:
                self.log("freshness: no API key — skipped")
                return
            import json as _json
            import urllib.request as _u
            url = os.environ.get("TST_DASHBOARD_URL", "http://localhost:8000").rstrip("/") \
                + "/api/ingest/health"
            req = _u.Request(url, data=_json.dumps(report).encode("utf-8"), method="POST",
                             headers={"Content-Type": "application/json", "X-API-Key": key})
            with _u.urlopen(req, timeout=20) as resp:
                self.log(f"freshness pushed -> {resp.status}")
        except Exception as exc:
            self.log(f"freshness push failed: {exc}")


# ======================================================================
# CLOCK  (injectable for tests)
# ======================================================================
_FAKE = {"now": None, "step": timedelta(minutes=5)}
_PERSIST_STATE = True   # set False in --self-test/--dry-run so tests don't write real state

def et_now() -> datetime:
    if _FAKE["now"] is not None:
        return _FAKE["now"]
    return datetime.now(timezone.utc).astimezone(ET)


# ======================================================================
# SUPERVISOR LOOP
# ======================================================================

def supervisor_tick(state: dict, fx, log) -> str:
    """One decision step. Reads the clock via et_now() (so the deadline check
    after a long blocking top-up uses CURRENT time, and tests stay deterministic
    via _FAKE). Returns an action label. All I/O goes through `fx`."""
    now_et = et_now()
    sess = session_date(now_et)
    last = state.get("last_success_session")
    seeding = is_weekend_seeding(now_et)

    # WEEKDAY BLACKOUT (Mon–Fri 08:00 ET .. 20:10 ET): Gateway MUST be off — never
    # collide with the user's manual trading. SUPPRESSED across the weekend
    # seeding span (Sat 00:00 .. Mon 08:00 ET) — market closed, safe to keep up.
    if not seeding and is_blackout(now_et.time()):
        if fx.gateway_is_up():
            log(f"[blackout {now_et:%a %H:%M ET}] Gateway up — shutting down")
            fx.gateway_down()
            return "BLACKOUT_GW_DOWN"
        return "BLACKOUT_IDLE"

    # A weekday trading session still needs its top-up — fetch it if we're before
    # the shutdown margin. (Also finishes a Friday run that spilled into Saturday.)
    if is_trading_session(sess) and last != sess:
        dl = deadline_for(now_et)
        if now_et < dl:
            if not fx.gateway_up():
                log("Gateway failed to come up — will retry next tick")
                return "GW_UP_FAILED"
            ok = fx.run_topup(dl, sess)
            if ok:
                # Weekday: shut the Gateway IMMEDIATELY (deep check is read-only,
                # so we minimise Gateway-up time before the trading window). On the
                # weekend span KEEP it up — seeding continues after the top-up.
                if not seeding:
                    fx.gateway_down()
                state["last_success_session"] = sess
                _save_state(state)
                deep = fx.run_deep_check()          # read-only, no Gateway
                regen = fx.run_regen()              # profile regen (local)
                fx.write_run_manifest(sess, deep, regen)
                fx.report_freshness()               # push freshness to dashboard
                log(f"[session {sess}] top-up DONE -> deep check -> profiles -> "
                    f"manifest -> freshness"
                    + (" (Gateway kept up for weekend seeding)" if seeding
                       else " -> Gateway down"))
                return "FETCH_OK"
            # Failed. On a weekday, if we're now past the deadline / in blackout,
            # shut the Gateway right away rather than waiting a tick.
            now2 = et_now()
            if not seeding and (now2 >= dl or is_blackout(now2.time())):
                if fx.gateway_is_up():
                    fx.gateway_down()
                log(f"[session {sess}] top-up aborted at deadline — Gateway down")
                return "FETCH_ABORTED"
            log(f"[session {sess}] top-up failed — retry next tick")
            return "FETCH_RETRY"
        elif not seeding:
            # Past the shutdown margin before 08:00 ET on a weekday morning: do
            # NOT start heavy work; make sure the Gateway is provably down.
            if fx.gateway_is_up():
                fx.gateway_down()
            log(f"[session {sess}] within shutdown margin before {RUN_END} ET — not starting")
            return "PAST_DEADLINE"

    # WEEKEND SEEDING (Sat 00:00 .. Mon 08:00 ET): keep the Gateway UP so a seed /
    # backfill can run uninterrupted. Resurrect it if it drops — including the
    # daily ~08:00 IBKR auto-logout, which leaves the Gateway PROCESS alive but
    # logged out (port down). A plain gateway_up() only *waits* for that stale
    # process, so after one failed bring-up we force a CLEAN RESTART (kill the
    # logged-out session, relaunch IBC). `gw_down_ticks` is in-memory only.
    if seeding:
        if fx.gateway_is_up():
            state["gw_down_ticks"] = 0
            return "WEEKEND_GW_LIVE"
        n = state.get("gw_down_ticks", 0) + 1
        state["gw_down_ticks"] = n
        if n == 1:
            # First detection — gentle: launch if dead, or wait for an in-flight
            # (re)login (don't kill a clean restart that's already coming up).
            log(f"[weekend {now_et:%a %H:%M ET}] Gateway down — bringing up for seeding")
            ok = fx.gateway_up()
            if ok:
                state["gw_down_ticks"] = 0
                return "WEEKEND_GW_UP"
            return "WEEKEND_GW_UP_FAILED"
        # Still down after a prior attempt — the session is stale/logged-out
        # (the 08:00 auto-logout). Force a clean restart: kill, then relaunch.
        log(f"[weekend {now_et:%a %H:%M ET}] Gateway still down after auto-logout — "
            f"forcing a clean restart (kill stale session + relaunch IBC)")
        fx.gateway_down()
        ok = fx.gateway_up()
        if ok:
            state["gw_down_ticks"] = 0
            return "WEEKEND_GW_RESURRECT"
        return "WEEKEND_GW_RESURRECT_FAILED"

    # WEEKDAY, in the run window, nothing to fetch (session already done, or the
    # early-morning gap): Gateway off, idle.
    if fx.gateway_is_up():
        fx.gateway_down()
        return "IDLE_GW_DOWN"
    return "IDLE"


# ---- state persistence (per-PC, state/) ----
def _state_path() -> Path:
    return SKILL_DIR / "state" / "ingest_supervisor_state.json"

def _load_state() -> dict:
    import json
    p = _state_path()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("last_success_session"):
                d["last_success_session"] = date.fromisoformat(d["last_success_session"])
            return d
        except Exception:
            pass
    return {}

def _save_state(state: dict) -> None:
    if not _PERSIST_STATE:
        return
    import json
    p = _state_path(); p.parent.mkdir(parents=True, exist_ok=True)
    out = dict(state)
    if isinstance(out.get("last_success_session"), date):
        out["last_success_session"] = out["last_success_session"].isoformat()
    p.write_text(json.dumps(out), encoding="utf-8")


def _heartbeat_path() -> Path:
    return SKILL_DIR / "state" / "supervisor_heartbeat.json"


def _write_heartbeat(action: str) -> None:
    """Stamp a tiny per-tick heartbeat (UTC ts + last action) so the tray can show
    'Supervisor: LIVE · <action>'. Liveness itself is confirmed by the tray via a
    process check (this file goes stale during a long blocking top-up/deep-check,
    when the supervisor is alive but not ticking)."""
    if not _PERSIST_STATE:
        return
    import json
    try:
        p = _heartbeat_path(); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "et": et_now().strftime("%a %H:%M ET"),
        }), encoding="utf-8")
    except Exception:
        pass


def run_loop(fx, log) -> None:
    state = _load_state()
    log(f"ingest_supervisor up. RUN_START={RUN_START} RUN_END={RUN_END} ET. "
        f"last_success_session={state.get('last_success_session')}")
    last_action = None
    ticks = 0
    HEARTBEAT_EVERY = 30   # ticks (~30 min at TICK_SEC=60) — proves it's alive
    while True:
        try:
            action = supervisor_tick(state, fx, log)
            _write_heartbeat(action)   # per-tick liveness/action for the tray
            # Log on every phase CHANGE, and a periodic heartbeat during idle, so
            # the supervisor is never silently stalled (the 2026-06-04 failure had
            # no log after it got stuck — a heartbeat would have made it obvious).
            if action != last_action or ticks % HEARTBEAT_EVERY == 0:
                log(f"[heartbeat] {action} (et={et_now():%a %H:%M ET}, "
                    f"last_success={state.get('last_success_session')})")
                last_action = action
            ticks += 1
        except Exception as exc:
            log(f"tick error: {exc!r}")
        _time.sleep(TICK_SEC)


# ======================================================================
# SELF-TEST  (pure timing assertions)
# ======================================================================

def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ET)

def self_test() -> int:
    fails = []
    def chk(desc, got, exp):
        if got != exp:
            fails.append(f"  FAIL {desc}: got {got!r} expected {exp!r}")
        else:
            print(f"  ok   {desc} -> {got!r}")

    # 2026-06-01 is a Monday. 06-05 Fri, 06-06 Sat, 06-07 Sun.
    print("# window boundaries")
    chk("Mon 20:09 -> blackout", is_blackout(_dt("2026-06-01 20:09").time()), True)
    chk("Mon 20:10 -> run window", in_run_window(_dt("2026-06-01 20:10").time()), True)
    chk("Tue 07:59 -> run window", in_run_window(_dt("2026-06-02 07:59").time()), True)
    chk("Tue 08:00 -> blackout", is_blackout(_dt("2026-06-02 08:00").time()), True)
    chk("Tue 12:00 -> blackout", is_blackout(_dt("2026-06-02 12:00").time()), True)

    print("# session attribution")
    chk("Mon 21:00 session", session_date(_dt("2026-06-01 21:00")), date(2026, 6, 1))
    chk("Tue 02:00 session=Mon", session_date(_dt("2026-06-02 02:00")), date(2026, 6, 1))
    chk("Tue 12:00 session=None", session_date(_dt("2026-06-02 12:00")), None)
    chk("Sat 02:00 session=Fri", session_date(_dt("2026-06-06 02:00")), date(2026, 6, 5))

    print("# should_fetch")
    chk("Mon eve, none done -> fetch", should_fetch(_dt("2026-06-01 20:10"), None), True)
    chk("Mon eve, Mon done -> no",
        should_fetch(_dt("2026-06-01 22:00"), date(2026, 6, 1)), False)
    chk("Tue 03:00, Mon done -> no (same session)",
        should_fetch(_dt("2026-06-02 03:00"), date(2026, 6, 1)), False)
    chk("Tue 08:00 blackout -> no", should_fetch(_dt("2026-06-02 08:00"), None), False)
    chk("Sat eve (weekend) -> no", should_fetch(_dt("2026-06-06 20:10"), None), False)
    chk("Sun eve (weekend) -> no", should_fetch(_dt("2026-06-07 21:00"), None), False)
    chk("Sat 02:00 = Fri session, not done -> fetch",
        should_fetch(_dt("2026-06-06 02:00"), None), True)

    print("# weekend seeding span (Sat 00:00 ET .. Mon 08:00 ET -> Gateway up)")
    chk("Fri 12:00 -> not seeding", is_weekend_seeding(_dt("2026-06-05 12:00")), False)
    chk("Fri 21:00 (Fri run) -> not seeding", is_weekend_seeding(_dt("2026-06-05 21:00")), False)
    chk("Sat 00:00 -> seeding", is_weekend_seeding(_dt("2026-06-06 00:00")), True)
    chk("Sat 12:00 -> seeding", is_weekend_seeding(_dt("2026-06-06 12:00")), True)
    chk("Sun 12:00 -> seeding", is_weekend_seeding(_dt("2026-06-07 12:00")), True)
    chk("Mon 02:00 -> seeding", is_weekend_seeding(_dt("2026-06-01 02:00")), True)
    chk("Mon 08:00 -> NOT seeding (trading week)", is_weekend_seeding(_dt("2026-06-01 08:00")), False)

    print("# deadline (RUN_END 08:00 ET minus 3-min shutdown margin = 07:57)")
    chk("Mon 20:10 -> deadline Tue 07:57",
        deadline_for(_dt("2026-06-01 20:10")), _dt("2026-06-02 07:57"))
    chk("Tue 02:00 -> deadline Tue 07:57",
        deadline_for(_dt("2026-06-02 02:00")), _dt("2026-06-02 07:57"))

    print("# DST sanity (ET offset flips; wall-clock logic unchanged)")
    # 2026 DST: starts Sun Mar 8, ends Sun Nov 1.
    jan = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).astimezone(ET)  # EST -5
    jul = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).astimezone(ET)  # EDT -4
    chk("Jan UTC offset = -5h", jan.utcoffset(), timedelta(hours=-5))
    chk("Jul UTC offset = -4h", jul.utcoffset(), timedelta(hours=-4))

    if fails:
        print("\n".join(fails)); print(f"\nSELF-TEST FAILED ({len(fails)})"); return 1
    print("\nSELF-TEST PASSED"); return 0


# ======================================================================
# DRY-RUN  (loop logic with mocked side effects + fake clock)
# ======================================================================

class MockEffects:
    def __init__(self, log, fail_topup=0, start_up=False, fail_gw_up=0):
        self.log = log; self._up = start_up; self.actions = []; self._fail = fail_topup
        self._fail_gw_up = fail_gw_up   # simulate N failed bring-ups (logged-out stale session)
    def gateway_is_up(self): return self._up
    def gateway_up(self):
        if self._fail_gw_up > 0:
            self._fail_gw_up -= 1
            self.actions.append("GW_UP_FAIL"); self.log("    [mock] gateway UP failed")
            return False
        if not self._up: self.actions.append("GW_UP"); self.log("    [mock] gateway UP")
        self._up = True; return True
    def gateway_down(self):
        if self._up: self.actions.append("GW_DOWN"); self.log("    [mock] gateway DOWN")
        self._up = False
    def run_topup(self, deadline, session=None):
        self.actions.append("TOPUP"); self.log(f"    [mock] top-up (deadline {deadline:%a %H:%M}, fresh_through={session})")
        if self._fail > 0:
            self._fail -= 1; self.actions.append("TOPUP_FAIL"); return False
        return True
    def run_deep_check(self):
        self.actions.append("DEEPCHECK"); self.log("    [mock] deep check + report"); return {"status": "clean"}
    def run_regen(self):
        self.actions.append("REGEN"); self.log("    [mock] profile regen"); return {"status": "ok"}
    def write_run_manifest(self, session, deep, regen):
        self.actions.append("MANIFEST"); self.log("    [mock] run manifest")
    def report_freshness(self):
        self.actions.append("FRESHNESS"); self.log("    [mock] freshness pushed")


def scenario_test() -> int:
    """Assert the failure/retry, deadline-stop, and force-off-during-blackout
    behaviours with mocked effects (the safety-critical loop logic)."""
    global _PERSIST_STATE
    _PERSIST_STATE = False   # never write real state during tests
    fails = []
    def chk(desc, got, exp):
        if got != exp: fails.append(f"  FAIL {desc}: got {got!r} expected {exp!r}")
        else: print(f"  ok   {desc}")
    nolog = lambda m: None

    def tick_at(when, state, fx):
        _FAKE["now"] = _dt(when)
        return supervisor_tick(state, fx, nolog)

    # A: top-up fails twice then succeeds within the window -> retry, retry, OK, idle
    fx = MockEffects(nolog, fail_topup=2); state = {}; seq = []
    for when in ("2026-06-01 20:10", "2026-06-01 20:40",
                 "2026-06-01 21:10", "2026-06-01 21:40"):
        seq.append(tick_at(when, state, fx))
    chk("A retry->retry->ok->idle", seq, ["FETCH_RETRY", "FETCH_RETRY", "FETCH_OK", "IDLE"])
    chk("A success session recorded", state.get("last_success_session"), date(2026, 6, 1))
    chk("A deep check ran exactly once", fx.actions.count("DEEPCHECK"), 1)
    chk("A gateway ended DOWN", fx.gateway_is_up(), False)

    # B: NO revive during blackout (the user's manual-trading window) — never fetch
    fx = MockEffects(nolog); state = {}
    chk("B blackout 10:00 -> no fetch", tick_at("2026-06-02 10:00", state, fx), "BLACKOUT_IDLE")
    chk("B no top-up attempted", "TOPUP" in fx.actions, False)

    # C: Gateway somehow UP during blackout -> force it OFF (manual-trade safety net)
    fx = MockEffects(nolog, start_up=True); state = {}
    chk("C blackout + gw up -> shut down", tick_at("2026-06-02 10:00", state, fx), "BLACKOUT_GW_DOWN")
    chk("C gateway now DOWN", fx.gateway_is_up(), False)

    # D: WEEKEND — Gateway is KEPT UP for seeding (not forced off). Sat 06-06.
    fx = MockEffects(nolog, start_up=True); state = {}
    chk("D weekend + gw up -> kept LIVE for seeding", tick_at("2026-06-06 21:00", state, fx), "WEEKEND_GW_LIVE")
    chk("D gateway stays UP", fx.gateway_is_up(), True)
    # D2: weekend daytime with Gateway down -> bring it UP (was blackout before)
    fx = MockEffects(nolog, start_up=False); state = {}
    chk("D2 Sat daytime + gw down -> brought UP", tick_at("2026-06-06 12:00", state, fx), "WEEKEND_GW_UP")
    chk("D2 gateway now UP", fx.gateway_is_up(), True)
    chk("D2 no top-up on weekend", "TOPUP" in fx.actions, False)
    # D3: Sunday daytime too
    fx = MockEffects(nolog, start_up=False); state = {}
    chk("D3 Sun daytime + gw down -> brought UP", tick_at("2026-06-07 12:00", state, fx), "WEEKEND_GW_UP")
    # D4: Monday early morning (<08:00) still seeding -> Gateway up
    fx = MockEffects(nolog, start_up=False); state = {}
    chk("D4 Mon 02:00 (<08:00) -> brought UP", tick_at("2026-06-01 02:00", state, fx), "WEEKEND_GW_UP")
    # D5: Monday 08:00 -> trading week resumes -> Gateway forced OFF (safety)
    fx = MockEffects(nolog, start_up=True); state = {}
    chk("D5 Mon 08:00 -> blackout shuts gateway", tick_at("2026-06-01 08:00", state, fx), "BLACKOUT_GW_DOWN")
    chk("D5 gateway now DOWN", fx.gateway_is_up(), False)
    # D6: Friday daytime still protected (Gateway off for Friday trading)
    fx = MockEffects(nolog, start_up=True); state = {}
    chk("D6 Fri 12:00 -> blackout shuts gateway", tick_at("2026-06-05 12:00", state, fx), "BLACKOUT_GW_DOWN")
    chk("D6 gateway now DOWN", fx.gateway_is_up(), False)

    # R: weekend AUTO-LOGOUT resurrection — the 08:00 logout leaves a stale,
    # logged-out session so the gentle bring-up FAILS once; the next tick forces
    # a clean restart (kill + relaunch) and the Gateway comes back for seeding.
    fx = MockEffects(nolog, start_up=False, fail_gw_up=1); state = {}
    chk("R Sat 08:30 tick1 (stale logout) -> bring-up failed",
        tick_at("2026-06-06 08:30", state, fx), "WEEKEND_GW_UP_FAILED")
    chk("R still down after tick1", fx.gateway_is_up(), False)
    chk("R tick2 -> forced clean restart resurrects",
        tick_at("2026-06-06 08:31", state, fx), "WEEKEND_GW_RESURRECT")
    chk("R gateway back UP", fx.gateway_is_up(), True)

    # E: already-succeeded session in window -> idle, no duplicate fetch
    fx = MockEffects(nolog); state = {"last_success_session": date(2026, 6, 1)}
    chk("E same session -> idle", tick_at("2026-06-02 02:00", state, fx), "IDLE")
    chk("E no top-up", "TOPUP" in fx.actions, False)

    # F: inside the shutdown margin before 08:00 ET -> do NOT start; Gateway off
    fx = MockEffects(nolog, start_up=True); state = {}
    chk("F 07:58 (margin) -> past deadline, no fetch", tick_at("2026-06-02 07:58", state, fx), "PAST_DEADLINE")
    chk("F no top-up attempted", "TOPUP" in fx.actions, False)
    chk("F gateway forced DOWN", fx.gateway_is_up(), False)

    _FAKE["now"] = None
    if fails:
        print("\n".join(fails)); print(f"\nSCENARIO TESTS FAILED ({len(fails)})"); return 1
    print("\nSCENARIO TESTS PASSED"); return 0


def dry_run(start: str, step_min: int, ticks: int) -> int:
    global _PERSIST_STATE
    _PERSIST_STATE = False   # dry-run must not write real state
    fx = MockEffects(print)
    state = {}
    _FAKE["now"] = _dt(start)
    print(f"# DRY RUN from {start} ET, step {step_min}m, {ticks} ticks "
          f"(RUN {RUN_START}-{RUN_END} ET)\n")
    for _ in range(ticks):
        now = _FAKE["now"]
        action = supervisor_tick(state, fx, lambda m: None)
        print(f"{now:%a %Y-%m-%d %H:%M ET}  ->  {action:16} "
              f"(gw={'UP' if fx.gateway_is_up() else 'down'}, "
              f"last_success={state.get('last_success_session')})")
        _FAKE["now"] = now + timedelta(minutes=step_min)
    _FAKE["now"] = None
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fake-start", default="2026-06-01 19:00")
    ap.add_argument("--fake-step", type=int, default=30)
    ap.add_argument("--fake-ticks", type=int, default=80)
    args = ap.parse_args()

    if args.self_test:
        rc1 = self_test()
        print("\n############ SCENARIO TESTS (failure / retry / blackout safety) ############")
        rc2 = scenario_test()
        return rc1 or rc2
    if args.dry_run:
        return dry_run(args.fake_start, args.fake_step, args.fake_ticks)

    # real mode
    from _common import get_data_root
    logp = get_data_root() / "_supervisor.log"
    def log(msg):
        line = f"[{et_now():%Y-%m-%d %H:%M:%S ET}] {msg}"
        try:
            with logp.open("a", encoding="utf-8") as f: f.write(line + "\n")
        except Exception: pass
        print(line, flush=True)
    run_loop(RealEffects(log), log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
