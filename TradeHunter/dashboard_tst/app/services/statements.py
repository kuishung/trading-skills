"""Detailed financial statements (Income Statement / Balance Sheet / Cash Flow) for
the Financials tab, from SEC EDGAR XBRL. Reuses sec_xbrl's fetch + per-year/quarter
merge helpers; adds subtotal computation and a TTM column.

Line items are the standard statement rows (not every provider-computed sub-line).
Dollar rows come out of the same YTD-differencing as sec_xbrl (correct for annual +
quarterly + TTM); per-share rows use the directly-tagged 3-month / FY values.
"""
from __future__ import annotations

from . import sec_xbrl as S

# (label, spec, bold, indent). spec: list[str] = XBRL concept candidates; str = a
# computed subtotal keyed below. kind is per-statement (flow / stock).
_INCOME = [
    ("Revenue", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"], True, 0),
    ("Cost of Revenue", ["CostOfRevenue", "CostOfGoodsAndServicesSold"], False, 0),
    ("Gross Profit", "@gross", True, 0),
    ("Research & Development", ["ResearchAndDevelopmentExpense"], False, 0),
    ("Selling, General & Admin", ["SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"], False, 0),
    ("Operating Income", ["OperatingIncomeLoss"], True, 0),
    ("Interest Expense", ["InterestExpense", "InterestExpenseNonoperating", "InterestAndDebtExpense"], False, 0),
    ("Pretax Income", ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                       "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"], False, 0),
    ("Income Tax Expense", ["IncomeTaxExpenseBenefit"], False, 0),
    ("Net Income", ["NetIncomeLoss", "ProfitLoss"], True, 0),
    ("EBITDA", "@ebitda", False, 0),
    ("Diluted EPS", ["EarningsPerShareDiluted"], False, 0, "eps"),
    ("Diluted Shares", ["WeightedAverageNumberOfDilutedSharesOutstanding"], False, 0, "shares"),
]

_BALANCE = [
    ("Cash & Equivalents", ["CashAndCashEquivalentsAtCarryingValue"], False, 0),
    ("Short-Term Investments", ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                                "AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue"], False, 0),
    ("Accounts Receivable", ["AccountsReceivableNetCurrent"], False, 0),
    ("Inventory", ["InventoryNet"], False, 0),
    ("Total Current Assets", ["AssetsCurrent"], True, 0),
    ("Net Property, Plant & Equip", ["PropertyPlantAndEquipmentNet"], False, 0),
    ("Goodwill", ["Goodwill"], False, 0),
    ("Intangible Assets", ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"], False, 0),
    ("Total Assets", ["Assets"], True, 0),
    ("Accounts Payable", ["AccountsPayableCurrent", "AccountsPayableCurrentAndNoncurrent"], False, 0),
    ("Total Current Liabilities", ["LiabilitiesCurrent"], True, 0),
    ("Long-Term Debt", ["LongTermDebtNoncurrent"], False, 0),
    ("Total Liabilities", ["Liabilities"], True, 0),
    ("Retained Earnings", ["RetainedEarningsAccumulatedDeficit"], False, 0),
    ("Total Shareholders' Equity", ["StockholdersEquity",
                                    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], True, 0),
]

_CASHFLOW = [
    ("Net Income", ["NetIncomeLoss", "ProfitLoss"], False, 0),
    ("Depreciation & Amortization", ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
                                     "DepreciationAndAmortization"], False, 0),
    ("Stock-Based Compensation", ["ShareBasedCompensation"], False, 0),
    ("Change in Working Capital", ["IncreaseDecreaseInOperatingCapital"], False, 0),
    ("Operating Cash Flow", ["NetCashProvidedByUsedInOperatingActivities",
                             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], True, 0),
    ("Capital Expenditures", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], False, 0),
    ("Investing Cash Flow", ["NetCashProvidedByUsedInInvestingActivities",
                             "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"], True, 0),
    ("Dividends Paid", ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"], False, 0),
    ("Share Repurchases", ["PaymentsForRepurchaseOfCommonStock"], False, 0),
    ("Financing Cash Flow", ["NetCashProvidedByUsedInFinancingActivities",
                             "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"], True, 0),
    ("Free Cash Flow", "@fcf", True, 0),
]

_STATEMENTS = {
    "income": ("Income Statement", _INCOME, "flow"),
    "balance": ("Balance Sheet", _BALANCE, "stock"),
    "cashflow": ("Cash Flow Statement", _CASHFLOW, "flow"),
}


def _q_direct(concept: dict) -> dict[str, float]:
    """3-month (single-quarter) values taken DIRECTLY (duration ~80-100d) — for
    per-share rows where YTD differencing is invalid."""
    if not concept:
        return {}
    best: dict[str, tuple] = {}
    for arr in concept.get("units", {}).values():
        for v in arr:
            if "start" not in v or "end" not in v or v.get("val") is None:
                continue
            try:
                if not (80 <= S._days(v["start"], v["end"]) <= 100):
                    continue
            except Exception:  # noqa: BLE001
                continue
            e, f = v["end"], v.get("filed", "")
            if e not in best or f > best[e][0]:
                best[e] = (f, float(v["val"]))
    return {e: val for e, (f, val) in best.items()}


def statements(symbol: str, n_years: int = 8, n_q: int = 8) -> dict:
    sym = (symbol or "").strip().upper()
    try:
        facts = S._companyfacts(sym)
        a = S.annual_financials(sym)
        q = S.quarterly_financials(sym)
    except Exception:  # noqa: BLE001
        facts, a, q = {}, {}, {}
    if not facts:
        return {"symbol": sym, "has_data": False, "statements": {}}

    years = (a.get("years") or [])[-n_years:]
    q_ends = (q.get("quarters") or [])[-n_q:]
    q_labels_all = dict(zip(q.get("quarters", []), q.get("labels", [])))
    q_labels = [q_labels_all.get(e, e) for e in q_ends]

    def extract(cands, kind):
        ann = S._annual_merged(facts, cands, kind)
        if kind == "flow":
            qtr = S._q_merged(facts, cands, kind)
        else:
            qtr = S._q_merged(facts, cands, "stock")
        return ann, qtr

    def extract_pershare(cands):
        ug = facts.get("facts", {}).get("us-gaap", {})
        ann, qtr = {}, {}
        for c in cands:
            concept = ug.get(c)
            if not concept:
                continue
            for yr, val in S._annual_series(concept, "flow").items():
                ann.setdefault(yr, val)
            for e, val in _q_direct(concept).items():
                qtr.setdefault(e, val)
        return ann, qtr

    out_statements = {}
    for skey, (title, lines, kind) in _STATEMENTS.items():
        # ── pass 1: build aligned value-lists per label ──
        vals: dict[str, dict] = {}  # label -> {"annual": [...+TTM], "quarterly": [...+TTM]}
        for line in lines:
            label, spec = line[0], line[1]
            fmt = line[4] if len(line) > 4 else "usd_m"
            if not isinstance(spec, list):
                continue
            if fmt in ("eps", "shares"):
                ann, qtr = extract_pershare(spec)  # direct 3-mo values (not differenced)
            else:
                ann, qtr = extract(spec, kind)
            a_list = [ann.get(y) for y in years]
            q_list = [qtr.get(e) for e in q_ends]
            # TTM
            if fmt == "eps":  # EPS is additive across quarters
                l4 = [qtr.get(e) for e in q_ends[-4:]]
                ttm = round(sum(l4), 2) if (len(l4) == 4 and all(v is not None for v in l4)) else None
            elif fmt == "shares":  # share count -> latest
                ttm = qtr.get(q_ends[-1]) if q_ends else None
            elif kind == "flow":
                last4 = [qtr.get(e) for e in q_ends[-4:]]
                ttm = round(sum(last4), 2) if (len(last4) == 4 and all(v is not None for v in last4)) else None
            else:
                ttm = qtr.get(q_ends[-1]) if q_ends else None
            vals[label] = {"annual": a_list + [ttm], "quarterly": q_list + [ttm]}

        # hidden helper: D&A (from the cash-flow statement) so income EBITDA works
        if skey == "income":
            da_a, da_q = extract(["DepreciationDepletionAndAmortization",
                                  "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization"], "flow")
            if not da_a:
                # companies (e.g. AVGO) tag Depreciation + AmortizationOfIntangibleAssets separately
                dep_a, dep_q = extract(["Depreciation", "DepreciationNonproduction"], "flow")
                am_a, am_q = extract(["AmortizationOfIntangibleAssets"], "flow")
                da_a = {y: (dep_a.get(y) or 0) + (am_a.get(y) or 0) for y in set(dep_a) | set(am_a)}
                da_q = {e: (dep_q.get(e) or 0) + (am_q.get(e) or 0) for e in set(dep_q) | set(am_q)}
            la = [da_q.get(e) for e in q_ends[-4:]]
            da_ttm = round(sum(la), 2) if (len(la) == 4 and all(v is not None for v in la)) else None
            vals["Depreciation & Amortization"] = {
                "annual": [da_a.get(y) for y in years] + [da_ttm],
                "quarterly": [da_q.get(e) for e in q_ends] + [da_ttm],
            }

        # ── pass 2: subtotal rows (element-wise over the aligned lists) ──
        def combine(a_lbl, b_lbl, op):
            def one(period):
                A = vals.get(a_lbl, {}).get(period, [])
                B = vals.get(b_lbl, {}).get(period, [])
                out = []
                for i in range(max(len(A), len(B))):
                    x = A[i] if i < len(A) else None
                    y = B[i] if i < len(B) else None
                    out.append(op(x, y))
                return out
            return {"annual": one("annual"), "quarterly": one("quarterly")}

        def sub(x, y):
            return (x - y) if (x is not None and y is not None) else None

        def add(x, y):
            return (x + y) if (x is not None and y is not None) else None

        for line in lines:
            label, spec = line[0], line[1]
            if spec == "@gross":
                g = combine("Revenue", "Cost of Revenue", sub)
                # gross falls back to revenue when COGS missing
                for per in ("annual", "quarterly"):
                    rv = vals.get("Revenue", {}).get(per, [])
                    g[per] = [g[per][i] if g[per][i] is not None else (rv[i] if i < len(rv) else None) for i in range(len(g[per]))]
                vals[label] = g
            elif spec == "@ebitda":
                vals[label] = combine("Operating Income", "Depreciation & Amortization", add)
            elif spec == "@fcf":
                vals[label] = combine("Operating Cash Flow", "Capital Expenditures", sub)

        # EPS TTM: Q4 EPS isn't a filed figure, so compute TTM EPS = NI_ttm / shares_ttm
        if skey == "income" and "Diluted EPS" in vals:
            ni_ttm = (vals.get("Net Income", {}).get("annual") or [None])[-1]
            sh_ttm = (vals.get("Diluted Shares", {}).get("annual") or [None])[-1]
            if ni_ttm and sh_ttm:
                eps = round(ni_ttm / sh_ttm, 2)
                vals["Diluted EPS"]["annual"][-1] = eps
                vals["Diluted EPS"]["quarterly"][-1] = eps

        rows = []
        for line in lines:
            label, _, bold, indent = line[0], line[1], line[2], line[3]
            fmt = line[4] if len(line) > 4 else "usd_m"
            v = vals.get(label, {"annual": [], "quarterly": []})
            rows.append({"label": label, "bold": bold, "indent": indent, "fmt": fmt,
                         "annual": v["annual"], "quarterly": v["quarterly"]})

        out_statements[skey] = {
            "title": title,
            "annual_periods": [str(y) for y in years] + ["TTM"],
            "quarterly_periods": q_labels + ["TTM"],
            "rows": rows,
        }

    return {"symbol": sym, "has_data": bool(years), "statements": out_statements}
