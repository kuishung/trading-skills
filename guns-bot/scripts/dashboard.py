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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

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
    """Spawn/stop the trade_day.py bot as a child process.

    Limitation: if the dashboard restarts while the bot is running, the
    new dashboard loses the subprocess handle and reports 'stopped' until
    the bot exits or is killed externally. PID-file adoption is a TODO.
    """

    BOT_SCRIPT = "scripts/trade_day.py"

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._log_fh = None
        self._log_path: Path | None = None
        self.dry_run: bool = False

    @property
    def status(self) -> str:
        if self.proc is None:
            return "stopped"
        return "running" if self.proc.poll() is None else "stopped"

    @property
    def pid(self) -> int | None:
        if self.proc and self.proc.poll() is None:
            return self.proc.pid
        return None

    def start(self) -> dict[str, Any]:
        if self.status == "running":
            return {"status": "already_running", "pid": self.pid}
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / f"bot_{_today_str()}.log"
        self._log_fh = log_path.open("a", encoding="utf-8")
        self._log_path = log_path
        cflags = 0
        if os.name == "nt":
            cflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        script = str(SKILL_DIR / self.BOT_SCRIPT)
        argv = [sys.executable, script]
        if self.dry_run:
            argv.append("--dry-run")
        self.proc = subprocess.Popen(
            argv,
            cwd=str(SKILL_DIR),
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            creationflags=cflags,
        )
        return {"status": "started", "pid": self.proc.pid, "log": str(log_path),
                "dry_run": self.dry_run}

    def stop(self) -> dict[str, Any]:
        if self.status != "running":
            self._close_log()
            return {"status": "not_running"}
        assert self.proc is not None
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3.0)
        finally:
            self._close_log()
        return {"status": "stopped"}

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
        self._log_fh = None


bot = BotManager()


def _load_cfg() -> dict[str, Any]:
    cfg_path = SKILL_DIR / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _probe_ibkr_tcp(timeout: float = 0.5) -> str:
    """Return 'up' if TWS/Gateway API port accepts a TCP connect, else 'down'."""
    cfg = _load_cfg()
    host = cfg.get("ibkr_host", "127.0.0.1")
    try:
        port = int(cfg.get("ibkr_port", 7497))
    except (TypeError, ValueError):
        return "down"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "up"
    except Exception:
        return "down"


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
            "bot": {"status": bot.status, "pid": bot.pid, "dry_run": bot.dry_run},
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
                "bot": {"status": b_state[0], "pid": b_state[1], "dry_run": bot.dry_run},
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


@app.post("/shutdown")
async def shutdown() -> JSONResponse:
    """Stop the dashboard process. Does NOT touch the bot."""
    async def _kill() -> None:
        await asyncio.sleep(0.3)  # let the HTTP response flush
        os._exit(0)
    asyncio.create_task(_kill())
    return JSONResponse({"status": "shutting down dashboard (bot is unaffected)"})


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
    return JSONResponse({"status": bot.status, "pid": bot.pid, "dry_run": bot.dry_run})


@app.post("/bot/config")
async def bot_config(body: dict) -> JSONResponse:
    """Set bot startup config. Only honored on next start (not mid-flight)."""
    if "dry_run" in body:
        if bot.status == "running":
            return JSONResponse(
                {"error": "cannot change dry_run while bot is running"}, status_code=409
            )
        bot.dry_run = bool(body["dry_run"])
    return JSONResponse({"dry_run": bot.dry_run})


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
