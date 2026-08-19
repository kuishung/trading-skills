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

# (symbol, full sector name) — the 11 SPDR sectors.
ETF_UNIVERSE = [
    ("XLK", "Technology"), ("XLF", "Financials"), ("XLE", "Energy"),
    ("XLV", "Health Care"), ("XLI", "Industrials"), ("XLY", "Consumer Discretionary"),
    ("XLP", "Consumer Staples"), ("XLU", "Utilities"), ("XLB", "Materials"),
    ("XLRE", "Real Estate"), ("XLC", "Communication Services"),
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


def leader_order() -> list[str]:
    """Sector symbols ranked leaders-first by 1-month relative strength vs SPY.
    Single source of truth for 'leader' ordering across the whole Sector page —
    the RRG legend/list, the Sector & Industry panel, and the leaders table all
    key off this so they agree. Sectors that couldn't be ranked (insufficient
    history) fall to the end in universe order. Cheap: reads the cached leaders."""
    order = [r["symbol"] for r in etf_leaders().get("rows", [])]
    for sym, _ in ETF_UNIVERSE:
        if sym not in order:
            order.append(sym)
    return order


# ----------------------------------------------------- sector returns (1/2/4/8mo)
# Approx trading-day lookbacks: 1mo~21, 2mo~42, 4mo~84, 8mo~168.
_RET_WINDOWS = [("1M", 21), ("2M", 42), ("4M", 84), ("8M", 168)]


def sector_returns() -> dict:
    """Per-sector total return over 1 / 2 / 4 / 8 months, for the Sector & Industry
    left panel. Ordered leaders-first by relative strength vs SPY (shared
    leader_order(), so this panel agrees with the RRG list); includes an SPY row.
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
    rank = {s: i for i, s in enumerate(leader_order())}
    rows.sort(key=lambda r: rank.get(r["symbol"], 99))
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


# JdK RS-Ratio / RS-Momentum parameters (weekly bars).
#
#   RS          = 100 * price / benchmark, then smoothed (SMA RRG_SMOOTH)
#   RS-Ratio    = 100 * RS / SMA(RS, RRG_RATIO_WIN)
#   RS-Momentum = 100 * RS-Ratio / RS-Ratio[-RRG_MOM_LAG]
#
# BOTH AXES ARE PERCENTAGE DEVIATIONS, NOT Z-SCORES — this is what makes an RRG
# readable and it was the original defect here. A rolling z-score is bounded at
# roughly +/-2.5 by construction, so every sector sat in a 97.5-102.5 blob no matter
# what the market did; the commercial charts run 88-120 because a percentage
# deviation is unbounded (a sector 12% above its own norm reads 112).
#
# The SMOOTH step is not cosmetic and cannot be dropped just because the ratio's
# denominator is a slow SMA: the NUMERATOR is raw weekly RS, which genuinely moves
# several percent a week (XLV moved +7% vs SPY in the week to 2026-06-26), so
# without it the tail reverses direction almost every week -- measured at 92 degrees
# average turn between segments, versus 28 with it.
#
# Windows were calibrated against a reference Optuma weekly sector RRG for the same
# date, scoring BOTH quadrant agreement and tail smoothness: this triple puts all
# five unambiguously-labelled sectors in the SAME QUADRANT as the reference and
# gives the closest spread to it. It sits inside a broad plateau (smooth 5-6, win
# 20-32, lag 10-13 all score 5/5), so it is not a knife-edge fit.
#
# Why calibrate rather than copy: the JdK formula is licensed from RRG Research and
# StockCharts' own ChartSchool page documents only the SEMANTICS, never the maths.
# What it does state, we match -- values normalise around 100, and RS-Momentum is
# the rate of change OF RS-Ratio. It also wants ~50 weekly bars before the indicator
# is valid, the same order as RRG_MIN_WEEKS. Exact coordinate agreement is not
# expected anyway: the reference plots the UCITS share classes (SXLV et al) against
# a UCITS benchmark, a different price series from the US-listed SPDRs we fetch.
RRG_SMOOTH = 6       # weeks - SMA on the RS line before it is normalised
RRG_RATIO_WIN = 26   # weeks - the SMA that RS is measured against (half a year)
RRG_MOM_LAG = 13     # weeks - rate-of-change window for momentum (a quarter)
RRG_MIN_WEEKS = RRG_SMOOTH + RRG_RATIO_WIN + RRG_MOM_LAG + 2


def _jdk(wser: list[float], wbench: list[float],
         win: int = RRG_RATIO_WIN, smooth: int = RRG_SMOOTH,
         lag: int = RRG_MOM_LAG) -> tuple[list[float], list[float]]:
    """(RS-Ratio, RS-Momentum) for one symbol against the benchmark, weekly.

    Both lists are RIGHT-aligned (they end on the same, most recent week) but are
    not the same length - callers take the last `min(len(a), len(b))` of each,
    which keeps them aligned.
    """
    n = min(len(wser), len(wbench))
    if n < 2:
        return [], []
    a, b = wser[-n:], wbench[-n:]
    rs = [100.0 * a[i] / b[i] if b[i] else 100.0 for i in range(n)]
    if smooth > 1:
        rs = [sum(rs[i - smooth + 1:i + 1]) / smooth for i in range(smooth - 1, len(rs))]
    if len(rs) < win + 1:
        return [], []
    ratio = [100.0 * rs[i] / m if m else 100.0
             for i, m in ((i, sum(rs[i - win + 1:i + 1]) / win) for i in range(win - 1, len(rs)))]
    if len(ratio) <= lag:
        return ratio, []
    mom = [100.0 * ratio[i] / ratio[i - lag] if ratio[i - lag] else 100.0
           for i in range(lag, len(ratio))]
    return ratio, mom


def _quadrant(x: float, y: float) -> str:
    if x >= 100 and y >= 100:
        return "Leading"
    if x >= 100:
        return "Weakening"
    if y >= 100:
        return "Improving"
    return "Lagging"


def rrg(tail: int = 8, win: int = RRG_RATIO_WIN) -> dict:
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
        if min(len(wser), len(wbench)) < RRG_MIN_WEEKS + tail:
            continue
        ratio, mom = _jdk(wser, wbench, win=win)
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


def _week_labels(dates: list[str]) -> list[str]:
    """Ordered last-daily-date per ISO week (matches _weekly's ordering)."""
    wk: dict = {}
    order: list = []
    for d in dates:
        y, w, _ = date.fromisoformat(d).isocalendar()
        key = (y, w)
        if key not in wk:
            order.append(key)
        wk[key] = d
    return [wk[k] for k in order]


def rrg_series(win: int = RRG_RATIO_WIN, weeks: int = 26) -> dict:
    """Full weekly (RS-Ratio, RS-Momentum) series per sector for the INTERACTIVE RRG
    (scrubbable tail). Every sector is aligned to the same week axis; the frontend
    picks the tail length + end-week. Returns the last `weeks` weekly points."""
    hit = _cache.get("rrg_series")
    if hit and hit[0] > time.time():
        return hit[1]
    a = _aligned()
    closes, dates = a["closes"], a["dates"]
    wbench = _weekly(dates, closes.get(BENCHMARK, []))
    wlabels = _week_labels(dates)

    tmp: list = []
    m_common: int | None = None
    for sym, name in ETF_UNIVERSE:
        wser = _weekly(dates, closes.get(sym, []))
        n = min(len(wser), len(wbench), len(wlabels))
        if n < RRG_MIN_WEEKS:
            continue
        wser, labs = wser[-n:], wlabels[-n:]
        ratio, mom = _jdk(wser, wbench[-n:], win=win)
        m = min(len(ratio), len(mom))
        if m < 2:
            continue
        rr, mm = ratio[-m:], mom[-m:]
        pts = [{"x": round(rr[i], 2), "y": round(mm[i], 2)} for i in range(m)]
        tmp.append({"symbol": sym, "name": name, "pts": pts, "labs": labs[-m:]})
        m_common = m if m_common is None else min(m_common, m)

    keep = min(m_common or 0, weeks)
    week_axis: list = []
    sectors: list = []
    for t in tmp:
        if not week_axis:
            week_axis = t["labs"][-keep:]
        sectors.append({"symbol": t["symbol"], "name": t["name"], "pts": t["pts"][-keep:]})
    # Order the RRG list leaders-first (shared leader_order()), so the legend/list
    # agrees with the Sector & Industry panel and the leaders table.
    rank = {s: i for i, s in enumerate(leader_order())}
    sectors.sort(key=lambda s: rank.get(s["symbol"], 99))
    # Benchmark weekly closes for the scrubber sparkline (same `keep` weeks as the
    # week axis, so the highlighted tail lines up with the slider). wbench shares the
    # weekly calendar with wlabels, so its last `keep` values align with week_axis.
    bench = [round(v, 2) for v in wbench[-keep:]] if keep else []
    out = {"weeks": week_axis, "sectors": sectors, "bench": bench, "bench_name": BENCHMARK}
    _cache["rrg_series"] = (time.time() + _TTL, out)
    return out
