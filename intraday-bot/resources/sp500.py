"""S&P 500 constituent list -- cached + refreshable.

Scrapes the canonical Wikipedia table at
  https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
which is updated daily and is the most-cited public source for the
index composition. Wikipedia is more reliable than yfinance for the
full list -- yfinance's `^GSPC` doesn't expose constituents.

Caches the result at `state/cache/sp500.json` with a 7-day TTL, so
repeated calls within a week don't re-hit Wikipedia.

Public API:
    get_sp500_symbols(force_refresh=False) -> list[str]
    refresh_sp500() -> list[str]

CLI:
    py resources/sp500.py                    # prints the list
    py resources/sp500.py --force-refresh    # re-fetches from Wikipedia
    py resources/sp500.py --count            # just the count
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

CACHE_PATH = SKILL_DIR / "state" / "cache" / "sp500.json"
TTL_SECONDS = 7 * 24 * 3600  # 7 days
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _fetch_from_wikipedia() -> list[str]:
    """Scrape the first table on the Wikipedia page. Returns ticker symbols."""
    req = urllib.request.Request(WIKI_URL, headers={
        "User-Agent": "Mozilla/5.0 (intraday-bot S&P 500 list fetcher)"
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # The constituents table has id="constituents". Find it.
    m = re.search(r'<table[^>]*id="constituents"[^>]*>(.*?)</table>', html, re.S)
    if not m:
        raise RuntimeError("could not find #constituents table on Wikipedia page")
    table_html = m.group(1)

    # Each row's first <td> is the symbol, wrapped in <a> linking to NYSE/NASDAQ.
    # Pattern: <td>...<a ... >SYMBOL</a>...</td>
    symbols: list[str] = []
    seen: set[str] = set()
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)
        if not cells:
            continue
        # First cell — strip tags, then strip whitespace.
        first = re.sub(r"<[^>]+>", "", cells[0]).strip()
        # Common cleanups: "BRK.B" stays as-is, but some rows have footnote refs.
        sym = re.sub(r"\s+", "", first)
        # Wikipedia uses "." for share classes (BRK.B); yfinance + IBKR want "-"
        # for IBKR side; here we keep the dotted form -- callers can map if needed.
        if sym and re.match(r"^[A-Z][A-Z0-9.\-]*$", sym):
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)

    if len(symbols) < 450:
        raise RuntimeError(
            f"Wikipedia returned only {len(symbols)} symbols; expected ~500. "
            "Page structure may have changed; re-check the scrape."
        )
    return symbols


def _read_cache() -> tuple[list[str], float] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        d = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return list(d["symbols"]), float(d["fetched_at"])
    except Exception:
        return None


def _write_cache(symbols: list[str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({
        "fetched_at": time.time(),
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": WIKI_URL,
        "n": len(symbols),
        "symbols": symbols,
    }, indent=2), encoding="utf-8")


def refresh_sp500() -> list[str]:
    """Force-refresh from Wikipedia. Updates the cache."""
    symbols = _fetch_from_wikipedia()
    _write_cache(symbols)
    return symbols


def get_sp500_symbols(force_refresh: bool = False) -> list[str]:
    """Return the cached S&P 500 ticker list, refreshing from Wikipedia
    if the cache is missing or older than `TTL_SECONDS`."""
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            symbols, fetched_at = cached
            if time.time() - fetched_at < TTL_SECONDS:
                return symbols
    return refresh_sp500()


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force-refresh", action="store_true",
                    help="ignore the 7-day cache and re-fetch from Wikipedia")
    ap.add_argument("--count", action="store_true",
                    help="print just the count")
    args = ap.parse_args()

    syms = get_sp500_symbols(force_refresh=args.force_refresh)
    if args.count:
        print(len(syms))
        return 0
    for s in syms:
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
