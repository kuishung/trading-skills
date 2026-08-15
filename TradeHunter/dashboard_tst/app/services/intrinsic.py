"""Intrinsic Value — DCF models + multiple-based fair values + EV ratios, from SEC
XBRL fundamentals × live price (valuation.py). Everything here is computed with
DOCUMENTED assumptions (a 2-stage DCF: 10 high-growth years + 10 terminal years,
12% discount, 4% terminal growth, growth capped at 20%). These are TradeHunter's
own estimates — NOT GuruFocus's proprietary OracleValue™ or "without-NRI" figures,
which use an undisclosed methodology and are deliberately omitted.
"""
from __future__ import annotations

_DISCOUNT = 0.12
_TERMINAL_G = 0.04
_GROWTH_CAP = 0.20
_Y1 = 10   # high-growth years
_Y2 = 10   # terminal-growth years


def _dcf(base: float, growth: float, terminal: bool = False) -> float | None:
    """2-stage per-share DCF: `_Y1` years at `growth` (capped), then `_Y2` at the
    terminal rate, discounted at `_DISCOUNT`. If `terminal`, add a Gordon terminal
    value at the end. Returns None for a non-positive base."""
    if base is None or base <= 0:
        return None
    g1 = max(min(growth if growth is not None else 0.10, _GROWTH_CAP), 0.0)
    r = _DISCOUNT
    val, cf = 0.0, base
    for y in range(1, _Y1 + 1):
        cf *= (1 + g1)
        val += cf / (1 + r) ** y
    for y in range(_Y1 + 1, _Y1 + _Y2 + 1):
        cf *= (1 + _TERMINAL_G)
        val += cf / (1 + r) ** y
    if terminal and r > _TERMINAL_G:
        tv = cf * (1 + _TERMINAL_G) / (r - _TERMINAL_G)
        val += tv / (1 + r) ** (_Y1 + _Y2)
    return round(val, 2)


def intrinsic(symbol: str, annual: dict, ttm: dict, valn: dict) -> dict:
    years = annual.get("years", [])
    series = annual.get("series", {})
    price = valn.get("price")
    mc = valn.get("market_cap")
    shares = ttm.get("shares_out") or (series.get("shares_out", {}).get(years[-1]) if years else None)

    ni, fcf, rev, ocf = ttm.get("net_income"), ttm.get("fcf"), ttm.get("revenue"), ttm.get("ocf")
    debt, cash = ttm.get("total_debt"), ttm.get("cash_and_sti")
    eq = series.get("equity", {}).get(years[-1]) if years else None

    def ps(x):
        return (x / shares) if (x is not None and shares) else None

    eps, fcfps, sps = ps(ni), ps(fcf), ps(rev)
    bvps = ps(eq)
    ev = (mc + (debt or 0) - (cash or 0)) if mc is not None else None

    # growth (10y CAGR, fall back to 5y, then 10%)
    def cagr(metric, back):
        s = series.get(metric, {})
        if len(years) <= back:
            return None
        cur, old = s.get(years[-1]), s.get(years[-1 - back])
        if cur and old and old > 0:
            return (cur / old) ** (1 / back) - 1
        return None

    g_ni = cagr("net_income", 10) or cagr("net_income", 5) or 0.10
    g_fcf = cagr("fcf", 10) or cagr("fcf", 5) or g_ni

    # mean historical multiples -> fair value = mean multiple × current per-share
    def mean_ratio(k):
        vals = [valn.get("by_year", {}).get(fy, {}).get(k) for fy in years]
        vals = [v for v in vals if v is not None]
        return (sum(vals) / len(vals)) if vals else None

    mean_pe, mean_ps, mean_pb = mean_ratio("pe"), mean_ratio("ps"), mean_ratio("pb")
    pe_val = round(mean_pe * eps, 2) if (mean_pe and eps) else None
    ps_val = round(mean_ps * sps, 2) if (mean_ps and sps) else None
    pb_val = round(mean_pb * bvps, 2) if (mean_pb and bvps) else None

    # the valuation-bar estimates (label, value, kind: dcf / multiple)
    estimates = [
        ("Discounted Net Income 20-yr (DNI-20)", _dcf(eps, g_ni), "dcf"),
        ("Discounted Free Cash Flow 20-yr (DFCF-20)", _dcf(fcfps, g_fcf), "dcf"),
        ("Discounted FCF Terminal (DFCF-Terminal)", _dcf(fcfps, g_fcf, terminal=True), "dcf"),
        ("Mean Price-to-Earnings value", pe_val, "multiple"),
        ("Mean Price-to-Sales value", ps_val, "multiple"),
        ("Mean Price-to-Book value", pb_val, "multiple"),
    ]
    present = [v for (_, v, _) in estimates if v is not None]
    fair = round(sum(present) / len(present), 2) if present else None

    # margin of safety / verdict vs price
    def verdict(v):
        if v is None or not price:
            return None
        return round((v / price - 1) * 100, 1)  # upside %

    bars = [{"label": lbl, "value": v, "kind": k, "upside": verdict(v)}
            for (lbl, v, k) in estimates if v is not None]
    if fair is not None:
        bars.append({"label": "Fair Value (blended)", "value": fair, "kind": "fair", "upside": verdict(fair)})

    ebitda = ttm.get("operating_income")
    da = None  # EBITDA/share approximated from op income if D&A unavailable here
    other = [
        ("Enterprise Value", ev, "usd_m"),
        ("EV / Revenue", (ev / rev) if (ev and rev) else None, "x"),
        ("EV / EBIT", (ev / ebitda) if (ev and ebitda) else None, "x"),
        ("EV / Free Cash Flow", (ev / fcf) if (ev and fcf) else None, "x"),
        ("Earnings Yield", (eps / price * 100) if (eps and price) else None, "pct"),
        ("Free Cash Flow Yield", (fcf / mc * 100) if (fcf and mc) else None, "pct"),
        ("Mean P/E value", pe_val, "num2"),
        ("Mean P/S value", ps_val, "num2"),
        ("Mean P/B value", pb_val, "num2"),
        ("Rule of 40", ((cagr("revenue", 1) or 0) * 100 + (fcf / rev * 100 if (fcf and rev) else 0))
         if (rev and fcf) else None, "pct"),
    ]

    return {
        "symbol": (symbol or "").strip().upper(),
        "price": price, "fair": fair, "fair_upside": verdict(fair),
        "bars": bars, "other": [{"label": l, "value": v, "fmt": f} for (l, v, f) in other],
        "assumptions": f"2-stage DCF: {_Y1}y growth + {_Y2}y terminal | {int(_DISCOUNT*100)}% discount | {int(_TERMINAL_G*100)}% terminal growth | growth capped {int(_GROWTH_CAP*100)}%",
    }
