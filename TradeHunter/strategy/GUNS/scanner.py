"""Build the GUNS watchlist for today — self-contained, no sibling-skill
dependencies.

Produces `state/watchlist_guns_<date>.txt` — the GUNS-family input
file consumed by guns_setup1.py and guns_setup5.py. The "guns_"
prefix is intentional: each strategy family (GUNS today; ORB, DITP
etc. in future) has its own scanner with its own filter criteria,
writing to its own watchlist.

Pipeline (all stages live inside TradeHunter/, no external skills):

  1. Gather candidates from up to 2 sources:
       a. IBKR ScannerSubscription — GUNS-tuned filters (PDF slides
          11-12), price 1.50-500, change >= 5%, type=CORP only.
       b. thestockmarketwatch.com top-gainers scrape.
     Union by symbol, ranked: in-both first, then IBKR rank, then SMW
     by abs(change%).

  2. Float filter (scripts/guns_float_lookup.py).
     GUNS rule: float < 100M. Drops large-caps; flags unknown float as
     CAUTION (kept unless --strict-float).

  3. Catalyst classifier (scripts/guns_catalyst_classifier.py).
     Reads each symbol's recent news (yfinance) and tags as good/bad/
     unknown. BAD (M&A target, secondary offering, dilution, going
     concern, SEC action, FDA rejection) gets dropped. UNKNOWN is
     kept with CAUTION unless --strict-catalyst.

  4. Cap to --top N (default 20).

Output file format:
    # GUNS watchlist for YYYY-MM-DD
    # built at HH:MM:SS ET — fully filtered, ready to trade
    # filters: float-cap=100M, catalyst=on
    # source counts: ibkr=N smw=M  -> merged=K  -> filtered=F  -> top=T
    SYM1   # IBKR rank=1 chg=+8.2% px=$3.45 float=22.4M cat=earnings_beat
    SYM2   # SMW chg=+12.5% px=$5.20 float=8.1M cat=fda_good
    ...

Run:
    py scripts/guns_scanner.py                 # both sources, default
    py scripts/guns_scanner.py --source smw    # stockmarketwatch only
    py scripts/guns_scanner.py --top 10        # cap merged list
    py scripts/guns_scanner.py --no-float      # skip float filter
    py scripts/guns_scanner.py --no-catalyst   # skip catalyst filter
    py scripts/guns_scanner.py --strict-catalyst   # drop unknown-news too
    py scripts/guns_scanner.py --float-cap 50_000_000   # tighter cap
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

# --- TradeHunter bootstrap: make sibling layers importable ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution", "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

from _common import (  # noqa: E402  (scripts/_common.py)
    STATE_DIR, et_now, et_today_iso, load_config, safe_log_stdout,
)
# Absolute package path -- works whether scanner runs as a script
# (py strategy/GUNS/scanner.py) or is imported (TradeHunter root is
# on sys.path via the bootstrap above).
from strategy.GUNS._helpers import guns_watchlist_path  # noqa: E402
from yfinance_float import (  # noqa: E402  (resources/yfinance_float.py)
    bulk_get_floats, passes_float_filter, GUNS_FLOAT_CAP,
)
from yfinance_news import (  # noqa: E402  (resources/yfinance_news.py)
    bulk_classify, passes_catalyst_filter,
)


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
    # Filter-stage fields, populated after merge
    float_shares: int | None = None
    float_status: str | None = None     # "low_float" | "unknown" | "high_float"
    catalyst_class: str | None = None   # "good" | "bad" | "unknown"
    catalyst_category: str | None = None
    catalyst_headline: str | None = None
    cautions: list[str] = field(default_factory=list)
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
        if self.float_shares is not None:
            pieces.append(f"float={_fmt_shares(self.float_shares)}")
        elif self.float_status == "unknown":
            pieces.append("float=?")
        if self.catalyst_category:
            pieces.append(f"cat={self.catalyst_category}")
        elif self.catalyst_class == "unknown":
            pieces.append("cat=?")
        if self.cautions:
            pieces.append("CAUTION:" + ",".join(self.cautions))
        return "; ".join(pieces)


def _fmt_shares(n: int | None) -> str:
    if n is None:
        return "n/a"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n:,}"


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


# ---------- Filter stages ----------

def apply_float_filter(
    candidates: list[Candidate],
    *,
    enabled: bool,
    cap: int,
    strict: bool,
) -> tuple[list[Candidate], dict]:
    """Look up float for each symbol; drop high_float / invalid; flag
    unknown as CAUTION (or drop if strict). Returns (kept, stats)."""
    if not enabled or not candidates:
        return candidates, {"checked": 0, "dropped": 0, "unknown": 0}
    symbols = [c.symbol for c in candidates]
    safe_log_stdout(f"Float lookup: {len(symbols)} symbols ...")
    floats = bulk_get_floats(symbols)
    kept: list[Candidate] = []
    n_drop = n_unknown = 0
    for c in candidates:
        n = floats.get(c.symbol)
        c.float_shares = n
        passes, reason = passes_float_filter(n, cap=cap)
        c.float_status = reason
        if reason == "unknown":
            if strict:
                n_drop += 1
                continue
            if "float=?" not in c.cautions:
                c.cautions.append("float=?")
            n_unknown += 1
            kept.append(c)
        elif not passes:
            n_drop += 1
            continue
        else:
            kept.append(c)
    return kept, {"checked": len(symbols), "dropped": n_drop, "unknown": n_unknown}


def apply_catalyst_filter(
    candidates: list[Candidate],
    *,
    enabled: bool,
    strict: bool,
    keep_mna: bool,
) -> tuple[list[Candidate], dict]:
    """Classify each symbol's news; drop BAD (M&A, offering, etc.);
    flag UNKNOWN as CAUTION. With --keep-mna, M&A names are kept with
    a CAUTION tag instead of dropped (the PDF says drop, so default
    is to drop)."""
    if not enabled or not candidates:
        return candidates, {"checked": 0, "dropped": 0, "unknown": 0}
    symbols = [c.symbol for c in candidates]
    safe_log_stdout(f"Catalyst classify: {len(symbols)} symbols ...")
    results = bulk_classify(symbols)
    kept: list[Candidate] = []
    n_drop = n_unknown = 0
    for c in candidates:
        r = results.get(c.symbol, {})
        c.catalyst_class = r.get("classification")
        c.catalyst_category = r.get("category")
        c.catalyst_headline = r.get("headline")
        passes, reason = passes_catalyst_filter(r, strict=strict)
        if c.catalyst_class == "bad" and keep_mna:
            tag = f"bad-news:{c.catalyst_category}"
            if tag not in c.cautions:
                c.cautions.append(tag)
            kept.append(c)
            continue
        if not passes:
            n_drop += 1
            continue
        if c.catalyst_class == "unknown":
            if "no-fresh-news" not in c.cautions:
                c.cautions.append("no-fresh-news")
            n_unknown += 1
        kept.append(c)
    return kept, {"checked": len(symbols), "dropped": n_drop, "unknown": n_unknown}


# ---------- Output ----------

def render_watchlist_text(
    date_iso: str,
    candidates: list[Candidate],
    *,
    n_ibkr: int, n_smw: int, n_merged: int, n_after_filters: int,
    float_enabled: bool, float_cap: int,
    catalyst_enabled: bool, catalyst_strict: bool,
) -> str:
    now_et = et_now().strftime("%H:%M:%S ET")
    float_desc = (
        f"float-cap={_fmt_shares(float_cap)}" if float_enabled else "float=off"
    )
    cat_desc = (
        ("catalyst=strict" if catalyst_strict else "catalyst=on")
        if catalyst_enabled else "catalyst=off"
    )
    lines = [
        f"# GUNS watchlist for {date_iso}",
        f"# built at {now_et} -- fully filtered, ready to trade",
        f"# filters: {float_desc}, {cat_desc}",
        f"# universe filters (per source): price ${MIN_PRICE:.2f}-${MAX_PRICE:.0f}, "
        f"change% >= {MIN_CHANGE_PCT}, IBKR avg vol >= {MIN_AVG_VOLUME}, "
        f"today vol > {MIN_TODAY_VOLUME}, type={STOCK_TYPE_FILTER}",
        f"# counts: ibkr={n_ibkr} smw={n_smw} -> merged={n_merged} "
        f"-> after-filters={n_after_filters} -> top={len(candidates)}",
        "",
    ]
    if not candidates:
        lines.append("# (no symbols passed all filters today)")
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
    # Filter stages
    p.add_argument("--no-float", action="store_true",
                   help="Skip the float filter entirely.")
    p.add_argument("--float-cap", type=int, default=GUNS_FLOAT_CAP,
                   help=f"Max float in shares (default: {GUNS_FLOAT_CAP:,} = 100M, "
                        f"per GUNS PDF).")
    p.add_argument("--strict-float", action="store_true",
                   help="Drop symbols whose float couldn't be determined "
                        "(default: keep with CAUTION).")
    p.add_argument("--no-catalyst", action="store_true",
                   help="Skip the catalyst classifier entirely.")
    p.add_argument("--strict-catalyst", action="store_true",
                   help="Drop symbols with no fresh-window news "
                        "(default: keep with CAUTION). BAD catalysts are "
                        "always dropped unless --keep-mna.")
    p.add_argument("--keep-mna", action="store_true",
                   help="Keep M&A / offering / dilution names with CAUTION "
                        "instead of dropping. Not recommended.")
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
    n_merged = len(merged)

    # Filter stages — float first (cheap, weeds out large-caps before we
    # spend yfinance-news round-trips on them).
    float_kept, float_stats = apply_float_filter(
        merged,
        enabled=not args.no_float,
        cap=args.float_cap,
        strict=args.strict_float,
    )
    if not args.no_float:
        safe_log_stdout(
            f"Float filter: dropped {float_stats['dropped']}, "
            f"unknown {float_stats['unknown']}, kept {len(float_kept)}"
        )

    catalyst_kept, cat_stats = apply_catalyst_filter(
        float_kept,
        enabled=not args.no_catalyst,
        strict=args.strict_catalyst,
        keep_mna=args.keep_mna,
    )
    if not args.no_catalyst:
        safe_log_stdout(
            f"Catalyst filter: dropped {cat_stats['dropped']}, "
            f"unknown {cat_stats['unknown']}, kept {len(catalyst_kept)}"
        )

    ranked = rank_for_output(catalyst_kept)
    capped = ranked[: args.top] if args.top > 0 else ranked

    text = render_watchlist_text(
        date_iso, capped,
        n_ibkr=len(ibkr_cands), n_smw=len(smw_cands),
        n_merged=n_merged, n_after_filters=len(catalyst_kept),
        float_enabled=not args.no_float, float_cap=args.float_cap,
        catalyst_enabled=not args.no_catalyst,
        catalyst_strict=args.strict_catalyst,
    )
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
