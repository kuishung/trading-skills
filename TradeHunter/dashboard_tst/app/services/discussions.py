"""Discussions (v4.275, DISCUSSIONS_DESIGN.md): one thread per subject on Discord and / or Telegram,
the members' contributions read back and kept.

The user (2026-11-26): *"we moderator put up a topic or study in the chat either discord or telegram,
there will be a thread created for that topic or studies and user can contribute in the thread which
can be read in tradehunter"* - and: build the STRUCTURE first; which subjects open a discussion, and
when, is not decided. So this module knows nothing about studies, curated calls, macro topics or
companies beyond a ``(kind, ref)`` key and a link; whatever decides to open a discussion calls
:func:`open_discussion`, and every page that owns a subject can show its thread with
:func:`for_subject` + :func:`messages`.

Channels: Discord = a thread in the configured channel (the bot; the study threads' mechanism);
Telegram = a forum TOPIC in the group saved as ``telegram_group_id`` (Topics on, the bot an admin).
A channel that cannot be opened is recorded in words in ``channel_errors`` and never blocks the other.
The sync (``discussion_sync``) reads both chats and upserts :class:`Contribution` rows, deduplicated
per channel by the chat's message id; the bot's own messages (the opener, the mirrored app posts) are
skipped, so nothing is stored twice. An app post is mirrored as "Name: text" into both chats.
Soft-fail throughout: the chats are an extension of the page, never a dependency of it.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import logging

from sqlalchemy.orm import Session

from ..config import settings
from ..models import DISCUSSION_KINDS, Contribution, Discussion, User
from . import discord, telegram

log = logging.getLogger(__name__)

KIND_LABELS = {"setup": "Study", "curated": "Curated setup", "macro": "Macro", "company": "Company", "topic": "Topic"}
CHANNELS = ("discord", "telegram")
SYNC_WINDOW_DAYS = 30          # discussions active within this many days are re-read by the sync
MAX_TEXT = 4000


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _name(user: User | None) -> str:
    if user is None:
        return "TradeHunter"
    return getattr(user, "display_name", None) or getattr(user, "email", None) or "member"


def subject_url(kind: str, ref: str | None) -> str | None:
    """Where the subject lives in TradeHunter (None for a free topic)."""
    r = (ref or "").strip()
    if kind == "setup" and r:
        return f"/studies/{r}"
    if kind == "curated":
        return "/curated" + (f"?id={r}" if r else "")
    if kind == "macro":
        return "/macro" + (f"#{r}" if r else "")
    if kind == "company" and r:
        return f"/company-analysis?symbol={r.upper()}"
    return None


def page_url(d: Discussion) -> str:
    return f"/discussions?id={d.id}"


def links(d: Discussion) -> dict:
    """The chat links for a discussion: ``{"discord": url | None, "telegram": url | None}``."""
    return {"discord": discord.thread_link(d.discord_thread_id) if d.discord_thread_id else None,
            "telegram": telegram.topic_link(d.telegram_chat_id, d.telegram_topic_id)}


def _opener_text(d: Discussion, by: str) -> str:
    base = (settings.public_url or "").rstrip("/")
    lines = [d.title.strip()]
    if d.body:
        lines.append(d.body.strip()[:1500])
    lines.append(f"opened by {by} on TradeHunter" + (f" - {base}{page_url(d)}" if base else ""))
    return "\n".join(lines)


# ─────────────────────────────────────── opening ───────────────────────────────────────

def open_discussion(db: Session, *, kind: str, ref: str | None, title: str, body: str | None, user: User | None,
                    channels=CHANNELS, discord_thread_id: str | None = None) -> Discussion:
    """Create the discussion and open it on each asked-for channel that is configured. ``discord_thread_id``
    adopts a thread that already exists (a study's) instead of opening one."""
    if kind not in DISCUSSION_KINDS:
        raise ValueError(f"unknown discussion kind {kind!r}")
    title = (title or "").strip()[:200] or f"{KIND_LABELS.get(kind, kind)} {ref or ''}".strip()
    d = Discussion(kind=kind, ref=(ref or "").strip()[:80] or None, title=title, body=(body or "").strip() or None,
                   created_by=getattr(user, "id", None), channel_errors={})
    db.add(d)
    db.flush()                                      # an id for the links
    by = _name(user)
    opener = _opener_text(d, by)
    errors: dict = {}
    wanted = set(channels or ())
    # Discord: a thread in the configured channel (the bot)
    if discord_thread_id:
        d.discord_thread_id = str(discord_thread_id)
        d.discord_channel_id = discord._cfg("discord_channel_id")  # noqa: SLF001
    elif "discord" in wanted:
        if not discord.bot_configured():
            errors["discord"] = "no Discord bot token / channel id saved (Settings → Integration)"
        else:
            try:
                base = (settings.public_url or "").rstrip("/")
                embed = {"title": title[:256], "description": ((d.body or "")[:1800] + f"\n\nopened by {by}").strip(),
                         "color": discord.COLOR_EMERALD}
                if base:
                    embed["url"] = base + page_url(d)
                tid = discord.create_study_thread(name=title[:100], embed=embed)
                if tid:
                    d.discord_thread_id = str(tid)
                    d.discord_channel_id = discord._cfg("discord_channel_id")  # noqa: SLF001
                else:
                    errors["discord"] = "Discord refused to open the thread (see the server log: the bot's permissions in the channel)"
            except Exception as exc:  # noqa: BLE001
                errors["discord"] = f"{type(exc).__name__}: {exc}"[:300]
    # Telegram: a topic in the group with Topics on
    if "telegram" in wanted:
        gid = telegram.group_id()
        if not telegram.configured():
            errors["telegram"] = "no Telegram bot token saved (Settings → Integration)"
        elif not gid:
            errors["telegram"] = "no Telegram group id saved (Settings → Integration → Telegram group id)"
        else:
            try:
                topic = telegram.create_forum_topic(gid, title)
                d.telegram_chat_id = gid
                d.telegram_topic_id = topic
                try:
                    telegram.send_message(_html.escape(opener), chat_id=gid, thread_id=topic)
                except telegram.TelegramError as exc:
                    errors["telegram"] = f"topic opened, but the opener was not posted: {exc}"[:300]
            except telegram.TelegramError as exc:
                why = str(exc)
                if "not a forum" in why.lower() or "forum" in why.lower() and "not" in why.lower():
                    why += " - switch Topics on in the group's settings"
                elif "not enough rights" in why.lower() or "administrator" in why.lower():
                    why += " - make the bot an administrator with Manage Topics"
                elif "chat not found" in why.lower():
                    why += " - the group id is wrong, or the bot is not in that group"
                errors["telegram"] = why[:300]
    d.channel_errors = errors or None
    now = _utcnow()
    db.add(Contribution(discussion_id=d.id, channel="app", external_id=f"app:opener:{d.id}", author_name=by,
                        user_id=getattr(user, "id", None), text=opener, posted_at=now, is_opener=True))
    d.n_contributions = 1
    d.last_activity_at = now
    db.commit()
    db.refresh(d)
    return d


def for_subject(db: Session, kind: str, ref: str | None, *, open_only: bool = True) -> Discussion | None:
    """The newest discussion of a subject (open ones first)."""
    q = db.query(Discussion).filter(Discussion.kind == kind, Discussion.ref == ((ref or "").strip() or None))
    if open_only:
        q = q.filter(Discussion.status == "open")
    return q.order_by(Discussion.id.desc()).first()


def close(db: Session, d: Discussion) -> None:
    d.status = "closed"
    d.closed_at = _utcnow()
    db.commit()


def reopen(db: Session, d: Discussion) -> None:
    d.status = "open"
    d.closed_at = None
    db.commit()


# ─────────────────────────────────────── posting ───────────────────────────────────────

def post(db: Session, d: Discussion, text: str, user: User | None) -> Contribution | None:
    """An app post: stored, then mirrored as "Name: text" into every chat the discussion has."""
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        return None
    by = _name(user)
    c = Contribution(discussion_id=d.id, channel="app", author_name=by, user_id=getattr(user, "id", None),
                     text=text, posted_at=_utcnow(), mirrored={})
    db.add(c)
    db.flush()
    c.external_id = f"app:{c.id}"
    mirrored: dict = {}
    line = f"{by}: {text}"
    if d.discord_thread_id and discord.bot_configured():
        try:
            mirrored["discord"] = bool(discord.post_thread_message(d.discord_thread_id, line[:1900]))
        except Exception as exc:  # noqa: BLE001
            mirrored["discord"] = f"error: {exc}"[:200]
    if d.telegram_chat_id and d.telegram_topic_id and telegram.configured():
        try:
            m = telegram.send_message(_html.escape(line), chat_id=d.telegram_chat_id, thread_id=d.telegram_topic_id)
            mirrored["telegram"] = str(m.get("message_id") or True)
        except telegram.TelegramError as exc:
            mirrored["telegram"] = f"error: {exc}"[:200]
    c.mirrored = mirrored or None
    d.n_contributions = (d.n_contributions or 0) + 1
    d.last_activity_at = c.posted_at
    db.commit()
    return c


# ─────────────────────────────────────── reading ───────────────────────────────────────

def messages(db: Session, d: Discussion, *, me: User | None = None) -> list[dict]:
    """Every contribution, oldest first, shaped for a chat strip."""
    out = []
    my_id = getattr(me, "id", None)
    for c in db.query(Contribution).filter(Contribution.discussion_id == d.id).order_by(Contribution.posted_at, Contribution.id).all():
        ts = c.posted_at.strftime("%Y-%m-%d %H:%M:%S") if c.posted_at else ""
        out.append({"id": c.id, "author": c.author_name or "member", "text": c.text, "ts": ts,
                    "ts_iso": (ts.replace(" ", "T") + "Z") if ts else "", "channel": c.channel,
                    "source": "web" if c.channel == "app" else c.channel,
                    "me": bool(my_id and c.user_id == my_id), "is_opener": bool(c.is_opener)})
    return out


def recent_for_sync(db: Session, *, days: int = SYNC_WINDOW_DAYS) -> list[Discussion]:
    """Open discussions with a Discord thread, active (or opened) within the window - newest first."""
    since = _utcnow() - _dt.timedelta(days=days)
    rows = (db.query(Discussion).filter(Discussion.status == "open", Discussion.discord_thread_id.isnot(None)).all())
    rows = [d for d in rows if (d.last_activity_at or d.created_at or since) >= since]
    rows.sort(key=lambda d: d.last_activity_at or d.created_at or since, reverse=True)
    return rows


def _store(db: Session, d: Discussion, *, channel: str, external_id: str, author_id: str | None, author_name: str,
           text: str, posted_at: _dt.datetime, reply_to: str | None) -> bool:
    """Upsert one chat message; True when it was new."""
    if not external_id:
        return False
    exists = db.query(Contribution.id).filter(Contribution.discussion_id == d.id, Contribution.channel == channel,
                                               Contribution.external_id == external_id).first()
    if exists:
        return False
    db.add(Contribution(discussion_id=d.id, channel=channel, external_id=external_id, author_external_id=author_id,
                        author_name=(author_name or "member")[:120], text=(text or "")[:MAX_TEXT] or "(no text)",
                        posted_at=posted_at, reply_to_external_id=reply_to))
    d.n_contributions = (d.n_contributions or 0) + 1
    if d.last_activity_at is None or posted_at > d.last_activity_at:
        d.last_activity_at = posted_at
    return True


def sync_discord(db: Session, d: Discussion) -> int:
    """Read the discussion's Discord thread and store what is new. Returns the number stored;
    -1 when the thread could not be read."""
    if not d.discord_thread_id:
        return 0
    msgs = discord.fetch_thread_messages(d.discord_thread_id, limit=100)
    if msgs is None:
        return -1
    n = 0
    for m in msgs:
        if m.get("bot"):
            continue                                   # the opener embed and our mirrored posts
        text = m.get("content") or m.get("embed_text") or ("(attachment)" if m.get("has_attach") else "")
        try:
            when = _dt.datetime.strptime(m.get("ts") or "", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            when = _utcnow()
        if _store(db, d, channel="discord", external_id=m.get("id") or "", author_id=m.get("author_id"),
                  author_name=m.get("author") or "member", text=text, posted_at=when, reply_to=m.get("reply_to")):
            n += 1
    d.last_synced_at = _utcnow()
    db.commit()
    return n


def route_telegram_update(db: Session, upd: dict) -> dict:
    """One update from ``getUpdates``. Returns ``{"stored": bool, "chat": {...} | None, "start": bool}`` -
    ``chat`` is any group the bot saw (for the Integration card's "groups the bot has seen"), ``start`` a
    ``/start`` in a private chat that was answered with the chat id (the link-up handshake's first half)."""
    out = {"stored": False, "chat": None, "start": False}
    msg = upd.get("message") or upd.get("channel_post")
    if not msg and upd.get("my_chat_member"):
        chat = (upd["my_chat_member"].get("chat") or {})
        if chat.get("type") in ("group", "supergroup"):
            out["chat"] = {"id": str(chat.get("id")), "title": chat.get("title") or "", "is_forum": bool(chat.get("is_forum"))}
        return out
    if not msg:
        return out
    chat = msg.get("chat") or {}
    if chat.get("type") in ("group", "supergroup"):
        out["chat"] = {"id": str(chat.get("id")), "title": chat.get("title") or "", "is_forum": bool(chat.get("is_forum"))}
    frm = msg.get("from") or {}
    text = msg.get("text") or msg.get("caption") or ""
    if chat.get("type") == "private" and text.strip().lower().startswith("/start"):
        try:
            telegram.send(telegram.START_REPLY.format(chat_id=chat.get("id")), chat_id=str(chat.get("id")))
            out["start"] = True
        except Exception:  # noqa: BLE001
            pass
        return out
    thread_id = msg.get("message_thread_id")
    if not thread_id or frm.get("is_bot"):
        return out                                      # not in a topic, or the bot's own (opener, mirrored posts)
    d = (db.query(Discussion).filter(Discussion.telegram_chat_id == str(chat.get("id")),
                                     Discussion.telegram_topic_id == str(thread_id)).order_by(Discussion.id.desc()).first())
    if d is None:
        return out
    if not text:
        text = "(attachment)" if any(k in msg for k in ("photo", "document", "video", "voice", "sticker", "audio")) else ""
    if not text:
        return out                                      # a service message (topic created, member joined)
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or frm.get("username") or "member"
    reply = (msg.get("reply_to_message") or {}).get("message_id")
    reply = None if reply is None or str(reply) == str(thread_id) else str(reply)   # every topic message "replies" to the topic
    when = _dt.datetime.utcfromtimestamp(int(msg.get("date") or 0)) if msg.get("date") else _utcnow()
    out["stored"] = _store(db, d, channel="telegram", external_id=str(msg.get("message_id") or ""),
                           author_id=str(frm.get("id") or "") or None, author_name=name, text=text, posted_at=when,
                           reply_to=reply)
    d.last_synced_at = _utcnow()
    db.commit()
    return out
