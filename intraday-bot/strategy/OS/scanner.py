"""OS family pre-market scanner.

Builds tomorrow's intraday watchlist by hitting IBKR's TOP_PERC_GAIN
scanner. No catalyst classifier, no float cap — just price/volume/change
filters. Designed to be called from the orchestrator's shortlist phase
at 09:00 ET; can also be invoked manually via CLI.

Output: `state/watchlist_os_<date>.txt`, one symbol per line.

CLI:
    py strategy/OS/scanner.py                 # default settings
    py strategy/OS/scanner.py --rows 30
    py strategy/OS/scanner.py --min-change-pct 5.0
    py strategy/OS/scanner.py --no-write      # print only
"""
from __future__ import annotations

import argparse
import sys
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

from _common import load_config  # noqa: E402
from ibkr_movers import get_movers  # noqa: E402  (resources/ibkr_movers.py)
from strategy.OS._helpers import (  # noqa: E402
    OS_MAX_PRICE, OS_MIN_PRICE, os_watchlist_path,
)


def build_os_watchlist(*,
                       rows: int = 50,
                       min_change_pct: float = 3.0,
                       min_avg_volume: int = 200_000,
                       cfg: dict | None = None) -> list[str]:
    """Pull TOP_PERC_GAIN, apply OS-specific price band ($1.50-$50), drop
    multi-word tickers. Returns the candidate list (does not write the
    file — see write_os_watchlist for that)."""
    cfg = cfg or load_config()
    movers = get_movers(
        scan_code="TOP_PERC_GAIN",
        location="STK.US.MAJOR",
        rows=rows,
        min_price=OS_MIN_PRICE,
        max_price=OS_MAX_PRICE,
        min_avg_volume=min_avg_volume,
        min_change_pct=min_change_pct,
        stock_type_filter="CORP",
        cfg=cfg,
    )
    out: list[str] = []
    seen: set[str] = set()
    for m in movers:
        sym = (m.symbol or "").upper()
        if not sym or " " in sym:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def write_os_watchlist(symbols: list[str], date_iso: str) -> Path:
    """Write the watchlist file. Header line carries a built-at timestamp
    + filter summary so consumers (and humans reading the file) can tell
    when + how it was generated."""
    path = os_watchlist_path(date_iso)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        f"# OS family pre-market watchlist for {date_iso}",
        f"# built at {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"# filters: price [{OS_MIN_PRICE}, {OS_MAX_PRICE}], min change % 3, "
        f"min avg vol 200K, source = IBKR TOP_PERC_GAIN",
        f"# {len(symbols)} symbols",
    ]
    lines = header + list(symbols)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------- CLI ----------

def _today_et_iso() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        import pytz  # type: ignore
        tz = pytz.timezone("America/New_York")
    return datetime.now(timezone.utc).astimezone(tz).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=50)
    ap.add_argument("--min-change-pct", type=float, default=3.0)
    ap.add_argument("--min-avg-volume", type=int, default=200_000)
    ap.add_argument("--no-write", action="store_true",
                    help="print only; do not write state/watchlist_os_*.txt")
    ap.add_argument("--date",
                    help="YYYY-MM-DD target date (default: today ET)")
    args = ap.parse_args()

    syms = build_os_watchlist(rows=args.rows,
                              min_change_pct=args.min_change_pct,
                              min_avg_volume=args.min_avg_volume)
    print(f"# OS scanner: {len(syms)} candidates")
    for s in syms:
        print(s)
    if not args.no_write:
        target = args.date or _today_et_iso()
        path = write_os_watchlist(syms, target)
        rel = path.relative_to(SKILL_DIR)
        sys.stdout.write(f"# wrote {rel}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
