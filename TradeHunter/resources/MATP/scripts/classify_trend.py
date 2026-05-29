#!/usr/bin/env python
"""Classify each ticker in MATP_table.csv as Uptrend / Sideways / Downtrend
using EMA20, EMA50, EMA200 on the 1-year / 1-day chart.

Rule (strict stacking + EMA50 slope):
  Uptrend   if close > EMA20 > EMA50 > EMA200 AND EMA50 today > EMA50 20 bars ago
  Downtrend if close < EMA20 < EMA50 < EMA200 AND EMA50 today < EMA50 20 bars ago
  Sideways  otherwise

Data source: yfinance (1y of daily bars, batched in one HTTP call).

Writes back to MATP_table.csv with a new `Trend` column appended at the end.
Run AFTER the main MATP pipeline (CSV must already exist).

Usage:
    py scripts/classify_trend.py
    py scripts/classify_trend.py --csv path/to/other.csv
    py scripts/classify_trend.py --slope-window 10
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_CSV = SKILL_DIR / "MATP_table.csv"

# How many trailing bars to compare EMA50 against to decide its slope sign.
# 20 trading days ≈ 1 calendar month — long enough to ignore noise, short
# enough to react when a trend genuinely turns.
DEFAULT_SLOPE_WINDOW = 20

# Minimum bars needed for EMA200 to be reasonably seeded. Below this we
# can't classify confidently — mark the ticker "Unknown".
MIN_BARS_FOR_EMA200 = 210


def ema(values: list[float], period: int) -> list[float]:
    """Standard exponential moving average. Seeded with values[0]."""
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def classify(closes: list[float], slope_window: int) -> str:
    if len(closes) < MIN_BARS_FOR_EMA200:
        return "Unknown"
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    c = closes[-1]
    a20, a50, a200 = e20[-1], e50[-1], e200[-1]
    # EMA50 slope: today vs. `slope_window` bars ago.
    slope_idx = -1 - slope_window
    if abs(slope_idx) > len(e50):
        return "Unknown"
    slope_up = e50[-1] > e50[slope_idx]
    slope_dn = e50[-1] < e50[slope_idx]
    if c > a20 > a50 > a200 and slope_up:
        return "Uptrend"
    if c < a20 < a50 < a200 and slope_dn:
        return "Downtrend"
    return "Sideways"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=str(DEFAULT_CSV),
                   help=f"Path to MATP CSV (default: {DEFAULT_CSV})")
    p.add_argument("--slope-window", type=int, default=DEFAULT_SLOPE_WINDOW,
                   help=f"Bars to look back for EMA50 slope (default: {DEFAULT_SLOPE_WINDOW})")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}. Run the MATP pipeline first.")

    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance not installed. Run: py -m pip install -r requirements.txt")

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if not rows:
        sys.exit(f"ERROR: CSV has no rows: {csv_path}")
    if "Ticker" not in fieldnames:
        sys.exit("ERROR: CSV missing 'Ticker' column.")

    tickers = [r["Ticker"].strip() for r in rows if r["Ticker"].strip()]
    if not tickers:
        sys.exit("ERROR: No tickers in CSV.")

    print(f"Fetching 1y daily bars for {len(tickers)} tickers from yfinance...")
    # One batched HTTP call. group_by="ticker" makes the columns easy to slice
    # back out per-symbol. auto_adjust=True applies split/dividend adjustments
    # so EMAs are computed on a continuous, comparable series.
    data = yf.download(
        tickers=tickers,
        period="1y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    def extract_closes(sub) -> list[float]:
        return [float(c) for c in sub["Close"].dropna().tolist()]

    trends: dict[str, str] = {}
    failed: list[str] = []
    for t in tickers:
        try:
            sub = data if len(tickers) == 1 else data[t]
            closes = extract_closes(sub)
            if not closes:
                failed.append(t)
                continue
            trends[t] = classify(closes, args.slope_window)
        except (KeyError, ValueError, TypeError):
            failed.append(t)

    # Retry failed tickers sequentially. The threaded download path hits a
    # SQLite cache lock ("database is locked") on yfinance's tz cache for a
    # small number of tickers per run, but the data is fine on a second
    # serial pass.
    if failed:
        print(f"Retrying {len(failed)} failed tickers sequentially: {', '.join(failed)}")
        for t in failed:
            try:
                hist = yf.Ticker(t).history(period="1y", interval="1d", auto_adjust=True)
                closes = extract_closes(hist)
                trends[t] = classify(closes, args.slope_window) if closes else "Unknown"
            except Exception:
                trends[t] = "Unknown"

    # Append Trend column at end. If the column already exists (re-run),
    # overwrite in place rather than duplicating.
    if "Trend" not in fieldnames:
        fieldnames.append("Trend")
    for r in rows:
        r["Trend"] = trends.get(r["Ticker"].strip(), "Unknown")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # Summary counts.
    from collections import Counter
    counts = Counter(r["Trend"] for r in rows)
    total = len(rows)
    print(f"\nWrote {total} rows to {csv_path}")
    print(f"\nTrend breakdown:")
    for label in ("Uptrend", "Sideways", "Downtrend", "Unknown"):
        n = counts.get(label, 0)
        pct = 100 * n / total if total else 0
        print(f"  {label:9s} {n:3d}  ({pct:4.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
