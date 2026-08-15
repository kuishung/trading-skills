"""Price/market ratios for the Financials tab — Beta, PE / PS / PB, and a WACC
estimate. These mix SEC fundamentals (revenue, net income, equity — from
`sec_xbrl`) with LIVE market prices (Yahoo via `prices.py`, free). Cached ~1h.

Honesty: **Forward PE**, **PEG**, and GuruFocus's **"without NRI"** variants need
analyst estimates / a proprietary non-recurring-items adjustment and are NOT
computed here (surfaced as n/a). WACC is an assumption-based estimate (documented
constants), not a filing figure.
"""
from __future__ import annotations

import time
from datetime import date

from . import prices

_TTL = 3600.0
_cache: dict = {}

# WACC assumptions (documented, editable). Estimate — not a filing value.
_RISK_FREE = 0.043      # ~10y US treasury
_EQUITY_PREMIUM = 0.050  # market equity risk premium
_BENCH = "SPY"


def _long_prices(symbol: str, rng: str = "5y") -> list[dict]:
    key = f"px:{symbol}:{rng}"
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        bars = prices._fetch(symbol.strip().upper(), rng)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        bars = []
    _cache[key] = (time.time() + _TTL, bars)
    return bars


def _close_on_or_before(bars: list[dict], iso: str) -> float | None:
    """The last close on/before a date (for pricing a fiscal-year end)."""
    best = None
    for b in bars:
        t, c = b.get("time"), b.get("close")
        if t and c and t <= iso:
            best = c
        elif t and t > iso:
            break
    return best


def beta(symbol: str) -> float | None:
    """Beta from ~2y of daily returns vs SPY. cov(stock, spy) / var(spy)."""
    a = _long_prices(symbol, "2y")
    b = _long_prices(_BENCH, "2y")
    if len(a) < 60 or len(b) < 60:
        return None
    ac = {x["time"]: x["close"] for x in a if x.get("close")}
    bc = {x["time"]: x["close"] for x in b if x.get("close")}
    days = sorted(set(ac) & set(bc))
    ra, rb = [], []
    for i in range(1, len(days)):
        pa0, pa1 = ac[days[i - 1]], ac[days[i]]
        pb0, pb1 = bc[days[i - 1]], bc[days[i]]
        if pa0 and pb0:
            ra.append(pa1 / pa0 - 1)
            rb.append(pb1 / pb0 - 1)
    n = len(rb)
    if n < 30:
        return None
    mb = sum(rb) / n
    ma = sum(ra) / n
    var = sum((x - mb) ** 2 for x in rb)
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    return round(cov / var, 2) if var else None


def valuation(symbol: str, annual: dict, ttm: dict) -> dict:
    """{price, market_cap, beta, wacc, current:{pe,ps,pb}, by_year:{fy:{pe,ps,pb}}}.
    Uses TTM fundamentals for the current column and per-FY price × shares for history."""
    sym = (symbol or "").strip().upper()
    key = f"val:{sym}"
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    out = {"price": None, "market_cap": None, "beta": None, "wacc": None,
           "current": {}, "by_year": {}}
    q = prices.fetch_quote(sym)
    price = q.get("price") if q else None
    bars = _long_prices(sym, "5y")
    if price is None and bars:
        price = bars[-1].get("close")
    out["price"] = price
    out["beta"] = beta(sym)

    series = annual.get("series", {})
    years = annual.get("years", [])

    def sfy(name, fy):
        return series.get(name, {}).get(fy)

    # latest shares: quarterly TTM value, else the latest fiscal-year value (dei share
    # dates don't always line up with fiscal quarter-ends, so fall back).
    shares_ttm = ttm.get("shares_out") or (sfy("shares_out", years[-1]) if years else None)
    ni_ttm = ttm.get("net_income")
    rev_ttm = ttm.get("revenue")
    eq_latest = sfy("equity", years[-1]) if years else None
    if price and shares_ttm:
        mc = price * shares_ttm
        out["market_cap"] = round(mc, 0)
        eps_ttm = (ni_ttm / shares_ttm) if (ni_ttm and shares_ttm) else None
        out["current"] = {
            "pe": round(price / eps_ttm, 2) if (eps_ttm and eps_ttm > 0) else None,
            "ps": round(mc / rev_ttm, 2) if rev_ttm else None,
            "pb": round(mc / eq_latest, 2) if eq_latest else None,
        }
        # WACC estimate
        debt = sfy("total_debt", years[-1]) if years else None
        if debt is None:
            dn, dc = sfy("debt_noncurrent", years[-1]), sfy("debt_current", years[-1])
            debt = ((dn or 0) + (dc or 0)) if (dn is not None or dc is not None) else 0
        ke = _RISK_FREE + (out["beta"] or 1.0) * _EQUITY_PREMIUM
        int_exp = ttm.get("interest_expense")
        kd = (int_exp / debt) if (int_exp and debt) else 0.05
        tax = 0.15
        v = mc + (debt or 0)
        if v:
            wacc = (mc / v) * ke + ((debt or 0) / v) * kd * (1 - tax)
            out["wacc"] = round(wacc * 100, 2)

    # Split-adjust historical shares to today's basis: Yahoo's `close` is split-
    # adjusted, but as-reported share counts aren't. A stock split shows up as a large
    # (~integer) jump between adjacent years' share counts (buybacks move it only a few
    # %). Detect that and scale earlier years so price×shares stays consistent.
    sh_series = series.get("shares_out", {})
    adj_shares: dict[int, float] = {}
    factor, newer = 1.0, None
    for fy in sorted(sh_series.keys(), reverse=True):
        cur = sh_series[fy]
        if newer and cur:
            ratio = newer / cur
            for sp in (10, 8, 7, 6, 5, 4, 3, 2):
                if abs(ratio - sp) / sp < 0.12:
                    factor *= sp
                    break
        adj_shares[fy] = cur * factor if cur else None
        newer = cur

    # historical PE/PS/PB: price at each fiscal-year END (real date from sec_xbrl,
    # handles Jan/Jun/Sep year-ends) × that year's split-adjusted shares.
    fy_end = annual.get("fy_end", {})
    for fy in years:
        end_iso = fy_end.get(fy) or f"{fy}-12-31"
        p = _close_on_or_before(bars, end_iso)
        sh = adj_shares.get(fy)
        ni = sfy("net_income", fy)
        rev = sfy("revenue", fy)
        eq = sfy("equity", fy)
        if not (p and sh):
            continue
        mc = p * sh
        row = {}
        if ni and ni > 0:
            row["pe"] = round(mc / ni, 2)
        if rev:
            row["ps"] = round(mc / rev, 2)
        if eq:
            row["pb"] = round(mc / eq, 2)
        if row:
            out["by_year"][fy] = row

    _cache[key] = (time.time() + _TTL, out)
    return out
