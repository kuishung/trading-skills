"""Structured decision journal for the multi-strategy bot.

Appends one JSONL record per decision to state/journal_<date>.jsonl.
Every entry has:
  ts        — ISO-8601 UTC timestamp
  strategy  — name of the strategy that made the decision (e.g. "orb_5min")
  event     — one of the canonical event names (see EVENT_VOCAB)
  symbol    — optional, when the decision concerns a specific ticker
  ...       — strategy-specific fields (reason, plan, evidence)

The journal is the substrate for post-trade analytics and any future
adaptive logic. Keep it append-only, never edited in place. The file
is rotated by date (ET) so a multi-day run produces one file per day.

Verbose mode (the default per the bot's config decision): every
shortlist / rejection / plan / fill / breakeven / exit gets a record.
Quiet runs would just call the same functions with a `level="silent"`
filter — not implemented yet.

Canonical event vocabulary (additive — add new strings as needed):

  shortlisted          — symbol entered the strategy's candidate pool
  rejected             — symbol rejected from the pool, with `reason`
  entry_planned        — plan computed, no order yet, includes `plan`
  entry_submitted      — order sent to Alpaca, includes `order_id` and `qty`
  entry_filled         — fill received, includes `avg_price` and `filled_qty`
  oco_attached         — OCO bracket attached after fill, includes leg ids
  breakeven_moved      — stop moved to entry at 1R unrealised
  exit_filled          — TP or SL hit, includes `leg` and `filled_avg_price`
  entry_cancelled      — unfilled entry order cancelled, includes `reason`
  force_closed         — EOD sweep liquidated open position
  strategy_started     — strategy entry phase began
  strategy_finished    — strategy entry phase ended (with count of submissions)
  universe_picked      — pick_universe finished, includes count
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"


def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz  # type: ignore
        return pytz.timezone("America/New_York")


def _date_iso_et() -> str:
    return datetime.now(timezone.utc).astimezone(_et_tz()).strftime("%Y-%m-%d")


def journal(strategy: str, event: str,
            symbol: str | None = None,
            **fields: Any) -> None:
    """Append a structured decision record to state/journal_<date>.jsonl.

    Best-effort: failures (disk full, permission denied) print to stderr
    but never raise, so the bot keeps trading even if journaling is broken.
    """
    date_iso = _date_iso_et()
    path = STATE_DIR / f"journal_{date_iso}.jsonl"
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy": strategy,
        "event": event,
    }
    if symbol:
        record["symbol"] = symbol
    record.update(fields)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        sys.stderr.write(f"[journal] write failed (event={event}, sym={symbol}): {exc}\n")


def read_journal(date_iso: str | None = None) -> list[dict]:
    """Load all journal records for a given ET date (default: today)."""
    date_iso = date_iso or _date_iso_et()
    path = STATE_DIR / f"journal_{date_iso}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception as exc:
        sys.stderr.write(f"[journal] read failed: {exc}\n")
    return out


def summarize_by_strategy(date_iso: str | None = None) -> dict[str, dict]:
    """Roll the journal up into per-strategy counters for the EOD report.

    Returns: {strategy_name: {n_shortlisted, n_rejected, n_planned,
                              n_submitted, n_filled, n_breakeven,
                              n_exit_tp, n_exit_sl, n_cancelled,
                              n_force_closed}}
    """
    records = read_journal(date_iso)
    out: dict[str, dict] = {}
    for r in records:
        s = r.get("strategy", "?")
        ev = r.get("event", "?")
        bucket = out.setdefault(s, {
            "n_shortlisted": 0, "n_rejected": 0, "n_planned": 0,
            "n_submitted": 0, "n_filled": 0, "n_breakeven": 0,
            "n_exit_tp": 0, "n_exit_sl": 0,
            "n_cancelled": 0, "n_force_closed": 0,
        })
        if ev == "shortlisted":      bucket["n_shortlisted"] += 1
        elif ev == "rejected":       bucket["n_rejected"] += 1
        elif ev == "entry_planned":  bucket["n_planned"] += 1
        elif ev == "entry_submitted": bucket["n_submitted"] += 1
        elif ev == "entry_filled":    bucket["n_filled"] += 1
        elif ev == "breakeven_moved": bucket["n_breakeven"] += 1
        elif ev == "exit_filled":
            leg = (r.get("leg") or "").lower()
            if leg == "tp":   bucket["n_exit_tp"] += 1
            elif leg == "sl": bucket["n_exit_sl"] += 1
        elif ev == "entry_cancelled":  bucket["n_cancelled"] += 1
        elif ev == "force_closed":     bucket["n_force_closed"] += 1
    return out
