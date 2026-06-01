"""report_ingest_health.py — Hermes-side parquet-ingest health reporter.

Runs as a scheduled task (the "cron") ON THE HERMES BOX, where the bars store
lives. It prepares a freshness report (file metadata only — never opens a parquet
file, per the backtest-only scope rule) plus the tail of the ingest log, and
POSTs it to TradeHunter's /api/ingest/health. The Data Ingest page then shows it
(like the agent heartbeat) — so you can eyeball ingest health daily without the
web app reading the data drive over the network.

No third-party deps (stdlib only) — run with `py`:
    py C:\\trading-skills\\TradeHunter\\dashboard_tst\\deploy\\report_ingest_health.py

Reads config from the dashboard's app/.env (next to this deploy/ dir) and/or env:
  TST_PRICE_HISTORY_DIR  bars store root (e.g. C:\\HermesSync\\MarketData\\price_history)
  TST_INGEST_API_KEY     X-API-Key for the dashboard API (same as the agent)
  TST_INGEST_LOG         optional path to ingest_log.jsonl (default: ../ingest_log.jsonl beside the store)
  TST_DASHBOARD_URL      dashboard base URL (default http://localhost:8000)
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.request

_TIMEFRAMES = ("1min", "5min", "15min", "daily")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV = os.path.join(_HERE, "..", "app", ".env")


def _load_env() -> dict:
    """Merge app/.env (if present) with os.environ (env wins)."""
    cfg: dict[str, str] = {}
    try:
        with open(_ENV, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("TST_")})
    return cfg


def _timeframes(root: str) -> list[dict]:
    out = []
    for tf in _TIMEFRAMES:
        d = os.path.join(root, tf)
        count = 0
        newest = 0.0
        total = 0
        if os.path.isdir(d):
            try:
                for e in os.scandir(d):
                    if e.is_file() and e.name.endswith(".parquet"):
                        count += 1
                        try:
                            st = e.stat()
                        except OSError:
                            continue
                        total += st.st_size
                        newest = max(newest, st.st_mtime)
            except OSError:
                pass
        out.append({
            "tf": tf, "symbols": count, "mb": round(total / 1048576, 1),
            "newest_epoch": round(newest, 0) if newest else 0,
        })
    return out


def _log_tail(path: str, n: int = 8) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()][-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
            ts = d.get("ts") or d.get("time") or ""
            msg = d.get("msg") or d.get("message") or d.get("event") or json.dumps(d)
            out.append(f"{ts} {msg}"[:200])
        except Exception:
            out.append(ln[:200])
    return out


def main() -> int:
    cfg = _load_env()
    root = (cfg.get("TST_PRICE_HISTORY_DIR") or "").strip()
    key = (cfg.get("TST_INGEST_API_KEY") or "").strip()
    url = (cfg.get("TST_DASHBOARD_URL") or "http://localhost:8000").rstrip("/")
    if not root or not os.path.isdir(root):
        print(f"ABORT: TST_PRICE_HISTORY_DIR missing/not a dir: {root!r}")
        return 1
    if not key:
        print("ABORT: TST_INGEST_API_KEY not set")
        return 1
    log_path = (cfg.get("TST_INGEST_LOG") or "").strip() or os.path.join(
        os.path.dirname(root.rstrip("\\/")), "ingest_log.jsonl"
    )
    report = {
        "host": socket.gethostname(),
        "root": root,
        "generated_epoch": round(time.time(), 0),
        "timeframes": _timeframes(root),
        "log_tail": _log_tail(log_path),
    }
    body = json.dumps(report).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/ingest/health", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"OK {resp.status} {resp.read(200).decode('utf-8', 'replace')}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"POST failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
