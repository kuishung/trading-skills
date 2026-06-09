"""report_edgar_health.py — AI-Hermes-side EDGAR ingest health reporter.

Runs as a scheduled task ON AI-HERMES (the Windows file server, 192.168.1.162),
where the EDGAR corpus lives (C:\\HermesSync\\MarketResearch\\QuarterlyReport).
The Nous Hermes agent FETCHES the filings into that share; this reporter SCANS
the folder and POSTs a per-ticker completeness report to TradeHunter's
/api/ingest/edgar. The Data Ingest page then shows which tickers have missing
quarters / stub MDs (see app/services/edgar_health.py).

No database of "what should exist" is needed: the folder IS the source of truth.
Each ticker files 3 ten-Q quarters + 1 ten-K per fiscal year, so a ticker's
present quarters reveal the gaps directly — a missing quarter inside its regular
filing cadence is a hole to re-fetch.

No third-party deps (stdlib only) — run with `py`:
    py C:\\trading-skills\\TradeHunter\\dashboard_tst\\deploy\\report_edgar_health.py

Reads config from the dashboard's app/.env (next to this deploy/ dir) and/or env:
  TST_EDGAR_DIR        corpus root (default C:\\HermesSync\\MarketResearch\\QuarterlyReport)
  TST_INGEST_API_KEY   X-API-Key for the dashboard API (same as the parquet reporter)
  TST_DASHBOARD_URL    dashboard base URL (default http://localhost:8000)

Flags: --dry-run (don't POST), --out FILE (dump the report JSON), --limit N.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
import urllib.request
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV = os.path.join(_HERE, "..", "app", ".env")
_DEFAULT_ROOT = r"C:\HermesSync\MarketResearch\QuarterlyReport"

# Pull (form, year, quarter) out of a filename like `AMD_10Q_2011-Q2.html`,
# `MSFT_2026_Q1.md`, `NVDA_10K_2025-FY.html`. Form token is optional (the stub
# files omit it); period token is `YYYY[-_](Q1-4 | FY)`.
_FORM_RE = re.compile(r"10[\-_]?([QK])", re.IGNORECASE)
_PERIOD_RE = re.compile(r"(20\d{2})[\-_](Q[1-4]|FY)", re.IGNORECASE)
_STUB_MAX_BYTES = 600          # an .md smaller than this carries no report body


def _load_env() -> dict:
    """Merge app/.env (if present) with os.environ (env wins) for TST_ keys."""
    cfg: dict[str, str] = {}
    try:
        with open(_ENV, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("TST_")})
    return cfg


def _missing_quarters(q_by_year: dict[int, set[int]]) -> list[str]:
    """Given {year: {quarters present as 10-Q}}, list missing quarters within the
    ticker's regular filing cadence. A company files 3 ten-Qs/year, so the
    "regular slots" are the quarters it files in >= half its years (handles
    non-calendar filers like NVDA whose 10-Q quarters are 2/3/4, not 1/2/3). Only
    gaps INSIDE the seeded span are flagged (partial first/last years don't
    false-flag)."""
    if not q_by_year:
        return []
    years = sorted(q_by_year)
    qcount: Counter[int] = Counter()
    for qs in q_by_year.values():
        qcount.update(qs)
    thresh = max(1, len(years) / 2)
    slots = {q for q in (1, 2, 3, 4) if qcount[q] >= thresh} or {1, 2, 3}
    present_yq = sorted((y, q) for y, qs in q_by_year.items() for q in qs)
    first_yq, last_yq = present_yq[0], present_yq[-1]
    missing: list[str] = []
    for y in range(years[0], years[-1] + 1):
        have = q_by_year.get(y, set())
        for q in sorted(slots):
            if q in have:
                continue
            if (y, q) < first_yq or (y, q) > last_yq:
                continue          # outside the seeded span -> not a hole
            missing.append(f"{y}-Q{q}")
    return missing


def _scan_ticker(dir_path: str) -> dict:
    """Folder facts for one ticker: present 10-Q quarters (-> missing list),
    whether any 10-K exists, latest period, newest mtime, html/md counts, and
    whether the .md bodies are all stubs."""
    q_by_year: dict[int, set[int]] = {}
    has_10k = False
    latest: tuple[int, int] | None = None
    newest = 0.0
    html = md = 0
    md_max = 0
    try:
        for e in os.scandir(dir_path):
            if not e.is_file():
                continue
            name = e.name
            low = name.lower()
            try:
                st = e.stat()
            except OSError:
                continue
            if st.st_mtime > newest:
                newest = st.st_mtime
            if low.endswith((".html", ".htm")):
                html += 1
            elif low.endswith(".md"):
                md += 1
                md_max = max(md_max, st.st_size)
            pm = _PERIOD_RE.search(name)
            if not pm:
                continue
            yr = int(pm.group(1))
            per = pm.group(2).upper()
            fm = _FORM_RE.search(name)
            form = fm.group(1).upper() if fm else "Q"   # bare period -> assume 10-Q
            # track latest period across everything (FY counts as Q4 for ordering)
            q_ord = 4 if per == "FY" else int(per[1])
            if latest is None or (yr, q_ord) > latest:
                latest = (yr, q_ord)
            if per == "FY" or form == "K":
                has_10k = True
            else:
                q_by_year.setdefault(yr, set()).add(int(per[1]))
    except OSError:
        pass

    period_label = f"{latest[0]}-Q{latest[1]}" if latest else None
    # "stub" = the ticker has filings (html) but no USABLE Markdown body — either
    # the .md is absent (md==0) or it's an empty stub (largest .md < threshold).
    # Both mean "not readable in Obsidian" and want the same fix (regenerate MD).
    stub_md = bool(html and (md == 0 or md_max < _STUB_MAX_BYTES))
    return {
        "latest_period": period_label,
        "newest_epoch": round(newest, 0) if newest else 0,
        "html": html,
        "md": md,
        "stub_md": stub_md,
        "has_10k": has_10k,
        "missing": _missing_quarters(q_by_year),
        "n_quarters": sum(len(v) for v in q_by_year.values()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Report EDGAR corpus completeness to TradeHunter.")
    ap.add_argument("--dry-run", action="store_true", help="scan + build but don't POST")
    ap.add_argument("--out", help="dump the report JSON to this file")
    ap.add_argument("--limit", type=int, default=0, help="only the first N tickers")
    args = ap.parse_args()

    cfg = _load_env()
    root = (cfg.get("TST_EDGAR_DIR") or _DEFAULT_ROOT).strip()
    key = (cfg.get("TST_INGEST_API_KEY") or "").strip()
    url = (cfg.get("TST_DASHBOARD_URL") or "http://localhost:8000").rstrip("/")
    if not os.path.isdir(root):
        print(f"ABORT: TST_EDGAR_DIR missing/not a dir: {root!r}")
        return 1
    if not key and not args.dry_run:
        print("ABORT: TST_INGEST_API_KEY not set (use --dry-run to test without POST)")
        return 1

    tickers = sorted(
        e.name for e in os.scandir(root)
        if e.is_dir() and not e.name.startswith(("_", "."))
    )
    if args.limit:
        tickers = tickers[: args.limit]
    print(f"# corpus root : {root}")
    print(f"# tickers     : {len(tickers)}")

    rows = []
    for sym in tickers:
        rows.append({"ticker": sym, **_scan_ticker(os.path.join(root, sym))})

    report = {
        "host": socket.gethostname(),
        "root": root,
        "generated_epoch": round(time.time(), 0),
        "tickers": rows,
        "log_tail": [
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"scanned {len(rows)} tickers"
        ],
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"# wrote report -> {args.out}")
    if args.dry_run:
        gaps = sum(1 for r in rows if r["missing"])
        stub = sum(1 for r in rows if r["stub_md"])
        no10k = sum(1 for r in rows if not r["has_10k"])
        print(f"# dry-run: {len(rows)} tickers — {gaps} with missing quarters, "
              f"{stub} stub-MD, {no10k} with no 10-K")
        return 0

    body = json.dumps(report).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/ingest/edgar", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"OK {resp.status} {resp.read(200).decode('utf-8', 'replace')}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"POST failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
