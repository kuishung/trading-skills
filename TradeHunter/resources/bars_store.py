"""Bars store -- thin read/write API over data/price_history/.

Single canonical place where the bot reads and writes historical OHLCV
bars. Everything else (patterns.py, ticker_profile.py, future
review/backtest.py) talks to this module; the parquet storage detail
stays here.

Layout produced:

    data/price_history/1min/<SYM>.parquet     one file per symbol, full history
    data/price_history/5min/<SYM>.parquet
    data/price_history/15min/<SYM>.parquet
    data/price_history/daily/<SYM>.parquet

Bar shape (matches resources/patterns.py):

    {"t": "2026-05-21T13:30:00Z",  # ISO UTC
     "o": 18.20, "h": 18.45, "l": 18.15, "c": 18.42, "v": 412300}

Design choices:
 - Parquet via pyarrow. Lazy import -- read/write paths only. Modules
   that don't touch bars never pay the import cost.
 - One file per symbol per timeframe. Simple to reason about, simple
   to share/refresh ("AAPL.parquet IS AAPL's history"). The tradeoff
   is rewrite-on-append: a new bar means reading + concatenating +
   rewriting the file. At ~3-5 MB per symbol-year that's ~50ms per
   write, fine. Revisit if we ever ingest tick-level data.
 - Duplicate timestamps are merged (last write wins). Final list is
   always sorted by timestamp.

Public API:
    load_bars(symbol, start, end, timeframe="1min") -> list[dict]
    write_bars(symbol, bars, timeframe="1min")      -> Path
    available_range(symbol, timeframe="1min")       -> (first, last) | None
    list_symbols(timeframe="1min")                  -> list[str]
    bars_dir(timeframe="1min")                      -> Path

CLI:
    py resources/bars_store.py list 1min
    py resources/bars_store.py range NVDA 1min
    py resources/bars_store.py head NVDA 1min --n 5
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

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

# Data root resolves via scripts._common.get_data_root() so it honours
# cfg["data_root"] for per-PC external paths (e.g., D:\HermesSync\MarketData
# on laptop, C:\HermesSync\MarketData on Hermes). Falls back to
# SKILL_DIR/"data" if no override.
from scripts._common import get_data_root  # noqa: E402
PRICE_HISTORY_ROOT = get_data_root() / "price_history"
SUPPORTED_TIMEFRAMES = ("1min", "3min", "5min", "15min", "daily")


# ---------- Path helpers ----------

def bars_dir(timeframe: str = "1min") -> Path:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(
            f"timeframe must be one of {SUPPORTED_TIMEFRAMES}, got {timeframe!r}"
        )
    return PRICE_HISTORY_ROOT / timeframe


# Windows reserves these names as device aliases (legacy DOS console / printer
# devices). NTFS allows CREATING a file named CON.parquet etc., but Win32 APIs
# (incl. pyarrow's path resolver) refuse to OPEN them, raising WinError 87
# "The parameter is incorrect". CON is a real S&P-universe ticker, so we
# can't just skip it — encode reserved names with a leading underscore on
# disk and translate back when listing symbols. The undecorated name is what
# the rest of the bot uses (strategies, journals, etc.); only the on-disk
# filename gets the prefix.
_RESERVED_BASENAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def _safe_basename(symbol: str) -> str:
    """Return the on-disk basename for a symbol (no extension).
    Prepends '_' for Windows-reserved device names to make them openable."""
    s = symbol.upper()
    if s in _RESERVED_BASENAMES:
        return f"_{s}"
    return s


def _symbol_from_basename(basename: str) -> str:
    """Reverse of _safe_basename: strip the leading '_' if the rest is reserved."""
    if basename.startswith("_"):
        tail = basename[1:]
        if tail.upper() in _RESERVED_BASENAMES:
            return tail
    return basename


def _symbol_path(symbol: str, timeframe: str) -> Path:
    """data/price_history/<tf>/<SYM>.parquet (with reserved-name escaping)."""
    return bars_dir(timeframe) / f"{_safe_basename(symbol)}.parquet"


def list_symbols(timeframe: str = "1min") -> list[str]:
    """Return the symbols (logical ticker names, not on-disk basenames) that
    have a parquet file at the given timeframe. Reserved-name files like
    _CON.parquet are reported as 'CON'."""
    d = bars_dir(timeframe)
    if not d.exists():
        return []
    return sorted(_symbol_from_basename(p.stem) for p in d.glob("*.parquet"))


# ---------- pyarrow shim ----------

def _require_pyarrow():
    try:
        import pyarrow  # type: ignore  # noqa: F401
        import pyarrow.parquet  # type: ignore  # noqa: F401
        return pyarrow
    except ImportError as exc:
        raise ImportError(
            "bars_store requires pyarrow. Install it with:\n"
            "    py -m pip install pyarrow\n"
            "(per-PC install. Lazy-imported -- only modules that "
            "actually read/write bars need it.)"
        ) from exc


# ---------- Bar normalization ----------

def _parse_ts(t) -> datetime:
    if isinstance(t, datetime):
        return t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(float(t), tz=timezone.utc)
    if isinstance(t, str):
        s = t.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise TypeError(f"unsupported bar timestamp: {t!r} ({type(t).__name__})")


_ET_TZ = None


def bar_session_date_et(t) -> date:
    """The US market SESSION date (America/New_York) for a bar timestamp.

    Buckets pre-market (04:00 ET) through after-hours (20:00 ET) into the same
    calendar date. A NAIVE UTC-date split is wrong for backtesting: an after-
    hours bar at 20:00 ET is 00:00 UTC the *next* day, so UTC-date bucketing
    files it under tomorrow's session. Always use this for session bucketing.
    """
    global _ET_TZ
    if _ET_TZ is None:
        from zoneinfo import ZoneInfo
        _ET_TZ = ZoneInfo("America/New_York")
    return _parse_ts(t).astimezone(_ET_TZ).date()


def _normalize_bars(bars: Iterable[dict]) -> list[dict]:
    """Coerce each bar into the canonical shape; sort by ts; dedup (last wins)."""
    seen: dict[str, dict] = {}
    for b in bars:
        ts = _parse_ts(b["t"])
        key = ts.isoformat()
        seen[key] = {
            "t": key,
            "o": float(b["o"]),
            "h": float(b["h"]),
            "l": float(b["l"]),
            "c": float(b["c"]),
            "v": int(b["v"]),
        }
    return [seen[k] for k in sorted(seen.keys())]


# ---------- Write ----------

def write_bars(symbol: str, bars: Iterable[dict],
               *, timeframe: str = "1min",
               source: str | None = None) -> Path:
    """Persist bars to data/price_history/<tf>/<SYM>.parquet.

    Merges with any existing file (dedup by timestamp, last write wins),
    then rewrites the whole file. Returns the path written.

    Also appends an event to `data/ingest_log.jsonl` (best-effort, never
    raises) recording the write — used by `data_integrity` checks and the
    dashboard's data-health pill. Pass `source` to identify the caller
    (e.g. "yfinance.seed-sp500", "ibkr_history.update").
    """
    pa = _require_pyarrow()
    import pyarrow.parquet as pq  # type: ignore

    rows = _normalize_bars(bars)
    if not rows:
        _log_ingest({
            "source": source or "unknown",
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "bars_added": 0,
            "note": "empty_input",
        })
        return _symbol_path(symbol, timeframe)

    path = _symbol_path(symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)

    n_added = len(rows)
    if path.exists():
        existing = _read_parquet(path, pa, pq)
        before_n = len(existing)
        merged = _normalize_bars(existing + rows)
        n_added = len(merged) - before_n   # net-new after dedup
    else:
        merged = rows

    _write_parquet(path, merged, pa, pq)
    _log_ingest({
        "source": source or "unknown",
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "bars_added": int(n_added),
        "last_bar": merged[-1]["t"] if merged else None,
        "n_total": len(merged),
    })
    return path


# ---------- Ingest log ----------
#
# Append-only audit trail of every parquet write (and every resume-skip /
# failure reported by the ingest CLIs). One JSONL line per event. Read by
# `resources/data_integrity.py` for the dashboard's data-health pill.

INGEST_LOG_PATH = get_data_root() / "ingest_log.jsonl"


def _log_ingest(event: dict) -> None:
    """Best-effort append to `data/ingest_log.jsonl`. Never raises — a
    broken log must not block trading or ingest."""
    try:
        from datetime import datetime, timezone
        INGEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **event,
        }
        with INGEST_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        sys.stderr.write(f"[bars_store] ingest_log append failed: {exc}\n")


def log_ingest_event(*, source: str, symbol: str, timeframe: str,
                     bars_added: int = 0, error: str | None = None,
                     note: str | None = None, **extra) -> None:
    """Public hook for ingest CLIs to log resume-skips, errors, and other
    events where `write_bars()` isn't called. Same JSONL shape."""
    event: dict = {
        "source": source,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "bars_added": bars_added,
    }
    if error is not None:
        event["error"] = error
    if note is not None:
        event["note"] = note
    event.update(extra)
    _log_ingest(event)


def _write_parquet(path: Path, rows: list[dict], pa, pq) -> None:
    cols = {
        "t": [r["t"] for r in rows],
        "o": [r["o"] for r in rows],
        "h": [r["h"] for r in rows],
        "l": [r["l"] for r in rows],
        "c": [r["c"] for r in rows],
        "v": [r["v"] for r in rows],
    }
    table = pa.table(cols)
    pq.write_table(table, path)


def _read_parquet(path: Path, pa, pq) -> list[dict]:
    if not path.exists():
        return []
    table = pq.read_table(path)
    cols = {name: table.column(name).to_pylist() for name in table.column_names}
    n = len(cols["t"])
    return [{k: cols[k][i] for k in cols} for i in range(n)]


# ---------- Read ----------

def load_bars(symbol: str,
              start: str | date | datetime | None = None,
              end: str | date | datetime | None = None,
              *, timeframe: str = "1min") -> list[dict]:
    """Load bars for `symbol` between `start` and `end` (inclusive, UTC).

    `start` and `end` can be ISO strings, date objects, or datetimes.
    `None` means unbounded on that side.
    Returns a sorted list of bar dicts in the canonical shape.
    """
    pa = _require_pyarrow()
    import pyarrow.parquet as pq  # type: ignore

    path = _symbol_path(symbol, timeframe)
    if not path.exists():
        return []
    rows = _read_parquet(path, pa, pq)
    if not rows:
        return []
    rows.sort(key=lambda r: r["t"])

    start_iso = _coerce_iso(start)
    end_iso = _coerce_iso(end)
    if start_iso:
        rows = [r for r in rows if r["t"] >= start_iso]
    if end_iso:
        rows = [r for r in rows if r["t"] <= end_iso]
    return rows


def _coerce_iso(x) -> str | None:
    if x is None:
        return None
    if isinstance(x, str):
        # accept "2026-05-21" or "2026-05-21T13:30:00Z"
        return x if "T" in x else x + "T00:00:00+00:00"
    if isinstance(x, datetime):
        return _parse_ts(x).isoformat()
    if isinstance(x, date):
        return f"{x.isoformat()}T00:00:00+00:00"
    raise TypeError(f"unsupported start/end: {x!r}")


def available_range_fast(symbol: str,
                         *, timeframe: str = "1min") -> tuple[str, str] | None:
    """Fast metadata-only version of `available_range`.

    Reads the parquet file's per-row-group statistics for the `t` column
    instead of materializing all rows. On a 730-row daily parquet this is
    ~1ms vs `available_range`'s ~50ms; on a 70k-row 3min parquet the gap
    widens to ~1ms vs several hundred ms — especially over Resilio-synced
    storage. Used by the bulk_update pre-flight scan (4554 calls per
    watcher iteration), where the round-trip difference is minutes.

    Returns `(min_t, max_t)` derived from row-group statistics, or None if
    the file doesn't exist, is empty, or doesn't carry the expected
    statistics. Falls back to None — caller treats that as "unreadable"
    and re-fetches.

    All exceptions (corrupted parquet, partial sync from Resilio, schema
    mismatch) are caught and surfaced as None, so a single bad file
    doesn't crash the calling pre-flight loop.
    """
    pa = _require_pyarrow()
    import pyarrow.parquet as pq  # type: ignore
    path = _symbol_path(symbol, timeframe)
    if not path.exists():
        return None
    try:
        meta = pq.ParquetFile(path).metadata
        if meta is None or meta.num_rows == 0 or meta.num_row_groups == 0:
            return None
        # Find the `t` column's position in the schema. ParquetSchema's
        # public API is `.names` (a list of column-path strings) — there
        # is no num_columns or column(i).name accessor on the schema
        # object itself. (Row-group columns have .path_in_schema for the
        # name, but we need the index into the row group's column list,
        # which matches the schema's positional ordering.)
        try:
            col_idx = meta.schema.names.index("t")
        except ValueError:
            return None
        # Collect per-row-group min/max for the `t` column; take the
        # overall min and max. ISO timestamp strings sort lexicographically
        # so string min/max is correct.
        mins: list[str] = []
        maxs: list[str] = []
        for g in range(meta.num_row_groups):
            stats = meta.row_group(g).column(col_idx).statistics
            if stats is None or not stats.has_min_max:
                return None
            mins.append(str(stats.min))
            maxs.append(str(stats.max))
        if not mins:
            return None
        return (min(mins), max(maxs))
    except Exception:
        # Corrupted parquet, partial Resilio sync, schema drift — let the
        # caller treat this as "unreadable" and decide what to do.
        return None


def available_range(symbol: str,
                    *, timeframe: str = "1min") -> tuple[str, str] | None:
    """Return (first_ts, last_ts) ISO strings, or None if no bars stored."""
    rows = load_bars(symbol, timeframe=timeframe)
    if not rows:
        return None
    return (rows[0]["t"], rows[-1]["t"])


# ---------- CLI ----------

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="bars_store -- read/write OHLCV bars")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub_list = sub.add_parser("list", help="list symbols in a timeframe")
    sub_list.add_argument("timeframe", nargs="?", default="1min",
                          choices=SUPPORTED_TIMEFRAMES)

    sub_range = sub.add_parser("range", help="show available date range for a symbol")
    sub_range.add_argument("symbol")
    sub_range.add_argument("timeframe", nargs="?", default="1min",
                           choices=SUPPORTED_TIMEFRAMES)

    sub_head = sub.add_parser("head", help="print the first N bars")
    sub_head.add_argument("symbol")
    sub_head.add_argument("timeframe", nargs="?", default="1min",
                          choices=SUPPORTED_TIMEFRAMES)
    sub_head.add_argument("--n", type=int, default=5)

    args = ap.parse_args()

    if args.cmd == "list":
        syms = list_symbols(args.timeframe)
        if not syms:
            print(f"(no symbols in {args.timeframe})")
            return 0
        for s in syms:
            print(s)
        return 0

    if args.cmd == "range":
        rng = available_range(args.symbol, timeframe=args.timeframe)
        if rng is None:
            print(f"(no bars for {args.symbol} / {args.timeframe})")
            return 1
        print(f"{args.symbol} {args.timeframe}: {rng[0]} -> {rng[1]}")
        return 0

    if args.cmd == "head":
        bars = load_bars(args.symbol, timeframe=args.timeframe)[: args.n]
        for b in bars:
            print(json.dumps(b))
        if not bars:
            print(f"(no bars for {args.symbol} / {args.timeframe})")
            return 1
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main() or 0)
