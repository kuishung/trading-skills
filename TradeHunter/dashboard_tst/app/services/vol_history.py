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

The rank window (v4.175)
------------------------
One lookback window for the WHOLE surface, chosen by the member: ``window_days`` calendar days
(``WINDOWS``: 30 / 60 / 90 / 180 / 270 / 365, default 365) ending at the surface's day (the
latest pass's session, or the past day picked). Every cell is ranked over the sessions dated
inside that window - so a contract listed three weeks ago and one listed two years ago are no
longer ranked over different spans. A cell carries ``window_days``, ``n`` (its sessions in the
window), ``first`` (its first one) and ``covered`` (calendar days from ``first`` to the end day).
On a window SHORTER than the default a cell is ranked only when it covers at least ``MIN_COVER``
(80 %) of the window (of the part of it the converted history reaches) and has ``MIN_HISTORY``
sessions; otherwise ``rank`` is None with ``why`` ("listed 21 days ago, shorter than the 90-day
window" - the first session in the data stands in for the listing day). At the default 365 days
the rule is the one before v4.175 (ranked from ``MIN_HISTORY`` sessions on), so nothing changes
until the member picks a shorter window; 365 calendar days is about the 252-session year.
The root is read and solved once over the default window and every window is cut from that
(switching the window is a cache hit).

Speed: the root's rows for the window are read once per (root, newest converted day) and kept
(``_TABLES``, a few roots), solved series are kept per contract (``_SERIES``), so the first surface
of a symbol takes a few seconds (AAPL: ~2.5 s read + ~1 s solve for a 20 % band) and the next ones
milliseconds. The page asks for this AFTER the grid is painted (``/options/api/surface/history``).
"""
from __future__ import annotations

import collections
import datetime as _dt
import logging
import math
import threading
import time

import numpy as np

from . import opt_history
from .opt_constants import RISK_FREE
from .vol_surface import SurfaceError, _f, _sym, _to_date, option_root_of, raw_closes, underlying

log = logging.getLogger(__name__)

HISTORY_DAYS = 252              # the past year of sessions (the screener's IV-rank window): a cap on the read
WINDOWS = (30, 60, 90, 180, 270, 365)   # v4.175: the rank windows a member can pick, in CALENDAR days
DEFAULT_WINDOW = 365
WINDOW_MIN, WINDOW_MAX = 10, 1500       # v4.197: a custom window (the user: "key in custom rank window") in this range
MIN_COVER = 0.8                 # a cell covers at least this share of a (shorter) window to be ranked
MIN_HISTORY = 10                # a rank needs at least this many days of history
SPARK_POINTS = 60               # points in a sparkline (about weekly over a year)
MAX_CONTRACTS = 6000            # legs solved for one grid (the nearest the price first; the rest reported as skipped)
IV_LO, IV_HI, IV_ITERS = 0.01, 5.0, 40     # the bisection bracket (fraction) and halvings (5 / 2^40 ~ 5e-12)
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


def _bs(sig, S, call, log_sk, sqrt_t, rT, disc):
    """Black-Scholes prices for the vols ``sig`` with the per-contract terms precomputed once
    (``log_sk`` = ln(S/K), ``sqrt_t`` = sqrt(T), ``rT`` = r T, ``disc`` = K e^(-rT))."""
    st = sig * sqrt_t
    d1 = (log_sk + rT + 0.5 * sig * sig * sqrt_t * sqrt_t) / st
    d2 = d1 - st
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
    terms = (S, call, np.log(S / K), np.sqrt(T), r * T, K * np.exp(-r * T))   # once, not per iteration
    lo = np.full(p.shape, IV_LO)
    hi = np.full(p.shape, IV_HI)
    ok = (p > _bs(lo, *terms)) & (p < _bs(hi, *terms))
    for _ in range(IV_ITERS):
        mid = 0.5 * (lo + hi)
        above = _bs(mid, *terms) > p
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


def norm_window(window=None) -> int:
    """The rank window in calendar days: a whole number from ``WINDOW_MIN`` to ``WINDOW_MAX``
    (the presets ``WINDOWS`` or a custom one, v4.197); ``DEFAULT_WINDOW`` when none. Raises
    ``ValueError`` on anything else."""
    if window is None or str(window).strip() == "":
        return DEFAULT_WINDOW
    why = "the rank window must be a whole number of days from %d to %d" % (WINDOW_MIN, WINDOW_MAX)
    if isinstance(window, bool):
        raise ValueError(why)
    try:
        f = float(str(window).strip())
    except (TypeError, ValueError):
        f = float("nan")
    if not math.isfinite(f) or f != int(f) or not (WINDOW_MIN <= int(f) <= WINDOW_MAX):
        raise ValueError(why)
    return int(f)


def _cut(days: list[str], end, window_days: int) -> list[str]:
    """The sessions of ``days`` dated within ``window_days`` calendar days before ``end``."""
    if end is None:
        return days
    start = (end - _dt.timedelta(days=int(window_days))).isoformat()
    return [d for d in days if d >= start]


def _window(end_day, root=None, window_days: int = DEFAULT_WINDOW) -> tuple[list[str], float | None]:
    """The converted sessions strictly before ``end_day`` and dated within ``window_days`` calendar
    days of it (at most the last ``HISTORY_DAYS``), and the contract-history manifest's modification
    time - a day converted again (re-published by Massive, or late) changes it, so the caches below
    see a new window."""
    from . import flat_files  # noqa: PLC0415

    end = _to_date(end_day)
    store = opt_history.Store(root=opt_history._root(root), flat_root=flat_files.DEFAULT_ROOT)  # noqa: SLF001
    days = [d for d in store.days() if end is None or d < end.isoformat()]
    return _cut(days, end, window_days)[-HISTORY_DAYS:], store._mtime  # noqa: SLF001 - the manifest's mtime


def history_window(end_day, root=None, window_days: int = DEFAULT_WINDOW) -> list[str]:
    """The converted sessions strictly before ``end_day`` within ``window_days`` calendar days of it."""
    return _window(end_day, root, window_days)[0]


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
        # only contracts still alive at the window's end: a grid for a later day shows no other, and
        # the rows are sorted by root then expiry, so whole row groups of expired contracts are skipped
        alive = pa.scalar(_dt.date.fromisoformat(days[-1]), pa.date32())
        tbl = ds.to_table(filter=(pc.field("root") == str(opt_root).upper()) & (pc.field("expiry") > alive),
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


def cell_stats(current, on_days: np.ndarray, iv: np.ndarray, *, window_days: int | None = None, end=None,
               data_from=None) -> dict | None:
    """The figures of one cell (see the module docstring); None with no history at all.
    ``window_days`` / ``end`` (v4.175): the rank window and the day it ends on - the cell then
    carries ``window_days``, ``covered`` (calendar days from its first session to ``end``) and
    ``why`` (None when ranked). On a window shorter than ``DEFAULT_WINDOW`` the cell must cover
    ``MIN_COVER`` of the window - of the part from ``data_from`` (the first session the converted
    history holds inside the window) when the history itself starts later."""
    n = int(len(iv))
    if n == 0:
        return None
    cur = _f(current)
    lo, hi = float(iv.min()), float(iv.max())
    first = _EPOCH + _dt.timedelta(days=int(on_days[0]))
    out = {"n": n, "lo": round(lo, 2), "hi": round(hi, 2), "first": first,
           "last": _EPOCH + _dt.timedelta(days=int(on_days[-1])), "rank": None, "pct": None}
    out["first"], out["last"] = out["first"].isoformat(), out["last"].isoformat()
    ranked = n >= MIN_HISTORY
    end_d = _to_date(end) if end is not None else None
    if window_days is not None and end_d is not None:
        covered = (end_d - first).days
        out["window_days"], out["covered"], out["why"] = int(window_days), covered, None
        if int(window_days) < DEFAULT_WINDOW:
            span = int(window_days)
            dfrom = _to_date(data_from) if data_from is not None else None
            if dfrom is not None:
                span = min(span, max(0, (end_d - dfrom).days))
            if covered < MIN_COVER * span:
                ranked = False
                out["why"] = "listed %d days ago, shorter than the %d-day window" % (covered, int(window_days))
        if out["why"] is None and n < MIN_HISTORY:
            out["why"] = "only %d %s with a price in the past %d days (a rank needs %d)" % (
                n, "session" if n == 1 else "sessions", int(window_days), MIN_HISTORY)
    if cur is not None and ranked:
        out["rank"] = round(max(0.0, min(1.0, (cur - lo) / (hi - lo))) if hi > lo else 0.5, 4)
        out["pct"] = round(float(np.mean(iv < cur)), 4)
    if n > SPARK_POINTS:                          # evenly spaced, plus the lowest and the highest day
        idx = np.unique(np.r_[np.rint(np.linspace(0, n - 1, SPARK_POINTS)).astype(int), int(iv.argmin()),
                              int(iv.argmax())])
    else:
        idx = np.arange(n)
    out["series"] = [[int(on_days[i]), round(float(iv[i]), 2)] for i in idx]
    return out


def earnings_in(sdb, symbol, start, on) -> dict:
    """The earnings dates the collector has seen for ``symbol`` (``scr_earnings``, v4.143):
    ``past`` = those from ``start`` up to but not including ``on`` (the history window),
    ``next`` = ``{on, when}`` of the first one ON or after ``on`` - the collector's own next date
    (``scr_underlying.earnings_date``, re-read daily from Nasdaq's projections) when it is on or
    after ``on`` and not later than the first kept date, else that kept date - or None; and
    ``coverage`` = how many of the window's weekdays had their calendar read (``fetched`` of
    ``weekdays``), so the page can say when the history is still loading."""
    from . import scr_store  # noqa: PLC0415

    sym = _sym(symbol)
    d = _to_date(on)
    on_s = d.isoformat() if d else None
    past = [e for e in scr_store.earnings_dates(sdb, sym, start, on_s) if on_s is None or e["on"] < on_s]
    kept = scr_store.earnings_dates(sdb, sym, on_s, None)
    nxt = dict(kept[0]) if kept else None
    u = underlying(sdb, sym) or {}
    ed = u.get("earnings_date")
    if ed and (on_s is None or ed >= on_s) and (nxt is None or ed <= nxt["on"]):
        nxt = {"on": ed, "when": nxt["when"] if nxt and nxt["on"] == ed else None}
    weekdays = []
    a = _to_date(start)
    if a and d:
        x = a
        while x < d:
            if x.weekday() < 5:
                weekdays.append(x.isoformat())
            x += _dt.timedelta(days=1)
    fetched = len(scr_store.earnings_days_fetched(sdb, weekdays)) if weekdays else 0
    return {"past": past, "next": nxt, "coverage": {"fetched": fetched, "weekdays": len(weekdays)}}


# ────────────────────────────── the payload for one grid ──────────────────────────────

def grid_history(sdb, symbol, grid: dict, *, root=None, window_days: int = DEFAULT_WINDOW) -> dict:
    """For the surface ``grid`` (``vol_surface.surface``'s result): ``{cells: {"r,c": stats},
    days: n, window: [first, last], window_days, epoch: "1970-01-01", note, ...}``. The leg behind
    a cell follows the grid's ``side`` (put below the price, call at / above, one side only, or both
    averaged). ``window_days`` (v4.175): the rank window, calendar days ending at the grid's day."""
    wd = norm_window(window_days)
    sym = _sym(symbol)
    why = opt_history.available()
    if why:
        raise SurfaceError("the contract history is not set up on this server (%s)" % why)
    oroot = option_root_of(sdb, sym)
    if oroot is None:
        return {"cells": {}, "days": 0, "window": None, "window_days": wd, "epoch": _EPOCH.isoformat(),
                "note": "%s is an index: the daily files carry no index close, so its contracts have no IV history"
                        % sym}
    on = grid["on"]
    spot = float(grid["spot"])
    side = grid.get("side") or "otm"
    strikes = grid["strikes"]
    exps = [_to_date(e["date"]) for e in grid["expiries"]]
    t0 = time.monotonic()
    timing: dict[str, int] = {}

    def lap(what: str, since: float) -> float:          # the phases, in ms (the log and the page show them)
        timing[what] = int((time.monotonic() - since) * 1000)
        return time.monotonic()

    end = _to_date(on)
    days_all = history_window(on, root)              # the default window: read and solved once, cut below
    days = _cut(days_all, end, wd)
    t = lap("window", t0)
    if not days:
        return {"cells": {}, "days": 0, "window": None, "window_days": wd, "epoch": _EPOCH.isoformat(),
                "note": ("no day of the contract history before %s is converted yet" % on if not days_all else
                         "no day of the contract history in the %d days before %s" % (wd, on)),
                "timing_ms": timing}
    spots = raw_closes(sdb, sym, days_all)
    earnings = earnings_in(sdb, sym, days[0], on)
    start_n = _days(_dt.date.fromisoformat(days[0]))
    t = lap("closes", t)

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
    try:
        root_rows(oroot, on, root)                     # the read, timed on its own
        t = lap("read", t)
        series = contract_series(oroot, set(keys), spots, on, root)
        t = lap("solve", t)
    except opt_history.HistoryError as exc:            # the real cause reaches the page
        raise SurfaceError("the contract history could not be read: %s" % exc) from None
    cells: dict[str, dict] = {}
    for (i, j), ks in wanted.items():
        on_d, ivs = _merge_mean([series.get(k) for k in ks])
        if len(on_d):                                   # the chosen window, cut from the default one
            keep = on_d >= start_n
            on_d, ivs = on_d[keep], ivs[keep]
        st = cell_stats(grid["cells"][i][j], on_d, ivs, window_days=wd, end=end, data_from=days[0])
        if st is not None:
            cells["%d,%d" % (i, j)] = st
    skipped = ["%d,%d" % (i, j) for i in range(len(strikes)) for j in range(len(exps))
               if grid["cells"][i][j] is not None and (i, j) not in wanted]
    lap("stats", t)
    lap("total", t0)
    log.info("surface history %s %s: %d cells, %d legs, %d days - window %d ms, closes %d ms, read %d ms, "
             "solve %d ms, stats %d ms, total %d ms", sym, on, len(cells), len(set(keys)), len(days),
             timing["window"], timing["closes"], timing["read"], timing["solve"], timing["stats"], timing["total"])
    return {"cells": cells, "days": len(days), "window": [days[0], days[-1]], "window_days": wd,
            "epoch": _EPOCH.isoformat(), "note": None, "timing_ms": timing,
            "earnings": earnings["past"], "next_earnings": earnings["next"], "earnings_coverage": earnings["coverage"],
            # the grid these figures belong to - the page checks it is still the one on screen
            "strikes": list(strikes), "expiries": [e.isoformat() for e in exps], "spot": spot, "side": side,
            "skipped": skipped,
            "method": "each contract's implied volatility solved from its daily close and the stock's close "
                      "(Black-Scholes, no dividend) over the %d sessions in the %d calendar days before %s"
                      % (len(days), wd, on)}


__all__ = ["HISTORY_DAYS", "WINDOWS", "DEFAULT_WINDOW", "MIN_COVER", "norm_window", "MIN_HISTORY", "SPARK_POINTS", "ncdf_fast", "implied_vols_fast", "contract_key",
           "history_window", "root_rows", "contract_series", "cell_stats", "grid_history"]
