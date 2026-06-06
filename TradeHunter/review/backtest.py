"""Strategy-agnostic backtest harness.

Loads a strategy adapter by name (review._adapter_registry), walks the
specified date range, asks the adapter for candidates per day, simulates
each candidate via review._trade_sim, then aggregates with review._metrics
and writes results to data/review/.

Architecture goal: this file knows ZERO strategy specifics. To backtest a
new strategy:
  1. Implement the adapter Protocol (review/_strategy_adapter.py) in
     strategy/<FAMILY>/<setup>/backtest_adapter.py
  2. Register it in review/_adapter_registry.py
  3. py review/backtest.py --strategy <name> --start <D> --end <D>

That's it. No edits to this file.

CLI:
  py review/backtest.py --strategy ditp_p2 --start 2026-05-12 --end 2026-05-22
  py review/backtest.py --list-strategies
  py review/backtest.py --strategy ditp_p2 --symbols NVRI,CTRE     # narrow universe
  py review/backtest.py --strategy ditp_p2 --no-write              # smoke
  py review/backtest.py --strategy ditp_p2 --bucket-by tier,variant,confluence_tier

Output:
  data/review/backtest_<strategy>_<ts>.jsonl      one trade record per line
  data/review/backtest_<strategy>_<ts>.json       aggregate summary
"""
from __future__ import annotations

import argparse
import json
import sys
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

from review import _adapter_registry  # noqa: E402
from review import _trade_sim          # noqa: E402
from review import _metrics            # noqa: E402


# ---- Trading-day helpers ----
#
# Phase 1: weekday-only (no US holiday calendar). The harness skips Sat/Sun
# automatically; a holiday will simply produce 0 candidates / 0 trades for
# that day -- not wrong, just empty. A proper calendar lives in
# resources/trading_calendar.py when we extract it (Phase 5 polish).

def _iter_trading_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:   # 0=Mon ... 4=Fri
            yield d
        d += timedelta(days=1)


# ---- Bar loading ----
#
# Filter bars to ONLY the simulation date. bars_store stores full history
# per symbol; we slice to the trading day. Bars are assumed UTC.
# RTH-only filtering would belong here too, but for Phase 1 we let the
# adapter's entry_signal decide what to act on -- the harness just hands
# over the date's bars.

def _bars_for_date(symbol: str, timeframe: str, d: date) -> list[dict]:
    """Return all bars belonging to the ET SESSION `d` (pre-market 04:00 ET
    through after-hours 20:00 ET). Empty list if the symbol's parquet doesn't
    cover the date.

    Uses `bars_store.bar_session_date_et` — NOT a naive UTC date. A UTC date
    mis-files after-hours bars (20:00 ET = 00:00 UTC next day) into tomorrow's
    session, which corrupts intraday backtests (GUNS, DITP extended-hours).
    """
    all_bars = bars_store.load_bars(symbol, timeframe=timeframe)
    if not all_bars:
        return []
    out = []
    for b in all_bars:
        try:
            if bars_store.bar_session_date_et(b["t"]) == d:
                out.append(b)
        except (TypeError, ValueError):
            continue
    return out


# ---- Main run loop ----

def run(strategy: str, start: date, end: date,
        symbols: list[str] | None = None,
        bucket_by: list[str] | None = None,
        write: bool = True) -> dict:
    """Backtest `strategy` over [start, end] inclusive. Returns the summary
    dict; writes JSONL + JSON to data/review/ unless write=False."""
    adapter = _adapter_registry.load(strategy, universe=symbols)
    tf = adapter.primary_timeframe
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{strategy}_{run_ts}"

    bucket_by = bucket_by or ["scanner_tier", "confluence_tier",
                              "variant", "exit_reason"]

    trades: list[dict] = []
    n_symbols_skipped_missing_bars = 0
    n_candidate_days = 0
    n_days = 0

    print(f"# backtest {strategy} v{adapter.engine_version} "
          f"({tf}) -- {start} to {end}", flush=True)

    # Data-sufficiency pre-flight: does the store actually cover this window for
    # the universe + primary timeframe? Warn (don't block) — partial coverage is
    # informative, not fatal. Skip the full-store scan unless a universe was
    # given OR it's cheap, to keep ad-hoc runs snappy.
    coverage = None
    try:
        from review import _coverage
        cov_syms = symbols or bars_store.list_symbols(tf)
        coverage = _coverage.check_coverage(cov_syms, tf, start, end)
        print(_coverage.headline(coverage), flush=True)
        if not coverage["fully_covered"]:
            print("#   ^ some symbols lack full history for this window — "
                  "their absence understates trade count (not a strategy result).",
                  flush=True)
    except Exception as exc:
        print(f"# coverage check skipped: {type(exc).__name__}: {exc}", flush=True)

    for d in _iter_trading_days(start, end):
        n_days += 1
        as_of = d - timedelta(days=1)
        # Step back over weekends so the scanner sees the previous TRADING day
        while as_of.weekday() >= 5:
            as_of -= timedelta(days=1)

        candidates = adapter.pick_candidates(as_of_date=as_of)
        if not candidates:
            print(f"  {d} ({as_of} scan) -- 0 candidates", flush=True)
            continue

        per_day_trades = 0
        per_day_no_trigger = 0
        per_day_rejected = 0
        per_day_no_bars = 0

        for cand in candidates:
            n_candidate_days += 1
            sym = cand["symbol"]
            bars = _bars_for_date(sym, tf, d)
            if not bars:
                per_day_no_bars += 1
                n_symbols_skipped_missing_bars += 1
                continue
            trade = _trade_sim.simulate_trade(adapter, cand, bars,
                                              date_iso=d.isoformat())
            if trade is None:
                per_day_no_bars += 1
                continue
            trade["run_id"] = run_id
            trades.append(trade)
            er = trade.get("exit_reason")
            if er in ("TP", "SL", "SL_AMBIGUOUS", "EOD"):
                per_day_trades += 1
            elif er == "no_trigger":
                per_day_no_trigger += 1
            elif er in ("rejected_tradeability", "rejected_bad_R"):
                per_day_rejected += 1

        print(f"  {d} -- {len(candidates):>3d} cand  | "
              f"{per_day_trades:>2d} trades  "
              f"{per_day_no_trigger:>2d} no-trig  "
              f"{per_day_rejected:>2d} rejected  "
              f"{per_day_no_bars:>2d} missing-bars", flush=True)

    summary = _metrics.compute(trades, bucket_by=bucket_by)
    summary["run_id"] = run_id
    summary["run_at_utc"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    summary["window"] = {
        "start": start.isoformat(), "end": end.isoformat(),
        "trading_days": n_days,
    }
    summary["strategy"] = strategy
    summary["engine_version"] = adapter.engine_version
    summary["primary_timeframe"] = tf
    summary["universe"] = {
        "n_symbols_in_universe": (len(symbols) if symbols
                                  else len(bars_store.list_symbols("daily"))),
        "n_candidate_days": n_candidate_days,
        "n_symbol_days_skipped_missing_bars": n_symbols_skipped_missing_bars,
        "note": "today's snapshot — survivorship bias caveat applies",
    }
    if coverage is not None:
        summary["coverage"] = coverage

    if write:
        from _common import get_data_root  # honours cfg["data_root"]
        out_dir = get_data_root() / "review"
        out_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = out_dir / f"backtest_{run_id}.jsonl"
        json_path  = out_dir / f"backtest_{run_id}.json"
        with jsonl_path.open("w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, default=str) + "\n")
        json_path.write_text(json.dumps(summary, indent=2, default=str),
                             encoding="utf-8")
        # Display path relative to SKILL_DIR if possible (compact); else absolute
        try:
            rel_jsonl = jsonl_path.relative_to(SKILL_DIR)
            rel_json = json_path.relative_to(SKILL_DIR)
            print(f"\n# wrote {rel_jsonl}", flush=True)
            print(f"# wrote {rel_json}", flush=True)
        except ValueError:
            print(f"\n# wrote {jsonl_path}", flush=True)
            print(f"# wrote {json_path}", flush=True)

    print("\n" + _metrics.headline(summary), flush=True)
    return summary


# ---- CLI ----

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--strategy",
                    help="registered strategy name (see --list-strategies)")
    ap.add_argument("--list-strategies", action="store_true",
                    help="print registered strategies and exit")
    ap.add_argument("--start", help="YYYY-MM-DD inclusive")
    ap.add_argument("--end", help="YYYY-MM-DD inclusive (default: today)")
    ap.add_argument("--symbols",
                    help="comma-separated symbols to restrict the universe")
    ap.add_argument("--bucket-by",
                    help="comma-separated trade/candidate_meta fields for per-group cuts; "
                         "default: scanner_tier,confluence_tier,variant,exit_reason")
    ap.add_argument("--no-write", action="store_true",
                    help="don't write data/review/backtest_*.{jsonl,json}")
    args = ap.parse_args()

    if args.list_strategies:
        print("registered strategies:")
        for n in _adapter_registry.known():
            print(f"  - {n}")
        return 0

    if not args.strategy:
        ap.error("--strategy is required (or use --list-strategies)")
    if not args.start:
        ap.error("--start is required")

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = (datetime.strptime(args.end, "%Y-%m-%d").date()
           if args.end else date.today())
    if end < start:
        ap.error(f"--end {end} is before --start {start}")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    bucket_by = ([s.strip() for s in args.bucket_by.split(",") if s.strip()]
                 if args.bucket_by else None)

    run(strategy=args.strategy, start=start, end=end,
        symbols=symbols, bucket_by=bucket_by, write=not args.no_write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
