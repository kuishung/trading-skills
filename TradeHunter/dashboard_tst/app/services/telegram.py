"""Telegram for the Options page: the sender, the ``/start`` answerer and the
handshake code (OPTIONS_MODULE_DESIGN.md II.2.15; Part D §D4.1).

Credentials come from the intraday bot's lookup (``scripts/_common.telegram_env``:
``INTRADAY_ENV_DIR`` -> ``TradeHunter/.env`` -> the vault's ``telegram.env``, then
``matp.env`` as the legacy filename) so the bot token lives in the vault ONCE. What
that lookup cannot give is a chat id per member - ``send_telegram`` posts to the
vault's single ``TELEGRAM_CHAT_ID`` - so the 20-line POST is repeated here with a
``chat_id`` parameter; the chunker (4000 characters, split on lines) and the
headers are the same as ``_common.send_telegram``'s, so the two behave identically.
The vault's chat id remains the administrator's default.

Everything here is soft-fail: a missing token makes ``configured()`` False and
``send`` returns ``(False, "not configured")``; a network error is returned, never
raised, except from ``send_code`` (whose caller must know the code did not go out).
``_api`` is the ONE network seam, which is what the tests replace.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from . import resources_bridge  # noqa: F401  - puts TradeHunter/ on sys.path
from scripts import _common as thc  # noqa: E402  - stdlib-only at import time
from . import clock, job_runs

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_CHUNK = 4000            # Telegram caps a message at 4096; split on lines before that
TIMEOUT = 15.0
USER_AGENT = "TradeHunter/0.7"   # the same string _common.send_telegram sends
CODE_TTL_MINUTES = 10

START_REPLY = ("Your chat id is {chat_id}. Enter it on the Options page → My rules "
               "→ Shared and press Send code.")
CODE_TEXT = ("Your TradeHunter code is <b>{code}</b>. Enter it on the Options page "
             "→ My rules → Shared and press Verify. It expires in 10 minutes.")


class TelegramError(RuntimeError):
    """The Bot API said no, or could not be reached."""


# ───────────────────────────────────── credentials ─────────────────────────────────────

def creds() -> tuple[str | None, str | None]:
    """``(token, default_chat_id)``: v4.193 - what is saved on the Settings gear ->
    Integration page first, then the vault lookup (``telegram.env``); ``(None, None)`` when
    the bot is not configured anywhere. Each half falls back on its own."""
    from . import app_settings  # noqa: PLC0415

    v_token, v_chat = None, None
    try:
        v_token, v_chat = thc.telegram_env(None)
    except Exception as exc:  # noqa: BLE001 - a broken vault must not break the page
        log.warning("telegram credentials lookup failed: %s", exc)
    return app_settings.get("telegram_bot_token", v_token), app_settings.get("telegram_chat_id", v_chat)


def configured() -> bool:
    """True when a bot token resolves (the chat id is per member, so it is not required)."""
    token, _ = creds()
    return bool(token)


def default_chat_id() -> str | None:
    """The vault's ``TELEGRAM_CHAT_ID`` - the administrator's fallback when their
    own chat id is blank."""
    _, chat_id = creds()
    return chat_id


# ─────────────────────────────────────── the API ───────────────────────────────────────

def _api(method: str, data: dict, *, timeout: float | None = None) -> dict:
    """POST ``data`` (form-encoded, like ``_common.send_telegram``) to a Bot API
    method and return the decoded body. Raises ``TelegramError`` when the token is
    missing, the request fails, or the body says ``ok: false``. ``timeout`` (v4.275)
    lets a long-poll ``getUpdates`` wait longer than the default."""
    token, _ = creds()
    if not token:
        raise TelegramError("not configured")
    body = urllib.parse.urlencode({k: v for k, v in data.items() if v is not None}).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/bot{token}/{method}", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("description") or ""
        except Exception:  # noqa: BLE001
            detail = ""
        raise TelegramError(f"HTTP {exc.code} {detail}".strip()) from exc
    except Exception as exc:  # noqa: BLE001 - DNS, timeout, refused
        raise TelegramError(str(exc) or type(exc).__name__) from exc
    if not isinstance(out, dict) or not out.get("ok"):
        raise TelegramError(str((out or {}).get("description") or out)[:300])
    return out


def chunks(html: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split a message on line breaks so no piece exceeds ``limit`` characters -
    ``_common.send_telegram``'s rule, so both senders cut the same way."""
    if len(html) <= limit:
        return [html]
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for line in html.split("\n"):
        if size + len(line) + 1 > limit and buf:
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out


def send(html: str, *, chat_id: str | None = None) -> tuple[bool, str]:
    """One message (HTML parse mode, chunked at 4000 characters) to ``chat_id``, or
    to the vault's default chat when ``chat_id`` is None. Returns ``(ok, error)``;
    never raises."""
    target = str(chat_id or default_chat_id() or "").strip()
    if not configured():
        return False, "not configured"
    if not target:
        return False, "no chat id"
    errors: list[str] = []
    for chunk in chunks(html):
        try:
            _api("sendMessage", {"chat_id": target, "text": chunk, "parse_mode": "HTML",
                                 "disable_web_page_preview": "true"})
        except TelegramError as exc:
            errors.append(str(exc))
            log.warning("telegram send to %s failed: %s", target, exc)
    return (not errors), "; ".join(errors)


# ─────────────────────────── v4.275: discussions - topics in a group ───────────────────────────

def group_id() -> str | None:
    """The group where discussions open as topics (Settings → Integration, ``telegram_group_id``)."""
    from . import app_settings  # noqa: PLC0415

    v = app_settings.get("telegram_group_id", None)
    return str(v).strip() if v else None


def send_message(html: str, *, chat_id: str, thread_id: str | int | None = None,
                 reply_to: str | int | None = None) -> dict:
    """One message into a chat - and, with ``thread_id``, into that topic of a forum group.
    Returns the sent message (``message_id`` etc.); raises ``TelegramError``. Long texts are
    chunked like :func:`send`; the FIRST chunk's message is returned."""
    first: dict | None = None
    for chunk in chunks(html):
        out = _api("sendMessage", {"chat_id": str(chat_id), "text": chunk, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true",
                                   "message_thread_id": str(thread_id) if thread_id else None,
                                   "reply_to_message_id": str(reply_to) if reply_to else None})
        if first is None:
            first = out.get("result") or {}
    return first or {}


def send_photo(image: bytes, *, chat_id: str, caption_html: str = "", thread_id: str | int | None = None,
               filename: str = "snapshot.png") -> dict:
    """v4.279: a PNG into a chat (and, with ``thread_id``, into that topic) with an HTML caption of at most
    1024 characters - the chart snapshot of a shared option setup. Multipart through httpx (the Bot API's
    ``sendPhoto``). Returns the sent message; raises ``TelegramError``."""
    import httpx  # noqa: PLC0415 - the only multipart call in this module

    token, _ = creds()
    if not token:
        raise TelegramError("not configured")
    data = {"chat_id": str(chat_id), "parse_mode": "HTML"}
    if caption_html:
        data["caption"] = caption_html[:1024]
    if thread_id:
        data["message_thread_id"] = str(thread_id)
    try:
        r = httpx.post(f"{API}/bot{token}/sendPhoto", data=data, files={"photo": (filename, image, "image/png")},
                       headers={"User-Agent": USER_AGENT}, timeout=30.0)
        out = r.json()
    except Exception as exc:  # noqa: BLE001
        raise TelegramError(str(exc) or type(exc).__name__) from exc
    if not isinstance(out, dict) or not out.get("ok"):
        raise TelegramError(str((out or {}).get("description") or out)[:300])
    return out.get("result") or {}


def create_forum_topic(chat_id: str, name: str) -> str:
    """Open a topic named ``name`` in a group with Topics switched on; the bot must be an
    administrator with Manage Topics. Returns the topic's ``message_thread_id`` (a string);
    raises ``TelegramError`` with Telegram's words otherwise."""
    out = _api("createForumTopic", {"chat_id": str(chat_id), "name": (name or "Discussion")[:128]})
    tid = (out.get("result") or {}).get("message_thread_id")
    if not tid:
        raise TelegramError("createForumTopic returned no topic id")
    return str(tid)


def get_updates(offset: int | None, *, timeout: int = 20) -> list[dict]:
    """Long-poll the bot's updates (messages only). ONE poller per token - the Bot API answers
    409 to a second one - so only the discussions sync calls this. Raises ``TelegramError``."""
    out = _api("getUpdates", {"offset": offset or None, "timeout": timeout,
                              "allowed_updates": json.dumps(["message", "my_chat_member"])},
               timeout=timeout + 15)
    return list(out.get("result") or [])


def topic_link(chat_id: str | None, topic_id: str | None) -> str | None:
    """``https://t.me/c/<group id without -100>/<topic id>`` - opens for members of the group."""
    if not chat_id or not topic_id:
        return None
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    elif cid.startswith("-"):
        cid = cid[1:]
    return f"https://t.me/c/{cid}/{topic_id}"


# ─────────────────────────────────── the handshake ───────────────────────────────────

def answer_starts(db) -> int:
    """Poll ``getUpdates`` and answer every ``/start`` with that chat's id (the first
    half of the handshake: Telegram only lets a bot message a person after they
    wrote to it first). The update offset is kept in the ONE
    ``option_jobs(job='telegram_poll')`` row's ``detail`` (re-used, not appended, so
    the ledger does not grow by one row per poll). Returns the number answered;
    soft-fail (0 and a log line) when the bot is not configured or unreachable.
    Called by the Telegram link handshake (the Options ideas push was removed in v4.133)."""
    if not configured():
        return 0
    row = job_runs.latest_any(db, "telegram_poll")
    prev = dict(getattr(row, "detail", None) or {}) if row is not None else {}
    offset = int(prev.get("offset") or 0)
    try:
        body = _api("getUpdates", {"offset": offset or None, "timeout": 0,
                                   "allowed_updates": json.dumps(["message"])})
    except TelegramError as exc:
        log.warning("telegram getUpdates failed: %s", exc)
        return 0
    updates = body.get("result") or []
    answered = 0
    nxt = offset
    for u in updates:
        try:
            nxt = max(nxt, int(u.get("update_id", 0)) + 1)
        except (TypeError, ValueError):
            pass
        msg = u.get("message") or {}
        text = str(msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if chat is None or not text.lower().startswith("/start"):
            continue
        ok, _err = send(START_REPLY.format(chat_id=chat), chat_id=str(chat))
        answered += int(ok)
    if row is None:
        row = job_runs.start(db, "telegram_poll", clock.et_today(), source="telegram")
    detail = {"offset": nxt, "last_poll": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
              "answered_total": int(prev.get("answered_total") or 0) + answered}
    job_runs.finish(db, row, ok=answered, rows=len(updates), detail=detail,
                    note="getUpdates offset for the /start answerer")
    return answered


def new_code() -> str:
    """A fresh 6-digit code (leading zeros kept)."""
    return f"{secrets.randbelow(10 ** 6):06d}"


def send_code(chat_id: str) -> str:
    """Send a 6-digit code to ``chat_id`` and return it (the second half of the
    handshake, D4.2: the drawer accepts the chat id only with this code). Raises
    ``TelegramError`` when the code could not be delivered - the caller must not
    store a pending code nobody received."""
    code = new_code()
    ok, err = send(CODE_TEXT.format(code=code), chat_id=str(chat_id))
    if not ok:
        raise TelegramError(err or "send failed")
    return code
