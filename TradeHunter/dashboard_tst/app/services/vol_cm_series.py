"""The interpolated history (v4.209 / v4.210) - the Interpolation page's "History" tab and, since
v4.210, the chart and rank in the Interpolated tab's hover card.

The user (2026-10-24): *"if i want to show this historical table but based on interpolated data?"*,
then *"when hover in the interpolated surface, i need the graph to be shown also"*. A row per trading
day of what the Interpolated surface would have shown for ONE cell - a fixed horizon and a fixed
moneyness (or a fixed strike) - rebuilt from THAT day's listed contracts (the Parquet contract
history: closes solved back to IVs) and that day's stock close. Not a contract: no open / high /
low / volume; each row carries the legs and weights it rested on.

Speed (v4.210, so a hover can ask for it): the root's rows over the window are read ONCE from the
Parquet day files into numpy arrays (``_root_rows``: every contract of the root traded on a day of
the window whose expiry is at most ``dte_cap(h)`` days past the window's end - unlike
``vol_history``'s table, contracts that expired INSIDE the window stay, since an early day's near
leg is one of them) and kept in memory (``ROOT_CACHE_N`` windows); each day then solves only the
~20 contracts near the level and the horizon (``vol_history.implied_vols_fast``) and interpolates
through ``vol_cm_point.point``. A computed series is reused ``TTL`` seconds. ``stats``: the range
over the window and where the newest value sits (rank / percentile), the same arithmetic as the
listed tab's ``vol_history``. This is the constant-maturity history the phase-2 note planned,
computed on demand for one cell.

v4.215 (user: the History tab of an AMD cell said "The server did not answer"): on Hermes a series could
outlast Cloudflare's ~100 s request limit - each horizon re-read the whole window from disk (the read was
cached per expiry cap, so a 30-day, a 90-day and a 365-day cell were three ~1.3 GB scans on Hermes's slow
virtual disk) and every hovered cell started its own rebuild. Now: ONE read per root and window, out to
``READ_CAP`` days of expiry, shared by every horizon, sorted by day so a day is a slice; each day solves only
the ``NEAR_EXP`` listed expiries either side of the horizon and the ``NEAR_K`` listed strikes either side of the
level (the legs and their neighbours - the same result, a tenth of the work); at most ``MAX_JOBS`` series
compute at once; and ``series_async`` runs a series as a background job, answering "pending" after ``wait``
seconds, so a request never sits on the tunnel for long - the page asks again until it is done. No Massive
API call is involved: everything is read from the stored Parquet days.
"""
from __future__ import annotations

import collections
import datetime as _dt
import logging
import threading
import time

import numpy as np

from . import contract_view, opt_history, vol_cm, vol_cm_point, vol_history, vol_surface
from .black_scholes import black_scholes
from .opt_constants import RISK_FREE
from .vol_surface import SurfaceError

K_BAND = 0.12                  # the outer bound: contracts within this fraction of the price around the level ...
NEAR_K = 3                     # ... and of those, the listed strikes this many either side of the level
NEAR_EXP = 6                   # the listed expiries this many either side of the horizon
TTL = 600.0                    # seconds a computed series is reused
MAX_DAYS = 400                 # sessions solved for one series at most (a 365-day window holds ~252)
ROOT_CACHE_N = 4               # root windows whose rows are kept in memory
MAX_JOBS = 2                   # series computed at the same time (the rest wait their turn)
JOB_ERROR_TTL = 30.0           # seconds a failed job's message is kept before a new request tries again
_LOCK = threading.Lock()
_READ_LOCK = threading.Lock()
_RUN = threading.BoundedSemaphore(MAX_JOBS)
_CACHE: dict[tuple, tuple[float, dict]] = {}
_ROOTS: collections.OrderedDict = collections.OrderedDict()
_JOBS: dict[tuple, dict] = {}
_EPOCH = _dt.date(1970, 1, 1)
log = logging.getLogger(__name__)


def dte_cap(h: int) -> int:
    """Expiries used for a horizon: up to twice the horizon plus two months, so the upper leg is there."""
    return int(h * 2 + 60)


READ_CAP = dte_cap(max(vol_cm.HORIZONS))   # v4.215: the one read covers the longest horizon, so every horizon shares it


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _ROOTS.clear()
        _JOBS.clear()


def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _dnum(d: _dt.date) -> int:
    return (d - _EPOCH).days


# ───────────────────────────── the root's rows over the window ─────────────────────────────

def _read_root(opt_root: str, days: list[str], cap: int, root=None) -> dict:
    """Every contract of ``opt_root`` traded on a day of ``days`` with an expiry at most ``cap``
    days past the last day (expired-inside-the-window contracts included), as numpy arrays:
    ``on`` (days since 1970), ``expiry`` (days), ``strike``, ``call``, ``close``."""
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.compute as pc  # noqa: PLC0415
    import pyarrow.dataset as pds  # noqa: PLC0415

    base = opt_history._root(root)  # noqa: SLF001 - the module's own root resolver
    paths = [str(opt_history.day_path(d, base)) for d in days]
    empty = {"on": np.zeros(0, np.int64), "expiry": np.zeros(0, np.int64), "strike": np.zeros(0),
             "call": np.zeros(0, bool), "close": np.zeros(0), "slices": {}}
    if not paths:
        return empty
    try:
        ds = pds.dataset(paths, format="parquet")
        last = _dt.date.fromisoformat(days[-1]) + _dt.timedelta(days=cap)
        tbl = ds.to_table(filter=(pc.field("root") == str(opt_root).upper()) & (pc.field("expiry") <= pa.scalar(last, pa.date32())),
                          columns=["on", "expiry", "right", "strike", "close"])
    except Exception as exc:  # noqa: BLE001
        raise opt_history.HistoryError("source", "the contract history could not be read (%s: %s)"
                                       % (type(exc).__name__, str(exc)[:200])) from None
    if tbl.num_rows == 0:
        return empty
    on = pc.cast(tbl["on"], pa.int32()).to_numpy().astype(np.int64)
    ex = pc.cast(tbl["expiry"], pa.int32()).to_numpy().astype(np.int64)
    strike = tbl["strike"].to_numpy(zero_copy_only=False).astype(np.float64)
    call = np.array(pc.equal(tbl["right"], "C").to_numpy(zero_copy_only=False), dtype=bool)
    close = tbl["close"].to_numpy(zero_copy_only=False).astype(np.float64)
    keep = (ex > on) & (close > 0)
    out = {"on": on[keep], "expiry": ex[keep], "strike": strike[keep], "call": call[keep], "close": close[keep]}
    return _by_day(out)


def _by_day(a: dict) -> dict:
    """v4.215: the arrays sorted by day, with ``slices`` = {day number: (start, end)} - a day is a slice."""
    order = np.argsort(a["on"], kind="stable")
    a = {kk: v[order] for kk, v in a.items()}
    if a["on"].size:
        u, starts = np.unique(a["on"], return_index=True)
        ends = list(starts[1:]) + [a["on"].size]
        a["slices"] = {int(d): (int(b), int(e)) for d, b, e in zip(u, starts, ends)}
    else:
        a["slices"] = {}
    return a


def _root_rows(opt_root: str, days: list[str], cap: int = READ_CAP, root=None) -> dict:
    """``_read_root`` cached per (root, window) - v4.215: no longer per expiry cap, so every horizon shares one read."""
    key = (str(opt_root).upper(), days[0] if days else "", days[-1] if days else "", len(days), str(root or ""))
    with _LOCK:
        hit = _ROOTS.get(key)
        if hit is not None:
            _ROOTS.move_to_end(key)
            return hit
    with _READ_LOCK:
        with _LOCK:
            hit = _ROOTS.get(key)
            if hit is not None:
                return hit
        t0 = time.monotonic()
        arrays = _read_root(opt_root, days, max(cap, READ_CAP), root)
        log.info("interpolated history: read %s, %d sessions, %d contract-days in %.1f s",
                 opt_root, len(days), arrays["on"].size, time.monotonic() - t0)
        with _LOCK:
            _ROOTS[key] = arrays
            while len(_ROOTS) > ROOT_CACHE_N:
                _ROOTS.popitem(last=False)
    return arrays


# ───────────────────────────────── one day, one cell ─────────────────────────────────

def _solve(d, dn, spot, rows_day, sel, h, m, k, side, r):
    """The chosen contracts of one day solved to IVs, built into a grid and interpolated: ``(point, None)`` or
    ``(None, why)``."""
    ex, Ks, call, close = rows_day["expiry"][sel], rows_day["strike"][sel], rows_day["call"][sel], rows_day["close"][sel]
    T = (ex - dn) / 365.0
    iv = vol_history.implied_vols_fast(close, np.full(close.shape, float(spot)), Ks, T, call, r)
    rows = [{"expiry": _EPOCH + _dt.timedelta(days=int(e)), "right": "C" if c else "P", "strike": float(kk),
             "iv": (None if v != v else float(v))}
            for e, kk, c, v in zip(ex.tolist(), Ks.tolist(), call.tolist(), iv.tolist())]
    o = vol_surface.norm_options(side, 0, 0, "all")
    try:
        grid = vol_surface.build(rows, spot, d, common=False, **o)
    except SurfaceError as exc:
        return None, str(exc)
    if not grid["n_cells"]:
        return None, "no contract near the level has a volatility"
    try:
        return vol_cm_point.point(grid, h, m=m, k=k, r=r), None
    except (SurfaceError, ValueError) as exc:
        return None, str(exc)


def day_point(d: _dt.date, spot: float | None, rows_day: dict, h: int, *, m=None, k=None, side=None,
              r: float = RISK_FREE) -> dict:
    """One day's row: ``{on, spot, K, iv, lo, hi, w, k_lo, k_hi, wk, price, n, why, exact}`` from that
    day's rows (numpy arrays ``expiry, strike, call, close``, already cut to the day)."""
    row = {"on": d.isoformat(), "spot": spot, "K": None, "iv": None, "lo": None, "hi": None, "w": None,
           "k_lo": None, "k_hi": None, "wk": None, "price": None, "n": 0, "why": None, "exact": False}
    if spot is None or spot <= 0:
        row["why"] = "no stock close on file"
        return row
    K = round(spot * m, 2) if m is not None else float(k)
    row["K"] = K
    if rows_day["close"].size == 0:
        row["why"] = "no trade"
        return row
    dn = _dnum(d)
    dte = rows_day["expiry"] - dn
    sel = (dte >= vol_cm.MIN_LEG_DTE) & (dte <= dte_cap(h)) & (np.abs(rows_day["strike"] - K) <= K_BAND * spot)
    if not sel.any():
        row["why"] = "no contract near the level traded"
        return row
    # v4.215: first only the listed expiries NEAR_EXP either side of the horizon and, on each, its own NEAR_K listed
    # strikes either side of the level - the legs and their neighbours, a tenth of the work; when that finds no value
    # (the strike pair the interpolation needs sits further out, mostly at long horizons), the day is solved again on
    # the full band exactly as before, so the answer never differs from the full computation
    ud = np.unique(dte[sel])
    keep_d = np.concatenate([ud[ud <= h][-NEAR_EXP:], ud[ud > h][:NEAR_EXP]])
    narrow = sel & np.isin(dte, keep_d)
    near = np.zeros_like(sel)
    for dd in keep_d:                                        # each expiry's OWN nearest strikes: the long-dated ones list
        on_d = narrow & (dte == dd)                          # coarser strikes than the weeklies, and must keep theirs
        uk = np.unique(rows_day["strike"][on_d])
        keep_k = np.concatenate([uk[uk <= K][-NEAR_K:], uk[uk > K][:NEAR_K]])
        near |= on_d & np.isin(rows_day["strike"], keep_k)
    pt, why = _solve(d, dn, spot, rows_day, near, h, m, k, side, r)
    if (pt is None or pt.get("iv") is None) and int(near.sum()) < int(sel.sum()):
        pt, why = _solve(d, dn, spot, rows_day, sel, h, m, k, side, r)
    if pt is None:
        row["why"] = why
        return row
    row["n"] = int(pt.get("n_contracts") or 0)
    if pt.get("iv") is None:
        row["why"] = pt.get("why") or "no legs around the point"
        return row
    t, s = pt.get("time") or {}, pt.get("strike") or {}
    row.update({"iv": pt["iv"], "exact": bool(pt.get("exact")),
                "lo": {**t["lo"], "iv": (t["per_strike"][0]["lo_iv"] if t.get("per_strike") else None)} if t else None,
                "hi": {**t["hi"], "iv": (t["per_strike"][0]["hi_iv"] if t.get("per_strike") else None)} if t else None,
                "w": t.get("w") if t else None,
                "k_lo": s.get("k_lo") if s else None, "k_hi": s.get("k_hi") if s else None, "wk": s.get("wk") if s else None,
                "price": ((pt.get("greeks") or {}).get("result") or {}).get("price")})
    if row["price"] is None and spot and K:
        # v4.210: a past day has no greeks pass - the model price is Black-Scholes at the row's IV, the fixed horizon,
        # the row's strike and the stock close (the hover card's Model price panel shows the same working)
        right = pt.get("right") or ("C" if K >= spot else "P")
        try:
            row["price"] = round(black_scholes(float(spot), float(K), max(h, 0.5) / 365.0, r, pt["iv"] / 100.0,
                                               "call" if right == "C" else "put").price, 4)
        except (ValueError, ZeroDivisionError, OverflowError):
            row["price"] = None
    return row


# ───────────────────────────────────── the series ─────────────────────────────────────

def series(sdb, symbol, h, *, m=None, k=None, side=None, window_days=None, root=None, end=None, today=None) -> dict:
    """The rows (newest first) and the stats for one cell over the rank window. ``end`` (v4.210):
    the last session to include (``YYYY-MM-DD``; default the newest converted day) - a past-day
    surface asks for the history up to its day."""
    sym = vol_surface._sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    h = vol_cm_point.norm_h(h)
    if (m is None) == (k is None):
        raise ValueError("give a moneyness (m) or a strike (k), not both")
    m = vol_cm_point.norm_m(m) if m is not None else None
    k = vol_cm_point.norm_k(k) if k is not None else None
    wd = vol_history.norm_window(window_days)
    side = vol_surface.norm_options(side, 0, 0, "all")["side"]
    why = opt_history.available()
    if why:
        raise SurfaceError("the contract history is not set up on this server (%s)" % why)
    oroot = vol_surface.option_root_of(sdb, sym)
    if oroot is None:
        raise SurfaceError("%s is an index - the daily files have no index close to price its options against" % sym)
    end_d = _to_date(end) if end else None
    key = (sym, h, m, k, side, wd, end_d.isoformat() if end_d else "", str(root or ""))
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    days_all = contract_view.history_days(root)             # newest first
    if end_d is not None:
        days_all = [d for d in days_all if _to_date(d) and _to_date(d) <= end_d]
    if not days_all:
        raise SurfaceError("no day of the contract history has been converted yet"
                           + (" up to %s" % end_d.isoformat() if end_d else ""))
    last = _to_date(days_all[0])
    start = last - _dt.timedelta(days=wd)
    days = [d for d in days_all if _to_date(d) and _to_date(d) > start][:MAX_DAYS]
    closes = vol_surface.raw_closes(sdb, sym, days)
    t0 = time.monotonic()
    with _RUN:                                               # v4.215: at most MAX_JOBS series at once
        with _LOCK:                                          # done by another caller while this one waited?
            hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] < TTL:
                return hit[1]
        arrays = _root_rows(oroot, sorted(days), READ_CAP, root)
        rows = []
        for ds in days:
            d = _to_date(ds)
            b, e = arrays["slices"].get(_dnum(d), (0, 0))
            rows_day = {kk: arrays[kk][b:e] for kk in ("expiry", "strike", "call", "close")}
            rows.append(day_point(d, closes.get(ds), rows_day, h, m=m, k=k, side=side))
    log.info("interpolated history: %s %dd %s over %d sessions in %.1f s", sym, h,
             ("m %.3f" % m) if m is not None else ("k %.2f" % k), len(days), time.monotonic() - t0)
    vals = [r["iv"] for r in rows if r["iv"] is not None]
    stats = None
    if vals:
        lo, hi = min(vals), max(vals)
        newest = next((r["iv"] for r in rows if r["iv"] is not None), None)
        below = sum(1 for v in vals if v < newest) if newest is not None else 0
        stats = {"n": len(vals), "lo": round(lo, 4), "hi": round(hi, 4), "newest": newest,
                 "newest_on": next((r["on"] for r in rows if r["iv"] is not None), None),
                 "rank": round((newest - lo) / (hi - lo), 4) if newest is not None and hi > lo else None,
                 "pct": round(below / len(vals), 4) if newest is not None else None,
                 "first": rows[-1]["on"] if rows else None, "last": rows[0]["on"] if rows else None}
    try:
        earn = vol_history.earnings_in(sdb, sym, start.isoformat(), days[0]) if days else {"past": [], "next": None, "coverage": None}
    except Exception:  # noqa: BLE001 - the dates are a decoration on the chart, never a reason to fail the series
        earn = {"past": [], "next": None, "coverage": None}
    out = {"symbol": sym, "h": h, "m": m,
           "earnings": earn.get("past") or [], "next_earnings": earn.get("next"), "earnings_coverage": earn.get("coverage"), "pct": round(m * 100.0, 1) if m is not None else None, "k": k,
           "fixed": "moneyness" if m is not None else "strike", "side": side, "window_days": wd,
           "window": [days[-1], days[0]] if days else None, "days": len(days), "rows": rows, "stats": stats,
           "rate": RISK_FREE, "root": oroot, "epoch": _EPOCH.isoformat(),
           # the chart's points, oldest first: [days since 1970, IV %]
           "series": [[_dnum(_to_date(r["on"])), r["iv"]] for r in reversed(rows) if r["iv"] is not None],
           "method": "each row: that day's listed contracts within %d %% of the level (closes solved to IV), expiries %d "
                     "to %d days out; linear in total variance between the two expiries around %d days, then linear "
                     "in strike between the two listed strikes around the level; nothing extrapolated"
                     % (int(K_BAND * 100), vol_cm.MIN_LEG_DTE, dte_cap(h), h)}
    with _LOCK:
        _CACHE[key] = (now, out)
    return out


def _job_key(symbol, h, m, k, side, window_days, end, root) -> tuple:
    """The cache key ``series`` uses, from the raw arguments (raises ValueError / SurfaceError as series does)."""
    sym = vol_surface._sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    h = vol_cm_point.norm_h(h)
    if (m is None) == (k is None):
        raise ValueError("give a moneyness (m) or a strike (k), not both")
    m = vol_cm_point.norm_m(m) if m is not None else None
    k = vol_cm_point.norm_k(k) if k is not None else None
    wd = vol_history.norm_window(window_days)
    side = vol_surface.norm_options(side, 0, 0, "all")["side"]
    end_d = _to_date(end) if end else None
    return (sym, h, m, k, side, wd, end_d.isoformat() if end_d else "", str(root or ""))


def series_async(session_factory, symbol, h, *, m=None, k=None, side=None, window_days=None, root=None, end=None,
                 wait: float = 20.0) -> dict:
    """v4.215: ``series`` as a background job, so no request outlasts the tunnel. ``{state: "done", result}``,
    ``{state: "pending", elapsed}`` (still computing after ``wait`` seconds - ask again) or ``{state: "error",
    error}``. One job per series key: a second caller waits on the same job instead of starting another.
    ``session_factory`` opens a screener session for the job's own thread."""
    key = _job_key(symbol, h, m, k, side, window_days, end, root)
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL:
            return {"state": "done", "result": hit[1]}
        job = _JOBS.get(key)
        if job is not None and job["done"].is_set() and job.get("error") and now - job["ended"] > JOB_ERROR_TTL:
            job = None                                       # a stale failure: try again
        if job is None:
            job = {"done": threading.Event(), "started": now, "ended": None, "error": None, "result": None}
            _JOBS[key] = job

            def run():
                try:
                    with session_factory() as sdb:
                        job["result"] = series(sdb, symbol, h, m=m, k=k, side=side, window_days=window_days,
                                               root=root, end=end)
                except (SurfaceError, ValueError) as exc:
                    job["error"] = str(exc)
                except Exception as exc:  # noqa: BLE001 - reported to the page, logged here
                    log.exception("interpolated history job %s failed", key)
                    job["error"] = "the history could not be rebuilt (%s)" % type(exc).__name__
                finally:
                    job["ended"] = time.monotonic()
                    job["done"].set()
                    if not job["error"]:
                        with _LOCK:
                            if _JOBS.get(key) is job:
                                _JOBS.pop(key, None)

            threading.Thread(target=run, name="cm-series", daemon=True).start()
    job["done"].wait(max(0.0, float(wait)))
    if job["done"].is_set():
        if job["error"]:
            return {"state": "error", "error": job["error"]}
        return {"state": "done", "result": job["result"]}
    return {"state": "pending", "elapsed": round(time.monotonic() - job["started"], 1)}


__all__ = ["K_BAND", "NEAR_K", "NEAR_EXP", "TTL", "MAX_DAYS", "ROOT_CACHE_N", "MAX_JOBS", "READ_CAP", "dte_cap",
           "day_point", "series", "series_async", "clear_cache"]
