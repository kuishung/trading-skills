"""Settings an administrator edits in the app (v4.193) - the Settings gear -> Integration page.

The user: *"Integration where discord and telegram will be set here"*. Until now the Discord
webhook / bot and the Telegram bot lived only in ``app\\.env`` on Hermes (and the vault's
``telegram.env``). The ``app_settings`` table (key / value, migration ``b9d2e4f6a8c1``) lets
them be set from the page: a saved value WINS over the environment, a blank or missing row
FALLS BACK to it, so nothing already configured on Hermes changes until someone types a value.

``get(key)`` is what the senders call (``services/discord.py``, ``services/telegram.py``): it
reads the table through its own short session, cached for ``TTL`` seconds so a burst of posts
costs one query, and never raises - a DB problem means the environment value. ``describe()``
is the page's view (masked values, where each comes from). Secrets are never logged or echoed
back in full: ``mask()`` keeps the last four characters.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# key -> (label, environment variable it falls back to, secret?)
KEYS: dict[str, tuple[str, str, bool]] = {
    "discord_webhook_url": ("Discord webhook URL", "TST_DISCORD_WEBHOOK_URL", True),
    "discord_bot_token": ("Discord bot token", "TST_DISCORD_BOT_TOKEN", True),
    "discord_channel_id": ("Discord channel id", "TST_DISCORD_CHANNEL_ID", False),
    "discord_guild_id": ("Discord server (guild) id", "TST_DISCORD_GUILD_ID", False),
    "telegram_bot_token": ("Telegram bot token", "TELEGRAM_BOT_TOKEN", True),
    "telegram_chat_id": ("Telegram default chat id", "TELEGRAM_CHAT_ID", False),
}
TTL = 10.0                   # seconds a read is reused before the table is asked again
MAX_LEN = 2000

_lock = threading.Lock()
_cache: dict[str, tuple[float, str | None]] = {}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _read_db(key: str) -> str | None:
    """The stored value (None = no row or blank), through a short session of the app DB."""
    from ..db import SessionLocal  # noqa: PLC0415
    from ..models import AppSetting  # noqa: PLC0415

    s = SessionLocal()
    try:
        row = s.get(AppSetting, key)
        v = (row.value or "").strip() if row is not None else ""
        return v or None
    finally:
        s.close()


def saved(key: str) -> str | None:
    """The value SAVED in the app (cached ``TTL`` seconds), None when none or unreadable."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    try:
        v = _read_db(key)
    except Exception as exc:  # noqa: BLE001 - the environment value stands in
        log.debug("app setting %s unreadable: %s", key, exc)
        v = None
    with _lock:
        _cache[key] = (now, v)
    return v


def env_value(key: str) -> str | None:
    """The environment fallback for ``key`` (the process environment; ``app\\.env`` is loaded
    into it at start-up)."""
    spec = KEYS.get(key)
    if not spec:
        return None
    v = os.environ.get(spec[1])
    return v.strip() if v and v.strip() else None


def get(key: str, fallback: str | None = None) -> str | None:
    """The value in force: saved in the app, else the environment, else ``fallback``."""
    return saved(key) or env_value(key) or fallback


def mask(v: str | None) -> str:
    """``••••1234`` for a secret (the last four characters), ``-`` for none."""
    if not v:
        return "-"
    v = str(v)
    return ("•" * 4) + v[-4:] if len(v) > 4 else "•" * len(v)


def set_many(db, user, values: dict) -> list[str]:
    """Save ``{key: value}`` (unknown keys ignored; a blank value clears the row so the
    environment stands in again). Returns the keys changed. Nothing is logged in full."""
    from ..models import AppSetting  # noqa: PLC0415

    changed: list[str] = []
    who = getattr(user, "email", None)
    for key, raw in (values or {}).items():
        if key not in KEYS:
            continue
        v = (str(raw) if raw is not None else "").strip()[:MAX_LEN]
        row = db.get(AppSetting, key)
        if not v:
            if row is not None and (row.value or "").strip():
                row.value = None
                row.updated_by = who
                row.updated_at = _utcnow()
                changed.append(key)
            continue
        if row is None:
            db.add(AppSetting(key=key, value=v, updated_by=who, updated_at=_utcnow()))
            changed.append(key)
        elif (row.value or "") != v:
            row.value = v
            row.updated_by = who
            row.updated_at = _utcnow()
            changed.append(key)
    if changed:
        db.commit()
        clear_cache()
    return changed


def describe(db=None) -> list[dict]:
    """The page's rows: ``[{key, label, secret, source ('saved' | 'env' | 'none'), shown,
    plain, env_name}]`` - ``shown`` is masked for a secret, ``plain`` is the value for a
    non-secret field (prefilled in its input)."""
    from ..models import AppSetting  # noqa: PLC0415

    out = []
    for key, (label, env_name, secret) in KEYS.items():
        sv = None
        if db is not None:
            try:
                row = db.get(AppSetting, key)
                sv = (row.value or "").strip() or None if row is not None else None
            except Exception:  # noqa: BLE001
                sv = None
        else:
            sv = saved(key)
        ev = env_value(key)
        if sv:
            source, val = "saved", sv
        elif ev:
            source, val = "env", ev
        else:
            source, val = "none", None
        out.append({"key": key, "label": label, "secret": secret, "source": source, "env_name": env_name,
                    "shown": mask(val) if secret else (val or "-"), "plain": "" if secret else (val or "")})
    return out


__all__ = ["KEYS", "TTL", "get", "saved", "env_value", "mask", "set_many", "describe", "clear_cache"]
