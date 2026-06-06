"""regen_profiles.py — unified profile-regen runner (intraday and/or swing).

ONE entry point used by three callers:
  - the nightly supervisor      -> intraday, full universe (after the ingest)
  - a weekly scheduled job       -> swing, full universe
  - the dashboard_tst triggers   -> full regen OR ad-hoc per-ticker, no Hermes login

Both profiles build offline from the seeded parquet store (no network):
  intraday -> ticker_profile.refresh_profile(t, source="store")
  swing    -> swing_profile.refresh_swing_profile / refresh_all_swing

CLI:
  py -3.12 scripts/regen_profiles.py intraday --all
  py -3.12 scripts/regen_profiles.py swing --all
  py -3.12 scripts/regen_profiles.py both --all
  py -3.12 scripts/regen_profiles.py intraday NVDA TSLA      # ad-hoc per ticker
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
for _p in [str(_root), str(_root / "scripts"), str(_root / "resources")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bars_store                       # noqa: E402
import ticker_profile                  # noqa: E402
import swing_profile                   # noqa: E402

KINDS = ("intraday", "swing", "both")


def _universe() -> list[str]:
    return bars_store.list_symbols("daily")


def regen_intraday(tickers: list[str], log=print) -> dict:
    written, skipped = 0, 0
    for i, t in enumerate(tickers, 1):
        try:
            p = ticker_profile.refresh_profile(t, source="store")
            written += 1 if p else 0
            skipped += 0 if p else 1
        except Exception as exc:
            skipped += 1
            log(f"  intraday {t}: {type(exc).__name__}: {exc}")
        if i % 200 == 0:
            log(f"  intraday {i}/{len(tickers)} ...")
    return {"written": written, "skipped": skipped}


def regen_swing(tickers: list[str], full_universe: bool, log=print) -> dict:
    if full_universe:
        # full run includes the cross-sectional RS percentile pass
        return swing_profile.refresh_all_swing(tickers, log=log)
    written, skipped = 0, 0
    for t in tickers:
        try:
            p = swing_profile.refresh_swing_profile(t)
            written += 1 if p else 0
            skipped += 0 if p else 1
        except Exception as exc:
            skipped += 1
            log(f"  swing {t}: {type(exc).__name__}: {exc}")
    return {"written": written, "skipped": skipped}


def regen(kind: str, tickers: list[str] | None = None, log=print) -> dict:
    """Run a profile regen. `tickers=None` => full daily universe.
    Returns a summary dict (also the per-run manifest payload)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    full = tickers is None
    syms = _universe() if full else [t.upper() for t in tickers]
    started = datetime.now(timezone.utc)
    t0 = time.time()
    log(f"[regen] kind={kind} scope={'all' if full else f'{len(syms)} tickers'} "
        f"({len(syms)} symbols)")

    phases: dict[str, dict] = {}
    if kind in ("intraday", "both"):
        ti = time.time()
        phases["intraday"] = regen_intraday(syms, log)
        phases["intraday"]["duration_s"] = round(time.time() - ti, 1)
    if kind in ("swing", "both"):
        ts = time.time()
        phases["swing"] = regen_swing(syms, full, log)
        phases["swing"]["duration_s"] = round(time.time() - ts, 1)

    out = {
        "kind": kind,
        "scope": "all" if full else "tickers",
        "n_symbols": len(syms),
        "tickers": None if full else syms,
        "phases": phases,
        "started": started.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(time.time() - t0, 1),
    }
    log(f"[regen] DONE in {out['duration_s']}s: "
        + "; ".join(f"{k} +{v.get('written',0)}/-{v.get('skipped',0)}"
                    for k, v in phases.items()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("tickers", nargs="*", help="symbols (omit with --all)")
    ap.add_argument("--all", action="store_true", help="full daily universe")
    args = ap.parse_args()
    if not args.all and not args.tickers:
        ap.error("give tickers, or --all")
    import json
    res = regen(args.kind, None if args.all else args.tickers)
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
