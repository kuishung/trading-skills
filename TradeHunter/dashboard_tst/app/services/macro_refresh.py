"""The Macro topics' series refresh (v4.243) - runs INSIDE the web app, in a daemon thread.

Once a day (and 90 s after the app starts) every active indicator in the store
(``services/indicators.py``: the seven Macro topics' series and the study board's) is fetched
again from FRED (the public CSV; no key needed) or Yahoo and stored - new observations added,
revised ones updated. A moderator can also run it now from Settings -> Data Ingest -> Macro
Series or the Macro page.

No Hermes task, no lock: the work is ~35 small HTTP requests a day. Status lives in ``_STATE``
(the Data Ingest card) and is mirrored to ``state/macro_refresh.json`` for the Hermes tray
(tray-sync rule). ``TST_MACRO_REFRESH=0`` switches the daily run off (a dev box).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("tst.macro_refresh")

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "macro_refresh.json"
ENV_OFF = "TST_MACRO_REFRESH"
FIRST_DELAY_S = 90
EVERY_S = 24 * 3600
TICK_S = 300

_LOCK = threading.Lock()
_STATE: dict = {
    "started": None, "running": False, "runs": 0, "last_started": None, "last_finished": None,
    "last_ms": None, "indicators": 0, "ok": 0, "failed": [], "added": 0, "revised": 0,
    "next_at": None, "last_error": None, "off": False, "results": [],
}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(t: _dt.datetime | None) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def _write_state(path: Path | None = None) -> None:
    p = path or STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {k: v for k, v in _STATE.items() if k != "results"}
        doc["written"] = _iso(_now())
        p.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("macro refresh state not written: %s", exc)


def run_once(db_factory=None, *, years: int = 25, state_path: Path | None = None) -> dict:
    """Refresh every active indicator. Returns the summary also kept in ``status()``.
    ``db_factory`` defaults to the app's ``SessionLocal``."""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "already running", **status()}
    t0 = time.monotonic()
    started = _now()
    _STATE.update(running=True, last_started=_iso(started), last_error=None)
    _write_state(state_path)
    results: list[dict] = []
    try:
        if db_factory is None:
            from ..db import SessionLocal as db_factory  # noqa: PLC0415
        from . import indicators  # noqa: PLC0415
        from ..models import MacroIndicator  # noqa: PLC0415

        db = db_factory()
        try:
            indicators.ensure_seeded(db)
            inds = (db.query(MacroIndicator).filter(MacroIndicator.active.is_(True))
                    .order_by(MacroIndicator.sort).all())
            for ind in inds:
                r = indicators.refresh(db, ind, years=years)
                r["label"] = ind.label
                r["source"] = f"{ind.source}:{ind.source_ref}"
                results.append(r)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        _STATE["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
        log.exception("macro refresh failed")
    finally:
        failed = [{"key": r["key"], "label": r.get("label"), "error": r.get("error")}
                  for r in results if not r.get("ok")]
        _STATE.update(
            running=False, runs=_STATE["runs"] + 1, last_finished=_iso(_now()),
            last_ms=int((time.monotonic() - t0) * 1000), indicators=len(results),
            ok=sum(1 for r in results if r.get("ok")), failed=failed,
            added=sum(r.get("added", 0) for r in results),
            revised=sum(r.get("revised", 0) for r in results),
            results=results,
        )
        _write_state(state_path)
        _LOCK.release()
    log.info("macro refresh: %d indicators, %d new, %d revised, %d failed, %d ms",
             _STATE["indicators"], _STATE["added"], _STATE["revised"], len(_STATE["failed"]), _STATE["last_ms"])
    return {"ok": True, **status()}


def _worker() -> None:
    nxt = time.monotonic() + FIRST_DELAY_S
    _STATE["next_at"] = _iso(_now() + _dt.timedelta(seconds=FIRST_DELAY_S))
    while True:
        time.sleep(min(TICK_S, max(1.0, nxt - time.monotonic())))
        if time.monotonic() < nxt:
            continue
        try:
            run_once()
        except Exception:  # noqa: BLE001
            log.exception("macro refresh worker")
        nxt = time.monotonic() + EVERY_S
        _STATE["next_at"] = _iso(_now() + _dt.timedelta(seconds=EVERY_S))
        _write_state()


def start_background() -> bool:
    """Start the daily refresh thread once. False when off (TST_MACRO_REFRESH=0) or started."""
    if (os.environ.get(ENV_OFF) or "").strip() == "0":
        _STATE["off"] = True
        _write_state()
        return False
    if _STATE["started"]:
        return False
    _STATE["started"] = _iso(_now())
    threading.Thread(target=_worker, daemon=True, name="macro-refresh").start()
    return True


def status() -> dict:
    """The worker's state for the Data Ingest card and the Macro page."""
    return dict(_STATE)


def read_state_file(path: Path | None = None) -> dict | None:
    """What the tray reads: the mirrored state file, or None when there is none."""
    p = path or STATE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
