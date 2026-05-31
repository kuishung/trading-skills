"""MATP run archive — one JSON file per run, in C:\\HermesSync\\MarketData\\MATP
(Resilio-synced) on Hermes, or <TradeHunter>/data/MATP by default.

This is the durable raw-extraction record (the "cream"): every /api/matp run is
written here verbatim. The dashboard's live views still read tst.db (Postgres-
portable); these files are the audit/archive trail you can browse per ticker.

One file per run keeps each run a self-contained snapshot:
    run_<UTC stamp>[_f<filter_id>].json
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from ..config import settings


def matp_dir() -> Path:
    """Resolve + ensure the MATP archive directory."""
    if settings.matp_dir:
        p = Path(settings.matp_dir)
    else:
        # dashboard_tst/app/services/ -> parents[3] == TradeHunter/
        p = Path(__file__).resolve().parents[3] / "data" / "MATP"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_run(payload, now: _dt.datetime, *, filter_desc: str | None = None) -> str | None:
    """Write one run archive file. `payload` is the MatpIngest pydantic model.
    Returns the filename, or None on failure (archiving must never break ingest).
    """
    try:
        try:
            data = payload.model_dump(mode="json")  # pydantic v2
        except AttributeError:
            data = payload.dict()  # pydantic v1 fallback
        stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
        suffix = f"_f{payload.filter_id}" if getattr(payload, "filter_id", None) else ""
        name = f"run_{stamp}{suffix}.json"
        record = {
            "saved_at": now.isoformat(),
            "source": getattr(payload, "source", None),
            "filter_id": getattr(payload, "filter_id", None),
            "filter_desc": filter_desc,
            "prune": getattr(payload, "prune", None),
            "items": data.get("items", []),
        }
        (matp_dir() / name).write_text(json.dumps(record, indent=2), encoding="utf-8")
        return name
    except Exception:
        return None


def runs_for_symbol(symbol: str, *, limit: int = 50) -> list[dict]:
    """Newest-first list of archived runs that included `symbol`. Each entry:
    {file, saved_at, source, filter_desc, item} where `item` is that ticker's
    extracted slice (matp/mbp/n_targets/last_earnings_date/targets/...)."""
    sym = symbol.strip().upper()
    out: list[dict] = []
    d = matp_dir()
    # newest-first by filename (timestamp-sortable), bounded
    files = sorted(d.glob("run_*.json"), reverse=True)
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        item = next(
            (it for it in rec.get("items", []) if (it.get("symbol") or "").upper() == sym),
            None,
        )
        if item is None:
            continue
        out.append(
            {
                "file": f.name,
                "saved_at": rec.get("saved_at"),
                "source": rec.get("source"),
                "filter_desc": rec.get("filter_desc"),
                "item": item,
            }
        )
        if len(out) >= limit:
            break
    return out
