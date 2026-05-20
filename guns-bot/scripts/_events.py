"""Tiny event-bus helper.

Bot scripts call emit(type, payload) to append a structured record to
state/events_YYYY-MM-DD.jsonl. The dashboard server tails that file and
pushes new lines to connected browsers.

Append-only JSONL keeps it simple — no IPC, no socket, no shared memory.
The dashboard wakes once per second to read new bytes. Good enough latency
for human eyes; cheap to replay after a bot restart.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"


def emit(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """Append an event to today's JSONL log.

    event_type is a short tag like 'scanner.emit', 'watchlist.add',
    'order.submit', 'fill', 'error'. Dashboard groups/colors by prefix.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    log_path = STATE_DIR / f"events_{now.strftime('%Y-%m-%d')}.jsonl"
    record = {
        "ts": now.isoformat(timespec="milliseconds"),
        "type": event_type,
        "payload": payload or {},
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
