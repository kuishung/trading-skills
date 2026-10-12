"""IV history from Massive's options DAY FILES (v4.138, OPTIONS_SCREENER_DESIGN.md §4): one
trading day's file -> the IV30 of every stock in it at once.

Why
---
The REST way (``opt_massive.iv30_history``) asks Massive for one option contract at a
time: about 70 requests per stock for a year, 12-20 hours for the whole market. Massive's
flat files hold every option contract that traded on a day in ONE file (the
``us_options_opra/day_aggs_v1`` dataset, about 4 MB compressed; ``services/flat_files``
downloads it). So a day is read once and every stock's IV30 for that day comes out of it.

The method is the REST one, number for number
---------------------------------------------
For the day ``day`` and each stock with a raw close ``S`` that day:

* the standard MONTHLY expiries 15-45 calendar days out (``opt_massive.monthly_expiry``:
  the third Friday, the Thursday before when that Friday is a holiday) - weeklies and
  every other expiry are ignored;
* per expiry, the strike nearest ``S`` among the strikes that traded that day (a call or
  a put with a close), the lower one on a tie; none within ``ATM_MAX_DIST_PCT`` (15 %)
  of ``S`` -> that expiry says nothing;
* the call's and the put's IV solved from their CLOSES by Black-Scholes at ``RISK_FREE``
  with no dividend, T = DTE / 365 (``black_scholes.implied_vol``: bisection on
  [0.01, 5.0], 60 halvings), averaged (``statistics.fmean``);
* the expiries' IVs (PERCENT) blended to 30 days in variance-time
  (``option_metrics.iv30_constant_maturity``), rounded to 4 decimals.

That is ``opt_massive._day_iv`` + ``iv30_constant_maturity``. The one difference is
that the file shows EVERY traded strike, so a stock whose REST read missed the nearest
strike can get a nearer one here. ``S`` is the UNADJUSTED close of that day: an option
keeps its listed strike through a split, so a split-adjusted close would pick the wrong
strike. A stock without a raw close that day gets nothing (never the adjusted one).

What is dropped
---------------
* adjusted series - an option root with digits (``GME1``, ``BRKB1``) after a split,
  spin-off or special dividend: its deliverable is not 100 plain shares;
* index options - ``SPX``, ``SPXW``, ``NDX``, ``RUT``, ``VIX`` ... (``INDEX_ROOTS``):
  Stocks Basic has no index closes to price them against;
* a row with no positive close, or with a volume of 0 (no trade, so no closing print -
  Massive's REST bars exist only for traded days, so the REST method never saw one);
* any root that is not one of our stocks (``symbol_map``: ``BRKB`` -> ``BRK-B``).

Speed
-----
A day file has ~400k rows. Rows are filtered on their expiry text before anything is
parsed, so only the 15-45 DTE monthlies (a small share) are grouped. The IV solve runs
on numpy arrays: the same bisection as ``implied_vol``, every contract at once, with the
same operations in the same order (``math.erf`` for the normal CDF, the log / sqrt /
discount per contract by ``math``) - so it returns the scalar solver's numbers to the
last bit. A whole day takes about a second on the laptop (tests/test_scr_flativ.py).

Pure: no database, no network. Units: a contract's IV is a FRACTION, an IV30 PERCENT.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import math
import re
import statistics
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np

from . import option_metrics
from .black_scholes import IV_HI, IV_ITERS, IV_LO
from .opt_constants import ATM_MAX_DIST_PCT, RISK_FREE
from .opt_massive import IV_DTE_HI, IV_DTE_LO, monthly_expiry

# Cash-settled index option roots: they never map to a stock (a bare "SPX" in the
# universe is an index; Stocks Basic has no closes for it).
INDEX_ROOTS = frozenset({
    "SPX", "SPXW", "SPXPM", "XSP", "NDX", "NDXP", "XND", "RUT", "RUTW", "MRUT",
    "VIX", "VIXW", "DJX", "OEX", "XEO",
})
TICKER_TAIL = 15                    # YYMMDD + C|P + strike x 1000 (8 digits)
_ROOT_RE = re.compile(r"^[A-Z]+\d*$")          # root letters (+ the digits of an ADJUSTED root)
_SQRT2 = math.sqrt(2.0)
_ERF = np.frompyfunc(math.erf, 1, 1)           # math.erf element-wise: black_scholes.norm_cdf's own erf


# ────────────────────────────────── small helpers ──────────────────────────────────

def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    s = str(x or "").strip()
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _flt(v) -> float | None:
    """A finite float from a number or CSV text, else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sym(s) -> str:
    return str(s or "").strip().upper()[:20]


# ────────────────────────────────── tickers + symbols ──────────────────────────────────

def parse_ticker(t) -> tuple[str, _dt.date, str, float] | None:
    """``O:AAPL261016C00150000`` -> ``("AAPL", date(2026, 10, 16), "C", 150.0)``; an
    adjusted series keeps its digits (``O:GME1...`` -> root ``GME1``). None when the text
    is not an option ticker."""
    s = str(t or "").strip().upper()
    if len(s) < 3 + TICKER_TAIL or not s.startswith("O:"):
        return None
    root, ymd, right, k = s[2:-TICKER_TAIL], s[-15:-9], s[-9], s[-8:]
    if right not in ("C", "P") or not ymd.isdigit() or not k.isdigit() or not _ROOT_RE.match(root):
        return None
    try:
        expiry = _dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    strike = int(k) / 1000.0
    if strike <= 0:
        return None
    return root, expiry, right, strike


def option_root(symbol, sec_type=None) -> str | None:
    """The OCC option root of one of our symbols (``massive.option_ticker``'s rule: the
    symbol without punctuation - ``BRK-B`` -> ``BRKB``), or None when it can have no
    standard stock options in the files: an index (``I:`` prefix, ``sec_type`` index, or
    an ``INDEX_ROOTS`` root) or a root with digits."""
    s = _sym(symbol)
    if not s or s.startswith("I:") or str(sec_type or "").strip().lower() == "index":
        return None
    root = re.sub(r"[^A-Z0-9]", "", s)
    if not root or not root.isalpha() or root in INDEX_ROOTS:
        return None
    return root


def symbol_map(universe_symbols) -> dict[str, str]:
    """``{option root: our symbol}`` for the universe - ``BRKB`` -> ``BRK-B``. Index
    symbols and roots with digits never map (``option_root``). ``universe_symbols`` is an
    iterable of symbols, or a dict ``{symbol: sec_type}`` (an ``index`` is left out). Two
    symbols with one root (``BRK-B`` and ``BRKB``): the one spelt like the root wins."""
    if isinstance(universe_symbols, dict):
        items = [(_sym(s), st) for s, st in universe_symbols.items()]
    else:
        items = [(_sym(s), None) for s in (universe_symbols or ())]
    out: dict[str, str] = {}
    for sym, st in sorted(items, key=lambda x: x[0]):
        root = option_root(sym, st)
        if root is None:
            continue
        prev = out.get(root)
        if prev is None or (sym == root and prev != root):
            out[root] = sym
    return out


# ────────────────────────────────── the day file ──────────────────────────────────

def _open_text(path):
    p = Path(path)
    with open(p, "rb") as fb:
        gz = fb.read(2) == b"\x1f\x8b"
    if gz:
        return gzip.open(p, "rt", encoding="utf-8-sig", errors="replace", newline="")
    return open(p, encoding="utf-8-sig", errors="replace", newline="")


def read_day(path, *, expiries=None, tally: list | None = None
             ) -> Iterator[tuple[str, float | None, float | None]]:
    """The rows of one options day file (``.csv.gz``, or plain ``.csv``) as ``(ticker,
    close, volume)`` - close / volume None when not a number. Columns are found by the
    header's names (``ticker``, ``close``, ``volume``), so their order does not matter.
    ``expiries`` (a set of ``YYMMDD`` texts) skips every row whose ticker carries another
    expiry before its numbers are parsed - the fast path ``day_iv30`` uses. ``tally`` (a
    one-item list) counts every data line read, skipped or not.
    Raises ``ValueError`` when the header has no ticker or close column."""
    with _open_text(path) as f:
        header = [c.strip().strip('"').lower() for c in f.readline().rstrip("\r\n").split(",")]
        if "ticker" not in header or "close" not in header:
            raise ValueError("not an options day file: the header has no ticker / close column")
        it, ic = header.index("ticker"), header.index("close")
        iv = header.index("volume") if "volume" in header else None
        need = max(it, ic, iv if iv is not None else 0) + 1
        want = set(expiries) if expiries is not None else None
        for line in f:
            if tally is not None:
                tally[0] += 1
            p = line.rstrip("\r\n").split(",")
            if len(p) < need:
                continue
            t = p[it].strip('"')
            if want is not None and t[-15:-9] not in want:
                continue
            yield t, _flt(p[ic]), (_flt(p[iv]) if iv is not None else None)


def _row_triple(r) -> tuple:
    """A row of a caller's iterable (dict, or tuple / list ``(ticker, close[, volume])``)."""
    if isinstance(r, dict):
        return r.get("ticker"), r.get("close"), r.get("volume")
    if isinstance(r, (tuple, list)) and len(r) >= 2:
        return r[0], r[1], (r[2] if len(r) > 2 else None)
    return None, None, None


def monthly_window(day) -> dict[str, tuple[_dt.date, int]]:
    """``{YYMMDD: (expiry, DTE)}`` - the standard monthly expiries 15-45 calendar days
    after ``day`` (``opt_massive.monthly_expiry``; the REST history's own window)."""
    d = _to_date(day)
    if d is None:
        return {}
    hi = d + _dt.timedelta(days=IV_DTE_HI)
    out = {}
    y, m = d.year, d.month
    while (y, m) <= (hi.year, hi.month):
        e = monthly_expiry(y, m)
        dte = (e - d).days
        if IV_DTE_LO <= dte <= IV_DTE_HI:
            out[e.strftime("%y%m%d")] = (e, dte)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ────────────────────────────────── the IV solve (vectorised) ──────────────────────────────────

def _ncdf(x: np.ndarray) -> np.ndarray:
    """``black_scholes.norm_cdf`` element-wise - same erf, same operations."""
    return 0.5 * (1.0 + _ERF(x / _SQRT2).astype(np.float64))


def _bs_prices(sig, S, K, T, is_call, log_sk, sqrt_t, disc_r) -> np.ndarray:
    """``black_scholes(...).price`` at ``RISK_FREE``, q = 0, element-wise: the call is
    S N(d1) - K e^(-rT) N(d2), the put K e^(-rT) N(-d2) - S N(-d1), in that order."""
    d1 = (log_sk + (RISK_FREE - 0.0 + 0.5 * sig * sig) * T) / (sig * sqrt_t)
    d2 = d1 - sig * sqrt_t
    n1 = _ncdf(np.where(is_call, d1, -d2))
    n2 = _ncdf(np.where(is_call, d2, -d1))
    kd = K * disc_r
    return np.where(is_call, S * n1 - kd * n2, kd * n1 - S * n2)


def implied_vols(prices, S, K, T, is_call) -> np.ndarray:
    """``black_scholes.implied_vol`` for many contracts at once (FRACTION; NaN where the
    scalar solver says None: no positive price, no time, or a price at / under the model's
    floor or at / over its ceiling). The same 60-step bisection on [0.01, 5.0] at
    ``RISK_FREE``; ``log(S/K)``, ``sqrt(T)`` and ``exp(-rT)`` come from ``math`` per
    contract and the normal CDF from ``math.erf``, so each answer is the scalar one."""
    p = np.asarray(prices, dtype=np.float64)
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    is_call = np.asarray(is_call, dtype=bool)
    n = p.shape[0]
    out = np.full(n, np.nan)
    if n == 0:
        return out
    good = (p > 0) & (S > 0) & (K > 0) & (T > 0)
    if not good.any():
        return out
    idx = np.nonzero(good)[0]
    p, S, K, T, is_call = p[idx], S[idx], K[idx], T[idx], is_call[idx]
    log_sk = np.array([math.log(s / k) for s, k in zip(S.tolist(), K.tolist())])
    sqrt_t = np.array([math.sqrt(t) for t in T.tolist()])
    disc_r = np.array([math.exp(-RISK_FREE * t) for t in T.tolist()])
    args = (S, K, T, is_call, log_sk, sqrt_t, disc_r)
    lo_p = _bs_prices(np.full(p.shape, IV_LO), *args)
    hi_p = _bs_prices(np.full(p.shape, IV_HI), *args)
    ok = ~((p <= lo_p) | (p >= hi_p))
    lo = np.full(p.shape, IV_LO)
    hi = np.full(p.shape, IV_HI)
    for _ in range(IV_ITERS):
        mid = 0.5 * (lo + hi)
        above = _bs_prices(mid, *args) > p
        hi = np.where(above, mid, hi)
        lo = np.where(above, lo, mid)
    iv = 0.5 * (lo + hi)
    iv[~ok] = np.nan
    out[idx] = iv
    return out


# ────────────────────────────────── one day -> every stock's IV30 ──────────────────────────────────

def day_iv30(path_or_rows, day, closes_raw: dict, roots: dict, *, raw: bool = False,
             stats: dict | None = None) -> dict[str, float]:
    """``{symbol: IV30 PERCENT}`` for ``day`` from one options day file (a path) or its
    rows (an iterable of ``(ticker, close, volume)`` tuples or dicts). ``closes_raw`` =
    ``{symbol: that day's UNADJUSTED close}``; ``roots`` = ``{option root: symbol}``
    (``symbol_map``). The REST history's method (see the module docstring); values are
    rounded to 4 decimals like ``iv30_history``'s points (``raw=True``: unrounded). A
    stock with no usable expiry, or no positive raw close, is left out.

    ``stats`` (a dict, optional) is filled with ``rows`` (the file's data rows, or the rows
    given), ``kept`` (rows of the 15-45 DTE monthlies of our stocks), ``contracts`` (IVs
    solved), ``symbols`` (IV30s out) and ``ms``."""
    t0 = time.monotonic()
    d = _to_date(day)
    if d is None:
        raise ValueError("day_iv30: a date is needed")
    on = d.isoformat()
    window = monthly_window(d)
    spots = {}
    for s, c in (closes_raw or {}).items():
        f = _flt(c)
        if f is not None and f > 0:
            spots[_sym(s)] = f
    rmap = {str(k).strip().upper(): _sym(v) for k, v in (roots or {}).items()}
    n_rows = n_kept = 0
    tally = [0]
    from_file = isinstance(path_or_rows, (str, Path))
    # (symbol, YYMMDD) -> {strike: [call close, put close]}
    groups: dict[tuple[str, str], dict[float, list]] = {}
    if window and spots and rmap:
        if from_file:
            src: Iterable = read_day(path_or_rows, expiries=set(window), tally=tally)
        else:
            src = (_row_triple(r) for r in (path_or_rows or ()))
        for t, close, vol in src:
            n_rows += 1
            if not isinstance(t, str):
                continue
            ex = t[-15:-9]
            if ex not in window:
                continue
            root = t[2:-TICKER_TAIL]
            sym = rmap.get(root)
            if sym is None or not root.isalpha() or root in INDEX_ROOTS or not t.startswith("O:"):
                continue
            spot = spots.get(sym)
            if spot is None:
                continue
            right, k = t[-9], t[-8:]
            if right not in ("C", "P") or not k.isdigit():
                continue
            c = _flt(close)
            if c is None or c <= 0:
                continue
            v = _flt(vol)
            if v is not None and v <= 0:
                continue
            strike = int(k) / 1000.0
            if strike <= 0:
                continue
            legs = groups.setdefault((sym, ex), {}).setdefault(strike, [None, None])
            legs[0 if right == "C" else 1] = c
            n_kept += 1

    # the strike nearest the raw close per (stock, expiry) -> the contracts to solve
    prices, S, K, T, calls, owner = [], [], [], [], [], []
    picked: list[tuple[str, str]] = []
    for (sym, ex), by_k in groups.items():
        spot = spots[sym]
        k = min(by_k, key=lambda x: (abs(x - spot), x))
        if abs(k - spot) / spot > ATM_MAX_DIST_PCT:
            continue
        gi = len(picked)
        picked.append((sym, ex))
        dte = window[ex][1]
        for j, is_call in ((0, True), (1, False)):
            c = by_k[k][j]
            if c is not None:
                prices.append(c)
                S.append(spot)
                K.append(k)
                T.append(dte / 365.0)
                calls.append(is_call)
                owner.append(gi)
    ivs = implied_vols(prices, S, K, T, calls).tolist() if prices else []

    per_group: list[list[float]] = [[] for _ in picked]
    for gi, v in zip(owner, ivs):
        if v == v:                                  # not NaN: the scalar solver's None
            per_group[gi].append(v)
    by_sym: dict[str, dict] = {}
    for (sym, ex), vals in zip(picked, per_group):
        if not vals:
            continue
        e, dte = window[ex]
        by_sym.setdefault(sym, {})[e.isoformat()] = {"dte": dte, "atm_iv": statistics.fmean(vals) * 100.0}
    out: dict[str, float] = {}
    for sym, by_exp in by_sym.items():
        iv30 = option_metrics.iv30_constant_maturity(by_exp, on)
        if iv30 is not None and iv30 > 0:
            out[sym] = iv30 if raw else round(iv30, 4)
    if stats is not None:
        stats.update(rows=tally[0] if from_file else n_rows, kept=n_kept, contracts=len(prices),
                     symbols=len(out), ms=int((time.monotonic() - t0) * 1000))
    return out


__all__ = ["INDEX_ROOTS", "parse_ticker", "option_root", "symbol_map", "read_day", "monthly_window",
           "implied_vols", "day_iv30"]
