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

import json as _json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path as _Path

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
            # 5y, not the 2y default: the RRG normalises RS against a multi-year SMA
            # and then needs a usable run of weekly points AFTER that window is
            # consumed. 2y left barely a year of plottable history and forced the
            # window shorter than it should be. Leaders/correlation only read the
            # tail of this, so the extra history costs one bigger fetch, cached.
            for s, bars in zip(syms, ex.map(lambda q: fetch_daily_ohlc(q, rng="5y"), syms)):
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


_SEED_PATH = _Path(__file__).with_name("rrg_seed.json")
_seed_cache: dict = {}


def _seed_rrg_points(timeframe: str) -> dict:
    """The chart's own values as SHIPPED IN THE REPO (rrg_seed.json).

    A deploy is a git pull, so the values have to travel with the code: making the
    panel wait for a hand-run POST meant a pulled-but-unseeded Hermes silently fell
    back to the estimate and showed Energy in Weakening again. Read-only and
    cached; the same 50-150 sanity check the ingest route applies, so a mis-typed
    seed is ignored rather than believed.
    """
    hit = _seed_cache.get("v")
    if hit is None:
        try:
            hit = _json.loads(_SEED_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            hit = {}
        _seed_cache["v"] = hit
    if (hit.get("timeframe") or "weekly") != timeframe:
        return {}
    out = {}
    for sym, xy in (hit.get("points") or {}).items():
        try:
            x, y = float(xy[0]), float(xy[1])
        except Exception:  # noqa: BLE001
            continue
        if 50.0 <= x <= 150.0 and 50.0 <= y <= 150.0:
            out[sym.upper()] = {"rs_ratio": x, "rs_momentum": y,
                                "as_of": hit.get("as_of") or "",
                                "source": hit.get("source") or "optuma"}
    return out


def panel_symbol_order(timeframe: str = "weekly") -> list[str]:
    """Sector symbols in the order the LEFT PANEL lists them.

    That order is rotation-meaningful -- quadrant groups (Leading, Weakening,
    Improving, Lagging) and RS-Ratio within each -- so the Sector ETFs tab reads
    strongest-first like the panel beside it instead of in the arbitrary order
    ETF_UNIVERSE happens to be declared in.

    Soft-fails to the declared order, and always ends up containing every sector:
    a symbol the panel dropped (a failed price fetch) is appended rather than
    disappearing from the tab.
    """
    try:
        d = sector_returns(timeframe=timeframe)
        out = [r["symbol"] for g in (d.get("groups") or []) for r in (g.get("rows") or [])]
    except Exception:  # noqa: BLE001
        out = []
    seen = set(out)
    out += [s for s, _ in ETF_UNIVERSE if s not in seen]
    return out


def real_rrg_points(timeframe: str) -> dict:
    """{symbol: {rs_ratio, rs_momentum, as_of, source}} — the chart's real numbers.

    Two sources, newest wins: the seed that ships in the repo, and rows posted to
    /api/rrg. Posting a fresher reading therefore overrides the seed without a
    deploy, and a deploy carrying a newer seed overrides a stale posted row.

    Soft-fails to {} so the panel keeps working on its own estimate when both are
    unavailable -- this is an ENHANCEMENT to the panel, never a dependency of it.
    """
    out = _seed_rrg_points(timeframe)
    try:
        from ..db import SessionLocal
        from ..models import RRGPoint
    except Exception:  # noqa: BLE001
        return out
    db = None
    try:
        db = SessionLocal()
        rows = db.query(RRGPoint).filter(RRGPoint.timeframe == timeframe).all()
        for r in rows:
            cur = out.get(r.symbol)
            if cur is None or str(r.as_of or "") >= str(cur["as_of"] or ""):
                out[r.symbol] = {"rs_ratio": r.rs_ratio, "rs_momentum": r.rs_momentum,
                                 "as_of": r.as_of, "source": r.source}
    except Exception:  # noqa: BLE001
        pass
    finally:
        if db is not None:
            db.close()
    return out


def sector_returns(timeframe: str = "daily") -> dict:
    """Per-sector total return over 1 / 2 / 4 / 8 months, for the Sector & Industry
    left panel. Ordered leaders-first by relative strength vs SPY (shared
    leader_order(), so this panel agrees with the RRG list); includes an SPY row.
    Reuses the shared ~15-min-cached aligned closes. Soft-fail."""
    timeframe = timeframe if timeframe in TIMEFRAMES else "daily"
    ck = f"sector_returns_{timeframe}"
    hit = _cache.get(ck)
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

    # Attach each sector's RRG quadrant so the panel reads the way the chart does.
    # The RRG chart itself is Optuma's embed (cross-origin — we can't read what it
    # shows), so these come from our own rrg(); a sector sitting right on a 100 line
    # can therefore disagree with the embed by one quadrant. Soft-fail: no RRG data
    # just means no badges, never a broken panel.
    try:
        r = rrg(timeframe=timeframe)
        pts, quad = r["points"], r["quad"]
    except Exception:  # noqa: BLE001
        pts, quad = {}, {}

    # REAL RRG points, when we have them, beat our own approximation. JdK
    # RS-Ratio/RS-Momentum is proprietary, so the estimate cannot be trusted near a
    # 100 line -- on 2026-09-04 it put Energy in Weakening (101.89/99.27) when the
    # chart had it Improving (98.02/101.34). A stored point replaces the estimate
    # outright rather than being blended with it; averaging a real number with a
    # guess produces a third number that is neither.
    real = real_rrg_points(timeframe)
    for row in rows:
        tail = (pts.get(row["symbol"]) or {}).get("tail") or []
        row["quadrant"] = quad.get(row["symbol"])
        row["rs_ratio"] = tail[-1]["x"] if tail else None
        row["rs_mom"] = tail[-1]["y"] if tail else None
        row["rrg_source"] = "estimate"
        row["rrg_as_of"] = None

        hit = real.get(row["symbol"])
        if hit:
            row["rs_ratio"] = hit["rs_ratio"]
            row["rs_mom"] = hit["rs_momentum"]
            row["quadrant"] = _quadrant(hit["rs_ratio"], hit["rs_momentum"])
            row["rrg_source"] = hit["source"]
            row["rrg_as_of"] = hit["as_of"]
        # Flag sectors near a quadrant line. The RRG's normalisation is proprietary
        # (RRG Research / Optuma) and this is an approximation of it, so a sector
        # close to a boundary can legitimately show one quadrant here and the
        # neighbouring one on the chart. Measured 2026-09-07: every disagreement
        # with the real chart was inside 0.75 of the RS-Momentum line, so surfacing
        # the band explains the mismatches instead of leaving them looking wrong.
        row["borderline"] = row["rrg_source"] == "estimate" and (
            row["rs_ratio"] is not None and row["rs_mom"] is not None
            and (abs(row["rs_ratio"] - 100) < BORDERLINE
                 or abs(row["rs_mom"] - 100) < BORDERLINE)
        )

    # Grouped leaders-first in rotation-strength order, each group sorted by RS-Ratio.
    # Anything without RRG data falls into a trailing untagged group.
    groups = []
    for key in QUADRANTS:
        members = [r2 for r2 in rows if r2["quadrant"] == key]
        members.sort(key=lambda r2: (r2["rs_ratio"] is None, -(r2["rs_ratio"] or 0)))
        if members:
            groups.append({"key": key, "rows": members})
    rest = [r2 for r2 in rows if r2["quadrant"] not in QUADRANTS]
    if rest:
        groups.append({"key": None, "rows": rest})

    out = {
        "windows": [lbl for lbl, _ in _RET_WINDOWS],
        "rows": rows,
        "groups": groups,
        "timeframe": timeframe,
        "spy": _row(BENCHMARK, "S&P 500"),
    }
    _cache[ck] = (time.time() + _TTL, out)
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
#   RS          = 100 * price / benchmark
#   RS-Ratio    = EMA( 100 * RS / EMA(RS, RRG_RATIO_WIN), RRG_SMOOTH )
#   RS-Momentum = 100 * RS-Ratio / RS-Ratio[-RRG_MOM_LAG]
#
# Both axes are PERCENTAGE DEVIATIONS, not z-scores: "RS-Ratio 105" means relative
# strength is 5% above its own baseline. A rolling z-score (the original version
# here) is bounded near +/-2.5 by construction, so every sector sat in a 97.5-102.5
# blob; the commercial charts run 88-120 because a percentage deviation is unbounded.
#
# EMA, and smoothing the RATIO rather than the RS line, both matter. Sector RS is
# genuinely noisy - XLK moves +/-3% against SPY in a week - and with SMAs the noise
# could only be removed by lengthening the average, which lagged the heads across
# the 100 lines: head accuracy and tail shape traded against each other and neither
# could be had. EMA gives more noise reduction per unit of lag and post-smoothing
# the ratio keeps the head where it belongs. Measured against a reference Optuma
# weekly sector RRG for the same date, this construction cut mean head error from
# 5.5 points to 1.5 and roughly a third of the excess tail travel, while keeping all
# five unambiguously-labelled sectors in the SAME QUADRANT as the reference.
#
# Why calibrate at all: the JdK formula is licensed from RRG Research, and both
# StockCharts' ChartSchool and RRG Research's own "building blocks" page document
# the semantics while deliberately withholding the maths. What they do state, this
# matches - values normalise around 100, and RS-Momentum is the rate of change OF
# RS-Ratio - and the shape agrees with the open-source implementations (the
# TradingView open script is EMA(RS/EMA(RS,n),m)*100, the same construction).
#
# Exact agreement is not reachable and is not the goal: the reference plots the
# UCITS share classes (SXLV, SXLK...) against a UCITS benchmark, a different price
# series from the US-listed SPDRs fetched here.
RRG_RATIO_WIN = 20   # weeks - EMA baseline that RS is measured against
RRG_SMOOTH = 14      # weeks - EMA applied to the ratio itself
RRG_MOM_LAG = 13     # weeks - rate-of-change window for momentum (a quarter)
# An EMA has no hard start, so early values are dominated by the seed. Drop a
# warm-up of 3x the baseline before anything is plotted, and require enough history
# to have that plus a usable run of points afterwards.
RRG_WARMUP = 3 * RRG_RATIO_WIN
RRG_MIN_WEEKS = RRG_WARMUP + RRG_MOM_LAG + 12

# DAILY parameters. A timeframe is not a cosmetic switch on an RRG — it changes which
# quadrant a sector is in, because the same rotation looks different measured over
# days versus weeks. Technology reads Weakening on the weekly and Leading on the
# daily right now, and that is not a contradiction, it is the point of the timeframe.
# The embedded Optuma chart defaults to 1 Day, so a weekly-only panel next to it
# disagreed for a reason that had nothing to do with the maths.
#
# These are calibrated the same way the weekly ones were — against what the vendor's
# chart actually shows — and carry the same caveat: their daily settings are
# proprietary, so treat agreement as close, not exact.
RRG_DAILY = {"win": 10, "smooth": 5, "lag": 13}
RRG_WEEKLY = {"win": RRG_RATIO_WIN, "smooth": RRG_SMOOTH, "lag": RRG_MOM_LAG}
TIMEFRAMES = ("daily", "weekly")


def _rrg_params(timeframe: str) -> dict:
    return RRG_DAILY if timeframe == "daily" else RRG_WEEKLY


def _rrg_series_for(sym: str, closes: dict, dates: list, timeframe: str):
    """(RS-Ratio, RS-Momentum) for one symbol on the requested timeframe."""
    p = _rrg_params(timeframe)
    if timeframe == "daily":
        ser, bench = closes.get(sym, []), closes.get(BENCHMARK, [])
    else:
        ser = _weekly(dates, closes.get(sym, []))
        bench = _weekly(dates, closes.get(BENCHMARK, []))
    return _jdk(ser, bench, win=p["win"], smooth=p["smooth"], lag=p["lag"])


def _ema(series: list[float], n: int) -> list[float]:
    """Exponential moving average, same length as the input (seeded on the first value)."""
    if n <= 1:
        return list(series)
    k = 2.0 / (n + 1)
    out = [series[0]]
    for v in series[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _jdk(wser: list[float], wbench: list[float],
         win: int = RRG_RATIO_WIN, smooth: int = RRG_SMOOTH,
         lag: int = RRG_MOM_LAG) -> tuple[list[float], list[float]]:
    """(RS-Ratio, RS-Momentum) for one symbol against the benchmark, weekly.

    Both lists are RIGHT-aligned (they end on the same, most recent week) but are
    not the same length - callers take the last `min(len(a), len(b))` of each,
    which keeps them aligned.
    """
    warmup = 3 * win
    n = min(len(wser), len(wbench))
    if n < warmup + lag + 12:
        return [], []
    a, b = wser[-n:], wbench[-n:]
    rs = [100.0 * a[i] / b[i] if b[i] else 100.0 for i in range(n)]
    base = _ema(rs, win)
    ratio = _ema([100.0 * rs[i] / base[i] if base[i] else 100.0 for i in range(n)], smooth)
    ratio = ratio[warmup:]
    if len(ratio) <= lag:
        return ratio, []
    mom = [100.0 * ratio[i] / ratio[i - lag] if ratio[i - lag] else 100.0
           for i in range(lag, len(ratio))]
    return ratio, mom


# Display order for the four RRG quadrants: strongest rotation state first. This is
# reading order for the sector panel, not the clockwise rotation cycle — a reader wants
# "who is strong now" at the top, not where the cycle happens to start.
# RRG ROTATION order, the way the chart is read: a sector rotates clockwise
# Leading -> Weakening -> Lagging -> Improving -> Leading. Listing the groups
# in that sequence means the panel walks the cycle in the same direction as the
# RRG itself, so a sector's position in the list says where it is in the
# rotation. (Was Leading/Improving/Weakening/Lagging -- strength order, which
# put the two ends of the cycle next to each other.)
# How close to a 100 line counts as "could go either way" in the panel.
BORDERLINE = 1.0

QUADRANTS = ["Leading", "Weakening", "Improving", "Lagging"]


def _quadrant(x: float, y: float) -> str:
    if x >= 100 and y >= 100:
        return "Leading"
    if x >= 100:
        return "Weakening"
    if y >= 100:
        return "Improving"
    return "Lagging"


def rrg(tail: int = 8, timeframe: str = "weekly") -> dict:
    """JdK-style Relative Rotation: per ETF a tail of (RS-Ratio, RS-Momentum) points
    vs SPY on weekly data, plus its current quadrant. Both axes centre on 100."""
    timeframe = timeframe if timeframe in TIMEFRAMES else "weekly"
    ck = f"rrg_{timeframe}"
    hit = _cache.get(ck)
    if hit and hit[0] > time.time():
        return hit[1]
    a = _aligned()
    closes, dates = a["closes"], a["dates"]
    points: dict = {}
    quad: dict = {}
    for sym, name in ETF_UNIVERSE:
        ratio, mom = _rrg_series_for(sym, closes, dates, timeframe)
        m = min(len(ratio), len(mom))
        if m < 1:
            continue
        rr, mm = ratio[-m:], mom[-m:]
        pts = [{"x": round(rr[i], 2), "y": round(mm[i], 2)}
               for i in range(max(0, m - tail), m)]
        if pts:
            points[sym] = {"name": name, "tail": pts}
            quad[sym] = _quadrant(pts[-1]["x"], pts[-1]["y"])
    out = {"points": points, "quad": quad, "timeframe": timeframe}
    _cache[ck] = (time.time() + _TTL, out)
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
