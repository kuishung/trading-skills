"""Dry-run the IBKR data adapter the bot uses in production.

Exercises the same code paths trade_day.py and scan_premarket.py walk:
  - ibkr_pm_bars (1-min bars 04:00 ET -> now, pre-market only)
  - ibkr_full_day_minute_bars (1-min bars 04:00 ET -> now, PM + RTH)
  - ibkr_latest_quote (top-of-book bid/ask)

Reads config.json for connection params. Safe to run any time — no orders,
no Alpaca touch.
"""
from __future__ import annotations

# Python 3.14 / eventkit shim — must run before importing the adapter.
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ibkr_data import (
    ibkr_pm_bars,
    ibkr_full_day_minute_bars,
    ibkr_latest_quote,
)

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
SYMBOLS = ["SPY", "AAPL", "NVDA"]


def _show_bars(label: str, bars_by_sym: dict) -> None:
    print(f"\n[{label}]")
    for sym in SYMBOLS:
        bars = bars_by_sym.get(sym, [])
        if not bars:
            print(f"  {sym}: (no bars)")
            continue
        first, last = bars[0], bars[-1]
        print(f"  {sym}: {len(bars):>4} bars  "
              f"first={first['t'].strftime('%H:%M')} O={first['o']:.2f}  "
              f"last={last['t'].strftime('%H:%M')} C={last['c']:.2f} V={last['v']}")


def _show_quotes(quotes: dict) -> None:
    print("\n[ibkr_latest_quote — top of book]")
    for sym in SYMBOLS:
        q = quotes.get(sym, {})
        bid, ask = q.get("bid"), q.get("ask")
        bs, asize = q.get("bid_size"), q.get("ask_size")
        spread = (ask - bid) if (bid and ask) else None
        spread_str = f"{spread:.3f}" if spread is not None else "n/a"
        print(f"  {sym}: bid={bid}@{bs}  ask={ask}@{asize}  spread={spread_str}")


def main() -> None:
    print(f"Using config: data_provider={CFG.get('data_provider')}, "
          f"port={CFG.get('ibkr_port')}, clientId={CFG.get('ibkr_client_id')}")
    print(f"UTC now: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"Symbols: {SYMBOLS}")

    pm = ibkr_pm_bars(SYMBOLS, CFG, fake_now=None)
    _show_bars("ibkr_pm_bars (04:00 ET -> now, PM only)", pm)

    full = ibkr_full_day_minute_bars(SYMBOLS, CFG, fake_now=None)
    _show_bars("ibkr_full_day_minute_bars (PM + RTH)", full)

    quotes = ibkr_latest_quote(SYMBOLS, CFG)
    _show_quotes(quotes)

    print("\nOK — adapter dry-run complete.")


if __name__ == "__main__":
    main()
