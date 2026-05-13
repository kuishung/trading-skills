#!/usr/bin/env python
"""Generate a TradingView-importable watchlist from MATP_table.csv.

The output (MATP_watchlist.txt) is plain text in TradingView's import
format: one EXCHANGE:TICKER per line, with a section header showing the
generation date so friends can see at a glance which screener vintage
they're looking at.

Import in TradingView:
  Watchlist panel -> "..." menu -> Import list -> select the .txt file.

Usage:
    py scripts/generate_watchlist.py
"""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CSV_PATH = SKILL_DIR / "MATP_table.csv"
TXT_PATH = SKILL_DIR / "MATP_watchlist.txt"


def main() -> int:
    if not CSV_PATH.exists():
        sys.exit(f"ERROR: CSV not found: {CSV_PATH}. Run the MATP pipeline first.")

    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        sys.exit(f"ERROR: CSV has no rows: {CSV_PATH}")

    required = {"Ticker", "Exchange"}
    missing = required - set(rows[0].keys())
    if missing:
        sys.exit(f"ERROR: CSV missing required columns: {missing}")

    today = date.today().isoformat()
    lines = [f"###MATP {today}"]
    for r in rows:
        exchange = r["Exchange"].strip()
        ticker = r["Ticker"].strip()
        if not exchange or not ticker:
            continue
        lines.append(f"{exchange}:{ticker}")

    TXT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} symbols to {TXT_PATH}")
    print(f"Section header: ###MATP {today}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
