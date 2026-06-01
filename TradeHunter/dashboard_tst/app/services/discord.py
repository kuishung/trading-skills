"""Discord outbound notifications via an incoming webhook.

Outbound-only: TradeHunter POSTs a JSON embed to a Discord channel webhook
(``settings.discord_webhook_url``). No bot, no inbound ports — it fits the same
outbound-only posture as the rest of the platform. **Soft-fail by design**: a
missing webhook or any network error returns False and is logged, but never
raises, so it can't break the request that triggered it. Callers should schedule
``post_embed`` via FastAPI ``BackgroundTasks`` so it never adds request latency.
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings

log = logging.getLogger("tst.discord")

# Discord embed accent colours (decimal ints)
COLOR_EMERALD = 0x10B981
COLOR_AMBER = 0xF59E0B
COLOR_ROSE = 0xEF4444


_API = "https://discord.com/api/v10"


def configured() -> bool:
    """True when a webhook URL is set (so callers can skip building a payload)."""
    return bool(settings.discord_webhook_url)


def bot_configured() -> bool:
    """True when a bot token + parent channel are set, so we can create a thread
    per study and READ its messages back (webhooks can't read)."""
    return bool(settings.discord_bot_token and settings.discord_channel_id)


def _bot_headers() -> dict:
    return {
        "Authorization": f"Bot {settings.discord_bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "TradeHunter (https://tradehunter.net, 1.0)",
    }


def create_study_thread(name: str, embed: dict | None = None) -> str | None:
    """Create a public thread in the configured channel for a study and post the
    opening embed into it. Returns the thread id (a string), or None on any
    failure (soft-fail — never raises). Needs the bot to have Create Public
    Threads + Send Messages in Threads."""
    if not bot_configured():
        return None
    try:
        # type 11 = public thread; 10080 min = 7-day auto-archive
        r = httpx.post(
            f"{_API}/channels/{settings.discord_channel_id}/threads",
            headers=_bot_headers(),
            json={"name": (name or "study")[:100], "type": 11, "auto_archive_duration": 10080},
            timeout=8.0,
        )
        if r.status_code // 100 != 2:
            log.warning("discord create-thread non-2xx: %s %s", r.status_code, (r.text or "")[:200])
            return None
        tid = str((r.json() or {}).get("id") or "")
        if not tid:
            return None
        if embed is not None:
            try:
                httpx.post(
                    f"{_API}/channels/{tid}/messages",
                    headers=_bot_headers(), json={"embeds": [embed]}, timeout=8.0,
                )
            except Exception as e:  # noqa: BLE001 — opening message is best-effort
                log.warning("discord thread opener error: %s", e)
        return tid
    except Exception as e:  # noqa: BLE001 — soft-fail
        log.warning("discord create-thread error: %s", e)
        return None


def post_thread_message(thread_id: str, content: str) -> bool:
    """Post a plain message into a thread (the bot speaks on a member's behalf,
    so web chat mirrors into Discord). Returns True on a 2xx. Needs Send Messages
    in Threads. Soft-fail."""
    if not (bot_configured() and thread_id and content):
        return False
    try:
        r = httpx.post(
            f"{_API}/channels/{thread_id}/messages",
            headers=_bot_headers(), json={"content": content[:2000]}, timeout=8.0,
        )
        if r.status_code // 100 == 2:
            return True
        log.warning("discord post-message non-2xx: %s %s", r.status_code, (r.text or "")[:200])
        return False
    except Exception as e:  # noqa: BLE001 — soft-fail
        log.warning("discord post-message error: %s", e)
        return False


def fetch_thread_messages(thread_id: str, limit: int = 50) -> list[dict] | None:
    """Read a thread's recent messages (chronological). Returns a list of
    ``{author, content, ts, bot}`` dicts, or None on failure. Needs the bot to
    have Read Message History (+ the Message Content intent for full content)."""
    if not (bot_configured() and thread_id):
        return None
    try:
        r = httpx.get(
            f"{_API}/channels/{thread_id}/messages",
            headers=_bot_headers(), params={"limit": max(1, min(100, limit))}, timeout=8.0,
        )
        if r.status_code // 100 != 2:
            log.warning("discord read-messages non-2xx: %s %s", r.status_code, (r.text or "")[:200])
            return None
        out = []
        for m in (r.json() or []):
            a = m.get("author") or {}
            # flatten any embeds (e.g. the bot's opening study card) to text, so
            # the panel shows something meaningful instead of "(embed)".
            parts = []
            for e in (m.get("embeds") or []):
                if e.get("title"):
                    parts.append(str(e["title"]))
                if e.get("description"):
                    parts.append(str(e["description"]))
                for f in (e.get("fields") or []):
                    parts.append(f"{f.get('name', '')}: {f.get('value', '')}".strip(": "))
            out.append({
                "author": a.get("global_name") or a.get("username") or "member",
                "content": m.get("content") or "",
                "embed_text": "\n".join(p for p in parts if p),
                "has_attach": bool(m.get("attachments")),
                "ts": (m.get("timestamp") or "")[:19].replace("T", " "),
                "bot": bool(a.get("bot")),
            })
        out.reverse()  # API returns newest-first; show oldest-first
        return out
    except Exception as e:  # noqa: BLE001 — soft-fail
        log.warning("discord read-messages error: %s", e)
        return None


def thread_link(thread_id: str | None) -> str | None:
    """Deep-link to a thread in the Discord client, if the guild id is set."""
    if thread_id and settings.discord_guild_id:
        return f"https://discord.com/channels/{settings.discord_guild_id}/{thread_id}"
    return None


def build_ticker_embed(
    *,
    symbol: str,
    matp: float | None = None,
    mbp: float | None = None,
    signal: str | None = None,
    price: float | None = None,
    next_earnings: dict | None = None,
    last_earnings: str | None = None,
    note: str | None = None,
    title_prefix: str = "MATP",
    public_url: str = "https://tradehunter.net",
    url: str | None = None,
) -> dict:
    """Build rich embed kwargs for a single ticker (used by the auto MATP-refresh
    post AND the manual 'Share to Discord' button, so both look identical).
    Colour follows the signal (HOT=rose, WARM=amber, else emerald). Pass the
    result straight to ``post_embed(**...)``."""
    sig = (signal or "").upper()
    color = COLOR_ROSE if sig == "HOT" else COLOR_AMBER if sig == "WARM" else COLOR_EMERALD

    fields: list[dict] = []
    if price is not None:
        fields.append({"name": "Price", "value": f"{price:.2f}", "inline": True})
    if matp is not None:
        fields.append({"name": "MATP", "value": f"{matp:.2f}", "inline": True})
    if mbp is not None:
        # flag whether the live price is within the max-buy price
        tag = ("  ✅ at/below" if price <= mbp else "  ⛔ above") if price is not None else ""
        fields.append({"name": "MBP (max buy)", "value": f"{mbp:.2f}{tag}", "inline": True})
    if sig in ("HOT", "WARM", "WATCHING"):
        fields.append({"name": "Signal", "value": sig, "inline": True})
    if next_earnings and next_earnings.get("date"):
        days = next_earnings.get("days")
        when = ""
        if days is not None:
            when = (" (today)" if days <= 0 else " (tomorrow)" if days == 1
                    else f" ({days} days)" if days < 14 else f" (~{round(days / 7)} wks)")
        fields.append({"name": "Next earnings", "value": f"{next_earnings['date']}{when}", "inline": True})
    if last_earnings:
        fields.append({"name": "Last earnings", "value": str(last_earnings), "inline": True})

    return {
        "title": f"{title_prefix} · {symbol}",
        "description": note or None,
        "url": url or f"{public_url}/matp?symbol={symbol}",
        "color": color,
        "fields": fields or None,
    }


def post_embed(
    title: str,
    description: str | None = None,
    url: str | None = None,
    color: int = COLOR_EMERALD,
    fields: list[dict] | None = None,
) -> bool:
    """POST one embed to the configured Discord webhook. Returns True on a 2xx,
    False otherwise (logged). Never raises. ``fields`` = list of
    ``{"name", "value", "inline"}`` dicts (Discord caps: 25 fields, name 256,
    value 1024, title 256, description 4096)."""
    hook = settings.discord_webhook_url
    if not hook:
        return False
    embed: dict = {"title": (title or "")[:256], "color": color}
    if description:
        embed["description"] = description[:4096]
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = [
            {
                "name": str(f.get("name", ""))[:256] or "​",
                "value": str(f.get("value", ""))[:1024] or "​",
                "inline": bool(f.get("inline", True)),
            }
            for f in fields[:25]
        ]
    try:
        r = httpx.post(hook, json={"embeds": [embed]}, timeout=8.0)
        if r.status_code // 100 == 2:
            return True
        log.warning("discord webhook non-2xx: %s %s", r.status_code, (r.text or "")[:200])
        return False
    except Exception as e:  # noqa: BLE001 — soft-fail, must never break the caller
        log.warning("discord webhook error: %s", e)
        return False
