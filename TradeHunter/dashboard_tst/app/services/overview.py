"""Company Overview snapshot — the Overview tab. Combines three free sources:
  - SEC EDGAR XBRL (sec_xbrl / valuation): the fundamentals + computed ratios,
    growth rates, ROIC, CCC, Altman-Z, and the historical context.
  - Yahoo quoteSummary (reusing prices._yahoo_session): company profile
    (description/sector/industry), float, forward PE / PEG, dividends, and the
    market-info fields.
  - Yahoo daily bars (prices): 52-week range + trailing performance.
Everything soft-fails to None; nothing is invented. Cached ~1h.
"""
from __future__ import annotations

import time
from datetime import date

import httpx

from . import prices
from .valuation import _long_prices
from .valuation import valuation as _valuation

_TTL = 3600.0
_cache: dict = {}
_UA = "Mozilla/5.0 (TradeHunter overview)"


def _quote_summary(symbol: str) -> dict:
    key = f"qs:{symbol}"
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    out: dict = {}
    crumb, cookies = prices._yahoo_session()  # noqa: SLF001
    if crumb:
        mods = "assetProfile,price,summaryDetail,defaultKeyStatistics,financialData,calendarEvents"
        for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
            try:
                r = httpx.get(
                    f"https://{host}/v10/finance/quoteSummary/{symbol}",
                    params={"modules": mods, "crumb": crumb},
                    headers={"User-Agent": _UA}, cookies=cookies, timeout=8.0,
                )
                if r.status_code != 200:
                    continue
                res = (r.json().get("quoteSummary") or {}).get("result") or []
                if res:
                    out = res[0]
                    break
            except Exception:  # noqa: BLE001
                continue
    _cache[key] = (time.time() + _TTL, out)
    return out


def _raw(mod: dict, key):
    v = (mod or {}).get(key)
    if isinstance(v, dict):
        return v.get("raw")
    return v


def _perf(bars: list[dict]) -> dict:
    """Trailing performance from daily closes."""
    if len(bars) < 2:
        return {}
    closes = [(b["time"], b["close"]) for b in bars if b.get("close")]
    if not closes:
        return {}
    last_t, last = closes[-1]

    def back(n):
        if len(closes) > n:
            p = closes[-1 - n][1]
            return round((last / p - 1) * 100, 2) if p else None
        return None

    def since(iso):
        p = None
        for t, c in closes:
            if t >= iso:
                p = c
                break
        return round((last / p - 1) * 100, 2) if p else None

    yr = last_t[:4]
    return {
        "1d": back(1), "1m": back(21), "ytd": since(f"{yr}-01-01"),
        "1y": back(252), "5y": back(252 * 5), "10y": back(252 * 10),
        "hi52": round(max(c for _, c in closes[-252:]), 2),
        "lo52": round(min(c for _, c in closes[-252:]), 2),
    }


def _growth(series: dict, years: list[int], back: int) -> float | None:
    """CAGR% over `back` fiscal years (simple % for back==1). Level-based -> split-safe."""
    if len(years) <= back:
        return None
    cur, old = series.get(years[-1]), series.get(years[-1 - back])
    if cur is None or old is None or old <= 0:
        return None
    if back == 1:
        return round((cur / old - 1) * 100, 2)
    return round(((cur / old) ** (1 / back) - 1) * 100, 2)


def overview(symbol: str, annual: dict, ttm: dict) -> dict:
    sym = (symbol or "").strip().upper()
    key = f"ov:{sym}"
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    qs = _quote_summary(sym)
    prof = qs.get("assetProfile") or {}
    dks = qs.get("defaultKeyStatistics") or {}
    sd = qs.get("summaryDetail") or {}
    fd = qs.get("financialData") or {}
    valn = _valuation(sym, annual, ttm)
    bars = _long_prices(sym, "10y")
    perf = _perf(bars)

    series = annual.get("series", {})
    ratios = annual.get("ratios", {})
    years = annual.get("years", [])

    def sfy(name, fy):
        return series.get(name, {}).get(fy)

    def rfy(name, fy):
        return ratios.get(name, {}).get(fy)

    def avg(name, n):
        vals = [ratios.get(name, {}).get(fy) for fy in years[-n:] if ratios.get(name, {}).get(fy) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    price = valn.get("price")
    shares = ttm.get("shares_out") or (sfy("shares_out", years[-1]) if years else None)
    mc = valn.get("market_cap")
    cash = ttm.get("cash_and_sti")
    debt = ttm.get("total_debt")
    ev = (mc + (debt or 0) - (cash or 0)) if mc is not None else _raw(dks, "enterpriseValue")
    ni_ttm, rev_ttm, fcf_ttm, ocf_ttm = ttm.get("net_income"), ttm.get("revenue"), ttm.get("fcf"), ttm.get("ocf")
    eps_ttm = (ni_ttm / shares) if (ni_ttm and shares) else None
    eq = sfy("equity", years[-1]) if years else None

    out = {
        "symbol": sym,
        "name": _raw(qs.get("price") or {}, "shortName") or sym,
        "description": prof.get("longBusinessSummary"),
        "sector": prof.get("sector"),
        "industry": prof.get("industry"),
        "sections": [],
    }

    def sec(title, items):
        rows = [{"label": l, "value": v, "fmt": f} for (l, v, f) in items]
        # key is "rows" (not "items") — Jinja resolves dict.items to the method.
        out["sections"].append({"title": title, "rows": rows})

    sec("Key Statistics", [
        ("Market Capitalization", mc, "usd_m"),
        ("Enterprise Value", ev, "usd_m"),
        ("Beta", _raw(dks, "beta") or _raw(sd, "beta") or valn.get("beta"), "num"),
        ("Shares Outstanding", shares, "shares_m"),
        ("Shares Float", _raw(dks, "floatShares"), "shares_m"),
        ("30-Day Average Volume", _raw(sd, "averageVolume"), "shares_m"),
        ("Previous Close", _raw(sd, "previousClose") or price, "price"),
        ("52-Week High", perf.get("hi52"), "price"),
        ("52-Week Low", perf.get("lo52"), "price"),
        ("Next Earnings Date", (prices.fetch_next_earnings(sym) or {}).get("date"), "date"),
    ])

    sec("Financial Snapshot (TTM)", [
        ("Revenue", rev_ttm, "usd_m"),
        ("Operating Income", ttm.get("operating_income"), "usd_m"),
        ("Net Income", ni_ttm, "usd_m"),
        ("Operating Cash Flow", ocf_ttm, "usd_m"),
        ("Free Cash Flow", fcf_ttm, "usd_m"),
        ("Earnings per Share", eps_ttm, "num2"),
        ("Revenue per Share", (rev_ttm / shares) if (rev_ttm and shares) else None, "num2"),
        ("Book Value per Share", (eq / shares) if (eq and shares) else None, "num2"),
        ("Net Cash per Share", (((cash or 0) - (debt or 0)) / shares) if shares else None, "num2"),
        ("Cash, Equiv & ST Investments", cash, "usd_m"),
        ("Total Debt", debt, "usd_m"),
        ("Op. Cash Flow / Net Income", (ocf_ttm / ni_ttm) if (ocf_ttm and ni_ttm) else None, "num2"),
    ])

    sec("Growth", [
        ("1-Year Revenue Growth", _growth(series.get("revenue", {}), years, 1), "pct"),
        ("5-Year Revenue Growth", _growth(series.get("revenue", {}), years, 5), "pct"),
        ("10-Year Revenue Growth", _growth(series.get("revenue", {}), years, 10), "pct"),
        ("1-Year Net Income Growth", _growth(series.get("net_income", {}), years, 1), "pct"),
        ("5-Year Net Income Growth", _growth(series.get("net_income", {}), years, 5), "pct"),
        ("10-Year Net Income Growth", _growth(series.get("net_income", {}), years, 10), "pct"),
        ("1-Year Free Cash Flow Growth", _growth(series.get("fcf", {}), years, 1), "pct"),
        ("5-Year Free Cash Flow Growth", _growth(series.get("fcf", {}), years, 5), "pct"),
        ("Projected EPS Growth (analyst)", _raw(fd, "earningsGrowth") and _raw(fd, "earningsGrowth") * 100, "pct"),
    ])

    latest = years[-1] if years else None
    sec("Financial Strength", [
        ("Total Debt / Equity", (debt / eq) if (debt and eq) else _raw(fd, "debtToEquity"), "num2"),
        ("Total Debt / EBITDA (TTM)", rfy("debt_to_ebitda", latest), "num2"),
        ("Interest Coverage", rfy("interest_coverage", latest), "num2"),
        ("Current Ratio", _raw(fd, "currentRatio") or rfy("current_ratio", latest), "num2"),
        ("Quick Ratio", _raw(fd, "quickRatio"), "num2"),
        ("Cash Ratio", rfy("cash_ratio", latest), "num2"),
    ])

    sec("Efficiency (TTM)", [
        ("Cash Conversion Cycle", ratios.get("ccc", {}).get(latest), "num2"),
        ("Days Inventory Outstanding", rfy("dio", latest), "num2"),
        ("Days Payables Outstanding", rfy("dpo", latest), "num2"),
        ("Inventory Turnover", rfy("inventory_turnover", latest), "num2"),
        ("Receivables Turnover", rfy("receivables_turnover", latest), "num2"),
        ("Asset Turnover", rfy("asset_turnover", latest), "num2"),
    ])

    def ttm_margin(num):
        return round(num / rev_ttm * 100, 2) if (num is not None and rev_ttm) else None

    sec("Profitability", [
        ("Gross Profit Margin (TTM)", ttm_margin(ttm.get("gross_profit")), "pct"),
        ("Gross Profit Margin (5Y Avg)", avg("gross_margin", 5), "pct"),
        ("Operating Margin (TTM)", ttm_margin(ttm.get("operating_income")), "pct"),
        ("Operating Margin (5Y Avg)", avg("operating_margin", 5), "pct"),
        ("Net Profit Margin (TTM)", ttm_margin(ni_ttm), "pct"),
        ("Net Profit Margin (5Y Avg)", avg("net_margin", 5), "pct"),
        ("Return on Equity (TTM)", (ni_ttm / eq * 100) if (ni_ttm and eq) else None, "pct"),
        ("Return on Equity (5Y Avg)", avg("roe", 5), "pct"),
        ("Return on Invested Capital", rfy("roic", latest), "pct"),
        ("Return on Assets (TTM)", (ni_ttm / sfy("total_assets", latest) * 100) if (ni_ttm and sfy("total_assets", latest)) else None, "pct"),
    ])

    cur = valn.get("current", {})
    fcf_margin = (fcf_ttm / rev_ttm * 100) if (fcf_ttm and rev_ttm) else None
    rev_growth_1y = _growth(series.get("revenue", {}), years, 1)
    sec("Valuation", [
        ("Price / Earnings (PE, TTM)", cur.get("pe"), "num2"),
        ("Forward PE (analyst)", _raw(dks, "forwardPE"), "num2"),
        ("PEG Ratio (analyst)", _raw(dks, "pegRatio") or _raw(dks, "trailingPegRatio"), "num2"),
        ("Price / Sales (PS)", cur.get("ps"), "num2"),
        ("Price / Book (PB)", cur.get("pb"), "num2"),
        ("Price / Free Cash Flow", (mc / fcf_ttm) if (mc and fcf_ttm) else None, "num2"),
        ("Earnings Yield", (eps_ttm / price * 100) if (eps_ttm and price) else None, "pct"),
        ("Free Cash Flow Yield", (fcf_ttm / mc * 100) if (fcf_ttm and mc) else None, "pct"),
        ("Enterprise Value", ev, "usd_m"),
        ("WACC (estimate)", valn.get("wacc"), "pct"),
        ("Rule of 40", (rev_growth_1y + fcf_margin) if (rev_growth_1y is not None and fcf_margin is not None) else None, "pct"),
    ])

    sec("Market Information", [
        ("1-Day Performance", perf.get("1d"), "pct"),
        ("1-Month Performance", perf.get("1m"), "pct"),
        ("Year to Date Performance", perf.get("ytd"), "pct"),
        ("1-Year Performance", perf.get("1y"), "pct"),
        ("5-Year Performance", perf.get("5y"), "pct"),
        ("10-Year Performance", perf.get("10y"), "pct"),
    ])

    div_rate = _raw(sd, "dividendRate") or _raw(sd, "trailingAnnualDividendRate")
    sec("Dividend & Buybacks", [
        ("Dividend per Share", div_rate, "num2"),
        ("Dividend Yield", _raw(sd, "dividendYield") and _raw(sd, "dividendYield") * 100, "pct"),
        ("Payout Ratio", _raw(sd, "payoutRatio") and _raw(sd, "payoutRatio") * 100, "pct"),
        ("Ex-Dividend Date", _fmt_epoch(_raw(sd, "exDividendDate")), "date"),
        ("Forward Dividend Yield", _raw(dks, "forwardAnnualDividendYield") and _raw(dks, "forwardAnnualDividendYield") * 100, "pct"),
    ])

    _cache[key] = (time.time() + _TTL, out)
    return out


def _fmt_epoch(ts):
    try:
        return date.fromtimestamp(ts).isoformat() if ts else None
    except Exception:  # noqa: BLE001
        return None
