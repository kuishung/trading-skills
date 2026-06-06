"""Pipeline-run report — reads the per-run manifests that the ingest supervisor
(and the manual regen runner) write to ``<data_root>/pipeline_runs/run_*.json``,
so the /pipeline page shows ingest -> deep-check -> profiles freshness.

File-read only, like ``ingest_health`` — the supervisor side OWNS this data; the
dashboard just surfaces it. No ORM, no parquet opens. Soft-fail throughout:
returns None when not configured / the dir isn't present on this host.

Locates the dir from ``TST_PRICE_HISTORY_DIR`` (= <data_root>/price_history), so
pipeline_runs is its sibling.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from ..config import settings


def _runs_dir() -> Path | None:
    ph = settings.price_history_dir
    if not ph:
        return None
    d = Path(ph).parent / "pipeline_runs"
    return d if d.exists() else None


def _ago(secs: float) -> str:
    secs = int(secs)
    if secs < 90:
        return "just now"
    if secs // 60 < 90:
        return f"{secs // 60} min ago"
    if secs // 3600 < 48:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _tier(age: float | None) -> int:
    # green <26h, amber <74h, red older/unknown (mirrors ingest_health)
    return 2 if age is None else (0 if age < 26 * 3600 else (1 if age < 74 * 3600 else 2))


def list_runs(limit: int = 30) -> list[dict] | None:
    d = _runs_dir()
    if d is None:
        return None
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    now = _dt.datetime.now(_dt.timezone.utc)
    out: list[dict] = []
    for f in files:
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        age = now.timestamp() - f.stat().st_mtime
        m["_file"] = f.name
        m["_ago"] = _ago(age)
        m["_tier"] = _tier(age)
        # normalise phases to a list for easy templating
        phases = []
        for name, ph in (m.get("phases") or {}).items():
            if not isinstance(ph, dict):
                continue
            phases.append({"name": name, "status": ph.get("status", "?"), **ph})
        m["_phases"] = phases
        out.append(m)
    return out


def latest() -> dict | None:
    runs = list_runs(limit=1)
    return runs[0] if runs else None
