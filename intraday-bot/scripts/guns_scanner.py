"""Build the GUNS watchlist for today.

Produces `state/watchlist_guns_<date>.txt` — the GUNS-family input
file consumed by guns_setup1.py and guns_setup5.py. The "guns_"
prefix is intentional: each strategy family (GUNS today; ORB, DITP
etc. in future) has its own scanner with its own filter criteria,
writing to its own watchlist. Two sources, both optional:

  1. IBKR scanner — runs a GUNS-tuned ScannerSubscription against
     IB Gateway / TWS. Filter recipe (user-defined; matches PDF
     slides 11-12 minus the avg-vol upper bound):
       - Universe: STK.US.MAJOR (NYSE + AMEX + ARCA + NQ.NM + NQ.SC + BATS)
       - Stock type: CORP only (no ADR / ETF / REIT / CEF)
       - Price 1.50 to 500
       - Change% >= 5
       - Avg volume >= 20K (no upper bound)
       - Today's volume > 30K

  2. thestockmarketwatch.com — scrapes the homepage's "Top Gainers"
     table (https://thestockmarketwatch.com/markets/today.aspx).
     Best-effort HTML parsing; the site can restructure without notice.
     Symbols below $1.50 or with change% < 5 are dropped here.

Output file format (state/watchlist_guns_<date>.txt):
    # GUNS watchlist for YYYY-MM-DD
    # built at HH:MM:SS ET
    # sources: ibkr=N, smw=M, dedupe-merged=K
    # IBKR scanner: STK.US.MAJOR, TOP_PERC_GAIN, price 1.50-500,
    #               change% >= 5, avg vol 20K-70M, today vol > 30K
    # thestockmarketwatch.com: top gainers
    SYM1                 # IBKR rank=1 price=$3.45 change=+8.2%
    SYM2                 # SMW change=+12.5% price=$5.20
    SYM3                 # IBKR rank=2; SMW change=+9.1%
    ...

Catalyst classification + float screening are NOT performed here.
The PDF lists those as mandatory pre-trade screens — they're upstream
of this scanner (use the sibling intraday-premarket-brief skill, or
curate manually). Symbols flagged for M&A by the brief should be
removed from the resulting file before the open.

Run:
    py scripts/guns_scanner.py                 # both sources, default
    py scripts/guns_scanner.py --source ibkr   # IBKR only
    py scripts/guns_scanner.py --source smw    # stockmarketwatch only
    py scripts/guns_scanner.py --top 10        # cap merged list
    py scripts/guns_scanner.py --no-write      # preview without writing
"""
from __future__ import annotations

# Python 3.14 / eventkit shim — install a default event loop before any
# ib_insync import touches asyncio.get_event_loop().
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _common import (  # noqa: E402
    STATE_DIR, et_now, et_today_iso, load_config, safe_log_stdout,
)
from strategies._guns_common import guns_watchlist_path  # noqa: E402


# ---------- GUNS criteria (user-defined, aligned with PDF slides 11-12) ----------

MIN_PRICE = 1.50
MAX_PRICE = 500.0
MIN_CHANGE_PCT = 5.0
MIN_AVG_VOLUME = 20_000
MIN_TODAY_VOLUME = 30_000
STOCK_TYPE_FILTER = "CORP"   # exclude ADR / ETF / REIT / CEF


# ---------- Candidate record ----------

@dataclass
class Candidate:
    symbol: str
    sources: list[str] = field(default_factory=list)   # ["ibkr", "smw"]
    ibkr_rank: int | None = None
    price: float | None = None
    change_pct: float | None = None
    today_volume: int | None = None
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "Candidate") -> None:
        for src in other.sources:
            if src not in self.sources:
                self.sources.append(src)
        if other.ibkr_rank is not None and (
            self.ibkr_rank is None or other.ibkr_rank < self.ibkr_rank
        ):
            self.ibkr_rank = other.ibkr_rank
        if other.price is not None and self.price is None:
            self.price = other.price
        if other.change_pct is not None and (
            self.change_pct is None or abs(other.change_pct) > abs(self.change_pct)
        ):
            self.change_pct = other.change_pct
        if other.today_volume is not None and self.today_volume is None:
            self.today_volume = other.today_volume

    def comment(self) -> str:
        pieces: list[str] = []
        if "ibkr" in self.sources:
            r = f"rank={self.ibkr_rank}" if self.ibkr_rank else "ranked"
            pieces.append(f"IBKR {r}")
        if "smw" in self.sources:
            pieces.append("SMW")
        if self.change_pct is not None:
            pieces.append(f"chg={self.change_pct:+.1f}%")
        if self.price is not None:
            pieces.append(f"px=${self.price:.2f}")
        if self.today_volume is not None:
            pieces.append(f"vol={self.today_volume:,}")
        return "; ".join(pieces)


# ---------- IBKR source ----------

def fetch_ibkr(cfg: dict, rows: int = 50, client_id: int = 82,
               settle_seconds: float = 6.0) -> list[Candidate]:
    """Subscribe to a GUNS-tuned scanner, wait for population, snapshot,
    unsubscribe. clientId defaults to 82 so it doesn't collide with the
    bot (71), the dashboard probe (99), or the observer (80)."""
    from ib_insync import IB, ScannerSubscription, TagValue

    host = cfg.get("ibkr_host", "127.0.0.1")
    port = int(cfg.get("ibkr_port", 7497))
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=8, readonly=True)
    except Exception as exc:
        safe_log_stdout(f"IBKR connect failed ({host}:{port}): {exc}")
        return []

    sub = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode="TOP_PERC_GAIN",
        numberOfRows=rows,
        abovePrice=MIN_PRICE,
        belowPrice=MAX_PRICE,
        aboveVolume=MIN_TODAY_VOLUME,
        stockTypeFilter=STOCK_TYPE_FILTER,
    )
    filters = [
        TagValue("changePercAbove", str(MIN_CHANGE_PCT)),
        TagValue("avgVolumeAbove", str(MIN_AVG_VOLUME)),
    ]
    candidates: list[Candidate] = []
    try:
        scan_data = ib.reqScannerSubscription(sub, [], filters)
        ib.sleep(settle_seconds)   # let the snapshot populate
        for i, row in enumerate(list(scan_data)[:rows]):
            sym = row.contractDetails.contract.symbol
            if not sym or " " in sym:    # skip preferred / unit listings
                continue
            candidates.append(Candidate(
                symbol=sym.upper(),
                sources=["ibkr"],
                ibkr_rank=i + 1,
            ))
        try:
            ib.cancelScannerSubscription(scan_data)
        except Exception:
            pass
    except Exception as exc:
        safe_log_stdout(f"IBKR scan failed: {exc}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    safe_log_stdout(f"IBKR: {len(candidates)} candidates")
    return candidates


# ---------- stockmarketwatch.com source ----------

SMW_URL = "https://thestockmarketwatch.com/markets/today.aspx"
SMW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.5",
}
SMW_TIMEOUT_S = 15


def _parse_change_pct(text: str) -> float | None:
    """Parse "+12.5%" or "-3.4%" or "+129%" -> float. Returns None on fail."""
    m = re.match(r"\s*([+\-]?\d+(?:\.\d+)?)\s*%", text.strip())
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_price(text: str) -> float | None:
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_smw(max_rows: int = 30) -> list[Candidate]:
    """Scrape the top-gainers table from thestockmarketwatch.com.

    The page renders 4 stockTable elements; table 0 is the top gainers
    (verified 2026-05-21). Best-effort — if the structure changes,
    returns [] with a warning rather than crashing.
    """
    try:
        req = urllib.request.Request(SMW_URL, headers=SMW_HEADERS)
        with urllib.request.urlopen(req, timeout=SMW_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        safe_log_stdout(f"SMW fetch failed: {exc}")
        return []

    tables = re.findall(r'<table class="stockTable".*?</table>', body, re.I | re.S)
    if not tables:
        safe_log_stdout("SMW: no stockTable elements found — page may have changed")
        return []

    candidates: list[Candidate] = []
    seen: set[str] = set()
    # Try the first 2 tables; on the live site, table 0 is gainers and
    # table 3 is sometimes the same list repeated. Stop scanning once
    # we have enough candidates or hit a table that's clearly losers
    # (negative change%).
    for ti, t in enumerate(tables[:2]):
        rows = re.findall(r"<tr.*?</tr>", t, re.I | re.S)
        for r in rows[:max_rows]:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.I | re.S)
            clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            # Expected row shape: [symbol, price, change%]. Header rows
            # (th cells with text labels) won't match _parse_price.
            if len(clean) < 3:
                continue
            sym = clean[0].upper()
            if not re.match(r"^[A-Z][A-Z0-9.\-]{0,7}$", sym):
                continue
            price = _parse_price(clean[1])
            chg = _parse_change_pct(clean[2])
            if price is None or chg is None:
                continue
            # GUNS filter at the source level (cheaper than carrying
            # ineligible symbols through the merge).
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            if chg < MIN_CHANGE_PCT:
                continue
            if sym in seen:
                continue
            seen.add(sym)
            candidates.append(Candidate(
                symbol=sym, sources=["smw"],
                price=price, change_pct=chg,
            ))
    safe_log_stdout(f"SMW: {len(candidates)} candidates")
    return candidates


# ---------- Merge ----------

def merge_sources(*sources: list[Candidate]) -> list[Candidate]:
    """Union by symbol. Preserves order of first appearance; later
    sources merge their fields into the existing record."""
    out: dict[str, Candidate] = {}
    order: list[str] = []
    for src in sources:
        for c in src:
            if c.symbol in out:
                out[c.symbol].merge(c)
            else:
                out[c.symbol] = c
                order.append(c.symbol)
    return [out[s] for s in order]


def rank_for_output(candidates: list[Candidate]) -> list[Candidate]:
    """Sort by: in-IBKR first (by rank), then SMW-only by abs(change_pct)
    desc. Symbols present in both sources rank highest."""
    def key(c: Candidate):
        in_both = ("ibkr" in c.sources and "smw" in c.sources)
        ibkr_rank = c.ibkr_rank if c.ibkr_rank is not None else 9999
        chg_desc = -abs(c.change_pct) if c.change_pct is not None else 0.0
        # Bool sorts False<True; we want True first so negate.
        return (not in_both, ibkr_rank, chg_desc)
    return sorted(candidates, key=key)


# ---------- Output ----------

def render_watchlist_text(date_iso: str, candidates: list[Candidate],
                          n_ibkr: int, n_smw: int) -> str:
    now_et = et_now().strftime("%H:%M:%S ET")
    lines = [
        f"# GUNS watchlist for {date_iso}",
        f"# built at {now_et}",
        f"# sources: ibkr={n_ibkr}, smw={n_smw}, merged={len(candidates)}",
        f"# IBKR scanner: STK.US.MAJOR (NYSE/AMEX/ARCA/NQ.NM/NQ.SC/BATS), "
        f"type={STOCK_TYPE_FILTER}, TOP_PERC_GAIN,",
        f"#               price {MIN_PRICE}-{MAX_PRICE}, "
        f"change% >= {MIN_CHANGE_PCT}, "
        f"avg vol >= {MIN_AVG_VOLUME}, today vol > {MIN_TODAY_VOLUME}",
        f"# thestockmarketwatch.com: top gainers",
        f"# UPSTREAM TODO before market open: remove M&A names, "
        f"non-catalyst names, and high-float (> 100M) names",
        "",
    ]
    for c in candidates:
        lines.append(f"{c.symbol:<8}  # {c.comment()}")
    return "\n".join(lines) + "\n"


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source", choices=["ibkr", "smw", "both"], default="both",
                   help="Which source(s) to query (default: both).")
    p.add_argument("--top", type=int, default=20,
                   help="Cap merged watchlist to top N (default: 20).")
    p.add_argument("--rows", type=int, default=50,
                   help="IBKR scanner numberOfRows (default: 50).")
    p.add_argument("--client-id", type=int, default=82,
                   help="IBKR clientId (default: 82, distinct from bot 71 / "
                        "observer 80 / dashboard 99).")
    p.add_argument("--no-write", action="store_true",
                   help="Print the watchlist to stdout, don't write the file.")
    p.add_argument("--out", default=None,
                   help="Override output path. "
                        "Default: state/watchlist_guns_<today>.txt")
    p.add_argument("--date", default=None,
                   help="Override date (YYYY-MM-DD). Default: today ET.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    date_iso = args.date or et_today_iso()

    ibkr_cands: list[Candidate] = []
    smw_cands: list[Candidate] = []
    if args.source in ("ibkr", "both"):
        ibkr_cands = fetch_ibkr(cfg, rows=args.rows, client_id=args.client_id)
    if args.source in ("smw", "both"):
        smw_cands = fetch_smw()

    merged = merge_sources(ibkr_cands, smw_cands)
    ranked = rank_for_output(merged)
    capped = ranked[: args.top] if args.top > 0 else ranked

    text = render_watchlist_text(date_iso, capped,
                                 n_ibkr=len(ibkr_cands), n_smw=len(smw_cands))
    if args.no_write:
        sys.stdout.write(text)
        return 0

    out_path = Path(args.out) if args.out else guns_watchlist_path(date_iso)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    safe_log_stdout(f"Wrote {len(capped)} symbols to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
