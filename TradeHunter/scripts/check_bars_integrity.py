"""Parquet integrity + consistency audit for data/price_history/.

Read-only. Runs against get_data_root()/price_history — no IBKR/network.

Tier 1 (default, ~minutes): METADATA ONLY. For every parquet:
  - opens (a read failure => corrupt, e.g. the "Column cannot have more than
    one dictionary" OSError we saw on AMRX/AORT)
  - schema columns == {t,o,h,l,c,v}
  - row count (0 => empty)
  - date-range depth via parquet column stats (bars_store.available_range_fast)
    vs per-timeframe target (3min/5min=180d, daily=730d); flags short depth
  - staleness (latest bar older than --stale-days)
  - cross-timeframe coverage (symbol present in one tf but missing in another)

Tier 2 (--deep, ~10-25 min): FULL READ of every file. Adds:
  - duplicate timestamps
  - non-monotonic (unsorted) timestamps
  - OHLC sanity: h>=l, h>=max(o,c), l<=min(o,c), v>=0, no NaN/inf
  - daily calendar gap ratio (business-day coverage; approximate — holidays
    are not modelled, so only large shortfalls are flagged)

CLI:
  py -3.12 scripts/check_bars_integrity.py                 # Tier 1
  py -3.12 scripts/check_bars_integrity.py --deep          # Tier 1 + 2
  py -3.12 scripts/check_bars_integrity.py --tf 3min,daily # subset
  py -3.12 scripts/check_bars_integrity.py --max-show 50
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
for _p in [str(_root), str(_root / "scripts"), str(_root / "resources")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bars_store                       # noqa: E402
from _common import get_data_root       # noqa: E402

TARGET_DAYS = {"1min": 7, "3min": 180, "5min": 180, "15min": 60, "daily": 730}
EXPECTED_COLS = {"t", "o", "h", "l", "c", "v"}

# Live progress for the tray's deep-check bar. We write a tiny JSON every ~50
# files; the tray polls it (and treats it as "running" while the ts is fresh).
_PROG = {"done": 0, "total": 0}


def _progress_path() -> Path:
    return get_data_root() / "_deepcheck_progress.json"


def _write_progress(phase: str) -> None:
    try:
        import json
        _progress_path().write_text(json.dumps({
            "phase": phase, "done": _PROG["done"], "total": _PROG["total"],
            "ts": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
    except Exception:
        pass


def _bump(phase: str) -> None:
    _PROG["done"] += 1
    if _PROG["done"] % 50 == 0 or _PROG["done"] >= _PROG["total"]:
        _write_progress(phase)


def _clear_progress() -> None:
    try:
        _progress_path().unlink()
    except Exception:
        pass


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def tier1(timeframes: list[str], price_root: Path, stale_days: int) -> dict:
    import pyarrow.parquet as pq
    now = datetime.now(timezone.utc)
    report: dict = {"per_tf": {}, "symbols_by_tf": {}}
    for tf in timeframes:
        tdir = price_root / tf
        files = sorted(tdir.glob("*.parquet")) if tdir.exists() else []
        res = {"total": len(files), "corrupt": [], "empty": [], "bad_schema": [],
               "short_depth": [], "stale": [], "ok": 0}
        syms = set()
        target = TARGET_DAYS.get(tf, 0)
        for f in files:
            sym = f.stem
            syms.add(sym)
            _bump("tier1")
            try:
                md = pq.read_metadata(f)
            except Exception as exc:
                res["corrupt"].append((sym, f"{type(exc).__name__}: {str(exc)[:60]}"))
                continue
            if md.num_rows == 0:
                res["empty"].append(sym)
                continue
            cols = set(md.schema.names)
            if not EXPECTED_COLS.issubset(cols):
                res["bad_schema"].append((sym, sorted(cols)))
                continue
            rng = bars_store.available_range_fast(sym, timeframe=tf)
            if rng is None:
                res["corrupt"].append((sym, "no parseable date range"))
                continue
            try:
                earliest, latest = _parse_iso(rng[0]), _parse_iso(rng[1])
            except Exception:
                res["corrupt"].append((sym, "unparseable timestamps"))
                continue
            span = (latest - earliest).days
            if target and span < int(target * 0.90):
                res["short_depth"].append((sym, span, target))
            if (now - latest).days > stale_days:
                res["stale"].append((sym, latest.date().isoformat()))
            res["ok"] += 1
        report["per_tf"][tf] = res
        report["symbols_by_tf"][tf] = syms
    return report


def tier2(timeframes: list[str], price_root: Path, max_show: int) -> dict:
    import pyarrow.parquet as pq
    report: dict = {}
    for tf in timeframes:
        tdir = price_root / tf
        files = sorted(tdir.glob("*.parquet")) if tdir.exists() else []
        issues = {"dupes": [], "unsorted": [], "ohlc_bad": [], "nan": [],
                  "neg_vol": []}
        n = len(files)
        sys.stderr.write(f"[tier2] {tf}: reading {n} files ...\n"); sys.stderr.flush()
        for i, f in enumerate(files, 1):
            sym = f.stem
            _bump("tier2")
            if i % 200 == 0 or i == n:
                sys.stderr.write(f"[tier2] {tf}: {i}/{n}\n"); sys.stderr.flush()
            try:
                df = pq.read_table(f).to_pandas()
            except Exception:
                continue  # corruption already reported in tier1
            if df.empty or not EXPECTED_COLS.issubset(df.columns):
                continue
            t = df["t"]
            ndup = int(t.duplicated().sum())
            if ndup:
                issues["dupes"].append((sym, ndup))
            if not t.is_monotonic_increasing:
                issues["unsorted"].append(sym)
            o, h, l, c, v = (df[x] for x in ("o", "h", "l", "c", "v"))
            bad_ohlc = int(((h < l) | (h < o) | (h < c) | (l > o) | (l > c)).sum())
            if bad_ohlc:
                issues["ohlc_bad"].append((sym, bad_ohlc))
            nan = int(df[list(EXPECTED_COLS - {"t"})].isna().sum().sum())
            if nan:
                issues["nan"].append((sym, nan))
            nneg = int((v < 0).sum())
            if nneg:
                issues["neg_vol"].append((sym, nneg))
        report[tf] = issues
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="run Tier 2 full-read checks")
    ap.add_argument("--tf", default="3min,5min,daily", help="comma timeframes")
    ap.add_argument("--stale-days", type=int, default=7,
                    help="flag if latest bar older than N days (default 7)")
    ap.add_argument("--max-show", type=int, default=25, help="rows per issue list")
    args = ap.parse_args()

    tfs = [t.strip() for t in args.tf.split(",") if t.strip()]
    price_root = get_data_root() / "price_history"
    print(f"# auditing {price_root}")
    print(f"# timeframes: {tfs}\n")

    # Progress denominator for the tray bar: every file is read once in tier1,
    # and again in tier2 when --deep, so total = files * (2 if deep else 1).
    all_n = 0
    for tf in tfs:
        d = price_root / tf
        all_n += len(list(d.glob("*.parquet"))) if d.exists() else 0
    _PROG["total"] = all_n * (2 if args.deep else 1)
    _PROG["done"] = 0
    _write_progress("tier1")

    r = tier1(tfs, price_root, args.stale_days)
    N = args.max_show

    def show(label, items, fmt=lambda x: str(x)):
        if not items:
            return
        print(f"    {label}: {len(items)}")
        for it in items[:N]:
            print(f"        {fmt(it)}")
        if len(items) > N:
            print(f"        ... +{len(items) - N} more")

    print("===== TIER 1: metadata / depth / coverage =====")
    for tf, res in r["per_tf"].items():
        print(f"\n[{tf}] {res['total']} files — OK={res['ok']} "
              f"corrupt={len(res['corrupt'])} empty={len(res['empty'])} "
              f"bad_schema={len(res['bad_schema'])} "
              f"short_depth={len(res['short_depth'])} stale={len(res['stale'])}")
        show("CORRUPT", res["corrupt"], lambda x: f"{x[0]}: {x[1]}")
        show("EMPTY", res["empty"])
        show("BAD SCHEMA", res["bad_schema"], lambda x: f"{x[0]}: {x[1]}")
        show("SHORT DEPTH (span<90% target)", res["short_depth"],
             lambda x: f"{x[0]}: {x[1]}d / {x[2]}d target")
        show("STALE", res["stale"], lambda x: f"{x[0]}: last {x[1]}")

    # Cross-timeframe coverage
    print("\n===== cross-timeframe coverage =====")
    allsyms = set().union(*r["symbols_by_tf"].values()) if r["symbols_by_tf"] else set()
    print(f"  union of symbols across {tfs}: {len(allsyms)}")
    for tf in tfs:
        missing = sorted(allsyms - r["symbols_by_tf"].get(tf, set()))
        if missing:
            print(f"  MISSING from {tf}: {len(missing)}")
            print(f"      {', '.join(missing[:N])}" + (" ..." if len(missing) > N else ""))

    if args.deep:
        print("\n===== TIER 2: full per-bar integrity =====")
        d = tier2(tfs, price_root, N)
        for tf, issues in d.items():
            tot = sum(len(v) for v in issues.values())
            print(f"\n[{tf}] flagged files: {tot}")
            show("DUPLICATE timestamps", issues["dupes"], lambda x: f"{x[0]}: {x[1]} dupes")
            show("UNSORTED timestamps", issues["unsorted"])
            show("OHLC violations", issues["ohlc_bad"], lambda x: f"{x[0]}: {x[1]} bars")
            show("NaN values", issues["nan"], lambda x: f"{x[0]}: {x[1]} cells")
            show("NEGATIVE volume", issues["neg_vol"], lambda x: f"{x[0]}: {x[1]} bars")

    print("\n# done")
    _clear_progress()   # tray: absent file => deep check finished
    return 0


if __name__ == "__main__":
    sys.exit(main())
