"""The discussions sync (v4.275, DISCUSSIONS_DESIGN.md §4): a daemon thread inside the web app that
long-polls Telegram for the groups' topic messages and re-reads the Discord threads, storing every
member contribution through ``services/discussions``.

- Telegram: ``getUpdates`` with a 20 s long poll whenever a bot token is saved. The Bot API allows ONE
  poller per token (a second gets 409), so this is the only caller; the old ``/start`` answerer is
  folded in (``route_telegram_update`` answers a private ``/start`` with the chat id).
- Discord: every ``DISCORD_EVERY_S`` the threads of the discussions active in the last 30 days.
- State file ``dashboard_tst/state/discussions_sync.json`` for the Data Ingest card and the Hermes tray
  (the tray-sync rule): running, the last poll, counts, the groups the bot has seen (so the admin can
  pick the group id on the Integration card), the last error. ``TST_DISCUSSIONS_SYNC=0`` switches it off.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "discussions_sync.json"
DISCORD_EVERY_S = 60
POLL_TIMEOUT_S = 20
IDLE_SLEEP_S = 15            # when no bot is configured
ERROR_SLEEP_S = 10
CONFLICT_SLEEP_S = 60        # 409: another poller holds the token - back off
MAX_SEEN_GROUPS = 20

_lock = threading.Lock()
_started = False
_state: dict = {
    "running": False, "started_at": None, "off": False,
    "telegram": {"polling": False, "offset": 0, "last_poll": None, "updates_total": 0, "stored_total": 0,
                 "starts_answered": 0, "last_error": None, "seen_groups": []},
    "discord": {"last_pass": None, "threads": 0, "stored_total": 0, "last_error": None},
    "last_error": None,
}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_state(path: Path | None = None) -> None:
    p = path or STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            doc = json.loads(json.dumps(_state))
        p.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - a state file must never stop the sync
        log.debug("discussions sync state not written: %s", exc)


def _load_state(path: Path | None = None) -> None:
    p = path or STATE_PATH
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        with _lock:
            _state["telegram"]["offset"] = int((d.get("telegram") or {}).get("offset") or 0)
            _state["telegram"]["seen_groups"] = list((d.get("telegram") or {}).get("seen_groups") or [])[:MAX_SEEN_GROUPS]
    except Exception:  # noqa: BLE001
        pass


def status(path: Path | None = None) -> dict:
    """The state for the Data Ingest card (the live dict while this process runs the sync, else the file)."""
    if _started:
        with _lock:
            return json.loads(json.dumps(_state))
    p = path or STATE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"running": False, "absent": True}


def _remember_group(chat: dict | None) -> None:
    if not chat or not chat.get("id"):
        return
    with _lock:
        seen = [g for g in _state["telegram"]["seen_groups"] if str(g.get("id")) != str(chat["id"])]
        seen.insert(0, {"id": str(chat["id"]), "title": chat.get("title") or "", "is_forum": bool(chat.get("is_forum")),
                        "seen_at": _now_iso()})
        _state["telegram"]["seen_groups"] = seen[:MAX_SEEN_GROUPS]


def poll_telegram_once(db_factory=None) -> int:
    """One long poll; routes every update. Returns the number stored. Raises TelegramError."""
    from ..db import SessionLocal
    from . import discussions, telegram

    with _lock:
        offset = int(_state["telegram"]["offset"] or 0)
    updates = telegram.get_updates(offset or None, timeout=POLL_TIMEOUT_S)
    stored = 0
    nxt = offset
    if updates:
        db = (db_factory or SessionLocal)()
        try:
            for u in updates:
                try:
                    nxt = max(nxt, int(u.get("update_id", 0)) + 1)
                except (TypeError, ValueError):
                    pass
                try:
                    r = discussions.route_telegram_update(db, u)
                except Exception as exc:  # noqa: BLE001 - one bad update must not stop the rest
                    log.warning("discussions: telegram update not routed: %s", exc)
                    db.rollback()
                    continue
                _remember_group(r.get("chat"))
                stored += int(bool(r.get("stored")))
                if r.get("start"):
                    with _lock:
                        _state["telegram"]["starts_answered"] += 1
        finally:
            db.close()
    with _lock:
        t = _state["telegram"]
        t["offset"] = nxt
        t["last_poll"] = _now_iso()
        t["updates_total"] += len(updates)
        t["stored_total"] += stored
        t["last_error"] = None
    return stored


def discord_pass(db_factory=None) -> tuple[int, int]:
    """Re-read every recent discussion's Discord thread. Returns (threads read, messages stored)."""
    from ..db import SessionLocal
    from . import discussions

    db = (db_factory or SessionLocal)()
    threads = stored = 0
    try:
        for d in discussions.recent_for_sync(db):
            n = discussions.sync_discord(db, d)
            threads += 1
            if n > 0:
                stored += n
    finally:
        db.close()
    with _lock:
        dd = _state["discord"]
        dd["last_pass"] = _now_iso()
        dd["threads"] = threads
        dd["stored_total"] += stored
        dd["last_error"] = None
    return threads, stored


def _loop() -> None:
    from . import discord, telegram

    _load_state()
    with _lock:
        _state["running"] = True
        _state["started_at"] = _now_iso()
    _write_state()
    last_discord = 0.0
    while True:
        slept = False
        try:
            if telegram.configured():
                with _lock:
                    _state["telegram"]["polling"] = True
                try:
                    poll_telegram_once()
                except telegram.TelegramError as exc:
                    with _lock:
                        _state["telegram"]["last_error"] = str(exc)[:200]
                        _state["telegram"]["polling"] = False
                    time.sleep(CONFLICT_SLEEP_S if "409" in str(exc) else ERROR_SLEEP_S)
                    slept = True
            else:
                with _lock:
                    _state["telegram"]["polling"] = False
            if discord.bot_configured() and time.time() - last_discord >= DISCORD_EVERY_S:
                try:
                    discord_pass()
                except Exception as exc:  # noqa: BLE001
                    with _lock:
                        _state["discord"]["last_error"] = str(exc)[:200]
                last_discord = time.time()
        except Exception as exc:  # noqa: BLE001 - the loop never dies
            with _lock:
                _state["last_error"] = str(exc)[:200]
            log.warning("discussions sync: %s", exc)
            time.sleep(ERROR_SLEEP_S)
            slept = True
        _write_state()
        if not slept and not telegram.configured():
            time.sleep(IDLE_SLEEP_S)


def start_background() -> bool:
    """Start the sync thread once per process. False when switched off or already running."""
    global _started
    if _started:
        return False
    if os.environ.get("TST_DISCUSSIONS_SYNC", "1").strip() in ("0", "false", "off", "no"):
        with _lock:
            _state["off"] = True
        _write_state()
        return False
    _started = True
    threading.Thread(target=_loop, daemon=True, name="discussions-sync").start()
    return True
