"""The Options module's nightly job and the on-demand Refresh
(OPTIONS_MODULE_DESIGN.md II.2.16; Part A §A4.1-A4.5, A4.7).

``run_nightly`` is the seven-step order the contract fixes, once per run::

    0  job_runs.start(db, "nightly", run_on, source=)      a row at once: a crash leaves
                                                            "started, never finished"
    1  option_backfill.backfill (default; --no-backfill)   idempotent
    2  universe = active basket symbols + open trades;      hashes = the house hash + every
       hashes = prefs_by_hash(db)                           DISTINCT saved prefs hash
    3  per symbol, sequential, the source's pacing between fetches (1.5 s Cboe / 0.4 s
       Alpaca): fetch the chain (fresh, 3 retries) -> bars + earnings (soft) ->
       option_metrics.all_for -> replace_snapshot -> upsert_iv_daily -> chart_state.read
       ONCE -> option_engine.compute + upsert_signal PER HASH -> commit
    4  option_exits.sweep(db)                               tonight's checks, BEFORE the push
    5  option_store.prune(db, run_on)                       snapshots 90 d, checks 90 d, pushes 45 d
    6  telegram_push.run(db, as_of=run_on, dry_run=)        soft-fail; "the push is step 6"
    7  job_runs.finish                                      counts + per-symbol detail + pushed

Soft-fail per ticker at every letter: a ``ChainError`` records ``status=error`` for that
symbol and the loop moves on; a Yahoo failure leaves ``hv*`` / ``earnings_*`` None and the
chain is still stored; an engine exception is written as ``option_signal.status="error"``
for THAT hash (the other hashes still run) and the data rows are kept - a card that says
the rules could not run tonight over fresh data beats a blank. Steps 4, 5 and 6 are each
caught on their own. The run returns normally whenever step 0 succeeded, so the deploy
script exits 0 on "a normal Tuesday" (a missing chain is not a failed job).

Engines that have not landed on a checkout (``chart_state`` / ``option_engine`` are built
by another step) are detected once and the run degrades to snapshot + metrics only, the
same thing ``engines=False`` (the CLI's ``--no-engines``) asks for on purpose.

``refresh_symbol`` is the same per-symbol pipeline for ``POST /options/refresh/{symbol}``:
``kind="intraday"``, no retries (a page request never sits through a backoff), the house
hash plus the caller's, and an ``option_jobs(job="refresh")`` row. ``refresh_allowed`` is
the 60 s per (member, ticker) cooldown the route applies - bypassed only by the ticket's
"Refresh first" press.

Re-running the same day is idempotent by construction: the snapshot is replaced per
``(symbol, snap_on, kind)``, ``iv_daily`` upserts on ``(symbol, on)``, the signal upserts
per hash, the push dedupes on its own rows; only ``option_jobs`` gains a row per run.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time

from ..models import UserOptionPrefs
from . import (clock, ema_setup, job_runs, option_data, option_metrics, option_prefs,
               option_store, prices)
from .option_quotes import ChainError

log = logging.getLogger(__name__)

JOB_NIGHTLY = "nightly"
JOB_REFRESH = "refresh"
RETRIES_NIGHTLY = 3           # Cboe 429 backoff inside option_quotes; the job can wait
RETRIES_REFRESH = 0           # a page request cannot
REFRESH_COOLDOWN_S = 60       # per (member, ticker); II.2.16
DEFAULT_PACING_S = 1.5        # when a source carries no capabilities (a test double)

_engines_warned = False
_last_refresh: dict[tuple[int | None, str], float] = {}


# ───────────────────────────────────── small helpers ─────────────────────────────────────

def _day(d) -> str:
    """``YYYY-MM-DD`` from a date / datetime / string; raises on garbage so a typo in
    ``--on`` fails before the job row is written."""
    if isinstance(d, _dt.datetime):
        return d.date().isoformat()
    if isinstance(d, _dt.date):
        return d.isoformat()
    return _dt.date.fromisoformat(str(d)[:10]).isoformat()


def _clean_symbols(symbols) -> list[str]:
    out: list[str] = []
    for s in symbols or ():
        s = str(s or "").strip().upper()
        if s and s not in out:
            out.append(s)
    return out


def _source(source):
    """The ``ChainSource`` for a run: an instance is used as given (the tests' double,
    or a caller that built one); a name or None goes through ``option_data.source()``,
    which raises ``ChainError`` on an unknown name - BEFORE any job row exists."""
    if source is None or isinstance(source, str):
        return option_data.source(source)
    return source


def _pacing(src) -> float:
    caps = getattr(src, "capabilities", None)
    try:
        return max(0.0, float(getattr(caps, "pacing_seconds", DEFAULT_PACING_S)))
    except (TypeError, ValueError):
        return DEFAULT_PACING_S


def _engines():
    """``((chart_state, option_engine), None)`` or ``(None, why)`` when either module
    has not landed on this checkout. Imported lazily on purpose: the data half of the
    job must run without them."""
    try:
        from . import chart_state, option_engine     # noqa: PLC0415 - lazy by design
    except Exception as exc:  # noqa: BLE001 - ImportError, or a module-level error inside
        return None, f"{type(exc).__name__}: {exc}"
    return (chart_state, option_engine), None


def _error_signal(message: str) -> dict:
    """What ``upsert_signal`` stores for a hash whose engines failed: the status, the
    message, nothing else (the card reads "the rules could not run tonight")."""
    return {"status": "error", "error": str(message)[:500], "headline": None, "setup": None,
            "iv": None, "strategies": None, "picks": None, "computed_ms": None}


def _universe(db, symbols=None) -> list[str]:
    """The symbols a run walks: the CLI's list when given, else every ACTIVE basket
    symbol over all owners plus every symbol with an OPEN ``option_trades`` row."""
    explicit = _clean_symbols(symbols)
    return explicit if explicit else option_store.basket_universe(db)


def prefs_by_hash(db) -> list[tuple[str, dict]]:
    """``[(prefs_hash, merged prefs)]`` for every DISTINCT rule set in use - the house
    hash first (``option_prefs.clean({})``), then each saved hash with the merged
    blocks of a row that carries it. A row whose stored hash no longer matches its
    recomputed one (the house defaults changed after it was saved) is written under
    BOTH, so the member's next read finds a row either way."""
    out: list[tuple[str, dict]] = [(option_prefs.HOUSE_HASH, option_prefs.clean({}))]
    seen = {option_prefs.HOUSE_HASH}
    for h in option_prefs.distinct_hashes(db):
        if not h or h in seen:
            continue
        row = (db.query(UserOptionPrefs).filter(UserOptionPrefs.prefs_hash == h)
                 .order_by(UserOptionPrefs.id).first())
        prefs = option_prefs.clean(getattr(row, "prefs", None) or {})
        out.append((h, prefs))
        seen.add(h)
        recomputed = option_prefs.prefs_hash(prefs)
        if recomputed != h and recomputed not in seen:
            log.warning("prefs hash %s (user_option_prefs #%s) recomputes as %s - the house "
                        "defaults changed since it was saved; writing both",
                        h, getattr(row, "id", "?"), recomputed)
            out.append((recomputed, prefs))
            seen.add(recomputed)
    return out


def _bars_for(symbol: str) -> tuple[list[dict], list[dict], str | None]:
    """``(bars 2y, long_bars deep, error)`` from ONE Yahoo fetch (the deep range,
    sliced to two years the way ``ema_setup.setup_for(deep=True)`` does, so the daily
    conditions match the page's). Soft-fail: ``([], [], why)``."""
    try:
        long_bars = prices.fetch_daily_ohlc(symbol, rng=getattr(ema_setup, "DEEP_RANGE", "10y")) or []
        bars = ema_setup._last_two_years(long_bars) if long_bars else []
        return bars, long_bars, None
    except Exception as exc:  # noqa: BLE001 - Yahoo down is not a failed symbol
        return [], [], f"{type(exc).__name__}: {exc}"


def _earnings_for(symbol: str) -> tuple[dict | None, str | None]:
    try:
        return prices.fetch_next_earnings(symbol), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _fetch(src, symbol: str, *, retries: int, fallback: bool):
    """The chain, fresh. With ``fallback`` (the configured source, no override) the
    module-level ``option_data.fetch_chain`` is used so ``TST_OPTIONS_FALLBACK`` applies;
    an explicit source is asked directly and fails on its own."""
    if fallback:
        return option_data.fetch_chain(symbol, fresh=True, retries=retries)
    return src.fetch_chain(symbol, fresh=True, retries=retries)


# ───────────────────────────────── the per-symbol pipeline ─────────────────────────────────

def _signals(db, chain, metrics, bars, long_bars, hashes) -> dict:
    """Step 3f: ``chart_state.read`` ONCE per symbol, then ``option_engine.compute`` +
    ``upsert_signal`` per hash. Returns ``{signals, engine_errors, engine_err}``; a
    chart-state failure writes an error row for EVERY hash (the picker has nothing
    to pick against), a compute failure only for its own hash."""
    global _engines_warned
    out = {"signals": 0, "engine_errors": 0, "engine_err": None}
    mods, why = _engines()
    if mods is None:
        if not _engines_warned:
            log.warning("engines unavailable (%s): snapshot + metrics only, no signal rows", why)
            _engines_warned = True
        out["engine_err"] = "engines unavailable: " + why
        return out
    chart_state, option_engine = mods
    sym = chain.symbol
    expiries = chain.expiries()
    state, state_err = None, None
    try:
        state = chart_state.read(sym, bars=bars, long_bars=long_bars, today=chain.snap_on,
                                 expiries=expiries)
    except Exception as exc:  # noqa: BLE001
        state_err = f"chart_state.read: {type(exc).__name__}: {exc}"
        log.warning("%-6s %s", sym, state_err, exc_info=True)
    for h, prefs in hashes:
        if state_err:
            sig = _error_signal(state_err)
            out["engine_errors"] += 1
        else:
            try:
                sig = option_engine.compute(chain, metrics, state, prefs)
            except Exception as exc:  # noqa: BLE001 - this hash only
                sig = _error_signal(f"option_engine.compute: {type(exc).__name__}: {exc}")
                out["engine_errors"] += 1
                log.warning("%-6s engine failed for prefs %s: %s", sym, h, exc, exc_info=True)
        option_store.upsert_signal(db, sig, prefs_hash=h, symbol=sym, snap_on=chain.snap_on,
                                   kind=chain.kind, as_of=chain.as_of, user_id=None)
        out["signals"] += 1
    if state_err:
        out["engine_err"] = state_err
    return out


def process_symbol(db, symbol: str, *, src, run_on: str, kind: str = "eod",
                   retries: int = RETRIES_NIGHTLY, hashes=(), engines: bool = True,
                   fallback: bool = True, snap_on: str | None = None) -> dict:
    """Step 3 for ONE symbol (A4.2 a-h). Returns the detail dict the job row keeps::

        {status: ok|error, ms, n (snapshot rows), expiries, signals, engine_errors,
         err, engine_err, bars_err, earnings_err, snap_on, as_of, source, partial, note}

    ``kind`` files the snapshot (``eod`` nightly, ``intraday`` on Refresh); ``snap_on``
    refiles the chain under that date (the CLI's ``--on``), otherwise the chain's own
    ET date stands. Commits on success; a ``ChainError`` returns ``status=error`` with
    nothing written. Any other exception propagates (the caller rolls back)."""
    t0 = time.perf_counter()
    sym = str(symbol or "").strip().upper()
    d: dict = {"status": "ok", "ms": 0, "n": 0, "expiries": 0, "signals": 0, "engine_errors": 0,
               "err": None, "engine_err": None, "bars_err": None, "earnings_err": None,
               "snap_on": None, "as_of": None, "source": None, "partial": False, "note": None}
    try:
        chain = _fetch(src, sym, retries=retries, fallback=fallback)
    except ChainError as exc:
        d.update(status="error", err=str(exc)[:300], ms=int((time.perf_counter() - t0) * 1000))
        log.warning("%-6s skipped: %s", sym, exc)
        return d
    if kind and chain.kind != kind:
        chain.kind = kind
    if snap_on:
        chain.snap_on = _day(snap_on)       # --on: the rows keep the feed's dte (one day off at most)

    bars, long_bars, d["bars_err"] = _bars_for(sym)
    earnings, d["earnings_err"] = _earnings_for(sym)
    series = option_store.iv_series_rows(db, sym, option_store.IV_SERIES_N, until=chain.snap_on)
    metrics = option_metrics.all_for(chain, bars, earnings, series, today=chain.snap_on)

    d["n"] = option_store.replace_snapshot(db, chain)
    option_store.upsert_iv_daily(db, chain, metrics)
    d.update(expiries=chain.n_expiries, snap_on=chain.snap_on, source=chain.source,
             partial=bool(chain.partial), note=(chain.note or None),
             as_of=chain.as_of.isoformat(timespec="seconds") if chain.as_of else None)
    if engines and hashes:
        d.update(_signals(db, chain, metrics, bars, long_bars, list(hashes)))
    db.commit()
    d["ms"] = int((time.perf_counter() - t0) * 1000)
    extra = ""
    if d["engine_errors"]:
        extra += f", {d['engine_errors']} engine error(s)"
    if d["bars_err"]:
        extra += ", no bars"
    if d["partial"]:
        extra += ", partial"
    log.info("%-6s ok %s rows, %d expiries, %d signal(s)%s %.1fs", sym, f"{d['n']:,}",
             d["expiries"], d["signals"], extra, d["ms"] / 1000.0)
    return d


# ─────────────────────────────────────── the nightly run ───────────────────────────────────────

def _sweep(db) -> dict:
    """Step 4, soft-fail; ``option_exits`` lands with the engines."""
    try:
        from . import option_exits          # noqa: PLC0415 - lazy by design
    except Exception as exc:  # noqa: BLE001
        log.info("exit sweep skipped: option_exits unavailable (%s)", exc)
        return {"skipped": f"unavailable: {type(exc).__name__}: {exc}"}
    try:
        res = option_exits.sweep(db)
        return res if isinstance(res, dict) else {"result": res}
    except Exception as exc:  # noqa: BLE001
        log.warning("exit sweep failed: %s", exc, exc_info=True)
        db.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}


def _prune(db, run_on: str) -> dict:
    """Step 5, soft-fail (``prune`` commits on its own)."""
    try:
        return option_store.prune(db, run_on)
    except Exception as exc:  # noqa: BLE001
        log.warning("prune failed: %s", exc, exc_info=True)
        db.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}


def _push(db, run_on: str, dry_run: bool) -> dict:
    """Step 6, soft-fail: the push's own dict, or ``{error}``."""
    try:
        from . import telegram_push         # noqa: PLC0415 - keeps the sender's imports off the data path
        res = telegram_push.run(db, as_of=run_on, dry_run=dry_run)
        return res if isinstance(res, dict) else {"sent": int(res or 0)}
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram push failed: %s", exc, exc_info=True)
        db.rollback()
        return {"error": f"{type(exc).__name__}: {exc}", "sent": 0}


def _skip_words(skipped: dict) -> str:
    return ", ".join(f"{k} x{v}" for k, v in sorted((skipped or {}).items())) or "none"


def run_nightly(db, *, symbols=None, on=None, source=None, push: bool = True,
                telegram_dry_run: bool = False, backfill: bool = True, engines: bool = True,
                progress=None, log=None, sleep=time.sleep) -> dict:
    """The seven-step nightly run (module docstring). ``symbols`` limits the universe;
    ``on`` refiles everything under that ET date; ``source`` is a name
    (``"cboe"`` / ``"alpaca"``) or a ``ChainSource`` instance - a bad name raises
    ``ChainError`` before any row is written, which the CLI turns into exit 1;
    ``push=False`` skips Telegram entirely, ``telegram_dry_run`` composes and logs
    without sending; ``engines=False`` stores the snapshot and metrics only;
    ``progress(sym, err)`` is the CLI's per-symbol hook; ``sleep`` is the pacing
    seam (the tests pass a no-op).

    Returns the summary (the ``spread_scan.run_scan`` shape)::

        {run_on, source, job_id, symbols, ok, errors, rows, signals, engine_errors,
         engines, pushed, telegram, swept, pruned, backfilled, elapsed_s, detail}

    ``detail`` is what the job row keeps: one dict per symbol under its symbol, and
    the run-level parts under ``_engines``, ``_backfill``, ``_sweep``, ``_prune``,
    ``_telegram`` (a leading underscore marks "not a symbol"). It is re-saved on the
    job row after EVERY symbol, so a killed run still shows how far it got."""
    lg = log if log is not None else logging.getLogger(__name__)
    t_run = time.perf_counter()
    src = _source(source)                                   # a bad name fails HERE (exit 1)
    run_on = _day(on) if on else clock.et_today()
    use_fallback = source is None

    # 0 - the row first, so a crash leaves "started, never finished"
    job = job_runs.start(db, JOB_NIGHTLY, run_on, source=getattr(src, "name", None))
    detail: dict = {}
    counts = {"ok": 0, "errors": 0, "rows": 0, "signals": 0, "engine_errors": 0}
    universe: list[str] = []
    pushed = 0
    note = None
    try:
        # 1 - the universe first, so the IV-history backfill is scoped to it: the
        #     screener's iv_history copied in, then a year from IB Gateway for whoever
        #     is still short (services/option_backfill). On by default since v4.131 -
        #     a ticker's first night gets its rank, not a 60-day wait. The rank is
        #     recomputed here too, so a symbol whose chain read then fails in step 3
        #     still carries a rank consistent with the history that landed.
        universe = _universe(db, symbols)
        if backfill:
            try:
                from . import option_backfill        # noqa: PLC0415 - lazy: optional on an old checkout
                detail["_backfill"] = option_backfill.backfill(db, universe, seed=True, recompute=True,
                                                               today=run_on, log=lg,
                                                               client_id=option_backfill.SEED_CLIENT_ID_NIGHTLY)
            except Exception as exc:  # noqa: BLE001
                lg.warning("backfill failed: %s", exc, exc_info=True)
                db.rollback()
                detail["_backfill"] = {"error": f"{type(exc).__name__}: {exc}"}

        # 2
        hashes: list[tuple[str, dict]] = []
        engines_mode = "off"
        if engines:
            mods, why = _engines()
            if mods is None:
                lg.warning("engines unavailable (%s): running snapshot + metrics only", why)
                engines_mode = "unavailable"
                engines = False
            else:
                hashes = prefs_by_hash(db)
                engines_mode = "on"
        detail["_engines"] = {"mode": engines_mode, "hashes": [h for h, _ in hashes]}
        job.symbols = len(universe)
        lg.info("nightly %s: %d symbol(s) via %s, %d rule set(s), engines %s", run_on,
                len(universe), getattr(src, "name", "?"), len(hashes), engines_mode)

        # 3
        pacing = _pacing(src)
        for i, sym in enumerate(universe):
            if i and pacing > 0:
                sleep(pacing)
            try:
                d = process_symbol(db, sym, src=src, run_on=run_on, kind="eod",
                                   retries=RETRIES_NIGHTLY, hashes=hashes, engines=engines,
                                   fallback=use_fallback, snap_on=run_on if on else None)
            except Exception as exc:  # noqa: BLE001 - a write failure must not end the run
                db.rollback()
                d = {"status": "error", "err": f"{type(exc).__name__}: {str(exc)[:280]}", "n": 0,
                     "signals": 0, "engine_errors": 0}
                lg.warning("%-6s failed: %s", sym, exc, exc_info=True)
            detail[sym] = d
            if d.get("status") == "ok":
                counts["ok"] += 1
            else:
                counts["errors"] += 1
            counts["rows"] += int(d.get("n") or 0)
            counts["signals"] += int(d.get("signals") or 0)
            counts["engine_errors"] += int(d.get("engine_errors") or 0)
            try:
                job.detail = dict(detail)                   # progress survives a kill
                job.ok, job.errors, job.rows = counts["ok"], counts["errors"], counts["rows"]
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            if progress:
                progress(sym, d.get("err"))

        # 4
        detail["_sweep"] = _sweep(db)
        # 5
        detail["_prune"] = _prune(db, run_on)
        # 6
        if push:
            tg = _push(db, run_on, telegram_dry_run)
            pushed = int(tg.get("sent") or 0)
            lg.info("telegram: pushed %d, composed %d, failed %d, skipped %s", pushed,
                    int(tg.get("ideas") or 0), int(tg.get("failed") or 0),
                    _skip_words(tg.get("skipped") or {}))
        else:
            tg = {"skipped": {"no-push": 1}, "sent": 0}
            lg.info("telegram: skipped (--no-push)")
        detail["_telegram"] = tg
    except Exception as exc:  # noqa: BLE001 - step 0 succeeded: record, do not crash the deploy script
        lg.exception("nightly run failed after %d symbol(s)", counts["ok"] + counts["errors"])
        db.rollback()
        note = f"failed: {type(exc).__name__}: {str(exc)[:200]}"
        detail["_error"] = note

    # 7
    if note is None:
        note = "%d/%d ok, %d signal row(s), engines %s, pushed %d" % (
            counts["ok"], len(universe), counts["signals"], detail.get("_engines", {}).get("mode", "?"), pushed)
    job_runs.finish(db, job, ok=counts["ok"], errors=counts["errors"], rows=counts["rows"],
                    pushed=pushed, note=note, detail=detail, symbols=len(universe))
    elapsed = time.perf_counter() - t_run
    lg.info("nightly %s: %d symbols, %d ok, %d errors, %s rows, %d signal(s), pushed %d, %.0fs",
            run_on, len(universe), counts["ok"], counts["errors"], f"{counts['rows']:,}",
            counts["signals"], pushed, elapsed)
    return {"run_on": run_on, "source": getattr(src, "name", None), "job_id": job.id,
            "symbols": len(universe), "ok": counts["ok"], "errors": counts["errors"],
            "rows": counts["rows"], "signals": counts["signals"],
            "engine_errors": counts["engine_errors"],
            "engines": detail.get("_engines", {}).get("mode"), "pushed": pushed,
            "telegram": detail.get("_telegram"), "swept": detail.get("_sweep"),
            "pruned": detail.get("_prune"), "backfilled": detail.get("_backfill"),
            "elapsed_s": round(elapsed, 1), "detail": detail}


# ───────────────────────────────────────── refresh ─────────────────────────────────────────

def refresh_allowed(user, symbol: str, *, bypass: bool = False) -> tuple[bool, int]:
    """The 60 s cooldown per (member, ticker) of II.2.16, kept in-process (the quote
    cache would answer the same chain anyway). Returns ``(allowed, seconds_left)``;
    an allowed call stamps the time. ``bypass`` is the ticket's "Refresh first"
    press, which is never blocked but still stamps."""
    key = (getattr(user, "id", None), str(symbol or "").strip().upper())
    now = time.monotonic()
    last = _last_refresh.get(key)
    if not bypass and last is not None and now - last < REFRESH_COOLDOWN_S:
        return False, int(REFRESH_COOLDOWN_S - (now - last)) + 1
    _last_refresh[key] = now
    return True, 0


def refresh_symbol(db, symbol: str, user, *, source=None) -> dict:
    """``POST /options/refresh/{symbol}`` (A4.7): the per-symbol pipeline with
    ``kind="intraday"``, ``fresh=True``, ``retries=0``, computing the house signal AND
    the caller's own hash, under an ``option_jobs(job="refresh")`` row. Returns the
    per-symbol detail dict plus ``symbol``, ``run_on``, ``job_id``; the route renders
    the card from ``option_store.card_for`` afterwards. Never raises for a missing
    chain (``status=error`` + ``err``)."""
    sym = str(symbol or "").strip().upper()
    src = _source(source)
    run_on = clock.et_today()
    job = job_runs.start(db, JOB_REFRESH, run_on, source=getattr(src, "name", None))
    hashes: list[tuple[str, dict]] = [(option_prefs.HOUSE_HASH, option_prefs.clean({}))]
    if user is not None:
        try:
            prefs = option_prefs.read(db, user)
            h = option_prefs.prefs_hash(prefs)
            if h != option_prefs.HOUSE_HASH:
                hashes.append((h, prefs))
        except Exception as exc:  # noqa: BLE001 - the house row still refreshes
            log.warning("refresh %s: could not read user %s prefs: %s", sym, getattr(user, "id", "?"), exc)
    try:
        d = process_symbol(db, sym, src=src, run_on=run_on, kind="intraday",
                           retries=RETRIES_REFRESH, hashes=hashes, engines=True,
                           fallback=source is None)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        d = {"status": "error", "err": f"{type(exc).__name__}: {str(exc)[:280]}", "n": 0,
             "signals": 0, "engine_errors": 0}
        log.warning("refresh %s failed: %s", sym, exc, exc_info=True)
    ok = d.get("status") == "ok"
    job_runs.finish(db, job, ok=int(ok), errors=int(not ok), rows=int(d.get("n") or 0),
                    detail={sym: d, "_user": getattr(user, "id", None),
                            "_hashes": [h for h, _ in hashes]},
                    symbols=1, note=(d.get("err") if not ok else
                                     f"{d.get('n', 0)} rows, {d.get('signals', 0)} signal row(s)"))
    d.update(symbol=sym, run_on=run_on, job_id=job.id, hashes=[h for h, _ in hashes])
    return d
