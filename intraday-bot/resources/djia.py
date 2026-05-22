"""Dow Jones Industrial Average (DJIA) constituent list — cached + refreshable.

Scrapes https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average. The
"Components" table holds 30 stocks. Same flexible scan-wikitables strategy
as resources/nasdaq100.py.

Caches at `state/cache/djia.json` with a 7-day TTL.

CLI:
    py resources/djia.py
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

CACHE_PATH = SKILL_DIR / "state" / "cache" / "djia.json"
TTL_SECONDS = 7 * 24 * 3600
WIKI_URL = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
EXPECTED_MIN, EXPECTED_MAX = 28, 32


def _extract_tickers_from_table(table_html: str, col: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, re.S):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row.group(1), re.S)
        if len(cells) <= col:
            continue
        raw = re.sub(r"<[^>]+>", "", cells[col]).strip()
        sym = re.sub(r"\s+", "", raw)
        if sym and re.match(r"^[A-Z][A-Z0-9.\-]{0,5}$", sym):
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def _fetch_from_wikipedia() -> list[str]:
    req = urllib.request.Request(WIKI_URL, headers={
        "User-Agent": "Mozilla/5.0 (intraday-bot DJIA list fetcher)"
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    tables = re.findall(
        r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>(.*?)</table>',
        html, re.S,
    )
    if not tables:
        raise RuntimeError("no wikitable tables found on the DJIA page")
    best: list[str] = []
    for t in tables:
        for col in range(4):
            syms = _extract_tickers_from_table(t, col=col)
            if EXPECTED_MIN <= len(syms) <= EXPECTED_MAX:
                return syms
            if len(syms) > len(best):
                best = syms
    raise RuntimeError(
        f"DJIA scrape: no table column yielded {EXPECTED_MIN}-{EXPECTED_MAX} tickers; "
        f"best attempt was {len(best)}. Page layout may have changed."
    )


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


def refresh_djia() -> list[str]:
    symbols = _fetch_from_wikipedia()
    _write_cache(symbols)
    return symbols


def get_djia_symbols(force_refresh: bool = False) -> list[str]:
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            symbols, fetched_at = cached
            if time.time() - fetched_at < TTL_SECONDS:
                return symbols
    return refresh_djia()


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force-refresh", action="store_true")
    ap.add_argument("--count", action="store_true")
    args = ap.parse_args()
    syms = get_djia_symbols(force_refresh=args.force_refresh)
    if args.count:
        print(len(syms))
        return 0
    for s in syms:
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
