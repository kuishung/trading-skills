"""Resource — scrape stockmarketwatch.com/movers/premarket.

A Layer-1 data source. Strategies that want a "what's gapping in
pre-market" feed call into here. Generic — not GUNS-specific — so any
future family (ORB, DITP, etc.) can reuse it.

The page renders server-side HTML containing one or more `stockTable`
elements. Each row has:
  - `data-stock-symbol="<TICKER>"` (attribute on the <tr>)
  - tdChangePct with chgUp/chgDown class + "+N%" or "-N%" text
  - tdChange with price
  - tdSymbol with <a class="symbol" href="/stock/X">X</a>
  - tdCompany
  - tdVolume — values like "10516k" / "2.5M"

The fetch is best-effort: on parse failure or network error it
returns [] with a stderr warning rather than raising. Caller can fall
back to other sources.

Run as CLI for debugging:
    py resources/smw_premarket_movers.py
    py resources/smw_premarket_movers.py --min-change-pct 5 --max 20
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# --- intraday-bot bootstrap: make sibling layers importable ---
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

from _common import safe_log_stdout  # noqa: E402

URL = "https://stockmarketwatch.com/movers/premarket"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.5",
}
TIMEOUT_S = 15


# --- Volume parser: "10516k" / "2.5M" / "850" -> int shares ---

def _parse_volume(text: str) -> int | None:
    if not text:
        return None
    t = text.strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([kKmMbB]?)$", t)
    if not m:
        try:
            return int(float(t))
        except ValueError:
            return None
    n = float(m.group(1))
    suf = m.group(2).lower()
    if suf == "k":
        n *= 1_000
    elif suf == "m":
        n *= 1_000_000
    elif suf == "b":
        n *= 1_000_000_000
    return int(n)


def _parse_change_pct(text: str) -> float | None:
    """Parse '+40%' / '-3.4%' / '+129%' -> float. None on fail."""
    m = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_price(text: str) -> float | None:
    cleaned = (text or "").replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# --- Row extraction ---

_ROW_RE = re.compile(
    r'<tr\s+data-stock-symbol="([A-Z][A-Z0-9.\-]{0,7})"[^>]*>'
    r'(?P<row>.*?)</tr>',
    re.I | re.S,
)
# chgUp/chgDown divs render numbers with HTML comments interleaved
# (Next.js / React SSR artifact), e.g. `+<!-- -->40<!-- -->%`.
# Capture the whole div, then strip comments and tags before parsing.
_CHG_UP_BLOCK_RE   = re.compile(r'class="chgUp"[^>]*>(.*?)</div>',   re.I | re.S)
_CHG_DOWN_BLOCK_RE = re.compile(r'class="chgDown"[^>]*>(.*?)</div>', re.I | re.S)
_TD_CHANGE_RE = re.compile(r'<td class="tdChange"[^>]*>(.*?)</td>', re.I | re.S)
_TD_VOLUME_RE = re.compile(r'<td class="tdVolume"[^>]*>([^<]+)</td>', re.I)


def _strip_html(s: str) -> str:
    """Strip HTML comments and tags, collapse whitespace."""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", "", s)


def fetch_smw_premarket_movers(
    *,
    direction: str = "gainers",       # "gainers" | "losers" | "both"
    min_change_pct: float = 0.0,
    min_price: float = 0.0,
    max_price: float = float("inf"),
    max_rows: int = 50,
) -> list[dict]:
    """Return premarket movers from stockmarketwatch.com.

    Each dict: {symbol, change_pct, price, volume, direction, source}.
    `direction` filters by chgUp/chgDown tag:
       "gainers" -> only positive movers (GUNS use case)
       "losers"  -> only negative movers
       "both"    -> everything
    Empty list on network / parse failure.
    """
    try:
        req = urllib.request.Request(URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        safe_log_stdout(f"smw_premarket_movers: fetch failed: {exc}")
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for m in _ROW_RE.finditer(body):
        symbol = m.group(1).upper()
        if symbol in seen:
            continue
        row = m.group("row")

        up = _CHG_UP_BLOCK_RE.search(row)
        dn = _CHG_DOWN_BLOCK_RE.search(row)
        if up:
            row_dir = "gainer"
            chg_text = _strip_html(up.group(1))     # e.g. "+40%"
        elif dn:
            row_dir = "loser"
            chg_text = _strip_html(dn.group(1))     # e.g. "-3.4%"
        else:
            continue

        if direction == "gainers" and row_dir != "gainer":
            continue
        if direction == "losers" and row_dir != "loser":
            continue

        chg_pct = _parse_change_pct(chg_text)
        if chg_pct is None:
            continue
        if abs(chg_pct) < min_change_pct:
            continue

        price_m = _TD_CHANGE_RE.search(row)
        price = _parse_price(_strip_html(price_m.group(1))) if price_m else None
        if price is not None and (price < min_price or price > max_price):
            continue

        vol_m = _TD_VOLUME_RE.search(row)
        volume = _parse_volume(vol_m.group(1)) if vol_m else None

        seen.add(symbol)
        out.append({
            "symbol": symbol,
            "change_pct": chg_pct,
            "price": price,
            "volume": volume,
            "direction": row_dir,
            "source": "smw_premarket",
        })
        if len(out) >= max_rows:
            break
    safe_log_stdout(f"smw_premarket_movers: {len(out)} {direction} parsed")
    return out


# --- CLI ---

def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--direction", choices=["gainers", "losers", "both"],
                   default="gainers")
    p.add_argument("--min-change-pct", type=float, default=5.0,
                   help="Minimum abs(%%change) to include (default 5).")
    p.add_argument("--min-price", type=float, default=1.50)
    p.add_argument("--max-price", type=float, default=500.0)
    p.add_argument("--max", type=int, default=30, dest="max_rows")
    args = p.parse_args(argv)

    rows = fetch_smw_premarket_movers(
        direction=args.direction,
        min_change_pct=args.min_change_pct,
        min_price=args.min_price,
        max_price=args.max_price,
        max_rows=args.max_rows,
    )
    if not rows:
        safe_log_stdout("(no rows)")
        return 0
    for r in rows:
        price_s = f"${r['price']:.2f}" if r["price"] is not None else "n/a"
        vol_s = f"{r['volume']:,}" if r["volume"] is not None else "n/a"
        safe_log_stdout(
            f"  {r['symbol']:<8} chg={r['change_pct']:+6.2f}%  "
            f"px={price_s:>8}  vol={vol_s:>14}  {r['direction']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
