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
    "net_income": (["NetIncomeLoss", "ProfitLoss"], "flow"),
    "ocf": (["NetCashProvidedByUsedInOperatingActivities",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], "flow"),
    "capex": (["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], "flow"),
    "sbc": (["ShareBasedCompensation", "ShareBasedCompensationExpense"], "flow"),
    "interest_expense": (["InterestExpense", "InterestExpenseNonoperating",
                          "InterestExpenseDebt", "InterestAndDebtExpense"], "flow"),
    "dep_amort": (["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
                   "DepreciationAndAmortization"], "flow"),
    "cash": (["CashAndCashEquivalentsAtCarryingValue"], "stock"),
    "sti": (["ShortTermInvestments", "MarketableSecuritiesCurrent",
             "AvailableForSaleSecuritiesCurrent", "OtherShortTermInvestments",
             "AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue"], "stock"),
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


def _annual_merged(facts: dict, candidates: list[str], kind: str) -> dict[int, float]:
    """Per-year union across candidate concepts, higher-priority (earlier) candidate
    winning each year. Handles XBRL tag drift where a company switches the tag it uses
    for a line item between fiscal years (e.g. NVDA's short-term-investments tag)."""
    ug = facts.get("facts", {}).get("us-gaap", {})
    merged: dict[int, float] = {}
    for c in candidates:
        concept = ug.get(c)
        if not concept:
            continue
        for yr, val in _annual_series(concept, kind).items():
            merged.setdefault(yr, val)
    return merged


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
        series[name] = _annual_merged(facts, cands, kind)

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

    # fiscal-year END dates (for pricing historical valuation ratios at the right day)
    fy_end: dict[int, str] = {}
    rev_c = _concept(facts, _BASE["revenue"][0])
    if rev_c:
        for arr in rev_c.get("units", {}).values():
            for v in arr:
                if v.get("fp") == "FY" and "start" in v and "end" in v:
                    try:
                        if 300 <= _days(v["start"], v["end"]) <= 400:
                            yr = date.fromisoformat(v["end"]).year
                            if v["end"] > fy_end.get(yr, ""):
                                fy_end[yr] = v["end"]
                    except Exception:  # noqa: BLE001
                        pass

    out = {"symbol": sym, "years": years, "series": series, "ratios": ratios, "fy_end": fy_end}
    with _lock:
        _cache[key] = (time.time() + _TTL, out)
    return out


def _pct(x):
    return None if x is None else x * 100.0


# ── Quarterly ─────────────────────────────────────────────────────────────────
def _q_flow(concept: dict) -> dict[str, float]:
    """Single-quarter flow values keyed by quarter-END date. Flows are reported
    year-to-date (cumulative from the fiscal-year start) — esp. cash-flow items — so
    we take the cumulative series per fiscal year and difference it (Q_n = YTD_n −
    YTD_{n-1}); Q4 falls out as FY − 9-month. Handles both income (3-mo tagged) and
    cash-flow (YTD-only) items uniformly."""
    if not concept:
        return {}
    pts = []
    for arr in concept.get("units", {}).values():
        for v in arr:
            if "start" in v and "end" in v and v.get("val") is not None:
                try:
                    dur = _days(v["start"], v["end"])
                except Exception:  # noqa: BLE001
                    continue
                pts.append((v["start"], v["end"], dur, float(v["val"]), v.get("filed", "")))
    fy_starts = {s for (s, e, dur, val, f) in pts if 300 <= dur <= 400}
    # also the CURRENT (in-progress) fiscal year's start — the day after each known
    # fiscal-year end — so the latest quarters get picked up before the FY 10-K exists.
    from datetime import timedelta
    for (s, e, dur, val, f) in pts:
        if 300 <= dur <= 400:
            try:
                fy_starts.add((date.fromisoformat(e) + timedelta(days=1)).isoformat())
            except Exception:  # noqa: BLE001
                pass
    cum: dict[str, dict[str, tuple]] = {}  # fy_start -> {end: (filed, val)}
    for (s, e, dur, val, f) in pts:
        if s in fy_starts and 60 <= dur <= 400:
            g = cum.setdefault(s, {})
            if e not in g or f > g[e][0]:
                g[e] = (f, val)
    out: dict[str, float] = {}
    for s, ends in cum.items():
        prev = 0.0
        for e, (f, val) in sorted(ends.items()):
            out[e] = round(val - prev, 2)
            prev = val
    return out


def _q_stock(concept: dict) -> dict[str, float]:
    """Balance-sheet (instant) values keyed by quarter-END date."""
    if not concept:
        return {}
    best: dict[str, tuple] = {}
    for arr in concept.get("units", {}).values():
        for v in arr:
            if "end" in v and "start" not in v and v.get("val") is not None:
                e, f = v["end"], v.get("filed", "")
                if e not in best or f > best[e][0]:
                    best[e] = (f, float(v["val"]))
    return {e: val for e, (f, val) in best.items()}


def _q_merged(facts: dict, candidates: list[str], kind: str) -> dict[str, float]:
    ug = facts.get("facts", {}).get("us-gaap", {})
    merged: dict[str, float] = {}
    for c in candidates:
        concept = ug.get(c)
        if not concept:
            continue
        s = _q_flow(concept) if kind == "flow" else _q_stock(concept)
        for e, val in s.items():
            merged.setdefault(e, val)
    return merged


def quarterly_financials(symbol: str, n: int = 13) -> dict:
    """Last ~n quarters of base line items (single-quarter flows + quarter-end stocks)
    keyed by quarter-END date, plus a TTM column (flows = sum of last 4 quarters;
    stocks = latest). {'symbol','quarters':[end...],'labels':[...],'series':{m:{end:v}},
    'ttm':{metric:val}}. Cached ~12h."""
    sym = (symbol or "").strip().upper()
    key = f"q:{sym}"
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    out = {"symbol": sym, "quarters": [], "labels": [], "series": {}, "ttm": {}}
    try:
        facts = _companyfacts(sym)
    except Exception:  # noqa: BLE001
        facts = {}
    if not facts:
        with _lock:
            _cache[key] = (time.time() + 300, out)
        return out

    series: dict[str, dict[str, float]] = {}
    for name, (cands, kind) in _BASE.items():
        series[name] = _q_merged(facts, cands, kind)
    dei = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    series["shares_out"] = _q_stock(dei) if dei else {}

    # the union of quarter-end dates that have a revenue value (the reliable spine)
    ends = sorted(series.get("revenue", {}).keys())[-n:]
    if not ends:
        with _lock:
            _cache[key] = (time.time() + 300, out)
        return out

    def g(name, e):
        return series.get(name, {}).get(e)

    # derived per-quarter level series
    for e in ends:
        ocf, capex = g("ocf", e), g("capex", e)
        if ocf is not None and capex is not None:
            series.setdefault("fcf", {})[e] = round(ocf - capex, 2)
        cash, sti = g("cash", e), g("sti", e)
        if cash is not None or sti is not None:
            series.setdefault("cash_and_sti", {})[e] = round((cash or 0) + (sti or 0), 2)
        dn, dc = g("debt_noncurrent", e), g("debt_current", e)
        if dn is not None or dc is not None:
            series.setdefault("total_debt", {})[e] = round((dn or 0) + (dc or 0), 2)
        rev = g("revenue", e)
        series.setdefault("gross_margin", {})[e] = _pct(_safe_div(
            (g("gross_profit", e) if g("gross_profit", e) is not None
             else (rev - g("cost_of_revenue", e)) if (rev is not None and g("cost_of_revenue", e) is not None) else None), rev))
        series.setdefault("operating_margin", {})[e] = _pct(_safe_div(g("operating_income", e), rev))
        series.setdefault("net_margin", {})[e] = _pct(_safe_div(g("net_income", e), rev))

    # rolling-TTM ratios per quarter (ROE/ROA on trailing-4 NI; CCC on TTM flows +
    # quarter-end average balances) so the Returns/CCC panels work in quarterly mode
    for i, e in enumerate(ends):
        if i < 3:
            continue
        w = ends[i - 3:i + 1]

        def tsum(m):
            vs = [series.get(m, {}).get(x) for x in w]
            return sum(vs) if all(v is not None for v in vs) else None

        def avgq(m):
            cur = g(m, e)
            prev = g(m, ends[i - 4]) if i >= 4 else None
            return (cur + prev) / 2.0 if (cur is not None and prev is not None) else cur

        ni, rev, cogs = tsum("net_income"), tsum("revenue"), tsum("cost_of_revenue")
        roe = _pct(_safe_div(ni, avgq("equity")))
        roa = _pct(_safe_div(ni, avgq("total_assets")))
        if roe is not None:
            series.setdefault("roe", {})[e] = roe
        if roa is not None:
            series.setdefault("roa", {})[e] = roa
        inv, rec, pay = avgq("inventory"), avgq("receivables"), avgq("payables")
        dio = _safe_div(365.0, _safe_div(cogs, inv)) if (cogs and inv) else None
        dso = _safe_div(365.0, _safe_div(rev, rec)) if (rev and rec) else None
        dpo = _safe_div(365.0, _safe_div(cogs, pay)) if (cogs and pay) else None
        if None not in (dio, dso, dpo):
            series.setdefault("ccc", {})[e] = dio + dso - dpo

    # TTM: flows = sum last 4 quarters; stocks = latest quarter value
    flow_names = {n for n, (c, k) in _BASE.items() if k == "flow"} | {"fcf"}
    last4 = ends[-4:]
    ttm: dict[str, float] = {}
    for name in list(series.keys()):
        if name in flow_names:
            vals = [series[name].get(e) for e in last4]
            if all(v is not None for v in vals) and len(vals) == 4:
                ttm[name] = round(sum(vals), 2)
        else:
            v = series.get(name, {}).get(ends[-1])
            if v is not None:
                ttm[name] = v
    labels = [f"{date.fromisoformat(e).strftime('%b')} '{e[2:4]}" for e in ends]
    out = {"symbol": sym, "quarters": ends, "labels": labels, "series": series, "ttm": ttm}
    with _lock:
        _cache[key] = (time.time() + _TTL, out)
    return out
