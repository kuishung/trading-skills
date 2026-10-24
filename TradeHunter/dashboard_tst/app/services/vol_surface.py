"""The volatility surface (v4.139, OPTIONS_SCREENER_DESIGN.md §14): one underlying on one
day, implied volatility by STRIKE (rows, the y-axis) and MATURITY (columns, the x-axis) -
the grid of financetrainingcourse.com's "Creating the volatility surface data set using
implied volatilities" (one observation date, one underlying, every strike x maturity, each
cell that option's implied vol; the user, 2026-10-13).

Where the numbers come from
---------------------------
* ``latest`` - the screener DB's chain (``scr_contract``: the last market pass, Massive's
  own IV per contract, 15 min delayed; only contracts with open interest or volume are
  kept) and the underlying's spot from the same pass (``scr_underlying``).
* a past session ``YYYY-MM-DD`` - the contract history (``opt_history``: Massive's options
  day files as Parquet): every contract that TRADED that day, its CLOSE -> Black-Scholes
  implied vol against the stock's UNADJUSTED close that day (``scr_underlying_daily.
  close_raw`` - an option keeps its listed strike through a split), r = ``RISK_FREE``, no
  dividend, T = calendar days / 365 - the IV history's own method
  (``scr_flativ.implied_vols``, the scalar solver's numbers to the last bit).

Which option fills a cell (``side``)
------------------------------------
``otm`` (default) - the put below the spot, the call at or above it: the out-of-the-money
option is the one that trades, and an in-the-money American put carries early-exercise
value Black-Scholes does not price; ``call`` / ``put`` - that side only (the article uses
calls); ``mid`` - the average of the call and the put when both have one. A cell with no
contract (or no IV) stays EMPTY - nothing is interpolated or smoothed. The ``ATM`` row is
each expiry's at-the-money IV (the strike nearest the spot; call and put averaged).

Common strikes only (``common``, the default; v4.140, the user: *"Common strikes only do a
default for this with a box ticked. If untick it will show all data"*): exchanges list some
strikes for a few expiries only - the $2.50 strikes of the near weeklies, the wider steps of
the far expiries - and every one of them is a row, mostly empty. With ``common`` a strike is
kept only when at least half (``COMMON_SHARE``) of the expiries shown have a cell at it;
``common=False`` shows every strike.

The tile figures (``extra``, v4.166; the user: *"configurable tiles in the vol surface ... the
top to show the greeks. the bottom will show the last price"*): beside ``cells`` a parallel grid
of the SAME contract's delta / gamma / theta / vega, last trade, model price, day change, open
interest and volume - from the latest pass only (``has_greeks``); a past day's rows carry only
the IV solved from the close, so its ``extra`` cells are all null. On ``mid`` the figures are the
call's (the put's when there is no call) and ``mixed`` says the IV is the average of both.

Pure grid building (``build``) is separate from the DB / Parquet reads (``latest_rows``,
``history_rows``) so it is tested without either. IVs in the result are PERCENT.
"""
from __future__ import annotations

import datetime as _dt
import math
import statistics

from . import opt_history
from .opt_constants import RISK_FREE

SIDES = ("otm", "call", "put", "mid")
BANDS = (10, 20, 30, 50, 0)          # strikes within +-N % of the spot (0 = every strike)
DTES = (30, 60, 90, 180, 365, 730, 0)  # expiries up to N calendar days out (0 = every expiry)
DEFAULT_SIDE, DEFAULT_BAND, DEFAULT_DTE = "otm", 20, 365
IV_LO, IV_HI = 0.005, 5.0            # a contract IV (FRACTION) outside this is left out
MAX_STRIKES = 400                    # rows: the strikes nearest the spot are kept
MAX_EXPIRIES = 80                    # columns: the nearest expiries are kept
SCALE_LO_PCT, SCALE_HI_PCT = 5, 95   # the colour scale's ends (percentiles of the cells)
COMMON_SHARE = 0.5                   # common strikes only: a strike with a cell in at least half the expiries
# the tile figures (v4.166): the row key -> the short key in an ``extra`` cell
FIGURES = (("delta", "d"), ("gamma", "g"), ("theta", "t"), ("vega", "v"), ("last", "last"), ("price", "price"),
           ("chg_pct", "chg"), ("oi", "oi"), ("volume", "vol"), ("liq", "liq"), ("liq_rank", "rk"))
LIQ_VOLUME_WEIGHT = 0.5              # liquidity depth = log(1 + open interest) + this x log(1 + today's volume)


class SurfaceError(ValueError):
    """A surface that cannot be drawn, in plain words for the page."""


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


def _f(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fig(v, src: str):
    """A tile figure, cleaned: a number (open interest / volume as whole numbers) or None."""
    f = _f(v)
    if f is None:
        return None
    return int(round(f)) if src in ("oi", "volume") else f


def _sym(s) -> str:
    return str(s or "").strip().upper()[:20]


def is_monthly(expiry) -> bool:
    """The standard monthly expiry (the third Friday, the Thursday before on a holiday)."""
    from .opt_massive import monthly_expiry  # noqa: PLC0415

    e = _to_date(expiry)
    return e is not None and monthly_expiry(e.year, e.month) == e


def norm_options(side=None, band=None, max_dte=None, expiries=None) -> dict:
    """The surface options, cleaned: unknown values fall back to the defaults."""
    s = str(side or DEFAULT_SIDE).strip().lower()
    try:
        b = int(band) if band is not None and str(band).strip() != "" else DEFAULT_BAND
    except (TypeError, ValueError):
        b = DEFAULT_BAND
    try:
        m = int(max_dte) if max_dte is not None and str(max_dte).strip() != "" else DEFAULT_DTE
    except (TypeError, ValueError):
        m = DEFAULT_DTE
    e = str(expiries or "all").strip().lower()
    return {"side": s if s in SIDES else DEFAULT_SIDE, "band": b if b in BANDS else DEFAULT_BAND,
            "max_dte": m if m in DTES else DEFAULT_DTE, "expiries": e if e in ("all", "monthly") else "all"}


def keep(strike: float, dte: int, spot: float, *, band: int, max_dte: int) -> bool:
    """Is a contract inside the surface's window? ``dte`` >= 1 (a contract expiring that
    day has no time value to read an IV from), up to ``max_dte`` (0 = no cap), strike within
    +-``band`` % of the spot (0 = every strike)."""
    if dte < 1 or (max_dte and dte > max_dte):
        return False
    if band and spot > 0 and abs(strike - spot) / spot > band / 100.0:
        return False
    return True


def liquidity_ranks(rows) -> None:
    """v4.181 (the user: a liquidity indicator on the tiles): give every row of one underlying's
    chain a ``liq`` in 0..1 - where its depth sits among ALL the contracts of that chain (0 =
    the thinnest, 1 = the deepest; ties share their mid rank). Depth = log(1 + open interest)
    + ``LIQ_VOLUME_WEIGHT`` x log(1 + today's volume): open interest is yesterday's standing
    size, today's volume (15 min delayed) a lighter, fresher vote. Ranked within the chain, not
    on a dollar cut-off, so the scale means the same on a mega-cap and a thin name. A row with
    neither figure gets None. No bid/ask is in the data, so the spread itself is unknown."""
    scored = []
    for r in rows or ():
        oi, vol = _f(r.get("oi")), _f(r.get("volume"))
        if oi is None and vol is None:
            r["liq"] = None
            r["liq_rank"] = None
            continue
        d = math.log1p(max(0.0, oi or 0.0)) + LIQ_VOLUME_WEIGHT * math.log1p(max(0.0, vol or 0.0))
        scored.append((d, r))
    n = len(scored)
    if not n:
        return
    if n == 1:
        scored[0][1]["liq"] = 1.0
        scored[0][1]["liq_rank"] = 1
        return
    scored.sort(key=lambda t: t[0])
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scored[j + 1][0] == scored[i][0]:
            j += 1
        mid = (i + j) / 2.0                        # the shared percentile of a tie
        for k in range(i, j + 1):
            scored[k][1]["liq"] = round(mid / (n - 1), 4)
            # v4.184 (the user: "xx/xx of the liquidity ranking"): 1 = the deepest contract of the chain;
            # a tie shares the better rank (1 + the number of contracts strictly deeper)
            scored[k][1]["liq_rank"] = n - j
        i = j + 1


def _pctile(vals: list[float], p: float) -> float:
    v = sorted(vals)
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


# ────────────────────────────────── the grid ──────────────────────────────────

def build(rows, spot, on, *, side=DEFAULT_SIDE, band=DEFAULT_BAND, max_dte=DEFAULT_DTE,
          expiries="all", common: bool = True) -> dict:
    """The surface grid from ``rows`` (dicts ``expiry`` (date / ISO), ``right`` C|P,
    ``strike``, ``iv`` FRACTION or None) for ``spot`` on day ``on``: the options used
    (``side``, ``band``, ``max_dte``, ``exp_mode`` all | monthly) and
    ``{expiries: [{date, dte, years, monthly}], strikes: [...] ascending, cells:
    [[iv PERCENT | None per expiry] per strike], atm: [ATM iv per expiry], atm_strikes,
    spot_row (strikes below the spot), scale: [lo, hi] | None, n_cells, n_contracts, common,
    hidden_strikes (the strikes ``common`` left out), extra: [[{d, g, t, v, last, price, chg, oi,
    vol[, mixed]} | None per expiry] per strike]}``. ``extra`` is the tile figures of the contract
    each cell's IV came from (None where the cell is empty or the rows carry no figures - a past
    day's rows have only the IV)."""
    o = norm_options(side, band, max_dte, expiries)
    d = _to_date(on)
    s = _f(spot)
    if d is None:
        raise SurfaceError("no date for the surface")
    if s is None or s <= 0:
        raise SurfaceError("no price for the underlying on that day, so the strikes cannot be placed")
    by: dict[tuple[float, _dt.date], dict[str, float]] = {}
    figs: dict[tuple[float, _dt.date], dict[str, dict]] = {}   # the same legs' tile figures (latest rows only)
    n_contracts = 0
    for r in rows or ():
        e = _to_date(r.get("expiry"))
        k = _f(r.get("strike"))
        iv = _f(r.get("iv"))
        right = str(r.get("right") or "").upper()[:1]
        if e is None or k is None or k <= 0 or right not in ("C", "P"):
            continue
        if iv is None or not (IV_LO <= iv <= IV_HI):
            continue
        dte = (e - d).days
        if not keep(k, dte, s, band=o["band"], max_dte=o["max_dte"]):
            continue
        if o["expiries"] == "monthly" and not is_monthly(e):
            continue
        by.setdefault((k, e), {})[right] = iv
        if any(src in r for src, _key in FIGURES):
            figs.setdefault((k, e), {})[right] = {key: _fig(r.get(src), src) for src, key in FIGURES}
        n_contracts += 1

    def cell(legs: dict, k: float) -> float | None:
        if o["side"] == "call":
            return legs.get("C")
        if o["side"] == "put":
            return legs.get("P")
        if o["side"] == "mid":
            vals = [v for v in (legs.get("C"), legs.get("P")) if v is not None]
            return statistics.fmean(vals) if vals else None
        return legs.get("P") if k < s else legs.get("C")

    def leg_of(legs: dict, k: float) -> str | None:
        """The right whose figures a cell carries (``mid``: the call's, else the put's)."""
        if o["side"] == "call":
            return "C"
        if o["side"] == "put":
            return "P"
        if o["side"] == "mid":
            return "C" if legs.get("C") is not None else ("P" if legs.get("P") is not None else None)
        return "P" if k < s else "C"

    def extra_of(k: float, e: _dt.date) -> dict | None:
        if (k, e) not in values:
            return None
        legs = by[(k, e)]
        f = (figs.get((k, e)) or {}).get(leg_of(legs, k) or "")
        if f is None:
            return None
        out = dict(f)
        if o["side"] == "mid":
            out["mixed"] = legs.get("C") is not None and legs.get("P") is not None
        return out

    exps = sorted({e for (_k, e) in by})
    if len(exps) > MAX_EXPIRIES:
        exps = exps[:MAX_EXPIRIES]
    exp_set = set(exps)
    values: dict[tuple[float, _dt.date], float] = {}
    for (k, e), legs in by.items():
        if e not in exp_set:
            continue
        v = cell(legs, k)
        if v is not None:
            values[(k, e)] = v
    strikes = sorted({k for (k, _e) in values})
    if len(strikes) > MAX_STRIKES:
        near = sorted(strikes, key=lambda k: (abs(k - s), k))[:MAX_STRIKES]
        strikes = sorted(near)
    hidden = 0
    if common and strikes and exps:
        need = max(1, math.ceil(COMMON_SHARE * len(exps)))
        fill = {k: sum(1 for e in exps if (k, e) in values) for k in strikes}
        keep_k = [k for k in strikes if fill[k] >= need]
        if keep_k:                                   # (never an empty grid: then every strike stays)
            hidden = len(strikes) - len(keep_k)
            strikes = keep_k
    kept = set(strikes)
    used = {e for (k, e) in values if k in kept}
    exps = [e for e in exps if e in used]
    cells = [[(round(values[(k, e)] * 100.0, 2) if (k, e) in values else None) for e in exps] for k in strikes]
    extra = [[extra_of(k, e) for e in exps] for k in strikes]

    # the ATM row: per expiry, the strike nearest the spot that has a call or a put IV
    atm, atm_k = [], []
    for e in exps:
        ks = [k for (k, ee) in by if ee == e]
        if not ks:
            atm.append(None)
            atm_k.append(None)
            continue
        k = min(ks, key=lambda x: (abs(x - s), x))
        legs = by[(k, e)]
        vals = [v for v in (legs.get("C"), legs.get("P")) if v is not None]
        atm.append(round(statistics.fmean(vals) * 100.0, 2) if vals else None)
        atm_k.append(k)
    flat = [v for row in cells for v in row if v is not None]
    scale = ([round(_pctile(flat, SCALE_LO_PCT), 2), round(_pctile(flat, SCALE_HI_PCT), 2)] if flat else None)
    return {
        "side": o["side"], "band": o["band"], "max_dte": o["max_dte"], "exp_mode": o["expiries"],
        "on": d.isoformat(), "spot": round(s, 4),
        "expiries": [{"date": e.isoformat(), "dte": (e - d).days, "years": round((e - d).days / 365.0, 4),
                      "monthly": is_monthly(e)} for e in exps],
        "strikes": strikes, "cells": cells, "atm": atm, "atm_strikes": atm_k,
        "spot_row": sum(1 for k in strikes if k < s), "scale": scale,
        "n_cells": len(flat), "n_contracts": n_contracts, "common": bool(common), "hidden_strikes": hidden,
        "extra": extra,
    }


# ────────────────────────────────── the reads ──────────────────────────────────

def _models():
    from ..screener_models import ScrContract, ScrUnderlying, ScrUnderlyingDaily  # noqa: PLC0415

    return ScrContract, ScrUnderlying, ScrUnderlyingDaily


def underlying(sdb, symbol) -> dict | None:
    """The screener's row for ``symbol`` (name, type, spot) or None."""
    _C, U, _D = _models()
    u = sdb.query(U).filter(U.symbol == _sym(symbol)).first()
    if u is None:
        return None
    return {"symbol": u.symbol, "name": u.name, "sec_type": u.sec_type, "spot": u.spot,
            "spot_src": u.spot_src, "spot_as_of": u.spot_as_of, "earnings_date": u.earnings_date}


def option_root_of(sdb, symbol) -> str | None:
    """The OCC option root the day files use for ``symbol`` (``BRK-B`` -> ``BRKB``), None for
    an index (no stock close to price its options against)."""
    from .scr_flativ import option_root  # noqa: PLC0415 - numpy

    u = underlying(sdb, symbol)
    return option_root(_sym(symbol), (u or {}).get("sec_type"))


def raw_closes(sdb, symbol, days) -> dict[str, float]:
    """``{day: the stock's UNADJUSTED close}`` for ``days`` (a day without one is left out)."""
    _C, _U, D = _models()
    want = sorted({str(x)[:10] for x in days or ()})
    if not want:
        return {}
    q = (sdb.query(D.on, D.close_raw)
            .filter(D.symbol == _sym(symbol), D.on >= want[0], D.on <= want[-1], D.close_raw.isnot(None)))
    keep_days = set(want)
    out = {}
    for on, c in q.all():
        f = _f(c)
        if on in keep_days and f is not None and f > 0:
            out[on] = f
    return out


def solve_ivs(prices, spots, strikes, years, calls) -> list[float | None]:
    """Black-Scholes implied vols (FRACTION, None where there is none) - the IV history's
    solver (``scr_flativ.implied_vols``)."""
    from .scr_flativ import implied_vols  # noqa: PLC0415 - numpy

    if not prices:
        return []
    out = implied_vols(prices, spots, strikes, years, calls).tolist()
    return [v if v == v else None for v in out]


def unknown_symbol_text(sdb, symbol, n: int = 3) -> str:
    """The message for a symbol the screener does not list - with the closest listed
    tickers when there are any (v4.176, the user typed MFST for MSFT): ``MFST is not in the
    screener's list of optionable stocks - did you mean MSFT?``."""
    import difflib  # noqa: PLC0415

    sym = _sym(symbol)
    text = "%s is not in the screener's list of optionable stocks" % sym
    try:
        _C, U, _D = _models()
        listed = [s for (s,) in sdb.query(U.symbol).all() if s]
    except Exception:  # noqa: BLE001 - a hint only
        listed = []
    close = difflib.get_close_matches(sym, listed, n=n, cutoff=0.6) if sym and listed else []
    # a transposition (MFST / MSFT) scores low in difflib: add tickers made of the same letters
    same = sorted(s for s in listed if len(s) == len(sym) and sorted(s) == sorted(sym) and s != sym)
    hints = list(dict.fromkeys(same + close))[:n]
    if hints:
        text += " - did you mean %s?" % " or ".join(hints)
    return text


def latest_rows(sdb, symbol) -> tuple[list[dict], dict]:
    """The last market pass's chain for ``symbol`` (Massive's IV per contract, plus the tile
    figures ``delta, gamma, theta, vega, last, price, chg_pct, oi, volume`` - None when missing)
    and its meta ``{source, on, spot, spot_src, as_of, name}``."""
    C, _U, _D = _models()
    sym = _sym(symbol)
    u = underlying(sdb, sym)
    q = (sdb.query(C.expiry, C.right, C.strike, C.iv, C.session, C.as_of, C.delta, C.gamma, C.theta, C.vega,
                   C.last, C.price, C.chg_pct, C.oi, C.volume)
            .filter(C.symbol == sym))
    rows, session, as_of = [], None, None
    for e, right, k, iv, sess, ao, de, ga, th, ve, la, pr, ch, oi, vo in q.all():
        rows.append({"expiry": e, "right": right, "strike": k, "iv": iv, "delta": de, "gamma": ga, "theta": th,
                     "vega": ve, "last": la, "price": pr, "chg_pct": ch, "oi": oi, "volume": vo})
        if sess and (session is None or sess > session):
            session = sess
        if ao and (as_of is None or ao > as_of):
            as_of = ao
    if u is None and not rows:
        raise SurfaceError(unknown_symbol_text(sdb, sym))
    if not rows:
        raise SurfaceError("the last market pass stored no option contracts for %s" % sym)
    liquidity_ranks(rows)                # v4.181: each contract's depth among the whole chain
    from . import clock  # noqa: PLC0415

    on = session or clock.et_today()
    return rows, {"source": "latest", "on": str(on)[:10], "spot": (u or {}).get("spot"),
                  "spot_src": (u or {}).get("spot_src"), "as_of": as_of, "name": (u or {}).get("name"),
                  "liq_n": sum(1 for r in rows if r.get("liq") is not None)}   # v4.184: the chain's ranked contracts


def history_rows(sdb, symbol, day, *, band=DEFAULT_BAND, max_dte=DEFAULT_DTE, root=None) -> tuple[list[dict], dict]:
    """The contracts of ``symbol`` that traded on ``day`` (the contract history) with an IV
    solved from each one's close, and the meta ``{source, on, spot, name, traded}``. Only
    the contracts inside the surface's window are solved (both sides)."""
    sym = _sym(symbol)
    d = _to_date(day)
    if d is None:
        raise SurfaceError("pick a day")
    u = underlying(sdb, sym)
    oroot = option_root_of(sdb, sym)
    if oroot is None:
        raise SurfaceError("%s is an index - the daily files have no index close to price its options against, "
                           "so only the latest surface is available" % sym)
    if opt_history.available():
        raise SurfaceError("the contract history is not set up on this server (%s)" % opt_history.available())
    tbl = opt_history.chain(oroot, d, columns=["expiry", "right", "strike", "close", "volume"], root=root)
    if tbl is None:
        raise SurfaceError("%s is not in the contract history yet (its daily file has not been converted)"
                           % d.isoformat())
    if tbl.num_rows == 0:
        raise SurfaceError("no %s option traded on %s" % (sym, d.isoformat()))
    spot = raw_closes(sdb, sym, [d.isoformat()]).get(d.isoformat())
    if spot is None:
        raise SurfaceError("no %s stock close is on file for %s, so its options cannot be priced"
                           % (sym, d.isoformat()))
    o = norm_options(None, band, max_dte, None)
    prices, S, K, T, calls, meta = [], [], [], [], [], []
    for r in tbl.to_pylist():
        e, k, c = r["expiry"], _f(r["strike"]), _f(r["close"])
        if e is None or k is None or c is None or c <= 0:
            continue
        dte = (e - d).days
        if not keep(k, dte, spot, band=o["band"], max_dte=o["max_dte"]):
            continue
        prices.append(c)
        S.append(spot)
        K.append(k)
        T.append(dte / 365.0)
        calls.append(r["right"] == "C")
        meta.append((e, r["right"], k))
    ivs = solve_ivs(prices, S, K, T, calls)
    rows = [{"expiry": e, "right": right, "strike": k, "iv": iv} for (e, right, k), iv in zip(meta, ivs)]
    return rows, {"source": "history", "on": d.isoformat(), "spot": spot, "spot_src": "close",
                  "as_of": None, "name": (u or {}).get("name"), "traded": tbl.num_rows, "root": oroot}


def surface(sdb, symbol, day="latest", *, side=None, band=None, max_dte=None, expiries=None, common=True,
            root=None) -> dict:
    """The surface for ``symbol`` on ``day`` (``latest`` or ``YYYY-MM-DD``): ``build``'s
    grid plus ``symbol``, ``name``, ``source``, ``as_of``, ``method``, ``has_greeks`` (the
    tile figures are there: the latest pass yes, a past day no). Raises
    ``SurfaceError`` with the reason when it cannot be drawn."""
    sym = _sym(symbol)
    if not sym:
        raise SurfaceError("type an underlying symbol")
    o = norm_options(side, band, max_dte, expiries)
    if str(day or "latest").strip().lower() == "latest":
        rows, meta = latest_rows(sdb, sym)
        method = "Massive's implied volatility per contract, from the last market pass (15 min delayed)"
    else:
        rows, meta = history_rows(sdb, sym, day, band=o["band"], max_dte=o["max_dte"], root=root)
        method = ("Black-Scholes implied volatility from each contract's closing price and the stock's close "
                  "(r = %.1f %%, no dividend, T = days / 365)" % (RISK_FREE * 100.0))
    grid = build(rows, meta.get("spot"), meta.get("on"), common=common, **o)
    if not grid["n_cells"]:
        raise SurfaceError("no %s option inside this window has an implied volatility - widen the strike "
                           "range or the expiries" % sym)
    as_of = meta.get("as_of")
    return {**grid, "symbol": sym, "name": meta.get("name"), "source": meta["source"],
            "spot_src": meta.get("spot_src"),
            "as_of": as_of.replace(microsecond=0).isoformat() + "Z" if isinstance(as_of, _dt.datetime) else None,
            "traded": meta.get("traded"), "method": method, "has_greeks": meta["source"] == "latest",
            "liq_n": meta.get("liq_n"),
            "rate": RISK_FREE}           # v4.205: the rate behind every model price, for the hover card's working


__all__ = ["SIDES", "FIGURES", "BANDS", "DTES", "SurfaceError", "build", "norm_options", "keep", "is_monthly",
           "latest_rows", "history_rows", "surface", "raw_closes", "solve_ivs", "option_root_of", "underlying",
           "unknown_symbol_text", "liquidity_ranks"]
