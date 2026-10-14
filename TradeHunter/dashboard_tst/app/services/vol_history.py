"""Each contract's own IV history behind the Volatility surface (v4.141, OPTIONS_SCREENER_DESIGN.md
§14): the colour of a cell = where today's IV sits in THAT CONTRACT's own range over the past year
(green at its lowest, red at its highest), and the sparkline on hover.

The user (2026-10-14): *"currently the colour scheme is based on the page comparison between the
different contracts of the same day, I want it to be based on the IV of the same contract compared
to its own historical IV. with the lowest IV be coloured green and the highest IV to the red. when
mouse over, it will shown the historical IV versus the current IV with a small graphical
representation."*

Where the history comes from
----------------------------
The contract history (``opt_history``: Massive's options day files as Parquet). For every
contract in the grid, its close on each day it traded during the ``HISTORY_DAYS`` (252) sessions
before the surface's day -> Black-Scholes implied vol against the stock's unadjusted close that day
(``scr_underlying_daily.close_raw``), r = ``RISK_FREE``, no dividend, T = calendar days / 365 - the
same inputs as the IV history and the past-day surface. The solver here is a numpy-only bisection
(``implied_vols_fast``: the normal CDF by Abramowitz-Stegun 7.1.26, |error| < 1.5e-7) - within
~0.005 volatility points of ``black_scholes.implied_vol``, and 200,000 contracts a second instead
of 60 Python erf calls per contract. Ranks and sparklines do not need the last digit.

The figures per cell (``cell_stats``)
-------------------------------------
``n`` days of history; ``lo`` / ``hi`` the lowest and highest IV in them (percent); ``rank`` =
(current - lo) / (hi - lo), clamped to 0..1 - the IV-rank convention the screener uses, 0 = at its
lowest, 1 = at its highest (``None`` under ``MIN_HISTORY`` days); ``pct`` = the share of history
days below the current IV; ``series`` = up to ``SPARK_POINTS`` evenly spaced points
``[day_index, iv]`` plus ``days`` (the dates) for the sparkline. A cell that shows the average of
the call and the put (the ATM row, the ``mid`` option) gets the per-day average of the two legs'
histories where both exist, else the one that does.

Speed: the root's rows for the window are read once per (root, newest converted day) and kept
(``_TABLES``, a few roots), solved series are kept per contract (``_SERIES``), so the first surface
of a symbol takes a few seconds (AAPL: ~2.5 s read + ~1 s solve for a 20 % band) and the next ones
milliseconds. The page asks for this AFTER the grid is painted (``/options/api/surface/history``).
"""
from __future__ import annotations

import collections
import datetime as _dt
import math
import threading

import numpy as np

from . import opt_history
from .opt_constants import RISK_FREE
from .vol_surface import SurfaceError, _f, _sym, _to_date, option_root_of, raw_closes

HISTORY_DAYS = 252              # the past year of sessions (the screener's IV-rank window)
MIN_HISTORY = 10                # a rank needs at least this many days of history
SPARK_POINTS = 60               # points in a sparkline (about weekly over a year)
MAX_CONTRACTS = 6000            # legs solved for one grid (the nearest the price first; the rest reported as skipped)
IV_LO, IV_HI, IV_ITERS = 0.01, 5.0, 50     # the bisection bracket (fraction) and halvings (~1e-15)
TABLE_CACHE_N = 4               # roots whose window rows are kept in memory
_EPOCH = _dt.date(1970, 1, 1)

_LOCK = threading.Lock()
_READ_LOCK = threading.Lock()   # one Parquet read at a time: a second request for the same root waits for the first
_TABLES: collections.OrderedDict = collections.OrderedDict()   # window key -> {arrays}
_SERIES: dict = {}                                            # window key -> {key: (on_days, iv_pct)}


# ────────────────────────────── a numpy-only Black-Scholes IV ──────────────────────────────

def ncdf_fast(x: np.ndarray) -> np.ndarray:
    """The standard normal CDF, Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7), numpy only."""
    z = np.abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    poly = ((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592
    y = 1.0 - poly * t * np.exp(-z * z)
    return 0.5 * (1.0 + np.where(x >= 0, y, -y))


def _bs(sig, S, K, T, call, r):
    st = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sig * sig) * T) / st
    d2 = d1 - st
    disc = K * np.exp(-r * T)
    return np.where(call, S * ncdf_fast(d1) - disc * ncdf_fast(d2), disc * ncdf_fast(-d2) - S * ncdf_fast(-d1))


def implied_vols_fast(prices, S, K, T, is_call, r: float = RISK_FREE) -> np.ndarray:
    """Implied vols (FRACTION) for many contracts at once; NaN where there is none (no price,
    no time, a price at / under the model's floor or at / over its ceiling)."""
    p = np.asarray(prices, dtype=np.float64)
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    call = np.asarray(is_call, dtype=bool)
    out = np.full(p.shape, np.nan)
    if p.size == 0:
        return out
    good = (p > 0) & (S > 0) & (K > 0) & (T > 0)
    if not good.any():
        return out
    idx = np.nonzero(good)[0]
    p, S, K, T, call = p[idx], S[idx], K[idx], T[idx], call[idx]
    lo = np.full(p.shape, IV_LO)
    hi = np.full(p.shape, IV_HI)
    ok = (p > _bs(lo, S, K, T, call, r)) & (p < _bs(hi, S, K, T, call, r))
    for _ in range(IV_ITERS):
        mid = 0.5 * (lo + hi)
        above = _bs(mid, S, K, T, call, r) > p
        hi = np.where(above, mid, hi)
        lo = np.where(above, lo, mid)
    iv = 0.5 * (lo + hi)
    iv[~ok] = np.nan
    out[idx] = iv
    return out


# ────────────────────────────── the root's rows, read once ──────────────────────────────

def _days(d: _dt.date) -> int:
    return (d - _EPOCH).days


def contract_key(expiry, right: str, strike: float) -> int:
    """One int per contract: expiry (days since 1970) x 2e7 + call x 1e7 + strike x 1000."""
    e = _to_date(expiry)
    return _days(e) * 20_000_000 + (10_000_000 if str(right).upper().startswith("C") else 0) + int(round(float(strike) * 1000))


def _window(end_day, root=None) -> tuple[list[str], float | None]:
    """The converted sessions strictly before ``end_day`` (the last ``HISTORY_DAYS`` of them) and
    the contract-history manifest's modification time - a day converted again (re-published by
    Massive, or late) changes it, so the caches below see a new window."""
    from . import flat_files  # noqa: PLC0415

    end = _to_date(end_day)
    store = opt_history.Store(root=opt_history._root(root), flat_root=flat_files.DEFAULT_ROOT)  # noqa: SLF001
    days = [d for d in store.days() if end is None or d < end.isoformat()]
    return days[-HISTORY_DAYS:], store._mtime  # noqa: SLF001 - the manifest's mtime, as opt_history's own cache uses


def history_window(end_day, root=None) -> list[str]:
    """The converted sessions strictly before ``end_day``, the last ``HISTORY_DAYS`` of them."""
    return _window(end_day, root)[0]


def _window_key(opt_root: str, days: list[str], mtime, root) -> tuple:
    return (str(opt_root).upper(), days[-1] if days else "", len(days), mtime, str(root or ""))


def _read_root(opt_root: str, days: list[str], root=None) -> dict:
    """The root's rows over ``days`` as numpy arrays: ``key``, ``on`` (days since 1970),
    ``expiry`` (days), ``strike``, ``call``, ``close``."""
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.compute as pc  # noqa: PLC0415
    import pyarrow.dataset as pds  # noqa: PLC0415

    base = opt_history._root(root)  # noqa: SLF001 - the module's own root resolver
    paths = [str(opt_history.day_path(d, base)) for d in days]
    empty = {"key": np.zeros(0, np.int64), "on": np.zeros(0, np.int64), "expiry": np.zeros(0, np.int64),
             "strike": np.zeros(0), "call": np.zeros(0, bool), "close": np.zeros(0)}
    if not paths:
        return empty
    try:
        ds = pds.dataset(paths, format="parquet")
        tbl = ds.to_table(filter=(pc.field("root") == str(opt_root).upper()),
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
    key = ex * 20_000_000 + np.where(call, 10_000_000, 0) + np.rint(strike * 1000).astype(np.int64)
    keep = ex > on                               # at least one day to expiry
    return {"key": key[keep], "on": on[keep], "expiry": ex[keep], "strike": strike[keep], "call": call[keep],
            "close": close[keep]}


def root_rows(opt_root: str, end_day, root=None) -> tuple[dict, list[str], tuple]:
    """``_read_root`` for the history window before ``end_day``, cached per window (root, its
    last day, its length, the manifest's mtime). Returns the arrays, the days and the window key."""
    days, mtime = _window(end_day, root)
    ck = _window_key(opt_root, days, mtime, root)

    def cached():
        hit = _TABLES.get(ck)
        if hit is not None:
            _TABLES.move_to_end(ck)
        return hit

    with _LOCK:
        hit = cached()
    if hit is not None:
        return hit, days, ck
    with _READ_LOCK:                              # a second request for the same window waits, then finds it
        with _LOCK:
            hit = cached()
        if hit is not None:
            return hit, days, ck
        arrays = _read_root(opt_root, days, root)
        with _LOCK:
            _TABLES[ck] = arrays
            while len(_TABLES) > TABLE_CACHE_N:
                _TABLES.popitem(last=False)
    return arrays, days, ck


# ────────────────────────────── the series per contract ──────────────────────────────

def contract_series(opt_root: str, keys, spots: dict, end_day, root=None) -> dict:
    """``{key: (on_days, iv_pct)}`` (both numpy, oldest first, NaN-free) for the contracts
    ``keys`` (``contract_key`` ints) - solved from the root's rows against ``spots``
    (``{YYYY-MM-DD: the stock's unadjusted close}``); solved once per contract and kept - unless
    a day of the window has no close yet (the stock lane behind): then the answer is given but
    not kept, so the next request solves again with the closes that arrived meanwhile."""
    arrays, days, ck = root_rows(opt_root, end_day, root)
    want = {int(k) for k in keys}
    complete = all(_f((spots or {}).get(d)) for d in days)
    with _LOCK:
        store = _SERIES.setdefault(ck, {})
        todo = sorted(k for k in want if k not in store)
        for old in [k for k in _SERIES if k not in _TABLES]:        # windows the table cache dropped
            _SERIES.pop(old, None)
    fresh: dict = {}
    if todo and arrays["key"].size:
        sel = np.isin(arrays["key"], np.array(todo, dtype=np.int64))
        key, on, ex = arrays["key"][sel], arrays["on"][sel], arrays["expiry"][sel]
        K, call, close = arrays["strike"][sel], arrays["call"][sel], arrays["close"][sel]
        spot_by_day = {_days(_dt.date.fromisoformat(d)): float(v) for d, v in (spots or {}).items() if _f(v) and v > 0}
        S = np.array([spot_by_day.get(int(x), np.nan) for x in on.tolist()])
        T = (ex - on) / 365.0
        iv = implied_vols_fast(close, S, K, T, call)
        order = np.lexsort((on, key))
        key, on, iv = key[order], on[order], iv[order]
        good = ~np.isnan(iv)
        key, on, iv = key[good], on[good], iv[good] * 100.0
        solved: dict = {}
        if key.size:
            starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
            ends = np.r_[starts[1:], key.size]
            for a, b in zip(starts, ends):
                solved[int(key[a])] = (on[a:b].copy(), iv[a:b].copy())
        fresh = {k: solved.get(k, _EMPTY) for k in todo}
        if complete:
            with _LOCK:
                store.update(fresh)
    elif todo:
        fresh = {k: _EMPTY for k in todo}
    with _LOCK:
        out = {k: store[k] for k in want if k in store}
    out.update(fresh)
    return out


_EMPTY = (np.zeros(0, np.int64), np.zeros(0))


def _merge_mean(series: list) -> tuple:
    """One leg's ``(on_days, iv)`` series as it is; two legs' (a cell that shows the average of
    the call and the put): their per-day average on the days BOTH traded - a day with one leg
    only would put that leg's level (puts sit above calls without a dividend in the model) into
    a history the two-leg average is ranked against. A leg with no history at all, or no common
    day: no history."""
    if not series or any(s is None or len(s[0]) == 0 for s in series):
        return _EMPTY
    if len(series) == 1:
        return series[0]
    (on_a, iv_a), (on_b, iv_b) = series[0], series[1]
    common, ia, ib = np.intersect1d(on_a, on_b, return_indices=True)
    if common.size == 0:
        return _EMPTY
    return common.astype(np.int64), 0.5 * (iv_a[ia] + iv_b[ib])


def cell_stats(current, on_days: np.ndarray, iv: np.ndarray) -> dict | None:
    """The figures of one cell (see the module docstring); None with no history at all."""
    n = int(len(iv))
    if n == 0:
        return None
    cur = _f(current)
    lo, hi = float(iv.min()), float(iv.max())
    out = {"n": n, "lo": round(lo, 2), "hi": round(hi, 2), "first": _EPOCH + _dt.timedelta(days=int(on_days[0])),
           "last": _EPOCH + _dt.timedelta(days=int(on_days[-1])), "rank": None, "pct": None}
    out["first"], out["last"] = out["first"].isoformat(), out["last"].isoformat()
    if cur is not None and n >= MIN_HISTORY:
        out["rank"] = round(max(0.0, min(1.0, (cur - lo) / (hi - lo))) if hi > lo else 0.5, 4)
        out["pct"] = round(float(np.mean(iv < cur)), 4)
    if n > SPARK_POINTS:                          # evenly spaced, plus the lowest and the highest day
        idx = np.unique(np.r_[np.rint(np.linspace(0, n - 1, SPARK_POINTS)).astype(int), int(iv.argmin()),
                              int(iv.argmax())])
    else:
        idx = np.arange(n)
    out["series"] = [[int(on_days[i]), round(float(iv[i]), 2)] for i in idx]
    return out


# ────────────────────────────── the payload for one grid ──────────────────────────────

def grid_history(sdb, symbol, grid: dict, *, root=None) -> dict:
    """For the surface ``grid`` (``vol_surface.surface``'s result): ``{cells: {"r,c": stats},
    atm: {"c": stats}, days: n, window: [first, last], epoch: "1970-01-01", note}``. The leg
    behind a cell follows the grid's ``side`` (put below the price, call at / above, one side
    only, or both averaged); the ATM row averages both legs at its strike."""
    sym = _sym(symbol)
    why = opt_history.available()
    if why:
        raise SurfaceError("the contract history is not set up on this server (%s)" % why)
    oroot = option_root_of(sdb, sym)
    if oroot is None:
        return {"cells": {}, "atm": {}, "days": 0, "window": None, "epoch": _EPOCH.isoformat(),
                "note": "%s is an index: the daily files carry no index close, so its contracts have no IV history"
                        % sym}
    on = grid["on"]
    spot = float(grid["spot"])
    side = grid.get("side") or "otm"
    strikes = grid["strikes"]
    exps = [_to_date(e["date"]) for e in grid["expiries"]]
    days = history_window(on, root)
    if not days:
        return {"cells": {}, "atm": {}, "days": 0, "window": None, "epoch": _EPOCH.isoformat(),
                "note": "no day of the contract history before %s is converted yet" % on}
    spots = raw_closes(sdb, sym, days)

    def legs_for(k: float, which: str) -> list[str]:
        if which == "call":
            return ["C"]
        if which == "put":
            return ["P"]
        if which == "mid":
            return ["C", "P"]
        return ["P"] if k < spot else ["C"]

    # the contracts to solve: the grid's cells (nearest the price first, at most MAX_CONTRACTS legs)
    wanted: dict[tuple[int, int], list[int]] = {}
    order = sorted(range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), strikes[i]))
    keys: list[int] = []
    for i in order:
        for j, e in enumerate(exps):
            if grid["cells"][i][j] is None:
                continue
            ks = [contract_key(e, r, strikes[i]) for r in legs_for(strikes[i], side)]
            wanted[(i, j)] = ks
            keys += ks
        if len(keys) >= MAX_CONTRACTS:
            break
    atm_keys: dict[int, list[int]] = {}
    for j, e in enumerate(exps):
        k = (grid.get("atm_strikes") or [None] * len(exps))[j]
        if k is not None and (grid.get("atm") or [None] * len(exps))[j] is not None:
            atm_keys[j] = [contract_key(e, "C", k), contract_key(e, "P", k)]
            keys += atm_keys[j]
    try:
        series = contract_series(oroot, set(keys), spots, on, root)
    except opt_history.HistoryError as exc:            # the real cause reaches the page
        raise SurfaceError("the contract history could not be read: %s" % exc) from None
    cells: dict[str, dict] = {}
    for (i, j), ks in wanted.items():
        st = cell_stats(grid["cells"][i][j], *_merge_mean([series.get(k) for k in ks]))
        if st is not None:
            cells["%d,%d" % (i, j)] = st
    atm: dict[str, dict] = {}
    for j, ks in atm_keys.items():
        st = cell_stats(grid["atm"][j], *_merge_mean([series.get(k) for k in ks]))
        if st is not None:
            atm[str(j)] = st
    skipped = ["%d,%d" % (i, j) for i in range(len(strikes)) for j in range(len(exps))
               if grid["cells"][i][j] is not None and (i, j) not in wanted]
    return {"cells": cells, "atm": atm, "days": len(days), "window": [days[0], days[-1]],
            "epoch": _EPOCH.isoformat(), "note": None,
            # the grid these figures belong to - the page checks it is still the one on screen
            "strikes": list(strikes), "expiries": [e.isoformat() for e in exps], "spot": spot, "side": side,
            "skipped": skipped,
            "method": "each contract's implied volatility solved from its daily close and the stock's close "
                      "(Black-Scholes, no dividend) over the %d sessions before %s" % (len(days), on)}


__all__ = ["HISTORY_DAYS", "MIN_HISTORY", "SPARK_POINTS", "ncdf_fast", "implied_vols_fast", "contract_key",
           "history_window", "root_rows", "contract_series", "cell_stats", "grid_history"]
