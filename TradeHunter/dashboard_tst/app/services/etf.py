"""ETF rotation analytics for the Today Overview (Phases 2-3).

US sector SPDRs benchmarked vs SPY. All data is LIVE from Yahoo daily bars
(prices.fetch_daily_ohlc) — a "now" view, never parquet (CLAUDE.md). Everything is
computed in PURE PYTHON (no numpy/pandas) so there's no extra Hermes dependency;
results are cached ~15 min and soft-fail. One aligned-close fetch feeds all three:
  - etf_leaders()        relative strength (1w/1m/3m vs SPY), ranked
  - correlation_matrix() 60-day daily-return correlation heatmap
  - rrg()                JdK-style RS-Ratio / RS-Momentum quadrant tails
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from .prices import fetch_daily_ohlc

# (symbol, short label) — the 11 SPDR sectors.
ETF_UNIVERSE = [
    ("XLK", "Technology"), ("XLF", "Financials"), ("XLE", "Energy"),
    ("XLV", "Health Care"), ("XLI", "Industrials"), ("XLY", "Cons Disc"),
    ("XLP", "Cons Staples"), ("XLU", "Utilities"), ("XLB", "Materials"),
    ("XLRE", "Real Estate"), ("XLC", "Comm Svcs"),
]
BENCHMARK = "SPY"
_TTL = 900.0  # 15 min
_cache: dict = {}


def _aligned() -> dict:
    """{'dates':[...], 'closes':{sym:[...]}} aligned on the dates common to SPY and
    every ETF. One ~15-min-cached fetch (parallel) shared by all three products."""
    hit = _cache.get("aligned")
    if hit and hit[0] > time.time():
        return hit[1]
    syms = [BENCHMARK] + [s for s, _ in ETF_UNIVERSE]
    per: dict = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for s, bars in zip(syms, ex.map(fetch_daily_ohlc, syms)):
                per[s] = {b["time"]: b["close"] for b in (bars or []) if b.get("close")}
    except Exception:  # noqa: BLE001
        per = {s: {} for s in syms}
    common: set | None = None
    for s in syms:
        ks = set(per.get(s, {}).keys())
        common = ks if common is None else (common & ks)
    dates = sorted(common or [])
    closes = {s: [per[s][d] for d in dates] for s in syms}
    out = {"dates": dates, "closes": closes}
    _cache["aligned"] = (time.time() + _TTL, out)
    return out


def _ret(series: list[float], lookback: int):
    if len(series) <= lookback or not series[-1 - lookback]:
        return None
    return series[-1] / series[-1 - lookback] - 1.0


def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / ((va * vb) ** 0.5)


# -------------------------------------------------------------- ETF leaders
def etf_leaders() -> dict:
    """Sector relative strength: 1w/1m/3m returns + outperformance vs SPY, ranked
    by 1-month relative strength (leaders first)."""
    hit = _cache.get("leaders")
    if hit and hit[0] > time.time():
        return hit[1]
    closes = _aligned()["closes"]
    spy = closes.get(BENCHMARK, [])
    out = {"rows": [], "spy": None}
    if len(spy) > 64:
        s1w, s1m, s3m = _ret(spy, 5), _ret(spy, 21), _ret(spy, 63)
        out["spy"] = {"r1w": s1w, "r1m": s1m, "r3m": s3m}
        rows = []
        for sym, name in ETF_UNIVERSE:
            c = closes.get(sym, [])
            r1w, r1m, r3m = _ret(c, 5), _ret(c, 21), _ret(c, 63)
            rs1m = (r1m - s1m) if (r1m is not None and s1m is not None) else None
            rows.append({
                "symbol": sym, "name": name, "r1w": r1w, "r1m": r1m, "r3m": r3m,
                "rs1m": rs1m, "leading": bool(rs1m is not None and rs1m > 0),
            })
        rows.sort(key=lambda x: (x["rs1m"] if x["rs1m"] is not None else -99), reverse=True)
        out["rows"] = rows
    _cache["leaders"] = (time.time() + _TTL, out)
    return out


# ----------------------------------------------------- sector returns (1/2/4/8mo)
# Approx trading-day lookbacks: 1mo~21, 2mo~42, 4mo~84, 8mo~168.
_RET_WINDOWS = [("1M", 21), ("2M", 42), ("4M", 84), ("8M", 168)]


def sector_returns() -> dict:
    """Per-sector total return over 1 / 2 / 4 / 8 months, for the Sector & Industry
    left panel. Sorted by 1-month return (leaders first); includes an SPY row.
    Reuses the shared ~15-min-cached aligned closes. Soft-fail."""
    hit = _cache.get("sector_returns")
    if hit and hit[0] > time.time():
        return hit[1]
    closes = _aligned()["closes"]

    def _row(sym, name):
        c = closes.get(sym, [])
        return {"symbol": sym, "name": name,
                "rets": {lbl: _ret(c, lb) for lbl, lb in _RET_WINDOWS}}

    rows = [_row(sym, name) for sym, name in ETF_UNIVERSE]
    rows.sort(key=lambda r: (r["rets"].get("1M") if r["rets"].get("1M") is not None else -99),
              reverse=True)
    out = {
        "windows": [lbl for lbl, _ in _RET_WINDOWS],
        "rows": rows,
        "spy": _row(BENCHMARK, "S&P 500"),
    }
    _cache["sector_returns"] = (time.time() + _TTL, out)
    return out


# -------------------------------------------------------------- correlation
def correlation_matrix(window: int = 60) -> dict:
    """60-day daily-return Pearson correlation across SPY + every sector ETF."""
    hit = _cache.get("corr")
    if hit and hit[0] > time.time():
        return hit[1]
    closes = _aligned()["closes"]
    labels = [BENCHMARK] + [s for s, _ in ETF_UNIVERSE]
    rets: dict = {}
    ok = True
    for s in labels:
        c = closes.get(s, [])
        if len(c) < window + 1:
            ok = False
            break
        seg = c[-(window + 1):]
        rets[s] = [seg[i] / seg[i - 1] - 1.0 for i in range(1, len(seg)) if seg[i - 1]]
    matrix = []
    if ok:
        for ra in labels:
            matrix.append([round(_pearson(rets[ra], rets[rb]), 2) for rb in labels])
    out = {"labels": labels, "matrix": matrix, "window": window}
    _cache["corr"] = (time.time() + _TTL, out)
    return out


# --------------------------------------------------------------------- RRG
def _weekly(dates: list[str], series: list[float]) -> list[float]:
    """Resample a daily series to weekly (last close of each ISO week)."""
    wk: dict = {}
    order: list = []
    for d, v in zip(dates, series):
        y, w, _ = date.fromisoformat(d).isocalendar()
        key = (y, w)
        if key not in wk:
            order.append(key)
        wk[key] = v
    return [wk[k] for k in order]


def _rolling_z100(series: list[float], win: int) -> list[float]:
    """Rolling z-score recentred at 100 (the RRG convention)."""
    out: list[float] = []
    for i in range(win - 1, len(series)):
        w = series[i - win + 1:i + 1]
        m = sum(w) / win
        sd = (sum((x - m) ** 2 for x in w) / win) ** 0.5
        out.append(100.0 + (0.0 if sd == 0 else (series[i] - m) / sd))
    return out


def _quadrant(x: float, y: float) -> str:
    if x >= 100 and y >= 100:
        return "Leading"
    if x >= 100:
        return "Weakening"
    if y >= 100:
        return "Improving"
    return "Lagging"


def rrg(tail: int = 8, win: int = 12) -> dict:
    """JdK-style Relative Rotation: per ETF a tail of (RS-Ratio, RS-Momentum) points
    vs SPY on weekly data, plus its current quadrant. Both axes centre on 100."""
    hit = _cache.get("rrg")
    if hit and hit[0] > time.time():
        return hit[1]
    a = _aligned()
    closes, dates = a["closes"], a["dates"]
    wbench = _weekly(dates, closes.get(BENCHMARK, []))
    points: dict = {}
    quad: dict = {}
    for sym, name in ETF_UNIVERSE:
        wser = _weekly(dates, closes.get(sym, []))
        n = min(len(wser), len(wbench))
        if n < win + tail + 2:
            continue
        wser, wb = wser[-n:], wbench[-n:]
        rs = [100.0 * wser[i] / wb[i] for i in range(n) if wb[i]]
        ratio = _rolling_z100(rs, win)
        roc = [ratio[i] - ratio[i - 1] for i in range(1, len(ratio))]
        mom = _rolling_z100(roc, win)
        m = min(len(ratio), len(mom))
        if m < 1:
            continue
        rr, mm = ratio[-m:], mom[-m:]
        pts = [{"x": round(rr[i], 2), "y": round(mm[i], 2)}
               for i in range(max(0, m - tail), m)]
        if pts:
            points[sym] = {"name": name, "tail": pts}
            quad[sym] = _quadrant(pts[-1]["x"], pts[-1]["y"])
    out = {"points": points, "quad": quad}
    _cache["rrg"] = (time.time() + _TTL, out)
    return out
