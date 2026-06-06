"""Push parquet-ingest DATA freshness to the TradeHunter dashboard.

Runs on Hermes (the ingest box — it's allowed to read parquets; the dashboard
is NOT, per the CLAUDE.md scope rule). For each seeded timeframe it reads the
**newest bar timestamp** from parquet row-group statistics (metadata only, ~1ms
per symbol via ``bars_store.available_range_fast`` — no row data materialised),
plus the symbol count + total size, and POSTs the report to::

    POST http://<dashboard>/api/ingest/health   (header X-API-Key)

The dashboard then shows "newest <age> ago" per timeframe (true DATA freshness),
not just the file write-time the local read can see. The pushed row is preferred
over the file-mtime read, so once this reporter runs, the Data Ingest page
answers "how fresh is the seeded data?" honestly.

Usage (Hermes — PowerShell, py -3.12 has pyarrow):
    py -3.12 scripts/report_ingest_health.py
    py -3.12 scripts/report_ingest_health.py --url http://localhost:8000 --api-key XXXX

The API key resolves from (first wins): --api-key, $env:TST_INGEST_API_KEY, or
the TST_INGEST_API_KEY line in dashboard_tst/app/.env. Soft-fail by design — a
reporting hiccup must never affect the ingest itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resources import bars_store  # noqa: E402

# The timeframes we actually seed (mirror dashboard_tst ingest_health._TIMEFRAMES).
TIMEFRAMES = ("3min", "5min", "daily")


def _epoch(iso: str | None) -> float:
    """Parse a bars_store `t` ISO string to a UTC unix epoch. Tolerant of
    trailing 'Z', missing tz (assume UTC), and space-vs-'T' separators."""
    if not iso:
        return 0.0
    s = str(iso).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        # last resort: date only
        try:
            d = dt.datetime.fromisoformat(s[:10])
        except ValueError:
            return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def _tf_report(tf: str) -> dict:
    """{tf, symbols, mb, newest_epoch} for one timeframe."""
    d = bars_store.bars_dir(tf)
    symbols = 0
    total = 0
    newest = 0.0
    if d.is_dir():
        for e in os.scandir(d):
            if not (e.is_file() and e.name.endswith(".parquet")):
                continue
            symbols += 1
            try:
                total += e.stat().st_size
            except OSError:
                pass
    # Newest bar across the universe (metadata-only stat read per symbol).
    for sym in bars_store.list_symbols(tf):
        try:
            rng = bars_store.available_range_fast(sym, timeframe=tf)
        except Exception:
            rng = None
        if rng:
            newest = max(newest, _epoch(rng[1]))
    return {
        "tf": tf,
        "symbols": symbols,
        "mb": round(total / 1048576, 1),
        "newest_epoch": int(newest) if newest else 0,
    }


def _canon(s: str) -> str:
    """Canonical symbol key for cross-source matching — folds share-class
    punctuation so BRK.B (Wikipedia) and BRK-B (stored) compare equal."""
    out = []
    for ch in str(s).upper():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def _universe_breakdown(stored: set[str]) -> list[dict]:
    """How the seeded symbols split across the index universes (memberships
    overlap — NASDAQ-100 names are mostly inside the S&P 500). Reads the cached
    Wikipedia membership lists via resources/*; soft-fail to [] if unavailable."""
    try:
        from resources import sp500, sp_midcap400, sp_smallcap600, nasdaq100
    except Exception:
        return []
    indices = [
        ("S&P 500", sp500.get_sp500_symbols),
        ("S&P 400 (mid)", sp_midcap400.get_sp400_symbols),
        ("S&P 600 (small)", sp_smallcap600.get_sp600_symbols),
        ("NASDAQ-100", nasdaq100.get_nasdaq100_symbols),
    ]
    canon_stored = {_canon(s) for s in stored}
    rows: list[dict] = []
    covered: set[str] = set()
    for name, fn in indices:
        try:
            members = {_canon(x) for x in fn()}
        except Exception:
            continue
        hit = canon_stored & members
        covered |= hit
        rows.append({"name": name, "count": len(hit)})
    other = len(canon_stored - covered)
    if other:
        rows.append({"name": "Other", "count": other})
    rows.append({"name": "Total seeded", "count": len(canon_stored), "_total": True})
    return rows


def _resolve_key(cli_key: str | None) -> str | None:
    if cli_key:
        return cli_key.strip()
    env = os.environ.get("TST_INGEST_API_KEY")
    if env:
        return env.strip()
    envf = ROOT / "dashboard_tst" / "app" / ".env"
    try:
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TST_INGEST_API_KEY"):
                _, _, v = line.partition("=")
                return v.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def build_report() -> dict:
    tfs = [_tf_report(tf) for tf in TIMEFRAMES]
    # Canonical universe = the daily store (one file per seeded symbol).
    stored = set(bars_store.list_symbols("daily")) or set(bars_store.list_symbols("3min"))
    return {
        "host": os.environ.get("COMPUTERNAME") or "hermes",
        "root": str(bars_store.PRICE_HISTORY_ROOT),
        "generated_epoch": int(time.time()),
        "timeframes": tfs,
        "universe": _universe_breakdown(stored),
        "log_tail": [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Push parquet ingest freshness to the dashboard.")
    ap.add_argument("--url", default=os.environ.get("TST_DASHBOARD_URL", "http://localhost:8000"),
                    help="Dashboard base URL (default http://localhost:8000).")
    ap.add_argument("--api-key", default=None, help="Override TST_INGEST_API_KEY.")
    ap.add_argument("--dry-run", action="store_true", help="Print the report, don't POST.")
    args = ap.parse_args()

    t0 = time.time()
    report = build_report()
    report["build_secs"] = round(time.time() - t0, 1)

    if args.dry_run:
        print(json.dumps(report, indent=2))
        return 0

    key = _resolve_key(args.api_key)
    if not key:
        print("ERROR: no API key (set $env:TST_INGEST_API_KEY or pass --api-key, "
              "or add TST_INGEST_API_KEY to dashboard_tst/app/.env).", file=sys.stderr)
        return 2

    url = args.url.rstrip("/") + "/api/ingest/health"
    body = json.dumps(report).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = resp.status in (200, 201)
            print(f"POST {url} -> {resp.status} {resp.read().decode('utf-8', 'replace')[:200]}")
    except Exception as exc:
        print(f"ERROR posting to {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    tfs = ", ".join(f"{t['tf']}:{t['symbols']}sym" for t in report["timeframes"])
    print(f"reported {tfs} in {report['build_secs']}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
