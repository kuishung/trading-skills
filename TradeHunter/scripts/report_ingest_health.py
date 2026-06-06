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
    # Newest bar per symbol (metadata-only stat read). Keyed by canonical symbol
    # so the per-category health pass can match index membership lists.
    per_sym: dict[str, float] = {}
    newest_iso = None
    for sym in bars_store.list_symbols(tf):
        try:
            rng = bars_store.available_range_fast(sym, timeframe=tf)
        except Exception:
            rng = None
        if rng:
            ep = _epoch(rng[1])
            if ep:
                per_sym[_canon(sym)] = ep
                if ep > newest:
                    newest = ep
                    newest_iso = rng[1]
    row = {
        "tf": tf,
        "symbols": symbols,
        "mb": round(total / 1048576, 1),
        "newest_epoch": int(newest) if newest else 0,
        "newest_bar": _newest_label(newest_iso, tf),   # the DATE of the newest bar
    }
    return row, per_sym


def _newest_label(iso, tf: str) -> str | None:
    """Human label for the newest bar: daily -> 'YYYY-MM-DD' (session date, taken
    as the stored UTC date so a UTC-midnight daily stamp shows the right day);
    intraday -> 'YYYY-MM-DD HH:MM ET'. So the user reads the data's last date, not
    a relative 'N ago'."""
    if not iso:
        return None
    s = str(iso).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return str(iso)[:10]
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    if tf == "daily":
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        from zoneinfo import ZoneInfo
        return d.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ago(secs: float) -> str:
    secs = int(secs or 0)
    if secs < 90:
        return "just now"
    if secs // 60 < 90:
        return f"{secs // 60} min ago"
    if secs // 3600 < 48:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _canon(s: str) -> str:
    """Canonical symbol key for cross-source matching — folds share-class
    punctuation so BRK.B (Wikipedia) and BRK-B (stored) compare equal."""
    out = []
    for ch in str(s).upper():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


# How far behind the freshest symbol (per timeframe) a symbol may fall before
# it counts as "lagging". Cohort-relative, so a market-closed weekend (all
# symbols equally old) flags nobody — only symbols that missed the last ingest.
_LAG_SECS = {"3min": 90000, "5min": 90000, "daily": 300000}  # ~25h / ~25h / ~3.5d


def _tf_health(members: set[str], per_sym: dict[str, float], ref: float, tf: str) -> dict:
    """Freshness of one category on one timeframe: fresh vs stale (lagging the
    cohort OR no file at all), plus the worst lag age, plus a 0/1/2 tier."""
    lag_secs = _LAG_SECS.get(tf, 90000)
    fresh = stale = 0
    worst = 0.0
    for s in members:
        ep = per_sym.get(s)
        if ep is None:
            stale += 1            # member has no parquet at this timeframe
            continue
        behind = ref - ep
        if behind > lag_secs:
            stale += 1
            worst = max(worst, behind)
        else:
            fresh += 1
    total = fresh + stale
    # tolerate a tiny tail (≤2% or 1 symbol) as amber; more is red
    tol = max(1, int(total * 0.02))
    tier = 0 if stale == 0 else (1 if stale <= tol else 2)
    return {"fresh": fresh, "stale": stale, "total": total,
            "worst_ago": _ago(worst) if worst else None, "tier": tier}


def _universe_health(tf_per_sym: dict[str, dict[str, float]],
                     store_newest: dict[str, float],
                     stored: set[str], timeframes) -> list[dict]:
    """Per-category completeness + per-timeframe freshness. Memberships overlap
    (NASDAQ-100 ⊂ S&P 500). Reads cached Wikipedia membership lists via
    resources/*; soft-fail to [] if unavailable."""
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

    def _row(name, members, expected):
        seeded_members = members & canon_stored
        seeded = len(seeded_members)
        per_tf, worst_tier = [], 0
        for tf in timeframes:
            h = _tf_health(seeded_members, tf_per_sym.get(tf, {}), store_newest.get(tf, 0.0), tf)
            h["tf"] = tf
            per_tf.append(h)
            worst_tier = max(worst_tier, h["tier"])
        return {"name": name, "expected": expected, "seeded": seeded,
                "missing": max(0, expected - seeded) if expected else 0,
                "timeframes": per_tf, "tier": worst_tier}

    for name, fn in indices:
        try:
            members = {_canon(x) for x in fn()}
        except Exception:
            continue
        covered |= (members & canon_stored)
        rows.append(_row(name, members, len(members)))
    # residual: seeded symbols not in any index above
    other = canon_stored - covered
    if other:
        rows.append(_row("Other", other, 0))
    rows.append(_row("All seeded", canon_stored, 0))
    rows[-1]["_total"] = True
    return rows


def _resolve_key(cli_key: str | None) -> str | None:
    if cli_key:
        return cli_key.strip()
    env = os.environ.get("TST_INGEST_API_KEY")
    if env:
        return env.strip()
    envf = ROOT / "dashboard_tst" / "app" / ".env"
    found: str | None = None
    try:
        for line in envf.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("TST_INGEST_API_KEY"):
                _, _, v = line.partition("=")
                v = v.strip().strip('"').strip("'")
                if v:
                    found = v   # LAST wins — matches python-dotenv, so a
                                # duplicate key never silently mismatches the app
    except OSError:
        pass
    return found


def build_report() -> dict:
    tfs = []
    tf_per_sym: dict[str, dict] = {}
    store_newest: dict[str, float] = {}
    now = time.time()
    for tf in TIMEFRAMES:
        row, per_sym = _tf_report(tf)
        tfs.append(row)
        tf_per_sym[tf] = per_sym
        # Cohort reference = 95th percentile of newest-bar epochs (capped at now),
        # not the raw max — so a single bad future-dated bar can't poison the ref
        # and paint every category red.
        vals = sorted(e for e in per_sym.values() if e <= now + 3600)
        store_newest[tf] = vals[int(len(vals) * 0.95)] if vals else 0.0
    # Canonical universe = the daily store (one file per seeded symbol).
    stored = set(bars_store.list_symbols("daily")) or set(bars_store.list_symbols("3min"))
    return {
        "host": os.environ.get("COMPUTERNAME") or "hermes",
        "root": str(bars_store.PRICE_HISTORY_ROOT),
        "generated_epoch": int(time.time()),
        "timeframes": tfs,
        "universe_health": _universe_health(tf_per_sym, store_newest, stored, TIMEFRAMES),
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
