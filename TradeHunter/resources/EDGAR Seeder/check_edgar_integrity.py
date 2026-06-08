"""Consistency / integrity audit for a seeded EDGAR corpus.

Read-only. Mirrors scripts/check_bars_integrity.py in spirit, but for the
earnings-filing corpus produced by edgar_seeder.py. Checks the manifest against
what's actually on disk and flags structural problems:

  - MISSING FILES   : manifest entry whose .html or .md is absent
  - EMPTY/TRUNCATED : .html < 2 KB or .md < 400 B (download / write went wrong)
  - TEXT FAILED     : md carries the "text extraction failed" marker (chars == 0)
  - NO FRONTMATTER  : md doesn't begin with a YAML frontmatter block
  - ORPHANS         : .html/.md on disk not referenced by the manifest
  - LABEL COLLISION : two accessions share one fiscal label for a ticker
  - PERIOD GAPS     : a ticker is missing an expected 10-Q quarter (Q1-Q3 per
                      fiscal year; Q4 is the 10-K, never a 10-Q) within its
                      seeded span — accounts for partial first/last years
  - STALE           : ticker's newest filing older than --stale-days (info)

CLI:
  py "resources/EDGAR Seeder/check_edgar_integrity.py"
  py "resources/EDGAR Seeder/check_edgar_integrity.py" --out <corpus> --json
  py "resources/EDGAR Seeder/check_edgar_integrity.py" --stale-days 120 --max-show 50
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import _edgar_common as ec   # noqa: E402

LABEL_RE = re.compile(r"^(\d{4})_(Q[1-4]|FY)")
MIN_HTML_BYTES = 2048
MIN_MD_BYTES = 400


def _parse_label(label: str):
    m = LABEL_RE.match(label or "")
    if not m:
        return None
    return int(m.group(1)), m.group(2)   # (year, 'Q3' | 'FY')


def _period_gaps(quarters_by_year: dict[int, set[int]]) -> list[str]:
    """Given {year: {1,2,3}} of 10-Q quarters present, list missing expected
    quarters. First/last seeded years are treated as partial (only expect
    quarters between the earliest and latest actually present)."""
    if not quarters_by_year:
        return []
    years = sorted(quarters_by_year)
    y0, y1 = years[0], years[-1]
    min_q_first = min(quarters_by_year[y0]) if quarters_by_year[y0] else 1
    max_q_last = max(quarters_by_year[y1]) if quarters_by_year[y1] else 3
    missing: list[str] = []
    for y in range(y0, y1 + 1):
        present = quarters_by_year.get(y, set())
        if y == y0:
            expected = {q for q in (1, 2, 3) if q >= min_q_first}
        elif y == y1:
            expected = {q for q in (1, 2, 3) if q <= max_q_last}
        else:
            expected = {1, 2, 3}
        for q in sorted(expected - present):
            missing.append(f"{y} Q{q}")
    return missing


def audit(root: Path, stale_days: int) -> dict:
    manifest = ec.load_manifest(root)
    now = datetime.now(timezone.utc)
    rep: dict = {
        "root": str(root), "tickers": len(manifest),
        "total_filings": 0,
        "missing_files": [], "empty": [], "text_failed": [],
        "no_frontmatter": [], "orphans": [], "label_collisions": [],
        "period_gaps": {}, "stale": [], "ok": 0,
    }

    referenced: dict[str, set[str]] = {}     # ticker -> {filenames in manifest}

    for ticker, entries in sorted(manifest.items()):
        tdir = root / ticker
        referenced[ticker] = set()
        labels_seen: dict[str, str] = {}     # label -> accession
        quarters_by_year: dict[int, set[int]] = {}
        latest_filing = ""

        for acc, e in entries.items():
            rep["total_filings"] += 1
            html_name = e.get("html", "")
            md_name = e.get("md", "")
            referenced[ticker].update({html_name, md_name})
            html_p = tdir / html_name
            md_p = tdir / md_name
            latest_filing = max(latest_filing, e.get("filing_date", ""))

            # missing
            miss = [n for n, p in ((html_name, html_p), (md_name, md_p)) if not p.exists()]
            if miss:
                rep["missing_files"].append(f"{ticker} {acc}: {', '.join(miss)}")
                continue
            # empty / truncated
            if html_p.stat().st_size < MIN_HTML_BYTES or md_p.stat().st_size < MIN_MD_BYTES:
                rep["empty"].append(
                    f"{ticker} {e.get('label', acc)}: "
                    f"html={html_p.stat().st_size}B md={md_p.stat().st_size}B")
            # text-extraction failure
            if int(e.get("chars", 0)) == 0:
                rep["text_failed"].append(f"{ticker} {e.get('label', acc)}")
            else:
                try:
                    head = md_p.read_text(encoding="utf-8")[:400]
                    if ec.TEXT_FAIL_MARKER in head:
                        rep["text_failed"].append(f"{ticker} {e.get('label', acc)}")
                except Exception:
                    pass
            # frontmatter
            try:
                if not md_p.read_text(encoding="utf-8").lstrip().startswith("---"):
                    rep["no_frontmatter"].append(f"{ticker} {e.get('label', acc)}")
            except Exception:
                rep["no_frontmatter"].append(f"{ticker} {e.get('label', acc)} (unreadable)")
            # label collision
            label = e.get("label", "")
            if label in labels_seen and labels_seen[label] != acc:
                rep["label_collisions"].append(
                    f"{ticker} {label}: {labels_seen[label]} & {acc}")
            labels_seen[label] = acc
            # quarter coverage (10-Q only)
            parsed = _parse_label(label)
            if parsed and e.get("form") == "10-Q":
                yr, per = parsed
                if per.startswith("Q"):
                    quarters_by_year.setdefault(yr, set()).add(int(per[1]))
            rep["ok"] += 1

        # period gaps
        gaps = _period_gaps(quarters_by_year)
        if gaps:
            rep["period_gaps"][ticker] = gaps
        # staleness
        if latest_filing:
            try:
                age = (now - datetime.strptime(latest_filing, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc)).days
                if age > stale_days:
                    rep["stale"].append(f"{ticker}: newest {latest_filing} ({age}d)")
            except ValueError:
                pass

        # orphans on disk
        if tdir.exists():
            for f in tdir.iterdir():
                if f.suffix.lower() in (".html", ".md") and f.name not in referenced[ticker]:
                    rep["orphans"].append(f"{ticker}/{f.name}")

    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="corpus root (default <data_root>/edgar)")
    ap.add_argument("--stale-days", type=int, default=120,
                    help="flag a ticker whose newest filing is older (default 120)")
    ap.add_argument("--max-show", type=int, default=25)
    ap.add_argument("--json", action="store_true",
                    help="also write <root>/_edgar_health.json for the dashboard")
    args = ap.parse_args()

    root = Path(args.out).expanduser() if args.out else ec.default_out_root()
    if not root.exists():
        print(f"# corpus root does not exist: {root}\n# nothing seeded yet.")
        return 0
    print(f"# auditing EDGAR corpus: {root}\n")
    rep = audit(root, args.stale_days)
    N = args.max_show

    def show(label, items, fmt=str):
        if not items:
            return
        print(f"  {label}: {len(items)}")
        for it in items[:N]:
            print(f"      {fmt(it)}")
        if len(items) > N:
            print(f"      ... +{len(items) - N} more")

    print(f"tickers={rep['tickers']}  filings={rep['total_filings']}  "
          f"OK={rep['ok']}")
    print("===== structural issues =====")
    show("MISSING FILES", rep["missing_files"])
    show("EMPTY / TRUNCATED", rep["empty"])
    show("TEXT EXTRACTION FAILED", rep["text_failed"])
    show("NO FRONTMATTER", rep["no_frontmatter"])
    show("LABEL COLLISIONS", rep["label_collisions"])
    show("ORPHAN FILES (on disk, not in manifest)", rep["orphans"])

    print("\n===== period coverage (missing 10-Q quarters) =====")
    if rep["period_gaps"]:
        shown = 0
        for tkr, gaps in sorted(rep["period_gaps"].items()):
            print(f"  {tkr}: {', '.join(gaps[:N])}" + (" ..." if len(gaps) > N else ""))
            shown += 1
            if shown >= N:
                print(f"  ... +{len(rep['period_gaps']) - shown} more tickers")
                break
    else:
        print("  no quarter gaps detected")

    show("\nSTALE (newest filing older than threshold)", rep["stale"])

    total_issues = (len(rep["missing_files"]) + len(rep["empty"]) +
                    len(rep["text_failed"]) + len(rep["no_frontmatter"]) +
                    len(rep["label_collisions"]) + len(rep["orphans"]) +
                    sum(len(v) for v in rep["period_gaps"].values()))
    status = "clean" if total_issues == 0 else "issues"
    rep["status"] = status
    rep["total_issues"] = total_issues
    rep["ts"] = ec.utcnow_iso()
    print(f"\n# {status.upper()} — {total_issues} issue(s), {len(rep['stale'])} stale")

    if args.json:
        out = root / "_edgar_health.json"
        out.write_text(json.dumps(rep, indent=2, sort_keys=True), encoding="utf-8")
        print(f"# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
