"""Live market-highlight feeds for the Today Overview page.

All data is fetched LIVE (httpx) — this is a "now" view, never parquet (CLAUDE.md).
Each fetch is wrapped in a short in-process TTL cache (market-wide data is the
same for every viewer) and soft-fails to None/[] so one dead source never 500s a
card. Sources are all free / no-key:
  - CNN Fear & Greed index (JSON)
  - Yahoo Finance RSS headlines (market = ^GSPC; company = per-ticker)
  - VIX comes from prices.fetch_quote("^VIX") in the route (Yahoo chart meta).
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_H = {"User-Agent": _UA}
_TTL = 600.0  # 10 min — highlights don't need sub-10-min freshness


def _ago(dt: datetime | None) -> str:
    """Compact relative age ('5m ago', '3h ago', '2d ago'). Timezone-safe."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    s = max(s, 0)
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def _parse_rss(text: str, limit: int) -> list[dict]:
    """RSS <item>s -> [{title, link, source, dt, ago}] (newest as given)."""
    out: list[dict] = []
    try:
        root = ET.fromstring(text)
    except Exception:  # noqa: BLE001 — malformed feed -> just no items
        return out
    for it in root.findall(".//item")[:limit]:
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        pub = it.findtext("pubDate")
        dt = None
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                dt = None
        out.append({
            "title": title,
            "link": (it.findtext("link") or "").strip(),
            "source": (it.findtext("source") or "").strip(),
            "dt": dt,
            "ago": _ago(dt),
        })
    return out


# ---------------------------------------------------------------- Fear & Greed
_fg_cache: dict = {}


def fear_greed() -> dict | None:
    """CNN Fear & Greed: {score 0-100, rating}. Cached 10 min, soft-fail None."""
    hit = _fg_cache.get("v")
    if hit and hit[0] > time.time():
        return hit[1]
    out = None
    try:
        r = httpx.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=_H, timeout=8.0,
        )
        fg = (r.json() or {}).get("fear_and_greed", {})
        score = fg.get("score")
        if score is not None:
            out = {"score": round(float(score)), "rating": (fg.get("rating") or "").strip()}
    except Exception:  # noqa: BLE001
        out = None
    _fg_cache["v"] = (time.time() + _TTL, out)
    return out


# ----------------------------------------------------------------- Market news
_news_cache: dict = {}
_YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={s}&region=US&lang=en-US"


def market_news(limit: int = 12) -> list[dict]:
    """Broad-market headlines (Yahoo RSS for ^GSPC). Cached 10 min, soft-fail []."""
    hit = _news_cache.get("v")
    if hit and hit[0] > time.time():
        return hit[1]
    items: list[dict] = []
    try:
        r = httpx.get(_YAHOO_RSS.format(s="^GSPC"), headers=_H, timeout=8.0)
        items = _parse_rss(r.text, limit)
    except Exception:  # noqa: BLE001
        items = []
    _news_cache["v"] = (time.time() + _TTL, items)
    return items


# ---------------------------------------------------------------- Company news
_co_cache: dict = {}


def _ticker_news(sym: str) -> list[dict]:
    hit = _co_cache.get(sym)
    if hit and hit[0] > time.time():
        return hit[1]
    items: list[dict] = []
    try:
        r = httpx.get(_YAHOO_RSS.format(s=sym), headers=_H, timeout=8.0)
        items = _parse_rss(r.text, 4)
        for it in items:
            it["symbol"] = sym
    except Exception:  # noqa: BLE001
        items = []
    _co_cache[sym] = (time.time() + _TTL, items)
    return items


def company_news(symbols, *, max_symbols: int = 6, limit: int = 12) -> list[dict]:
    """Merged per-ticker headlines for the watchlist (bounded thread pool, cached
    per symbol). Newest first. Soft-fail per symbol."""
    from concurrent.futures import ThreadPoolExecutor

    syms = list(dict.fromkeys(s for s in symbols if s))[:max_symbols]
    if not syms:
        return []
    merged: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=min(6, len(syms))) as ex:
            for res in ex.map(_ticker_news, syms):
                merged.extend(res)
    except Exception:  # noqa: BLE001
        pass
    _floor = datetime.min.replace(tzinfo=timezone.utc)
    merged.sort(key=lambda x: x.get("dt") or _floor, reverse=True)
    return merged[:limit]
