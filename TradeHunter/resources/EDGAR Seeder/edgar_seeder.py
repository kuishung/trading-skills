"""EDGAR Seeder — fetch SEC earnings filings (10-Q / 10-K) into a versioned,
Obsidian-friendly corpus, with real incremental "fetch only what's new".

Stdlib only (no pip install). Talks to SEC's REST API directly. See
_edgar_common.py for the compliance notes (real User-Agent + <=10 req/s).

Subcommands
-----------
  seed     bulk historical pull for the given tickers (default since 2011)
  update   incremental — only filings newer than what's already seeded
  tickers  resolve ticker -> CIK (sanity check before a big run)

Examples
--------
  # one-time historical seed
  py "resources/EDGAR Seeder/edgar_seeder.py" seed --tickers AAPL MSFT NVDA \\
      --email you@firm.com --company "Your Name"

  # nightly/weekly "get new earnings" — only fetches filings not yet seeded
  py "resources/EDGAR Seeder/edgar_seeder.py" update --tickers-file resources/universe_full.txt

  # update everything already in the corpus
  py "resources/EDGAR Seeder/edgar_seeder.py" update

Contact (required by SEC) resolves from --email/--company, EDGAR_EMAIL /
EDGAR_COMPANY env, or a config.json "edgar": {"email","company"} block.

Output corpus defaults to <data_root>/edgar/<TICKER>/... (override with --out).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import _edgar_common as ec   # noqa: E402


def _read_tickers(args) -> list[str]:
    out: list[str] = []
    if args.tickers:
        out += [t.strip().upper() for t in args.tickers if t.strip()]
    if args.tickers_file:
        p = Path(args.tickers_file)
        if not p.exists():
            sys.exit(f"--tickers-file not found: {p}")
        out += [ln.strip().upper() for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    # de-dupe, preserve order
    seen: set[str] = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


def _seed_one(ticker: str, cik: int, ua: str, root: Path, manifest: dict,
              forms: tuple[str, ...], since: str, force: bool) -> dict:
    """Fetch + write all `forms` for one ticker filed on/after `since`, skipping
    accessions already in the manifest (unless force). Returns counters."""
    stats = {"written": 0, "skipped": 0, "errors": 0}
    seen_acc = set((manifest.get(ticker) or {}).keys())
    # labels already used by some accession -> avoid silent overwrite on restated periods
    used_labels = {v.get("label"): acc
                   for acc, v in (manifest.get(ticker) or {}).items()}

    try:
        fye, filings = ec.fetch_filings(cik, ua, forms, since)
    except Exception as exc:
        print(f"    ! submissions fetch failed: {type(exc).__name__}: {exc}")
        stats["errors"] += 1
        return stats

    # oldest-first so labels collide deterministically (original keeps base name)
    filings.sort(key=lambda f: f.filing_date)
    for f in filings:
        if not force and f.accession in seen_acc:
            stats["skipped"] += 1
            continue
        if not f.primary_doc:
            print(f"    ! {f.accession} {f.form}: no primary document, skipped")
            stats["errors"] += 1
            continue
        label = ec.fiscal_label(f.form, f.report_date, fye)
        # restatement / amended period collision -> suffix with accession tail
        if label in used_labels and used_labels[label] != f.accession:
            label = f"{label}_{f.acc_nodash[-6:]}"
        url = ec.ARCHIVE_DOC_URL.format(cik=f.cik, acc=f.acc_nodash, doc=f.primary_doc)
        try:
            html_bytes = ec.http_get(url, ua, binary=True)
        except Exception as exc:
            print(f"    ! {f.accession} {f.form} {label}: download failed "
                  f"({type(exc).__name__}: {exc})")
            stats["errors"] += 1
            continue
        text = ec.html_to_text(html_bytes)
        html_name, md_name = ec.write_report(root, ticker, f, label, html_bytes, text)
        manifest.setdefault(ticker, {})[f.accession] = {
            "form": f.form,
            "filing_date": f.filing_date,
            "report_date": f.report_date,
            "label": label,
            "html": html_name,
            "md": md_name,
            "chars": len(text),
            "seeded_at": ec.utcnow_iso(),
        }
        used_labels[label] = f.accession
        stats["written"] += 1
        flag = "" if text else "  (TEXT EXTRACTION FAILED)"
        print(f"    + {f.form} {label}  ({len(text):,} chars){flag}")
    return stats


def _run(tickers: list[str], ua: str, root: Path, forms: tuple[str, ...],
         since: str | None, force: bool, update_mode: bool,
         cache_dir: Path) -> int:
    if not tickers:
        sys.exit("No tickers. Pass --tickers / --tickers-file, or (update) seed some first.")
    print(f"# corpus root : {root}")
    print(f"# forms       : {','.join(forms)}")
    print(f"# tickers     : {len(tickers)}")
    print(f"# mode        : {'update (new only)' if update_mode else 'seed'}\n")

    cik_map = ec.load_cik_map(ua, cache_dir)
    manifest = ec.load_manifest(root)
    total = {"written": 0, "skipped": 0, "errors": 0, "unknown_ticker": 0}

    for i, t in enumerate(tickers, 1):
        cik = cik_map.get(t) or cik_map.get(t.replace(".", "-"))
        if cik is None:
            print(f"[{i}/{len(tickers)}] {t}: unknown to SEC ticker map — skipped")
            total["unknown_ticker"] += 1
            continue
        # update mode: per-ticker since = day after latest seeded filing_date
        t_since = since or ec.DEFAULT_SINCE
        if update_mode and not since:
            seeded = manifest.get(t) or {}
            if seeded:
                latest = max(v["filing_date"] for v in seeded.values())
                t_since = (datetime.strptime(latest, "%Y-%m-%d").date()
                           + timedelta(days=1)).isoformat()
        print(f"[{i}/{len(tickers)}] {t}  (CIK {cik})  since {t_since}")
        s = _seed_one(t, cik, ua, root, manifest, forms, t_since, force)
        for k in ("written", "skipped", "errors"):
            total[k] += s[k]
        # persist after each ticker so a long run is resumable / crash-safe
        ec.save_manifest(root, manifest)

    print("\n# done — "
          f"written {total['written']}  ·  skipped {total['skipped']}  ·  "
          f"errors {total['errors']}  ·  unknown {total['unknown_ticker']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch SEC earnings filings into a corpus.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--tickers", nargs="*", help="ticker symbols")
        p.add_argument("--tickers-file", help="file with one ticker per line (# comments ok)")
        p.add_argument("--forms", default="10-Q,10-K", help="comma forms (default 10-Q,10-K)")
        p.add_argument("--out", help="corpus root (default <data_root>/edgar)")
        p.add_argument("--email", help="SEC User-Agent contact email")
        p.add_argument("--company", help="SEC User-Agent company/name")
        p.add_argument("--force", action="store_true", help="re-download even if already seeded")

    ps = sub.add_parser("seed", help="bulk historical pull (default since 2011)")
    add_common(ps)
    ps.add_argument("--since", default=ec.DEFAULT_SINCE, help="earliest filing date YYYY-MM-DD")

    pu = sub.add_parser("update", help="incremental — only filings newer than seeded")
    add_common(pu)
    pu.add_argument("--since", default=None,
                    help="override the per-ticker auto cutoff (YYYY-MM-DD)")

    pt = sub.add_parser("tickers", help="resolve ticker -> CIK (no download)")
    pt.add_argument("tickers", nargs="+")
    pt.add_argument("--email"); pt.add_argument("--company")

    args = ap.parse_args()
    email, company = ec.resolve_contact(getattr(args, "email", None),
                                        getattr(args, "company", None))
    ua = ec.user_agent(email, company)

    if args.cmd == "tickers":
        root = ec.default_out_root()
        cmap = ec.load_cik_map(ua, root)
        for t in (x.upper() for x in args.tickers):
            cik = cmap.get(t) or cmap.get(t.replace(".", "-"))
            print(f"{t:8s} -> {('CIK %d' % cik) if cik else 'UNKNOWN'}")
        return 0

    root = Path(args.out).expanduser() if args.out else ec.default_out_root()
    forms = tuple(f.strip() for f in args.forms.split(",") if f.strip())
    tickers = _read_tickers(args)
    if args.cmd == "update" and not tickers:
        tickers = sorted((ec.load_manifest(root)).keys())
    return _run(tickers, ua, root, forms,
                since=args.since, force=args.force,
                update_mode=(args.cmd == "update"), cache_dir=root)


if __name__ == "__main__":
    sys.exit(main())
