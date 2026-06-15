"""LLM for the Pattern Trainer teaching chat.

The user teaches a chart pattern by pointing at a marked region on a real chart;
we inject that region's OHLCV bars (re-loaded from parquet) plus the user's words,
and the model learns to codify the pattern. Unlike the Research chat, this does
NOT need the agent/corpus — the data it reasons over (the bars) is injected
directly — so it calls DeepSeek directly (same config as research_llm).

Soft-fail by design: returns a clear assistant-style message instead of raising.
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings

log = logging.getLogger("dashboard_tst.patterns")

SYSTEM_PROMPT = (
    "You are a chart-pattern learning assistant for a self-directed trader. The "
    "user TEACHES you to recognise ONE specific price pattern by showing you marked "
    "regions on real charts — you receive those regions as OHLCV bars plus the "
    "user's description. Your job in THIS chat is to LEARN the pattern: ask sharp "
    "clarifying questions, and converge on a precise, GENERALISABLE definition — the "
    "defining shape, the sequence of moves, proportions, and volume behaviour. "
    "CRITICAL: express every threshold in TICKER-RELATIVE terms (ATR multiples, "
    "percentages, volume z-scores, bar-count ranges) — NEVER absolute dollar prices "
    "or share counts — so the same definition works on a $4 stock and a $400 stock. "
    "Be concise and decision-useful. Do NOT write the final spec or detector code "
    "yet (that's a later step) — for now, learn, and confirm your understanding back "
    "to the user in plain language."
)

_MAX_TURNS = 40
_TIMEOUT_S = 90.0
_MAX_BARS_INJECT = 400  # cap so a big marked region can't blow the context window


def is_configured() -> bool:
    return bool(settings.deepseek_api_key)


def _format_marked(context: dict) -> str | None:
    """Render the marked chart region as a compact OHLCV block for the model."""
    bars = (context or {}).get("bars") or []
    if not bars:
        return None
    symbol = context.get("symbol", "?")
    tf = context.get("timeframe", "?")
    n = len(bars)
    note = ""
    if n > _MAX_BARS_INJECT:
        # keep head+tail so the model still sees the start/end of the region
        head = bars[: _MAX_BARS_INJECT // 2]
        tail = bars[-_MAX_BARS_INJECT // 2 :]
        bars = head + tail
        note = (f"\n(NOTE: region has {n} bars; showing the first and last "
                f"{_MAX_BARS_INJECT // 2} — the middle is elided.)")
    lines = [f"MARKED CHART REGION — {symbol} @ {tf} — {n} bars:",
             "t,o,h,l,c,v"]
    for b in bars:
        lines.append(f"{b.get('t')},{b.get('o')},{b.get('h')},{b.get('l')},"
                     f"{b.get('c')},{b.get('v')}")
    return "\n".join(lines) + note


def chat(history: list[dict], context: dict | None = None) -> dict:
    """Send the teaching conversation (+ optional marked-region bars) to the model.

    `history`: [{"role","content"}, ...]. `context`: {symbol, timeframe, bars}.
    Returns {"ok", "content", "tokens"}. Never raises.
    """
    if not is_configured():
        return {"ok": False,
                "content": ("⚠️ The pattern chat isn't configured — set "
                            "TST_DEEPSEEK_API_KEY in app/.env on the dashboard host."),
                "tokens": None}

    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    marked = _format_marked(context or {})
    if marked:
        msgs.append({"role": "system", "content": marked})
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-_MAX_TURNS:]]

    key = (settings.deepseek_api_key or "").strip()
    try:
        r = httpx.post(
            f"{settings.deepseek_base_url.rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": settings.deepseek_model, "messages": msgs, "stream": False},
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        log.warning("pattern deepseek call failed: %s", exc)
        return {"ok": False,
                "content": f"⚠️ Couldn't reach the model ({type(exc).__name__}). Try again.",
                "tokens": None}

    if r.status_code // 100 != 2:
        reason = ""
        try:
            reason = (r.json().get("error") or {}).get("message") or ""
        except Exception:
            reason = (r.text or "")[:120]
        suffix = f": {reason}" if reason else "."
        log.warning("pattern deepseek non-2xx: %s %s", r.status_code, reason)
        return {"ok": False, "content": f"⚠️ Model returned HTTP {r.status_code}{suffix}",
                "tokens": None}

    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"].strip()
        tokens = (data.get("usage") or {}).get("total_tokens")
    except Exception as exc:
        log.warning("pattern deepseek parse failed: %s", exc)
        return {"ok": False, "content": "⚠️ Couldn't parse the model response.", "tokens": None}

    return {"ok": True, "content": content, "tokens": tokens}
