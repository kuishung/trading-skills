"""yFinance daily-bar adapter for the dashboard's interactive scanner.

Distinct purpose from `resources/yfinance_history.py`:

- `yfinance_history.py` writes parquets (used for backtesting + the
  nightly/EOD ingest path).
- This module returns bars **in-memory only** in the canonical
  `[{t,o,h,l,c,v}, ...]` bar-dict shape -- never touches parquets.

User architectural directive 2026-05-27: "the parquet store i intend
to use it for backtesting only, this scanning of daily setup through
yfinance only". The dashboard's Scanner view fires this adapter so
the daily-chart scan path is fully yFinance-native -- no parquet
read, no bars_store coupling on disk.

Returns the same dict shape that `bars_store.load_bars` returns
(`{"t": "YYYY-MM-DDTHH:MM:SSZ", "o": ..., "h": ..., "l": ..., "c": ...,
"v": ...}`) so it can be monkey-patched into `bars_store.load_bars`
for the duration of a scan, letting the existing DITP detection code
(`strategy/DITP/scanner.py::evaluate`) run unchanged on yFinance data.

Usage:
    from yf_daily_bars import fetch_daily_batch
    bars_by_symbol = fetch_daily_batch(["AAPL", "MSFT"], lookback_days=400)
    # -> {"AAPL": [{t,o,h,l,c,v}, ...], "MSFT": [...]}

Performance: yfinance.download() with `threads=True` batches downloads
across symbols. ~30s for SP500 (~500 symbols, 1-year daily, prepost
disabled) on a warm session-cookie cache. Significantly faster than
IBKR's 60-per-600s pacing cap (would be ~9 hours for the same).

Caveats per user rule 2026-05-23 ("the real time data we cannot use
yFinance, we have to only rely to IBKR and Alpaca only"): this is
DAILY data, not real-time. yFinance daily bars stamp the SESSION
CLOSE for trading days, with ~15-min delay during the session and
no delay after close. Acceptable for "scan for tomorrow's setups
using today's completed daily candle" use case. Never use for
intraday live quotes.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Iterable


def _require_yfinance():
    try:
        import yfinance  # type: ignore
        return yfinance
    except ImportError as exc:
        raise ImportError(
            "yfinance is not installed. pip install yfinance"
        ) from exc


def _normalize_symbol_for_yf(sym: str) -> str:
    """yFinance uses '-' instead of '.' for share classes (BRK.B -> BRK-B).
    The S&P 500 list from Wikipedia uses '.'; map to '-' here for the
    fetch, then map BACK on the result keys so callers see the
    Wikipedia/AutoCount form.
    """
    return sym.replace(".", "-").upper()


def _denormalize_symbol_from_yf(sym: str) -> str:
    """Inverse of _normalize_symbol_for_yf -- yFinance returns 'BRK-B',
    but our journal + scanner code stores 'BRK.B'. The dashboard
    rendering can stay symmetric by undoing the swap on the way out.
    """
    return sym.replace("-", ".").upper()


def _df_to_bar_dicts(df) -> list[dict]:
    """Convert a single-symbol FLAT-COLUMN yfinance DataFrame to the
    canonical [{t,o,h,l,c,v}] bar-dict list that bars_store.load_bars
    returns.

    Caller is responsible for slicing a multi-index DataFrame to a
    single symbol BEFORE calling this (e.g. `df["AAPL"]`).

    yFinance daily indexes are TIMEZONE-NAIVE dates by default. We
    stamp them as UTC midnight in ISO format with trailing 'Z' so they
    sort consistently with the rest of the codebase (which uses
    "YYYY-MM-DDTHH:MM:SSZ" everywhere).
    """
    import math
    out: list[dict] = []
    if df is None or len(df) == 0:
        return out
    for idx, row in df.iterrows():
        # idx is a pandas Timestamp; normalize to UTC midnight.
        try:
            ts = idx.tz_localize("UTC") if idx.tzinfo is None else idx.tz_convert("UTC")
        except (AttributeError, TypeError):
            ts = datetime.fromisoformat(str(idx)).replace(tzinfo=timezone.utc)
        iso = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            o = float(row["Open"])
            h = float(row["High"])
            l = float(row["Low"])
            c = float(row["Close"])
            v = float(row["Volume"]) if row["Volume"] is not None else 0.0
        except (KeyError, ValueError, TypeError):
            continue
        if any(math.isnan(x) for x in (o, h, l, c)):
            continue
        out.append({"t": iso, "o": o, "h": h, "l": l, "c": c, "v": v})
    return out


def fetch_daily_batch(
    symbols: Iterable[str],
    *,
    lookback_days: int = 400,
    threads: bool = True,
    progress: bool = False,
) -> dict[str, list[dict]]:
    """Fetch daily bars for many symbols via yfinance.download() in one
    batched call. Returns {SYMBOL: [bars]} keyed by the symbol form the
    CALLER passed in (preserving '.' vs '-' conventions).

    lookback_days defaults to 400 because the DITP P2 detector needs >=
    220 daily bars for EMA200 to stabilize + headroom for the
    resistance lookback window (90 bars) + flush-up scan + breach
    check. 400 calendar days covers ~260 trading days, comfortably
    enough.
    """
    yf = _require_yfinance()
    in_symbols = [s.strip().upper() for s in symbols if s and s.strip()]
    if not in_symbols:
        return {}
    yf_symbols = [_normalize_symbol_for_yf(s) for s in in_symbols]
    # Map yf-form -> original-form for the return key mapping.
    yf_to_orig: dict[str, str] = {}
    for orig, yf_form in zip(in_symbols, yf_symbols):
        yf_to_orig[yf_form] = orig

    # period= accepts 'Xd', 'Xmo', 'Xy', 'max'. For 400 days, use period
    # spec "{N}d" -- yfinance handles weekend/holiday gaps gracefully.
    period = f"{lookback_days}d"

    # group_by="ticker" returns a multi-index DataFrame indexed by date
    # with top-level column = ticker symbol, second level = OHLCV. Easy
    # to slice per symbol.
    df = yf.download(
        tickers=" ".join(yf_symbols),
        period=period,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        prepost=False,        # daily close is end-of-RTH; prepost adds noise
        threads=threads,
        progress=progress,
    )

    out: dict[str, list[dict]] = {}
    # With group_by="ticker", yfinance returns 2-level multi-index columns
    # in BOTH single-symbol and multi-symbol cases. Top level = ticker,
    # second level = OHLCV. We slice by ticker to get a flat-column
    # sub-DataFrame, then convert.
    if not hasattr(df, "columns") or getattr(df.columns, "nlevels", 1) < 2:
        # Defensive: yfinance contract changed or returned flat for some reason.
        # If single symbol requested AND df is non-empty, treat the whole df
        # as that symbol's bars (must be flat-column at this point).
        if len(yf_symbols) == 1 and df is not None and len(df) > 0:
            sym_yf = yf_symbols[0]
            out[yf_to_orig[sym_yf]] = _df_to_bar_dicts(df)
        return out

    try:
        top_level = df.columns.get_level_values(0).unique().tolist()
    except AttributeError:
        return out
    for sym_yf in top_level:
        if sym_yf not in yf_to_orig:
            continue
        try:
            sub = df[sym_yf]
        except KeyError:
            continue
        out[yf_to_orig[sym_yf]] = _df_to_bar_dicts(sub)
    return out


def fetch_daily_single(symbol: str, *, lookback_days: int = 400) -> list[dict]:
    """Convenience wrapper for a single-symbol fetch. Returns the bar
    list directly (not wrapped in a dict). Empty list if symbol unknown
    or fetch fails.
    """
    result = fetch_daily_batch([symbol], lookback_days=lookback_days)
    return result.get(symbol.upper(), [])


# ---------- CLI for ad-hoc inspection ----------

def _main(argv: list[str] | None = None) -> int:
    """Quick CLI:
        py resources/yf_daily_bars.py AAPL
        py resources/yf_daily_bars.py AAPL MSFT --lookback 60
    Prints the LAST 5 bars per symbol.
    """
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("symbols", nargs="+")
    p.add_argument("--lookback", type=int, default=400)
    args = p.parse_args(argv)
    result = fetch_daily_batch(args.symbols, lookback_days=args.lookback)
    for sym, bars in result.items():
        sys.stdout.write(f"\n{sym}: {len(bars)} bars\n")
        for b in bars[-5:]:
            sys.stdout.write(f"  {b['t']}  O={b['o']:.2f}  H={b['h']:.2f}  L={b['l']:.2f}  C={b['c']:.2f}  V={b['v']:.0f}\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
