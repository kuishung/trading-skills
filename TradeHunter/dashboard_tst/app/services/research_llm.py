"""LLM for the Research planning chat.

The Nous agent is outbound-only (it polls us; we never call into it), so the
real-time planning chat talks to the model DIRECTLY from the dashboard. DeepSeek
exposes an OpenAI-compatible Chat Completions API, so this one client works for
DeepSeek cloud or a local deployment — only base_url/key differ (config). When
Claude is added later it slots in behind the same `chat()` signature.

Soft-fail by design: if no key is configured or the call errors, we return a
clear assistant-style message instead of raising, so the page never 500s.
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings

log = logging.getLogger("dashboard_tst.research")

# System prompt for the planning persona. The chat's job is to help the member
# SHAPE a research topic into a concrete, runnable plan — not to do the research
# now (that happens later on the agent, with the corpus).
SYSTEM_PROMPT = (
    "You are Nous Hermes, a markets research planning assistant for a self-directed "
    "investor. Your job in THIS chat is to co-design a single research topic with "
    "the user and converge on a concrete, runnable research PLAN — not to perform "
    "the research yet. Topics are either MACRO (economy/rates/sectors/themes) or "
    "COMPANY-specific (a ticker). Ask sharp clarifying questions, propose the "
    "research steps, the data sources to use (SEC filings, MATP trend levels, price "
    "history, web), and what the output should answer. Be concise and decision-"
    "useful. When the user is happy, summarise the agreed plan as numbered steps so "
    "it can be saved as the topic's PLAN.md."
)

# Keep the relayed history bounded so a long thread can't blow the context window.
_MAX_TURNS = 40
_TIMEOUT_S = 60.0


def is_configured() -> bool:
    return bool(settings.deepseek_api_key)


def chat(history: list[dict]) -> dict:
    """Send the conversation to the model and return one assistant turn.

    `history` is a list of {"role": "user"|"assistant", "content": str} in order.
    Returns {"ok": bool, "content": str, "tokens": int|None}. Never raises.
    """
    if not is_configured():
        return {
            "ok": False,
            "content": ("⚠️ The research chat isn't configured yet — set "
                        "TST_DEEPSEEK_API_KEY in app/.env on the dashboard host."),
            "tokens": None,
        }

    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-_MAX_TURNS:]]

    try:
        r = httpx.post(
            f"{settings.deepseek_base_url.rstrip('/')}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": settings.deepseek_model, "messages": msgs, "stream": False},
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:  # network / timeout
        log.warning("deepseek call failed: %s", exc)
        return {"ok": False,
                "content": f"⚠️ Couldn't reach the model ({type(exc).__name__}). Try again.",
                "tokens": None}

    if r.status_code // 100 != 2:
        log.warning("deepseek non-2xx: %s %s", r.status_code, r.text[:200])
        return {"ok": False,
                "content": f"⚠️ Model returned HTTP {r.status_code}.",
                "tokens": None}

    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"].strip()
        tokens = (data.get("usage") or {}).get("total_tokens")
    except Exception as exc:
        log.warning("deepseek parse failed: %s", exc)
        return {"ok": False, "content": "⚠️ Couldn't parse the model response.", "tokens": None}

    return {"ok": True, "content": content, "tokens": tokens}
