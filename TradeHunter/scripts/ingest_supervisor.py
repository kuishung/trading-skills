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
  - BLACKOUT: 08:00 ET -> 20:10 ET, plus ALL weekend. Gateway is forced OFF and
    nothing runs, so it can NEVER collide with the user's manual IBKR trading
    (which begins 90 min before the open).

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


# ======================================================================
# SIDE EFFECTS  (overridden by a mock in --dry-run)
# ======================================================================

class RealEffects:
    """Real Gateway / ingest / deep-check actions on Hermes."""

    def __init__(self, log):
        self.log = log

    # ---- Gateway ----
    def gateway_is_up(self) -> bool:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {GATEWAY_PORT} -State Listen "
                 f"-ErrorAction SilentlyContinue | Measure-Object).Count"],
                capture_output=True, text=True, timeout=20)
            return out.stdout.strip().isdigit() and int(out.stdout.strip()) > 0
        except Exception as exc:
            self.log(f"gateway_is_up check failed: {exc}")
            return False

    def gateway_up(self) -> bool:
        if self.gateway_is_up():
            return True
        # Don't launch a SECOND IBC if one is already starting up — login can
        # take ~30-60s before the port listens, and a duplicate means two
        # Gateway sessions on the same account (IBKR kicks one). If an IBC/
        # Gateway process is already alive, just wait for it to finish logging in.
        if self._gateway_proc_alive():
            self.log("IBC/Gateway process already alive (login in progress) — waiting, not relaunching")
        else:
            bat = SKILL_DIR / "ibc" / "StartIBC-intraday.bat"
            self.log(f"starting Gateway via {bat}")
            try:
                subprocess.Popen(["cmd.exe", "/c", str(bat)],
                                 cwd=str(bat.parent),
                                 creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            except Exception as exc:
                self.log(f"Gateway launch failed: {exc}")
                return False
        waited = 0
        while waited < GATEWAY_UP_TIMEOUT_SEC:
            _time.sleep(10); waited += 10
            if self.gateway_is_up():
                self.log(f"Gateway up after ~{waited}s")
                return True
        self.log(f"Gateway did NOT come up within {GATEWAY_UP_TIMEOUT_SEC}s")
        return False

    def gateway_down(self) -> None:
        if not self.gateway_is_up() and not self._gateway_proc_alive():
            return
        stop = SKILL_DIR / "ibc" / "Stop.bat"
        self.log("stopping Gateway (ibc/Stop.bat)")
        try:
            subprocess.run(["cmd.exe", "/c", str(stop)], cwd=str(stop.parent),
                           timeout=60)
        except Exception as exc:
            self.log(f"Stop.bat failed: {exc}")
        waited = 0
        while waited < 60 and self.gateway_is_up():
            _time.sleep(5); waited += 5
        if self.gateway_is_up() or self._gateway_proc_alive():
            self.log("Gateway still up after Stop.bat — force-killing ibgateway*/java")
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process | "
                            "Where-Object { $_.Name -match 'ibgateway' -or "
                            "($_.Name -match 'java' -and $_.CommandLine -match 'IBC') } | "
                            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                            "-ErrorAction SilentlyContinue }"],
                           timeout=30)

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

    def run_deep_check(self) -> None:
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

    # BLACKOUT (incl. weekend daytime): Gateway MUST be off — never collide with
    # the user's manual trading. Force it down if anything left it up.
    if is_blackout(now_et.time()):
        if fx.gateway_is_up():
            log(f"[blackout {now_et:%a %H:%M ET}] Gateway up — shutting down")
            fx.gateway_down()
            return "BLACKOUT_GW_DOWN"
        return "BLACKOUT_IDLE"

    # In the run window but not a trading session (weekend evening): nothing to
    # fetch; keep the Gateway off.
    if not is_trading_session(sess):
        if fx.gateway_is_up():
            fx.gateway_down()
            return "WEEKEND_GW_DOWN"
        return "WEEKEND_IDLE"

    # Already finished this session: stay idle with the Gateway off.
    if last == sess:
        if fx.gateway_is_up():
            fx.gateway_down()
            return "DONE_GW_DOWN"
        return "DONE_IDLE"

    # Inside the shutdown margin before RUN_END? Do NOT start heavy work; make
    # sure the Gateway is off so it's provably down before 08:00 ET.
    dl = deadline_for(now_et)
    if now_et >= dl:
        if fx.gateway_is_up():
            fx.gateway_down()
        log(f"[session {sess}] within shutdown margin before {RUN_END} ET — Gateway off, not starting")
        return "PAST_DEADLINE"

    # Need to fetch this session.
    if not fx.gateway_up():
        log("Gateway failed to come up — will retry next tick")
        return "GW_UP_FAILED"
    ok = fx.run_topup(dl, sess)
    if ok:
        # Shut the Gateway IMMEDIATELY — the deep check is read-only and needs no
        # Gateway, so we minimise Gateway-up time and never let the ~15-min deep
        # check push Gateway-down toward the trading window.
        fx.gateway_down()
        state["last_success_session"] = sess
        _save_state(state)
        fx.run_deep_check()
        log(f"[session {sess}] top-up DONE -> Gateway down -> deep check written")
        return "FETCH_OK"
    # Failed or deadline-aborted. If we're now at/after the deadline (or already
    # in blackout), shut the Gateway right away rather than waiting a tick.
    now2 = et_now()
    if now2 >= dl or is_blackout(now2.time()):
        if fx.gateway_is_up():
            fx.gateway_down()
        log(f"[session {sess}] top-up aborted at deadline — Gateway down")
        return "FETCH_ABORTED"
    log(f"[session {sess}] top-up failed — retry next tick")
    return "FETCH_RETRY"


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


def run_loop(fx, log) -> None:
    state = _load_state()
    log(f"ingest_supervisor up. RUN_START={RUN_START} RUN_END={RUN_END} ET. "
        f"last_success_session={state.get('last_success_session')}")
    while True:
        try:
            supervisor_tick(state, fx, log)
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
    def __init__(self, log, fail_topup=0, start_up=False):
        self.log = log; self._up = start_up; self.actions = []; self._fail = fail_topup
    def gateway_is_up(self): return self._up
    def gateway_up(self):
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
        self.actions.append("DEEPCHECK"); self.log("    [mock] deep check + report")


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
    chk("A retry->retry->ok->idle", seq, ["FETCH_RETRY", "FETCH_RETRY", "FETCH_OK", "DONE_IDLE"])
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

    # D: Gateway UP on a weekend evening -> force OFF (no session to fetch)
    fx = MockEffects(nolog, start_up=True); state = {}
    chk("D weekend + gw up -> shut down", tick_at("2026-06-06 21:00", state, fx), "WEEKEND_GW_DOWN")
    chk("D gateway now DOWN", fx.gateway_is_up(), False)

    # E: already-succeeded session in window -> idle, no duplicate fetch
    fx = MockEffects(nolog); state = {"last_success_session": date(2026, 6, 1)}
    chk("E same session -> idle", tick_at("2026-06-02 02:00", state, fx), "DONE_IDLE")
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
