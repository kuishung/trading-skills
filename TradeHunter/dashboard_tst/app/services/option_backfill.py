"""First-time data backfill for an Options basket ticker.

User, 2026-10-06: *"when a new ticker is added in the option basket, all the data
backfill required to compute the result need to be backfill in for the first time."*

What a card needs that ACCUMULATES is the IV history behind the IV rank
(``iv_daily``: one 30-day implied-volatility reading per session - 20 for a
percentile, 60 for a trusted rank, 252 for the full-year window). Everything else a
card reads - daily bars, the earnings date, today's chain - is fetched live by
``option_nightly.process_symbol`` on every read, so nothing else needs filling.

Three sources, cheapest first; each only INSERTS the days not yet on file:

1. ``iv_history`` - the Spread screener's nightly Cboe readings (the S&P 500 + every
   member's watchlist + the MATP board, since 2026-09-13) plus whatever
   ``deploy/iv_seed_ibkr.py`` seeded. Copied through
   ``option_store.backfill_from_iv_history(symbols=...)``.
2. IB Gateway / TWS on THIS box - a year of daily implied volatility per symbol
   through that same seeder run as a subprocess (it needs ``ib_insync``, which cannot
   import on Python 3.14 - the CLAUDE.md rule - so the interpreter is checked first:
   this app's own, then ``py -3.12``, or ``TST_IBKR_PYTHON``). Attempted only for the
   symbols still short after step 1, and only when an IB API port answers a 1.5 s
   socket probe (``TST_IBKR_PORT``, else 4002, 4001, 7497, 7496 in turn - Hermes runs
   the Gateway under IBC on 4002). A box without a Gateway skips it in milliseconds
   with the reason recorded; a symbol whose seed failed or returned nothing is not
   retried for six hours. ``TST_IV_SEED_IBKR=0`` switches the step off. One seed runs
   at a time per process (a lock), and the web app and the nightly use different IB
   client ids (88 / 87) so they never collide.
3. the member's own TWS through the card's Live button (bridge 1.6,
   ``option_store.bootstrap_iv``) - unchanged, per ticker, from the browser.

``backfill(db, symbols)`` runs 1 and 2, recomputes today's rank for every symbol
that gained days (and marks its signal rows ``stale_iv`` so the next card read
recomputes the gauge) and writes an ``option_jobs(job='backfill')`` row when days
landed or a seed failed (the status strip shows its note). ``first_read(db, symbols,
user)`` is what a basket add starts in the background: ``backfill``, then per symbol
the same read the Refresh button does (today's delayed chain -> metrics -> engines ->
signal), so a freshly added ticker has a complete card in seconds, not tomorrow. The
nightly job calls ``backfill`` for its whole universe before step 2, so tickers added
before this shipped get the same treatment on their next night.

Soft-fail throughout: a failed seed or read is recorded and logged, never raised.
Portable ORM only (one GROUP BY per count; the copy is option_store's
query-then-insert, committed per symbol).
"""
from __future__ import annotations

import logging
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import func

from ..models import IVDaily, IVHistory
from . import job_runs, option_store
from .opt_constants import IV_RANK_MIN_OBS

log = logging.getLogger(__name__)

JOB = "backfill"
IB_HOST = "127.0.0.1"                     # the seeder dials localhost only
IB_PORT_CANDIDATES = (4002, 4001, 7497, 7496)   # Gateway paper / live, TWS paper / live
SEED_CLIENT_ID_NIGHTLY = 87               # deploy/iv_seed_ibkr.py's own default (CLAUDE.md allocation table)
SEED_CLIENT_ID_WEB = 88                   # the web app's first-time reads: never the nightly's id
SEEDER = Path(__file__).resolve().parents[2] / "deploy" / "iv_seed_ibkr.py"
APP_ROOT = SEEDER.parent.parent
PROBE_TIMEOUT_S = 1.5
SEED_BASE_TIMEOUT_S = 90                  # connect + handshake
SEED_PER_SYMBOL_S = 5                     # ~1 s request + 1 s pause each, with headroom
SEED_MAX_TIMEOUT_S = 600                  # whatever the batch, the seed never pins a run longer than this
SEED_RETRY_S = 6 * 3600                   # a symbol whose seed failed / returned nothing: not retried sooner
ENOUGH_HISTORY = 200                      # = the seeder's ENOUGH: this many iv_history rows is "a year on file"
PREFLIGHT_IMPORTS = "import ib_insync, sqlalchemy, httpx, alembic, dotenv"
PREFLIGHT_TTL_S = 600

_seed_lock = threading.Lock()             # one seeder subprocess at a time per process
_seed_memo: dict[str, float] = {}         # symbol -> monotonic time of the last fruitless seed
_preflight: dict[tuple, tuple[float, bool, str]] = {}


# ────────────────────────────────── settings ──────────────────────────────────

def seeder_enabled() -> bool:
    return os.environ.get("TST_IV_SEED_IBKR", "1").strip().lower() not in ("0", "off", "no", "false")


def ib_port() -> int | None:
    """``TST_IBKR_PORT`` when set and numeric, else None (= probe the candidates)."""
    raw = os.environ.get("TST_IBKR_PORT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def ib_ports() -> tuple[int, ...]:
    p = ib_port()
    return (p,) if p else IB_PORT_CANDIDATES


def python_candidates() -> list[list[str]]:
    """Interpreters to try for the seeder, in order: ``TST_IBKR_PYTHON`` alone when set
    (a path without spaces, or a launcher line such as ``py -3.12``); else this app's
    own interpreter (the Hermes venv is built with py -3.12), then the platform's 3.12."""
    raw = os.environ.get("TST_IBKR_PYTHON", "").strip()
    if raw:
        return [[raw] if " " not in raw else shlex.split(raw, posix=(os.name != "nt"))]
    return [[sys.executable], ["py", "-3.12"] if os.name == "nt" else ["python3.12"]]


# ────────────────────────────────── helpers ──────────────────────────────────

def _clean(symbols) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols or []:
        s = str(s or "").strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def counts(db, symbols, table=IVDaily) -> dict[str, int]:
    """``{symbol: days with an iv30}`` in ``iv_daily`` (default) or ``iv_history`` - one
    GROUP BY query; every requested symbol is a key (0 when absent)."""
    syms = _clean(symbols)
    out = {s: 0 for s in syms}
    if not syms:
        return out
    q = (db.query(table.symbol, func.count(table.on))
           .filter(table.symbol.in_(syms), table.iv30.isnot(None))
           .group_by(table.symbol))
    for s, n in q.all():
        out[str(s)] = int(n or 0)
    return out


def gateway_reachable(host: str = IB_HOST, port: int | None = None,
                      timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Does something listen on that IB API port? A plain TCP connect, closed at once."""
    port = port or ib_port() or IB_PORT_CANDIDATES[0]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gateway_port(timeout: float = PROBE_TIMEOUT_S) -> tuple[int | None, list[int]]:
    """The first IB API port that answers, and every port tried."""
    tried: list[int] = []
    for p in ib_ports():
        tried.append(p)
        if gateway_reachable(IB_HOST, p, timeout):
            return p, tried
    return None, tried


def preflight(cmd: list[str], *, timeout: float = 40.0) -> tuple[bool, str]:
    """Can ``cmd`` import ib_insync and the app's own packages? Cached ten minutes."""
    key = tuple(cmd)
    now = time.monotonic()
    hit = _preflight.get(key)
    if hit and now - hit[0] < PREFLIGHT_TTL_S:
        return hit[1], hit[2]
    try:
        p = subprocess.run([*cmd, "-c", PREFLIGHT_IMPORTS], capture_output=True, text=True,
                           timeout=timeout, check=False)
        ok = p.returncode == 0
        lines = [ln for ln in ((p.stderr or "") + "\n" + (p.stdout or "")).splitlines() if ln.strip()]
        why = "" if ok else (lines[-1] if lines else f"exit {p.returncode}")[:200]
    except FileNotFoundError:
        ok, why = False, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        ok, why = False, "the import check timed out"
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"{type(exc).__name__}: {str(exc)[:160]}"
    _preflight[key] = (now, ok, why)
    return ok, why


def resolve_ibkr_python() -> tuple[list[str] | None, str]:
    """The first candidate interpreter that passes ``preflight``, else ``(None, why)``."""
    whys: list[str] = []
    for cmd in python_candidates():
        ok, why = preflight(cmd)
        if ok:
            return cmd, ""
        whys.append(f"{' '.join(cmd)}: {why}")
    return None, ("no interpreter can run the IB seeder (" + "; ".join(whys)[:300]
                  + ") - py -3.12 -m pip install ib_insync sqlalchemy httpx alembic python-dotenv, "
                  "or set TST_IBKR_PYTHON")


def _db_url_for_child() -> str | None:
    """The database URL the seeder must use - the app's own, with a relative SQLite
    path made absolute so the child never resolves it against a different directory."""
    url = None
    try:
        from ..config import settings      # noqa: PLC0415
        url = getattr(settings, "database_url", None)
    except Exception:  # noqa: BLE001
        url = None
    url = url or os.environ.get("TST_DATABASE_URL")
    if not url:
        return None
    url = str(url)
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith("sqlite:////"):
        rel = url[len(prefix):]
        is_abs = rel.startswith("/") or (len(rel) > 1 and rel[1] == ":")
        if rel and not is_abs and not rel.startswith(":memory:"):
            url = prefix + Path(rel).resolve().as_posix()
    return url


def run_seeder(symbols, *, port: int, python: list[str] | None = None,
               client_id: int = SEED_CLIENT_ID_WEB, timeout: float | None = None) -> dict:
    """``deploy/iv_seed_ibkr.py <symbols> --port N --client-id K --no-init`` as a
    subprocess, cwd = dashboard_tst, the app's database URL in its environment.
    Returns ``{ok, rc, seconds, tail, err, cmd}``; never raises."""
    syms = _clean(symbols)
    py = list(python or ["py", "-3.12"])
    cmd = [*py, str(SEEDER), *syms, "--port", str(port), "--client-id", str(client_id), "--no-init"]
    timeout = timeout or min(SEED_MAX_TIMEOUT_S, SEED_BASE_TIMEOUT_S + SEED_PER_SYMBOL_S * len(syms))
    env = dict(os.environ)
    url = _db_url_for_child()
    if url:
        env["TST_DATABASE_URL"] = url
    t0 = time.perf_counter()
    out: dict = {"ok": False, "rc": None, "seconds": 0.0, "tail": "", "err": None,
                 "cmd": " ".join(py) + " " + SEEDER.name}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(APP_ROOT), env=env, check=False)
    except FileNotFoundError as exc:
        out["err"] = (f"{py[0]} not found ({exc}) - install Python 3.12 with ib_insync, "
                      "or set TST_IBKR_PYTHON")
    except subprocess.TimeoutExpired:
        out["err"] = f"the IB Gateway seed timed out after {timeout:.0f} s"
    except Exception as exc:  # noqa: BLE001
        out["err"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    else:
        text = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        out["tail"] = "\n".join(text.splitlines()[-8:])
        out["rc"] = p.returncode
        out["ok"] = p.returncode == 0
        if not out["ok"]:
            lines = [ln for ln in out["tail"].splitlines() if ln.strip()]
            out["err"] = (lines[-1] if lines else f"exit {p.returncode}")[:300]
    out["seconds"] = round(time.perf_counter() - t0, 1)
    return out


def _note(syms: list[str], gained: int, seed: dict | None, short: list[str]) -> str:
    n = len(syms)
    bits = [f"{n} ticker{'s' if n != 1 else ''}: {gained} day{'s' if gained != 1 else ''} of IV history added"]
    seed = seed or {}
    st = seed.get("status")
    if st in ("ok", "error") and seed.get("symbols"):
        bits.append("a year seeded from IB Gateway for " + ", ".join(seed["symbols"]))
    if st in ("ok", "error") and seed.get("empty"):
        bits.append("no IB history for " + ", ".join(seed["empty"]))
    if st == "error":
        bits.append("the IB Gateway seed failed (" + str(seed.get("err") or "?")[:120] + ")")
    elif st in ("skipped", "unavailable", "recent"):
        bits.append(str(seed.get("why") or st))
    if short:
        more = ", ..." if len(short) > 6 else ""
        bits.append(f"{len(short)} still under {IV_RANK_MIN_OBS} days ({', '.join(short[:6])}{more}) - "
                    "press Live on the card for a year from your TWS, or wait")
    return "; ".join(bits)


# ────────────────────────────────── the seed step ──────────────────────────────────

def _seed(db, short: list[str], client_id: int, lg) -> dict:
    """Step 2 for the symbols still short: who needs it, can we run it, is anything
    listening, then the seeder under the lock and the copy of what it wrote.
    ``status``: ok | error (the seeder ran) | skipped (no IB port answers) |
    unavailable (no interpreter can import ib_insync) | recent (tried lately) |
    none (the screener already holds a year)."""
    in_hist = counts(db, short, IVHistory)
    cands = [s for s in short if in_hist.get(s, 0) < ENOUGH_HISTORY]
    if not cands:
        return {"status": "none", "why": "the screener already holds a year for these"}
    now = time.monotonic()
    recent = [s for s in cands if now - _seed_memo.get(s, float("-inf")) < SEED_RETRY_S]
    to_seed = [s for s in cands if s not in recent]
    if not to_seed:
        return {"status": "recent", "recent": recent,
                "why": f"the IB Gateway seed was tried within the last {SEED_RETRY_S // 3600} h for "
                       + ", ".join(recent[:6]) + (", ..." if len(recent) > 6 else "")}
    python, why = resolve_ibkr_python()
    if python is None:
        return {"status": "unavailable", "why": why, "recent": recent}
    port, tried = gateway_port()
    if port is None:
        return {"status": "skipped", "recent": recent,
                "why": f"IB Gateway / TWS not reachable on {IB_HOST} (tried {', '.join(map(str, tried))})"}
    with _seed_lock:
        db.commit()                      # end our transaction: the seeder writes through its own connection
        hist_before = counts(db, to_seed, IVHistory)
        r = run_seeder(to_seed, port=port, python=python, client_id=client_id)
        hist_after = counts(db, to_seed, IVHistory)
        copied = int(option_store.backfill_from_iv_history(db, symbols=to_seed) or 0)
    gained_syms = [s for s in to_seed if hist_after.get(s, 0) > hist_before.get(s, 0)]
    empty = [s for s in to_seed if s not in gained_syms]
    for s in empty:
        _seed_memo[s] = time.monotonic()
    for s in gained_syms:
        _seed_memo.pop(s, None)
    info = {"status": "ok" if r["ok"] else "error", "symbols": gained_syms, "empty": empty,
            "attempted": to_seed, "port": port, "client_id": client_id, "seconds": r["seconds"],
            "err": r["err"], "tail": r["tail"], "cmd": r["cmd"], "copied": copied, "recent": recent}
    if r["ok"]:
        lg.info("IB Gateway seed (port %s, %.0fs): %d symbol(s) gained history, %d returned nothing%s",
                port, r["seconds"], len(gained_syms), len(empty), (" (" + ", ".join(empty) + ")") if empty else "")
    else:
        lg.warning("IB Gateway seed FAILED (rc=%s, %.0fs, %s, port %s): %s",
                   r["rc"], r["seconds"], r["cmd"], port, r["tail"] or r["err"])
    return info


# ────────────────────────────────── the two entry points ──────────────────────────────────

def backfill(db, symbols, *, seed: bool = True, recompute: bool = True, today=None,
             client_id: int = SEED_CLIENT_ID_WEB, log=None) -> dict:
    """Steps 1 and 2 for ``symbols``; the rank recompute for every symbol that gained days
    (``recompute``); a ``backfill`` job row when days landed or a seed failed. Returns::

        {symbols, copied, gained, seed: {status, why|symbols|empty|err|seconds|tail|port},
         short, per: {sym: {before, after, gained, rank?, stale_marked?}}, job_id, note}
    """
    lg = log or logging.getLogger(__name__)
    syms = _clean(symbols)
    today = option_store._day(today)
    out: dict = {"symbols": syms, "copied": 0, "gained": 0, "seed": None, "short": [],
                 "per": {}, "job_id": None, "note": ""}
    if not syms:
        return out
    before = counts(db, syms)
    out["copied"] = int(option_store.backfill_from_iv_history(db, symbols=syms) or 0)
    after = counts(db, syms)
    short = [s for s in syms if after.get(s, 0) < IV_RANK_MIN_OBS]
    seed_info: dict = {"status": "none"}
    if short and not seed:
        seed_info = {"status": "off", "why": "seed=False"}
    elif short and not seeder_enabled():
        seed_info = {"status": "off", "why": "TST_IV_SEED_IBKR=0"}
    elif short:
        seed_info = _seed(db, short, client_id, lg)
        out["copied"] += int(seed_info.get("copied") or 0)
        after = counts(db, syms)
    out["seed"] = seed_info
    out["short"] = [s for s in syms if after.get(s, 0) < IV_RANK_MIN_OBS]
    for s in syms:
        d: dict = {"before": before.get(s, 0), "after": after.get(s, 0)}
        d["gained"] = d["after"] - d["before"]
        if recompute and d["gained"] > 0:
            try:
                r = option_store.rank_after_history(db, s, today)
                d["rank"] = (r.get("rank") or {}).get("iv_rank") if r else None
                d["stale_marked"] = int(r.get("stale_marked") or 0) if r else 0
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                d["rank_err"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                lg.warning("backfill %s: rank recompute failed: %s", s, exc)
        out["per"][s] = d
    out["gained"] = sum(d["gained"] for d in out["per"].values())
    out["note"] = _note(syms, out["gained"], seed_info, out["short"])
    seeded = seed_info.get("status") in ("ok", "error")
    if out["gained"] > 0 or seed_info.get("status") == "error":
        run = job_runs.start(db, JOB, today, source="hist+ibkr" if seeded else "iv_history")
        detail = {**out["per"], "_seed": seed_info, "_copied": out["copied"], "_short": out["short"]}
        job_runs.finish(db, run, ok=len(syms) - len(out["short"]),
                        errors=1 if seed_info.get("status") == "error" else 0,
                        rows=out["gained"], symbols=len(syms), detail=detail, note=out["note"])
        out["job_id"] = run.id
    lg.info("backfill: %s", out["note"])
    return out


def first_read(db, symbols, user=None, *, on_done=None, sleep=time.sleep, log=None,
               client_id: int = SEED_CLIENT_ID_WEB) -> dict:
    """The first-time read a basket add starts: ``backfill`` (rank recomputed, so a
    read that then fails still leaves the stored rank consistent with the history
    that landed), then per symbol the Refresh pipeline (``option_nightly.refresh_symbol``:
    today's delayed chain -> metrics -> engines -> the house and the member's signal
    rows), Cboe-paced. ``on_done(sym, status, err)`` fires after each symbol so a
    waiting card can stop polling - and say why when the read failed. Returns
    ``{backfill, reads: {sym: {status, err, n, signals}}}``; never raises."""
    from . import option_nightly       # noqa: PLC0415 - option_nightly imports this module

    lg = log or logging.getLogger(__name__)
    syms = _clean(symbols)
    out: dict = {"backfill": None, "reads": {}}
    if not syms:
        return out
    try:
        out["backfill"] = backfill(db, syms, seed=True, recompute=True, client_id=client_id, log=lg)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        out["backfill"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        lg.warning("first read: backfill failed: %s", exc, exc_info=True)
    try:
        pacing = float(option_nightly._pacing(option_nightly._source(None)) or 0.0)
    except Exception:  # noqa: BLE001
        pacing = 1.5
    for i, s in enumerate(syms):
        if i and pacing > 0:
            sleep(pacing)
        try:
            d = option_nightly.refresh_symbol(db, s, user)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            d = {"status": "error", "err": f"{type(exc).__name__}: {str(exc)[:200]}"}
            lg.warning("first read %s failed: %s", s, exc, exc_info=True)
        status = d.get("status") or "error"
        err = d.get("err")
        out["reads"][s] = {"status": status, "err": err, "n": d.get("n"), "signals": d.get("signals")}
        if on_done is not None:
            try:
                on_done(s, status, err)
            except Exception:  # noqa: BLE001 - a UI bookkeeping hook must not stop the reads
                pass
    lg.info("first read: %d symbol(s), %d ok", len(syms),
            sum(1 for r in out["reads"].values() if r.get("status") == "ok"))
    return out
