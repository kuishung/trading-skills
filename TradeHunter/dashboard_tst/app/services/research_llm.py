"""LLM for the Research planning chat.

Two modes, chosen at call time:

1. **Agent-grounded (preferred when configured).** If TST_RESEARCH_RUNNER_URL is
   set, we relay the conversation to the LAN-only `research-runner` shim on the
   Nous agent (Linux box). That shim runs the REAL Hermes agent
   (`hermes chat -q … -s research-planning`), which can read the EDGAR 10-Q
   corpus on its mount ON DEMAND, web-search, etc. — so the planning chat is
   grounded in the actual filings. See nous_hermes/research_runner/.

2. **DeepSeek-direct (fallback).** The agent is outbound-only, so historically
   the chat called DeepSeek's OpenAI-compatible API directly. We keep this as the
   fallback: used when no runner is configured, OR when the runner is unreachable
   / errors — so the page never breaks. (Same client works for DeepSeek cloud or
   a local deployment; only base_url/key differ.)

Soft-fail by design: every path returns a clear assistant-style message instead
of raising, so the page never 500s.
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


def runner_enabled() -> bool:
    """Agent-grounded mode is available (URL + shared token configured)."""
    return bool(settings.research_runner_url and settings.research_runner_token)


def is_configured() -> bool:
    """Chat works if EITHER the agent runner OR DeepSeek-direct is configured."""
    return runner_enabled() or bool(settings.deepseek_api_key)


def chat_mode() -> str:
    """Which path a chat() call will try first — for the UI status pill."""
    if runner_enabled():
        return "agent"
    if settings.deepseek_api_key:
        return "direct"
    return "off"


def chat(history: list[dict], topic: dict | None = None) -> dict:
    """Send the conversation to the model and return one assistant turn.

    `history` is a list of {"role": "user"|"assistant", "content": str} in order.
    `topic` (optional) carries {kind, subject, title} so the agent runner knows
    what the topic is about. Returns {"ok", "content", "tokens", "via"}.
    Never raises.

    Tries the agent runner first when configured; on any failure falls back to
    DeepSeek-direct so the page never breaks.
    """
    if runner_enabled():
        result = _chat_runner(history, topic or {})
        if result["ok"]:
            return result
        log.warning("research runner failed (%s) — falling back to DeepSeek-direct",
                    result.get("content"))
        if settings.deepseek_api_key:
            fb = _chat_deepseek(history)
            # Surface that we degraded, but only if the fallback itself failed.
            return fb
        return result  # no fallback available → return the runner error

    if settings.deepseek_api_key:
        return _chat_deepseek(history)

    return {
        "ok": False,
        "content": ("⚠️ The research chat isn't configured yet — set "
                    "TST_RESEARCH_RUNNER_URL (+ TST_RESEARCH_TOKEN) for the "
                    "agent-grounded chat, or TST_DEEPSEEK_API_KEY for the direct "
                    "chat, in app/.env on the dashboard host."),
        "tokens": None, "via": "off",
    }


def _chat_runner(history: list[dict], topic: dict) -> dict:
    """Relay to the LAN research-runner shim (the real Hermes agent)."""
    url = settings.research_runner_url.rstrip("/") + "/chat"
    payload = {
        "topic": {
            "kind": topic.get("kind"),
            "subject": topic.get("subject"),
            "title": topic.get("title"),
        },
        "history": [{"role": m["role"], "content": m["content"]}
                    for m in history[-_MAX_TURNS:]],
    }
    try:
        r = httpx.post(
            url,
            headers={"X-Research-Token": settings.research_runner_token,
                     "Content-Type": "application/json"},
            json=payload,
            timeout=settings.research_runner_timeout,
        )
    except Exception as exc:  # network / timeout — caller falls back
        return {"ok": False, "content": f"runner unreachable ({type(exc).__name__})",
                "tokens": None, "via": "agent"}

    try:
        data = r.json()
    except Exception:
        return {"ok": False, "content": f"runner HTTP {r.status_code} (bad body)",
                "tokens": None, "via": "agent"}

    if r.status_code // 100 != 2 or not data.get("ok"):
        return {"ok": False, "content": data.get("content") or f"runner HTTP {r.status_code}",
                "tokens": None, "via": "agent"}

    return {"ok": True, "content": (data.get("content") or "").strip(),
            "tokens": data.get("tokens"), "via": "agent"}


def _chat_deepseek(history: list[dict]) -> dict:
    """Call DeepSeek's OpenAI-compatible API directly (blind — no corpus)."""
    key = (settings.deepseek_api_key or "").strip()
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-_MAX_TURNS:]]

    try:
        r = httpx.post(
            f"{settings.deepseek_base_url.rstrip('/')}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"model": settings.deepseek_model, "messages": msgs, "stream": False},
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:  # network / timeout
        log.warning("deepseek call failed: %s", exc)
        return {"ok": False,
                "content": f"⚠️ Couldn't reach the model ({type(exc).__name__}). Try again.",
                "tokens": None, "via": "direct"}

    if r.status_code // 100 != 2:
        log.warning("deepseek non-2xx: %s %s", r.status_code, r.text[:200])
        # Surface DeepSeek's own reason (e.g. 401 Authentication Fails / 402
        # Insufficient Balance) instead of a bare code.
        reason = ""
        try:
            err = r.json().get("error") or {}
            reason = err.get("message") or ""
        except Exception:
            reason = (r.text or "")[:120]
        suffix = f": {reason}" if reason else "."
        return {"ok": False,
                "content": f"⚠️ Model returned HTTP {r.status_code}{suffix}",
                "tokens": None, "via": "direct"}

    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"].strip()
        tokens = (data.get("usage") or {}).get("total_tokens")
    except Exception as exc:
        log.warning("deepseek parse failed: %s", exc)
        return {"ok": False, "content": "⚠️ Couldn't parse the model response.",
                "tokens": None, "via": "direct"}

    return {"ok": True, "content": content, "tokens": tokens, "via": "direct"}
