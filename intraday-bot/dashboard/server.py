"""Local dashboard server for intraday_bot.

FastAPI + WebSocket. Serves dashboard/web/index.html at
http://localhost:8000. Tails state/events_*.jsonl and re-reads plan/
fills/equity files so a browser tab shows what the bot is doing in
real time.

Read-only. The dashboard never sends orders -- it observes files the
bot writes plus the same data adapter the bot reads. The bot is what
places orders (execution/orchestrator.py).

Run:
    py dashboard/server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# --- intraday-bot bootstrap: make all layer folders importable ---
_root = Path(__file__).resolve().parent.parent   # intraday-bot/
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---
import _gating  # noqa: E402
from strategy import KNOWN_STRATEGIES  # noqa: E402

DASHBOARD_START_TS = _time.time()

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
WEB_DIR = Path(__file__).resolve().parent / "web"   # dashboard/web/
INDEX_HTML = WEB_DIR / "index.html"

HOST = "127.0.0.1"
PORT = 8000


class Hub:
    """Tracks WebSocket clients and broadcasts to all of them."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()


# ------------- Config + Bot subprocess manager -------------

def _load_cfg() -> dict[str, Any]:
    cfg_path = SKILL_DIR / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


class BotManager:
    """Spawn/stop the trading session -- trade_day.py.

    Each strategy family runs its own scanner CLI (e.g. guns_scanner.py)
    that writes its own watchlist file pre-market. There is no shared
    continuous scanner subprocess.

    Per-strategy gating: each strategy has TWO live filesystem flags
    (managed by scripts/_gating.py):
      state/enabled_<name>.flag  -- ON/OFF gate (does the pipeline run?)
      state/armed_<name>.flag    -- ARM gate    (do plans submit?)
    The bot reads ON/OFF at the top of _fire_strategy_entries and ARM
    at the submit site, so dashboard toggles take effect mid-session
    (ON/OFF on the next scheduled fire, ARM on the next submit). This
    class does NOT gate the launch on either flag; the bot always
    launches in flag-aware mode.

    Limitation: if the dashboard restarts while the bot is running, the
    new dashboard loses the subprocess handle and reports 'stopped' until
    the child exits or is killed externally. PID-file adoption is a TODO.
    """

    BOT_SCRIPT = "execution/orchestrator.py"

    def __init__(self) -> None:
        self.bot_proc: subprocess.Popen | None = None
        self._bot_log_fh = None
        self._bot_started_ts: float | None = None
        # One-shot migrations / first-run seeding. All idempotent; safe
        # to call on every dashboard start.
        _gating.migrate_global_arm_flag(KNOWN_STRATEGIES)
        # Use the framework's merged loader so seed values come from the
        # example file even when config.json hasn't overridden them.
        try:
            from _common import load_config as _load_merged_cfg
            seed_cfg = _load_merged_cfg()
        except Exception:
            seed_cfg = _load_cfg()
        _gating.bootstrap_from_config(seed_cfg, KNOWN_STRATEGIES)

    # ---- gating (per-strategy, lives in scripts/_gating.py) ----

    @staticmethod
    def arm_map() -> dict[str, bool]:
        """Return {strategy_name: armed} over every KNOWN strategy."""
        return _gating.all_armed_state(KNOWN_STRATEGIES)

    @staticmethod
    def any_armed() -> bool:
        return any(_gating.all_armed_state(KNOWN_STRATEGIES).values())

    @staticmethod
    def set_strategy_armed(name: str, value: bool) -> None:
        _gating.set_armed(name, value)

    @staticmethod
    def set_all_armed(value: bool) -> None:
        _gating.set_all_armed(KNOWN_STRATEGIES, value)

    @staticmethod
    def enable_map() -> dict[str, bool]:
        """Return {strategy_name: enabled} over every KNOWN strategy."""
        return _gating.all_enabled_state(KNOWN_STRATEGIES)

    @staticmethod
    def any_enabled() -> bool:
        return any(_gating.all_enabled_state(KNOWN_STRATEGIES).values())

    @staticmethod
    def set_strategy_enabled(name: str, value: bool) -> None:
        _gating.set_enabled(name, value)

    @staticmethod
    def set_all_enabled(value: bool) -> None:
        _gating.set_all_enabled(KNOWN_STRATEGIES, value)

    # ---- process plumbing ----

    @staticmethod
    def _proc_state(proc: subprocess.Popen | None) -> str:
        if proc is None:
            return "stopped"
        return "running" if proc.poll() is None else "stopped"

    @staticmethod
    def _proc_pid(proc: subprocess.Popen | None) -> int | None:
        if proc and proc.poll() is None:
            return proc.pid
        return None

    @property
    def status(self) -> str:
        return self._proc_state(self.bot_proc)

    @property
    def pid(self) -> int | None:
        return self._proc_pid(self.bot_proc)

    @property
    def bot_uptime_s(self) -> float | None:
        if self._bot_started_ts and self._proc_state(self.bot_proc) == "running":
            return _time.time() - self._bot_started_ts
        return None

    def start(self) -> dict[str, Any]:
        if self.status == "running":
            return {
                "status": "already_running",
                "bot_pid": self._proc_pid(self.bot_proc),
            }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cflags = 0
        if os.name == "nt":
            cflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        bot_log = STATE_DIR / f"bot_{_today_str()}.log"
        self._bot_log_fh = bot_log.open("a", encoding="utf-8")
        # No --dry-run: the bot always runs in flag-aware mode. Arming is
        # per-strategy, read live at the submit site. --dry-run is now a
        # deliberate operator override (used for replays / smoke tests).
        bot_argv = [sys.executable, str(SKILL_DIR / self.BOT_SCRIPT)]
        self.bot_proc = subprocess.Popen(
            bot_argv, cwd=str(SKILL_DIR),
            stdout=self._bot_log_fh, stderr=subprocess.STDOUT,
            creationflags=cflags,
        )
        self._bot_started_ts = _time.time()

        return {
            "status": "started",
            "bot_pid": self.bot_proc.pid,
            "enabled_strategies": self.enable_map(),
            "armed_strategies": self.arm_map(),
        }

    def stop(self) -> dict[str, Any]:
        result: dict[str, Any] = {"bot": "not_running"}
        proc = self.bot_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3.0)
            except Exception:
                pass
            result["bot"] = "stopped"
        if self._bot_log_fh:
            try: self._bot_log_fh.close()
            except Exception: pass
            self._bot_log_fh = None
        return result


bot = BotManager()


def _probe_ibkr_listener_bind() -> str:
    """Cheap: detect whether something owns the API port. Does NOT open a
    connection to TWS, so leaves no CloseWait. Used as a fast fallback
    between full handshake probes.
    """
    cfg = _load_cfg()
    host = cfg.get("ibkr_host", "127.0.0.1")
    try:
        port = int(cfg.get("ibkr_port", 7497))
    except (TypeError, ValueError):
        return "down"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return "down"   # bind succeeded -> nothing listening
    except OSError:
        return "up"     # port in use -> a listener owns it
    finally:
        try: s.close()
        except Exception: pass


# Probe client id distinct from the bot's 71 so they can coexist.
PROBE_CLIENT_ID = 99
_last_handshake_at: float = 0.0
_last_handshake_result: str = "unknown"
HANDSHAKE_INTERVAL_S = 30.0


def _probe_ibkr_handshake() -> str:
    """Do a real ib_insync connect+disconnect. Verifies TWS's accept loop
    is responsive, not just that the OS-level port is bound. Protocol-clean
    disconnect means TWS drains the socket properly (no CloseWait residue).

    Uses a distinct clientId so it doesn't collide with the bot.
    """
    # Run the asyncio shim before importing ib_insync (Python 3.14 / eventkit).
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        from ib_insync import IB
    except Exception:
        return "unknown"
    cfg = _load_cfg()
    host = cfg.get("ibkr_host", "127.0.0.1")
    try:
        port = int(cfg.get("ibkr_port", 7497))
    except (TypeError, ValueError):
        return "down"
    ib = IB()
    try:
        ib.connect(host, port, clientId=PROBE_CLIENT_ID, timeout=4, readonly=True)
        ok = ib.isConnected()
        try:
            ib.disconnect()
        except Exception:
            pass
        return "up" if ok else "down"
    except Exception:
        return "down"


def _probe_ibkr_tcp(timeout: float = 0.5) -> str:
    """Composite probe used by the health loop.

    Most calls are cheap bind-probes. Once every HANDSHAKE_INTERVAL_S we do
    a real ib_insync handshake to detect wedged-accept-loop states (TWS
    UI thread blocked by modal dialog, etc.) that a bind-probe can't see.
    """
    import time as _time
    global _last_handshake_at, _last_handshake_result
    now = _time.time()
    bind_state = _probe_ibkr_listener_bind()
    if bind_state == "down":
        # Listener gone — no point doing the heavy probe.
        _last_handshake_result = "down"
        return "down"
    if now - _last_handshake_at >= HANDSHAKE_INTERVAL_S:
        _last_handshake_result = _probe_ibkr_handshake()
        _last_handshake_at = now
    # If the last handshake said up, keep reporting up between probes; if it
    # said down (wedged accept loop), keep reporting down until next attempt.
    return _last_handshake_result if _last_handshake_result in ("up", "down") else bind_state


_ibkr_status_cache = "unknown"
_bot_status_cache: tuple[str, int | None] = ("stopped", None)


# ------------- Alpaca paper integration -------------

_alpaca_client = None
_alpaca_cache: dict[str, Any] = {
    "status": "unknown",
    "account": None,
    "positions": [],
    "orders": [],
}


def _vault_root() -> Path | None:
    """Locate Dropbox VAULT root using parent-walk; cache unset."""
    here = Path(__file__).resolve()
    for ancestor in [here, *here.parents]:
        candidate = ancestor.parent / "VAULT" / "Claude Credential"
        if candidate.is_dir():
            return candidate
        candidate2 = ancestor / "VAULT" / "Claude Credential"
        if candidate2.is_dir():
            return candidate2
    return None


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_alpaca_env() -> dict[str, str] | None:
    """Resolve Alpaca creds. Order:
       1. <skill>/../alpaca-trader-paper/.env (local sibling)
       2. <vault>/alpaca-trader-paper.env
       3. <vault>/alpaca.env
    Returns dict with ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, ALPACA_BASE_URL,
    or None if no source found.
    """
    cfg = _load_cfg()
    candidates: list[Path] = []
    alp_path = cfg.get("alpaca_skill_path")
    if alp_path:
        candidates.append((SKILL_DIR / alp_path).resolve() / ".env")
    vault = _vault_root()
    if vault is not None:
        candidates.append(vault / "alpaca-trader-paper.env")
        candidates.append(vault / "alpaca.env")
    for p in candidates:
        env = _read_dotenv(p)
        if env.get("ALPACA_API_KEY_ID") and env.get("ALPACA_API_SECRET_KEY"):
            return env
    return None


def _get_alpaca_client():
    """Cache + return a paper TradingClient. None if env missing."""
    global _alpaca_client
    if _alpaca_client is not None:
        return _alpaca_client
    env = _load_alpaca_env()
    if env is None:
        return None
    base = env.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if "paper" not in base.lower():
        return None  # refuse non-paper
    try:
        from alpaca.trading.client import TradingClient
        _alpaca_client = TradingClient(
            env["ALPACA_API_KEY_ID"], env["ALPACA_API_SECRET_KEY"], paper=True
        )
        return _alpaca_client
    except Exception:
        return None


def _alpaca_snapshot() -> dict[str, Any]:
    client = _get_alpaca_client()
    if client is None:
        return {"status": "no_credentials", "account": None, "positions": [], "orders": []}
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        acct = client.get_account()
        positions = client.get_all_positions()
        orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))

        def _f(v):
            try: return float(v)
            except (TypeError, ValueError): return None

        return {
            "status": "ok",
            "account": {
                "equity": _f(acct.equity),
                "last_equity": _f(acct.last_equity),
                "cash": _f(acct.cash),
                "buying_power": _f(acct.buying_power),
                "trading_blocked": bool(acct.trading_blocked),
                "pattern_day_trader": bool(getattr(acct, "pattern_day_trader", False)),
            },
            "positions": [{
                "symbol": p.symbol,
                "qty": int(float(p.qty)),
                "avg_entry": _f(p.avg_entry_price),
                "current_price": _f(p.current_price),
                "market_value": _f(p.market_value),
                "unrealized_pl": _f(p.unrealized_pl),
                "unrealized_plpc": _f(p.unrealized_plpc),
                "side": str(p.side).split(".")[-1] if p.side else "long",
            } for p in positions],
            "orders": [{
                "id": str(o.id),
                "symbol": o.symbol,
                "side": str(o.side).split(".")[-1],
                "type": str(o.type).split(".")[-1] if o.type else "",
                "qty": int(float(o.qty or 0)),
                "limit_price": _f(o.limit_price),
                "stop_price": _f(o.stop_price),
                "status": str(o.status).split(".")[-1],
                "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            } for o in orders],
        }
    except Exception as e:
        # Force client rebuild next time in case session went stale.
        global _alpaca_client
        _alpaca_client = None
        return {"status": "error", "error": str(e),
                "account": None, "positions": [], "orders": []}


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path, tail: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-tail:] if tail else out


def _read_text_tail(path: Path, n_lines: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = text.splitlines()
    return lines[-n_lines:]


def _snapshot() -> dict[str, Any]:
    today = _today_str()
    cfg = _load_cfg()
    return {
        "type": "snapshot",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan": _read_json(STATE_DIR / f"plan_{today}.json"),
        "fills": _read_jsonl(STATE_DIR / f"fills_{today}.jsonl"),
        "equity": _read_json(STATE_DIR / f"equity_{today}.json"),
        "events": _read_jsonl(STATE_DIR / f"events_{today}.jsonl", tail=100),
        "bot_log": _read_text_tail(STATE_DIR / f"bot_{today}.log", n_lines=200),
        "alpaca": _alpaca_cache,
        "health": {
            "ibkr": _ibkr_status_cache,
            "ibkr_host": cfg.get("ibkr_host", "127.0.0.1"),
            "ibkr_port": cfg.get("ibkr_port", 7497),
            "alpaca": _alpaca_cache.get("status", "unknown"),
            "bot": {
                "status": bot.status,
                "pid": bot.pid,
                "enabled_strategies": bot.enable_map(),
                "any_enabled": bot.any_enabled(),
                "armed_strategies": bot.arm_map(),
                "any_armed": bot.any_armed(),
            },
        },
    }


async def _tail_events() -> None:
    """Watch events_<today>.jsonl for appended lines; broadcast each."""
    last_size = 0
    current_path: Path | None = None
    while True:
        today_path = STATE_DIR / f"events_{_today_str()}.jsonl"
        if today_path != current_path:
            # Day rolled over (or first iteration). Reset cursor.
            current_path = today_path
            last_size = today_path.stat().st_size if today_path.exists() else 0
        if today_path.exists():
            size = today_path.stat().st_size
            if size > last_size:
                with today_path.open("r", encoding="utf-8") as f:
                    f.seek(last_size)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        await hub.broadcast({"type": "event", "event": event})
                last_size = size
            elif size < last_size:
                # File was truncated or rotated externally.
                last_size = 0
        await asyncio.sleep(1.0)


async def _poll_health() -> None:
    """Probe IBKR TCP port + bot subprocess. Broadcast on transition."""
    global _ibkr_status_cache, _bot_status_cache
    loop = asyncio.get_event_loop()
    while True:
        ibkr = await loop.run_in_executor(None, _probe_ibkr_tcp)
        b_state = (bot.status, bot.pid)
        if ibkr != _ibkr_status_cache or b_state != _bot_status_cache:
            _ibkr_status_cache = ibkr
            _bot_status_cache = b_state
            await hub.broadcast({"type": "health", "health": {
                "ibkr": ibkr,
                "bot": {
                    "status": b_state[0], "pid": b_state[1],
                    "enabled_strategies": bot.enable_map(),
                    "any_enabled": bot.any_enabled(),
                    "armed_strategies": bot.arm_map(),
                    "any_armed": bot.any_armed(),
                },
            }})
        await asyncio.sleep(3.0)


async def _poll_alpaca() -> None:
    """Poll Alpaca paper account every 10s; broadcast on any change."""
    global _alpaca_cache
    loop = asyncio.get_event_loop()
    prev_signature = ""
    while True:
        snap = await loop.run_in_executor(None, _alpaca_snapshot)
        signature = json.dumps(snap, sort_keys=True, default=str)
        if signature != prev_signature:
            _alpaca_cache = snap
            await hub.broadcast({"type": "alpaca", "alpaca": snap})
            await hub.broadcast({"type": "health", "health": {
                "alpaca": snap.get("status", "unknown"),
            }})
            prev_signature = signature
        await asyncio.sleep(10.0)


async def _auto_start_loop() -> None:
    """Auto-launch the trading session at the configured ET wall-clock.

    Behaviour:
      - Wakes every 60s.
      - On weekdays (Mon-Fri ET), when ET time crosses cfg.auto_start_et
        (default 08:30 -- 1 hour before NYSE open at 09:30), call
        bot.start() once and write state/auto_started_<date>.flag so a
        dashboard restart doesn't re-trigger today. T-60 gives the bot
        a 30-min warm-up before Setup 1's shortlist_et (09:00) fires.
      - No-op if cfg.auto_start_enabled is false.
      - No-op if the bot is already running.
      - Stops trying once ET passes 10:30 — past that the entry window
        is closed anyway.

    US market holidays are NOT special-cased yet; on a holiday the auto
    trigger will fire and the bot will find no plan / no data and idle.
    """
    et = _et_tz_dash()
    while True:
        try:
            cfg = _load_cfg()
            if not cfg.get("auto_start_enabled", True):
                await asyncio.sleep(60)
                continue
            now_et = datetime.now(timezone.utc).astimezone(et)
            if now_et.weekday() >= 5:   # Sat/Sun
                await asyncio.sleep(60)
                continue
            target_str = str(cfg.get("auto_start_et", "08:30"))
            try:
                hh, mm = target_str.split(":")
                target_dt = now_et.replace(
                    hour=int(hh), minute=int(mm), second=0, microsecond=0
                )
            except Exception:
                await asyncio.sleep(60)
                continue
            window_end = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
            today_iso = now_et.strftime("%Y-%m-%d")
            flag = STATE_DIR / f"auto_started_{today_iso}.flag"

            should_trigger = (
                target_dt <= now_et < window_end
                and not flag.exists()
                and bot.status != "running"
            )
            if should_trigger:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                print(f"[auto-start] ET {now_et.strftime('%H:%M:%S')} — "
                      f"launching session (target was {target_str})")
                result = bot.start()
                try:
                    flag.write_text(
                        json.dumps({
                            "ts": now_et.isoformat(timespec="seconds"),
                            **{k: v for k, v in result.items() if k != "status"},
                        }, default=str),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                # Surface in event log + push immediate health update so the
                # browser pill flips green without waiting for the 3s health poll.
                try:
                    from events import emit as _emit
                    _emit("session.auto_start", result)
                except Exception:
                    pass
                await hub.broadcast({"type": "health", "health": {
                    "bot": {
                        "status": bot.status, "pid": bot.pid,
                        "enabled_strategies": bot.enable_map(),
                        "any_enabled": bot.any_enabled(),
                        "armed_strategies": bot.arm_map(),
                        "any_armed": bot.any_armed(),
                    },
                }})
        except Exception as e:
            print(f"[auto-start] error: {e}")
        await asyncio.sleep(60)


def _et_tz_dash():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        return pytz.timezone("America/New_York")


async def _tail_bot_log() -> None:
    """Tail state/bot_<today>.log and push new lines."""
    last_size = 0
    current_path: Path | None = None
    while True:
        today_path = STATE_DIR / f"bot_{_today_str()}.log"
        if today_path != current_path:
            current_path = today_path
            last_size = today_path.stat().st_size if today_path.exists() else 0
        if today_path.exists():
            size = today_path.stat().st_size
            if size > last_size:
                with today_path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    chunk = f.read()
                if chunk:
                    lines = chunk.splitlines()
                    for line in lines:
                        await hub.broadcast({"type": "botlog", "line": line})
                last_size = size
            elif size < last_size:
                last_size = 0
        await asyncio.sleep(0.5)


async def _poll_state() -> None:
    """Re-read plan/fills/equity periodically; broadcast on change."""
    prev_signature: str | None = None
    while True:
        snap = _snapshot()
        # Cheap content signature (skip ts/events from diff).
        signature = json.dumps(
            {k: v for k, v in snap.items() if k not in ("ts", "events", "type")},
            sort_keys=True,
            default=str,
        )
        if signature != prev_signature:
            await hub.broadcast({"type": "state", "state": {
                "plan": snap["plan"],
                "fills": snap["fills"],
                "equity": snap["equity"],
            }})
            prev_signature = signature
        await asyncio.sleep(5.0)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    tasks = [
        asyncio.create_task(_tail_events()),
        asyncio.create_task(_poll_state()),
        asyncio.create_task(_poll_health()),
        asyncio.create_task(_tail_bot_log()),
        asyncio.create_task(_poll_alpaca()),
        asyncio.create_task(_auto_start_loop()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(title="intraday_bot dashboard", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    if not INDEX_HTML.exists():
        return "<h1>Dashboard UI missing</h1><p>Expected web/index.html.</p>"
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/snapshot")
async def snapshot() -> JSONResponse:
    return JSONResponse(_snapshot())


@app.get("/data/health")
async def data_health(timeframe: str = "daily", details: bool = False) -> JSONResponse:
    """Parquet data integrity summary for the dashboard pill.

    Runs `resources/data_integrity.health_report()` over the requested
    timeframe (default daily, which is the universe DITP scans). Returns
    aggregate counts (fresh/stale/ancient/missing + consistency/validity
    failures) plus a single `overall` status ("ok"/"warn"/"critical") that
    the dashboard pill colors itself by.

    Pass `details=true` for full per-symbol lists of the stale / ancient /
    invalid symbols — used by the click-through modal.
    """
    try:
        import data_integrity  # type: ignore  # resources/data_integrity.py
        r = data_integrity.health_report(timeframe=timeframe)
        from dataclasses import asdict
        payload = asdict(r)
        if not details:
            # Trim per-symbol lists when caller doesn't need them; the pill
            # only needs counts + overall status.
            payload["stale_symbols"] = payload["stale_symbols"][:0]
            payload["ancient_symbols"] = payload["ancient_symbols"][:0]
            payload["invalid_symbols"] = payload["invalid_symbols"][:0]
        return JSONResponse(payload)
    except Exception as exc:
        # Never let the pill take down the page; degrade gracefully.
        return JSONResponse({"overall": "unknown", "error": str(exc),
                             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})


@app.get("/strategy/ditp/watchlist")
async def ditp_watchlist() -> JSONResponse:
    """Return the latest DITP P2 watchlist (highest-dated
    `state/watchlist_ditp_<date>.json`). The DITP tab in the dashboard's
    Strategy Analysis panel renders this since DITP's content model is
    end-of-day scanner output, not live journal events like GUNS.
    """
    try:
        state_dir = SKILL_DIR / "state"
        files = sorted(state_dir.glob("watchlist_ditp_*.json"))
        if not files:
            return JSONResponse({"candidates": [], "note": "no_watchlist_yet"})
        latest = files[-1]
        payload = json.loads(latest.read_text(encoding="utf-8"))
        payload["_file"] = latest.name
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"candidates": [], "error": str(exc)})


@app.post("/data/refresh-stale")
async def data_refresh_stale(timeframe: str = "daily") -> JSONResponse:
    """Re-fetch every stale / ancient / missing parquet via yfinance.
    Targeted recovery — only the symbols flagged by health_report() get
    pulled. Returns the per-symbol bars-written counts."""
    try:
        import data_integrity  # type: ignore
        # The yfinance pull blocks, so run it in a thread to keep the
        # dashboard responsive. Default loop executor is fine here.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: data_integrity.refresh_stale(timeframe=timeframe)
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/data/ingest-log")
async def data_ingest_log(tail: int = 50) -> JSONResponse:
    """Recent ingest log entries (most recent last). Read by the data-health
    modal's "recent ingest events" section."""
    try:
        import data_integrity  # type: ignore
        events = data_integrity._read_ingest_log_tail(tail)
        return JSONResponse({"events": events, "n": len(events)})
    except Exception as exc:
        return JSONResponse({"events": [], "error": str(exc)})


@app.get("/config")
async def config_view() -> JSONResponse:
    """Public view of harmless config knobs. Never leaks secrets — credentials
    live in VAULT, not in config.json."""
    cfg = _load_cfg()
    return JSONResponse({
        "auto_start_enabled": bool(cfg.get("auto_start_enabled", True)),
        "auto_start_et": str(cfg.get("auto_start_et", "08:30")),
    })


@app.post("/shutdown")
async def shutdown() -> JSONResponse:
    """Stop the dashboard process only. The bot stays alive."""
    async def _kill() -> None:
        await asyncio.sleep(0.3)  # let the HTTP response flush
        os._exit(0)
    asyncio.create_task(_kill())
    return JSONResponse({"status": "shutting down dashboard (bot unaffected)"})


@app.post("/restart")
async def restart() -> JSONResponse:
    """Exit with code 100. start_dashboard.bat's supervisor loop catches that
    and re-launches the dashboard. The bot is NOT touched — a code change
    picked up by the new dashboard inherits it as an orphan until it exits
    naturally."""
    async def _exit100() -> None:
        await asyncio.sleep(0.3)
        os._exit(100)
    asyncio.create_task(_exit100())
    return JSONResponse({"status": "restarting dashboard (children unaffected)"})


@app.post("/shutdown-all")
async def shutdown_all() -> JSONResponse:
    """Terminate the bot subprocess THEN exit the dashboard."""
    result = bot.stop()
    async def _kill() -> None:
        await asyncio.sleep(0.3)
        os._exit(0)
    asyncio.create_task(_kill())
    return JSONResponse({"status": "shutting down everything", "stopped": result})


@app.post("/bot/start")
async def bot_start() -> JSONResponse:
    result = bot.start()
    return JSONResponse(result)


@app.post("/bot/stop")
async def bot_stop() -> JSONResponse:
    result = bot.stop()
    return JSONResponse(result)


@app.get("/bot/status")
async def bot_status() -> JSONResponse:
    return JSONResponse({
        "status": bot.status,
        "pid": bot.pid,
        "enabled_strategies": bot.enable_map(),
        "any_enabled": bot.any_enabled(),
        "armed_strategies": bot.arm_map(),
        "any_armed": bot.any_armed(),
    })


async def _broadcast_gate_state() -> None:
    """Push the current per-strategy ON/OFF + ARM maps to every connected
    browser so toggles flip the UI immediately without waiting for the
    3s poll."""
    await hub.broadcast({"type": "health", "health": {
        "bot": {
            "status": bot.status, "pid": bot.pid,
            "enabled_strategies": bot.enable_map(),
            "any_enabled": bot.any_enabled(),
            "armed_strategies": bot.arm_map(),
            "any_armed": bot.any_armed(),
        },
    }})


# ---- ARM gate (submit-time check) ----

@app.get("/bot/arm")
async def bot_arm_get() -> JSONResponse:
    """Return the per-strategy ARM map. Keys are KNOWN_STRATEGIES; values
    are booleans (true = armed = bot will submit real orders for this
    strategy, provided the strategy is also ON)."""
    return JSONResponse({
        "armed_strategies": bot.arm_map(),
        "any_armed": bot.any_armed(),
        "known_strategies": list(KNOWN_STRATEGIES),
    })


@app.post("/bot/arm")
async def bot_arm(body: dict) -> JSONResponse:
    """Toggle ARM state. Body can be either:

      Per-strategy:     {"strategy": "guns_setup1", "armed": true}
      All-at-once:      {"all": true}    or   {"all": false}

    Changes apply LIVE to a running bot (the per-strategy flag is read
    on each entry attempt). No restart required.

    Note: ARM is independent of ON/OFF. A strategy that is ARMED but
    OFF will not fire (the pipeline doesn't run at all). The flag
    is remembered so flipping ON restores the prior ARMED state.
    """
    if "all" in body and "strategy" not in body:
        value = bool(body["all"])
        bot.set_all_armed(value)
        await _broadcast_gate_state()
        return JSONResponse({
            "armed_strategies": bot.arm_map(),
            "any_armed": bot.any_armed(),
            "applied": f"set_all_armed({value})",
        })
    strategy = body.get("strategy")
    if not strategy:
        return JSONResponse(
            {"error": "body must include 'strategy' (and 'armed'), "
                      "or 'all': bool"},
            status_code=400,
        )
    if strategy not in KNOWN_STRATEGIES:
        return JSONResponse(
            {"error": f"unknown strategy {strategy!r}; "
                      f"known: {list(KNOWN_STRATEGIES)}"},
            status_code=400,
        )
    if "armed" not in body:
        return JSONResponse(
            {"error": "body must include 'armed' bool"},
            status_code=400,
        )
    bot.set_strategy_armed(strategy, bool(body["armed"]))
    await _broadcast_gate_state()
    return JSONResponse({
        "armed_strategies": bot.arm_map(),
        "any_armed": bot.any_armed(),
        "applied": f"{strategy} armed={bool(body['armed'])}",
    })


# ---- ON/OFF gate (pipeline-runs check) ----

@app.get("/bot/enable")
async def bot_enable_get() -> JSONResponse:
    """Return the per-strategy ON/OFF map. true = strategy is ON
    (analysis pipeline runs at scheduled fire); false = OFF (no scanner,
    no resources, no analysis, no journal entries)."""
    return JSONResponse({
        "enabled_strategies": bot.enable_map(),
        "any_enabled": bot.any_enabled(),
        "known_strategies": list(KNOWN_STRATEGIES),
    })


@app.post("/bot/enable")
async def bot_enable(body: dict) -> JSONResponse:
    """Toggle ON/OFF state. Body shape matches /bot/arm:

      Per-strategy:     {"strategy": "guns_setup1", "enabled": true}
      All-at-once:      {"all": true}    or   {"all": false}

    Changes apply LIVE -- next scheduled fire honors the new state.
    A strategy currently in mid-fire completes its in-flight entry
    phase regardless (the check is at the TOP of _fire_strategy_entries).
    """
    if "all" in body and "strategy" not in body:
        value = bool(body["all"])
        bot.set_all_enabled(value)
        await _broadcast_gate_state()
        return JSONResponse({
            "enabled_strategies": bot.enable_map(),
            "any_enabled": bot.any_enabled(),
            "applied": f"set_all_enabled({value})",
        })
    strategy = body.get("strategy")
    if not strategy:
        return JSONResponse(
            {"error": "body must include 'strategy' (and 'enabled'), "
                      "or 'all': bool"},
            status_code=400,
        )
    if strategy not in KNOWN_STRATEGIES:
        return JSONResponse(
            {"error": f"unknown strategy {strategy!r}; "
                      f"known: {list(KNOWN_STRATEGIES)}"},
            status_code=400,
        )
    if "enabled" not in body:
        return JSONResponse(
            {"error": "body must include 'enabled' bool"},
            status_code=400,
        )
    bot.set_strategy_enabled(strategy, bool(body["enabled"]))
    await _broadcast_gate_state()
    return JSONResponse({
        "enabled_strategies": bot.enable_map(),
        "any_enabled": bot.any_enabled(),
        "applied": f"{strategy} enabled={bool(body['enabled'])}",
    })


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await hub.add(ws)
    try:
        await ws.send_json(_snapshot())
        while True:
            # Keep the connection open; ping if quiet.
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=25.0)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping",
                                    "ts": datetime.now(timezone.utc).isoformat()})
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(ws)


def main() -> None:
    print(f"intraday_bot dashboard at http://{HOST}:{PORT}")
    print("(Read-only observer. The bot is what places orders.)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
