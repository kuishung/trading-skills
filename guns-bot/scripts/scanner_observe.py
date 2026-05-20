"""IBKR scanner subscriber for intraday_bot — observe-only.

Subscribes to an IBKR market scanner and logs the live ranking every 30s
into state/events_YYYY-MM-DD.jsonl. The dashboard reads those events and
renders the scanner panel. **Does not trade.** No watchlist mutation, no
order submission. Pure observation so we can compare scanner output to
the user's manual intuition before letting it drive entries.

Default scan: TOP_PERC_GAIN among US major exchanges, price ≥ $1.50.
The "right" scan code is empirical — start here, swap via --scan-code.

Other useful scan codes:
  TOP_PERC_GAIN          % gainers across the session
  HOT_BY_VOLUME          relative volume surge
  TOP_OPEN_PERC_GAIN     leaders since the open (use after 9:30 ET)
  TOP_TRADE_RATE         momentum by trade count
  HIGH_VS_52W_HL         near 52-week extremes

Connection notes:
  - clientId 80 chosen to avoid the bot (71) and dashboard probe (99).
  - Read-only API enforced — never tries to send an order via IBKR.
  - Disconnects cleanly on Ctrl-C / SIGTERM so TWS doesn't accumulate
    CloseWait sockets (see the IBKR API probe lesson in SKILL.md).

Run:
    py scripts/scanner_observe.py
    py scripts/scanner_observe.py --scan-code HOT_BY_VOLUME --rows 25
"""
from __future__ import annotations

# Python 3.14 / eventkit shim — install a default event loop before the
# ib_insync import path touches asyncio.get_event_loop().
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ib_insync import IB, ScannerSubscription, Stock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _events import emit  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent


def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        return pytz.timezone("America/New_York")


def _now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(_et_tz())


def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def _today_et_at(hhmm: str) -> datetime:
    hh, mm = _parse_hhmm(hhmm)
    now = _now_et()
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _load_cfg() -> dict:
    path = SKILL_DIR / "config.json"
    if not path.exists():
        sys.exit(f"config.json missing at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _row_to_dict(rank: int, row) -> dict:
    """Flatten a ScanData entry. IBKR returns rank, contractDetails (with a
    Contract), distance, benchmark, projection, legsStr. distance/benchmark/
    projection are scan-code-specific strings — passed through verbatim."""
    c = row.contractDetails.contract
    return {
        "rank": int(rank),
        "symbol": c.symbol,
        "currency": c.currency or "USD",
        "primary_exchange": c.primaryExchange or "",
        "distance": str(row.distance or ""),
        "benchmark": str(row.benchmark or ""),
        "projection": str(row.projection or ""),
    }


def _safe_float(x) -> float | None:
    """Coerce to float, returning None for None / NaN / un-coercible."""
    if x is None:
        return None
    try:
        f = float(x)
        if f != f:   # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


# Module-level quote cache keyed by symbol -> {"ts": float, "data": dict}
# TTL chosen to be just over the 30s scanner cycle so the same symbol seen
# across multiple parallel scanners reuses one fetch.
_QUOTE_CACHE: dict[str, dict] = {}
QUOTE_TTL_S = 35.0


def _fetch_quote_snapshots(ib: IB, symbols: list[str]) -> dict[str, dict]:
    """Batch IBKR snapshot mktData for `symbols`. Returns a dict
    {symbol: {"last", "prev_close", "change_pct", "day_volume"}}.

    Cached entries inside the TTL window are reused. The snapshot mode
    auto-releases each market-data line after data arrives, so concurrency
    against the 100-line cap is short-lived.

    Failures are swallowed — a per-symbol failure just leaves it absent
    from the result dict.
    """
    now = time.time()
    fresh: list[str] = []
    out: dict[str, dict] = {}
    for s in symbols:
        cached = _QUOTE_CACHE.get(s)
        if cached and (now - cached["ts"]) < QUOTE_TTL_S:
            out[s] = cached["data"]
        else:
            fresh.append(s)
    if not fresh:
        return out

    tickers: list[tuple[str, object]] = []
    for s in fresh:
        try:
            contract = Stock(s, "SMART", "USD")
            t = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
            tickers.append((s, t))
        except Exception:
            continue
    # Snapshot takes up to ~11s to fully populate; 5s is enough for
    # last + prev_close + volume on liquid US equities.
    ib.sleep(5)

    for s, t in tickers:
        last = _safe_float(getattr(t, "last", None))
        if last is None:
            # Fallback chain: marketPrice() -> close
            try:
                mp = t.marketPrice()
                last = _safe_float(mp)
            except Exception:
                last = None
        prev_close = _safe_float(getattr(t, "close", None))
        vol = _safe_float(getattr(t, "volume", None))
        # IBKR returns US-stock volume in 100-share lots historically.
        # We'll show as-is; if downstream needs raw shares, multiply *100.
        change_pct = None
        if last is not None and prev_close is not None and prev_close > 0:
            change_pct = (last - prev_close) / prev_close * 100.0
        data = {
            "last": last,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "day_volume": vol,
        }
        out[s] = data
        _QUOTE_CACHE[s] = {"ts": now, "data": data}
    return out


# Parallel-scanner architecture: subscribe to all of these from session
# start. trade_day.py reads the snapshot at 09:35 to build the ORB "Stocks
# in Play" universe. Scanners that need data you don't have access to
# (e.g. HOT_BY_OPT_VOLUME without options data sub) skip gracefully with
# a warning rather than killing the process.
DEFAULT_PARALLEL_SCANNERS = (
    # Window-baseline rankers — each makes sense in a specific regime,
    # but we keep them all running so consumers don't have to re-subscribe.
    "TOP_PERC_GAIN",                           # PM baseline (vs prior close)
    "TOP_OPEN_PERC_GAIN",                      # RTH baseline (vs today's open)
    "HOT_BY_VOLUME",                           # late-RTH baseline
    "TOP_STOCK_BUY_IMBALANCE_ADV_RATIO",       # closing-window baseline (15:50+)
    # Always-on leading signals — time-invariant, useful in every regime.
    "TOP_VOLUME_RATE",                         # leading: volume velocity
    "HOT_BY_OPT_VOLUME",                       # leading: options-flow tell
    # Always-on defensive — used as a reject set, not as a candidate source.
    "HALTED",
)


def _make_subscription(scan_code: str, location: str, rows: int,
                       above_price: float, above_volume: int) -> ScannerSubscription:
    sub = ScannerSubscription(
        instrument="STK",
        locationCode=location,
        scanCode=scan_code,
        numberOfRows=rows,
        abovePrice=above_price,
    )
    if above_volume > 0:
        sub.aboveVolume = above_volume
    return sub


def main() -> None:
    ap = argparse.ArgumentParser(description="Observe-only IBKR scanner")
    ap.add_argument("--scan-code", default="auto",
                    help="IBKR scan code(s). Default 'auto' = subscribe to "
                         "the DEFAULT_PARALLEL_SCANNERS list (7 scanners "
                         "in parallel). Comma-separate to override with a "
                         "specific subset, e.g. 'TOP_PERC_GAIN,HALTED'.")
    ap.add_argument("--location-code", default="STK.US.MAJOR",
                    help="IBKR location code (default: STK.US.MAJOR)")
    ap.add_argument("--rows", type=int, default=50,
                    help="Number of rows to track per scanner (default: 50). "
                         "Bumped from 20 to surface mid-cap movers (e.g. ALAB) "
                         "that get buried on small-cap-rip days where the top "
                         "of TOP_PERC_GAIN is filled with 100%+ micro-caps.")
    ap.add_argument("--above-price", type=float, default=1.50,
                    help="Minimum price filter (default: 1.50)")
    ap.add_argument("--above-volume", type=int, default=0,
                    help="Minimum daily volume filter; 0 disables (default: 0)")
    ap.add_argument("--interval", type=int, default=30,
                    help="Seconds between snapshots (default: 30)")
    ap.add_argument("--client-id", type=int, default=80,
                    help="IBKR clientId (default: 80, distinct from bot 71)")
    ap.add_argument("--start-et", default="09:00",
                    help="Start scanning at this ET wall-clock (HH:MM). "
                         "Before this time the process idles. Default 09:00 — "
                         "30 minutes before the US market open.")
    ap.add_argument("--end-et", default="10:30",
                    help="Stop scanning at this ET wall-clock (HH:MM) and "
                         "exit cleanly. Default 10:30 — matches the bot's "
                         "Setup-1 entry cutoff.")
    args = ap.parse_args()

    # ---------- Time gate: wait until start_et, refuse if past end_et ----------
    start_dt = _today_et_at(args.start_et)
    end_dt = _today_et_at(args.end_et)
    now = _now_et()
    if end_dt <= now:
        print(f"end-et {args.end_et} already passed (now ET {now.strftime('%H:%M')}). "
              f"Nothing to do — exit.")
        emit("scanner.skipped", {
            "reason": "end_et already passed",
            "start_et": args.start_et, "end_et": args.end_et,
            "now_et": now.strftime("%H:%M"),
        })
        return
    if now < start_dt:
        wait_s = (start_dt - now).total_seconds()
        print(f"Idle until {args.start_et} ET — waiting {int(wait_s)}s "
              f"(now ET {now.strftime('%H:%M:%S')}).")
        emit("scanner.waiting", {
            "until_et": args.start_et,
            "wait_s": int(wait_s),
        })
        # Sleep in chunks so signals interrupt promptly.
        stop_during_wait = [False]

        def _wait_signal(_sig, _frame):
            stop_during_wait[0] = True

        signal.signal(signal.SIGINT, _wait_signal)
        try:
            signal.signal(signal.SIGTERM, _wait_signal)
        except Exception:
            pass
        while not stop_during_wait[0] and _now_et() < start_dt:
            time.sleep(min(15, max(1, (start_dt - _now_et()).total_seconds())))
        if stop_during_wait[0]:
            emit("scanner.stop", {"reason": "interrupted while waiting"})
            print("Interrupted while waiting.")
            return

    cfg = _load_cfg()
    host = cfg.get("ibkr_host", "127.0.0.1")
    port = int(cfg.get("ibkr_port", 7497))

    ib = IB()
    print(f"Connecting to {host}:{port} clientId={args.client_id} ...")
    try:
        ib.connect(host, port, clientId=args.client_id, timeout=8, readonly=True)
    except Exception as exc:
        emit("scanner.error", {"phase": "connect", "error": str(exc)})
        sys.exit(f"FAIL connect: {exc}")

    # ---- Determine which scan codes to subscribe to.
    if args.scan_code != "auto":
        scan_codes = [c.strip() for c in args.scan_code.split(",") if c.strip()]
    else:
        scan_codes = list(DEFAULT_PARALLEL_SCANNERS)

    print(f"Subscribing to {len(scan_codes)} scanner(s) in parallel: "
          f"{', '.join(scan_codes)}")

    scanners: list[dict] = []
    for code in scan_codes:
        try:
            sub = _make_subscription(code, args.location_code, args.rows,
                                     args.above_price, args.above_volume)
            scan_data = ib.reqScannerSubscription(sub, [], [])
            scanners.append({
                "code": code,
                "scan_data": scan_data,
                "prev_symbols": set(),
                "last_snapshot_by_sym": {},
            })
            emit("scanner.subscribed", {
                "scan_code": code,
                "location_code": args.location_code,
                "rows": args.rows,
                "above_price": args.above_price,
            })
        except Exception as exc:
            # Common cause: scanner needs a data subscription you don't have
            # (e.g. options data for HOT_BY_OPT_VOLUME). Skip and continue.
            sys.stderr.write(f"scanner subscribe '{code}' failed: {exc}\n")
            emit("scanner.subscribe_failed", {"scan_code": code, "error": str(exc)})

    if not scanners:
        emit("scanner.error", {"phase": "no_active_subscriptions"})
        sys.exit("No scanners successfully subscribed — exit.")

    emit("scanner.start", {
        "scan_codes": [s["code"] for s in scanners],
        "location_code": args.location_code,
        "rows": args.rows,
        "above_price": args.above_price,
        "above_volume": args.above_volume or None,
        "interval_s": args.interval,
        "mode": "parallel",
    })

    # Graceful shutdown handler.
    _stop = {"flag": False}

    def _on_signal(_sig, _frame):
        _stop["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass  # SIGTERM not supported on some Windows configurations

    print(f"Observing until {args.end_et} ET — Ctrl-C to stop early.")
    try:
        while not _stop["flag"]:
            # Auto-stop at end_et.
            if _now_et() >= end_dt:
                emit("scanner.stop", {"reason": f"end_et {args.end_et} reached"})
                print(f"End-ET {args.end_et} reached — stopping.")
                break
            ib.sleep(args.interval)  # cooperative wait; ib_insync processes events
            if _stop["flag"]:
                break

            # ---- Snapshot each active scanner. ----
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            preview_chunks: list[str] = []

            # Pre-compute per-scanner row lists so we can dedupe symbols
            # across scanners before issuing the quote fetch (one fetch per
            # unique symbol per cycle).
            scanner_rows: list[tuple[dict, list[dict]]] = []
            unique_syms: list[str] = []
            seen_syms: set[str] = set()
            for s in scanners:
                rows = list(s["scan_data"])[: args.rows]
                current = [_row_to_dict(i + 1, r) for i, r in enumerate(rows)]
                scanner_rows.append((s, current))
                for r in current:
                    sym = r["symbol"]
                    # Skip non-stock placeholders (e.g. "FOO PRE" preferred-share
                    # listings the scanner occasionally returns) — Stock contract
                    # routing requires a clean symbol.
                    if sym and " " not in sym and sym not in seen_syms:
                        unique_syms.append(sym)
                        seen_syms.add(sym)

            # Enrich with snapshot mktData (last, prev_close, change%, volume).
            try:
                quotes = _fetch_quote_snapshots(ib, unique_syms) if unique_syms else {}
            except Exception as exc:
                sys.stderr.write(f"quote enrichment failed (continuing): {exc}\n")
                quotes = {}

            for s, current in scanner_rows:
                for r in current:
                    q = quotes.get(r["symbol"]) or {}
                    r["last"] = q.get("last")
                    r["prev_close"] = q.get("prev_close")
                    r["change_pct"] = q.get("change_pct")
                    r["day_volume"] = q.get("day_volume")
                current_by_sym = {r["symbol"]: r for r in current}

                emit("scanner.snapshot", {
                    "scan_code": s["code"],
                    "n": len(current),
                    "rows": current,
                })

                # Diff vs previous (per scanner): new entrants and drop-outs.
                curr_symbols = set(current_by_sym.keys())
                for sym in sorted(curr_symbols - s["prev_symbols"]):
                    emit("scanner.emit", {"scan_code": s["code"],
                                          **current_by_sym[sym]})
                for sym in sorted(s["prev_symbols"] - curr_symbols):
                    last_row = s["last_snapshot_by_sym"].get(sym, {"symbol": sym})
                    emit("scanner.drop", {
                        "scan_code": s["code"], "symbol": sym,
                        "last_rank": last_row.get("rank"),
                    })
                s["prev_symbols"] = curr_symbols
                s["last_snapshot_by_sym"] = current_by_sym

                top_syms = ",".join(r["symbol"] for r in current[:3]) or "—"
                preview_chunks.append(f"{s['code']}({len(current)}){'·' + top_syms if top_syms != '—' else ''}")
            print(f"[{ts}] " + " | ".join(preview_chunks))
    finally:
        for s in scanners:
            try:
                ib.cancelScannerSubscription(s["scan_data"])
            except Exception:
                pass
        try:
            ib.disconnect()
        except Exception:
            pass
        emit("scanner.stop", {"reason": "signal" if _stop["flag"] else "loop_exit"})
        print("Stopped.")


if __name__ == "__main__":
    main()
