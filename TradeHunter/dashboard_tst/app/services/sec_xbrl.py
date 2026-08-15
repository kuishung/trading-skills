"""SEC EDGAR XBRL — free structured fundamentals for the Company page's Financials
trend charts + ratio tables.

Source: `data.sec.gov` companyfacts (no key; a descriptive User-Agent is required,
`settings.sec_user_agent`). We pull the raw us-gaap/dei line items, build a clean
per-fiscal-year series (deduped, with candidate-tag fallbacks for concepts that
drift between companies), and derive the ratios. Everything is cached ~12h (filings
are quarterly) and soft-fails to empty.

Scope note: ANNUAL series first (the charts default to Annual). Quarterly + TTM and
the price/market ratios (Beta, PE/PS/PB) layer on later. All exact figures come
from the filings; nothing is invented.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date

from ..config import settings

_TTL = 12 * 3600.0
_cache: dict = {}
_lock = threading.Lock()

# ticker -> (concept candidates, kind). kind: 'flow' (duration, income/cash-flow) or
# 'stock' (instant, balance-sheet). Candidate lists cover XBRL tag drift across filers.
_BASE = {
    "revenue": (["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"], "flow"),
    "cost_of_revenue": (["CostOfRevenue", "CostOfGoodsAndServicesSold"], "flow"),
    "gross_profit": (["GrossProfit"], "flow"),
    "operating_income": (["OperatingIncomeLoss"], "flow"),
    "net_income": (["NetIncomeLoss"], "flow"),
    "ocf": (["NetCashProvidedByUsedInOperatingActivities",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], "flow"),
    "capex": (["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], "flow"),
    "sbc": (["ShareBasedCompensation", "ShareBasedCompensationExpense"], "flow"),
    "interest_expense": (["InterestExpense", "InterestExpenseDebt", "InterestAndDebtExpense"], "flow"),
    "dep_amort": (["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
                   "DepreciationAndAmortization"], "flow"),
    "cash": (["CashAndCashEquivalentsAtCarryingValue"], "stock"),
    "sti": (["ShortTermInvestments", "MarketableSecuritiesCurrent",
             "AvailableForSaleSecuritiesCurrent", "OtherShortTermInvestments"], "stock"),
    "debt_noncurrent": (["LongTermDebtNoncurrent"], "stock"),
    "debt_current": (["LongTermDebtCurrent", "DebtCurrent"], "stock"),
    "inventory": (["InventoryNet"], "stock"),
    "receivables": (["AccountsReceivableNetCurrent"], "stock"),
    "payables": (["AccountsPayableCurrent", "AccountsPayableCurrentAndNoncurrent"], "stock"),
    "total_assets": (["Assets"], "stock"),
    "current_assets": (["AssetsCurrent"], "stock"),
    "current_liabilities": (["LiabilitiesCurrent"], "stock"),
    "equity": (["StockholdersEquity",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "stock"),
    "ppe_net": (["PropertyPlantAndEquipmentNet"], "stock"),
    "income_tax": (["IncomeTaxExpenseBenefit"], "flow"),
}


def _get_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": settings.sec_user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": url.split("/")[2],
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw)


def _cik_map() -> dict:
    hit = _cache.get("cikmap")
    if hit and hit[0] > time.time():
        return hit[1]
    out: dict = {}
    try:
        data = _get_json("https://www.sec.gov/files/company_tickers.json")
        for row in data.values():
            out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    except Exception:  # noqa: BLE001
        out = {}
    _cache["cikmap"] = (time.time() + 24 * 3600, out)
    return out


def cik_for(symbol: str) -> str | None:
    return _cik_map().get((symbol or "").strip().upper())


def _companyfacts(symbol: str) -> dict:
    sym = (symbol or "").strip().upper()
    cik = cik_for(sym)
    if not cik:
        return {}
    return _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _concept(facts: dict, candidates: list[str]) -> dict | None:
    ug = facts.get("facts", {}).get("us-gaap", {})
    for c in candidates:
        if c in ug:
            return ug[c]
    return None


def _annual_series(concept: dict, kind: str) -> dict[int, float]:
    """{fiscal_year: value} from a concept's datapoints, keyed by the YEAR THE PERIOD
    ENDS (matches how data tools label fiscal years — NVDA's year ended Jan-2024 is
    "2024", not the XBRL `fy` tag which is offset for early-calendar year-ends).
    Annual = fp 'FY'; flows need a ~1-year duration; on duplicates keep the most
    recently filed (restated) value."""
    if not concept:
        return {}
    best: dict[int, tuple[str, float]] = {}
    for arr in concept.get("units", {}).values():
        for v in arr:
            if v.get("fp") != "FY" or v.get("val") is None or "end" not in v:
                continue
            if kind == "flow":
                if "start" not in v:
                    continue
                try:
                    if not (300 <= _days(v["start"], v["end"]) <= 400):
                        continue
                except Exception:  # noqa: BLE001
                    continue
            try:
                yr = date.fromisoformat(v["end"]).year
            except Exception:  # noqa: BLE001
                continue
            filed = v.get("filed", "")
            if yr not in best or filed > best[yr][0]:
                best[yr] = (filed, float(v["val"]))
    return {yr: val for yr, (f, val) in best.items()}


def _safe_div(a, b):
    return (a / b) if (a is not None and b) else None


def annual_financials(symbol: str) -> dict:
    """Per-fiscal-year base line items + derived ratios for the Financials tab.
    {'symbol','years':[...],'series':{metric:{fy:val}},'ratios':{ratio:{fy:val}}}.
    Cached ~12h; empty on failure."""
    sym = (symbol or "").strip().upper()
    key = f"annual:{sym}"
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    out = {"symbol": sym, "years": [], "series": {}, "ratios": {}}
    try:
        facts = _companyfacts(sym)
    except Exception:  # noqa: BLE001
        facts = {}
    if not facts:
        with _lock:
            _cache[key] = (time.time() + 300, out)
        return out

    series: dict[str, dict[int, float]] = {}
    for name, (cands, kind) in _BASE.items():
        series[name] = _annual_series(_concept(facts, cands), kind)

    # shares outstanding from the dei taxonomy (instant)
    dei = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    series["shares_out"] = _annual_series(dei, "stock") if dei else {}

    years = sorted({fy for s in series.values() for fy in s})
    # keep the last ~11 fiscal years (10Y + current)
    years = years[-11:]

    def g(name, fy):
        return series.get(name, {}).get(fy)

    def avg(name, fy):
        """Average of this year's and last year's ending balance (the convention data
        tools use for turnover/return ratios); falls back to the single value."""
        cur, prev = g(name, fy), g(name, fy - 1)
        if cur is None:
            return None
        return (cur + prev) / 2.0 if prev is not None else cur

    ratios: dict[str, dict[int, float]] = {}

    def put(rn, fy, val):
        if val is not None:
            ratios.setdefault(rn, {})[fy] = val

    for fy in years:
        rev = g("revenue", fy)
        cogs = g("cost_of_revenue", fy)
        gp = g("gross_profit", fy)
        if gp is None and rev is not None and cogs is not None:
            gp = rev - cogs
        oi = g("operating_income", fy)
        ni = g("net_income", fy)
        ocf = g("ocf", fy)
        capex = g("capex", fy)
        fcf = (ocf - capex) if (ocf is not None and capex is not None) else None
        debt = None
        dn, dc = g("debt_noncurrent", fy), g("debt_current", fy)
        if dn is not None or dc is not None:
            debt = (dn or 0) + (dc or 0)
        ebitda = (oi + g("dep_amort", fy)) if (oi is not None and g("dep_amort", fy) is not None) else None
        inv, rec, pay = g("inventory", fy), g("receivables", fy), g("payables", fy)

        # margins (%)
        put("gross_margin", fy, _pct(_safe_div(gp, rev)))
        put("operating_margin", fy, _pct(_safe_div(oi, rev)))
        put("net_margin", fy, _pct(_safe_div(ni, rev)))
        put("ocf_margin", fy, _pct(_safe_div(ocf, rev)))
        put("fcf_margin", fy, _pct(_safe_div(fcf, rev)))
        # returns (%) — average balances
        put("roa", fy, _pct(_safe_div(ni, avg("total_assets", fy))))
        put("roe", fy, _pct(_safe_div(ni, avg("equity", fy))))
        # leverage / liquidity (point-in-time)
        put("debt_to_ebitda", fy, _safe_div(debt, ebitda))
        put("interest_coverage", fy, _safe_div(oi, g("interest_expense", fy)))
        put("current_ratio", fy, _safe_div(g("current_assets", fy), g("current_liabilities", fy)))
        cl = g("current_liabilities", fy)
        put("cash_ratio", fy, _safe_div((g("cash", fy) or 0) + (g("sti", fy) or 0) if cl else None, cl))
        # efficiency — average balances for turnover/days
        put("asset_turnover", fy, _safe_div(rev, avg("total_assets", fy)))
        dio = _safe_div(365.0, _safe_div(cogs, avg("inventory", fy))) if (cogs and avg("inventory", fy)) else None
        dso = _safe_div(365.0, _safe_div(rev, avg("receivables", fy))) if (rev and avg("receivables", fy)) else None
        dpo = _safe_div(365.0, _safe_div(cogs, avg("payables", fy))) if (cogs and avg("payables", fy)) else None
        put("dio", fy, dio)
        put("dso", fy, dso)
        put("dpo", fy, dpo)
        if dio is not None and dso is not None and dpo is not None:
            put("ccc", fy, dio + dso - dpo)
        # turnovers (average balances)
        put("inventory_turnover", fy, _safe_div(cogs, avg("inventory", fy)))
        put("receivables_turnover", fy, _safe_div(rev, avg("receivables", fy)))
        put("payables_turnover", fy, _safe_div(cogs, avg("payables", fy)))
        put("fixed_asset_turnover", fy, _safe_div(rev, avg("ppe_net", fy)))
        # capex intensity
        put("capex_to_revenue", fy, _safe_div(capex, rev))
        put("capex_to_ocf", fy, _safe_div(capex, ocf))
        put("capex_to_opinc", fy, _safe_div(capex, oi))
        # ROIC = NOPAT / avg invested capital (debt + equity). NOPAT = op income x
        # (1 - effective tax rate), tax rate from tax / pretax (pretax = NI + tax).
        eq = g("equity", fy)
        inv_cap = ((debt or 0) + eq) if eq is not None else None
        if inv_cap is not None:
            series.setdefault("invested_capital", {})[fy] = inv_cap
        tax = g("income_tax", fy)
        nopat = None
        if oi is not None:
            if tax is not None and ni is not None and (ni + tax):
                nopat = oi * (1 - tax / (ni + tax))
            else:
                nopat = oi
        put("roic", fy, _pct(_safe_div(nopat, avg("invested_capital", fy))))
        # store derived level series too (for the trend charts)
        if fcf is not None:
            series.setdefault("fcf", {})[fy] = fcf
        if debt is not None:
            series.setdefault("total_debt", {})[fy] = debt
        cs = (g("cash", fy) or 0) + (g("sti", fy) or 0) if (g("cash", fy) is not None or g("sti", fy) is not None) else None
        if cs is not None:
            series.setdefault("cash_and_sti", {})[fy] = cs

    out = {"symbol": sym, "years": years, "series": series, "ratios": ratios}
    with _lock:
        _cache[key] = (time.time() + _TTL, out)
    return out


def _pct(x):
    return None if x is None else x * 100.0
