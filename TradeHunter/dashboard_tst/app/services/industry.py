"""Sector → industry → tickers, from a Finviz sector overview screen.

Powers the Sector & Industry page's industry drill-down: pick a sector (SPDR) and
list its tickers grouped by industry. Live from Finviz via the shared
resources.finviz_screener (v=111 overview, industry parsed from the
data-boxover-industry attribute; 1h cached). Soft-fail → empty.
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse as _urlparse

from . import resources_bridge  # noqa: F401  (puts TradeHunter's resources.* on sys.path)


def parse_finviz_filters(url: str) -> str:
    """Extract the Finviz screener `f=` criteria codes from a pasted screener URL,
    for the Sector page's Symbol-panel filter. Drops sector/industry codes
    (``sec_*`` / ``ind_*`` — we set those from the clicked sector/industry) and any
    token that isn't a plain finviz filter code. Returns a comma-joined, de-duped
    codes string (``''`` if none / not a finviz URL). We never fetch the pasted URL —
    only its codes are reused against our own finviz.com query, so there's no SSRF."""
    if not url or "finviz.com" not in url.lower():
        return ""
    try:
        params = _urlparse.parse_qs(_urlparse.urlparse(url.strip()).query)
    except Exception:  # noqa: BLE001
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (params.get("f") or [""])[0].split(","):
        c = raw.strip().lower()
        if not c or "_" not in c or c.startswith(("sec_", "ind_")):
            continue
        if re.fullmatch(r"[a-z0-9_]+", c) and c not in seen:
            seen.add(c)
            out.append(c)
    return ",".join(out)

# SPDR sector ETF -> Finviz sector filter code.
_SPDR_TO_FINVIZ = {
    "XLK": "sec_technology", "XLF": "sec_financial", "XLE": "sec_energy",
    "XLV": "sec_healthcare", "XLI": "sec_industrials", "XLY": "sec_consumercyclical",
    "XLP": "sec_consumerdefensive", "XLU": "sec_utilities", "XLB": "sec_basicmaterials",
    "XLRE": "sec_realestate", "XLC": "sec_communicationservices",
}
_SECTOR_NAMES = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLI": "Industrials", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate",
    "XLC": "Communication Services",
}


def sector_industries(sector: str, extra_filters: str = "") -> dict:
    """{sector, name, industries:[{name, tickers:[...]}], n, filtered} for a SPDR
    sector. Industries sorted by ticker count desc; tickers sorted alphabetically.

    ``extra_filters`` = extra Finviz `f=` criteria codes (comma-joined, from
    ``parse_finviz_filters``). When present they're ANDed into the sector query so
    Finviz returns only tickers matching the sector AND the criteria — the default
    ``cap_midover`` is dropped so the user's own criteria fully govern the screen.
    Filtered variants cache separately (fetch caches by URL)."""
    sym = (sector or "").strip().upper()
    fkey = _SPDR_TO_FINVIZ.get(sym)
    if not fkey:
        return {"sector": sym, "name": _SECTOR_NAMES.get(sym, sym), "industries": [], "n": 0, "filtered": False}
    extra = (extra_filters or "").strip()
    # cap_midover = mid-cap and above (notable/liquid names, not micro-caps).
    # o=-marketcap => biggest first, so a small page cap still yields the names that
    # matter. Capping pages + a short courtesy sleep keeps even a COLD load ~fast
    # (~5s vs ~20s); results are disk-cached (6h) so later loads are instant. When a
    # user filter is active we drop cap_midover and let their criteria rule, and walk
    # a few more pages since a restrictive screen returns fewer names.
    fpart = f"{fkey},{extra}" if extra else f"{fkey},cap_midover"
    url = f"https://finviz.com/screener.ashx?v=111&f={fpart}&o=-marketcap"
    try:
        from resources import finviz_screener
        rows = finviz_screener.fetch_ticker_industries(
            url, max_pages=10 if extra else 5, page_sleep_s=0.3
        )
    except Exception:  # noqa: BLE001
        rows = []
    groups: dict = {}
    for r in rows:
        ind = _html.unescape((r.get("industry") or "Other").strip()) or "Other"
        groups.setdefault(ind, []).append({
            "symbol": r["symbol"],
            "company": _html.unescape((r.get("company") or "").strip()),
            "price": r.get("price"),
        })
    industries = [
        {"name": k, "tickers": sorted(v, key=lambda t: t["symbol"])}
        for k, v in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return {"sector": sym, "name": _SECTOR_NAMES.get(sym, sym),
            "industries": industries, "n": len(rows), "filtered": bool(extra)}
