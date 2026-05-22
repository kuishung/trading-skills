"""S&P SmallCap 600 constituent list — cached + refreshable.

Mirrors `resources/sp500.py` for the small-cap S&P 600 index. Scrapes
  https://en.wikipedia.org/wiki/List_of_S%26P_600_companies
Same `id="constituents"` table structure as the 500 and 400 pages.

Caches at `state/cache/sp600.json` with a 7-day TTL.

Public API:
    get_sp600_symbols(force_refresh=False) -> list[str]
    refresh_sp600() -> list[str]

CLI:
    py resources/sp_smallcap600.py
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

CACHE_PATH = SKILL_DIR / "state" / "cache" / "sp600.json"
TTL_SECONDS = 7 * 24 * 3600
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"


def _fetch_from_wikipedia() -> list[str]:
    req = urllib.request.Request(WIKI_URL, headers={
        "User-Agent": "Mozilla/5.0 (intraday-bot S&P 600 list fetcher)"
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m = re.search(r'<table[^>]*id="constituents"[^>]*>(.*?)</table>', html, re.S)
    if not m:
        raise RuntimeError("could not find #constituents table on Wikipedia page")
    table_html = m.group(1)

    symbols: list[str] = []
    seen: set[str] = set()
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)
        if not cells:
            continue
        first = re.sub(r"<[^>]+>", "", cells[0]).strip()
        sym = re.sub(r"\s+", "", first)
        if sym and re.match(r"^[A-Z][A-Z0-9.\-]*$", sym):
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)

    if len(symbols) < 550:
        raise RuntimeError(
            f"Wikipedia returned only {len(symbols)} symbols; expected ~600. "
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


def refresh_sp600() -> list[str]:
    symbols = _fetch_from_wikipedia()
    _write_cache(symbols)
    return symbols


def get_sp600_symbols(force_refresh: bool = False) -> list[str]:
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            symbols, fetched_at = cached
            if time.time() - fetched_at < TTL_SECONDS:
                return symbols
    return refresh_sp600()


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force-refresh", action="store_true")
    ap.add_argument("--count", action="store_true")
    args = ap.parse_args()
    syms = get_sp600_symbols(force_refresh=args.force_refresh)
    if args.count:
        print(len(syms))
        return 0
    for s in syms:
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
