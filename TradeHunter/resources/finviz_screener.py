"""Finviz screener URL -> symbol list, with on-disk caching.

Use case: the dashboard's `/scanner/yf_scan` endpoint reads a Finviz
screener URL from `cfg["finviz_screener_url"]` and uses the returned
ticker list as the scan universe. The user changes the URL when they
want different filter criteria; no code change needed.

Example URL (intraday-tradeable mid-cap+):
    https://finviz.com/screener.ashx?v=111&f=cap_midover,geo_usa,
    sh_avgvol_10000to,sh_price_o20,ta_averagetruerange_o2,ta_beta_o1,
    ta_volatility_2tox2to&ft=3&o=-volume

Why scrape instead of paying for the Finviz Elite API: the user's
volume is low (a handful of scans per day), and the public screener
page returns enough data via HTML attributes for our needs. We rate-
limit ourselves to one page per second and cache for 1 hour by
default. If Finviz tightens its ToS or markup, swap to Elite or
substitute another source (the public function returns a list of
symbols, callers don't care where they came from).

Parsing strategy: each ticker row has a `data-boxover-ticker="SYM"`
attribute that drives the hover tooltip. The attribute appears twice
per row (left and right cells share it), so we dedupe. This is the
most stable hook -- Finviz has changed the `class` names on link
elements multiple times in recent years, but the boxover attribute
has been consistent.

Public API:
    fetch_screener_symbols(url, max_pages=20, cache_ttl_s=3600,
                           force_refresh=False) -> list[str]
        Walks pages until no new symbols appear or max_pages reached.
        Returns deduplicated, uppercased symbols in the dotted-form
        share-class convention (BRK.B not BRK-B) for consistency with
        the rest of the codebase.

CLI:
    py resources/finviz_screener.py <URL>
    py resources/finviz_screener.py <URL> --force-refresh
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# Resolve SKILL_DIR by walking up to find SKILL.md (mirrors the pattern in
# strategy/DITP/scanner.py + other modules in this folder).
_p = Path(__file__).resolve().parent
while _p != _p.parent and not (_p / "SKILL.md").exists():
    _p = _p.parent
SKILL_DIR = _p
CACHE_DIR = SKILL_DIR / "state" / "cache"
del _p

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)
_PAGE_SLEEP_S = 1.0   # courtesy rate limit between page fetches
_ROWS_PER_PAGE = 20   # Finviz default for v=111 / v=131 views


def _cache_path(url: str) -> Path:
    """Hash the URL into a stable cache filename. URL changes -> new
    cache file; filter changes naturally invalidate.
    """
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"finviz_{h}.json"


def _read_cache(path: Path, ttl_s: int) -> list[str] | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if (time.time() - obj.get("fetched_at", 0)) > ttl_s:
        return None
    syms = obj.get("symbols")
    if not isinstance(syms, list):
        return None
    return [str(s).upper() for s in syms]


def _write_cache(path: Path, url: str, symbols: list[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "url":        url,
            "fetched_at": time.time(),
            "count":      len(symbols),
            "symbols":    symbols,
        }, indent=2),
        encoding="utf-8",
    )


def _normalize_url(url: str) -> str:
    """Strip any existing `&r=N` pagination param so we can supply our
    own. Returns the URL ready to have `&r=<offset>` appended.
    """
    return re.sub(r"[&?]r=\d+", "", url)


def _page_url(base: str, offset: int) -> str:
    if offset <= 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}r={offset}"


def _fetch_page(url: str) -> str:
    # Finviz 301-redirects screener.ashx -> screener and drops requests that
    # lack browser-like Accept headers (closes the connection -> urllib
    # RemoteDisconnected). Send a full header set; urllib follows the 301.
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


# Ticker regex: 1-5 uppercase letters, optionally followed by .X for
# share classes (BRK.B, BF.B). Excludes dashes because we normalize
# back to dotted form on return.
_TICKER_RE = re.compile(r'data-boxover-ticker="([A-Z][A-Z0-9.]{0,9})"')

# The TS comment block at the bottom of every screener page is a
# server-side-rendered "ticker stream" -- pipe-separated rows of
# SYM|PRICE|VOLUME, one per row on that page. Cleaner to parse than
# walking <td> cells because it's not dependent on which "view" the
# URL requested (v=111, v=131, etc. all emit the same TS block).
# Format: <!-- TS\nSYM|PRICE|VOLUME\nSYM|PRICE|VOLUME\n...\n-->
_TS_BLOCK_RE = re.compile(r'<!--\s*TS\s*\n(.*?)-->', re.DOTALL)


def _extract_symbols(html: str) -> list[str]:
    """Return deduplicated symbols (in document order) from one page's
    HTML. `data-boxover-ticker` appears twice per row (left + right
    cells), so the dedup keeps the first-seen order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for sym in _TICKER_RE.findall(html):
        sym = sym.upper()
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


# Ticker + its industry from the v=111 overview: both live in one <td> tag as
# `data-boxover-ticker="SYM" data-boxover-company="..." data-boxover-industry="X"`.
# Non-greedy [^>]*? stays inside the same tag. Appears twice per row (dedup on sym).
_TICKER_INDUSTRY_RE = re.compile(
    r'data-boxover-ticker="([A-Z][A-Z0-9.]{0,9})"\s+data-boxover-company="([^"]*)"'
    r'[^>]*?data-boxover-industry="([^"]*)"'
)
_MEM_CACHE_IND: dict[str, tuple[float, list[dict]]] = {}
# Industry rows are near-fixed classification; persist to disk so a cold start
# (restart) doesn't re-scrape the whole sector. Longer TTL than the mem layer.
_IND_DISK_TTL_S = 6 * 3600


def _ind_cache_path(url: str) -> Path:
    return CACHE_DIR / f"finviz_ind_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}.json"


def _read_ind_cache(path: Path, ttl_s: int) -> list[dict] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if (time.time() - float(d["fetched_at"])) > ttl_s:
            return None
        return list(d.get("rows") or [])
    except Exception:  # noqa: BLE001
        return None


def _write_ind_cache(path: Path, url: str, rows: list[dict]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "url": url, "rows": rows}),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def fetch_ticker_industries(
    url: str, *, max_pages: int = 40, cache_ttl_s: int = 3600, force_refresh: bool = False,
) -> list[dict]:
    """Walk a Finviz screener URL (must use the v=111 overview view) and return
    [{symbol, industry}] across all pages, deduped by symbol. Reuses the same
    fetch/pagination as fetch_screener_symbols; in-process cached by URL. Empty
    list on failure. Lets callers group a sector's tickers by industry."""
    if not url or not url.strip():
        return []
    base = _normalize_url(url.strip())
    disk = _ind_cache_path(base)
    if not force_refresh:
        hit = _MEM_CACHE_IND.get(base)
        if hit is not None and (time.time() - hit[0]) <= cache_ttl_s:
            return [dict(r) for r in hit[1]]
        drows = _read_ind_cache(disk, _IND_DISK_TTL_S)
        if drows is not None:
            _MEM_CACHE_IND[base] = (time.time(), drows)
            return [dict(r) for r in drows]
    out: list[dict] = []
    seen: set[str] = set()
    for page_idx in range(max_pages):
        offset = 1 + page_idx * _ROWS_PER_PAGE
        try:
            html = _fetch_page(_page_url(base, offset))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            sys.stderr.write(f"[finviz_screener] industry page {offset} failed: {exc}\n")
            break
        price_map = {r["symbol"]: r.get("price") for r in _extract_rows(html)}
        new_on_page = 0
        for sym, company, ind in _TICKER_INDUSTRY_RE.findall(html):
            sym = sym.upper()
            if sym in seen:
                continue
            seen.add(sym)
            out.append({"symbol": sym, "company": (company or "").strip(),
                        "industry": (ind or "").strip() or "Other",
                        "price": price_map.get(sym)})
            new_on_page += 1
        if new_on_page == 0:
            break
        time.sleep(_PAGE_SLEEP_S)
    if out:
        _MEM_CACHE_IND[base] = (time.time(), out)
        _write_ind_cache(disk, base, out)
    return out


def _extract_rows(html: str) -> list[dict]:
    """Return per-row dicts {symbol, price, volume} from the TS comment
    block at the bottom of the page. Float-parses price + volume;
    rejects rows that don't parse.
    """
    m = _TS_BLOCK_RE.search(html)
    if not m:
        return []
    rows: list[dict] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        sym = parts[0].strip().upper()
        if not sym:
            continue
        try:
            price = float(parts[1])
        except (ValueError, TypeError):
            price = None
        try:
            volume = int(parts[2])
        except (ValueError, TypeError):
            volume = None
        rows.append({"symbol": sym, "price": price, "volume": volume})
    return rows


# In-process LRU on top of the disk cache (added 2026-05-29 efficiency
# Pass 1 #5). Dashboard's `_universe_for_setup` calls this on every
# scan, every Scanner-2 ticker reload, every health-pill render --
# disk cache still does SHA1 + stat + read + JSON parse (~5-10ms per
# call). Memory cache keyed by normalized URL collapses to a dict
# lookup. TTL semantics identical; force_refresh bypasses both layers.
_MEM_CACHE: dict[str, tuple[float, list[str]]] = {}


def fetch_screener_symbols(
    url: str,
    *,
    max_pages: int = 20,
    cache_ttl_s: int = 3600,
    force_refresh: bool = False,
) -> list[str]:
    """Walk the Finviz screener URL across pages, return all symbols.

    Pagination: appends `&r=<offset>` with offsets 1, 21, 41, ... until
    a page yields zero NEW symbols (sentinel for end-of-results) or
    `max_pages` is hit. Sleeps `_PAGE_SLEEP_S` between page fetches.

    Caching layers (in order):
      1. In-process `_MEM_CACHE` keyed by normalized URL.
      2. Disk cache `state/cache/finviz_<sha1>.json` keyed by the same.
    Both honour `cache_ttl_s`. Pass `force_refresh=True` to skip both.

    Returns dedup'd list, uppercase, dotted-form share classes.
    Empty list if the fetch fails -- caller should treat empty as
    "fall back to default universe", NOT "screener has 0 matches".
    """
    if not url or not url.strip():
        return []
    base = _normalize_url(url.strip())

    if not force_refresh:
        # Layer 1: in-process cache.
        hit = _MEM_CACHE.get(base)
        if hit is not None and (time.time() - hit[0]) <= cache_ttl_s:
            return list(hit[1])

    cache = _cache_path(base)

    if not force_refresh:
        cached = _read_cache(cache, cache_ttl_s)
        if cached is not None:
            # Promote disk hit into memory so subsequent calls skip disk.
            _MEM_CACHE[base] = (time.time(), cached)
            return cached

    all_symbols: list[str] = []
    seen: set[str] = set()
    for page_idx in range(max_pages):
        offset = 1 + page_idx * _ROWS_PER_PAGE
        page_url = _page_url(base, offset)
        try:
            html = _fetch_page(page_url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            sys.stderr.write(f"[finviz_screener] page {offset} fetch failed: {exc}\n")
            break
        page_syms = _extract_symbols(html)
        new_on_page = 0
        for s in page_syms:
            if s in seen:
                continue
            seen.add(s)
            all_symbols.append(s)
            new_on_page += 1
        if new_on_page == 0:
            # End of results (sentinel for both "no more pages" and
            # "Finviz looped back to page 1"). Without this we'd loop
            # forever on a stale/empty result set.
            break
        if page_idx + 1 < max_pages:
            time.sleep(_PAGE_SLEEP_S)

    if all_symbols:
        _write_cache(cache, base, all_symbols)
        # Also promote into the in-process cache (Pass 1 #5).
        _MEM_CACHE[base] = (time.time(), all_symbols)
    return all_symbols


def fetch_screener_rows(
    url: str,
    *,
    max_pages: int = 20,
    cache_ttl_s: int = 3600,
    force_refresh: bool = False,
) -> list[dict]:
    """Same pagination + caching contract as fetch_screener_symbols,
    but returns richer per-row dicts: {symbol, price, volume}.

    Cache file separate from the symbols-only cache (suffixed `_rows`)
    so callers can pick whichever shape they need without coupling.
    """
    if not url or not url.strip():
        return []
    base = _normalize_url(url.strip())
    rows_cache = _cache_path(base).with_name(_cache_path(base).stem + "_rows.json")

    if not force_refresh:
        if rows_cache.exists():
            try:
                obj = json.loads(rows_cache.read_text(encoding="utf-8"))
                if (time.time() - obj.get("fetched_at", 0)) <= cache_ttl_s:
                    rows = obj.get("rows")
                    if isinstance(rows, list):
                        return rows
            except Exception:
                pass

    all_rows: list[dict] = []
    seen: set[str] = set()
    for page_idx in range(max_pages):
        offset = 1 + page_idx * _ROWS_PER_PAGE
        page_url = _page_url(base, offset)
        try:
            html = _fetch_page(page_url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            sys.stderr.write(f"[finviz_screener] page {offset} fetch failed: {exc}\n")
            break
        page_rows = _extract_rows(html)
        new_on_page = 0
        for r in page_rows:
            s = r["symbol"]
            if s in seen:
                continue
            seen.add(s)
            all_rows.append(r)
            new_on_page += 1
        if new_on_page == 0:
            break
        if page_idx + 1 < max_pages:
            time.sleep(_PAGE_SLEEP_S)

    if all_rows:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rows_cache.write_text(
            json.dumps({
                "url":        base,
                "fetched_at": time.time(),
                "count":      len(all_rows),
                "rows":       all_rows,
            }, indent=2),
            encoding="utf-8",
        )
    return all_rows


# ---------- CLI for ad-hoc inspection ----------

def _main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("url", help="Full Finviz screener URL")
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--cache-ttl", type=int, default=3600)
    p.add_argument("--force-refresh", action="store_true")
    args = p.parse_args(argv)
    syms = fetch_screener_symbols(
        args.url,
        max_pages=args.max_pages,
        cache_ttl_s=args.cache_ttl,
        force_refresh=args.force_refresh,
    )
    sys.stdout.write(f"# {len(syms)} symbols\n")
    for s in syms:
        sys.stdout.write(s + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
