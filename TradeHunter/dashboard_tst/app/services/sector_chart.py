"""Sector Rotation -> the "Sector Chart" tab (v4.250).

The user (2026-11-09): *"I want a chart on top that will show all the ETF chart with ticker box
when tick the historical data of the etf will be shown in the chart. Then below the chart is a
table that show all the ETF performance with option to compare it as follow 1d..10d, 1w..10w,
1m..6m with a small chart on the table also. With the ETF selected on the 3rd table below it
will list the tickers ... with sector and industry ... and the performance."*

Three products, one live source for prices (Yahoo daily closes through ``services.prices``,
the chart's own path and cache; never parquet - a live view):

* ``etf_table()`` - every ETF (the 11 SPDR sectors + SPY/QQQ): its daily closes for the top
  chart, its return over each of the 26 periods and the closes tail the row's sparkline is
  drawn from.
* ``holdings_table(etf)`` - what the picked fund holds (``services.etf_holdings``, the
  issuer's own book with weights), each holding with its Finviz sector + industry and its
  returns over the same 26 periods from its own daily closes. Returns are computed for the
  heaviest ``RETURN_CAP`` holdings (SPY holds ~500; the tail would cost a minute cold).
* The sector/industry map is built once from the 11 Finviz sector screens
  (``services.industry``), in a background thread the first time, so a cold request never
  waits on ~30 s of scraping - the rows come back with the map's state and the page re-asks.

A "period" is counted in TRADING DAYS: 1d-10d = 1..10 sessions, 1w-10w = 5 sessions each,
1m-6m = 21 sessions each. The return is close[-1] / close[-1-n] - 1.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .etf import ETF_FULL_NAMES, ETF_UNIVERSE, INDEX_ETFS

# ── the periods ─────────────────────────────────────────────────────────────
PERIODS: list[tuple[str, int, str]] = (
    [(f"{n}d", n, "days") for n in range(1, 11)]
    + [(f"{n}w", 5 * n, "weeks") for n in range(1, 11)]
    + [(f"{n}m", 21 * n, "months") for n in range(1, 7)]
)
PERIOD_DAYS = {k: n for k, n, _ in PERIODS}
DEFAULT_PERIOD = "1m"
DEFAULT_GROUP = "months"     # the columns both tables open on: days | weeks | months (v4.251)
MAX_DAYS = max(PERIOD_DAYS.values())          # 126 - the sparkline tail is MAX_DAYS + 1 closes

# one colour per ETF, shared by the tick boxes, the chart lines and the table rows
ETF_COLORS = {
    "XLK": "#38bdf8", "XLF": "#34d399", "XLE": "#f59e0b", "XLV": "#fb7185", "XLI": "#a78bfa",
    "XLY": "#f472b6", "XLP": "#4ade80", "XLU": "#facc15", "XLB": "#fb923c", "XLRE": "#2dd4bf",
    "XLC": "#818cf8", "SPY": "#e2e8f0", "QQQ": "#94a3b8",
}
ETFS: list[tuple[str, str]] = list(ETF_UNIVERSE) + list(INDEX_ETFS)
ETF_SYMBOLS = [s for s, _ in ETFS]

_RNG = "5y"            # the same Yahoo range services.etf uses, so the two share one cache entry
_HOLD_RNG = "2y"       # the charts' default range: a holding's bars are then a cache hit for its chart
RETURN_CAP = 120       # holdings priced per fund (the heaviest), see the module docstring
_WORKERS = 12


def _closes(bars: list[dict] | None) -> tuple[list[str], list[float]]:
    dates, closes = [], []
    for b in bars or []:
        c = b.get("close")
        t = b.get("time")
        if c is None or not t:
            continue
        dates.append(t)
        closes.append(float(c))
    return dates, closes


def ret(closes: list[float], n: int) -> float | None:
    """close[-1] / close[-1-n] - 1, as a PERCENT; None when the series is too short."""
    if n <= 0 or len(closes) <= n or not closes[-1 - n]:
        return None
    return (closes[-1] / closes[-1 - n] - 1.0) * 100.0


def returns(closes: list[float]) -> dict[str, float | None]:
    return {k: ret(closes, n) for k, n, _ in PERIODS}


def _fetch_many(symbols: list[str], rng: str) -> dict[str, tuple[list[str], list[float]]]:
    from .prices import fetch_daily_ohlc

    out: dict[str, tuple[list[str], list[float]]] = {}
    if not symbols:
        return out
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(symbols))) as ex:
        for sym, bars in zip(symbols, ex.map(lambda s: _safe_bars(fetch_daily_ohlc, s, rng), symbols)):
            out[sym] = _closes(bars)
    return out


def _safe_bars(fn, sym: str, rng: str):
    try:
        return fn(sym, rng=rng)
    except Exception:  # noqa: BLE001
        return []


# ── the ETF table + chart series ───────────────────────────────────────────
_ETF_TTL = 600.0
_etf_cache: dict = {}


def etf_table() -> dict:
    """{periods, default, etfs:[{symbol, name, full_name, color, last, as_of, ret:{period: %},
    tail:[closes]}], series:{symbol:[[date, close], ...]}} - the chart's history (2 years of
    daily closes per ETF) and the performance table's rows. Cached 10 min."""
    hit = _etf_cache.get("table")
    if hit and hit[0] > time.time():
        return hit[1]
    data = _fetch_many(ETF_SYMBOLS, _RNG)
    etfs, series = [], {}
    for sym, name in ETFS:
        dates, closes = data.get(sym) or ([], [])
        keep = 2 * 252 + 5
        series[sym] = [[d, round(c, 4)] for d, c in zip(dates[-keep:], closes[-keep:])]
        etfs.append({
            "symbol": sym, "name": name, "full_name": ETF_FULL_NAMES.get(sym, name),
            "color": ETF_COLORS.get(sym, "#94a3b8"),
            "last": round(closes[-1], 2) if closes else None, "as_of": dates[-1] if dates else None,
            "ret": {k: (round(v, 2) if v is not None else None) for k, v in returns(closes).items()},
            "tail": [round(c, 4) for c in closes[-(MAX_DAYS + 1):]],
        })
    out = {
        "periods": [{"key": k, "days": n, "group": g} for k, n, g in PERIODS],
        "default": DEFAULT_PERIOD, "etfs": etfs, "series": series,
    }
    if any(e["last"] is not None for e in etfs):
        _etf_cache["table"] = (time.time() + _ETF_TTL, out)
    return out


# ── ticker -> (sector, industry), from the 11 Finviz sector screens ────────
_IND_LOCK = threading.Lock()
_IND: dict = {"state": "cold", "map": {}, "built": 0.0, "error": None}
_IND_TTL = 6 * 3600.0


def _build_industry_map() -> None:
    from .industry import _SECTOR_NAMES, sector_industries

    m: dict[str, dict] = {}
    err = None
    try:
        for sym, _ in ETF_UNIVERSE:
            data = sector_industries(sym)
            for ind in data.get("industries") or []:
                for t in ind.get("tickers") or []:
                    m.setdefault(t["symbol"], {"sector": _SECTOR_NAMES.get(sym, sym), "industry": ind["name"],
                                               "company": t.get("company") or ""})
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:160]
    with _IND_LOCK:
        _IND.update({"state": "ready" if m else "failed", "map": m or _IND["map"],
                     "built": time.time(), "error": err})


def industry_map(*, wait: bool = False) -> tuple[dict[str, dict], str]:
    """(ticker -> {sector, industry, company}, state). ``state`` is ready | loading | failed.
    A cold map is built in a background thread (``wait=True`` builds it in this thread -
    tests and one-off scripts)."""
    with _IND_LOCK:
        fresh = _IND["state"] == "ready" and time.time() - _IND["built"] < _IND_TTL
        if fresh:
            return _IND["map"], "ready"
        if _IND["state"] == "failed" and time.time() - _IND["built"] < 300:
            return _IND["map"], "failed"
        if _IND["state"] == "loading":
            return _IND["map"], "loading"
        _IND["state"] = "loading"
    if wait:
        _build_industry_map()
        with _IND_LOCK:
            return _IND["map"], _IND["state"]
    threading.Thread(target=_build_industry_map, daemon=True, name="sector-industry-map").start()
    with _IND_LOCK:
        return _IND["map"], "loading"


PREWARM_DELAY_S = 60.0


def start_prewarm(delay: float = PREWARM_DELAY_S) -> bool:
    """Build the sector / industry map in the background ``delay`` seconds after the web app
    starts (main.py), so the first Holdings table has its columns. Off with
    ``TST_SECTOR_PREWARM=0``; the delay keeps test runs and quick restarts from scraping."""
    import os

    if os.environ.get("TST_SECTOR_PREWARM", "1").strip().lower() in ("0", "false", "off", "no"):
        return False
    t = threading.Timer(delay, lambda: industry_map())
    t.daemon = True
    t.name = "sector-industry-prewarm"
    t.start()
    return True


# ── the holdings table ─────────────────────────────────────────────────────
def holdings_table(etf: str, *, cap: int = RETURN_CAP) -> dict:
    """{etf, name, full_name, as_of, source, stale, error, industry_state, capped, rows:[{symbol,
    name, weight, sector, industry, last, as_of, ret:{period: %}}]}. Rows in weight order."""
    from . import etf_holdings as eh

    sym = (etf or "").strip().upper()
    hold = eh.holdings(sym)
    ind_map, ind_state = industry_map()
    rows = sorted(hold.get("rows") or [], key=lambda r: -(r.get("weight") or 0.0))
    priced = rows[:cap]
    bars = _fetch_many([eh.chart_symbol(r["symbol"]) for r in priced], _HOLD_RNG)
    out_rows = []
    for i, r in enumerate(rows):
        info = ind_map.get(r["symbol"]) or {}
        dates, closes = (bars.get(eh.chart_symbol(r["symbol"])) or ([], [])) if i < len(priced) else ([], [])
        out_rows.append({
            "symbol": r["symbol"], "chart_symbol": eh.chart_symbol(r["symbol"]),
            "name": info.get("company") or (r.get("name") or "").title(),
            "weight": r.get("weight"),
            "sector": info.get("sector"), "industry": info.get("industry"),
            "last": round(closes[-1], 2) if closes else None,
            "as_of": dates[-1] if dates else None,
            "ret": {k: (round(v, 2) if v is not None else None) for k, v in returns(closes).items()}
                   if closes else None,
        })
    names = dict(ETFS)
    return {
        "etf": sym, "name": names.get(sym, sym), "full_name": ETF_FULL_NAMES.get(sym, sym),
        "as_of": hold.get("as_of"), "source": hold.get("source"), "stale": hold.get("stale"),
        "error": hold.get("error"), "industry_state": ind_state,
        "capped": cap if len(rows) > cap else None, "n": len(rows), "rows": out_rows,
    }
