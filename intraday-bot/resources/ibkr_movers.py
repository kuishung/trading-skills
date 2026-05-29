"""IBKR US market scanner — general-purpose movers / volume / gappers query.

Wraps `ib_insync.ScannerSubscription` to expose IBKR's full market-scanner
catalog as a clean Python API + CLI. Unlike `strategy/GUNS/scanner.py`
(which is GUNS-tuned and writes a watchlist file), this module is
strategy-agnostic — it returns the raw scan rows + their snapshot prices /
volumes / change-% so any caller (other strategies, dashboard panels,
ad-hoc CLI inspection) can build on top.

ScannerSubscription is a STREAMING subscription endpoint, not a historical
request — it does NOT count toward IBKR's 60-per-600s historical-data
pacing cap. Safe to run alongside the 1m parquet ingest (clientId 83) or
the live bot (clientId 71). This module uses clientId 84 to stay clear.

Public API:
    list_scan_codes() -> list[str]                  # common scan codes
    get_movers(scan_code, *, location, rows, ...) -> list[MoverRow]

CLI:
    py resources/ibkr_movers.py gainers                  # top % gainers
    py resources/ibkr_movers.py losers --rows 20         # top % losers
    py resources/ibkr_movers.py active                   # most active
    py resources/ibkr_movers.py volume                   # hot by volume
    py resources/ibkr_movers.py gappers                  # high-open-gap
    py resources/ibkr_movers.py custom --scan TOP_PERC_GAIN --location STK.US.MAJOR --min-price 5 --min-volume 1000000

Common scan codes (IBKR's full catalog has ~200):
    TOP_PERC_GAIN          top % gainers
    TOP_PERC_LOSE          top % losers
    MOST_ACTIVE            most actively traded
    HOT_BY_VOLUME          highest volume vs avg
    HOT_BY_PRICE           biggest price changes
    TOP_TRADE_RATE         high trade rate
    HIGH_OPEN_GAP          high open-gap (gap-up)
    LOW_OPEN_GAP           low open-gap (gap-down)
    TOP_OPEN_PERC_GAIN     top % gain from today's open
    TOP_OPEN_PERC_LOSE     top % loss from today's open
    HIGH_VS_13W_HL         near 13-week high
    LOW_VS_13W_HL          near 13-week low
    HIGH_VS_52W_HL         near 52-week high
    LOW_VS_52W_HL          near 52-week low

Common locations:
    STK.US.MAJOR           NYSE + NASDAQ + AMEX
    STK.US                 broader US
    STK.NYSE               NYSE only
    STK.NASDAQ             NASDAQ only
    STK.OTC                OTC (pink sheets)
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

# --- TradeHunter bootstrap ---
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

from _common import load_config  # noqa: E402

# Reserved clientIds (see ibkr_data.py + ibkr_history.py):
#   71 live bot  ·  80 observer  ·  82 GUNS scanner  ·
#   83 history ingest  ·  98 probe  ·  99 dashboard
# This module uses 84 to stay clear.
DEFAULT_CLIENT_ID = 84

COMMON_SCAN_CODES = [
    "TOP_PERC_GAIN", "TOP_PERC_LOSE", "MOST_ACTIVE",
    "HOT_BY_VOLUME", "HOT_BY_PRICE", "TOP_TRADE_RATE",
    "HIGH_OPEN_GAP", "LOW_OPEN_GAP",
    "TOP_OPEN_PERC_GAIN", "TOP_OPEN_PERC_LOSE",
    "HIGH_VS_13W_HL", "LOW_VS_13W_HL",
    "HIGH_VS_52W_HL", "LOW_VS_52W_HL",
]

# Friendly preset names mapped to (scan_code, default_min_change_pct, default_min_volume)
PRESETS = {
    "gainers":  ("TOP_PERC_GAIN",      3.0, 500_000),
    "losers":   ("TOP_PERC_LOSE",      3.0, 500_000),
    "active":   ("MOST_ACTIVE",        None, None),
    "volume":   ("HOT_BY_VOLUME",      None, None),
    "gappers":  ("HIGH_OPEN_GAP",      3.0, 500_000),
    "downgap":  ("LOW_OPEN_GAP",       3.0, 500_000),
    "open_up":  ("TOP_OPEN_PERC_GAIN", 1.0, 500_000),
    "open_dn":  ("TOP_OPEN_PERC_LOSE", 1.0, 500_000),
    "near_hi":  ("HIGH_VS_52W_HL",     None, 100_000),
    "near_lo":  ("LOW_VS_52W_HL",      None, 100_000),
}


@dataclass
class MoverRow:
    rank: int
    symbol: str
    exchange: str | None
    sec_type: str | None
    primary_exchange: str | None


def list_scan_codes() -> list[str]:
    return list(COMMON_SCAN_CODES)


def get_movers(scan_code: str = "TOP_PERC_GAIN", *,
               location: str = "STK.US.MAJOR",
               rows: int = 25,
               min_price: float | None = 1.0,
               max_price: float | None = None,
               min_volume: int | None = 500_000,
               min_avg_volume: int | None = None,
               min_change_pct: float | None = None,
               max_change_pct: float | None = None,
               min_market_cap_million: float | None = None,
               stock_type_filter: str = "CORP",
               settle_seconds: float = 4.0,
               client_id: int = DEFAULT_CLIENT_ID,
               cfg: dict | None = None) -> list[MoverRow]:
    """Run an IBKR market scanner subscription and return the ranked rows.

    `scan_code`     — IBKR scan code (see COMMON_SCAN_CODES for the typical set).
    `location`      — universe slice; STK.US.MAJOR is NYSE+NASDAQ+AMEX.
    `rows`          — max rows to return (IBKR cap = 50 per scan).
    `min/max_price` — price band filter (USD).
    `min_volume`    — today's volume floor.
    `min_avg_volume`— 30-day average volume floor.
    `min/max_change_pct` — % change band filter.
    `stock_type_filter` — "CORP" (common shares), "ETF", "ALL", etc.
    `settle_seconds` — how long to let the streaming snapshot populate.
    `client_id`     — IBKR clientId; default 84 stays clear of other bot processes.
    """
    try:
        from ib_insync import IB, ScannerSubscription, TagValue
    except ImportError as exc:
        raise ImportError(
            "ibkr_movers requires ib_insync; install via `pip install ib_insync`."
        ) from exc

    cfg = cfg or load_config()
    host = cfg.get("ibkr_host", "127.0.0.1")
    port = int(cfg.get("ibkr_port", 7497))

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=8, readonly=True)
    except Exception as exc:
        sys.stderr.write(f"[ibkr_movers] connect failed {host}:{port}: {exc}\n")
        return []

    sub_kwargs: dict = dict(
        instrument="STK",
        locationCode=location,
        scanCode=scan_code,
        numberOfRows=rows,
        stockTypeFilter=stock_type_filter,
    )
    if min_price is not None:
        sub_kwargs["abovePrice"] = float(min_price)
    if max_price is not None:
        sub_kwargs["belowPrice"] = float(max_price)
    if min_volume is not None:
        sub_kwargs["aboveVolume"] = int(min_volume)
    sub = ScannerSubscription(**sub_kwargs)

    tag_filters: list = []
    if min_change_pct is not None:
        tag_filters.append(TagValue("changePercAbove", str(min_change_pct)))
    if max_change_pct is not None:
        tag_filters.append(TagValue("changePercBelow", str(max_change_pct)))
    if min_avg_volume is not None:
        tag_filters.append(TagValue("avgVolumeAbove", str(int(min_avg_volume))))
    if min_market_cap_million is not None:
        tag_filters.append(TagValue("marketCapAbove1e6",
                                    str(int(min_market_cap_million))))

    out: list[MoverRow] = []
    try:
        scan_data = ib.reqScannerSubscription(sub, [], tag_filters)
        ib.sleep(settle_seconds)
        for i, row in enumerate(list(scan_data)[:rows]):
            try:
                c = row.contractDetails.contract
            except AttributeError:
                continue
            sym = (c.symbol or "").upper()
            if not sym or " " in sym:
                continue
            out.append(MoverRow(
                rank=i + 1,
                symbol=sym,
                exchange=getattr(c, "exchange", None),
                sec_type=getattr(c, "secType", None),
                primary_exchange=getattr(c, "primaryExchange", None),
            ))
        try:
            ib.cancelScannerSubscription(scan_data)
        except Exception:
            pass
    except Exception as exc:
        sys.stderr.write(f"[ibkr_movers] scan failed ({scan_code}): {exc}\n")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return out


# ---------- CLI ----------

def _print_rows(rows: list[MoverRow], header: str) -> None:
    sys.stdout.write(f"# {header} — {len(rows)} rows\n")
    sys.stdout.write(f"{'#':>3}  {'sym':<7} {'exch':<8} {'primary':<10}\n")
    for r in rows:
        sys.stdout.write(
            f"{r.rank:>3}  {r.symbol:<7} "
            f"{(r.exchange or '-'):<8} {(r.primary_exchange or '-'):<10}\n"
        )


def _cmd_preset(args) -> int:
    """Run a named preset (`gainers`, `losers`, `active`, …)."""
    if args.preset not in PRESETS:
        sys.stderr.write(f"unknown preset: {args.preset}\n"
                         f"choices: {sorted(PRESETS)}\n")
        return 2
    scan_code, default_min_change, default_min_vol = PRESETS[args.preset]
    rows = get_movers(
        scan_code=scan_code,
        location=args.location,
        rows=args.rows,
        min_price=args.min_price,
        max_price=args.max_price,
        min_volume=args.min_volume if args.min_volume is not None else default_min_vol,
        min_change_pct=args.min_change_pct if args.min_change_pct is not None else default_min_change,
        settle_seconds=args.settle,
    )
    if args.json:
        sys.stdout.write(json.dumps({
            "scan_code": scan_code, "preset": args.preset,
            "location": args.location, "rows": [asdict(r) for r in rows],
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2))
        sys.stdout.write("\n")
    else:
        _print_rows(rows, f"preset={args.preset} scan={scan_code} loc={args.location}")
    return 0


def _cmd_custom(args) -> int:
    rows = get_movers(
        scan_code=args.scan,
        location=args.location,
        rows=args.rows,
        min_price=args.min_price,
        max_price=args.max_price,
        min_volume=args.min_volume,
        min_avg_volume=args.min_avg_volume,
        min_change_pct=args.min_change_pct,
        max_change_pct=args.max_change_pct,
        min_market_cap_million=args.min_market_cap_million,
        stock_type_filter=args.stock_type,
        settle_seconds=args.settle,
    )
    if args.json:
        sys.stdout.write(json.dumps({
            "scan_code": args.scan, "location": args.location,
            "rows": [asdict(r) for r in rows],
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2))
        sys.stdout.write("\n")
    else:
        _print_rows(rows, f"scan={args.scan} loc={args.location}")
    return 0


def _cmd_codes(args) -> int:
    for c in COMMON_SCAN_CODES:
        print(c)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Preset shortcuts: gainers, losers, active, etc.
    for name in PRESETS:
        p = sub.add_parser(name, help=f"preset: {PRESETS[name][0]}")
        p.add_argument("--location", default="STK.US.MAJOR")
        p.add_argument("--rows", type=int, default=25)
        p.add_argument("--min-price", type=float, default=1.0)
        p.add_argument("--max-price", type=float, default=None)
        p.add_argument("--min-volume", type=int, default=None)
        p.add_argument("--min-change-pct", type=float, default=None)
        p.add_argument("--settle", type=float, default=4.0)
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=_cmd_preset, preset=name)

    # Custom: full control
    p_cust = sub.add_parser("custom", help="full-control scan with any scan code")
    p_cust.add_argument("--scan", default="TOP_PERC_GAIN",
                        help=f"IBKR scan code (see `codes` for the common set)")
    p_cust.add_argument("--location", default="STK.US.MAJOR")
    p_cust.add_argument("--rows", type=int, default=25)
    p_cust.add_argument("--min-price", type=float, default=1.0)
    p_cust.add_argument("--max-price", type=float, default=None)
    p_cust.add_argument("--min-volume", type=int, default=None)
    p_cust.add_argument("--min-avg-volume", type=int, default=None)
    p_cust.add_argument("--min-change-pct", type=float, default=None)
    p_cust.add_argument("--max-change-pct", type=float, default=None)
    p_cust.add_argument("--min-market-cap-million", type=float, default=None)
    p_cust.add_argument("--stock-type", default="CORP")
    p_cust.add_argument("--settle", type=float, default=4.0)
    p_cust.add_argument("--json", action="store_true")
    p_cust.set_defaults(func=_cmd_custom)

    p_codes = sub.add_parser("codes", help="list common scan codes")
    p_codes.set_defaults(func=_cmd_codes)

    return ap


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
