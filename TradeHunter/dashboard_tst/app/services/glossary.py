"""Plain-English definitions for every metric label shown on the Company page.

Purpose (user, 2026-08-16): hovering a row label on the Overview / Financials
tabs should explain what the figure MEANS and how to read it -- so a member who
isn't a career analyst can interpret the number, not just see it.

House style for every entry: one or two short sentences, "what it is" then "how
to read it" (which direction is good / what a typical value looks like). No
jargon that isn't itself defined here. Never invent precision we don't have.

Lookup is by NORMALISED label (lowercase, parentheticals and % stripped) so one
definition serves every variant of a row -- e.g. "Gross Profit Margin (TTM)",
"Gross Profit Margin (5Y Avg)" and "Gross Profit Margin %" all resolve to the
same entry. An unknown label returns None and the template renders it plainly:
a missing tooltip is correct, a wrong one is not.
"""
from __future__ import annotations

import re

_PARENS = re.compile(r"\([^)]*\)")
_WS = re.compile(r"\s+")


def _norm(label: str) -> str:
    """Canonical lookup key: lowercase, no parenthetical qualifiers, no '%'."""
    s = _PARENS.sub(" ", label or "")
    s = s.replace("%", " ")
    return _WS.sub(" ", s).strip().lower()


# ---------------------------------------------------------------------------
# Definitions, keyed by _norm(label). Grouped to match the on-screen sections.
# ---------------------------------------------------------------------------
DEFS: dict[str, str] = {}


def _add(group: dict[str, str]) -> None:
    for k, v in group.items():
        DEFS[_norm(k)] = v


# ── Key statistics / market ────────────────────────────────────────────────
_add({
    "Market Capitalization":
        "The total value of all the company's shares (share price x shares outstanding). "
        "This is what the stock market says the whole company is worth.",
    "Enterprise Value":
        "Market cap plus debt, minus cash -- what it would really cost to buy the whole "
        "business outright. Better than market cap for comparing companies with very "
        "different debt loads.",
    "Beta":
        "How much the stock moves relative to the overall market. 1.0 = moves with the "
        "market, above 1.0 = swings harder than the market, below 1.0 = steadier.",
    "Shares Outstanding":
        "The total number of shares that exist. If this number keeps rising, existing "
        "shareholders are being diluted -- each share owns a smaller slice.",
    "Shares Float":
        "The shares actually available to trade, excluding those locked up by insiders "
        "and big strategic holders. A small float can mean sharper price swings.",
    "30-Day Average Volume":
        "Average shares traded per day over the last month. Higher volume means it's "
        "easier to get in and out without moving the price against you.",
    "Previous Close":
        "The share price at the end of the last completed trading session.",
    "52-Week High":
        "The highest price over the past year. Trading near it signals strength; it also "
        "marks a level where sellers have appeared before.",
    "52-Week Low":
        "The lowest price over the past year. Sitting near it signals weakness, though "
        "it can also mark a level where buyers have stepped in before.",
    "Next Earnings Date":
        "When the company next reports quarterly results. Expect above-average volatility "
        "around this date -- many traders avoid holding through it.",
    "Price":
        "The latest traded share price.",
})

# ── Income / cash-flow line items ──────────────────────────────────────────
_add({
    "Revenue":
        "Total sales before any costs -- the 'top line'. Growing revenue is the clearest "
        "sign the business is expanding.",
    "Cost of Revenue":
        "The direct cost of producing what was sold (materials, manufacturing, delivery). "
        "Revenue minus this is gross profit.",
    "Gross Profit":
        "Revenue minus the direct cost of producing it. What's left to cover R&D, sales, "
        "admin, and profit.",
    "Research & Development":
        "Spending on building future products. High R&D depresses today's profit to buy "
        "tomorrow's growth -- normal and healthy in tech and pharma.",
    "Selling, General & Admin":
        "Overheads: salespeople, marketing, executives, offices. If this grows faster than "
        "revenue for long, the business is losing operating discipline.",
    "Operating Income":
        "Profit from the core business after all operating costs, but before interest and "
        "tax. The cleanest read on whether the actual operation makes money.",
    "Interest Expense":
        "The cost of servicing debt. Compare it to operating income -- see Interest Coverage.",
    "Pretax Income":
        "Profit after all costs including interest, but before income tax.",
    "Income Tax Expense":
        "Tax charged on this period's profit.",
    "Net Income":
        "The 'bottom line' -- profit left after every cost, including interest and tax. "
        "This is what earnings per share is calculated from.",
    "EBITDA":
        "Earnings before interest, tax, depreciation and amortisation -- a rough proxy for "
        "cash profit from operations. Useful for comparison, but it ignores the real cost "
        "of replacing equipment.",
    "Diluted EPS":
        "Net income divided by all shares that would exist if every option and convertible "
        "were exercised. The conservative, standard measure of per-share profit.",
    "Diluted Shares":
        "The share count used for diluted EPS -- includes shares that could be issued from "
        "options and convertibles.",
    "Operating Cash Flow":
        "Actual cash generated by running the business. Harder to massage than net income, "
        "so it's the more trustworthy profit signal.",
    "Net Operating Cash Flow":
        "Actual cash generated by running the business. Harder to massage than net income, "
        "so it's the more trustworthy profit signal.",
    "Capital Expenditures":
        "Cash spent on long-lived assets -- factories, equipment, servers. Necessary to "
        "sustain and grow, but it's cash the owners don't get.",
    "Free Cash Flow":
        "Operating cash flow minus capital expenditure -- the cash genuinely left over for "
        "dividends, buybacks and debt paydown. Many investors treat this as the truest "
        "measure of profit.",
    "Investing Cash Flow":
        "Net cash spent on or received from investments and assets. Usually negative for a "
        "growing company (it's buying things).",
    "Financing Cash Flow":
        "Net cash from borrowing, repaying debt, issuing shares, buybacks and dividends. "
        "Persistently positive can mean the company is funding itself by raising money.",
    "Depreciation & Amortization":
        "The accounting charge that spreads the cost of long-lived assets over their useful "
        "life. A real cost, but no cash leaves the business this period.",
    "Stock-Based Compensation":
        "Employee pay issued as shares. No cash goes out, but it dilutes existing "
        "shareholders -- treat it as a real cost.",
    "Stock-Based Comp":
        "Employee pay issued as shares. No cash goes out, but it dilutes existing "
        "shareholders -- treat it as a real cost.",
    "Change in Working Capital":
        "Cash tied up in (or released from) inventory and unpaid customer bills. A big "
        "negative number means growth is consuming cash.",
    "Dividends Paid":
        "Cash paid out to shareholders during the period.",
    "Share Repurchases":
        "Cash spent buying back the company's own shares, which lifts earnings per share "
        "by shrinking the share count.",
})

# ── Balance sheet ──────────────────────────────────────────────────────────
_add({
    "Cash & Equivalents":
        "Cash on hand and holdings convertible to cash almost immediately.",
    "Cash, Equiv & ST Investments":
        "Cash plus investments that can be sold quickly. The company's firepower for "
        "surviving a downturn or funding growth without borrowing.",
    "Cash & ST Investments":
        "Cash plus investments that can be sold quickly -- the buffer available on short notice.",
    "Short-Term Investments":
        "Securities the company can sell within about a year -- effectively cash in waiting.",
    "Accounts Receivable":
        "Money owed by customers who have been billed but haven't paid yet.",
    "Net Accounts Receivable":
        "Money owed by customers, after allowing for bills expected to go unpaid. If this "
        "grows faster than revenue, customers are paying more slowly.",
    "Inventory":
        "Goods made or bought but not yet sold. Rising inventory against flat sales can "
        "signal weakening demand.",
    "Total Current Assets":
        "Assets expected to convert to cash within a year -- cash, receivables, inventory.",
    "Net Property, Plant & Equip":
        "Land, buildings and equipment, after accumulated depreciation.",
    "Goodwill":
        "The premium paid above fair value in past acquisitions. A large balance means "
        "growth was bought; it gets written down if those deals disappoint.",
    "Intangible Assets":
        "Non-physical assets like patents, brands and software.",
    "Total Assets":
        "Everything the company owns.",
    "Accounts Payable":
        "Money the company owes suppliers but hasn't paid yet.",
    "Total Current Liabilities":
        "Debts and bills due within a year.",
    "Long-Term Debt":
        "Borrowings due more than a year out.",
    "Total Debt":
        "All borrowings, short and long term. Judge it against cash and earnings, not on "
        "its own -- see Total Debt / EBITDA.",
    "Total Liabilities":
        "Everything the company owes.",
    "Retained Earnings":
        "Cumulative profit kept in the business rather than paid out since day one. A "
        "negative figure means lifetime losses exceed lifetime profits.",
    "Total Shareholders' Equity":
        "Assets minus liabilities -- the accounting value belonging to shareholders. Also "
        "called book value.",
    "Cash & Debt":
        "Cash and investments against total borrowings. You want to see cash comfortably "
        "covering debt, or at least the gap not widening.",
})

# ── Per-share ──────────────────────────────────────────────────────────────
_add({
    "Earnings per Share":
        "Net income divided by shares outstanding -- profit attributable to each share. "
        "The number the P/E ratio is built on.",
    "Revenue per Share":
        "Sales attributable to each share. Useful for companies not yet profitable, where "
        "earnings per share isn't meaningful.",
    "Book Value per Share":
        "Shareholders' equity divided by shares outstanding -- the accounting value behind "
        "each share. Compare against the share price via Price / Book.",
    "Net Cash per Share":
        "Cash minus debt, per share. If this is a large slice of the share price, you're "
        "partly buying a pile of cash.",
    "Op. Cash Flow / Net Income":
        "How much of reported profit shows up as actual cash. Consistently below 1.0 is a "
        "warning that earnings aren't backed by cash.",
})

# ── Growth ─────────────────────────────────────────────────────────────────
_add({
    "1-Year Revenue Growth":
        "Sales growth over the most recent year. The fastest read on whether demand is "
        "accelerating or cooling.",
    "5-Year Revenue Growth":
        "Average yearly sales growth over five years (compounded). Smooths out one-off "
        "good and bad years.",
    "10-Year Revenue Growth":
        "Average yearly sales growth over a decade (compounded) -- the long-run trend "
        "through at least one full economic cycle.",
    "1-Year Net Income Growth":
        "Profit growth over the most recent year.",
    "5-Year Net Income Growth":
        "Average yearly profit growth over five years (compounded). Ideally it keeps pace "
        "with or beats revenue growth, which means margins are improving.",
    "10-Year Net Income Growth":
        "Average yearly profit growth over a decade (compounded).",
    "1-Year Free Cash Flow Growth":
        "Growth in cash left over after capital spending, over the most recent year.",
    "5-Year Free Cash Flow Growth":
        "Average yearly growth in free cash flow over five years (compounded).",
    "Projected EPS Growth":
        "Analysts' consensus forecast for earnings-per-share growth. An estimate, not a "
        "fact -- treat it as sentiment about the future.",
})

# ── Financial strength ─────────────────────────────────────────────────────
_add({
    "Total Debt / Equity":
        "Borrowings measured against shareholders' equity. Below ~1.0 is generally "
        "comfortable; much higher means the business leans heavily on debt.",
    "Total Debt / EBITDA":
        "How many years of cash profit it would take to repay all debt. Under ~3x is "
        "usually comfortable; above ~4-5x is stretched.",
    "Interest Coverage":
        "How many times operating profit covers the interest bill. Above ~5x is safe; "
        "below ~2x means debt costs are eating the profit.",
    "Current Ratio":
        "Assets due within a year divided by bills due within a year. Above 1.0 means "
        "short-term obligations are covered; ~1.5-3x is typically healthy.",
    "Quick Ratio":
        "Like the current ratio but excluding inventory -- a stricter test, since inventory "
        "can be slow to sell. Above 1.0 is comfortable.",
    "Cash Ratio":
        "The strictest liquidity test: cash alone against bills due within a year. Above "
        "1.0 means the company could settle them from cash today.",
})

# ── Efficiency ─────────────────────────────────────────────────────────────
_add({
    "Cash Conversion Cycle":
        "Days between paying for inventory and collecting cash from customers. Lower is "
        "better -- negative means customers pay before suppliers do, which funds growth "
        "for free.",
    "Days Inventory Outstanding":
        "Average days stock sits before it's sold. Lower means inventory turns quickly; a "
        "rising figure can signal softening demand.",
    "Days Sales Outstanding":
        "Average days to collect payment after a sale. Rising means customers are paying "
        "more slowly, which can foreshadow bad debts.",
    "Days Payables Outstanding":
        "Average days the company takes to pay its suppliers. Higher preserves cash, but "
        "stretching too far can strain supplier relationships.",
    "Inventory Turnover":
        "How many times inventory is sold and replaced per year. Higher means leaner, "
        "faster-moving stock.",
    "Receivables Turnover":
        "How many times customer bills are collected per year. Higher means faster "
        "collection.",
    "Asset Turnover":
        "Revenue generated per dollar of assets. Higher means the asset base is being used "
        "more productively.",
    "Fixed Asset Turnover":
        "Revenue generated per dollar of property and equipment -- how hard the physical "
        "asset base is working.",
    "CapEx to Revenue":
        "Share of sales reinvested into physical assets. Low means the business is "
        "asset-light; a sharp rise means a heavy investment cycle has started.",
    "CapEx to Operating Cash Flow":
        "Share of operating cash consumed by capital spending. The rest is free cash flow, "
        "so a high figure leaves little for shareholders.",
    "CapEx to Operating Income":
        "Capital spending measured against operating profit -- another read on how "
        "investment-hungry the business is.",
})

# ── Profitability ──────────────────────────────────────────────────────────
_add({
    "Gross Profit Margin":
        "Gross profit as a share of revenue -- what's left after direct production costs. "
        "Higher and stable signals pricing power; a falling trend signals competition.",
    "Gross Margin":
        "Gross profit as a share of revenue -- what's left after direct production costs. "
        "Higher and stable signals pricing power.",
    "Operating Profit Margin":
        "Operating profit as a share of revenue -- how much of each sales dollar survives "
        "all operating costs. The core measure of operating efficiency.",
    "Operating Margin":
        "Operating profit as a share of revenue -- how much of each sales dollar survives "
        "all operating costs.",
    "Net Profit Margin":
        "Net profit as a share of revenue -- what finally reaches shareholders after every "
        "cost, including interest and tax.",
    "Net Margin":
        "Net profit as a share of revenue, after every cost including interest and tax.",
    "Operating Cash Flow Margin":
        "Operating cash flow as a share of revenue -- how much of each sales dollar arrives "
        "as actual cash.",
    "Free Cash Flow Margin":
        "Free cash flow as a share of revenue. Shows how efficiently sales convert into "
        "spendable cash after investment.",
    "Return on Assets":
        "Profit generated per dollar of assets. Measures how well management uses the "
        "resources it has; compare only within the same industry.",
    "Return on Equity":
        "Profit generated per dollar of shareholders' equity. Above ~15% sustained is "
        "strong -- but check debt, since heavy borrowing inflates it.",
    "Return on Invested Capital":
        "Profit generated per dollar of all capital employed, debt and equity together. "
        "The cleanest measure of business quality -- above the cost of capital (WACC) "
        "means the company is genuinely creating value.",
    "ROE": "Return on equity -- profit generated per dollar of shareholders' equity.",
    "ROA": "Return on assets -- profit generated per dollar of assets.",
    "Margins":
        "Gross, operating and net margin side by side. Watch the direction and the gaps: "
        "widening margins mean pricing power or cost discipline.",
    "Returns":
        "Return on equity and return on assets over time -- whether the company keeps "
        "converting its capital into profit.",
})

# ── Valuation ──────────────────────────────────────────────────────────────
_add({
    "Price / Earnings":
        "Share price divided by earnings per share -- the years of current profit you're "
        "paying for one share. Higher means the market expects more growth; compare within "
        "an industry, never across.",
    "Forward PE":
        "Price divided by analysts' forecast earnings for the year ahead. Lower than the "
        "trailing P/E implies profits are expected to grow.",
    "PEG Ratio":
        "P/E divided by the expected growth rate, so fast growers aren't automatically "
        "'expensive'. Around 1.0 is often treated as fair value.",
    "Price / Sales":
        "Share price against revenue per share. Useful for companies with little or no "
        "profit yet, where P/E doesn't work.",
    "Price / Book":
        "Share price against book value per share. Below 1.0 means the market values the "
        "company at less than its accounting net worth -- either a bargain or a warning.",
    "Price / Free Cash Flow":
        "Share price against free cash flow per share. Like P/E but based on actual cash, "
        "so it's harder to distort.",
    "Earnings Yield":
        "Earnings per share as a percentage of the price -- the P/E flipped over. Lets you "
        "compare a stock's profit yield against bond yields directly.",
    "Free Cash Flow Yield":
        "Free cash flow as a percentage of market cap. The cash return you'd get if all "
        "free cash were paid out to you. Higher is cheaper.",
    "WACC":
        "Weighted average cost of capital -- the blended annual return that lenders and "
        "shareholders require. An estimate. If ROIC sits above it, the business creates "
        "value; below it, it destroys value.",
    "Rule of 40":
        "Revenue growth percentage plus free-cash-flow margin percentage. A software-world "
        "yardstick: above 40 means growth and profitability are in a healthy balance.",
})

# ── Market performance ─────────────────────────────────────────────────────
_add({
    "1-Day Performance": "Share-price change over the last trading day.",
    "1-Month Performance": "Share-price change over the last month.",
    "Year to Date Performance": "Share-price change since the first trading day of this year.",
    "1-Year Performance":
        "Share-price change over the last year, excluding dividends. Compare against the "
        "S&P 500 over the same span to judge whether it actually outperformed.",
    "5-Year Performance": "Share-price change over five years, excluding dividends.",
    "10-Year Performance": "Share-price change over ten years, excluding dividends.",
})

# ── Dividends ──────────────────────────────────────────────────────────────
_add({
    "Dividend per Share": "Cash paid per share over the past year.",
    "Dividend Yield":
        "Annual dividend as a percentage of the share price -- the cash return at today's "
        "price. An unusually high yield often means the price has fallen for a reason.",
    "Payout Ratio":
        "Share of profit paid out as dividends. Below ~60% leaves room to keep paying "
        "through a bad year; above 100% means the dividend exceeds earnings.",
    "Ex-Dividend Date":
        "Buy on or after this date and you do NOT receive the upcoming dividend.",
    "Forward Dividend Yield":
        "Expected yield over the next year based on the current declared dividend rate.",
})

# ── Chart panel + strip headings ───────────────────────────────────────────
_add({
    "Revenue · Operating · Net Income":
        "The income statement's three headline lines together. Ideally all three rise, and "
        "profit rises at least as fast as revenue.",
    "Cash Flow":
        "Operating cash flow, free cash flow and net income together. When cash flow tracks "
        "or exceeds net income, reported profits are backed by real cash.",
    "Market Cap":
        "The total value of all the company's shares -- what the market says the whole "
        "company is worth.",
    "Revenue vs Net Accounts Receivable":
        "Sales against money customers still owe. Receivables growing faster than revenue "
        "means customers are paying more slowly -- an early warning sign.",
})


def describe(label: str) -> str | None:
    """The plain-English definition for a metric label, or None if we don't have
    one (the caller then renders the label with no tooltip)."""
    if not label:
        return None
    return DEFS.get(_norm(label))
