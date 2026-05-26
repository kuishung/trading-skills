"""IBKR historical bars ingest -> data/price_history/.

Pulls OHLCV history from IB Gateway / TWS via ib_insync and writes
through `bars_store.write_bars()` (Parquet). Two flows:

  1. SEED   -- one-shot bulk lookback when first ingesting a symbol.
                py resources/ibkr_history.py ingest NVDA MSFT --timeframes 1min,daily --days 60
  2. UPDATE -- incremental, only bars after the last stored timestamp.
                py resources/ibkr_history.py update --universe
                py resources/ibkr_history.py update --symbols NVDA,MSFT --timeframes 1min,daily

IBKR pacing -- respected by spacing requests ~7 seconds apart by default
(under IB's 60-requests / 600-seconds cap). For ~50 symbols x 2
timeframes = 100 requests that's ~12 minutes. The orchestrator EOD-hook
calls `update --universe` which is normally only ~5-10 symbols since the
session, so it finishes in ~1-2 minutes.

Universe selection (the "most of the tickers" the user means):
- All symbols seen in `data/journal/` over the last N days (default 30).
- Plus today's GUNS watchlist if it exists.
- Plus any symbol explicitly in cfg["history_universe"] (manual additions).

Connection:
- Reuses `ibkr_data._connect()` semantics but holds ONE long-lived
  connection across the whole bulk to avoid IBKR's per-connect overhead.
- clientId 83 (intentional non-collision: 71 live bot, 80 observer,
  82 GUNS scanner, 98 probe, 99 dashboard).

CLI:
    py resources/ibkr_history.py ingest NVDA MSFT --timeframes 1min,daily --days 60
    py resources/ibkr_history.py update --universe
    py resources/ibkr_history.py update --symbols NVDA --timeframes 1min
    py resources/ibkr_history.py list                              # what we have stored
    py resources/ibkr_history.py universe                          # what we should have
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- intraday-bot bootstrap ---
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

import bars_store           # noqa: E402  resources/bars_store.py
from _common import load_config  # noqa: E402  scripts/_common.py

# History-ingest client id. Picked to NOT collide with:
#   71 live bot, 80 observer, 82 GUNS scanner, 98 probe, 99 dashboard
HISTORY_CLIENT_ID = 83
DEFAULT_PACING_S = 7.0   # seconds between IBKR requests; safe under 60/600s cap

# IBKR barSizeSetting + sensible max-duration-per-request mapping.
# IB historical-data pacing tightens for smaller bars; chunk accordingly.
TIMEFRAME_TO_IB = {
    "1min":  {"barSize": "1 min",  "chunk_days": 7,   "use_rth": False},
    "3min":  {"barSize": "3 mins", "chunk_days": 14,  "use_rth": False},
    "5min":  {"barSize": "5 mins", "chunk_days": 30,  "use_rth": False},
    "15min": {"barSize": "15 mins","chunk_days": 60,  "use_rth": False},
    "daily": {"barSize": "1 day",  "chunk_days": 365, "use_rth": True},
}


# ---------- IBKR connection ----------

def _connect(cfg: dict):
    """Open a long-lived IB connection for the bulk operation."""
    # Reuse the existing connection plumbing from ibkr_data.
    from ibkr_data import _connect as _base_connect, DEFAULT_CLIENT_ID
    # Override the client id to avoid collision with the live bot.
    cfg_local = dict(cfg or {})
    cfg_local["ibkr_client_id"] = HISTORY_CLIENT_ID
    return _base_connect(cfg_local)


def _stock(symbol: str):
    from ibkr_data import _stock as _base_stock
    return _base_stock(symbol)


def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        return pytz.timezone("America/New_York")


# ---------- Bar coercion ----------

def _ib_bar_to_dict(b) -> dict | None:
    """ib_insync BarData -> canonical {t,o,h,l,c,v} (ISO UTC string)."""
    t = b.date
    if isinstance(t, datetime):
        dt = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=_et_tz()).astimezone(timezone.utc)
    elif isinstance(t, (int, float)):
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
    else:
        # IB sometimes returns YYYYMMDD for daily bars (no time)
        try:
            dt = datetime.strptime(str(t)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return {
        "t": dt.isoformat(),
        "o": float(b.open),
        "h": float(b.high),
        "l": float(b.low),
        "c": float(b.close),
        "v": int(b.volume) if b.volume and b.volume > 0 else 0,
    }


# ---------- Single-request fetch ----------

def _fetch_chunk(ib, symbol: str, end_dt: datetime,
                 duration_str: str, timeframe: str) -> list[dict]:
    """One reqHistoricalData call. Returns bars in canonical shape."""
    spec = TIMEFRAME_TO_IB[timeframe]
    contract = _stock(symbol)
    try:
        ib.qualifyContracts(contract)
    except Exception as exc:
        sys.stderr.write(f"[ibkr_history] qualify({symbol}) failed: {exc}\n")
        bars_store.log_ingest_event(source="ibkr_history", symbol=symbol,
                                    timeframe=timeframe, bars_added=0,
                                    error=f"qualify_failed: {exc}")
        return []
    # Empty string = "now"; otherwise format "YYYYMMDD HH:MM:SS UTC"
    end_str = "" if end_dt is None else end_dt.strftime("%Y%m%d %H:%M:%S UTC")
    try:
        ib_bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_str,
            durationStr=duration_str,
            barSizeSetting=spec["barSize"],
            whatToShow="TRADES",
            useRTH=spec["use_rth"],
            formatDate=2,           # epoch seconds
            keepUpToDate=False,
        )
    except Exception as exc:
        sys.stderr.write(f"[ibkr_history] reqHistoricalData({symbol}, {timeframe}, {duration_str}) failed: {exc}\n")
        bars_store.log_ingest_event(source="ibkr_history", symbol=symbol,
                                    timeframe=timeframe, bars_added=0,
                                    error=f"reqHistoricalData_failed: {exc}")
        return []
    out: list[dict] = []
    for b in ib_bars or []:
        d = _ib_bar_to_dict(b)
        if d is not None:
            out.append(d)
    return out


# ---------- Public API ----------

def ingest_history(ib, symbol: str, timeframe: str,
                   *, lookback_days: int,
                   pacing_s: float = DEFAULT_PACING_S,
                   end_dt: datetime | None = None) -> int:
    """Bulk pull `lookback_days` of `timeframe` bars and write to bars_store.

    Chunks the request so each call stays under IBKR's per-bar-size duration
    cap. Pauses `pacing_s` seconds between chunks to respect IB's
    60-requests-per-600-seconds rule. Returns total bars written.
    """
    spec = TIMEFRAME_TO_IB[timeframe]
    chunk_days = spec["chunk_days"]
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    remaining = lookback_days
    total_written = 0
    cursor_end = end_dt
    while remaining > 0:
        this_chunk = min(chunk_days, remaining)
        duration_str = f"{this_chunk} D"
        bars = _fetch_chunk(ib, symbol, cursor_end, duration_str, timeframe)
        if bars:
            bars_store.write_bars(symbol, bars, timeframe=timeframe,
                                  source="ibkr_history")
            total_written += len(bars)
            # advance cursor backwards by chunk_days; use the earliest bar we got
            earliest = bars[0]["t"]
            try:
                cursor_end = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
            except ValueError:
                cursor_end = cursor_end - timedelta(days=this_chunk)
        else:
            cursor_end = cursor_end - timedelta(days=this_chunk)
        remaining -= this_chunk
        if remaining > 0:
            time.sleep(pacing_s)
    return total_written


def update_history(ib, symbol: str, timeframe: str,
                   *, max_lookback_days: int = 30,
                   pacing_s: float = DEFAULT_PACING_S) -> int:
    """Incremental: fetch only bars after the last stored timestamp.

    If we have no bars for this symbol, this is a no-op (the caller should
    use ingest_history with an explicit lookback). If the last stored bar
    is older than `max_lookback_days`, only the last `max_lookback_days`
    are fetched (caller should re-seed if they want deeper history).
    """
    rng = bars_store.available_range(symbol, timeframe=timeframe)
    if rng is None:
        sys.stderr.write(
            f"[ibkr_history] update({symbol}, {timeframe}): no existing data; "
            f"use ingest_history(..., lookback_days=N) to seed first.\n"
        )
        return 0
    _, last_iso = rng
    last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    gap_days = (now - last_dt).days
    if gap_days <= 0:
        gap_days = 1  # cover today's bars
    lookback = min(gap_days + 1, max_lookback_days)
    return ingest_history(ib, symbol, timeframe,
                          lookback_days=lookback, pacing_s=pacing_s)


def _classify_pair(sym: str, tf: str, target_days: int, force_seed: bool
                   ) -> tuple[str, str]:
    """LOCAL-ONLY classification (no IBKR calls). Decides what bulk_update
    needs to do for one (sym, tf) pair based solely on the parquet on disk.

    Returns (action, note) where action is one of:
        "seed"      no data on disk → full fetch needed
        "force"     force_seed=True → full re-fetch regardless of disk state
        "refill"    has data but depth < 90% of target → backfill
        "top_up"    has data, deep enough → only an incremental update is needed
        "unreadable" parquet timestamp couldn't be parsed → treat as refill

    Used by the pre-flight pass to plan work and surface a summary up front,
    so that on watcher restart the user sees immediately how many symbols are
    already complete and doesn't have to wonder whether the loop is "really
    doing something" while it slowly skips through fully-seeded names.
    """
    if force_seed:
        return "force", f"force_seed({target_days}d)"
    rng = bars_store.available_range(sym, timeframe=tf)
    if rng is None:
        return "seed", f"seed({target_days}d)"
    try:
        earliest = datetime.fromisoformat(rng[0].replace("Z", "+00:00"))
        days_back = (datetime.now(timezone.utc) - earliest).days
    except (ValueError, AttributeError):
        return "unreadable", "depth=unparseable"
    if days_back < int(target_days * 0.90):
        return "refill", f"depth={days_back}d<{target_days}d"
    return "top_up", f"depth={days_back}d ok"


def bulk_update(symbols: list[str], timeframes: list[str], cfg: dict | None = None,
                *, lookback_days_for_new: int = 60,
                lookback_days_by_tf: dict[str, int] | None = None,
                pacing_s: float = DEFAULT_PACING_S,
                force_seed: bool = False,
                skip_up_to_date: bool = False) -> dict[tuple[str, str], int]:
    """Update many (symbol, timeframe) pairs in one IBKR connection.

    Pre-flight pass (NO IBKR calls): classify every (sym, tf) into one of
    `seed` / `force` / `refill` / `top_up` / `unreadable` based on what's
    already in the parquet, log a summary, and build the work list. Then
    iterate ONLY the work items, with a `[i/N]` progress counter on each
    line so the user can see exactly where the run is.

    Per (sym, tf) work modes:

    1. **`seed`** — no data on disk → full `ingest_history(lookback_days=N)`
    2. **`force`** — `force_seed=True` → full re-seed regardless of disk
    3. **`refill`** — partial seed (depth < 90% target, e.g., previous run
       crashed mid-symbol) → full re-fetch to target depth. bars_store
       deduplicates on write, so existing chunks are safely overwritten.
    4. **`top_up`** — has data, deep enough → incremental update only.
       Included in the work list when `skip_up_to_date=False` (the
       orchestrator's post-EOD path wants today's new bars). Filtered out
       when `skip_up_to_date=True` (the watcher's bulk backfill path
       doesn't need today's incremental — that's what the post-EOD
       orchestrator job is for).

    Seed depth resolution: `lookback_days_by_tf[tf]` if set, else
    `lookback_days_for_new`.

    `lookback_days_by_tf` (added 2026-05-26) overrides
    `lookback_days_for_new` per timeframe — useful for mixed runs like
    `{"3min": 180, "5min": 180, "daily": 730}` where daily wants more
    depth than the intraday bars.

    `skip_up_to_date` (added 2026-05-26 — user request after observing
    that restart of the watcher iterated all 1518 symbols paying full
    pacing even though many were already at depth): when True, drop
    `top_up` pairs from the work list entirely. Default False preserves
    the existing orchestrator post-EOD behaviour.

    Returns: {(symbol, timeframe): bars_written}. Pairs that were skipped
    (top_up + skip_up_to_date=True) are NOT in the dict.
    """
    cfg = cfg or load_config()
    if not symbols:
        return {}

    # ---------- Pre-flight (no IBKR calls) ----------
    # Classify everything, log a summary, then build the work list. This
    # is what gives the user the "which has been done and which now" view
    # on every watcher restart.
    plan: list[tuple[str, str, str, str, int]] = []   # (sym, tf, action, note, target_days)
    counts: dict[str, int] = {"seed": 0, "force": 0, "refill": 0,
                              "top_up": 0, "unreadable": 0}
    already_done: list[tuple[str, str, int]] = []   # for the optional verbose listing
    for sym in symbols:
        for tf in timeframes:
            target_days = (lookback_days_by_tf or {}).get(tf, lookback_days_for_new)
            action, note = _classify_pair(sym, tf, target_days, force_seed)
            counts[action] = counts.get(action, 0) + 1
            if action == "top_up":
                # Track depth for the "already done" report. note format is
                # "depth=Nd ok"; pull out N for the report.
                try:
                    days_back = int(note.split("=", 1)[1].split("d", 1)[0])
                except (IndexError, ValueError):
                    days_back = -1
                already_done.append((sym, tf, days_back))
            plan.append((sym, tf, action, note, target_days))

    n_total = len(plan)
    work_items = [
        (sym, tf, action, note, target_days)
        for (sym, tf, action, note, target_days) in plan
        if not (skip_up_to_date and action == "top_up")
    ]
    n_work = len(work_items)
    n_skipped = n_total - n_work

    sys.stdout.write(
        f"[pre-flight] {n_total} (sym,tf) pairs scanned: "
        f"seed={counts['seed']} force={counts['force']} "
        f"refill={counts['refill']} top_up={counts['top_up']} "
        f"unreadable={counts['unreadable']}\n"
    )
    if n_skipped:
        sys.stdout.write(
            f"[pre-flight] skipping {n_skipped} top_up pairs already at full depth "
            f"(skip_up_to_date=True). Estimated time saved: ~{n_skipped * pacing_s / 60:.1f} min "
            f"of pacing.\n"
        )
    # Per-timeframe breakdown of already-done — gives the user a one-glance
    # view of which slot is the closest to complete.
    by_tf_done: dict[str, int] = {}
    by_tf_total: dict[str, int] = {}
    for (sym, tf, action, _note, _td) in plan:
        by_tf_total[tf] = by_tf_total.get(tf, 0) + 1
        if action == "top_up":
            by_tf_done[tf] = by_tf_done.get(tf, 0) + 1
    sys.stdout.write("[pre-flight] per-timeframe completion: ")
    parts = []
    for tf in timeframes:
        done = by_tf_done.get(tf, 0)
        tot = by_tf_total.get(tf, 0)
        parts.append(f"{tf}={done}/{tot}")
    sys.stdout.write(" ".join(parts) + "\n")
    sys.stdout.write(f"[pre-flight] {n_work} work items remaining\n")
    sys.stdout.flush()

    if n_work == 0:
        sys.stdout.write("[pre-flight] nothing to do — all pairs at full depth\n")
        return {}

    # ---------- Connect + execute ----------
    ib = _connect(cfg)
    results: dict[tuple[str, str], int] = {}
    reconnect_attempts = 0
    MAX_RECONNECT_ATTEMPTS = 20

    def _ensure_connected(current_ib):
        """If the connection has dropped (TWS reset, auto-logoff, transient),
        try to reconnect with exponential-ish backoff. Returns the live IB
        instance (possibly new), or raises after MAX attempts."""
        nonlocal reconnect_attempts
        if current_ib.isConnected():
            reconnect_attempts = 0
            return current_ib
        reconnect_attempts += 1
        wait_s = min(60, 5 * reconnect_attempts)
        sys.stderr.write(
            f"[ibkr_history] connection lost — reconnect attempt #{reconnect_attempts} "
            f"after {wait_s}s wait...\n"
        )
        sys.stderr.flush()
        time.sleep(wait_s)
        try: current_ib.disconnect()
        except Exception: pass
        new_ib = _connect(cfg)
        if new_ib.isConnected():
            sys.stderr.write("[ibkr_history] reconnected.\n")
            sys.stderr.flush()
            reconnect_attempts = 0
            return new_ib
        if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            raise RuntimeError(
                f"ibkr_history: failed to reconnect after {MAX_RECONNECT_ATTEMPTS} attempts"
            )
        return current_ib

    try:
        first = True
        for i, (sym, tf, action, note, target_days) in enumerate(work_items, start=1):
            if not first:
                time.sleep(pacing_s)
            first = False
            # Auto-reconnect on TWS drops — prevents the "spin forever
            # on qualify_failed: Not connected" failure mode that
            # required manual ingest restarts (user reports 2026-05-23).
            try:
                ib = _ensure_connected(ib)
            except Exception as exc:
                sys.stderr.write(f"[ibkr_history] {exc} — aborting run\n")
                break
            # Per-symbol try/except — one bad ticker (delisted, ticker
            # change, weird IBKR error, parquet write failure, etc.)
            # must NOT kill the entire 1519-symbol run. Log the failure,
            # record 0 bars in results, move on to the next symbol.
            # Added 2026-05-26 per user request: "let it run without
            # interruption". Pairs with the existing _ensure_connected
            # reconnect logic — reconnect failures still break (those
            # are hopeless), but per-symbol failures are skipped.
            progress = f"[{i}/{n_work}]"
            try:
                if action in ("seed", "force", "refill", "unreadable"):
                    n = ingest_history(ib, sym, tf,
                                       lookback_days=target_days,
                                       pacing_s=pacing_s)
                    if action == "force":
                        label = f"force-seed({target_days}d)"
                    elif action == "seed":
                        label = f"seed({target_days}d)"
                    elif action == "refill":
                        label = f"refill({note})"
                    else:
                        label = f"refill(unreadable→{target_days}d)"
                else:   # action == "top_up"
                    n = update_history(ib, sym, tf, pacing_s=pacing_s)
                    label = f"update({note})"
                results[(sym, tf)] = n
                sys.stdout.write(
                    f"  {progress:<12} {sym:<8} {tf:<6} {label:<22} +{n} bars\n"
                )
            except Exception as exc:
                err_str = f"{type(exc).__name__}: {str(exc)[:80]}"
                results[(sym, tf)] = 0
                sys.stderr.write(
                    f"  {progress:<12} {sym:<8} {tf:<6} FAILED: {err_str} — skipping\n"
                )
            sys.stdout.flush()
            sys.stderr.flush()
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return results


# ---------- Universe selection ----------

def universe_from_journal(days: int = 30) -> list[str]:
    """Symbols that appeared in any journal event in the last `days`."""
    from review.stats import journal_files_in_window, read_events
    files = journal_files_in_window(days)
    if not files:
        return []
    events = read_events(files)
    syms = {ev.get("symbol") for ev in events if ev.get("symbol")}
    return sorted(s for s in syms if s)


def universe_from_watchlist() -> list[str]:
    """Today's GUNS watchlist if it exists. Best-effort -- empty list on any issue."""
    try:
        from strategy.GUNS._helpers import guns_watchlist_path
    except Exception:
        return []
    today_iso = datetime.now(_et_tz()).strftime("%Y-%m-%d")
    p = guns_watchlist_path(today_iso)
    if not p.exists():
        return []
    try:
        syms: list[str] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # watchlist lines may be "SYM" or "SYM<tab>note"; take first token
            syms.append(line.split()[0].upper())
        return syms
    except Exception:
        return []


def build_universe(cfg: dict | None = None,
                   *, journal_days: int = 30) -> list[str]:
    """Combined universe: journal + watchlist + cfg.history_universe."""
    cfg = cfg or load_config()
    syms = set(universe_from_journal(journal_days))
    syms.update(universe_from_watchlist())
    for s in cfg.get("history_universe", []) or []:
        syms.add(s.upper())
    return sorted(syms)


# ---------- CLI ----------

def _parse_timeframes(s: str) -> list[str]:
    out = [t.strip() for t in s.split(",") if t.strip()]
    bad = [t for t in out if t not in bars_store.SUPPORTED_TIMEFRAMES]
    if bad:
        raise SystemExit(f"Unsupported timeframes: {bad}. "
                         f"Choose from {bars_store.SUPPORTED_TIMEFRAMES}.")
    return out


def _cmd_ingest(args) -> int:
    cfg = load_config()
    timeframes = _parse_timeframes(args.timeframes)
    symbols = [s.upper() for s in args.symbols]
    if not symbols:
        sys.stderr.write("ingest: at least one SYMBOL required\n")
        return 2
    ib = _connect(cfg)
    try:
        for sym in symbols:
            for tf in timeframes:
                n = ingest_history(ib, sym, tf,
                                   lookback_days=args.days,
                                   pacing_s=args.pacing)
                print(f"  {sym:<8} {tf:<6} seed({args.days}d) +{n} bars")
                sys.stdout.flush()
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return 0


def _cmd_update(args) -> int:
    cfg = load_config()
    timeframes = _parse_timeframes(args.timeframes)
    if args.universe:
        symbols = build_universe(cfg, journal_days=args.journal_days)
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols.split(",") if s.strip()]
    else:
        sys.stderr.write("update: pass --universe or --symbols S1,S2,...\n")
        return 2
    if not symbols:
        print("(universe is empty; nothing to update)")
        return 0
    print(f"# updating {len(symbols)} symbols x {len(timeframes)} timeframes "
          f"(~{len(symbols) * len(timeframes) * args.pacing:.0f}s pacing budget)")
    results = bulk_update(symbols, timeframes, cfg,
                          lookback_days_for_new=args.seed_days,
                          pacing_s=args.pacing,
                          force_seed=args.force_seed)
    total = sum(results.values())
    print(f"# DONE -- {total} bars written across {len(results)} (symbol, timeframe) pairs.")
    return 0


def _cmd_list(args) -> int:
    for tf in bars_store.SUPPORTED_TIMEFRAMES:
        syms = bars_store.list_symbols(tf)
        if not syms:
            print(f"[{tf}] (empty)")
            continue
        print(f"[{tf}] {len(syms)} symbols")
        for s in syms:
            rng = bars_store.available_range(s, timeframe=tf)
            if rng:
                print(f"    {s:<8} {rng[0]} -> {rng[1]}")
    return 0


def _cmd_universe(args) -> int:
    cfg = load_config()
    syms = build_universe(cfg, journal_days=args.journal_days)
    print(f"# Universe: {len(syms)} symbols (journal_days={args.journal_days})")
    for s in syms:
        print(s)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="seed N days of history for given symbols")
    p_ingest.add_argument("symbols", nargs="+")
    p_ingest.add_argument("--timeframes", default="1min,daily")
    p_ingest.add_argument("--days", type=int, default=60)
    p_ingest.add_argument("--pacing", type=float, default=DEFAULT_PACING_S)
    p_ingest.set_defaults(func=_cmd_ingest)

    p_update = sub.add_parser("update", help="incremental update of stored bars")
    g = p_update.add_mutually_exclusive_group()
    g.add_argument("--universe", action="store_true",
                   help="auto-derive symbols from journal+watchlist+cfg")
    g.add_argument("--symbols", help="comma-separated symbols")
    p_update.add_argument("--timeframes", default="1min,daily")
    p_update.add_argument("--journal-days", type=int, default=30,
                          help="how far back to look in journal for universe (default 30)")
    p_update.add_argument("--seed-days", type=int, default=60,
                          help="if symbol has no stored bars yet, seed N days (default 60)")
    p_update.add_argument("--pacing", type=float, default=DEFAULT_PACING_S)
    p_update.add_argument("--force-seed", action="store_true",
                          help="re-seed EVERY symbol with --seed-days of history, "
                               "even ones that already have bars. Use to extend "
                               "historical depth backward (e.g., bump 14d to 180d "
                               "for backtest). Existing bars are deduplicated on "
                               "write -- safe but slower than incremental update.")
    p_update.set_defaults(func=_cmd_update)

    p_list = sub.add_parser("list", help="show what's currently stored")
    p_list.set_defaults(func=_cmd_list)

    p_univ = sub.add_parser("universe", help="show the universe build_universe() would update")
    p_univ.add_argument("--journal-days", type=int, default=30)
    p_univ.set_defaults(func=_cmd_universe)

    return ap


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
