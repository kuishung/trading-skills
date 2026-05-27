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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
    """Cheap TCP-bind probe — tells us only whether TWS's port is listening.

    Used by `_poll_health()` every 3s. We intentionally DO NOT do a real
    `ib.connect()` handshake here on a schedule — even with try/except,
    each failed handshake leaves asyncio cleanup events (ConnectionReset,
    TimeoutError) in the FastAPI main loop. Over a long-running session
    those accumulate and every endpoint starts taking ~2s. See user
    incident 2026-05-23.

    The "is the API actually responsive" question is answered on demand
    when something actually tries to use it (quote fetch, order submit) —
    those paths have their own error handling and don't poison the loop.
    """
    return _probe_ibkr_listener_bind()


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


@app.get("/profile/health")
async def profile_health(details: bool = False) -> JSONResponse:
    """Ticker-profile coverage + freshness summary for the dashboard pill.

    Reads every JSON under `data/ticker_profile/` and returns aggregate
    counts (total / fresh / stale / full / partial / no_daily) plus
    timestamps of the oldest / newest profile. Drives the "Profile
    health" pill in the status bar.

    Pass `details=true` to also get the full list of symbols whose
    `stats_3m_rth` section is populated (used by the click-through modal).
    """
    try:
        import ticker_profile  # type: ignore  # resources/ticker_profile.py
        h = ticker_profile.profile_health()
        if not details:
            # Trim symbol list when the pill is just polling; modal asks
            # for details=true when it opens.
            h["symbols_3m"] = []
        # Roll-up status: same colour buckets as the data-health pill.
        # ok       : ≥80% of profiles full + fresh
        # warn     : 50-80% full+fresh, OR there are stale profiles
        # critical : <50% full, or all stale
        n = max(h["n_total"], 1)
        full_fresh_ratio = (h["n_full"] - max(0, h["n_full"] - h["n_fresh"])) / n
        if h["n_total"] == 0:
            h["overall"] = "unknown"
        elif full_fresh_ratio >= 0.8:
            h["overall"] = "ok"
        elif full_fresh_ratio >= 0.5 or h["n_stale"] > 0:
            h["overall"] = "warn"
        else:
            h["overall"] = "critical"
        h["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return JSONResponse(h)
    except Exception as exc:
        return JSONResponse({"overall": "unknown", "error": str(exc),
                             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})


@app.post("/profile/refresh")
async def profile_refresh(scope: str = "watchlist") -> JSONResponse:
    """Trigger a profile refresh. `scope` options:
      "watchlist"  : symbols in today's DITP / GUNS watchlists (fast, ~10-30 tickers)
      "ingested"   : every symbol with local 1m parquet (~500-1500, slower)
      "universe"   : every symbol with daily parquet on disk (the full ~1519)

    Runs in a thread so the dashboard stays responsive. Returns the
    summary from `ticker_profile.refresh_many()`.
    """
    try:
        import ticker_profile  # type: ignore
        import bars_store      # type: ignore

        if scope == "watchlist":
            symbols: list[str] = []
            for p in sorted((SKILL_DIR / "state").glob("watchlist_*_*.json")):
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                    for c in obj.get("candidates", []):
                        s = c.get("symbol")
                        if s:
                            symbols.append(s.upper())
                except Exception:
                    pass
            # de-dup, keep order
            seen = set(); ordered = []
            for s in symbols:
                if s not in seen:
                    seen.add(s); ordered.append(s)
            symbols = ordered
        elif scope == "ingested":
            symbols = bars_store.list_symbols("1min")
        elif scope == "universe":
            symbols = bars_store.list_symbols("daily")
        else:
            return JSONResponse({"error": f"unknown scope {scope!r}"}, status_code=400)

        if not symbols:
            return JSONResponse({"n_total": 0, "n_ok": 0, "n_partial": 0,
                                 "n_failed": 0, "failures": [],
                                 "note": "no symbols in scope"})

        loop = asyncio.get_running_loop()
        summary = await loop.run_in_executor(
            None, lambda: ticker_profile.refresh_many(symbols, pacing_s=0.4)
        )
        summary["scope"] = scope
        return JSONResponse(summary)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/lists/all")
async def lists_all() -> JSONResponse:
    """Unified active-lists view across every strategy family. Returns
    `{candidates: [...], watchlist: [...]}`.

    Definitions (operational, not philosophical):
      * **watchlist**: every symbol on every `state/watchlist_*_*.{txt,json}`
        file the family scanners produced. The raw under-radar pool.
      * **candidates**: the SUBSET of watchlist whose strategy is currently
        ON + ARMED (i.e. the bot would actually fire on a trigger). Once
        the DITP intraday monitor lands, this also subtracts symbols whose
        intraday anti-pattern count has crossed the demotion threshold.

    Each row carries: symbol, last, chg, chg_pct, vol, strategy, tier?,
    variant?, resistance?. Quote data is fetched in one yfinance batch
    with a 30s in-process cache to stay below free-tier rate limits.
    """
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _build_lists_payload)
    return JSONResponse(payload)


# ---- /lists/all helpers (sync, run via executor) -------------------------

_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
# 5s cache for non-streaming sources (Alpaca / yfinance fallback). IBKR
# quotes don't go through this cache — they live in the streamer's in-memory
# `_LIVE_QUOTES` dict, updated continuously via streaming subscriptions, so
# `/lists/all` reads them in sub-millisecond.
_QUOTE_TTL_S = 5.0

# ---- IBKR streaming-quote background thread ----------------------------
#
# A single dedicated thread owns one persistent ib_insync `IB()` connection
# (clientId 99) and maintains streaming `reqMktData` subscriptions for every
# symbol currently in any state/watchlist_*_*.json. Tickers auto-update as
# trades print; we snapshot the dict every 2s to populate `_LIVE_QUOTES`.
# FastAPI handlers read from `_LIVE_QUOTES` directly — no per-request IBKR
# round-trip, no 20s cold-fetch latency.
#
# Why thread, not asyncio: ib_insync's `IB` instance is NOT thread-safe but
# DOES want its own event loop. Running it in its own thread with a private
# asyncio loop is the canonical way to use it alongside another async
# framework (FastAPI). Cross-thread communication = a lock + plain dict
# + an atomic "subscribed_symbols" set updated by the FastAPI side.

import threading

_STREAMER_LOCK = threading.Lock()
_STREAMER_THREAD: "threading.Thread | None" = None
# Ordered priority list — the streamer subscribes from the head up to the
# 95-slot cap. Candidates first, then watchlist non-candidates.
_STREAMER_PRIORITY: list[str] = []
_LIVE_QUOTES: dict[str, dict] = {}       # symbol -> {last, prev_close, chg, chg_pct, vol, ts}

# IBKR standard account allows ~100 simultaneous market data subscriptions
# (paper and many live accounts share this cap). We hold back 5 slots for
# ephemeral calls (probes, one-shot reqTickers) so the live streamer can't
# starve them out.
_STREAMER_MAX_SUBS = 95

_STREAMER_STATUS: dict = {
    "connected":     False,
    "last_update":   None,
    "subscribed_n":  0,
    "requested_n":   0,     # how many symbols the caller WANTED
    "capped":        False, # True when requested > max
    "max_subs":      _STREAMER_MAX_SUBS,
    "error":         None,
}


def _set_streamer_symbols(symbols) -> None:
    """Called by /lists/all on each request. Order matters — the head of
    the list wins when the count exceeds the IBKR subscription cap.

    The streamer thread reads `_STREAMER_PRIORITY` on its next reconcile
    (~2s) and adjusts subscriptions to match the first N entries (N=cap)."""
    global _STREAMER_PRIORITY
    seen: set[str] = set()
    ordered: list[str] = []
    for s in symbols:
        if not s:
            continue
        u = str(s).upper()
        if u in seen:
            continue
        seen.add(u)
        ordered.append(u)
    with _STREAMER_LOCK:
        _STREAMER_PRIORITY = ordered
        _STREAMER_STATUS["requested_n"] = len(ordered)
        _STREAMER_STATUS["capped"] = len(ordered) > _STREAMER_MAX_SUBS


# Kill-switch for the streaming thread. Set to True to disable IBKR
# streaming entirely — `/lists/all` falls through to the on-demand
# `_fetch_quotes_ibkr` (ephemeral reqTickers), then Alpaca-IEX, then
# yfinance. The streaming thread's uncaught asyncio exceptions
# (ConnectionResetError, "Peer closed connection" etc.) were
# destabilising the FastAPI process when TWS rejected connections
# (paper-disclaimer popup, clientId collision). Re-enabled with a
# custom asyncio exception handler that swallows the noise (see
# _streamer_main) so a bad connection cycle no longer crashes the
# dashboard. Re-flip to True if instability returns.
_STREAMER_DISABLED = False


_STREAMER_SHUTDOWN_REQUESTED = threading.Event()
_STREAMER_IB_REF: list = []  # one-slot reference to the live IB so atexit can drain


def _streamer_atexit_drain():
    """Best-effort: cancel every reqMktData + disconnect cleanly on
    process shutdown. TWS releases the market-data lines immediately
    instead of waiting for its orphan-session timeout (5-10 min)."""
    _STREAMER_SHUTDOWN_REQUESTED.set()
    if not _STREAMER_IB_REF:
        return
    ib = _STREAMER_IB_REF[0]
    try:
        if not ib.isConnected():
            return
        for tk in list(ib.tickers()):
            try: ib.cancelMktData(tk.contract)
            except Exception: pass
        try: ib.disconnect()
        except Exception: pass
    except Exception:
        pass


import atexit
atexit.register(_streamer_atexit_drain)


def _start_streamer_once() -> None:
    """Idempotent — starts the streamer thread on first call, no-op after."""
    if _STREAMER_DISABLED:
        _STREAMER_STATUS["error"] = "streaming disabled (see _STREAMER_DISABLED in server.py)"
        return
    global _STREAMER_THREAD
    if _STREAMER_THREAD is not None and _STREAMER_THREAD.is_alive():
        return
    _STREAMER_THREAD = threading.Thread(
        target=_streamer_main, daemon=True, name="ibkr-quote-streamer"
    )
    _STREAMER_THREAD.start()


def _streamer_main() -> None:
    """Background thread: persistent IBKR connection + streaming subscriptions.

    Lifecycle:
      1. Create a private asyncio loop for this thread (ib_insync needs one).
      2. Connect to TWS / Gateway via cfg.ibkr_port + clientId 99.
      3. Loop forever:
         a. Reconcile subscriptions vs `_STREAMER_SYMBOLS` (add new, drop gone).
         b. ib.sleep(2) — pump the event loop, let tickers update.
         c. Snapshot all live tickers into `_LIVE_QUOTES`.
      4. On disconnect: brief wait, reconnect.
    """
    import asyncio, math, time

    # Mute ib_insync's logger — it's chatty about every reconnect attempt
    # ("Paper trading disclaimer must first be accepted", "clientId 99
    # already in use?" etc.). They land in dashboard.log via the asyncio
    # exception handler below; we don't also need them on stderr where
    # they fill the supervisor's cmd window.
    import logging as _logging
    for _name in ("ib_insync", "ib_insync.client", "ib_insync.wrapper",
                  "ib_insync.ib", "ib_insync.event"):
        _logging.getLogger(_name).setLevel(_logging.CRITICAL)

    # Each ib_insync IB instance needs its own asyncio loop in its own thread.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Swallow asyncio noise from ib_insync. The library emits stack traces
    # via the default exception handler whenever a TCP connection drops
    # mid-flight (ConnectionResetError, "Peer closed connection",
    # TimeoutError). These come from transports tearing down AFTER we've
    # already detected the disconnect via ib.isConnected() and started a
    # reconnect cycle — they're noise, not a real error condition. The
    # default handler used to write them to dashboard.log; that wasn't
    # a problem in itself, but the volume + interaction with stdout
    # buffering across threads correlated with dashboard hangs. Quiet
    # handler keeps the streamer recovery path the only authority on
    # what an error means.
    def _quiet_async_exc_handler(_loop, context):
        msg = context.get("message") or ""
        exc = context.get("exception")
        if exc is None:
            return                # benign warning
        # Categorize so we can still surface unknown errors on stderr.
        ignored = (
            "ConnectionResetError", "TimeoutError",
            "ConnectionRefusedError", "OSError",
        )
        if type(exc).__name__ in ignored:
            return
        # Anything else: write a single line, no stack trace.
        sys.stderr.write(f"[streamer-asyncio] {type(exc).__name__}: {msg}\n")
    loop.set_exception_handler(_quiet_async_exc_handler)

    try:
        from ib_insync import IB, Stock  # type: ignore
        from _common import load_config  # type: ignore
    except ImportError as exc:
        _STREAMER_STATUS["error"] = f"import_failed: {exc}"
        return

    cfg = load_config()
    host = cfg.get("ibkr_host", "127.0.0.1")
    port = int(cfg.get("ibkr_port", 7497))

    ib = IB()
    # Park the IB instance where atexit can find it for shutdown cleanup.
    if _STREAMER_IB_REF:
        _STREAMER_IB_REF[0] = ib
    else:
        _STREAMER_IB_REF.append(ib)
    contract_by_sym: dict[str, object] = {}
    fail_streak = 0
    MAX_FAILS_BEFORE_SLEEP = 3       # then back off to 5-min retries
    LONG_BACKOFF_S = 300
    # Pool of clientIds to cycle through on consecutive failures. When TWS
    # has a half-broken session at clientId 99 (orphaned from a previous
    # crash), the slot stays locked for several minutes until TWS times
    # out. Trying a different clientId on each retry sidesteps that —
    # 100/101/102 are unlikely to be locked at the same time.
    CLIENT_IDS = [99, 100, 101, 102, 103]

    while True:
        # ---- Connect / reconnect ----
        if not ib.isConnected():
            contract_by_sym.clear()
            _STREAMER_STATUS["connected"] = False
            cid = CLIENT_IDS[fail_streak % len(CLIENT_IDS)]
            try:
                ib.connect(host, port, clientId=cid, timeout=8)
                _STREAMER_STATUS["connected"] = True
                _STREAMER_STATUS["error"] = None
                fail_streak = 0
            except Exception as exc:
                fail_streak += 1
                msg = str(exc) or type(exc).__name__
                _STREAMER_STATUS["error"] = f"clientId={cid}: {msg} (fail #{fail_streak})"
                # After repeated failures (TWS disclaimer not accepted,
                # clientId collision, etc.), back off hard so we don't
                # spam asyncio errors into the FastAPI process. Quotes
                # fall through to Alpaca silently while this is the case.
                if fail_streak >= MAX_FAILS_BEFORE_SLEEP:
                    time.sleep(LONG_BACKOFF_S)
                else:
                    time.sleep(15)
                continue

        # ---- Reconcile subscriptions ----
        try:
            with _STREAMER_LOCK:
                # Take the first N entries of the priority list, where N is
                # the IBKR subscription cap. Candidates come first, then the
                # rest of the watchlist — so when the universe outgrows the
                # cap, the watch-only tail gets dropped, not the actively-
                # traded names.
                want = set(_STREAMER_PRIORITY[:_STREAMER_MAX_SUBS])
            have = set(contract_by_sym.keys())
            to_add = want - have
            to_remove = have - want

            if to_add:
                contracts = [Stock(s, "SMART", "USD") for s in to_add]
                try:
                    qualified = ib.qualifyContracts(*contracts)
                except Exception as exc:
                    _STREAMER_STATUS["error"] = f"qualify: {exc}"
                    qualified = []
                for c in qualified:
                    sym = getattr(c, "symbol", "").upper()
                    if not sym:
                        continue
                    try:
                        ib.reqMktData(c, "", False, False)
                        contract_by_sym[sym] = c
                    except Exception:
                        continue

            if to_remove:
                for sym in to_remove:
                    c = contract_by_sym.pop(sym, None)
                    if c is None:
                        continue
                    try:
                        ib.cancelMktData(c)
                    except Exception:
                        pass

            _STREAMER_STATUS["subscribed_n"] = len(contract_by_sym)

            # ---- Pump event loop (2s) — lets ticker updates flow in ----
            ib.sleep(2)

            # ---- Snapshot tickers into the live cache ----
            now_ts = time.time()
            updated: dict[str, dict] = {}
            for tk in ib.tickers():
                sym = getattr(tk.contract, "symbol", "").upper()
                if not sym or sym not in contract_by_sym:
                    continue
                row = _ticker_to_quote_row(tk, math)
                if row is None:
                    continue
                row["ts"] = now_ts
                updated[sym] = row
            if updated:
                with _STREAMER_LOCK:
                    _LIVE_QUOTES.update(updated)
                _STREAMER_STATUS["last_update"] = now_ts

        except Exception as exc:
            # Don't let a single hiccup kill the thread.
            _STREAMER_STATUS["error"] = f"loop: {exc}"
            # Clean cancel of every subscription before disconnect — this
            # tells TWS to release the market-data line immediately rather
            # than wait for its orphan-session timeout (5-10 min). Without
            # this, repeated reconnects accumulate phantom subscriptions
            # in TWS's accounting (the "112 tickers" symptom from user
            # report 2026-05-23).
            for sym, c in list(contract_by_sym.items()):
                try: ib.cancelMktData(c)
                except Exception: pass
            contract_by_sym.clear()
            try: ib.disconnect()
            except Exception: pass
            time.sleep(5)


def _ticker_to_quote_row(tk, math) -> dict | None:
    """Extract a quote row from an ib_insync Ticker. Returns None if there's
    no usable data (e.g. just-subscribed, still waiting for first tick).
    Reused logic from `_fetch_quotes_ibkr`."""
    try:
        last = tk.last
        if last is None or (isinstance(last, float) and math.isnan(last)):
            last = tk.close
        prev = tk.close
        if last is None or prev is None:
            return None
        if isinstance(last, float) and math.isnan(last):
            return None
        if isinstance(prev, float) and math.isnan(prev):
            return None
        last = float(last); prev = float(prev)
        if prev <= 0:
            return None
        chg = last - prev
        chg_pct = (chg / prev * 100.0) if prev > 0 else 0.0
        vol_raw = tk.volume
        if vol_raw is None or (isinstance(vol_raw, float) and math.isnan(vol_raw)):
            v = 0
        else:
            v = int(float(vol_raw) * 100)
        return {
            "last":       round(last, 4),
            "prev_close": round(prev, 4),
            "chg":        round(chg, 4),
            "chg_pct":    round(chg_pct, 3),
            "vol":        v,
        }
    except Exception:
        return None


def _streamer_get(symbols: list[str]) -> dict[str, dict]:
    """Read the streamer cache for the given symbols. Returns whatever is
    available (no waiting). Symbols missing from the cache simply aren't
    in the result — caller falls back to the next quote source."""
    out: dict[str, dict] = {}
    with _STREAMER_LOCK:
        for s in symbols:
            row = _LIVE_QUOTES.get(s.upper())
            if row is not None:
                out[s.upper()] = dict(row)   # shallow copy
    return out


def _aggregate_watchlist_rows() -> list[dict]:
    """Scan state/watchlist_*_*.{txt,json} -> list of rows tagged with
    the strategy family that produced the file.

    Filename convention (set by each family scanner):
        watchlist_<family>_<YYYY-MM-DD>.{txt,json}
    Family is the second token, lowercased. txt is line-oriented
    (SYM[<tab>extras]); json is the rich version with tier/variant.
    """
    state_dir = SKILL_DIR / "state"
    rows: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}   # (symbol, family) -> row

    # Prefer JSON files first; their richer fields populate the row.
    for path in sorted(state_dir.glob("watchlist_*_*.json")):
        try:
            parts = path.stem.split("_")
            family = parts[1].lower() if len(parts) >= 2 else "unknown"
        except Exception:
            family = "unknown"
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in obj.get("candidates", []):
            sym = (c.get("symbol") or "").upper()
            if not sym:
                continue
            key = (sym, family)
            seen[key] = {
                "symbol":   sym,
                "strategy": family,
                "tier":     c.get("tier"),
                "variant":  c.get("variant"),
                "resistance": c.get("resistance"),
                "source":   path.name,
            }

    # Then .txt files for families that only emit text (GUNS today).
    for path in sorted(state_dir.glob("watchlist_*_*.txt")):
        try:
            parts = path.stem.split("_")
            family = parts[1].lower() if len(parts) >= 2 else "unknown"
        except Exception:
            family = "unknown"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                sym = line.split()[0].upper()
                if not sym:
                    continue
                key = (sym, family)
                if key in seen:
                    continue       # JSON entry wins
                seen[key] = {
                    "symbol":   sym,
                    "strategy": family,
                    "source":   path.name,
                }
        except Exception:
            continue

    return list(seen.values())


def _fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """Layered REAL-TIME quote fetch with 30s in-process cache.

    Priority chain (each row's `source` field records which one served it):
      1. IBKR streaming cache (clientId 99, real-time, full consolidated tape)
      2. IBKR ephemeral snapshot (warm-up window, before streamer is hot)
      3. Alpaca IEX (real-time price; volume is IEX-only — marked as such)

    yfinance is **NOT** in this chain by user rule 2026-05-23: *"the real
    time data we cannot use yFinance, we have to only rely to IBKR and
    Alpaca only"*. yfinance is 15-min delayed on the free tier — not
    safe to drive intraday decisions or display as a live quote. The
    `_fetch_quotes_yfinance` function below is kept callable for
    non-realtime consumers (ticker_profile daily-bar refresh, etc.) but
    is never invoked from this real-time path.

    Returns {SYM: {last, prev_close, chg, chg_pct, vol, source}}. When
    neither IBKR nor Alpaca can serve a symbol, the row is omitted from
    the result — the row shows in the UI with `last: null` and a
    `no quote` tag, which is the honest signal that we have no live
    data for it right now.
    """
    import time
    now = time.time()
    out: dict[str, dict] = {}
    needed: list[str] = []
    for s in symbols:
        s = s.upper()
        c = _QUOTE_CACHE.get(s)
        if c and (now - c[0]) < _QUOTE_TTL_S:
            out[s] = c[1]
        else:
            needed.append(s)
    if not needed:
        return out

    # --- 1. IBKR streaming cache (primary, real-time) ---
    # The streamer thread maintains persistent subscriptions and updates
    # `_LIVE_QUOTES` every ~2s. Reads here are instant. Symbols just-added
    # to the watchlist may not have data yet (streamer takes 1-2 reconcile
    # cycles to qualify + populate); those fall through to the next source.
    streamer_rows = _streamer_get(needed)
    for sym, row in streamer_rows.items():
        row["source"] = "ibkr"
        # NOT cached in _QUOTE_CACHE — we want to re-read the live dict each
        # call so updates appear immediately.
        out[sym] = row

    still_needed = [s for s in needed if s not in out]
    if not still_needed:
        return out

    # --- 1b. IBKR ephemeral snapshot ---
    # Warm-up path — runs only when the streamer is enabled but hasn't yet
    # connected (typical for the first 5-10 seconds after dashboard start).
    # Once the streamer is connected, this path is skipped entirely; once
    # it's gone for a long time, we don't keep poking IBKR — Alpaca/yfinance
    # fallback handles it.
    if (not _STREAMER_DISABLED) and (not _STREAMER_STATUS.get("connected")):
        try:
            ibkr_rows = _fetch_quotes_ibkr(still_needed)
        except Exception:
            ibkr_rows = {}
        for sym, row in ibkr_rows.items():
            row["source"] = "ibkr"
            _QUOTE_CACHE[sym] = (now, row)
            out[sym] = row
        still_needed = [s for s in needed if s not in out]
        if not still_needed:
            return out

    # --- 2. Alpaca IEX fallback ---
    try:
        alpaca_rows = _fetch_quotes_alpaca(still_needed)
    except Exception:
        alpaca_rows = {}
    for sym, row in alpaca_rows.items():
        row["source"] = "alpaca_iex"
        _QUOTE_CACHE[sym] = (now, row)
        out[sym] = row

    # yfinance is deliberately NOT in the real-time chain — see this
    # function's docstring + user rule 2026-05-23. Symbols neither IBKR
    # nor Alpaca could serve return without a `last` value; the UI
    # surfaces this as `no quote` on the row so the user knows the data
    # is missing, not stale.
    return out


def _fetch_quotes_ibkr(symbols: list[str]) -> dict[str, dict]:
    """Snapshot quotes from IBKR via ib_insync. Connects ephemerally per
    call (~1-2s overhead) using the dashboard's reserved clientId 99
    so it can coexist with the live bot (clientId 71). Soft-fails to
    empty dict if TWS / Gateway is down or any contract can't qualify.
    """
    if not symbols:
        return {}
    try:
        from ib_insync import IB, Stock  # type: ignore
    except ImportError:
        return {}
    try:
        from _common import load_config  # type: ignore
    except ImportError:
        return {}
    cfg = load_config()
    host = cfg.get("ibkr_host", "127.0.0.1")
    port = int(cfg.get("ibkr_port", 7497))

    ib = IB()
    try:
        ib.connect(host, port, clientId=99, timeout=4)
    except Exception:
        return {}

    try:
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        try:
            qualified = ib.qualifyContracts(*contracts)
        except Exception:
            qualified = []
        if not qualified:
            return {}

        # reqTickers is the synchronous snapshot wrapper — blocks until each
        # contract's ticker has populated. More reliable than reqMktData +
        # ib.sleep, which silently times out on most symbols when batching
        # 5+ subscriptions at once.
        try:
            tickers = ib.reqTickers(*qualified)
        except Exception:
            return {}

        out: dict[str, dict] = {}
        import math
        for tk in tickers:
            try:
                sym = tk.contract.symbol.upper()
                last = tk.last
                if last is None or (isinstance(last, float) and math.isnan(last)):
                    # No live tick (off-hours, or no data subscription) →
                    # use yesterday's close as the "current" reference.
                    last = tk.close
                prev = tk.close
                if last is None or prev is None:
                    continue
                if isinstance(last, float) and math.isnan(last):
                    continue
                if isinstance(prev, float) and math.isnan(prev):
                    continue
                last = float(last)
                prev = float(prev)
                if prev <= 0:
                    continue
                chg = last - prev
                chg_pct = (chg / prev * 100.0) if prev > 0 else 0.0
                # IBKR volume is in 100-share lots; multiply.
                vol_raw = tk.volume
                if vol_raw is None or (isinstance(vol_raw, float) and math.isnan(vol_raw)):
                    v = 0
                else:
                    v = int(float(vol_raw) * 100)
                out[sym] = {
                    "last":       round(last, 4),
                    "prev_close": round(prev, 4),
                    "chg":        round(chg, 4),
                    "chg_pct":    round(chg_pct, 3),
                    "vol":        v,
                }
            except Exception:
                continue
        return out
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _fetch_quotes_alpaca(symbols: list[str]) -> dict[str, dict]:
    """Alpaca IEX feed via REST. Free-tier paper accounts have IEX by
    default; price is real-time, volume reflects IEX trades only.

    Reads credentials via the existing alpaca-skill-path adapter so we
    don't duplicate env-loading logic. Soft-fails to empty dict on any
    issue (creds missing, network, etc).
    """
    if not symbols:
        return {}
    try:
        import urllib.request, urllib.parse
        from _common import load_config  # type: ignore
        cfg = load_config()
    except Exception:
        return {}

    # Best-effort key lookup — mirrors what alpaca-trader-paper does.
    api_key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    api_secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("APCA_API_SECRET_KEY")
    if not api_key or not api_secret:
        # Try VAULT loader path (intraday-bot self-contained credential resolution)
        try:
            from _common import load_vendor_env  # type: ignore
            env = load_vendor_env("alpaca")
            api_key = env.get("APCA_API_KEY_ID")
            api_secret = env.get("APCA_API_SECRET_KEY")
        except Exception:
            pass
    if not api_key or not api_secret:
        return {}

    base = "https://data.alpaca.markets/v2/stocks"
    syms_csv = ",".join(symbols)
    # Latest trade endpoint — one round-trip for all symbols.
    url = f"{base}/trades/latest?{urllib.parse.urlencode({'symbols': syms_csv, 'feed': 'iex'})}"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID":     api_key,
        "APCA-API-SECRET-KEY": api_secret,
    })
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}
    trades = data.get("trades", {}) or {}

    # Need previous close for chg calc — pull a 2-day daily bar batch
    url2 = (f"{base}/bars?"
            + urllib.parse.urlencode({
                "symbols":  syms_csv,
                "timeframe": "1Day",
                "limit":    "2",
                "adjustment": "raw",
                "feed":     "iex",
            }))
    req2 = urllib.request.Request(url2, headers={
        "APCA-API-KEY-ID":     api_key,
        "APCA-API-SECRET-KEY": api_secret,
    })
    try:
        with urllib.request.urlopen(req2, timeout=4) as resp:
            bars_data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        bars_data = {"bars": {}}
    bars = bars_data.get("bars", {}) or {}

    out: dict[str, dict] = {}
    for sym in symbols:
        sym = sym.upper()
        tr = trades.get(sym)
        if not tr:
            continue
        last = tr.get("p")
        v = int(tr.get("s", 0) or 0)
        sym_bars = bars.get(sym) or []
        prev = float(sym_bars[-2]["c"]) if len(sym_bars) >= 2 \
            else (float(sym_bars[-1]["c"]) if sym_bars else None)
        if last is None or prev is None:
            continue
        try:
            last = float(last); prev = float(prev)
        except (TypeError, ValueError):
            continue
        chg = last - prev
        chg_pct = (chg / prev * 100.0) if prev > 0 else 0.0
        out[sym] = {
            "last":       round(last, 4),
            "prev_close": round(prev, 4),
            "chg":        round(chg, 4),
            "chg_pct":    round(chg_pct, 3),
            "vol":        v,   # IEX-only — caller's `source` field marks this
        }
    return out


def _fetch_quotes_yfinance(symbols: list[str]) -> dict[str, dict]:
    """Last-resort: yfinance batch download. 15-min delayed on free tier."""
    if not symbols:
        return {}
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return {}
    try:
        df = yf.download(" ".join(symbols), period="2d", interval="1d",
                         progress=False, auto_adjust=False, threads=False)
    except Exception:
        return {}
    if df is None or df.empty:
        return {}

    def _series_for(sym: str, field: str):
        try:
            if (field, sym) in df.columns:
                return df[(field, sym)].dropna()
            if (sym, field) in df.columns:
                return df[(sym, field)].dropna()
            if field in df.columns and len(symbols) == 1:
                return df[field].dropna()
        except Exception:
            pass
        return None

    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            close = _series_for(sym, "Close")
            vol   = _series_for(sym, "Volume")
            if close is None or len(close) == 0:
                continue
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) >= 2 else last
            chg = last - prev
            chg_pct = (chg / prev * 100.0) if prev > 0 else 0.0
            v = int(vol.iloc[-1]) if vol is not None and len(vol) > 0 else 0
            out[sym] = {
                "last":       round(last, 4),
                "prev_close": round(prev, 4),
                "chg":        round(chg, 4),
                "chg_pct":    round(chg_pct, 3),
                "vol":        v,
            }
        except Exception:
            continue
    return out


def _strategy_armed(family: str) -> bool:
    """True if the family's primary strategy is currently ARMED.
    Conservative: if any setup within the family is armed, the family is
    armed for the purpose of the candidates view.
    """
    try:
        import _gating  # type: ignore  scripts/_gating.py
    except ImportError:
        return False
    try:
        from strategy import KNOWN_STRATEGIES  # type: ignore
    except ImportError:
        return False
    for name in KNOWN_STRATEGIES:
        if not name.lower().startswith(family.lower()):
            continue
        try:
            if _gating.is_enabled(name) and _gating.is_armed(name):
                return True
        except Exception:
            continue
    return False


def _build_lists_payload() -> dict:
    rows = _aggregate_watchlist_rows()
    # Per-family armed status — needed BOTH for the candidates split below
    # AND for ordering the streamer-priority list (candidates first).
    families = sorted({r["strategy"] for r in rows})
    armed_map = {f: _strategy_armed(f) for f in families}

    # Streamer priority: armed strategies' symbols first (the "candidates"
    # set), then the rest of the watchlist. When the IBKR 100-line cap is
    # hit, the watch-only tail drops — the actively-traded names always
    # keep their live subscription.
    cand_syms: list[str] = []
    rest_syms: list[str] = []
    seen: set[str] = set()
    for r in rows:
        s = (r.get("symbol") or "").upper()
        if not s or s in seen:
            continue
        seen.add(s)
        if armed_map.get(r["strategy"], False):
            cand_syms.append(s)
        else:
            rest_syms.append(s)
    ordered_syms = cand_syms + rest_syms

    # Sentiment ETFs (SPY/QQQ/IWM/DIA, VXX, UUP/TLT/HYG/GLD, 11 sectors)
    # always go first — they drive the top-of-page Market Sentiment panel
    # and need live quotes regardless of which strategy is armed.
    sentiment_first = [s for s in SENTIMENT_SYMBOLS if s not in seen]
    ordered_with_sentiment = sentiment_first + ordered_syms

    if ordered_with_sentiment:
        _set_streamer_symbols(ordered_with_sentiment)
        _start_streamer_once()

    syms = sorted(seen)
    quotes = _fetch_quotes(syms) if syms else {}
    for r in rows:
        q = quotes.get(r["symbol"], {})
        r["last"]    = q.get("last")
        r["chg"]     = q.get("chg")
        r["chg_pct"] = q.get("chg_pct")
        r["vol"]     = q.get("vol")
        r["source"]  = q.get("source")   # 'ibkr' | 'alpaca_iex' | 'yfinance' | None

    # Quote-source coverage for the active-lists header pill.
    by_source: dict[str, int] = {}
    for r in rows:
        s = r.get("source") or "none"
        by_source[s] = by_source.get(s, 0) + 1

    watchlist = sorted(rows, key=lambda r: (r["strategy"], r["symbol"]))
    candidates = [r for r in watchlist if armed_map.get(r["strategy"], False)]

    streamer_snapshot = {
        "connected":    _STREAMER_STATUS.get("connected"),
        "subscribed_n": _STREAMER_STATUS.get("subscribed_n", 0),
        "requested_n":  _STREAMER_STATUS.get("requested_n", 0),
        "max_subs":     _STREAMER_STATUS.get("max_subs", _STREAMER_MAX_SUBS),
        "capped":       _STREAMER_STATUS.get("capped", False),
        "last_update":  _STREAMER_STATUS.get("last_update"),
        "error":        _STREAMER_STATUS.get("error"),
    }
    return {
        "candidates":    candidates,
        "watchlist":     watchlist,
        "armed_map":     armed_map,
        "quote_sources": by_source,
        "streamer":      streamer_snapshot,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


_MARKET_CLOCK_CACHE: dict = {"ts": 0.0, "payload": None}
_MARKET_CLOCK_TTL_S = 30.0


def _alpaca_market_clock() -> dict:
    """Query Alpaca's `/v2/clock` (free, authoritative US equity market
    state — handles weekends, federal holidays, and early closes that
    a pure time-of-day check can't know about). Cached 30s.

    Returns:
      {is_open, timestamp, next_open, next_close, source}
    `source` is "alpaca" on success, "fallback" if Alpaca creds missing
    or unreachable (caller should then compute client-side)."""
    import time
    now = time.time()
    cached = _MARKET_CLOCK_CACHE.get("payload")
    if cached and (now - _MARKET_CLOCK_CACHE["ts"]) < _MARKET_CLOCK_TTL_S:
        return cached

    client = _get_alpaca_client()
    if client is None:
        result = {"is_open": None, "source": "fallback",
                  "reason": "no_alpaca_credentials"}
        _MARKET_CLOCK_CACHE.update(ts=now, payload=result)
        return result
    try:
        c = client.get_clock()
        result = {
            "is_open":    bool(c.is_open),
            "timestamp":  c.timestamp.isoformat() if c.timestamp else None,
            "next_open":  c.next_open.isoformat()  if c.next_open  else None,
            "next_close": c.next_close.isoformat() if c.next_close else None,
            "source":     "alpaca",
        }
    except Exception as exc:
        result = {"is_open": None, "source": "fallback",
                  "reason": f"alpaca_clock_failed: {exc}"}
    _MARKET_CLOCK_CACHE.update(ts=now, payload=result)
    return result


# ----- Market Sentiment panel (Option D — full composite) -----------
#
# Universe of "sentiment feed" symbols. Always Tier-1 priority on the
# streamer, so they have live IBKR quotes regardless of strategy state.
# 4 indices + 1 vol proxy + 4 macro + 11 SPDR sectors = 20 symbols.
SENTIMENT_INDICES   = ["SPY", "QQQ", "IWM", "DIA"]
SENTIMENT_VOLATILITY = ["VXX"]              # VIX proxy ETF (real VIX is INDEX/CBOE, requires special contract handling — VXX is sufficient as a directional gauge)
SENTIMENT_MACRO     = ["UUP", "TLT", "HYG", "GLD"]   # USD-bull proxy, long bonds, high-yield credit, gold
SENTIMENT_SECTORS   = [
    "XLK", "XLY", "XLC", "XLF", "XLI", "XLB",       # risk-on
    "XLE", "XLP", "XLV", "XLU", "XLRE",             # mixed / defensive
]
SENTIMENT_SYMBOLS = SENTIMENT_INDICES + SENTIMENT_VOLATILITY + SENTIMENT_MACRO + SENTIMENT_SECTORS

# Risk-on vs risk-off sectors — for the composite "sectors" sub-score.
RISK_ON_SECTORS  = {"XLK", "XLY", "XLC", "XLF", "XLI", "XLB"}
RISK_OFF_SECTORS = {"XLP", "XLV", "XLU"}            # classic defensives

# Tooltips shown on hover for each cell. Used by the frontend; sent in
# the payload so the explanations stay in sync with the data.
SENTIMENT_TIPS = {
    "SPY":  "S&P 500 ETF — broadest US large-cap index. Today's % change vs prior close. Green = risk-on day. The benchmark.",
    "QQQ":  "Nasdaq-100 ETF — tech-heavy. Typically leads SPY in growth-led rallies and lags in defensive rotations. Divergence vs SPY is informative.",
    "IWM":  "Russell 2000 small-cap ETF. Leading indicator: small caps lead in early-cycle bull markets and lag in late-cycle / defensive moves.",
    "DIA":  "Dow Jones 30 — mega-cap blue-chip industrials. Less broad than SPY; useful as a sanity check on the index move.",
    "VXX":  "VIX short-term futures ETF (proxy for VIX). Up = volatility/fear expanding. Down = vol contracting. <15 VIX-equivalent = calm regime where breakouts work; >25 = chop / mean-reversion regime.",
    "UUP":  "Invesco DB US Dollar Index Bullish ETF (DXY proxy). Up = USD strengthening. Typically inverse to equities — strong dollar = headwind for US multinationals + emerging markets.",
    "TLT":  "iShares 20+ Year Treasury Bond ETF. Long-duration safe haven. Up = yields down = risk-off rotation. Down = yields up = inflation / risk-on.",
    "HYG":  "iShares High-Yield Corporate Bond ETF (junk bonds). Risk-on credit gauge. Up = credit healthy, risk appetite expanding. Down = credit stress, early recession warning.",
    "GLD":  "SPDR Gold Shares ETF. Safe haven + inflation hedge. Up = fear or USD weakness. Use as context, not signal.",
    "XLK":  "Tech sector ETF — RISK-ON. Growth-led, leads in bull markets. Up when investors are bullish on long-duration earnings.",
    "XLY":  "Consumer Discretionary ETF — RISK-ON. Cyclical, up when consumers feel confident. Strong economy signal.",
    "XLC":  "Communication Services ETF — RISK-ON. Internet + media + telecom. Tied to tech sentiment + advertising spend.",
    "XLF":  "Financials ETF — RISK-ON. Banks + brokers. Up with rising yields and economic activity.",
    "XLI":  "Industrials ETF — cyclical. Strength signals capex / business confidence.",
    "XLB":  "Materials ETF — cyclical. Commodities + chemicals. Economic activity proxy.",
    "XLE":  "Energy ETF — cyclical, commodity-driven. Up with oil prices. Risk-on when oil demand is healthy.",
    "XLP":  "Consumer Staples ETF — RISK-OFF. Defensive. People buy toilet paper regardless of economy. Up = defensive rotation, bearish signal.",
    "XLV":  "Healthcare ETF — DEFENSIVE. People need pills regardless of economy. Up = defensive rotation.",
    "XLU":  "Utilities ETF — RISK-OFF. Bond-proxy. Up = yields down + defensive rotation = bearish signal.",
    "XLRE": "Real Estate ETF (REITs). Rate-sensitive. Up with falling yields, down with rising. Mixed risk signal.",
    "advances":         "S&P 500 names trading UP today vs yesterday's close. Compared to declines to gauge breadth of the move. Strong tape needs >300 advances.",
    "declines":         "S&P 500 names trading DOWN today vs yesterday's close. >300 declines while SPY is positive = narrow leadership = fragile move.",
    "new_highs":        "S&P 500 names making a new 52-week high today. Conviction signal — strong tape concentrates buying at the high.",
    "new_lows":         "S&P 500 names making a new 52-week low today. Divergence warning — if SPY positive but new lows expanding, expect a top within weeks.",
    "pct_above_50sma":  "% of S&P 500 names above their 50-day SMA. >70% = strong uptrend regime (breakouts work). <30% = downtrend regime (long setups fail).",
    "pct_above_200sma": "% of S&P 500 names above their 200-day SMA. Structural bull/bear demarcation. >60% = bull market. <40% = bear market.",
    "composite":        "Weighted blend of all sub-scores: 30% indices · 20% A/D · 15% MAs · 15% VIX · 10% sectors · 10% new H/L. Range -100 (max bearish) to +100 (max bullish). >+40 = strong tailwind for breakouts; <-40 = defensive day.",
    "score_indices":    "Avg %-change of SPY/QQQ/IWM/DIA × 20. +40 ≈ +2% broad-market day. -40 ≈ -2% day.",
    "score_ad":         "(advances − declines) / 500 × 200. +80 = 350 vs 150 (very broad). -80 = 150 vs 350.",
    "score_mas":        "Avg of (%above_50SMA − 50) + (%above_200SMA − 50) / 2. Centered on 50% (no skew).",
    "score_vix":        "(20 − VIX) × 4. VIX 12 → +32. VIX 25 → -20. VIX 35 → -60. Calm market = positive contribution.",
    "score_sectors":    "(n_risk_on_green − n_risk_off_green) × 10. Risk-on: XLK XLY XLC XLF XLI XLB. Risk-off: XLP XLV XLU.",
    "score_newHL":      "(new_highs − new_lows) / 500 × 200. Concentration of conviction at the top vs bottom of yearly ranges.",
}


def _sentiment_breadth_from_parquets() -> dict:
    """Compute S&P 500 breadth metrics from daily parquets. Cached 60s.
    Result fields:
      advances / declines           (count vs prev close)
      new_highs / new_lows          (today's high/low vs 252d rolling)
      pct_above_50sma               (today's close vs 50-day SMA)
      pct_above_200sma              (today's close vs 200-day SMA)
      n_universe                    (S&P 500 names actually loaded)
    Off-hours: "today" = most recent daily bar on disk.
    """
    import time
    cached = _SENTIMENT_BREADTH_CACHE.get("payload")
    if cached and (time.time() - _SENTIMENT_BREADTH_CACHE["ts"]) < 60.0:
        return cached
    try:
        import bars_store      # type: ignore
        import sp500            # type: ignore  resources/sp500.py
    except ImportError:
        return {"error": "sp500_module_missing", "n_universe": 0}

    try:
        symbols = sp500.get_sp500_symbols()
    except Exception:
        symbols = []
    adv = dec = nh = nl = above50 = above200 = n_universe = 0
    for sym in symbols:
        bars = bars_store.load_bars(sym, timeframe="daily")
        if len(bars) < 2:
            continue
        n_universe += 1
        today = bars[-1]
        prev  = bars[-2]
        if today["c"] > prev["c"]: adv += 1
        elif today["c"] < prev["c"]: dec += 1
        # 52-week (252 trading days) rolling extremes — exclude today's bar
        window_252 = bars[-253:-1] if len(bars) >= 253 else bars[:-1]
        if window_252:
            max_h = max(b["h"] for b in window_252)
            min_l = min(b["l"] for b in window_252)
            if today["h"] > max_h: nh += 1
            if today["l"] < min_l: nl += 1
        # SMAs
        if len(bars) >= 50:
            sma50 = sum(b["c"] for b in bars[-50:]) / 50
            if today["c"] > sma50: above50 += 1
        if len(bars) >= 200:
            sma200 = sum(b["c"] for b in bars[-200:]) / 200
            if today["c"] > sma200: above200 += 1
    out = {
        "advances":         adv,
        "declines":         dec,
        "new_highs":        nh,
        "new_lows":         nl,
        "pct_above_50sma":  round(above50  / n_universe * 100, 1) if n_universe else 0,
        "pct_above_200sma": round(above200 / n_universe * 100, 1) if n_universe else 0,
        "n_universe":       n_universe,
    }
    _SENTIMENT_BREADTH_CACHE["payload"] = out
    _SENTIMENT_BREADTH_CACHE["ts"] = time.time()
    return out


_SENTIMENT_BREADTH_CACHE: dict = {"ts": 0.0, "payload": None}


def _build_sentiment_payload() -> dict:
    """Assemble the full /market/sentiment panel payload — live quotes
    for the 20 sentiment ETFs + breadth from S&P 500 parquets + composite
    score. Synchronous; runs in executor."""
    # 1. Quotes for all 20 sentiment symbols via the streamer cache.
    quotes = _fetch_quotes(SENTIMENT_SYMBOLS)

    def _row(sym):
        q = quotes.get(sym.upper(), {})
        return {
            "symbol":  sym,
            "last":    q.get("last"),
            "chg":     q.get("chg"),
            "chg_pct": q.get("chg_pct"),
            "source":  q.get("source"),
            "tip":     SENTIMENT_TIPS.get(sym, ""),
        }
    indices    = [_row(s) for s in SENTIMENT_INDICES]
    volatility = [_row(s) for s in SENTIMENT_VOLATILITY]
    macro      = [_row(s) for s in SENTIMENT_MACRO]
    sectors    = [_row(s) for s in SENTIMENT_SECTORS]

    # 2. Breadth from parquets.
    breadth = _sentiment_breadth_from_parquets()

    # 3. Sub-scores + composite.
    def _safe(v, default=0.0):
        try: return float(v) if v is not None else default
        except (TypeError, ValueError): return default

    # indices avg %
    idx_pcts = [_safe(r["chg_pct"]) for r in indices if r["chg_pct"] is not None]
    avg_idx_pct = sum(idx_pcts) / len(idx_pcts) if idx_pcts else 0.0
    score_indices = max(-100, min(100, avg_idx_pct * 20))

    adv  = breadth.get("advances", 0)
    dec  = breadth.get("declines", 0)
    score_ad = max(-100, min(100, (adv - dec) / 500 * 200))

    p50  = breadth.get("pct_above_50sma", 0)
    p200 = breadth.get("pct_above_200sma", 0)
    score_mas = max(-100, min(100, ((p50 - 50) + (p200 - 50)) / 2))

    # VIX proxy — VXX. Translate VXX dollar level to VIX-equivalent very
    # crudely: not a tight relationship, but +ve VXX % = +ve VIX % usually.
    # Use VXX's % change as a "vol getting worse" proxy.
    vxx_pct = _safe(volatility[0]["chg_pct"]) if volatility else 0.0
    # vxx up 5% ≈ "vol regime worsening" → -20; vxx down 5% → +20.
    score_vix = max(-100, min(100, -vxx_pct * 4))

    on_green  = sum(1 for s in sectors if s["symbol"] in RISK_ON_SECTORS  and _safe(s["chg_pct"]) > 0)
    off_green = sum(1 for s in sectors if s["symbol"] in RISK_OFF_SECTORS and _safe(s["chg_pct"]) > 0)
    score_sectors = max(-100, min(100, (on_green - off_green) * 10))

    nh = breadth.get("new_highs", 0)
    nl = breadth.get("new_lows", 0)
    score_nhl = max(-100, min(100, (nh - nl) / 500 * 200))

    composite = (
        0.30 * score_indices +
        0.20 * score_ad +
        0.15 * score_mas +
        0.15 * score_vix +
        0.10 * score_sectors +
        0.10 * score_nhl
    )
    composite = max(-100, min(100, round(composite, 1)))

    if   composite >=  40: label = "STRONG BULLISH"
    elif composite >=  20: label = "BULLISH"
    elif composite >  -20: label = "NEUTRAL"
    elif composite >  -40: label = "BEARISH"
    else:                  label = "STRONG BEARISH"

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "composite": {
            "score": composite,
            "label": label,
            "sub": {
                "indices": round(score_indices, 1),
                "ad":       round(score_ad, 1),
                "mas":      round(score_mas, 1),
                "vix":      round(score_vix, 1),
                "sectors":  round(score_sectors, 1),
                "nhl":      round(score_nhl, 1),
            },
            "tip": SENTIMENT_TIPS["composite"],
        },
        "indices":    indices,
        "volatility": volatility,
        "macro":      macro,
        "sectors":    sectors,
        "breadth":    {**breadth, "tips": {
            k: SENTIMENT_TIPS[k] for k in
            ("advances","declines","new_highs","new_lows","pct_above_50sma","pct_above_200sma")
        }},
    }


@app.get("/market/sentiment")
async def market_sentiment() -> JSONResponse:
    """Composite market-sentiment panel — indices, volatility, macro,
    sectors, S&P 500 breadth, and a weighted composite score. Quotes
    via the IBKR streamer (with Alpaca fallback); breadth from daily
    parquets (60s cache). Tooltips for every cell included in payload."""
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _build_sentiment_payload)
    return JSONResponse(payload)


@app.get("/market/clock")
async def market_clock() -> JSONResponse:
    """US equity market status. Authoritative via Alpaca's `/v2/clock`
    (handles weekends, holidays, early closes). Falls back to `{source:
    "fallback"}` with `is_open: null` when Alpaca creds are missing —
    caller can then degrade to local time-of-day estimation.

    Dashboard polls every 30s; cache TTL is 30s server-side so we hit
    Alpaca at most twice a minute regardless of tab count.
    """
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _alpaca_market_clock)
    return JSONResponse(payload)


@app.get("/chart/data")
async def chart_data(symbol: str, timeframe: str = "daily", days: int = 120) -> JSONResponse:
    """Bars + strategy overlays for the in-dashboard chart panel.

    Used by the lightweight-charts widget. Returns:
      {symbol, timeframe, bars[], overlays[]}

    bars[]    — `{time(unix-sec), open, high, low, close, volume}` rows
    overlays[] — per-strategy levels & zones to draw on the chart:
                 {kind: "level", label, price, color, lineStyle?, strategy}
                 {kind: "zone",  label, low, high, color, strategy}
    """
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(
        None, lambda: _build_chart_payload(symbol.upper(), timeframe, max(1, days))
    )
    return JSONResponse(payload)


_CHART_CACHE: dict[tuple, tuple[float, dict]] = {}
_CHART_CACHE_TTL_S = 60.0


def _build_chart_payload(symbol: str, timeframe: str, days: int) -> dict:
    """Sync chart-data builder (runs in executor thread). Reads parquet
    via bars_store, aggregates 1m → 3m if requested, gathers overlays.

    Result cached for 60s by (symbol, timeframe, days). Repeat clicks on
    the same symbol return instantly from cache; the daily / intraday
    bars don't change minute-to-minute anyway."""
    try:
        import bars_store      # type: ignore  resources/bars_store.py
        import patterns        # type: ignore  resources/patterns.py
    except ImportError as exc:
        return {"symbol": symbol, "timeframe": timeframe, "bars": [],
                "overlays": [], "error": f"import_failed: {exc}"}

    # Normalize timeframe → bars_store / aggregator key
    tf_in = (timeframe or "daily").lower()
    if tf_in in ("d", "1d", "daily"):
        tf = "daily"
    elif tf_in in ("3", "3m", "3min"):
        tf = "3m"
    elif tf_in in ("5", "5m", "5min"):
        tf = "5min"
    elif tf_in in ("15", "15m", "15min"):
        tf = "15min"
    elif tf_in in ("1", "1m", "1min"):
        tf = "1min"
    else:
        tf = "daily"

    # Cache lookup
    import time
    cache_key = (symbol, tf, days)
    cached = _CHART_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _CHART_CACHE_TTL_S:
        return cached[1]

    # Load bars. 3m has no parquet → aggregate from 1m. The aggregator
    # needs datetime timestamps; everything else takes ISO strings directly.
    if tf == "3m":
        src = bars_store.load_bars(symbol, timeframe="1min")
        # Tail BEFORE coercion + aggregation — saves time on big histories.
        # ~540 1min bars per RTH day; days=5 → 2700 bars to start with.
        per_day_1m = 540
        cap = days * per_day_1m
        if len(src) > cap:
            src = src[-cap:]
        src_dt = _coerce_bar_timestamps(src)
        bars = patterns.aggregate_to_n_min(src_dt, n=3)
    else:
        bars = bars_store.load_bars(symbol, timeframe=tf)
        # Tail BEFORE the per-bar timestamp coercion so we only parse the
        # bars we're returning, not the entire 2-year history.
        if tf == "daily":
            if len(bars) > days:
                bars = bars[-days:]
        else:
            per_day = {"1min": 540, "5min": 108, "15min": 36}.get(tf, 200)
            cap = days * per_day
            if len(bars) > cap:
                bars = bars[-cap:]

    # Single-pass: coerce timestamps AND format for lightweight-charts.
    out_bars = []
    from datetime import datetime
    for b in bars:
        t = b.get("t")
        if t is None:
            continue
        try:
            if isinstance(t, str):
                # Cheap ISO parse — common path. fromisoformat takes the
                # "+00:00" suffix natively on Py 3.11+.
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            ts = int(t.timestamp())
        except Exception:
            continue
        out_bars.append({
            "time":   ts,
            "open":   float(b["o"]),
            "high":   float(b["h"]),
            "low":    float(b["l"]),
            "close":  float(b["c"]),
            "volume": int(b.get("v", 0) or 0),
        })

    overlays = _gather_chart_overlays(symbol)
    payload = {
        "symbol":    symbol,
        "timeframe": tf,
        "bars":      out_bars,
        "overlays":  overlays,
        "n_bars":    len(out_bars),
    }
    _CHART_CACHE[cache_key] = (time.time(), payload)
    return payload


def _coerce_bar_timestamps(bars: list) -> list:
    """bars_store returns ISO strings for `t`; some helpers want datetime
    objects. Coerce in-place into a fresh list of dicts."""
    if not bars:
        return []
    from datetime import datetime
    out = []
    for b in bars:
        t = b.get("t")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                continue
        out.append({**b, "t": t})
    return out


def _premarket_support(symbol: str) -> float | None:
    """Level A — lowest low of yesterday's premarket session (04:00-09:30 ET).
    Reads the last day of 1min parquet bars and tails to the PM window.
    Returns None if 1m parquet doesn't exist or no PM bars."""
    try:
        import bars_store  # type: ignore
    except ImportError:
        return None
    bars = bars_store.load_bars(symbol.upper(), timeframe="1min")
    if not bars:
        return None
    # Find the most recent trading-day's PM bars. We scan back from the
    # last bar's date and collect 1m bars whose UTC hour is in 08:00-13:29
    # (which covers EDT 04:00-09:29 and EST 03:00-09:29 PM windows for
    # both halves of the year). Then take min(low).
    from datetime import datetime
    last_t = bars[-1].get("t")
    if isinstance(last_t, str):
        try:
            last_t = datetime.fromisoformat(last_t.replace("Z","+00:00"))
        except ValueError:
            return None
    if last_t is None:
        return None
    target_date = last_t.date()
    pm_lows: list[float] = []
    for b in reversed(bars):
        t = b.get("t")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z","+00:00"))
            except ValueError:
                continue
        if t is None:
            continue
        d = t.date()
        if d != target_date:
            # We've walked past the most recent day's bars; stop.
            if pm_lows: break
            target_date = d
        # PM window in UTC: 08:00-13:29 (covers both EDT and EST)
        hr = t.hour
        mn = t.minute
        if (hr == 8) or (9 <= hr <= 12) or (hr == 13 and mn < 30):
            pm_lows.append(float(b["l"]))
    return round(min(pm_lows), 2) if pm_lows else None


def _first_pullback_valley(symbol: str) -> float | None:
    """Level B — the valley (local low) formed after RTH open in the
    most recent trading day. Defined as: the lowest low printed AFTER
    the first higher-high since 09:30 ET. If price hasn't yet made a
    higher-high (still in the opening drive), returns None.

    Walks 1min RTH bars from market open forward, looking for the
    sequence: ascend, peak, descend → that descent's low IS the
    first pullback valley.
    """
    try:
        import bars_store  # type: ignore
    except ImportError:
        return None
    bars = bars_store.load_bars(symbol.upper(), timeframe="1min")
    if not bars:
        return None
    from datetime import datetime
    last_t = bars[-1].get("t")
    if isinstance(last_t, str):
        try:
            last_t = datetime.fromisoformat(last_t.replace("Z","+00:00"))
        except ValueError:
            return None
    if last_t is None:
        return None
    target_date = last_t.date()
    rth: list[dict] = []
    for b in bars:
        t = b.get("t")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z","+00:00"))
            except ValueError:
                continue
        if t is None or t.date() != target_date:
            continue
        # RTH window UTC: 13:30-20:00 (covers EDT 09:30-16:00 and EST 08:30-15:00 too)
        hr = t.hour
        mn = t.minute
        if (hr == 13 and mn >= 30) or (14 <= hr <= 19) or (hr == 20 and mn == 0):
            rth.append({"t": t, "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"])})
    if len(rth) < 10:
        return None
    # Find first higher-high after market open: track running max(high).
    # Once high makes new max at bar i and then a subsequent bar j > i has
    # high < running_max_at_j AND we then see ANOTHER bar k > j with
    # high > rth[j].high, that means we had a peak then a valley.
    # Simpler: walk forward keeping max-high; record min-low between
    # consecutive higher-highs. The first such valley is the answer.
    running_max = rth[0]["h"]
    valley_low = None
    in_pullback = False
    for b in rth[1:]:
        if b["h"] > running_max:
            if in_pullback and valley_low is not None:
                # We just made a higher-high — confirms the pullback.
                return round(valley_low, 2)
            running_max = b["h"]
            in_pullback = False
            valley_low = None
        else:
            in_pullback = True
            valley_low = b["l"] if valley_low is None else min(valley_low, b["l"])
    return None  # no completed pullback yet


def _round_number_neighbours(price: float) -> list[float]:
    """Level C — psychological round numbers near the current price.
    Returns up to 3 levels: the nearest round below, nearest above, and
    the next round above. Grid scales with price tier.
    """
    if price is None or price <= 0:
        return []
    if price < 5:    grid = 0.50
    elif price < 20: grid = 1.00
    elif price < 100: grid = 5.00
    elif price < 500: grid = 10.0
    else:             grid = 25.0
    below = (price // grid) * grid
    above = below + grid
    above2 = above + grid
    out = [round(below, 2), round(above, 2), round(above2, 2)]
    return [x for x in out if 0.95 * price <= x <= 1.10 * price]


def _gather_chart_overlays(symbol: str) -> list[dict]:
    """For `symbol`, return the strategy-specific lines/zones that should
    be drawn on its chart. Reads the latest state/watchlist_*_*.json files
    for each family.

    Today we cover:
      - **DITP P2**: resistance level + range (range_low → range_high).
        Round-number snap if it sits inside the zone.
      - **GUNS / OS**: pre-market high once a candidate file is available
        (placeholder for now; falls back to whatever the journal carries).
    """
    overlays: list[dict] = []
    state_dir = SKILL_DIR / "state"
    sym_u = symbol.upper()

    # --- DITP P2 ---
    for p in sorted(state_dir.glob("watchlist_ditp_*.json"), reverse=True):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        match = None
        for c in blob.get("candidates", []) or []:
            if (c.get("symbol") or "").upper() == sym_u:
                match = c
                break
        if not match:
            continue
        r       = match.get("resistance")
        r_low   = match.get("resistance_low")
        tier    = match.get("tier", "?")
        variant = match.get("variant", "?")
        if r is not None:
            overlays.append({
                "kind":     "level",
                "label":    f"DITP R · tier {tier} · P2-{variant}",
                "price":    float(r),
                "color":    "#f39c12",
                "lineStyle": "solid",
                "strategy": "ditp",
            })
            if r_low is not None and float(r_low) != float(r):
                overlays.append({
                    "kind":      "level",
                    "label":     "DITP R low",
                    "price":     float(r_low),
                    "color":     "#f39c12",
                    "lineStyle": "dashed",
                    "strategy":  "ditp",
                })
                # Bonus zone — top + bottom of the mountain consensus band
                overlays.append({
                    "kind":     "zone",
                    "label":    "DITP mountain zone",
                    "low":      float(r_low),
                    "high":     float(r),
                    "color":    "rgba(243,156,18,0.10)",
                    "strategy": "ditp",
                })
        # Round-number snap inside the zone
        if r is not None:
            snap = _round_number_snap(float(r_low or r), float(r))
            if snap is not None:
                overlays.append({
                    "kind":      "level",
                    "label":     f"Round # ${snap:.2f}",
                    "price":     float(snap),
                    "color":     "#74c0ff",
                    "lineStyle": "dotted",
                    "strategy":  "ditp",
                })
        # Prior-day key levels (DITP P2 v0.2 spec — codes D / E / F in the
        # user's key-level taxonomy). Drawn dotted to distinguish from the
        # solid daily resistance. E (yesterday's high) is a potential
        # polarity-flip target; F (yesterday's close) is a fair-value anchor.
        # D (yesterday's low) is included for completeness but in muted color
        # since it sits below entry and isn't actionable for P2 breakouts.
        y_high  = match.get("yesterday_high")
        y_low   = match.get("yesterday_low")
        y_close = match.get("yesterday_close")
        if y_high is not None and float(y_high) > 0:
            overlays.append({
                "kind":      "level",
                "label":     f"E · Yest H ${float(y_high):.2f}",
                "price":     float(y_high),
                "color":     "#e67e22",
                "lineStyle": "dotted",
                "strategy":  "ditp",
            })
        if y_close is not None and float(y_close) > 0:
            overlays.append({
                "kind":      "level",
                "label":     f"F · Yest C ${float(y_close):.2f}",
                "price":     float(y_close),
                "color":     "#9b59b6",
                "lineStyle": "dotted",
                "strategy":  "ditp",
            })
        if y_low is not None and float(y_low) > 0:
            overlays.append({
                "kind":      "level",
                "label":     f"D · Yest L ${float(y_low):.2f}",
                "price":     float(y_low),
                "color":     "#7f8c8d",
                "lineStyle": "dotted",
                "strategy":  "ditp",
            })
        # Confluence annotation — bubble up Tier ≥ 1 reasons so the chart
        # legend tells the trader WHY this candidate made the cut.
        conf_tier = match.get("confluence_tier") or 0
        conf_reasons = match.get("confluence_reasons") or []
        if conf_tier > 0 and conf_reasons:
            overlays.append({
                "kind":      "annotation",
                "label":     f"Confluence T{conf_tier}: {'; '.join(conf_reasons)}",
                "strategy":  "ditp",
            })
        break   # latest watchlist wins

    # --- Intraday levels per user rule 2026-05-23 P2 execution guide ---
    # (A) Premarket support, (B) first-pullback valley, (C) round-number
    # S/R. All rendered as DOTTED lines to distinguish from the SOLID
    # daily resistance above.
    pm_supp = _premarket_support(sym_u)
    if pm_supp is not None:
        overlays.append({
            "kind":      "level",
            "label":     "Intraday A · PM support",
            "price":     pm_supp,
            "color":     "#74c0ff",
            "lineStyle": "dotted",
            "strategy":  "intraday",
        })
    pullback = _first_pullback_valley(sym_u)
    if pullback is not None:
        overlays.append({
            "kind":      "level",
            "label":     "Intraday B · 1st pullback",
            "price":     pullback,
            "color":     "#b076ff",
            "lineStyle": "dotted",
            "strategy":  "intraday",
        })
    # (C) round numbers neighbouring the most recent close
    try:
        import bars_store  # type: ignore
        daily = bars_store.load_bars(sym_u, timeframe="daily")
        last_close = float(daily[-1]["c"]) if daily else None
    except Exception:
        last_close = None
    if last_close:
        for rn in _round_number_neighbours(last_close):
            overlays.append({
                "kind":      "level",
                "label":     f"Round # ${rn:.2f}",
                "price":     rn,
                "color":     "#5fd97a",
                "lineStyle": "dotted",
                "strategy":  "intraday",
            })

    # --- GUNS / OS pre-market high (best-effort) ---
    # The PMH is computed at entry time and isn't persisted as a standalone
    # state file today. If a `shortlist_<strategy>_<date>.json` exists with
    # a per-symbol pmh field, render it; otherwise skip until that data is
    # surfaced. (Wiring spot for future Step 3 anti-pattern levels too.)
    for p in sorted(state_dir.glob("shortlist_*_*.json"), reverse=True):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        fam = blob.get("strategy", "").lower()
        family = "guns" if fam.startswith("guns") else ("os" if fam.startswith("os") else fam)
        color  = "#f5b342" if family == "guns" else ("#b076ff" if family == "os" else "#aaaaaa")
        for c in blob.get("candidates", []) or blob.get("rows", []) or []:
            if (c.get("symbol") or "").upper() != sym_u:
                continue
            pmh = c.get("pmh") or c.get("pm_high")
            if pmh:
                overlays.append({
                    "kind":     "level",
                    "label":    f"{family.upper()} PMH",
                    "price":    float(pmh),
                    "color":    color,
                    "lineStyle": "solid",
                    "strategy": family,
                })
            break
        if overlays:
            break

    return overlays


def _round_number_snap(low: float, high: float) -> float | None:
    """If a psychological round number (whole dollar / half-dollar / $5 /
    $10 grid) sits inside [low, high], return it. Snap grid scales with
    the absolute price level. Returns None if no clean snap exists."""
    if high <= 0:
        return None
    if high < 5:    grid = 0.50
    elif high < 20: grid = 1.00
    elif high < 100: grid = 5.00
    elif high < 500: grid = 10.0
    else:            grid = 25.0
    candidates: list[float] = []
    # Round to grid and check immediate neighbours
    for k in (-1, 0, 1):
        cand = round(((low + high) / 2) / grid) * grid + k * grid
        if low <= cand <= high:
            candidates.append(cand)
    # Also try the smaller half-grid
    half = grid / 2
    for k in (-1, 0, 1):
        cand = round(((low + high) / 2) / half) * half + k * half
        if low <= cand <= high and cand not in candidates:
            candidates.append(cand)
    if not candidates:
        return None
    # Prefer larger (it's the level price actually has to clear)
    return max(candidates)


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


@app.get("/strategy/ditp/tc_watchlist")
async def ditp_tc_watchlist() -> JSONResponse:
    """Return the latest DITP TC (Trend Continuation) watchlist
    (highest-dated `state/watchlist_tc_<date>.json`). Mirror of the P2
    endpoint above. Source: strategies-reference/DITP.md §6 Setup 4.

    The TC scanner runs EOD on Day 0 (after the orchestrator's post-EOD
    history ingest completes), reads the P2 watchlist for "today", filters
    to symbols whose Day-0 candle both broke out AND printed bullish, and
    writes this file targeted at Day +1. The dashboard's DITP family tab
    renders this above the P2 table so the trader sees tomorrow's
    follow-through candidates alongside today's pending breakouts.
    """
    try:
        state_dir = SKILL_DIR / "state"
        files = sorted(state_dir.glob("watchlist_tc_*.json"))
        if not files:
            return JSONResponse({"candidates": [], "note": "no_watchlist_yet"})
        latest = files[-1]
        payload = json.loads(latest.read_text(encoding="utf-8"))
        payload["_file"] = latest.name
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"candidates": [], "error": str(exc)})


# ----------------------------------------------------------------------
# SCANNER METADATA + ON-DEMAND RUN (added 2026-05-27 for dashboard Scanner view)
#
# The Scanner view needs two things:
#   1. Per-family metadata (latest watchlist file, target_date, age in
#      days vs today ET, candidate count) so it can show "X days stale"
#      indicators next to each family.
#   2. A way to trigger a re-scan on demand without the user dropping
#      to a terminal.
#
# Whitelist design: the family -> command mapping is hard-coded here.
# The client only ever sends a family name; the server picks the
# command. No way to inject arbitrary shell. cwd is pinned to SKILL_DIR
# so the scanner imports its sibling modules correctly. sys.executable
# is whatever launched this server -- in production that's `py -3.12`
# (per dashboard/_supervise_dashboard.bat), which is what ib_insync
# needs.
# ----------------------------------------------------------------------

# Whitelist: family -> (filename prefix used in watchlist_<prefix>_<date>, scanner CLI path)
# The prefix can differ from the family name -- ditp_tc writes
# watchlist_tc_<date>.json (the TC scanner uses the short "tc" prefix
# to keep filenames manageable).
_SCANNER_REGISTRY = {
    "guns": {
        "label":  "GUNS",
        "prefix": "guns",
        "cli":    str(SKILL_DIR / "strategy" / "GUNS" / "scanner.py"),
    },
    "ditp": {
        "label":  "DITP P2",
        "prefix": "ditp",
        "cli":    str(SKILL_DIR / "strategy" / "DITP" / "scanner.py"),
    },
    "ditp_tc": {
        "label":  "DITP TC",
        "prefix": "tc",
        "cli":    str(SKILL_DIR / "strategy" / "DITP" / "tc_scanner.py"),
    },
}

# Generous: the DITP scanner walks ~500 daily parquets and can take
# 60s+ on a cold Resilio cache. 300s is comfortable headroom; if a
# scanner is genuinely hung past that, we fail visibly rather than
# block the dashboard forever.
_SCANNER_TIMEOUT_S = 300


def _today_et_iso() -> str:
    """Today's date in America/New_York as YYYY-MM-DD. Used for
    age-in-days comparisons against watchlist filename dates (which
    are always written in the scanner's local ET context).
    """
    return datetime.now(_et_tz_dash()).date().isoformat()


def _scanner_run_meta(family: str) -> dict:
    """Build the metadata dict for one family. Always returns SOMETHING
    (even when no file exists) so the dashboard can render an empty
    state with a Run button rather than a missing entry."""
    reg = _SCANNER_REGISTRY[family]
    state_dir = SKILL_DIR / "state"
    today = _today_et_iso()
    meta = {
        "family":         family,
        "label":          reg["label"],
        "latest_file":    None,
        "target_date":    None,
        "scanner_run_at": None,
        "n_candidates":   0,
        "age_days":       None,
        "stale":          True,    # default to stale until proven fresh
        "today_et":       today,
    }
    # Prefer JSON (richer); fall back to txt. Sort reverse by name so
    # the highest-dated file wins.
    json_files = sorted(state_dir.glob(f"watchlist_{reg['prefix']}_*.json"), reverse=True)
    txt_files  = sorted(state_dir.glob(f"watchlist_{reg['prefix']}_*.txt"),  reverse=True)
    latest = json_files[0] if json_files else (txt_files[0] if txt_files else None)
    if latest is None:
        return meta
    meta["latest_file"] = latest.name
    # Filename convention: watchlist_<prefix>_<YYYY-MM-DD>.<ext>
    try:
        date_str = latest.stem.split("_")[-1]
        meta["target_date"] = date_str
        from datetime import date as _date
        try:
            td = _date.fromisoformat(date_str)
            today_d = _date.fromisoformat(today)
            meta["age_days"] = (today_d - td).days
            meta["stale"] = meta["age_days"] > 0
        except Exception:
            pass
    except Exception:
        pass
    # Pull richer fields from JSON if available.
    if latest.suffix == ".json":
        try:
            obj = json.loads(latest.read_text(encoding="utf-8"))
            meta["scanner_run_at"] = obj.get("scanner_run_at_utc") or obj.get("scanner_run_at")
            if "n_candidates" in obj:
                meta["n_candidates"] = obj["n_candidates"]
            elif "candidates" in obj:
                meta["n_candidates"] = len(obj["candidates"])
        except Exception:
            pass
    else:
        try:
            count = 0
            for line in latest.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    count += 1
            meta["n_candidates"] = count
        except Exception:
            pass
    return meta


@app.get("/scanner/runs")
async def scanner_runs() -> JSONResponse:
    """Per-family scanner-run metadata for the dashboard Scanner view.

    For each known family (guns, ditp, ditp_tc) returns:
      latest_file, target_date, scanner_run_at, n_candidates,
      age_days (vs today ET), stale (bool).

    The dashboard uses this to draw an "X days stale" badge next to
    each family and to enable/disable per-family Run buttons.
    """
    out = {
        "today_et": _today_et_iso(),
        "families": {fam: _scanner_run_meta(fam) for fam in _SCANNER_REGISTRY},
    }
    return JSONResponse(out)


@app.post("/scanner/run")
async def scanner_run(family: str) -> JSONResponse:
    """Spawn a scanner subprocess for the given family. Synchronous --
    waits up to _SCANNER_TIMEOUT_S for completion, returns stdout/stderr
    tails + the new watchlist's metadata.

    Safety: family is validated against _SCANNER_REGISTRY whitelist.
    cwd is pinned to SKILL_DIR. shell=False (default for subprocess.run
    with a list argv). No client-supplied data ever reaches the shell.
    """
    family = (family or "").lower()
    if family not in _SCANNER_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"unknown family '{family}'. Valid: {list(_SCANNER_REGISTRY)}",
        )
    reg = _SCANNER_REGISTRY[family]
    cli_path = Path(reg["cli"])
    if not cli_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"scanner CLI not found at {cli_path}",
        )
    cmd = [sys.executable, str(cli_path)]
    started = _time.time()
    # Run synchronously in a thread so the event loop stays responsive
    # for other dashboard polls while the scanner is working.
    loop = asyncio.get_running_loop()
    try:
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                cwd=str(SKILL_DIR),
                capture_output=True,
                text=True,
                timeout=_SCANNER_TIMEOUT_S,
                check=False,
            ),
        )
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {
                "ok":         False,
                "family":     family,
                "error":      f"timeout after {_SCANNER_TIMEOUT_S}s",
                "duration_s": round(_time.time() - started, 1),
                "cmd":        " ".join(cmd),
            },
            status_code=504,
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok":         False,
                "family":     family,
                "error":      f"subprocess failed: {exc}",
                "duration_s": round(_time.time() - started, 1),
                "cmd":        " ".join(cmd),
            },
            status_code=500,
        )
    duration = round(_time.time() - started, 1)
    # Tail stdout/stderr (last ~2KB each) so the dashboard can show
    # the scanner's progress lines without exploding the response.
    return JSONResponse({
        "ok":         proc.returncode == 0,
        "family":     family,
        "returncode": proc.returncode,
        "duration_s": duration,
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
        "meta":       _scanner_run_meta(family),
        "cmd":        " ".join(cmd),
    })


# ----------------------------------------------------------------------
# YFINANCE-DIRECT DAILY SCAN (added 2026-05-27)
#
# User architectural directive: "the parquet store i intend to use it
# for backtesting only, this scanning of daily setup through yfinance
# only". The dashboard's Scanner view runs through THIS endpoint, NOT
# /scanner/run (which spawns the CLI scanner that reads parquets).
#
# Design:
#   1. Fetch fresh daily bars via yfinance.download() in ONE batch call
#      (resources/yf_daily_bars.py). ~30s for SP500.
#   2. Monkey-patch `bars_store.load_bars` to return the in-memory
#      bars cache for the duration of THIS scan. This lets the existing
#      DITP detection code (`strategy/DITP/scanner.py::evaluate()`) run
#      unchanged -- it still calls `bars_store.load_bars(sym, timeframe="daily")`,
#      but the call now returns yFinance bars instead of reading parquet.
#   3. Loop the symbols, collect P2Candidate objects, return as JSON.
#   4. Nothing is written to disk. No state/watchlist_*.json file is
#      created. Parquets are NEVER touched (read or write).
#
# Why monkey-patch instead of refactor evaluate()?
#   - evaluate() is deep production code with the full DITP detection.
#     Touching it carries regression risk for the nightly batch scanners.
#   - Monkey-patch is scoped to one request via try/finally; no
#     persistent state change.
#   - bars_store.load_bars is a pure read API -- swapping its
#     implementation is safe as long as the return shape matches.
# ----------------------------------------------------------------------

# Universe registry per setup. Two-tier resolution:
#   1. If `cfg["finviz_screener_url"]` is set, use the Finviz screener
#      result as the universe (cached 1h). Lets the user steer the
#      universe by pasting a screener URL into config.json -- no code
#      change to alter filters (mid-cap+, ATR, beta, volatility, etc.).
#   2. Else fall back to S&P 500 (`resources/sp500.py`).
#
# Returns (symbols, source_label) so the response can tell the UI which
# universe was actually used.
_VALID_SETUPS = ("ditp", "ditp_tc", "ema_rebound")


def _universe_for_setup(setup: str) -> tuple[list[str], str]:
    setup = setup.lower()
    if setup not in _VALID_SETUPS:
        raise ValueError(f"unknown setup '{setup}' (no universe builder wired)")

    try:
        from _common import load_config  # type: ignore  (scripts/_common.py)
        cfg = load_config()
    except Exception:
        cfg = {}
    fv_url = (cfg.get("finviz_screener_url") or "").strip() if cfg else ""

    if fv_url:
        try:
            import finviz_screener  # type: ignore  (resources/finviz_screener.py)
            syms = finviz_screener.fetch_screener_symbols(fv_url, cache_ttl_s=3600)
            if syms:
                return syms, f"finviz ({len(syms)} symbols)"
            # Empty result -- Finviz returned nothing OR scrape failed. Fall
            # back to SP500 rather than scanning nothing.
            print(f"[scanner/yf_scan] finviz_screener returned 0 symbols; falling back to SP500. URL: {fv_url[:120]}")
        except Exception as exc:
            print(f"[scanner/yf_scan] finviz fetch failed ({exc}); falling back to SP500")

    import sp500  # type: ignore  (resources/sp500.py)
    return sp500.get_sp500_symbols(), "sp500"


@app.get("/chart/yf_bars")
async def chart_yf_bars(symbol: str, lookback_days: int = 400) -> JSONResponse:
    """Return daily OHLCV bars for one symbol, fetched fresh via yFinance.

    Consumer: the dashboard's chart pane. Renders candles + EMAs using
    Lightweight Charts (TradingView's open-source library) -- the
    Advanced Charts iframe/widget didn't reliably accept per-study
    color overrides on the free tier, so we moved to a programmatic
    chart that we fully control.

    Reuses resources/yf_daily_bars.fetch_daily_single() so this stays
    consistent with the universe scan path (same fetch code, same
    canonical bar shape). No parquets touched -- user directive
    2026-05-27: "the parquet store i intend to use it for backtesting
    only, this scanning of daily setup through yfinance only".
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol query param required")
    if not (1 <= lookback_days <= 1000):
        raise HTTPException(status_code=400, detail="lookback_days must be 1..1000")
    try:
        import yf_daily_bars  # type: ignore  resources/yf_daily_bars.py
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"import yf_daily_bars failed: {exc}")
    loop = asyncio.get_running_loop()
    try:
        bars = await loop.run_in_executor(
            None,
            lambda: yf_daily_bars.fetch_daily_single(symbol, lookback_days=lookback_days),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"yfinance fetch failed: {exc}")
    return JSONResponse({"symbol": symbol, "count": len(bars), "bars": bars})


@app.get("/chart/sr_levels")
async def chart_sr_levels(symbol: str, lookback_days: int = 400) -> JSONResponse:
    """Return key support / resistance levels for one symbol.

    Built on resources/sr_levels.find_key_levels which calls:
      - patterns.horizontal_resistance_np  -> mountain-anchored R above
      - sr_levels.horizontal_support_np    -> mountain-valley-anchored S below
      - sr_levels.find_broken_resistance_below -> P3 polarity-flip retest
                                                   candidates (prior peaks
                                                   now below current price)

    Consumer: dashboard chart pane's S/R strip (below the chart header)
    so the user can see "R: $X.XX | S: $Y.YY | P3 retest: $Z.ZZ" at a
    glance whenever they click a watchlist symbol -- saves redrawing
    levels in TradingView for every name.

    Bars come from FRESH yFinance (no parquets touched), consistent
    with /chart/yf_bars and the user's 2026-05-27 directive that the
    parquet store is backtest-only on the dashboard side.

    Returns:
      { symbol, current, atr14,
        resistance_above: { level, range_low, range_high, ... } | null,
        support_below:    { level, range_low, range_high, ... } | null,
        broken_resistance: [ { level, bars_ago, mountain }, ... ] }
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol query param required")
    if not (50 <= lookback_days <= 1000):
        # Need enough history for the 120-bar resistance + 180-bar broken-R
        # finders. 50-day floor keeps callers honest; 400 default matches
        # /chart/yf_bars.
        raise HTTPException(status_code=400, detail="lookback_days must be 50..1000")
    try:
        import yf_daily_bars  # type: ignore  resources/yf_daily_bars.py
        import bars_store      # type: ignore  resources/bars_store.py
        import sr_levels        # type: ignore  resources/sr_levels.py
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"import failure: {exc}")

    loop = asyncio.get_running_loop()
    try:
        bars = await loop.run_in_executor(
            None,
            lambda: yf_daily_bars.fetch_daily_single(symbol, lookback_days=lookback_days),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"yfinance fetch failed: {exc}")

    # Monkey-patch bars_store.load_bars so find_key_levels() reads our
    # in-memory yfinance bars instead of parquets. Same pattern as
    # /scanner/yf_scan.
    original_load = bars_store.load_bars

    def _yf_backed_load(sym, start=None, end=None, *, timeframe: str = "1min"):
        if timeframe != "daily" or sym.upper() != symbol:
            return original_load(sym, start, end, timeframe=timeframe)
        return bars

    try:
        bars_store.load_bars = _yf_backed_load  # type: ignore[assignment]
        result = await loop.run_in_executor(
            None,
            lambda: sr_levels.find_key_levels(symbol),
        )
    finally:
        bars_store.load_bars = original_load  # type: ignore[assignment]

    return JSONResponse(result)


@app.get("/scanner/finviz_tickers")
async def scanner_finviz_tickers(force_refresh: bool = False) -> JSONResponse:
    """Return the current Finviz screener result set as a row list.

    Reads `cfg["finviz_screener_url"]`, scrapes the URL via
    `resources/finviz_screener.fetch_screener_rows()` (cached 1h),
    returns `{ url, count, rows: [{symbol, price, volume}, ...] }`.

    This is the FIRST step of the manual scanning workflow per user
    directive 2026-05-27: pull the Finviz tickers, then user decides
    which to chart / which to apply a setup to. NO setup detection
    runs here; the response is just the universe.

    Empty URL -> 400 (the dashboard should surface this to the user
    so they configure config.json before expecting results).
    """
    try:
        from _common import load_config  # type: ignore
        cfg = load_config()
    except Exception:
        cfg = {}
    url = (cfg.get("finviz_screener_url") or "").strip()
    if not url:
        raise HTTPException(
            status_code=400,
            detail="cfg.finviz_screener_url is empty. Paste a Finviz screener URL into config.json.",
        )
    try:
        import finviz_screener  # type: ignore
        rows = finviz_screener.fetch_screener_rows(url, force_refresh=force_refresh)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"finviz fetch failed: {exc}")
    return JSONResponse({
        "url":   url,
        "count": len(rows),
        "rows":  rows,
    })


@app.get("/scanner/universe")
async def scanner_universe(setup: str = "ditp") -> JSONResponse:
    """Cheap probe: returns which universe `/scanner/yf_scan?setup=X`
    WOULD use right now, without running the full scan. Lets the
    dashboard label the panel before the user clicks Scan.

    Resolves the same way the real endpoint does (finviz_screener_url
    first, then SP500 fallback) but does NOT fetch yFinance bars.
    Cached finviz results return instantly; uncached ones still cost
    one Finviz round trip (~3s).
    """
    setup = (setup or "").lower()
    if setup not in _VALID_SETUPS:
        raise HTTPException(status_code=400, detail=f"unknown setup '{setup}'")
    try:
        symbols, source = _universe_for_setup(setup)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse({
        "setup":  setup,
        "source": source,
        "size":   len(symbols),
        "sample": symbols[:10],
    })


@app.post("/scanner/yf_scan")
async def scanner_yf_scan(
    setup: str,
    limit: int | None = None,
) -> JSONResponse:
    """Run a daily-chart setup scan using FRESH yFinance bars (no
    parquets touched). Returns candidates in-memory; nothing written
    to disk.

    Query params:
      setup -- one of: ditp, ditp_tc
      limit -- optional, cap universe to first N symbols (debug aid).

    Returns:
      { ok, setup, universe_size, n_candidates, candidates: [...],
        fetch_duration_s, scan_duration_s, total_duration_s, today_et }
    """
    setup = (setup or "").lower()
    if setup not in _VALID_SETUPS:
        raise HTTPException(
            status_code=400,
            detail=f"setup must be one of {list(_VALID_SETUPS)}. Got: {setup!r}",
        )
    if setup == "ditp_tc":
        # ditp_tc has no yFinance-direct detector yet (needs in-memory P2
        # watchlist plumbing). The frontend's SETUPS array marks it as a
        # stub so it shouldn't actually call this; guard anyway.
        raise HTTPException(
            status_code=501,
            detail="ditp_tc via yFinance not wired yet. Use the CLI scanner or DITP P2/ema_rebound for now.",
        )

    # Heavy imports done lazily so server startup stays cheap.
    try:
        import yf_daily_bars                # type: ignore  resources/yf_daily_bars.py
        import bars_store                   # type: ignore  resources/bars_store.py
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"import failure: {exc}")

    started = _time.time()
    try:
        symbols, universe_source = _universe_for_setup(setup)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if limit and limit > 0:
        symbols = symbols[:limit]

    loop = asyncio.get_running_loop()
    # --- Step 1: yFinance batch fetch (run in thread; yfinance is blocking) ---
    t_fetch0 = _time.time()
    try:
        bars_by_symbol = await loop.run_in_executor(
            None,
            lambda: yf_daily_bars.fetch_daily_batch(
                symbols, lookback_days=400, threads=True, progress=False,
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"yfinance fetch failed: {exc}")
    fetch_duration = round(_time.time() - t_fetch0, 1)
    fetched_n = sum(1 for v in bars_by_symbol.values() if v)

    # --- Step 2: scan with monkey-patched bars_store.load_bars ---
    t_scan0 = _time.time()
    original_load = bars_store.load_bars

    def _yf_backed_load(symbol, start=None, end=None, *, timeframe: str = "1min"):
        if timeframe != "daily":
            # Detectors only request daily on this code path. If
            # something asks for intraday we honour the original
            # (which would read parquets) -- safer than crashing.
            return original_load(symbol, start, end, timeframe=timeframe)
        return bars_by_symbol.get(symbol.upper(), [])

    errors: list[str] = []
    try:
        bars_store.load_bars = _yf_backed_load  # type: ignore[assignment]
        # Dispatch by setup. Each branch returns a list of JSON-serializable
        # candidate dicts. CPU-bound numpy work runs in the default
        # executor so the FastAPI event loop stays responsive.
        if setup == "ditp":
            from strategy.DITP import scanner as ditp_scanner  # type: ignore
            cfg = ditp_scanner.P2Config()
            variants_allowed = {"A", "B", "C"}
            cand_objs = await loop.run_in_executor(
                None,
                lambda: ditp_scanner.scan_universe(symbols, cfg, variants_allowed),
            )
            from dataclasses import asdict as _asdict
            candidates_dicts = [_asdict(c) for c in cand_objs]
        elif setup == "ema_rebound":
            from strategy.DITP import ema_rebound as ema_mod  # type: ignore
            cfg = ema_mod.EMARebConfig()
            candidates_dicts = await loop.run_in_executor(
                None,
                lambda: ema_mod.scan_universe(symbols, cfg),
            )
        else:
            # Should be unreachable -- _VALID_SETUPS guard at top + ditp_tc
            # 501 guard above mean we only get here for setups whose dispatch
            # is missing. Surface that loudly.
            raise HTTPException(status_code=500, detail=f"no dispatch wired for setup '{setup}'")
    finally:
        bars_store.load_bars = original_load  # type: ignore[assignment]

    scan_duration = round(_time.time() - t_scan0, 1)
    total_duration = round(_time.time() - started, 1)
    today_et = _today_et_iso()

    return JSONResponse({
        "ok":               True,
        "setup":            setup,
        "today_et":         today_et,
        "universe_source":  universe_source,
        "universe_size":    len(symbols),
        "fetched_n":        fetched_n,
        "n_candidates":     len(candidates_dicts),
        "candidates":       candidates_dicts,
        "fetch_duration_s": fetch_duration,
        "scan_duration_s":  scan_duration,
        "total_duration_s": total_duration,
        "errors_tail":      errors[-20:],   # last 20 per-symbol errors for diagnosis
        "errors_count":     len(errors),
    })


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
