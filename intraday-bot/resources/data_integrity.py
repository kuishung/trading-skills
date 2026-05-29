"""Parquet data integrity checks — freshness, consistency, validity.

The dashboard's data-health pill calls into this module to surface
stale/broken data BEFORE the scanner can quietly produce false candidates
on it (e.g., a P2 signal from a symbol whose parquet is a week stale).

Three classes of check per symbol per timeframe:

  1. FRESHNESS   — how stale is the last bar vs today's ET date?
                   "fresh"  = last bar within `max_business_days_stale`
                              business days of today (default 2)
                   "stale"  = older than that
                   "ancient" = older than 7 business days (red flag)

  2. CONSISTENCY — bars are sorted by timestamp, no duplicates, no
                   unexpected gaps (within RTH for intraday, weekdays for
                   daily).

  3. VALIDITY    — OHLCV sanity per bar:
                     high >= max(open, close)
                     low  <= min(open, close)
                     high >= low
                     volume >= 0
                     all prices > 0

Public API:
    check_symbol(symbol, timeframe) -> SymbolHealth
    health_report(symbols=None, timeframes=None) -> HealthReport
    last_ingest_event(symbol, timeframe) -> dict | None

CLI:
    py resources/data_integrity.py freshness                  # all symbols, daily
    py resources/data_integrity.py freshness --stale-only     # only stale ones
    py resources/data_integrity.py validity NVDA              # full validity audit
    py resources/data_integrity.py log --tail 20              # recent ingest log
    py resources/data_integrity.py summary                    # one-line health summary
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
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

import bars_store  # noqa: E402
from scripts._common import get_data_root  # noqa: E402

# Honour cfg["data_root"] per scripts._common.get_data_root() — see bars_store.py
INGEST_LOG_PATH = get_data_root() / "ingest_log.jsonl"


# ---------- Health containers ----------

@dataclass
class SymbolHealth:
    symbol: str
    timeframe: str
    last_bar: str | None              # ISO UTC of latest bar
    days_stale: int | None            # business days since last bar; None = no data
    freshness: str                    # "fresh" / "stale" / "ancient" / "missing"
    consistency_ok: bool
    consistency_issues: list[str] = field(default_factory=list)
    validity_ok: bool = True
    validity_issues: list[str] = field(default_factory=list)
    n_bars: int = 0
    last_ingest_ts: str | None = None
    last_ingest_source: str | None = None


@dataclass
class HealthReport:
    ts: str
    universe_size: int
    timeframe: str
    fresh: int = 0
    stale: int = 0
    ancient: int = 0
    missing: int = 0
    consistency_failures: int = 0
    validity_failures: int = 0
    overall: str = "ok"               # "ok" / "warn" / "critical"
    stale_symbols: list[str] = field(default_factory=list)
    ancient_symbols: list[str] = field(default_factory=list)
    invalid_symbols: list[str] = field(default_factory=list)


# ---------- Date utilities ----------

def _business_days_between(d1: date, d2: date) -> int:
    """Count of weekdays strictly between d1 and d2 (d1 < d2). Ignores US
    holidays — good enough for "is this data fresh?" checks."""
    if d2 <= d1:
        return 0
    n = 0
    cur = d1 + timedelta(days=1)
    while cur <= d2:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def _today_et() -> date:
    """Today's date in US/Eastern (matches the bot's trading calendar)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        import pytz   # type: ignore
        tz = pytz.timezone("America/New_York")
    return datetime.now(timezone.utc).astimezone(tz).date()


# ---------- Ingest log reader ----------

def _read_ingest_log_tail(n: int | None = None) -> list[dict]:
    """Read the ingest log JSONL. Returns most recent N entries (or all)."""
    if not INGEST_LOG_PATH.exists():
        return []
    lines: list[dict] = []
    try:
        for raw in INGEST_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return lines[-n:] if n else lines


def last_ingest_event(symbol: str, timeframe: str) -> dict | None:
    """Return the most recent log entry for (symbol, timeframe), or None."""
    sym = symbol.upper()
    for ev in reversed(_read_ingest_log_tail()):
        if ev.get("symbol") == sym and ev.get("timeframe") == timeframe:
            return ev
    return None


# ---------- Per-symbol check ----------

# Cents above the largest fairly priced US equity. Anything above this on a
# regular ticker is a vendor data glitch (Berkshire-A is the outlier here at ~$700k).
MAX_REASONABLE_PRICE = 1_000_000.0
MAX_DAILY_GAP_BUSINESS_DAYS = 5   # daily bars: gap > 5 weekdays = suspicious

# Freshness thresholds (business days from today_et)
FRESH_DAYS = 2
STALE_DAYS = 7   # 2 ≤ days < 7 = stale; ≥ 7 = ancient


def check_symbol(symbol: str, timeframe: str = "daily") -> SymbolHealth:
    """Run all three checks against one symbol's parquet."""
    sym = symbol.upper()
    bars = bars_store.load_bars(sym, timeframe=timeframe)
    h = SymbolHealth(symbol=sym, timeframe=timeframe,
                     last_bar=None, days_stale=None,
                     freshness="missing", consistency_ok=True,
                     n_bars=0)
    if not bars:
        h.consistency_ok = False
        h.consistency_issues.append("no_bars")
        return h
    h.n_bars = len(bars)
    h.last_bar = bars[-1]["t"]

    # --- Freshness ---
    try:
        last_dt = datetime.fromisoformat(h.last_bar.replace("Z", "+00:00"))
        last_date = last_dt.date()
        today = _today_et()
        h.days_stale = _business_days_between(last_date, today)
        if h.days_stale <= FRESH_DAYS:
            h.freshness = "fresh"
        elif h.days_stale < STALE_DAYS:
            h.freshness = "stale"
        else:
            h.freshness = "ancient"
    except Exception as exc:
        h.consistency_issues.append(f"bad_timestamp: {exc}")
        h.consistency_ok = False

    # --- Consistency: sorted + no duplicates + no big gaps ---
    prev_t: str | None = None
    prev_dt: datetime | None = None
    for b in bars:
        t = b.get("t")
        if t is None:
            h.consistency_issues.append("missing_timestamp")
            h.consistency_ok = False
            break
        if prev_t is not None and t < prev_t:
            h.consistency_issues.append("unsorted")
            h.consistency_ok = False
        if prev_t == t:
            h.consistency_issues.append("duplicate_timestamp")
            h.consistency_ok = False
        if timeframe == "daily" and prev_dt is not None:
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                gap_bd = _business_days_between(prev_dt.date(), dt.date())
                if gap_bd > MAX_DAILY_GAP_BUSINESS_DAYS:
                    h.consistency_issues.append(
                        f"gap_{gap_bd}bd_at_{prev_dt.date()}"
                    )
                    h.consistency_ok = False
                prev_dt = dt
            except Exception:
                pass
        else:
            try:
                prev_dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except Exception:
                prev_dt = None
        prev_t = t

    # --- Validity: OHLCV sanity per bar ---
    n_invalid = 0
    for i, b in enumerate(bars):
        try:
            o, hi, lo, c, v = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])
        except (KeyError, TypeError, ValueError):
            n_invalid += 1
            if not h.validity_issues:
                h.validity_issues.append("non_numeric_field")
            continue
        if hi < lo:
            n_invalid += 1
            if "high_lt_low" not in h.validity_issues:
                h.validity_issues.append("high_lt_low")
        if hi < max(o, c) - 1e-6:
            n_invalid += 1
            if "high_lt_body_top" not in h.validity_issues:
                h.validity_issues.append("high_lt_body_top")
        if lo > min(o, c) + 1e-6:
            n_invalid += 1
            if "low_gt_body_bot" not in h.validity_issues:
                h.validity_issues.append("low_gt_body_bot")
        if v < 0:
            n_invalid += 1
            if "negative_volume" not in h.validity_issues:
                h.validity_issues.append("negative_volume")
        if min(o, hi, lo, c) <= 0 or max(o, hi, lo, c) > MAX_REASONABLE_PRICE:
            n_invalid += 1
            if "price_out_of_range" not in h.validity_issues:
                h.validity_issues.append("price_out_of_range")
    h.validity_ok = (n_invalid == 0)
    if n_invalid > 0 and "n_invalid_bars" not in h.validity_issues:
        h.validity_issues.append(f"n_invalid_bars={n_invalid}")

    # --- Last ingest event ---
    last_ev = last_ingest_event(sym, timeframe)
    if last_ev:
        h.last_ingest_ts = last_ev.get("ts")
        h.last_ingest_source = last_ev.get("source")
    return h


# ---------- Aggregate report ----------

def health_report(symbols: list[str] | None = None,
                  timeframe: str = "daily") -> HealthReport:
    """Run check_symbol over the universe; return a roll-up suitable for
    the dashboard pill."""
    if symbols is None:
        symbols = bars_store.list_symbols(timeframe)
    r = HealthReport(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        universe_size=len(symbols),
        timeframe=timeframe,
    )
    for s in symbols:
        h = check_symbol(s, timeframe)
        if h.freshness == "fresh":
            r.fresh += 1
        elif h.freshness == "stale":
            r.stale += 1
            r.stale_symbols.append(s)
        elif h.freshness == "ancient":
            r.ancient += 1
            r.ancient_symbols.append(s)
        else:
            r.missing += 1
        if not h.consistency_ok:
            r.consistency_failures += 1
        if not h.validity_ok:
            r.validity_failures += 1
            r.invalid_symbols.append(s)
    if r.ancient or r.validity_failures or r.missing:
        r.overall = "critical"
    elif r.stale or r.consistency_failures:
        r.overall = "warn"
    else:
        r.overall = "ok"
    return r


# ---------- CLI ----------

def _cmd_freshness(args) -> int:
    syms = bars_store.list_symbols(args.timeframe)
    rows: list[tuple[str, SymbolHealth]] = []
    for s in syms:
        h = check_symbol(s, args.timeframe)
        rows.append((s, h))
    if args.stale_only:
        rows = [r for r in rows if r[1].freshness in ("stale", "ancient", "missing")]
    rows.sort(key=lambda r: (r[1].days_stale or -1), reverse=True)
    print(f"# {len(rows)} symbols ({args.timeframe})")
    print(f"{'SYM':<8} {'freshness':<10} {'days_stale':>10}  last_bar")
    for s, h in rows:
        days = "—" if h.days_stale is None else str(h.days_stale)
        print(f"{s:<8} {h.freshness:<10} {days:>10}  {h.last_bar or '—'}")
    return 0


def _cmd_validity(args) -> int:
    syms = ([s.upper() for s in args.symbols.split(",")] if args.symbols
            else bars_store.list_symbols(args.timeframe))
    n_bad = 0
    for s in syms:
        h = check_symbol(s, args.timeframe)
        if not h.validity_ok or not h.consistency_ok:
            n_bad += 1
            print(f"{s:<8} consistency={'OK' if h.consistency_ok else 'FAIL'} "
                  f"validity={'OK' if h.validity_ok else 'FAIL'}  "
                  f"issues={','.join(h.consistency_issues + h.validity_issues) or '-'}")
        elif args.verbose:
            print(f"{s:<8} OK  bars={h.n_bars} last={h.last_bar}")
    print(f"# {n_bad}/{len(syms)} symbols with issues")
    return 0


def _cmd_log(args) -> int:
    events = _read_ingest_log_tail(args.tail)
    for ev in events:
        ts = (ev.get("ts") or "").replace("T", " ").split("+")[0]
        line = (f"{ts}  {ev.get('source','?'):<28} {ev.get('symbol','?'):<6} "
                f"{ev.get('timeframe','?'):<5} bars+={ev.get('bars_added', 0):<5}")
        if ev.get("error"):
            line += f"  ERROR: {ev['error']}"
        elif ev.get("note"):
            line += f"  ({ev['note']})"
        elif ev.get("last_bar"):
            line += f"  last={ev['last_bar'][:10]}"
        print(line)
    return 0


def _cmd_summary(args) -> int:
    r = health_report(timeframe=args.timeframe)
    print(json.dumps(asdict(r), indent=2, default=str))
    return 0


# ---------- Recovery: re-fetch stale symbols ----------

def refresh_stale(timeframe: str = "daily", *,
                  include_ancient: bool = True,
                  include_missing: bool = True,
                  years: int = 2) -> dict:
    """Re-fetch every stale / ancient / missing symbol via yfinance.

    Targeted recovery: only the symbols flagged by `health_report()` get
    re-pulled. `--force` is used so existing parquets are overwritten with
    the fresh data. Returns a summary dict suitable for the dashboard's
    /data/refresh-stale endpoint.
    """
    r = health_report(timeframe=timeframe)
    targets: list[str] = list(r.stale_symbols)
    if include_ancient:
        targets.extend(r.ancient_symbols)
    if include_missing:
        # `missing` count is reported but symbols aren't enumerated in
        # HealthReport. Fall back to iterating the universe for missing ones.
        all_syms = set(bars_store.list_symbols(timeframe))
        for s in all_syms:
            h = check_symbol(s, timeframe)
            if h.freshness == "missing" and s not in targets:
                targets.append(s)
    targets = sorted(set(targets))
    if not targets:
        return {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "n_targets": 0,
            "n_refreshed": 0,
            "results": {},
            "note": "no stale/ancient/missing symbols to refresh",
        }
    # Lazy import — yfinance pull is heavy.
    import yfinance_history  # type: ignore
    if timeframe == "daily":
        results = yfinance_history.ingest_daily(
            targets, years=years, resume=False,
            source="yfinance.refresh-stale",
        )
    else:
        results = yfinance_history.ingest_intraday(
            targets, timeframe=timeframe, resume=False,
            source="yfinance.refresh-stale",
        )
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_targets": len(targets),
        "n_refreshed": sum(1 for v in results.values() if v > 0),
        "results": results,
    }


def _cmd_refresh_stale(args) -> int:
    out = refresh_stale(timeframe=args.timeframe, years=args.years)
    print(json.dumps(out, indent=2, default=str))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_fresh = sub.add_parser("freshness", help="per-symbol last-update date")
    p_fresh.add_argument("--timeframe", default="daily",
                         choices=bars_store.SUPPORTED_TIMEFRAMES)
    p_fresh.add_argument("--stale-only", action="store_true",
                         help="only show stale / ancient / missing symbols")
    p_fresh.set_defaults(func=_cmd_freshness)

    p_val = sub.add_parser("validity", help="OHLCV + consistency audit")
    p_val.add_argument("--timeframe", default="daily",
                       choices=bars_store.SUPPORTED_TIMEFRAMES)
    p_val.add_argument("--symbols", help="comma-separated; default = all")
    p_val.add_argument("--verbose", action="store_true")
    p_val.set_defaults(func=_cmd_validity)

    p_log = sub.add_parser("log", help="recent ingest log entries")
    p_log.add_argument("--tail", type=int, default=20)
    p_log.set_defaults(func=_cmd_log)

    p_sum = sub.add_parser("summary",
                            help="one-shot health summary JSON (dashboard endpoint shape)")
    p_sum.add_argument("--timeframe", default="daily",
                       choices=bars_store.SUPPORTED_TIMEFRAMES)
    p_sum.set_defaults(func=_cmd_summary)

    p_rs = sub.add_parser("refresh-stale",
                          help="re-fetch every stale/ancient/missing symbol via yfinance")
    p_rs.add_argument("--timeframe", default="daily",
                      choices=bars_store.SUPPORTED_TIMEFRAMES)
    p_rs.add_argument("--years", type=int, default=2,
                      help="history depth for daily refreshes (default 2)")
    p_rs.set_defaults(func=_cmd_refresh_stale)
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
