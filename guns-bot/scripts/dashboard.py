"""Local dashboard server for guns-bot.

FastAPI + WebSocket. Serves web/index.html at http://localhost:8000.
Tails state/events_*.jsonl and re-reads plan/fills/equity files so a
browser tab shows what the bot is doing in real time.

Read-only. The dashboard never sends orders — it observes files the
bot writes plus the same data adapter the bot reads.

Run:
    py scripts/dashboard.py
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

DASHBOARD_START_TS = _time.time()

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
WEB_DIR = SKILL_DIR / "web"
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


# ------------- Bot subprocess manager -------------

class BotManager:
    """Spawn/stop the trading session — trade_day.py + scanner_observe.py.

    Both children start together on /bot/start and terminate together on
    /bot/stop. The scanner has its own ET time guards so it idles outside
    the configured pre-market window even if launched early.

    Limitation: if the dashboard restarts while children are running, the
    new dashboard loses the subprocess handles and reports 'stopped' until
    the children exit or are killed externally. PID-file adoption is a TODO.
    """

    BOT_SCRIPT = "scripts/trade_day.py"
    SCANNER_SCRIPT = "scripts/scanner_observe.py"

    # Scanner ET-window — feeds ORB's "Stocks in Play" universe at 09:35.
    # Internally rotates scan codes by wall-clock so the universe matches each regime.
    SCANNER_START_ET = "09:00"
    SCANNER_END_ET = "15:58"

    ARMED_FLAG = "armed.flag"  # presence in state/ = armed

    def __init__(self) -> None:
        self.bot_proc: subprocess.Popen | None = None
        self.scanner_proc: subprocess.Popen | None = None
        self._bot_log_fh = None
        self._scanner_log_fh = None
        self._bot_started_ts: float | None = None
        self._scanner_started_ts: float | None = None
        # Reflects the mode the running bot was LAUNCHED with — not the
        # current arm flag. Used to surface "applies at next start" in UI.
        self._launched_armed: bool | None = None

    @property
    def armed(self) -> bool:
        """Persistent on-disk arm state. Survives dashboard restart."""
        return (STATE_DIR / self.ARMED_FLAG).exists()

    def set_armed(self, value: bool) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        flag = STATE_DIR / self.ARMED_FLAG
        if value:
            flag.write_text(
                f"armed at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
                encoding="utf-8",
            )
        else:
            try:
                flag.unlink()
            except FileNotFoundError:
                pass

    @property
    def launched_armed(self) -> bool | None:
        return self._launched_armed

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
        if any(self._proc_state(p) == "running" for p in (
            self.bot_proc, self.scanner_proc,
        )):
            return "running"
        return "stopped"

    @property
    def pid(self) -> int | None:
        return self._proc_pid(self.bot_proc)

    @property
    def scanner_status(self) -> str:
        return self._proc_state(self.scanner_proc)

    @property
    def scanner_pid(self) -> int | None:
        return self._proc_pid(self.scanner_proc)

    @property
    def bot_uptime_s(self) -> float | None:
        if self._bot_started_ts and self._proc_state(self.bot_proc) == "running":
            return _time.time() - self._bot_started_ts
        return None

    @property
    def scanner_uptime_s(self) -> float | None:
        if self._scanner_started_ts and self._proc_state(self.scanner_proc) == "running":
            return _time.time() - self._scanner_started_ts
        return None

    def start(self) -> dict[str, Any]:
        if self.status == "running":
            return {
                "status": "already_running",
                "bot_pid": self._proc_pid(self.bot_proc),
                "scanner_pid": self._proc_pid(self.scanner_proc),
            }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cflags = 0
        if os.name == "nt":
            cflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        # ---- bot ----
        bot_log = STATE_DIR / f"bot_{_today_str()}.log"
        self._bot_log_fh = bot_log.open("a", encoding="utf-8")
        bot_argv = [sys.executable, str(SKILL_DIR / self.BOT_SCRIPT)]
        armed_at_launch = self.armed
        if not armed_at_launch:
            bot_argv.append("--dry-run")
        self._launched_armed = armed_at_launch
        self.bot_proc = subprocess.Popen(
            bot_argv, cwd=str(SKILL_DIR),
            stdout=self._bot_log_fh, stderr=subprocess.STDOUT,
            creationflags=cflags,
        )
        self._bot_started_ts = _time.time()

        # ---- scanner ----
        scanner_log = STATE_DIR / f"scanner_{_today_str()}.log"
        self._scanner_log_fh = scanner_log.open("a", encoding="utf-8")
        scanner_argv = [
            sys.executable, str(SKILL_DIR / self.SCANNER_SCRIPT),
            "--start-et", self.SCANNER_START_ET,
            "--end-et", self.SCANNER_END_ET,
        ]
        self.scanner_proc = subprocess.Popen(
            scanner_argv, cwd=str(SKILL_DIR),
            stdout=self._scanner_log_fh, stderr=subprocess.STDOUT,
            creationflags=cflags,
        )
        self._scanner_started_ts = _time.time()

        return {
            "status": "started",
            "bot_pid": self.bot_proc.pid,
            "scanner_pid": self.scanner_proc.pid,
            "armed": armed_at_launch,
            "scanner_window_et": f"{self.SCANNER_START_ET}-{self.SCANNER_END_ET}",
        }

    def stop(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "bot": "not_running",
            "scanner": "not_running",
        }
        for label, proc_attr, log_attr in (
            ("bot", "bot_proc", "_bot_log_fh"),
            ("scanner", "scanner_proc", "_scanner_log_fh"),
        ):
            proc = getattr(self, proc_attr)
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
                result[label] = "stopped"
            log_fh = getattr(self, log_attr)
            if log_fh:
                try: log_fh.close()
                except Exception: pass
                setattr(self, log_attr, None)
        # Reset the launched-with-armed tracker so UI shows correct next-start state.
        self._launched_armed = None
        return result


bot = BotManager()


def _load_cfg() -> dict[str, Any]:
    cfg_path = SKILL_DIR / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


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
                "armed": bot.armed,
                "launched_armed": bot.launched_armed,
            },
            "scanner": {"status": bot.scanner_status, "pid": bot.scanner_pid},
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
    """Probe IBKR TCP port + bot/scanner subprocess. Broadcast on transition,
    and re-broadcast the processes block every cycle so uptime ticks live."""
    global _ibkr_status_cache, _bot_status_cache
    loop = asyncio.get_event_loop()
    prev_scanner: tuple[str, int | None] = ("stopped", None)
    while True:
        ibkr = await loop.run_in_executor(None, _probe_ibkr_tcp)
        b_state = (bot.status, bot.pid)
        s_state = (bot.scanner_status, bot.scanner_pid)
        if (ibkr != _ibkr_status_cache
                or b_state != _bot_status_cache
                or s_state != prev_scanner):
            _ibkr_status_cache = ibkr
            _bot_status_cache = b_state
            prev_scanner = s_state
            await hub.broadcast({"type": "health", "health": {
                "ibkr": ibkr,
                "bot": {
                    "status": b_state[0], "pid": b_state[1],
                    "armed": bot.armed, "launched_armed": bot.launched_armed,
                },
                "scanner": {"status": s_state[0], "pid": s_state[1]},
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
        (default 09:15 — 15 minutes before NYSE open), call bot.start()
        once and write state/auto_started_<date>.flag so a dashboard
        restart doesn't re-trigger today.
      - No-op if cfg.auto_start_enabled is false.
      - No-op if the bot is already running.
      - Stops trying once ET passes 10:30 — past that the entry window
        is closed anyway.

    US market holidays are NOT special-cased yet; on a holiday the auto
    trigger will fire and the bot will find no plan / no data and idle.
    Worth fixing when we wire scanner -> watchlist (Stage 3).
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
            target_str = str(cfg.get("auto_start_et", "09:15"))
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
                    from _events import emit as _emit
                    _emit("session.auto_start", result)
                except Exception:
                    pass
                await hub.broadcast({"type": "health", "health": {
                    "bot": {
                        "status": bot.status, "pid": bot.pid,
                        "armed": bot.armed, "launched_armed": bot.launched_armed,
                    },
                    "scanner": {"status": bot.scanner_status, "pid": bot.scanner_pid},
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


app = FastAPI(title="guns-bot dashboard", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    if not INDEX_HTML.exists():
        return "<h1>Dashboard UI missing</h1><p>Expected web/index.html.</p>"
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/snapshot")
async def snapshot() -> JSONResponse:
    return JSONResponse(_snapshot())


@app.get("/config")
async def config_view() -> JSONResponse:
    """Public view of harmless config knobs. Never leaks secrets — credentials
    live in VAULT, not in config.json."""
    cfg = _load_cfg()
    return JSONResponse({
        "auto_start_enabled": bool(cfg.get("auto_start_enabled", True)),
        "auto_start_et": str(cfg.get("auto_start_et", "09:00")),
        "scanner_start_et": BotManager.SCANNER_START_ET,
        "scanner_end_et": BotManager.SCANNER_END_ET,
    })


@app.post("/shutdown")
async def shutdown() -> JSONResponse:
    """Stop the dashboard process only. Bot and scanner stay alive."""
    async def _kill() -> None:
        await asyncio.sleep(0.3)  # let the HTTP response flush
        os._exit(0)
    asyncio.create_task(_kill())
    return JSONResponse({"status": "shutting down dashboard (children unaffected)"})


@app.post("/restart")
async def restart() -> JSONResponse:
    """Exit with code 100. start_dashboard.bat's supervisor loop catches that
    and re-launches the dashboard. Bot and scanner are NOT touched —
    a code change picked up by the new dashboard inherits them as orphans
    until they exit naturally."""
    async def _exit100() -> None:
        await asyncio.sleep(0.3)
        os._exit(100)
    asyncio.create_task(_exit100())
    return JSONResponse({"status": "restarting dashboard (children unaffected)"})


@app.post("/shutdown-all")
async def shutdown_all() -> JSONResponse:
    """Terminate every trading-session subprocess THEN exit the dashboard."""
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
        "armed": bot.armed,
        "launched_armed": bot.launched_armed,
    })


@app.post("/bot/arm")
async def bot_arm(body: dict) -> JSONResponse:
    """Set the armed flag. Live trading is enabled at the bot's next launch.

    Toggling while the bot is running is allowed — it persists for the
    next session, but the currently running bot keeps the mode it was
    launched with (surfaced as `launched_armed`).
    """
    if "armed" not in body:
        return JSONResponse({"error": "body must include 'armed' bool"},
                            status_code=400)
    bot.set_armed(bool(body["armed"]))
    # Broadcast immediately so every connected browser updates without
    # waiting for the next 3s health poll (which wouldn't fire anyway
    # if nothing else changed).
    await hub.broadcast({"type": "health", "health": {
        "bot": {
            "status": bot.status, "pid": bot.pid,
            "armed": bot.armed, "launched_armed": bot.launched_armed,
        },
    }})
    return JSONResponse({
        "armed": bot.armed,
        "launched_armed": bot.launched_armed,
        "applies_to_running_session": False,
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
    print(f"guns-bot dashboard at http://{HOST}:{PORT}")
    print("(Read-only observer. The bot is what places orders.)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
