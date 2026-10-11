"""The screener's market in memory: every kept contract and every underlying of the latest
market pass as numpy arrays (OPTIONS_SCREENER_DESIGN.md §5).

A ``Frame`` is immutable once built (its arrays are read-only) so any number of requests can
screen it at once. ``current()`` hands out the latest one:

* the first call loads it synchronously from the screener DB;
* afterwards, at most every ``RELOAD_CHECK_S`` seconds, a request starts a background check
  (``_needs_reload``). A new Frame is built in that thread and swapped in when:
  (a) the cached frame is empty and the DB holds contracts it has not seen;
  (b) no pass that STORED contracts has finished yet (the first real pass is running - a
      legacy finished pass that read nothing does not count) and, at most every
      ``GROW_RELOAD_S`` (and never more often than 4x the last load time), the DB has more
      contracts or more security types than the frame - so the first pass's results grow;
  (c) any frame with underlyings of unknown security type, when more types are filed (the
      identity job landed after the pass finished);
  (d) ``scr_pass`` has a newer finished pass (or the ET date has rolled, so every DTE moved).
  Requests keep using the previous Frame meanwhile; a lock guarantees there are never two
  loads at once. ``reloading()`` is True only while such a load really runs.
* v4.137: while the cached frame is EMPTY, a request checks at most every ``EMPTY_CHECK_S``
  and, when there is something to load, waits up to ``EMPTY_WAIT_S`` for it - so the first
  request after the first rows land already gets them instead of "no data".

The DB is read through the ORM (``screener_db.session()``, column selects, ``yield_per``
chunks of ``LOAD_CHUNK`` rows) so ~1M contracts never sit in memory as ORM objects.

Contract arrays (``frame.c``) are sorted by (symbol, right, expiry, strike) - calls before
puts, strikes ascending inside an expiry - and carry the group keys the pairing code uses:

* ``g_sre``  the (symbol, right, expiry) group of a contract; ``gstart`` / ``gend`` its bounds
* ``g_se``   the (symbol, expiry) group (calls and puts together), numbered by (symbol, expiry)
* ``km``     the strike in thousandths of a dollar (int64)
* ``ck``     ``g_sre * KEY_SPAN + km`` - strictly ascending over the whole array, so a
             (group, strike) is found with one ``searchsorted``
* ``sk``     ``g_se * KEY_SPAN + km`` - ascending within the calls (and within the puts)

Underlying arrays (``frame.u``) are indexed by symbol code (codes are alphabetical, so a
code sort is a symbol sort). String fields (sec_type, exchange, trend) are small int codes
into ``SEC_TYPES`` / ``EXCHANGES`` / ``TRENDS`` (-1 = unknown).

Units: a contract's ``iv`` is a FRACTION (``iv_pct`` is the percent copy the fields show);
every per-underlying IV / HV figure is PERCENT; probabilities are PERCENT (0-100).
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import threading
import time

import numpy as np

from .. import clock
from ..opt_constants import RISK_FREE

log = logging.getLogger("tst.screener.frame")

RELOAD_CHECK_S = 30.0          # a request checks for a newer pass at most this often
EMPTY_CHECK_S = 5.0            # an EMPTY cached frame is checked for new rows at most this often
EMPTY_WAIT_S = 3.0             # ...and the request waits at most this long for that load
GROW_RELOAD_S = 120.0          # a growing first-pass frame / late security types: at most this often
GROW_LOAD_FACTOR = 4.0         # ...and never more often than this many times the last load time
LOAD_CHUNK = 50_000            # ORM yield_per / partition size while loading contracts
KEY_SPAN = 1_000_000_000       # a strike in thousandths of a dollar stays under this ($1M)
EXP_SPAN = 1_000_000           # day offsets inside the (symbol, expiry) key
MIN_T_DAYS = 0.5               # opt_massive.model_price's floor on time to expiry, in days
EPOCH = _dt.date(1970, 1, 1)

SEC_TYPES = ("stock", "etf", "index", "other")
EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "INDEX", "OTHER")
TRENDS = ("up", "down", "sideways")

# the ScrContract / ScrUnderlying columns the frame reads (all of them the engine uses)
C_COLS = ("symbol", "expiry", "right", "strike", "weekly", "price", "last", "chg_pct", "volume",
          "oi", "vol_prev", "oi_prev", "iv", "delta", "gamma", "theta", "vega", "last_trade",
          "as_of")
_C_NUM = ("strike", "price", "last", "chg_pct", "volume", "oi", "vol_prev", "oi_prev", "iv",
          "delta", "gamma", "theta", "vega")
U_COLS = ("symbol", "name", "sec_type", "exchange", "spot", "prev_close", "chg_pct",
          "stock_volume", "avg_vol20", "avg_vol50", "sma20", "sma50", "sma200", "rsi14", "atr14",
          "atr_pct", "hv20", "hv60", "hi52", "lo52", "perf5", "perf20", "trend", "iv30",
          "iv30_prev", "iv_rank", "iv_pct", "iv_hi", "iv_lo", "iv_n", "exp_move30", "call_vol",
          "put_vol", "call_oi", "put_oi", "n_contracts", "earnings_date", "history_done")
_U_NUM = ("spot", "prev_close", "chg_pct", "stock_volume", "avg_vol20", "avg_vol50", "sma20",
          "sma50", "sma200", "rsi14", "atr14", "atr_pct", "hv20", "hv60", "hi52", "lo52", "perf5",
          "perf20", "iv30", "iv30_prev", "iv_rank", "iv_pct", "iv_hi", "iv_lo", "iv_n",
          "exp_move30", "call_vol", "put_vol", "call_oi", "put_oi", "n_contracts")

# the results area's texts (fix plan 2.T: T-20, T-21, T-22). Why there is no data is the
# collector's to say - the page's empty panel (routes/options_page.py) shows it.
NO_DATA = "No option data loaded yet."
FIRST_PASS_WARN = ("The first market pass is still running: results cover {n:,} of {total:,} underlyings "
                   "read by {hm} ET (refreshed every 2 minutes).")
FIRST_PASS_WARN_NO_TOTAL = ("The first market pass is still running: results cover the {n:,} underlyings "
                            "read by {hm} ET (refreshed every 2 minutes).")
ZERO_PASS_WARN = ("The last market pass ({what}, finished {hm} ET) stored no contracts - see the "
                  "collector status above.")
_KIND_WORDS = {"eod": "end-of-day", "cycle": "intraday", "manual": "manual"}


# ─────────────────────────────────── math ───────────────────────────────────

def ncdf(x) -> np.ndarray:
    """The standard normal CDF, vectorised (numpy has no erf): Numerical Recipes' erfc
    Chebyshev fit, fractional error < 1.2e-7 everywhere. NaN in, NaN out."""
    x = np.asarray(x, dtype=float)
    z = np.abs(x) * (1.0 / math.sqrt(2.0))
    t = 1.0 / (1.0 + 0.5 * z)
    poly = (-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (
        -0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (
            -0.82215223 + t * 0.17087277)))))))))
    erfc = t * np.exp(poly)
    return np.where(x >= 0, 1.0 - 0.5 * erfc, 0.5 * erfc)


def prob_above(S, X, sigma, T, r: float = RISK_FREE) -> np.ndarray:
    """P(S_T > X) = N(d2), d2 = (ln(S/X) + (r - sigma^2/2) T) / (sigma sqrt T) - the
    lognormal, risk-neutral convention of §5 (a FRACTION). X <= 0 -> 1; a missing or
    non-positive S / sigma / T -> NaN."""
    S, X, sig, T = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in (S, X, sigma, T)))
    ok = (S > 0) & (sig > 0) & (T > 0) & np.isfinite(X)
    with np.errstate(all="ignore"):
        d2 = (np.log(S / X) + (r - 0.5 * sig * sig) * T) / (sig * np.sqrt(T))
        p = ncdf(d2)
    p = np.where(X <= 0, 1.0, p)
    return np.where(ok, p, np.nan)


# ─────────────────────────────────── helpers ───────────────────────────────────

def _as_date(v) -> _dt.date | None:
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _f64(seq) -> np.ndarray:
    """A float array; None -> NaN."""
    if isinstance(seq, np.ndarray) and seq.dtype.kind == "f":
        return seq.astype(float, copy=False)
    try:
        return np.asarray(seq, dtype=float)
    except (TypeError, ValueError):
        out = []
        for v in seq:
            try:
                out.append(float(v) if v is not None else np.nan)
            except (TypeError, ValueError):
                out.append(np.nan)
        return np.asarray(out, dtype=float)


def _days(seq) -> np.ndarray:
    """ISO dates / ``date`` objects / datetime64 -> float days since 1970-01-01 (NaN = none)."""
    if isinstance(seq, np.ndarray) and seq.dtype.kind in "fiu":
        return seq.astype(float)
    try:
        a = np.asarray(seq, dtype="datetime64[D]")
    except (TypeError, ValueError):
        a = np.asarray([_as_date(v) for v in seq], dtype="datetime64[D]")
    out = a.astype(np.int64).astype(float)
    out[np.isnat(a)] = np.nan
    return out


def _epoch_s(seq) -> np.ndarray:
    """Naive-UTC datetimes (or epoch seconds) -> float epoch seconds (NaN = none)."""
    if isinstance(seq, np.ndarray) and seq.dtype.kind in "fiu":
        return seq.astype(float)
    vals = list(seq)
    try:
        a = np.asarray(vals, dtype="datetime64[s]")
    except (TypeError, ValueError):
        conv = []
        for v in vals:
            if isinstance(v, _dt.datetime) and v.tzinfo is not None:
                v = v.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            conv.append(v if isinstance(v, _dt.datetime) else None)
        a = np.asarray(conv, dtype="datetime64[s]")
    out = a.astype(np.int64).astype(float)
    out[np.isnat(a)] = np.nan
    return out


def _codes(seq, vocab: tuple, upper: bool) -> np.ndarray:
    look = {v.upper() if upper else v.lower(): i for i, v in enumerate(vocab)}
    out = np.full(len(seq), -1, dtype=np.int8)
    for i, v in enumerate(seq):
        if v is not None:
            s = str(v).strip()
            out[i] = look.get(s.upper() if upper else s.lower(), -1)
    return out


def _iso_utc(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, _dt.datetime):
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t.replace(microsecond=0).isoformat() + "Z"
    return str(t)


# ─────────────────────────────────── the Frame ───────────────────────────────────

class Frame:
    """One immutable snapshot of the market. Build it with ``from_records`` /
    ``from_columns`` (tests, tools) or let ``current()`` load it from the screener DB."""

    def __init__(self, *, c: dict, u: dict, g: dict, symbols: tuple, names: tuple, meta: dict,
                 today: _dt.date):
        self.c = c
        self.u = u
        self.g = g
        self.symbols = symbols
        self.names = names
        self.sym_index = {s: i for i, s in enumerate(symbols)}
        self.meta = meta
        self.today = today
        self.today_d = (today - EPOCH).days
        self.n = int(len(c["strike"]))
        self.nu = int(len(symbols))
        self.loaded_mono: float | None = None    # load_from_db: when it was read (monotonic s)
        self._cache: dict = {}
        self._cache_lock = threading.Lock()

    @property
    def empty(self) -> bool:
        return self.n == 0

    def cached(self, name: str, build):
        """A derived structure computed once per frame on first use (deterministic, so a
        rare double build under contention is harmless; the lock just avoids the waste)."""
        hit = self._cache.get(name)
        if hit is None:
            with self._cache_lock:
                hit = self._cache.get(name)
                if hit is None:
                    hit = build()
                    self._cache[name] = hit
        return hit

    # ---- the per-right subsets the cross-right pairing searches (sk ascending) ----
    def right_index(self, put: bool):
        def build():
            pos = np.flatnonzero(self.c["is_put"] == (1 if put else 0))
            return pos, self.c["sk"][pos]
        return self.cached("rpos_P" if put else "rpos_C", build)

    # ---- (symbol, right, strike) order for calendars: expiries ascending inside ----
    def strike_order(self):
        def build():
            c = self.c
            order = np.lexsort((c["exp"], c["km"], c["is_put"], c["sym"]))
            n = len(order)
            rank = np.empty(n, dtype=np.int64)
            rank[order] = np.arange(n)
            if n:
                s, p, k = c["sym"][order], c["is_put"][order], c["km"][order]
                brk = np.empty(n, dtype=bool)
                brk[0] = True
                brk[1:] = (s[1:] != s[:-1]) | (p[1:] != p[:-1]) | (k[1:] != k[:-1])
                starts = np.flatnonzero(brk)
                ends = np.r_[starts[1:], n]
                gend = ends[np.cumsum(brk) - 1]
            else:
                gend = np.zeros(0, dtype=np.int64)
            return order, rank, gend
        return self.cached("strike_order", build)

    # ---- the end (exclusive) of each (symbol, right) run of sre groups ----
    def sr_group_end(self):
        def build():
            gs, gp = self.g["sym"], self.g["is_put"]
            G = len(gs)
            if not G:
                return np.zeros(0, dtype=np.int64)
            brk = np.empty(G, dtype=bool)
            brk[0] = True
            brk[1:] = (gs[1:] != gs[:-1]) | (gp[1:] != gp[:-1])
            starts = np.flatnonzero(brk)
            ends = np.r_[starts[1:], G]
            return ends[np.cumsum(brk) - 1]
        return self.cached("sr_group_end", build)

    # ---- constructors ----
    @classmethod
    def empty_frame(cls, warning: str | None = None, today=None, meta: dict | None = None) -> "Frame":
        b = _Builder()
        m = dict(meta or {})
        if warning:
            m.setdefault("warnings", []).append(warning)
        return b.build(m, today, source=m.get("source", "empty"))

    @classmethod
    def from_records(cls, contracts: list[dict], underlyings: list[dict], meta: dict | None = None,
                     today=None) -> "Frame":
        """Contracts / underlyings as dicts keyed like ``ScrContract`` / ``ScrUnderlying``
        columns (missing keys read as None). ``today`` (a date or ISO string) pins the ET
        session date the DTEs count from; default ``clock.et_date()``."""
        ccols = {k: [r.get(k) for r in contracts] for k in C_COLS}
        ucols = {k: [r.get(k) for r in underlyings] for k in U_COLS}
        return cls.from_columns(ccols, ucols, meta=meta, today=today, source="records")

    @classmethod
    def from_columns(cls, contracts: dict, underlyings: dict, meta: dict | None = None, today=None,
                     source: str = "columns") -> "Frame":
        """Column-wise input (sequences or numpy arrays, keys as ``C_COLS`` / ``U_COLS``);
        the fast path for big synthetic markets."""
        b = _Builder()
        b.add_underlyings(underlyings)
        b.add_contracts(contracts)
        return b.build(dict(meta or {}), today, source=source)


class _Builder:
    """Accumulates contract chunks and underlying rows, then assembles a Frame. Symbols get
    provisional codes as they are met; ``build`` renumbers them alphabetically."""

    def __init__(self):
        self.codes: dict[str, int] = {}
        self.parts: dict[str, list] = {k: [] for k in ("sym", "exp", "is_put", "weekly", "last_trade",
                                                       *_C_NUM)}
        self.u_parts: list[tuple[np.ndarray, dict]] = []
        self.as_of: _dt.datetime | None = None

    def _code(self, s) -> int:
        return self.codes.setdefault(str(s), len(self.codes))

    def add_underlyings(self, cols: dict) -> None:
        syms = list(cols.get("symbol") or [])
        if not syms:
            return
        codes = np.fromiter((self._code(s) for s in syms), dtype=np.int64, count=len(syms))
        self.u_parts.append((codes, {k: (list(cols.get(k)) if cols.get(k) is not None else [None] * len(syms))
                                     for k in U_COLS if k != "symbol"}))

    def add_contracts(self, cols: dict) -> None:
        syms = cols.get("symbol")
        n = 0 if syms is None else len(syms)
        if not n:
            return
        self.parts["sym"].append(np.fromiter((self._code(s) for s in syms), dtype=np.int64, count=n))
        self.parts["exp"].append(_days(cols["expiry"]))
        rights = cols.get("right")
        ra = np.asarray(rights if rights is not None else ["C"] * n, dtype="U1")
        self.parts["is_put"].append(((ra == "P") | (ra == "p")).astype(np.int8))
        wk = cols.get("weekly")
        self.parts["weekly"].append(np.asarray(wk if wk is not None else [False] * n, dtype=bool).astype(float))
        lt = cols.get("last_trade")
        self.parts["last_trade"].append(_epoch_s(lt) if lt is not None else np.full(n, np.nan))
        for k in _C_NUM:
            v = cols.get(k)
            self.parts[k].append(_f64(v) if v is not None else np.full(n, np.nan))
        ao = cols.get("as_of")
        if ao is not None:
            if isinstance(ao, np.ndarray) and ao.dtype.kind in "fiu":
                hi = np.nanmax(ao) if len(ao) else np.nan
                best = _dt.datetime(1970, 1, 1) + _dt.timedelta(seconds=float(hi)) if np.isfinite(hi) else None
            else:
                best = max((t for t in ao if isinstance(t, _dt.datetime)), default=None)
            if best is not None and best.tzinfo is not None:
                best = best.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            if best is not None and (self.as_of is None or best > self.as_of):
                self.as_of = best

    def build(self, meta: dict, today, source: str) -> Frame:
        t0 = time.perf_counter()
        today = _as_date(today) or clock.et_date()
        td = (today - EPOCH).days
        meta = dict(meta)
        meta.setdefault("warnings", [])

        # ---- symbols, alphabetical ----
        prov = list(self.codes.keys())                 # provisional code -> symbol
        order = sorted(range(len(prov)), key=lambda i: prov[i])
        remap = np.empty(len(prov), dtype=np.int64)
        remap[np.asarray(order, dtype=np.int64)] = np.arange(len(prov))
        symbols = tuple(prov[i] for i in order)
        U = len(symbols)

        # ---- underlyings ----
        u: dict[str, np.ndarray] = {k: np.full(U, np.nan) for k in _U_NUM}
        u["sec_type"] = np.full(U, -1, dtype=np.int8)
        u["exchange"] = np.full(U, -1, dtype=np.int8)
        u["trend"] = np.full(U, -1, dtype=np.int8)
        u["earn_day"] = np.full(U, np.nan)
        u["history_done"] = np.zeros(U)
        names: list = [None] * U
        for codes, cols in self.u_parts:
            at = remap[codes]
            for k in _U_NUM:
                u[k][at] = _f64(cols[k])
            u["sec_type"][at] = _codes(cols["sec_type"], SEC_TYPES, upper=False)
            u["exchange"][at] = _codes(cols["exchange"], EXCHANGES, upper=True)
            u["trend"][at] = _codes(cols["trend"], TRENDS, upper=False)
            u["earn_day"][at] = _days(cols["earnings_date"])
            u["history_done"][at] = np.asarray([1.0 if v else 0.0 for v in cols["history_done"]])
            for i, nm in zip(at.tolist(), cols["name"]):
                names[i] = nm

        # ---- contracts ----
        if self.parts["sym"]:
            raw = {k: np.concatenate(v) for k, v in self.parts.items()}
            raw["sym"] = remap[raw["sym"]]
        else:
            raw = {k: np.zeros(0) for k in self.parts}
            raw["sym"] = np.zeros(0, dtype=np.int64)
            raw["is_put"] = np.zeros(0, dtype=np.int8)
        strike = raw["strike"]
        exp = raw["exp"]
        keep = np.isfinite(exp) & (exp >= td) & np.isfinite(strike) & (strike > 0) & (strike * 1000 < KEY_SPAN)
        n_dropped = int(len(keep) - keep.sum())
        raw = {k: v[keep] for k, v in raw.items()}
        raw["exp"] = raw["exp"].astype(np.int64)
        raw["sym"] = raw["sym"].astype(np.int32)
        order_c = np.lexsort((raw["strike"], raw["exp"], raw["is_put"], raw["sym"]))
        c = {k: v[order_c] for k, v in raw.items()}
        n = len(order_c)

        sym, exp, is_put, K = c["sym"], c["exp"], c["is_put"], c["strike"]
        call = is_put == 0
        # spot: the live one, else the previous close
        spot_u = np.where(np.isfinite(u["spot"]) & (u["spot"] > 0), u["spot"], u["prev_close"])
        spot_u = np.where(spot_u > 0, spot_u, np.nan)
        u["spot"] = spot_u
        S = spot_u[sym] if n else np.zeros(0)
        p = np.where(np.isfinite(c["price"]), c["price"], c["last"])
        c["price"] = p
        sig = np.where(c["iv"] > 0, c["iv"], np.nan)
        c["iv"] = sig
        dte = (exp - td).astype(float)
        T = np.maximum(dte, MIN_T_DAYS) / 365.0
        with np.errstate(all="ignore"):
            c["spot"] = S
            c["dte"] = dte
            c["T"] = T
            c["iv_pct"] = sig * 100.0
            c["moneyness"] = np.where(call, (S - K) / S, (K - S) / S) * 100.0
            intr = np.where(call, np.maximum(S - K, 0.0), np.maximum(K - S, 0.0))
            c["intrinsic"] = intr
            c["tp"] = p - intr
            c["tp_pct"] = (p - intr) / S * 100.0
            be = np.where(call, K + p, K - p)
            c["breakeven"] = be
            c["breakeven_pct"] = (be - S) / S * 100.0
            pa_k = prob_above(S, K, sig, T)
            itm = np.where(call, pa_k, 1.0 - pa_k)
            c["itm_prob"] = itm * 100.0
            c["otm_prob"] = (1.0 - itm) * 100.0
            pa_be = prob_above(S, be, sig, T)
            c["profit_prob"] = np.where(call, pa_be, 1.0 - pa_be) * 100.0
            vol, oi = c["volume"], c["oi"]
            c["vol_oi"] = np.where(oi > 0, vol / oi, np.nan)
            c["oi_chg"] = oi - c["oi_prev"]
            c["oi_chg_pct"] = np.where(c["oi_prev"] > 0, (oi - c["oi_prev"]) / c["oi_prev"] * 100.0, np.nan)
            c["vol_chg_pct"] = np.where(c["vol_prev"] > 0, (vol - c["vol_prev"]) / c["vol_prev"] * 100.0, np.nan)
            c["premium"] = p * 100.0 * vol
            c["dist_strike_pct"] = (K - S) / S * 100.0
            c["iv_hv"] = sig * 100.0 / (u["hv20"][sym] if n else np.zeros(0))
            c["exp_move"] = S * sig * np.sqrt(T)
            c["exp_move_pct"] = sig * np.sqrt(T) * 100.0
            earn = u["earn_day"][sym] if n else np.zeros(0)
            eb = (earn >= td) & (earn <= exp)
            c["earn_before"] = eb.astype(float)
            c["exp_before_earn"] = (~eb).astype(float)
            c["exp_f"] = exp.astype(float)
            c["is_put_f"] = is_put.astype(float)

        # ---- group keys ----
        if n:
            brk = np.empty(n, dtype=bool)
            brk[0] = True
            brk[1:] = (sym[1:] != sym[:-1]) | (is_put[1:] != is_put[:-1]) | (exp[1:] != exp[:-1])
            g_sre = (np.cumsum(brk) - 1).astype(np.int64)
            starts = np.flatnonzero(brk)
            ends = np.r_[starts[1:], n]
            se_key = sym.astype(np.int64) * EXP_SPAN + (exp - exp.min())
            _, g_se = np.unique(se_key, return_inverse=True)
            g_se = g_se.astype(np.int64).reshape(-1)
        else:
            g_sre = np.zeros(0, dtype=np.int64)
            starts = np.zeros(0, dtype=np.int64)
            ends = np.zeros(0, dtype=np.int64)
            g_se = np.zeros(0, dtype=np.int64)
        km = np.rint(K * 1000.0).astype(np.int64)
        c["g_sre"] = g_sre
        c["gstart"] = starts[g_sre] if n else np.zeros(0, dtype=np.int64)
        c["gend"] = ends[g_sre] if n else np.zeros(0, dtype=np.int64)
        c["g_se"] = g_se
        c["km"] = km
        c["ck"] = g_sre * KEY_SPAN + km
        c["sk"] = g_se * KEY_SPAN + km
        g = {"start": starts, "end": ends, "sym": sym[starts] if n else np.zeros(0, dtype=np.int32),
             "is_put": is_put[starts] if n else np.zeros(0, dtype=np.int8),
             "exp": exp[starts] if n else np.zeros(0, dtype=np.int64)}

        # ---- underlying derived figures ----
        with np.errstate(all="ignore"):
            for w in ("20", "50", "200"):
                sma = u["sma" + w]
                u["pct_sma" + w] = np.where(sma > 0, (spot_u / sma - 1.0) * 100.0, np.nan)
            u["pct_hi52"] = np.where(u["hi52"] > 0, (spot_u / u["hi52"] - 1.0) * 100.0, np.nan)
            u["pct_lo52"] = np.where(u["lo52"] > 0, (spot_u / u["lo52"] - 1.0) * 100.0, np.nan)
            u["iv_chg"] = u["iv30"] - u["iv30_prev"]
            u["iv30_hv20"] = np.where(u["hv20"] > 0, u["iv30"] / u["hv20"], np.nan)
            vol0 = np.nan_to_num(c["volume"]) if n else np.zeros(0)
            oi0 = np.nan_to_num(c["oi"]) if n else np.zeros(0)
            for key, wts, put_side in (("call_vol", vol0, False), ("put_vol", vol0, True),
                                       ("call_oi", oi0, False), ("put_oi", oi0, True)):
                side = (is_put == 1) if put_side else (is_put == 0)
                derived = np.bincount(sym, weights=np.where(side, wts, 0.0), minlength=U)[:U] if n else np.zeros(U)
                u[key] = np.where(np.isfinite(u[key]), u[key], derived)
            u["total_vol"] = u["call_vol"] + u["put_vol"]
            u["total_oi"] = u["call_oi"] + u["put_oi"]
            u["pc_vol"] = np.where(u["call_vol"] > 0, u["put_vol"] / u["call_vol"], np.nan)
            u["pc_oi"] = np.where(u["call_oi"] > 0, u["put_oi"] / u["call_oi"], np.nan)
            u["vol_oi_total"] = np.where(u["total_oi"] > 0, u["total_vol"] / u["total_oi"], np.nan)
            u["days_to_earn"] = np.where(u["earn_day"] >= td, u["earn_day"] - td, np.nan)
            u["sym_code"] = np.arange(U, dtype=float)
            u["sec_type_f"] = u["sec_type"].astype(float)
            u["exchange_f"] = u["exchange"].astype(float)
            u["trend_f"] = u["trend"].astype(float)
            u["iv_pctl"] = u["iv_pct"]

        for arr in (*c.values(), *u.values(), *g.values()):
            arr.setflags(write=False)

        with_contracts = np.zeros(U, dtype=bool)
        if n:
            with_contracts[np.unique(sym)] = True
        lt = c["last_trade"]
        ref_ts = None
        if self.as_of is not None:
            ref_ts = (self.as_of - _dt.datetime(1970, 1, 1)).total_seconds()
        elif n and np.isfinite(lt).any():
            ref_ts = float(np.nanmax(lt))
        meta.update({
            "source": source,
            "empty": n == 0,
            "today": today.isoformat(),
            "n_contracts": n,
            "n_underlyings": int(with_contracts.sum()),
            "n_symbols": U,
            "n_expired_dropped": n_dropped,
            "as_of": _iso_utc(self.as_of),
            "ref_ts": ref_ts,
            "iv_history_done": int(np.sum(u["history_done"][with_contracts] > 0)),
            "iv_history_total": int(with_contracts.sum()),
            "n_sec_type_unknown": int(np.sum(u["sec_type"][with_contracts] < 0)),
            "loaded_at": _iso_utc(_dt.datetime.now(_dt.timezone.utc)),
            "build_ms": int((time.perf_counter() - t0) * 1000),
        })
        for k in ("pass_id", "pass_kind", "session", "started", "finished", "load_ms"):
            meta.setdefault(k, None)
        return Frame(c=c, u=u, g=g, symbols=symbols, names=tuple(names), meta=meta, today=today)


# ─────────────────────────────────── loading from the DB ───────────────────────────────────

def _sqlite_missing(url: str) -> bool:
    if not url.startswith("sqlite") or ":///" not in url:
        return False
    path = url.split(":///", 1)[1].split("?", 1)[0]
    if not path or path.startswith(":memory:"):
        return False
    return not os.path.exists(path)


def _transpose(rows, keys) -> dict:
    if not rows:
        return {k: [] for k in keys}
    cols = list(zip(*rows))
    return {k: list(cols[i]) for i, k in enumerate(keys)}


def _mono() -> float:
    """The clock the reload throttles use (a function so tests can move it)."""
    return time.monotonic()


def _et_hm(t) -> str:
    """A naive-UTC datetime as ``HH:MM`` on the US Eastern clock."""
    if not isinstance(t, _dt.datetime):
        t = _dt.datetime.now(_dt.timezone.utc)
    return clock.et_now(t).strftime("%H:%M")


def _session_words(kind, session) -> str:
    """``eod`` + ``2026-10-09`` -> ``end-of-day Fri Oct 9`` (plain words for the warnings)."""
    d = _as_date(session)
    when = f"{d.strftime('%a %b')} {d.day}" if d else ""
    words = _KIND_WORDS.get(str(kind or ""), str(kind or "")).strip()
    return " ".join(x for x in (words, when) if x) or "market pass"


def load_from_db(today=None) -> Frame:
    """Build a Frame from the screener DB. Never raises: a DB that does not exist, is not
    migrated, or cannot be read yields an empty Frame whose ``meta.warnings`` says why.

    Besides the data, ``meta`` records what the reload checks compare against: ``max_cid``
    (the highest ``scr_contract.id`` read), ``n_typed`` (underlyings with a security type),
    ``run_id`` / ``run_total`` (the newest unfinished pass and its symbol count) and
    ``pass_contracts`` (the last finished pass's contract count). ``real_pass_id`` /
    ``real_pass_kind`` / ``real_session`` / ``real_finished`` name the newest finished pass
    that STORED contracts (``n_contracts`` > 0, or unknown) - None while only a pass that
    read nothing has finished (the first real pass is then still running: the frame grows
    and says so). ``frame.loaded_mono`` (and the module's ``_loaded_mono``) stamp when it was
    read."""
    global _loaded_mono
    fr = _load_from_db(today)
    fr.loaded_mono = _mono()
    _loaded_mono = fr.loaded_mono
    return fr


def _load_from_db(today=None) -> Frame:
    t0 = time.perf_counter()
    try:
        from sqlalchemy import func, or_, select

        from ... import screener_db
        from ...screener_models import ScrContract, ScrPass, ScrUnderlying
    except Exception as exc:  # noqa: BLE001
        return Frame.empty_frame(f"{NO_DATA} (screener modules unavailable: {exc})", today,
                                 meta={"source": "db"})
    url = screener_db.database_url()
    if _sqlite_missing(url):
        return Frame.empty_frame(NO_DATA + " (the screener database has not been created yet)", today,
                                 meta={"source": "db"})
    b = _Builder()
    meta: dict = {"source": "db", "warnings": []}
    try:
        with screener_db.session() as s:
            last = s.execute(
                select(ScrPass.id, ScrPass.kind, ScrPass.session, ScrPass.started, ScrPass.finished,
                       ScrPass.n_contracts)
                .where(ScrPass.finished.is_not(None)).order_by(ScrPass.id.desc()).limit(1)).first()
            # the newest finished pass that stored contracts (NULL = unknown counts as stored);
            # pass_id stays the newest of ANY count - rule (d) and the T-22 warning read it
            real = s.execute(
                select(ScrPass.id, ScrPass.kind, ScrPass.session, ScrPass.finished)
                .where(ScrPass.finished.is_not(None),
                       or_(ScrPass.n_contracts.is_(None), ScrPass.n_contracts > 0))
                .order_by(ScrPass.id.desc()).limit(1)).first()
            run = s.execute(
                select(ScrPass.id, ScrPass.n_symbols)
                .where(ScrPass.finished.is_(None)).order_by(ScrPass.id.desc()).limit(1)).first()
            # read BEFORE the rows: a row filed during the load only makes the next check
            # reload once more, it is never missed
            meta["max_cid"] = s.execute(select(func.max(ScrContract.id))).scalar()
            meta["n_typed"] = int(s.execute(select(func.count(ScrUnderlying.id))
                                            .where(ScrUnderlying.sec_type.is_not(None))).scalar() or 0)
            urows = s.execute(select(*[getattr(ScrUnderlying, k) for k in U_COLS])).all()
            b.add_underlyings(_transpose(urows, U_COLS))
            # the read time: one aggregate instead of a datetime parsed per contract
            ao = s.execute(select(func.max(ScrContract.as_of))).scalar()
            if isinstance(ao, _dt.datetime) and ao.tzinfo is not None:
                ao = ao.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            b.as_of = ao if isinstance(ao, _dt.datetime) else None
            cols = tuple(k for k in C_COLS if k != "as_of")
            res = s.execute(select(*[getattr(ScrContract, k) for k in cols])
                            .execution_options(yield_per=LOAD_CHUNK))
            for part in res.partitions(LOAD_CHUNK):
                b.add_contracts(_transpose(part, cols))
    except Exception as exc:  # noqa: BLE001 - no such table / locked / unreadable
        log.warning("screener frame load failed: %s", exc)
        return Frame.empty_frame(f"{NO_DATA} (the screener database could not be read: "
                                 f"{type(exc).__name__})", today, meta={"source": "db"})
    if last is not None:
        meta.update(pass_id=last.id, pass_kind=last.kind, session=last.session,
                    started=_iso_utc(last.started), finished=_iso_utc(last.finished),
                    pass_contracts=last.n_contracts)
    meta["run_id"] = run.id if run is not None else None
    meta["run_total"] = run.n_symbols if run is not None else None
    meta.update(real_pass_id=real.id if real is not None else None,
                real_pass_kind=real.kind if real is not None else None,
                real_session=real.session if real is not None else None,
                real_finished=_iso_utc(real.finished) if real is not None else None)
    frame = b.build(meta, today, source="db")
    if real is None and not frame.empty:          # T-21: no pass that stored contracts has finished
        n, total = frame.meta["n_underlyings"], frame.meta.get("run_total")
        hm = _et_hm(b.as_of)
        frame.meta["warnings"].append(FIRST_PASS_WARN.format(n=n, total=total, hm=hm)
                                      if total and total >= n else
                                      FIRST_PASS_WARN_NO_TOTAL.format(n=n, hm=hm))
    if frame.empty and last is not None and not (last.n_contracts or 0):
        frame.meta["warnings"].append(ZERO_PASS_WARN.format(what=_session_words(last.kind, last.session),
                                                            hm=_et_hm(last.finished)))
    if frame.empty and not frame.meta["warnings"]:
        frame.meta["warnings"].append(NO_DATA)
    frame.meta["load_ms"] = int((time.perf_counter() - t0) * 1000)
    log.info("screener frame loaded: %s contracts, %s underlyings, pass %s, %s ms",
             frame.n, frame.meta["n_underlyings"], frame.meta.get("pass_id"), frame.meta["load_ms"])
    return frame


def _needs_reload(cur: Frame | None) -> bool:
    """Is there something worth loading for ``cur``? Cheap ORM reads only (an id, a max,
    a count); see the module docstring for rules (a)-(d). Never raises."""
    if cur is None:
        return True
    try:
        if cur.today != clock.et_date():
            return True
        from sqlalchemy import func, select

        from ... import screener_db
        from ...screener_models import ScrContract, ScrPass, ScrUnderlying

        if _sqlite_missing(screener_db.database_url()):
            return False
        m = cur.meta
        with screener_db.session() as s:
            row = s.execute(select(ScrPass.id).where(ScrPass.finished.is_not(None))
                            .order_by(ScrPass.id.desc()).limit(1)).first()
            latest = row[0] if row else None

            def max_cid():
                return s.execute(select(func.max(ScrContract.id))).scalar()

            def n_typed() -> int:
                return int(s.execute(select(func.count(ScrUnderlying.id))
                                     .where(ScrUnderlying.sec_type.is_not(None))).scalar() or 0)

            # (a) an empty frame and contracts it has not seen (an unchanged max id means
            #     rows the loader drops anyway - expired - so no reload every few seconds)
            if cur.empty:
                cid = max_cid()
                if cid is not None and cid != m.get("max_cid"):
                    return True
            # (d) a newer finished pass
            if latest is not None and latest != m.get("pass_id"):
                return True
            if cur.empty:
                return False
            loaded = cur.loaded_mono
            elapsed = float("inf") if loaded is None else _mono() - loaded
            if elapsed < GROW_RELOAD_S:
                return False
            # (b) the first REAL pass is still running (no finished pass, or only ones that
            #     read nothing): the frame grows (never on later passes)
            real = m["real_pass_id"] if "real_pass_id" in m else m.get("pass_id")
            first = latest is None or real is None
            if first and elapsed >= max(GROW_RELOAD_S,
                                        GROW_LOAD_FACTOR * (m.get("load_ms") or 0) / 1000.0):
                cid = max_cid()
                if cid is not None and (m.get("max_cid") is None or cid > m["max_cid"]):
                    return True
                if n_typed() != (m.get("n_typed") or 0):
                    return True
            # (c) security types landed after the frame was read
            if (m.get("n_sec_type_unknown") or 0) > 0 and n_typed() > (m.get("n_typed") or 0):
                return True
            return False
    except Exception as exc:  # noqa: BLE001
        log.debug("screener reload check failed: %s", exc)
        return False


# ─────────────────────────────────── current() ───────────────────────────────────

_state_lock = threading.Lock()      # guards the fields below
_load_lock = threading.Lock()       # held for the whole of a load: never two at once
_frame: Frame | None = None
_pinned = False                     # set_current(): tests pin a frame, no background checks
_last_check = float("-inf")
_last_empty_check = float("-inf")
_loading = False                    # a load that _needs_reload asked for is running
_loaded_mono: float | None = None   # when the last load_from_db() read the DB
_bg: threading.Thread | None = None


def reloading() -> bool:
    """True only while a load is really running (never for a check that found nothing).
    Callers add it to a COPY of ``meta`` - a frame's own meta never carries it."""
    return _loading


def _set_loading(on: bool) -> None:
    global _loading
    with _state_lock:
        _loading = bool(on)


def current() -> Frame:
    """The latest Frame. The first call loads synchronously; later calls may start a
    background reload (see the module docstring) and return the previous Frame meanwhile -
    except while that frame is EMPTY, when the request waits up to ``EMPTY_WAIT_S`` for it."""
    global _frame, _last_check
    f = _frame
    if f is None:
        with _load_lock:
            if _frame is None:
                _set_loading(True)
                try:
                    _frame = load_from_db()
                finally:
                    _set_loading(False)
                with _state_lock:
                    _last_check = _mono()
            return _frame
    if _pinned:
        return f
    if f.empty:
        return _empty_fast_path(f)
    _maybe_schedule()
    return f


def _empty_fast_path(f: Frame) -> Frame:
    """An empty cached frame: at most every ``EMPTY_CHECK_S`` start the background check
    (or join the one running) and wait up to ``EMPTY_WAIT_S`` for it. Returns the new frame
    when it finished in time, else ``f`` (``reloading()`` then tells the page why)."""
    global _last_check, _last_empty_check, _bg
    now = _mono()
    with _state_lock:
        if _pinned or _frame is not f:              # swapped meanwhile: hand out the new one
            return _frame if _frame is not None else f
        t = _bg if (_bg is not None and _bg.is_alive()) else None
        if t is None:
            if now - _last_empty_check < EMPTY_CHECK_S:
                return f
            _last_empty_check = now
            _last_check = now
            t = threading.Thread(target=_bg_reload, name="screener-frame-reload", daemon=True)
            _bg = t
            t.start()
    t.join(EMPTY_WAIT_S)
    cur = _frame
    return cur if cur is not None else f


def _maybe_schedule() -> None:
    global _last_check, _bg
    now = _mono()
    with _state_lock:
        if _pinned or now - _last_check < RELOAD_CHECK_S:
            return
        if _bg is not None and _bg.is_alive():
            return
        _last_check = now
        _bg = threading.Thread(target=_bg_reload, name="screener-frame-reload", daemon=True)
        _bg.start()


def _bg_reload() -> None:
    global _frame
    if not _load_lock.acquire(blocking=False):
        return
    try:
        cur = _frame                                # re-read under the lock: one load only
        if _pinned or not _needs_reload(cur):
            return
        _set_loading(True)
        new = load_from_db()
        if new.empty and cur is not None and not cur.empty and new.meta.get("pass_id") is None:
            log.warning("screener reload produced no data; keeping the previous frame")
            return
        if not _pinned:
            _frame = new
    except Exception as exc:  # noqa: BLE001 - a failed reload keeps the old frame
        log.warning("screener background reload failed: %s", exc)
    finally:
        _set_loading(False)                         # after the swap: never "done" with the old frame
        _load_lock.release()


def warm() -> None:
    """Start the first load in a background thread (call it at app startup): a request that
    arrives meanwhile waits for that load instead of starting its own."""
    def _go():
        try:
            current()
        except Exception as exc:  # noqa: BLE001
            log.warning("screener warm-up failed: %s", exc)
    threading.Thread(target=_go, name="screener-frame-warm", daemon=True).start()


def wait_reload(timeout: float = 30.0) -> None:
    """Join a running background reload (tests and tools)."""
    t = _bg
    if t is not None:
        t.join(timeout)


def set_current(frame: Frame | None) -> None:
    """Test hook: pin ``frame`` as the current one (no background reloads while pinned)."""
    global _frame, _pinned
    with _state_lock:
        _frame = frame
        _pinned = frame is not None


def reset() -> None:
    """Test hook: forget the current frame; the next ``current()`` loads from the DB."""
    global _frame, _pinned, _last_check, _last_empty_check, _loading, _loaded_mono
    wait_reload(5.0)
    with _state_lock:
        _frame = None
        _pinned = False
        _last_check = float("-inf")
        _last_empty_check = float("-inf")
        _loading = False
        _loaded_mono = None
