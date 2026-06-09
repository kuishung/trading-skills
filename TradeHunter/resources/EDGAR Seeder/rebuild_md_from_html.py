"""rebuild_md_from_html.py — regenerate full-text Markdown from the HTML already
on disk.

WHY: the legacy `fetch_edgar.py` prototype left a corpus whose `.md` files are
245-byte stubs (no report body) — unusable in Obsidian. The full SEC primary
document IS on disk as the paired `.html`, so we can rebuild proper, full-text
`.md` files WITHOUT re-downloading anything from SEC. (Genuinely missing filings
— absent quarters, the never-fetched 10-Ks — still need `edgar_seeder.py`; this
only fixes the Markdown bodies for HTML we already have.)

For each `<TICKER>/<name>.html` it writes a sibling `<name>.md` with YAML
frontmatter + an Obsidian `[[wikilink]]` to the HTML + the extracted full text
(via the shared `_edgar_common.html_to_text`, which now drops the inline-XBRL
header so the body starts at the real report, not XBRL gibberish).

By default it only (re)writes stub/missing `.md`; `--force` rewrites all.

CLI (stdlib only — runs anywhere, no SEC contact needed):
  py "resources/EDGAR Seeder/rebuild_md_from_html.py" --root <corpus> [--dry-run]
  py "resources/EDGAR Seeder/rebuild_md_from_html.py" --root <corpus> --tickers AMD NVDA
  py "resources/EDGAR Seeder/rebuild_md_from_html.py" --root <corpus> --force
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import _edgar_common as ec   # noqa: E402

MIN_MD_BYTES = 600   # an existing .md at/below this is a stub -> rebuild it
_PERIOD_RE = re.compile(r"(20\d{2})[\-_](Q[1-4]|FY)", re.IGNORECASE)
_FORM_RE = re.compile(r"10[\-_]?([QK])", re.IGNORECASE)


def _meta_from_name(ticker: str, html_name: str) -> tuple[str, str]:
    """(period, form) parsed from a filename like `AMD_10Q_2011-Q2.html` or
    `MSFT_2026_Q1.html`. Period -> 'YYYY-Qn'/'YYYY-FY'; form -> '10-Q'/'10-K'."""
    pm = _PERIOD_RE.search(html_name)
    period = f"{pm.group(1)}-{pm.group(2).upper()}" if pm else "unknown"
    fm = _FORM_RE.search(html_name)
    form = f"10-{fm.group(1).upper()}" if fm else "10-Q"
    return period, form


def _build_md(ticker: str, html_name: str, period: str, form: str, text: str) -> str:
    body = text if text else ec.TEXT_FAIL_MARKER
    return (
        "---\n"
        "type: financial-filing\n"
        f"ticker: {ticker}\n"
        f"period: {period}\n"
        f"document: {form}\n"
        "source: rebuilt-from-html\n"
        "status: Rebuilt\n"
        "tags:\n"
        "  - equity-research\n"
        f"  - earnings-{form.lower().replace('-', '')}\n"
        f"  - {ticker.lower()}\n"
        "---\n\n"
        f"# {ticker} - {period} {form}\n"
        f"**Interactive Layout:** [[{html_name}]]\n\n"
        "## Summary & Key Metrics\n"
        "*(insert financial-metrics analysis here)*\n\n"
        "---\n\n"
        "## FULL REPORT CONTENT\n\n"
        f"{body}\n"
    )


def rebuild(root: Path, tickers: list[str] | None, force: bool,
            dry_run: bool) -> dict:
    stats = {"written": 0, "skipped_ok": 0, "empty_text": 0, "errors": 0,
             "tickers": 0}
    tdirs = ([root / t for t in tickers] if tickers
             else sorted((p for p in root.iterdir() if p.is_dir()
                          and not p.name.startswith(("_", "."))),
                         key=lambda p: p.name))
    for tdir in tdirs:
        if not tdir.is_dir():
            print(f"  ! {tdir.name}: no such ticker dir")
            continue
        stats["tickers"] += 1
        ticker = tdir.name
        for html_p in sorted(tdir.glob("*.htm*")):
            md_p = html_p.with_suffix(".md")
            if md_p.exists() and md_p.stat().st_size > MIN_MD_BYTES and not force:
                stats["skipped_ok"] += 1
                continue
            try:
                text = ec.html_to_text(html_p.read_bytes())
            except Exception as exc:
                print(f"  ! {ticker}/{html_p.name}: read/extract failed ({exc})")
                stats["errors"] += 1
                continue
            if not text:
                stats["empty_text"] += 1
            period, form = _meta_from_name(ticker, html_p.name)
            md = _build_md(ticker, html_p.name, period, form, text)
            if dry_run:
                print(f"  · would write {ticker}/{md_p.name}  "
                      f"({len(text):,} chars, {period} {form})")
            else:
                md_p.write_text(md, encoding="utf-8")
            stats["written"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild full-text .md from existing .html.")
    ap.add_argument("--root", help="corpus root (default <data_root>/edgar)")
    ap.add_argument("--tickers", nargs="*", help="only these tickers (default: all)")
    ap.add_argument("--force", action="store_true",
                    help="rewrite every .md, not just stubs/missing")
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    args = ap.parse_args()

    root = Path(args.root).expanduser() if args.root else ec.default_out_root()
    if not root.is_dir():
        sys.exit(f"corpus root not found: {root}")
    tickers = [t.strip().upper() for t in (args.tickers or []) if t.strip()] or None
    print(f"# corpus root : {root}")
    print(f"# mode        : {'DRY-RUN' if args.dry_run else 'write'}"
          f"{' (force all)' if args.force else ' (stubs/missing only)'}\n")
    s = rebuild(root, tickers, args.force, args.dry_run)
    print(f"\n# done — tickers {s['tickers']}  ·  "
          f"{'would write' if args.dry_run else 'wrote'} {s['written']}  ·  "
          f"skipped(ok) {s['skipped_ok']}  ·  empty-text {s['empty_text']}  ·  "
          f"errors {s['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
