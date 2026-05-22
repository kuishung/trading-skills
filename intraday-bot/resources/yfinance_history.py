"""yfinance -> Parquet bulk ingest pipeline.

Companion to `ibkr_history.py`. Use yfinance for the INITIAL bulk seed
(S&P 500 daily history in ~5 min, no pacing) and switch to IBKR for
the ongoing daily/intraday updates (which the user pays for and which
is the live data source anyway).

Why two providers:
- yfinance is bulk-friendly and free. Daily bars for 500 symbols in
  one HTTP batch (or a handful of batches). 1-min capped at 7 days
  per symbol.
- IBKR has the freshest, cleanest data but pacing-limits to ~60
  historical requests per 600 seconds. Bulk-seeding 500 symbols would
  take ~9 hours.

Both write through `bars_store.write_bars()` so downstream
(`patterns.py`, `ticker_profile.py`, `review/backtest.py`) doesn't
care which provider sourced the bars. Dedup-on-write makes repeated
calls idempotent.

Public API:
    ingest_daily(symbols, years=2)      -> dict[symbol, n_bars]
    ingest_intraday(symbols, days=7)    -> dict[symbol, n_bars]  (1-min)
    seed_sp500(timeframes=("daily",), ...) -> dict

CLI:
    py resources/yfinance_history.py seed-sp500                      # daily, 2y
    py resources/yfinance_history.py seed-sp500 --include-1min       # +7d 1-min
    py resources/yfinance_history.py seed-sp500 --years 5            # 5y daily
    py resources/yfinance_history.py ingest NVDA MSFT --tf daily --years 2
    py resources/yfinance_history.py ingest NVDA --tf 1min --days 7

Behavior:
- Skips symbols already present in bars_store for that timeframe if
  --resume is set. Use --force to re-fetch.
- Batches yfinance.download() into groups of 50 symbols to avoid
  Yahoo's "too many tickers in one call" failures.
- Per-batch retry on transient HTTP failures (3 attempts, exponential
  backoff).
- Coerces Yahoo's tz-naive timestamps to UTC for the bars_store shape.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
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

import bars_store           # noqa: E402

DEFAULT_BATCH_SIZE = 50      # symbols per yfinance.download() call
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_S = 5.0

# yfinance interval mapping + Yahoo's per-interval lookback cap (in days).
# Intraday intervals include pre-market + after-hours when prepost=True.
TIMEFRAME_TO_YF = {
    "1min":  {"interval": "1m",  "max_days": 7,    "prepost": True},
    "5min":  {"interval": "5m",  "max_days": 60,   "prepost": True},
    "15min": {"interval": "15m", "max_days": 60,   "prepost": True},
    "daily": {"interval": "1d",  "max_days": None, "prepost": False},
}


def _to_yahoo_symbol(sym: str) -> str:
    """Convert canonical symbol -> Yahoo's format.
    Yahoo uses '-' for share classes (BRK-B), most data vendors use '.' (BRK.B)."""
    return sym.replace(".", "-")


def _from_yahoo_symbol(sym: str) -> str:
    """Inverse of _to_yahoo_symbol -- store with canonical dot form."""
    return sym.replace("-", ".")


# ---------- yfinance shim ----------

def _require_yfinance():
    try:
        import yfinance  # type: ignore
        return yfinance
    except ImportError as exc:
        raise ImportError(
            "yfinance_history requires yfinance. Install it:\n"
            "    py -m pip install yfinance\n"
        ) from exc


# ---------- Bar coercion ----------

def _coerce_yf_index_to_utc_iso(idx) -> str | None:
    """yfinance returns either tz-aware datetimes (intraday) or tz-naive
    midnight stamps (daily). Coerce to ISO-UTC string."""
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        return None
    if idx is None:
        return None
    try:
        # pandas.Timestamp
        if hasattr(idx, "tz_localize"):
            if idx.tz is None:
                # daily bars from yfinance are tz-naive; treat as UTC noon-ish?
                # NO -- they represent ET market close. But for storage we just
                # want a stable date key. Use the date with T00:00:00Z.
                return idx.strftime("%Y-%m-%dT00:00:00+00:00")
            return idx.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except Exception:
        return None
    return None


def _df_to_bars(df, symbol: str) -> list[dict]:
    """Convert a yfinance per-symbol DataFrame to bars_store dict list."""
    if df is None or df.empty:
        return []
    rows: list[dict] = []
    for idx, row in df.iterrows():
        t_iso = _coerce_yf_index_to_utc_iso(idx)
        if not t_iso:
            continue
        try:
            o = float(row["Open"])
            h = float(row["High"])
            l_ = float(row["Low"])
            c = float(row["Close"])
            v = int(row["Volume"]) if not _isnan(row["Volume"]) else 0
        except (KeyError, ValueError, TypeError):
            continue
        # Skip rows with all-NaN OHLCV (yfinance pads holidays sometimes)
        if _isnan(o) or _isnan(h) or _isnan(l_) or _isnan(c):
            continue
        rows.append({"t": t_iso, "o": o, "h": h, "l": l_, "c": c, "v": v})
    return rows


def _isnan(x) -> bool:
    try:
        return x != x  # NaN != NaN
    except Exception:
        return False


# ---------- Bulk fetch ----------

def _download_batch(yf, symbols: list[str], period: str, interval: str,
                    *, prepost: bool = False,
                    retries: int = DEFAULT_RETRIES,
                    backoff_s: float = DEFAULT_BACKOFF_S):
    """Wrapper around yfinance.download with retry. Returns the
    multi-index DataFrame or None on persistent failure."""
    last_exc = None
    for attempt in range(retries):
        try:
            df = yf.download(
                tickers=" ".join(symbols),
                period=period,
                interval=interval,
                prepost=prepost,
                group_by="ticker",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=True,
            )
            return df
        except Exception as exc:
            last_exc = exc
            sys.stderr.write(
                f"[yfinance] batch fetch attempt {attempt + 1}/{retries} "
                f"failed: {exc}; retrying in {backoff_s:.0f}s...\n"
            )
            time.sleep(backoff_s)
            backoff_s *= 2
    sys.stderr.write(f"[yfinance] batch fetch gave up after {retries} attempts: {last_exc}\n")
    return None


def _slice_per_symbol(df, symbols: list[str]):
    """Yield (symbol, sub_df) from yfinance multi-index DataFrame.

    yfinance with group_by='ticker' returns columns as (ticker, field)
    MultiIndex even for a single symbol. We slice df[sym] which strips
    the ticker level, leaving flat ['Open','High','Low','Close','Volume'].
    """
    if df is None or df.empty:
        return
    has_multi = hasattr(df.columns, "levels")
    available_tickers = (set(df.columns.get_level_values(0))
                         if has_multi else set())
    for sym in symbols:
        if has_multi and sym in available_tickers:
            sub = df[sym].dropna(how="all")
            if not sub.empty:
                yield sym, sub
        elif not has_multi and len(symbols) == 1:
            # Fallback: yfinance occasionally returns flat columns for
            # a single ticker. Treat the whole df as that symbol's data.
            yield sym, df.dropna(how="all")


# ---------- Public API ----------

def ingest_daily(symbols: list[str], *, years: int = 2,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 resume: bool = False,
                 source: str = "yfinance") -> dict[str, int]:
    """Bulk-fetch `years` of daily bars for all symbols and write to bars_store.

    Returns: {symbol: bars_written}. Symbols absent from yfinance return 0.
    """
    yf = _require_yfinance()
    spec = TIMEFRAME_TO_YF["daily"]
    period = f"{int(years)}y"
    return _ingest(yf, symbols, period=period, interval=spec["interval"],
                   timeframe="daily", prepost=spec["prepost"],
                   batch_size=batch_size, resume=resume, source=source)


def ingest_intraday(symbols: list[str], *,
                    timeframe: str = "1min",
                    days: int | None = None,
                    batch_size: int = DEFAULT_BATCH_SIZE,
                    resume: bool = False,
                    source: str = "yfinance") -> dict[str, int]:
    """Bulk-fetch `days` of intraday bars (1min / 5min / 15min) and write
    to bars_store. Yahoo caps history per interval:
        1min  -> 7 days
        5min  -> 60 days
        15min -> 60 days

    `days=None` requests the maximum allowed for the timeframe. Intraday
    pulls include pre-market + after-hours (prepost=True) so GUNS-style
    strategies that key off pre-market action have the data they need;
    callers can filter at query time.
    """
    if timeframe not in TIMEFRAME_TO_YF:
        raise ValueError(f"unknown timeframe {timeframe!r}; "
                         f"choose from {list(TIMEFRAME_TO_YF)}")
    if timeframe == "daily":
        raise ValueError("use ingest_daily() for daily bars")
    spec = TIMEFRAME_TO_YF[timeframe]
    max_d = spec["max_days"]
    if days is None:
        days = max_d
    elif max_d and days > max_d:
        sys.stderr.write(f"[yfinance] note: {timeframe} cap is {max_d} days; "
                         f"requested {days} will be silently truncated.\n")
        days = max_d
    yf = _require_yfinance()
    period = f"{int(days)}d"
    return _ingest(yf, symbols, period=period, interval=spec["interval"],
                   timeframe=timeframe, prepost=spec["prepost"],
                   batch_size=batch_size, resume=resume, source=source)


def _ingest(yf, symbols: list[str], *, period: str, interval: str,
            timeframe: str, prepost: bool, batch_size: int,
            resume: bool, source: str = "yfinance") -> dict[str, int]:
    if not symbols:
        return {}
    if resume:
        already = set(bars_store.list_symbols(timeframe))
        skipped = [s for s in symbols if s in already]
        symbols = [s for s in symbols if s not in already]
        if skipped:
            sys.stderr.write(f"[yfinance] resume: skipping {len(skipped)} "
                             f"symbols already in {timeframe}\n")
            # Audit-trail resume-skips so the dashboard's stale-data
            # detection can explain why coverage is uneven.
            for s in skipped:
                bars_store.log_ingest_event(source=source, symbol=s,
                                            timeframe=timeframe, bars_added=0,
                                            note="resume_skipped")
    results: dict[str, int] = {}
    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    total_batches = len(batches)
    t0 = time.time()
    for b_i, batch in enumerate(batches, 1):
        # Map canonical symbols (BRK.B) -> Yahoo form (BRK-B) for the request.
        yahoo_batch = [_to_yahoo_symbol(s) for s in batch]
        yahoo_to_canonical = dict(zip(yahoo_batch, batch))
        sys.stderr.write(f"[yfinance] batch {b_i}/{total_batches} "
                         f"({len(batch)} syms, {timeframe})...\n")
        sys.stderr.flush()
        df = _download_batch(yf, yahoo_batch, period=period,
                             interval=interval, prepost=prepost)
        if df is None:
            for s in batch:
                results[s] = 0
            continue
        for yahoo_sym, sub in _slice_per_symbol(df, yahoo_batch):
            canonical = yahoo_to_canonical.get(yahoo_sym, yahoo_sym)
            bars = _df_to_bars(sub, canonical)
            if not bars:
                results[canonical] = 0
                continue
            bars_store.write_bars(canonical, bars, timeframe=timeframe, source=source)
            results[canonical] = len(bars)
    elapsed = time.time() - t0
    n_with_data = sum(1 for v in results.values() if v > 0)
    total_bars = sum(results.values())
    sys.stderr.write(
        f"[yfinance] {timeframe} ingest complete: "
        f"{n_with_data}/{len(results)} symbols ok, {total_bars} bars total, "
        f"{elapsed:.1f}s elapsed.\n"
    )
    return results


def _seed_universe(symbols: list[str], label: str, *,
                   years_daily: int,
                   include_1min: bool, include_5min: bool, include_15min: bool,
                   intraday_days_1min: int, intraday_days_5min: int,
                   intraday_days_15min: int, resume: bool) -> dict:
    """Shared engine: ingest daily + optionally intraday for a given symbol
    list. Used by seed_sp500() and seed_sp400() (and any future index)."""
    sys.stderr.write(f"[{label}] {len(symbols)} symbols\n")
    source = f"yfinance.{label}"
    out: dict = {"label": label, "n_symbols": len(symbols)}
    daily_results = ingest_daily(symbols, years=years_daily, resume=resume, source=source)
    out["daily"] = {
        "symbols_with_data": sum(1 for v in daily_results.values() if v > 0),
        "total_bars": sum(daily_results.values()),
    }
    if include_15min:
        r = ingest_intraday(symbols, timeframe="15min",
                            days=intraday_days_15min, resume=resume, source=source)
        out["15min"] = {"symbols_with_data": sum(1 for v in r.values() if v > 0),
                        "total_bars": sum(r.values())}
    if include_5min:
        r = ingest_intraday(symbols, timeframe="5min",
                            days=intraday_days_5min, resume=resume, source=source)
        out["5min"] = {"symbols_with_data": sum(1 for v in r.values() if v > 0),
                       "total_bars": sum(r.values())}
    if include_1min:
        r = ingest_intraday(symbols, timeframe="1min",
                            days=intraday_days_1min, resume=resume, source=source)
        out["1min"] = {"symbols_with_data": sum(1 for v in r.values() if v > 0),
                       "total_bars": sum(r.values())}
    return out


def seed_sp400(*, years_daily: int = 2,
               include_1min: bool = False,
               include_5min: bool = False,
               include_15min: bool = False,
               intraday_days_1min: int = 7,
               intraday_days_5min: int = 60,
               intraday_days_15min: int = 60,
               resume: bool = True,
               force_refresh_list: bool = False) -> dict:
    """Seed the S&P MidCap 400 (mid-cap index). ~400 symbols, ~12 MB for
    daily 2y. Same shape + thresholds as seed_sp500()."""
    from sp_midcap400 import get_sp400_symbols
    symbols = get_sp400_symbols(force_refresh=force_refresh_list)
    return _seed_universe(symbols, "seed-midcap400",
                          years_daily=years_daily,
                          include_1min=include_1min, include_5min=include_5min,
                          include_15min=include_15min,
                          intraday_days_1min=intraday_days_1min,
                          intraday_days_5min=intraday_days_5min,
                          intraday_days_15min=intraday_days_15min,
                          resume=resume)


def seed_sp600(*, years_daily: int = 2, resume: bool = True,
               force_refresh_list: bool = False) -> dict:
    """Seed the S&P SmallCap 600. ~600 symbols, ~15 MB daily 2y."""
    from sp_smallcap600 import get_sp600_symbols
    symbols = get_sp600_symbols(force_refresh=force_refresh_list)
    return _seed_universe(symbols, "seed-smallcap600",
                          years_daily=years_daily,
                          include_1min=False, include_5min=False, include_15min=False,
                          intraday_days_1min=7, intraday_days_5min=60,
                          intraday_days_15min=60, resume=resume)


def seed_nasdaq100(*, years_daily: int = 2, resume: bool = True,
                   force_refresh_list: bool = False) -> dict:
    """Seed the NASDAQ-100. ~100 symbols (heavy overlap with S&P 500)."""
    from nasdaq100 import get_nasdaq100_symbols
    symbols = get_nasdaq100_symbols(force_refresh=force_refresh_list)
    return _seed_universe(symbols, "seed-nasdaq100",
                          years_daily=years_daily,
                          include_1min=False, include_5min=False, include_15min=False,
                          intraday_days_1min=7, intraday_days_5min=60,
                          intraday_days_15min=60, resume=resume)


def seed_djia(*, years_daily: int = 2, resume: bool = True,
              force_refresh_list: bool = False) -> dict:
    """Seed the Dow Jones Industrial Average. 30 symbols (all in S&P 500)."""
    from djia import get_djia_symbols
    symbols = get_djia_symbols(force_refresh=force_refresh_list)
    return _seed_universe(symbols, "seed-djia",
                          years_daily=years_daily,
                          include_1min=False, include_5min=False, include_15min=False,
                          intraday_days_1min=7, intraday_days_5min=60,
                          intraday_days_15min=60, resume=resume)


def seed_sp500(*, years_daily: int = 2,
               include_1min: bool = False,
               include_5min: bool = False,
               include_15min: bool = False,
               intraday_days_1min: int = 7,
               intraday_days_5min: int = 60,
               intraday_days_15min: int = 60,
               resume: bool = True,
               force_refresh_list: bool = False) -> dict:
    """One-shot seed: fetch S&P 500 list, ingest daily + optionally intraday.

    Defaults (~93s on a real run):
      - 2 years of daily for all ~500 symbols.

    Optional intraday seeds:
      - include_1min=True   ->  7 days of 1-min   (~1-2 min)
      - include_5min=True   -> 60 days of 5-min   (~2-3 min)
      - include_15min=True  -> 60 days of 15-min  (~1-2 min)

    All intraday pulls include pre-market + after-hours.
    """
    from sp500 import get_sp500_symbols
    symbols = get_sp500_symbols(force_refresh=force_refresh_list)
    sys.stderr.write(f"[seed-sp500] {len(symbols)} symbols\n")

    out: dict = {"n_symbols": len(symbols)}
    daily_results = ingest_daily(symbols, years=years_daily, resume=resume)
    out["daily"] = {
        "symbols_with_data": sum(1 for v in daily_results.values() if v > 0),
        "total_bars": sum(daily_results.values()),
    }
    if include_15min:
        r = ingest_intraday(symbols, timeframe="15min",
                            days=intraday_days_15min, resume=resume)
        out["15min"] = {
            "symbols_with_data": sum(1 for v in r.values() if v > 0),
            "total_bars": sum(r.values()),
        }
    if include_5min:
        r = ingest_intraday(symbols, timeframe="5min",
                            days=intraday_days_5min, resume=resume)
        out["5min"] = {
            "symbols_with_data": sum(1 for v in r.values() if v > 0),
            "total_bars": sum(r.values()),
        }
    if include_1min:
        r = ingest_intraday(symbols, timeframe="1min",
                            days=intraday_days_1min, resume=resume)
        out["1min"] = {
            "symbols_with_data": sum(1 for v in r.values() if v > 0),
            "total_bars": sum(r.values()),
        }
    return out


# ---------- CLI ----------

def _cmd_seed_sp500(args) -> int:
    include_1min = args.include_1min or args.include_all_intraday
    include_5min = args.include_5min or args.include_all_intraday
    include_15min = args.include_15min or args.include_all_intraday
    summary = seed_sp500(
        years_daily=args.years,
        include_1min=include_1min,
        include_5min=include_5min,
        include_15min=include_15min,
        intraday_days_1min=args.intraday_days_1min,
        intraday_days_5min=args.intraday_days_5min,
        intraday_days_15min=args.intraday_days_15min,
        resume=not args.force,
        force_refresh_list=args.force_refresh_list,
    )
    import json
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_seed_sp400(args) -> int:
    include_1min = args.include_1min or args.include_all_intraday
    include_5min = args.include_5min or args.include_all_intraday
    include_15min = args.include_15min or args.include_all_intraday
    summary = seed_sp400(
        years_daily=args.years,
        include_1min=include_1min,
        include_5min=include_5min,
        include_15min=include_15min,
        intraday_days_1min=args.intraday_days_1min,
        intraday_days_5min=args.intraday_days_5min,
        intraday_days_15min=args.intraday_days_15min,
        resume=not args.force,
        force_refresh_list=args.force_refresh_list,
    )
    import json
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_seed_sp600(args) -> int:
    summary = seed_sp600(years_daily=args.years, resume=not args.force,
                         force_refresh_list=args.force_refresh_list)
    import json
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_seed_nasdaq100(args) -> int:
    summary = seed_nasdaq100(years_daily=args.years, resume=not args.force,
                             force_refresh_list=args.force_refresh_list)
    import json
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_seed_djia(args) -> int:
    summary = seed_djia(years_daily=args.years, resume=not args.force,
                        force_refresh_list=args.force_refresh_list)
    import json
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_ingest(args) -> int:
    symbols = [s.upper() for s in args.symbols]
    if args.tf == "daily":
        results = ingest_daily(symbols, years=args.years, resume=not args.force,
                               source="yfinance.ingest")
    elif args.tf in ("1min", "5min", "15min"):
        results = ingest_intraday(symbols, timeframe=args.tf,
                                  days=args.days, resume=not args.force,
                                  source="yfinance.ingest")
    else:
        raise SystemExit(f"yfinance supports daily/1min/5min/15min; got {args.tf!r}")
    n_ok = sum(1 for v in results.values() if v > 0)
    total = sum(results.values())
    print(f"{n_ok}/{len(results)} symbols ok, {total} bars written")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser("seed-sp500", help="bulk-seed the entire S&P 500")
    p_seed.add_argument("--years", type=int, default=2,
                        help="years of daily history (default 2)")
    p_seed.add_argument("--include-1min", action="store_true",
                        help="also pull 7 days of 1-min bars (~1-2 min extra)")
    p_seed.add_argument("--include-5min", action="store_true",
                        help="also pull 60 days of 5-min bars (~2-3 min extra)")
    p_seed.add_argument("--include-15min", action="store_true",
                        help="also pull 60 days of 15-min bars (~1-2 min extra)")
    p_seed.add_argument("--include-all-intraday", action="store_true",
                        help="shortcut: enable 1min + 5min + 15min")
    p_seed.add_argument("--intraday-days-1min", type=int, default=7,
                        help="days of 1-min history (Yahoo cap = 7)")
    p_seed.add_argument("--intraday-days-5min", type=int, default=60,
                        help="days of 5-min history (Yahoo cap = 60)")
    p_seed.add_argument("--intraday-days-15min", type=int, default=60,
                        help="days of 15-min history (Yahoo cap = 60)")
    p_seed.add_argument("--force", action="store_true",
                        help="re-fetch symbols even if already in bars_store")
    p_seed.add_argument("--force-refresh-list", action="store_true",
                        help="ignore the S&P 500 list cache; re-pull from Wikipedia")
    p_seed.set_defaults(func=_cmd_seed_sp500)

    p_seed_mid = sub.add_parser("seed-midcap400",
                                 help="bulk-seed the S&P MidCap 400 (~400 mid-cap symbols)")
    p_seed_mid.add_argument("--years", type=int, default=2)
    p_seed_mid.add_argument("--include-1min", action="store_true")
    p_seed_mid.add_argument("--include-5min", action="store_true")
    p_seed_mid.add_argument("--include-15min", action="store_true")
    p_seed_mid.add_argument("--include-all-intraday", action="store_true")
    p_seed_mid.add_argument("--intraday-days-1min", type=int, default=7)
    p_seed_mid.add_argument("--intraday-days-5min", type=int, default=60)
    p_seed_mid.add_argument("--intraday-days-15min", type=int, default=60)
    p_seed_mid.add_argument("--force", action="store_true")
    p_seed_mid.add_argument("--force-refresh-list", action="store_true")
    p_seed_mid.set_defaults(func=_cmd_seed_sp400)

    p_seed_small = sub.add_parser("seed-smallcap600",
                                   help="bulk-seed the S&P SmallCap 600 (~600 small-cap symbols)")
    p_seed_small.add_argument("--years", type=int, default=2)
    p_seed_small.add_argument("--force", action="store_true")
    p_seed_small.add_argument("--force-refresh-list", action="store_true")
    p_seed_small.set_defaults(func=_cmd_seed_sp600)

    p_seed_ndx = sub.add_parser("seed-nasdaq100",
                                 help="bulk-seed the NASDAQ-100 (~100 symbols; heavy S&P 500 overlap)")
    p_seed_ndx.add_argument("--years", type=int, default=2)
    p_seed_ndx.add_argument("--force", action="store_true")
    p_seed_ndx.add_argument("--force-refresh-list", action="store_true")
    p_seed_ndx.set_defaults(func=_cmd_seed_nasdaq100)

    p_seed_dj = sub.add_parser("seed-djia",
                                help="bulk-seed the Dow Jones Industrial Average (30 symbols; all in S&P 500)")
    p_seed_dj.add_argument("--years", type=int, default=2)
    p_seed_dj.add_argument("--force", action="store_true")
    p_seed_dj.add_argument("--force-refresh-list", action="store_true")
    p_seed_dj.set_defaults(func=_cmd_seed_djia)

    p_ing = sub.add_parser("ingest", help="fetch specific symbols")
    p_ing.add_argument("symbols", nargs="+")
    p_ing.add_argument("--tf", default="daily",
                       choices=("daily", "1min", "5min", "15min"))
    p_ing.add_argument("--years", type=int, default=2,
                       help="years (daily only)")
    p_ing.add_argument("--days", type=int, default=None,
                       help="days (intraday only; defaults to Yahoo's cap)")
    p_ing.add_argument("--force", action="store_true")
    p_ing.set_defaults(func=_cmd_ingest)

    return ap


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
