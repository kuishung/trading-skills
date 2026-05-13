"""Append-only JSONL trade log.

One line per event. Strategy skills can tail this file to compare what
actually executed against what they expected.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _client import SKILL_DIR, load_config


def _log_path():
    cfg = load_config()
    p = Path(cfg["trade_log_path"])
    return p if p.is_absolute() else SKILL_DIR / p


def append_event(event_type, payload):
    """Append a single event. event_type is e.g. 'order_submitted', 'order_filled'."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "payload": payload,
    }
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return record


def read_log(limit=None):
    path = _log_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    if limit:
        records = records[-limit:]
    return records


def main():
    parser = argparse.ArgumentParser(description="Read the local trade log")
    parser.add_argument("--limit", type=int, default=20, help="Show last N events")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    records = read_log(limit=args.limit)
    if args.json:
        print(json.dumps(records, indent=2, default=str))
        return
    if not records:
        print("(trade log is empty)")
        return
    for r in records:
        print(f"{r['timestamp']}  {r['event']}")
        for k, v in r["payload"].items():
            print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
