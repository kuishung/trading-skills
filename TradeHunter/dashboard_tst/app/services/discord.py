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


def configured() -> bool:
    """True when a webhook URL is set (so callers can skip building a payload)."""
    return bool(settings.discord_webhook_url)


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
