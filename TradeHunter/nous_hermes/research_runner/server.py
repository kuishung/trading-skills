#!/usr/bin/env python3
"""research-runner — LAN-only HTTP shim that lets TradeHunter's Research page
get an *agent-grounded* planning reply from this Nous Hermes box.

Why this exists
---------------
The dashboard (Windows "Hermes" box) is outbound-only and talks to a plain
DeepSeek API for the planning chat — which has NO access to the EDGAR corpus on
this machine's mount. This shim flips that for the chat path: the dashboard
POSTs the conversation here over the LAN, and we run the *real agent*:

    hermes chat -q "<conversation>" -s research-planning -Q --max-turns N --yolo

`hermes` is the full tool-calling agent, so it can read the 10-Q corpus at
/mnt/hermes_sync/QuarterlyReport ON DEMAND, web-search, etc., then return a
grounded answer. We relay that answer back as JSON.

Design notes
------------
- **Stdlib only** (http.server) — no pip installs (this box's disk is tight).
- **LAN-only + token auth.** Bind to the LAN interface; require X-Research-Token
  (read from ~/.hermes/.env -> TST_RESEARCH_TOKEN). The agent is never exposed to
  the internet; only the dashboard, on the LAN, calls this.
- **Stateless MVP.** Each call sends the recent transcript and runs a fresh agent
  invocation. Simple + robust; the dashboard DB stays the source of truth. A
  later optimisation can use `hermes chat -c <session>` for per-topic memory so
  filings aren't re-read every turn (see README "Follow-ups").
- **Soft surface.** Any failure returns a JSON error; the dashboard falls back to
  DeepSeek-direct so the page never breaks.

Run:
    python3 server.py          # reads HOST/PORT/token from env or ~/.hermes/.env
Env (optional overrides):
    RESEARCH_RUNNER_HOST  (default 0.0.0.0)
    RESEARCH_RUNNER_PORT  (default 8787)
    RESEARCH_RUNNER_MAX_TURNS (default 12)
    RESEARCH_RUNNER_TIMEOUT   (default 150 seconds)
    HERMES_BIN            (default: `which hermes` or ~/.local/bin/hermes)
    TST_RESEARCH_TOKEN    (REQUIRED — shared secret with the dashboard)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0.0"
HOME = Path(os.path.expanduser("~"))
HERMES_ENV = HOME / ".hermes" / ".env"
LOG_DIR = HOME / ".hermes" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "research_runner.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("research_runner")


def _load_env_file(path: Path) -> dict:
    """Parse a simple KEY=VALUE .env (no export, no interpolation)."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


_FILE_ENV = _load_env_file(HERMES_ENV)


def _cfg(name: str, default: str | None = None) -> str | None:
    """Env var wins; else ~/.hermes/.env; else default."""
    return os.environ.get(name) or _FILE_ENV.get(name) or default


HOST = _cfg("RESEARCH_RUNNER_HOST", "0.0.0.0")
PORT = int(_cfg("RESEARCH_RUNNER_PORT", "8787"))
MAX_TURNS = _cfg("RESEARCH_RUNNER_MAX_TURNS", "12")
TIMEOUT = int(_cfg("RESEARCH_RUNNER_TIMEOUT", "150"))
TOKEN = _cfg("TST_RESEARCH_TOKEN")
HERMES_BIN = (
    _cfg("HERMES_BIN")
    or shutil.which("hermes")
    or str(HOME / ".local" / "bin" / "hermes")
)


def _build_query(topic: dict, history: list) -> str:
    """Compose the transcript into a single query for `hermes chat -q`.
    The persona + corpus instructions come from the `research-planning` skill;
    this just hands the agent the conversation to continue."""
    kind = (topic.get("kind") or "company").lower()
    subject = (topic.get("subject") or "").strip()
    title = (topic.get("title") or "").strip()

    lines: list[str] = []
    if kind == "company" and subject:
        lines.append(f"[Research topic — COMPANY: {subject}]")
    elif kind == "macro":
        lines.append(f"[Research topic — MACRO: {title or subject or 'unspecified'}]")
    else:
        lines.append(f"[Research topic: {title or subject or 'unspecified'}]")
    lines.append(
        "This is a planning conversation on TradeHunter's Research page. "
        "Continue it as the assistant's NEXT turn only — do not run the research."
    )
    lines.append("")
    lines.append("Conversation so far:")
    for m in history:
        role = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content', '')}")
    lines.append("")
    lines.append("Assistant:")
    return "\n".join(lines)


def _run_agent(query: str) -> dict:
    cmd = [
        HERMES_BIN, "chat",
        "-q", query,
        "-s", "research-planning",
        "-Q",                       # quiet: only the final response
        "--max-turns", str(MAX_TURNS),
        "--yolo",                   # auto-approve tools (no TTY here)
    ]
    log.info("invoking agent (%d msgs in query, max-turns=%s)", query.count("\n"), MAX_TURNS)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=str(HOME),
            env={**os.environ, "HERMES_ACCEPT_HOOKS": "1"},
        )
    except FileNotFoundError:
        return {"ok": False, "content": f"hermes binary not found at {HERMES_BIN}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "content": f"agent timed out after {TIMEOUT}s"}

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        log.warning("agent exit %s: %s", proc.returncode, (proc.stderr or "")[:400])
        return {"ok": False, "content": out or (proc.stderr or "agent failed").strip()[:500]}
    if not out:
        return {"ok": False, "content": "agent returned no output"}
    return {"ok": True, "content": out}


class Handler(BaseHTTPRequestHandler):
    server_version = f"research-runner/{VERSION}"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # route access logs through our logger
        log.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/status"):
            self._json(200, {"ok": True, "agent": "research_runner", "version": VERSION,
                             "hermes_bin": HERMES_BIN, "token_set": bool(TOKEN)})
        else:
            self._json(404, {"ok": False, "content": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/chat":
            self._json(404, {"ok": False, "content": "not found"})
            return
        if not TOKEN:
            self._json(503, {"ok": False, "content": "runner has no TST_RESEARCH_TOKEN set"})
            return
        if self.headers.get("X-Research-Token") != TOKEN:
            self._json(401, {"ok": False, "content": "bad or missing X-Research-Token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"ok": False, "content": "invalid JSON body"})
            return

        history = data.get("history") or []
        topic = data.get("topic") or {}
        if not history:
            self._json(400, {"ok": False, "content": "empty history"})
            return

        query = _build_query(topic, history)
        result = _run_agent(query)
        self._json(200 if result["ok"] else 502, result)


def main():
    if not TOKEN:
        log.warning("TST_RESEARCH_TOKEN is NOT set — /chat will refuse all calls. "
                    "Add it to %s and restart.", HERMES_ENV)
    log.info("research-runner %s listening on %s:%s  (hermes=%s)", VERSION, HOST, PORT, HERMES_BIN)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
