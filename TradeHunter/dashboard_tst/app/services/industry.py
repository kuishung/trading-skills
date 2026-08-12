"""Sector → industry → tickers, from a Finviz sector overview screen.

Powers the Sector & Industry page's industry drill-down: pick a sector (SPDR) and
list its tickers grouped by industry. Live from Finviz via the shared
resources.finviz_screener (v=111 overview, industry parsed from the
data-boxover-industry attribute; 1h cached). Soft-fail → empty.
"""
from __future__ import annotations

import html as _html

from . import resources_bridge  # noqa: F401  (puts TradeHunter's resources.* on sys.path)

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


def sector_industries(sector: str) -> dict:
    """{sector, name, industries:[{name, tickers:[...]}], n} for a SPDR sector.
    Industries sorted by ticker count desc; tickers sorted alphabetically."""
    sym = (sector or "").strip().upper()
    fkey = _SPDR_TO_FINVIZ.get(sym)
    if not fkey:
        return {"sector": sym, "name": _SECTOR_NAMES.get(sym, sym), "industries": [], "n": 0}
    # cap_midover = mid-cap and above (notable/liquid names, not micro-caps).
    # o=-marketcap => biggest first, so a small page cap still yields the names that
    # matter. Capping pages + a short courtesy sleep keeps even a COLD load ~fast
    # (~5s vs ~20s); results are disk-cached (6h) so later loads are instant.
    url = f"https://finviz.com/screener.ashx?v=111&f={fkey},cap_midover&o=-marketcap"
    try:
        from resources import finviz_screener
        rows = finviz_screener.fetch_ticker_industries(url, max_pages=5, page_sleep_s=0.3)
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
            "industries": industries, "n": len(rows)}
