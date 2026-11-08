"""The seven Macro topics - Insights -> Macro (v4.243).

Source of the topics, the bands and the reading rules: the user's Beyond Insights "Macro
Insights" section 1 (Inflation, Fed Funds Rate, Exchange Rates, Unemployment, Economic Cycles,
Economic Growth, US National Debt). The page does not repeat the course: each topic is ONE
interactive history chart (the stored `MacroReading` series, 25 years where the source has
them) with the course's healthy band drawn on it, and a READING written from the data in the
course's own terms - where the latest point sits against the band, how the trend has moved,
the next release. The course text is condensed into a "how to read it" panel per topic.

Everything here is pure functions over the indicator store (``services/indicators.py``) plus
the Calendar's release feed (``services/calendars.economic_events``) for the next release.
The nightly refresh of the series is ``services/macro_refresh.py``.
"""
from __future__ import annotations

import datetime as _dt
import math
import re

from sqlalchemy.orm import Session

from ..models import MacroIndicator
from . import indicators as _ind
from . import macro_links as _links

# Colours: the chart's series. Amber for the headline series of a topic, sky for the second,
# violet / rose / emerald for the rest; a bar series colours itself by sign.
_AMBER, _SKY, _VIOLET, _ROSE, _EMERALD, _SLATE = ("#f59e0b", "#38bdf8", "#a78bfa", "#fb7185",
                                                  "#34d399", "#94a3b8")

# ---------------------------------------------------------------------------
# The topics. ``series`` lists indicator keys from indicators.SEED; ``band`` is the course's
# healthy range (drawn as a tinted strip); ``lines`` are fixed reference lines; ``release``
# matches the Calendar feed's release titles for "next release".
# ---------------------------------------------------------------------------
TOPICS: list[dict] = [
    {
        "key": "inflation", "label": "Inflation", "blurb": "CPI, PCE and PPI against the 2-4% zone",
        "cadence": "Monthly", "cadence_text": "Check monthly - on the CPI and PCE release days, and know the dates: the print moves prices.",
        "headline": "cpi",
        "series": [
            {"key": "cpi", "color": _AMBER, "on": True},
            {"key": "pce", "color": _SKY, "on": True},
            {"key": "ppi", "color": _VIOLET, "on": True},
            # The Fed Funds Rate as a TICK on each of the three charts, off until ticked (user,
            # 2026-11-07: "in the CPI PCE and PPI I want you to give a ticker to show the Fed Fund
            # Rate in the chart") - the course's point is that the Fed moves the rate to keep
            # inflation in the zone, so the two belong on one chart. Same % axis.
            {"key": "effr", "color": _ROSE, "on": False, "toggle": True, "width": 1,
             "label": "Fed Funds Rate (effective)"},
            # (v4.245 added SPY and QQQ ticks here on a left $ axis; v4.246 removed them - user:
            # "i dont need the SPY and QQQ tick box".)
        ],
        # Three charts, one per index (user, 2026-11-07: "i need the PPC and PCI to be shown in
        # separate chart"), each with the 2-4% zone.
        # The 2-4% zone is the course's band for the INFLATION RATE, which it measures with CPI and
        # PCE. It gives PPI no band ("more volatile than CPI or PCE"), so the PPI chart has none.
        "panels": [
            # "reading": each tab has its OWN reading (number, gauge, verdict, next release) - user,
            # 2026-11-07: "when i press CPI, PCE and PPI the top data did not change"
            {"key": "cpi", "label": "CPI - Consumer Price Index", "series": ["cpi", "effr"], "reading": "cpi",
             "note": "the consumer inflation rate, read against the 2-4% zone"},
            {"key": "pce", "label": "PCE - Personal Consumption Expenditures price index", "series": ["pce", "effr"], "reading": "pce",
             "note": "the Fed's preferred measure, read against the same 2-4% zone"},
            {"key": "ppi", "label": "PPI - Producer Price Index", "series": ["ppi", "effr"], "band": None, "reading": "ppi",
             "note": "producers' input costs: no target band - more volatile, and an early warning for CPI and PCE"},
        ],
        "band": {"lo": 2.0, "hi": 4.0, "label": "Goldilocks zone 2-4%"},
        "lines": [{"v": 0.0, "label": "0%", "color": _SLATE}],
        "release": r"^(Inflation Rate YoY|Core Inflation Rate YoY|PCE Price Index YoY|Core PCE Price Index YoY|PPI MoM)$",
        "text": {
            "definition": "A sustained rise in the average price of a basket of goods and services. A carton of milk at $5.00 last year and $5.50 this year is 10% inflation. Demand-pull inflation comes from buyers (stimulus, easy credit, low rates, government spending against limited supply); cost-push from sellers (tariffs, wages, oil and energy, supply-chain breaks, a weaker currency).",
            "covers": "Every part of the economy: interest rates, tax policy, government programmes, employment, business investment and consumer spending.",
            "why": "It eats investment returns (a 4% return under 5% inflation is a real loss), raises borrowing costs, and drives central-bank policy. Out of control, it can destroy a country.",
            "stocks": "High inflation raises input costs and cuts purchasing power, so profits and demand fall and prices follow. High-growth names (tech) suffer most; value, energy, consumer staples and oil & gas are affected least.",
            "measured": "A year-over-year percentage. CPI (Bureau of Labor Statistics, a consumer-expenditure survey) and PCE (Bureau of Economic Analysis, what businesses sell at) are the two most cited; PPI measures producers' input costs and is more volatile.",
            "interpret": "The inflation rate (CPI, and PCE) should sit in the 2-4% zone. At or below 0% the Fed cuts rates to spur demand through borrowing, investment and consumption; at or above 4% it hikes to rein demand in. PPI has no band: it is producers' input costs, more volatile, and tends to move before CPI and PCE do.",
            "takeaways": [
                "Inflation is the tendency of prices to rise over time.",
                "The Fed raises or lowers rates to keep it in the 2-4% zone.",
                "Hyperinflation means an economy in serious trouble.",
                "Monitor it monthly and watch the release dates.",
            ],
        },
    },
    {
        "key": "fed_funds", "label": "Fed Funds Rate", "blurb": "The FOMC's target range, the effective rate and the market's lean",
        "cadence": "Monthly", "cadence_text": "Check monthly: the target range and its probabilities before each FOMC meeting, and whether the EFFR is being held inside the range.",
        "headline": "effr",
        "series": [
            {"key": "fftr_upper", "color": _AMBER, "on": True, "width": 1},
            {"key": "fftr_lower", "color": _AMBER, "on": True, "width": 1},
            {"key": "effr", "color": _SKY, "on": True},
            {"key": "dgs2", "color": _VIOLET, "on": True, "label": "2-year Treasury yield (the market's lean)"},
        ],
        "band": None,
        "lines": [],
        "release": r"^(Fed Interest Rate Decision|FOMC .*|Fed Press Conference)$",
        "text": {
            "definition": "The rate at which banks lend their reserve balances to each other overnight. The FOMC sets a target RANGE (the FFTR, an upper and a lower bound) at its eight meetings a year; open market operations then keep the effective rate (EFFR) inside that range.",
            "covers": "Every other interest rate - mortgages, credit cards, bank loans, deposits, Treasury yields - and the value of the dollar, so household and business assets, commodities and foreign currencies.",
            "why": "A lower rate makes borrowing cheap, so consumers and companies spend, invest and buy assets such as stocks; that can overheat into inflation, which a higher rate then reins in.",
            "stocks": "A change, or the expectation of one, hits the stock market almost at once; the wider economy takes about a year to show it. Traders position before the announcement on the probabilities.",
            "measured": "The FFTR as a range (for example 3.50%-3.75%), moved in basis points: a 0.25% step is 25 bp. Announced after the FOMC meetings, usually in Jan/Mar, May, Jun/Jul, Sep, Nov and Dec.",
            "interpret": "The course reads the CME FedWatch probabilities, which have no free feed. The chart uses the 2-year Treasury yield instead: below the effective rate, the market expects cuts; above it, hikes; the gap says roughly how much.",
            "takeaways": [
                "The Fed sets the target range eight times a year.",
                "The effective rate steers every other rate and the dollar.",
                "The probabilities and the decision itself move stocks almost immediately.",
            ],
        },
    },
    {
        "key": "dollar", "label": "Exchange Rates", "blurb": "The dollar's trend, with its 52-week range and 200-day average",
        "cadence": "Monthly", "cadence_text": "Check monthly to read the trend; set alerts on the key support and resistance areas for a break.",
        "headline": "dxy",
        "series": [
            {"key": "dxy", "color": _AMBER, "on": True, "label": "US Dollar Index (DXY)"},
            {"key": "dtwexbgs", "color": _SKY, "on": False, "axis": "left"},
        ],
        "band": None,
        "lines": [],       # the 52-week high/low and the 200-day average are computed per read
        "release": None,
        "text": {
            "definition": "The value of one country's currency against another's - how many dollars one euro, pound or 100 yen buys. Most major currencies float on supply and demand (central banks step in when a move is too fast or too far); some are pegged, like the HKD held in 7.75-7.85 to the dollar.",
            "covers": "A weak currency makes exports cheap abroad (good for exporters); a strong one makes imports cheap (good for importers and companies that buy foreign goods) but prices exporters out.",
            "why": "A currency that appreciates makes local companies less competitive and their exports fall; one that depreciates does the opposite.",
            "stocks": "A strong dollar cuts the exports of US manufacturers and the overseas revenue of US international companies (MSFT, ADBE, CRM...) when it is converted back; their share prices feel it.",
            "measured": "DXY (1973, heavily weighted to the euro, weights rarely change) and the Fed's trade-weighted broad index DTWEXBGS (1998, weighted to the US's trading partners, refreshed yearly). Both rise when the dollar strengthens.",
            "interpret": "An uptrend means a stronger dollar. Draw support and resistance; a break through a key area signals strength or weakness, and the slope says how fast the move is. The chart marks the 52-week high and low and the 200-day average as the reference levels.",
            "takeaways": [
                "Weaker exchange rates favour exporters; stronger ones favour importers.",
                "Central banks intervene when a currency moves too fast or too far.",
                "A strong dollar lowers the overseas revenue of US international companies.",
                "Read DXY or DTWEXBGS monthly for the trend, support and resistance.",
            ],
        },
    },
    {
        "key": "unemployment", "label": "Unemployment", "blurb": "The jobless rate against the 4-6% range, and the Sahm rule",
        "cadence": "Monthly", "cadence_text": "Check monthly, on the jobs report (first Friday), to be sure the rate stays in range and the trend is healthy.",
        "headline": "unrate",
        "series": [
            {"key": "unrate", "color": _AMBER, "on": True},
            {"key": "sahm", "color": _ROSE, "on": False, "axis": "left"},
        ],
        "band": {"lo": 4.0, "hi": 6.0, "label": "Usual range 4-6%"},
        "lines": [],
        "release": r"^(Unemployment Rate|Non Farm Payrolls)$",
        "text": {
            "definition": "Someone actively looking for work who cannot find it. A high rate lowers a country's output and can lead to recession and unrest.",
            "covers": "Lower consumer spending, more bad debt, bigger government payouts, less tax collected, less output, more social unrest - the signs of an economy in distress.",
            "why": "Consumer consumption is most of US GDP, so fewer pay cheques means less demand, lower company revenue and a weaker GDP; it also steers the Fed's rate decisions.",
            "stocks": "Less income, less spending, lower revenue, lower prices - and the Fed's reaction on top.",
            "measured": "The Bureau of Labor Statistics' monthly household survey: unemployed people divided by the labour force, as a percentage.",
            "interpret": "As low as possible, usually 4-6%. Read it as a TREND: a sharp rise or a steady climb brings the Fed in to spur hiring. Very low readings can mean an overheated economy. The Sahm rule (the 3-month average minus its 12-month low) at 0.50 or more has marked every recession start since 1970.",
            "takeaways": [
                "The unemployment rate is a key read on the whole economy.",
                "High is distress; extremely low can be overheating.",
                "Watch the monthly chart for a healthy trend.",
                "The Fed moves rates to keep it in the 4-6% range.",
            ],
        },
    },
    {
        "key": "cycle", "label": "Economic Cycles", "blurb": "Expansion, peak, contraction, trough - and which sectors lead",
        "cadence": "Quarterly", "cadence_text": "Check quarterly for a change of trajectory, and know the projected path: institutions rotate sectors ahead of it.",
        "headline": "gdp_yoy",
        "series": [
            {"key": "gdp_yoy", "color": _AMBER, "on": True},
            {"key": "unrate", "color": _SKY, "on": True, "axis": "left"},
            {"key": "cpi", "color": _VIOLET, "on": False},
            {"key": "t10y2y", "color": _ROSE, "on": False, "axis": "left"},
        ],
        "band": None,
        "lines": [{"v": 0.0, "label": "0%", "color": _SLATE}],
        "release": None,
        "text": {
            "definition": "The predictable pattern an economy follows: expansion, peak, contraction, trough. Each stage is set by GDP, interest rates, unemployment and consumer spending.",
            "covers": "A country, a bloc (the EU) or the whole world - each in a different stage at a different time.",
            "why": "Knowing the current and the coming stage lets you position in the right sectors and avoid the wrong ones.",
            "stocks": "The market and the economy move together: a growing economy (GDP up) lifts stocks, a shrinking one sinks them. Institutions rotate into the sectors that lead the next stage before it arrives (the sector rotation model).",
            "measured": "The NBER keeps the official chronology, but it lags - it is for history. Third parties such as Fidelity publish current projections of the stage.",
            "interpret": "Fidelity's cycle chart has no free feed, so the stage here is ESTIMATED from the data: real GDP growth, the unemployment trend (the Sahm rule), inflation and the yield curve. Early cycle: financials, consumer discretionary, industrials, real estate. Mid: technology, communication. Late: energy, materials, staples, health care. Recession: utilities, staples, health care. Check the leaders on the Sector Rotation page.",
            "takeaways": [
                "Cycles apply to a country, a bloc or the globe.",
                "Knowing the stage puts you in the right sectors and out of the wrong ones.",
                "Four predictable stages, driven by GDP, rates, unemployment and spending.",
                "Check the trajectory quarterly.",
            ],
        },
    },
    {
        "key": "growth", "label": "Economic Growth", "blurb": "Real GDP each quarter, with the GDPNow projection",
        "cadence": "Quarterly", "cadence_text": "Check quarterly that the GDP line keeps rising; know the release dates, and watch the projections - they point to future health.",
        "headline": "gdp_qoq",
        "series": [
            {"key": "gdp_qoq", "style": "bars", "color": _EMERALD, "on": True},
            {"key": "gdpnow", "color": _SKY, "on": True},
            {"key": "gdp_yoy", "color": _AMBER, "on": False},
        ],
        "band": None,
        "lines": [{"v": 0.0, "label": "0%", "color": _SLATE}],
        "release": r"^GDP Growth Rate QoQ.*$",
        "text": {
            "definition": "The economy producing more, in inflation-adjusted terms, over time - the main economic goal of every country. Its parts: consumption, investment, government spending, and exports less imports.",
            "covers": "Consumption, investment, government expenditure, exports and imports.",
            "why": "Continuous growth brings more jobs and income, less poverty, more consumption and tax revenue, more public spending, investment, productivity and innovation.",
            "stocks": "Growth brings companies more profit and capital to invest; they hire and pay more; people buy more; that drives further growth - and share prices rise with it. It is the most-watched indicator.",
            "measured": "GDP by the expenditure approach: GDP = C + I + G + X (consumption, investment, government expenditure, net exports), presented as REAL GDP, adjusted for inflation.",
            "interpret": "GDP should rise quarter on quarter and year on year - a rising chart. One dip can be a one-off; two consecutive quarters of contraction is a technical recession; a year-on-year contraction is more alarming still. GDPNow is the Atlanta Fed's running estimate of the current quarter.",
            "takeaways": [
                "Continuous growth benefits the whole society; stock prices rise with it.",
                "Growth is measured as GDP, by GDP = C + I + G + X.",
                "Check the GDP chart quarterly for a healthy trend.",
            ],
        },
    },
    {
        "key": "debt", "label": "US National Debt", "blurb": "Total federal debt and its share of GDP - news-driven, not a monthly check",
        "cadence": "News only", "cadence_text": "No regular check needed - but be aware of major news on the debt (a ceiling fight, a ratings move).",
        "headline": "debt_gdp",
        "series": [
            {"key": "debt_gdp", "color": _AMBER, "on": True},
            {"key": "debt", "color": _SKY, "on": True, "axis": "left", "scale": 1e-6, "unit": "$tn",
             "label": "Federal debt ($ trillion)"},
        ],
        "band": None,
        "lines": [{"v": 100.0, "label": "100% of GDP", "color": _SLATE}],
        "release": None,
        "text": {
            "definition": "Everything the US government owes its creditors, mostly as Treasury bills, notes and bonds. Buying those is lending to the government: banks, pension funds, other nations, the Fed.",
            "covers": "The debt has risen steadily since 2001, and so has debt as a share of GDP. Debt in a country's own currency, while that currency is in demand, cannot bankrupt it - it can print to pay.",
            "why": "Excessive debt reduces public and private investment, narrows economic opportunity, raises the risk of a fiscal crisis and challenges national security.",
            "stocks": "More debt can cost the US its AAA rating; weaker confidence demands higher yields on government debt; corporate debt is priced off government debt, so company borrowing costs rise. At the crisis stage a country can default.",
            "measured": "Total public debt in dollars (the chart shows trillions) and debt as a percentage of GDP, both quarterly.",
            "interpret": "The trend is unsustainable over the long run, but nothing says today's level is overly worrying as long as the US keeps its military, economic and dollar supremacy. Japan has the highest debt-to-GDP ratio.",
            "takeaways": [
                "Too much national debt affects the US and the global economy.",
                "The debt is everything the government owes its creditors.",
                "Own-currency debt does not bankrupt a country while its currency is in demand.",
            ],
        },
    },
]
TOPIC_KEYS = [t["key"] for t in TOPICS]
_BY_KEY = {t["key"]: t for t in TOPICS}


def topic(key: str) -> dict:
    return _BY_KEY.get(key) or TOPICS[0]


def all_series_keys() -> list[str]:
    """Every indicator key a topic draws (the nightly refresh's list)."""
    out: list[str] = []
    for t in TOPICS:
        for s in t["series"]:
            if s["key"] not in out:
                out.append(s["key"])
    return out


# ---------------------------------------------------------------------------
# Reading the stored series
# ---------------------------------------------------------------------------
# Newest stored rows to load per read. A YoY needs a year of earlier rows; the dollar's
# 52-week range needs a year of daily closes; 800 covers both with margin.
_READ_ROWS = 800


def _inds(db: Session, keys) -> dict[str, MacroIndicator]:
    rows = db.query(MacroIndicator).filter(MacroIndicator.key.in_(list(keys))).all()
    return {r.key: r for r in rows}


def _series(db: Session, inds: dict, key: str, limit: int | None = _READ_ROWS):
    ind = inds.get(key)
    if ind is None:
        return []
    return _ind.points_for(db, ind, limit=limit)


def _near(points, when: _dt.datetime):
    return _ind._value_near(points, when)


def _fmt(v, unit: str | None = "%", digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    unit = unit or ""
    if unit == "%":
        return f"{v:.{digits}f}%"
    if unit == "pp":
        return f"{v:.2f} pp"
    if unit == "$tn":
        return f"${v:,.1f} tn"
    if unit == "bp":
        return f"{v:+.0f} bp"
    return f"{v:,.{digits}f}"


def _signed(v, unit="%", digits=1) -> str:
    if v is None:
        return "-"
    s = _fmt(abs(v), unit, digits)
    return ("+" if v >= 0 else "-") + s


def _date(t: _dt.datetime | None) -> str | None:
    return t.date().isoformat() if t else None


def _month(t: _dt.datetime | None) -> str:
    return t.strftime("%b %Y") if t else "-"


def _quarter(t: _dt.datetime | None) -> str:
    return f"Q{(t.month - 1) // 3 + 1} {t.year}" if t else "-"


def _streak(points, rising: bool = True) -> int:
    """How many consecutive latest steps moved in one direction (a flat step ends it)."""
    n = 0
    for i in range(len(points) - 1, 0, -1):
        d = points[i][1] - points[i - 1][1]
        if (d > 1e-9) if rising else (d < -1e-9):
            n += 1
        else:
            break
    return n


def _band_verdict(v: float, lo: float, hi: float, *, below_word: str, above_word: str,
                  in_word: str = "In the zone") -> tuple[str, str]:
    """(tone, word) for a value against a band: ok inside, watch just outside, bad far outside."""
    if lo <= v <= hi:
        return "ok", in_word
    if v < lo:
        return ("bad" if v <= lo - (hi - lo) else "watch"), below_word
    return ("bad" if v >= hi + (hi - lo) / 2 else "watch"), above_word


def _blank(t: dict, why: str) -> dict:
    return {"tone": "none", "word": "No data yet", "sentence": why, "headline": None, "lines": [],
            "flags": [], "trend": {}}


# The three price indexes of the Inflation topic: (name, release-title regex, has the 2-4% band).
# PPI has no band - the course gives none ("more volatile than CPI or PCE").
_PRICE_INDEX = {
    "cpi": ("CPI", r"^(Inflation Rate YoY|Core Inflation Rate YoY)$", True),
    "pce": ("PCE", r"^(PCE Price Index YoY|Core PCE Price Index YoY|PCE Price Index MoM)$", True),
    "ppi": ("PPI", r"^(PPI MoM|PPI YoY|Core PPI MoM)$", False),
}


def read_price_index(db: Session, inds: dict, key: str = "cpi") -> dict:
    """One price index (CPI, PCE or PPI) against the course's rules: CPI and PCE against the 2-4%
    zone with the Fed's lean; PPI with no band, read as the trend of producers' input costs."""
    name, _pat, banded = _PRICE_INDEX[key]
    s = _series(db, inds, key)
    if not s:
        return _blank(_BY_KEY["inflation"], f"The {name} series has not been fetched yet.")
    t, v = s[-1]
    v3, v12 = _near(s, t - _dt.timedelta(days=91)), _near(s, t - _dt.timedelta(days=365))
    up, down = _streak(s, True), _streak(s, False)
    flags = []
    if banded:
        tone, word = _band_verdict(v, 2.0, 4.0, below_word="Below the zone", above_word="Above the zone",
                                   in_word="In the Goldilocks zone")
        if v <= 0:
            tone, word = "bad", "Deflation"
        lean = ("the Fed leans to hiking to rein demand in" if v >= 4.0
                else "the Fed leans to cutting to spur demand" if v <= 0
                else "below the 2-4% target; room to ease" if v < 2.0
                else "inside the 2-4% target; no pressure on the Fed from prices")
        sentence = f"{name} {_fmt(v)} year over year in {_month(t)} - {lean}."
        if up >= 3:
            flags.append({"tone": "watch" if v < 4 else "bad", "text": f"Rising for {up} months in a row"})
        elif down >= 3:
            flags.append({"tone": "ok", "text": f"Falling for {down} months in a row"})
    else:
        if up >= 3:
            tone, word = ("bad" if v >= 4 else "watch"), f"Rising {up} months"
        elif down >= 3:
            tone, word = "ok", f"Falling {down} months"
        else:
            tone, word = ("watch" if v >= 4 else "info"), ("Elevated" if v >= 4 else "Steady")
        sentence = (f"PPI {_fmt(v)} year over year in {_month(t)} - producers' input costs, no target band; "
                    + ("rising input costs tend to reach consumer prices later." if up >= 2 or v >= 4
                       else "easing input costs point to softer consumer prices ahead." if down >= 2
                       else "it tends to move before CPI and PCE do."))
        if v >= 4:
            flags.append({"tone": "watch", "text": "Above 4% - pressure building in the pipeline"})
        if v < 0:
            flags.append({"tone": "info", "text": "Producer prices falling year over year"})
    lines = []
    for other, (oname, _p, _b) in _PRICE_INDEX.items():
        if other == key:
            continue
        o = _series(db, inds, other)
        if o:
            lines.append(f"{oname} {_fmt(o[-1][1])} ({_month(o[-1][0])})")
    return {
        "tone": tone, "word": word, "sentence": sentence,
        "headline": {"label": f"{name} (YoY)", "text": _fmt(v), "as_of": _date(t), "when": _month(t)},
        "lines": lines, "flags": flags,
        "trend": {"3m": _signed(v - v3) if v3 is not None else None,
                  "12m": _signed(v - v12) if v12 is not None else None, "unit": "pp"},
    }


def read_inflation(db: Session, inds: dict) -> dict:
    return read_price_index(db, inds, "cpi")


def read_fed_funds(db: Session, inds: dict) -> dict:
    up, lo, ef, y2 = (_series(db, inds, k) for k in ("fftr_upper", "fftr_lower", "effr", "dgs2"))
    if not ef or not up:
        return _blank(_BY_KEY["fed_funds"], "The fed funds series have not been fetched yet.")
    t, e = ef[-1]
    hi_v, lo_v = up[-1][1], (lo[-1][1] if lo else None)
    inside = lo_v is not None and lo_v - 1e-9 <= e <= hi_v + 1e-9
    rng = f"{_fmt(lo_v, '%', 2)}-{_fmt(hi_v, '%', 2)}" if lo_v is not None else _fmt(hi_v, "%", 2)
    lines = [f"Target range {rng}", f"EFFR {_fmt(e, '%', 2)} - {'inside' if inside else 'OUTSIDE'} the range"]
    flags = []
    tone, word = "ok", "Held in range"
    if not inside:
        tone, word = "watch", "EFFR outside the range"
        flags.append({"tone": "watch", "text": "The effective rate is outside the FOMC's target range"})
    lean = ""
    if y2:
        gap = (y2[-1][1] - e) * 100.0
        if gap <= -25:
            lean = f"the 2-year yield is {abs(gap):.0f} bp below the EFFR - the market leans to CUTS"
        elif gap >= 25:
            lean = f"the 2-year yield is {gap:.0f} bp above the EFFR - the market leans to HIKES"
        else:
            lean = f"the 2-year yield is within {abs(gap):.0f} bp of the EFFR - the market leans to a HOLD"
        lines.append(f"2-year yield {_fmt(y2[-1][1], '%', 2)}")
    # the last move of the range
    moved_at, moved_by = None, None
    for i in range(len(up) - 1, 0, -1):
        d = up[i][1] - up[i - 1][1]
        if abs(d) > 1e-9:
            moved_at, moved_by = up[i][0], d * 100.0
            break
    if moved_at:
        lines.append(f"Last change {_fmt(moved_by, 'bp')} on {moved_at.date().isoformat()}")
    sentence = f"The target range is {rng}; the EFFR sits at {_fmt(e, '%', 2)}" + (f"; {lean}" if lean else "") + "."
    return {
        "tone": tone, "word": word, "sentence": sentence,
        "headline": {"label": "Target range", "text": rng, "as_of": _date(t), "when": t.date().isoformat()},
        "lines": lines, "flags": flags,
        "trend": {"3m": _signed((e - (_near(ef, t - _dt.timedelta(days=91)) or e)) * 100, "bp", 0),
                  "12m": _signed((e - (_near(ef, t - _dt.timedelta(days=365)) or e)) * 100, "bp", 0),
                  "unit": "bp"},
    }


def read_dollar(db: Session, inds: dict) -> dict:
    dxy = _series(db, inds, "dxy")
    name = "DXY"
    if not dxy:
        # v4.247: DXY (Yahoo) missing but the Fed's broad index (FRED) stored - read that
        # instead of "No data yet" (both rise when the dollar strengthens; the course names both).
        dxy = _series(db, inds, "dtwexbgs")
        name = "Broad dollar index"
    if not dxy:
        return _blank(_BY_KEY["dollar"], "The dollar index has not been fetched yet.")
    t, v = dxy[-1]
    year = [p for p in dxy if p[0] >= t - _dt.timedelta(days=365)]
    hi = max(p[1] for p in year)
    lo = min(p[1] for p in year)
    last200 = [p[1] for p in dxy[-200:]]
    ma200 = sum(last200) / len(last200) if len(last200) >= 100 else None
    v3, v12 = _near(dxy, t - _dt.timedelta(days=91)), _near(dxy, t - _dt.timedelta(days=365))
    pct3 = (v / v3 - 1) * 100 if v3 else None
    pct12 = (v / v12 - 1) * 100 if v12 else None
    above = ma200 is not None and v > ma200
    if pct3 is not None and pct3 >= 2:
        tone, word = "watch", "Strengthening"
    elif pct3 is not None and pct3 <= -2:
        tone, word = "watch", "Weakening"
    else:
        tone, word = "ok", "Range-bound"
    flags = []
    if v >= hi - 1e-9:
        flags.append({"tone": "watch", "text": "At its 52-week high - a break higher is dollar strength"})
    elif v <= lo + 1e-9:
        flags.append({"tone": "watch", "text": "At its 52-week low - a break lower is dollar weakness"})
    where = ("above" if above else "below") + " its 200-day average" if ma200 is not None else ""
    sentence = (f"{name} {v:,.2f}, {_signed(pct3, '%', 1) if pct3 is not None else '-'} over 3 months"
                + (f", {where}" if where else "") + f"; the 52-week range is {lo:,.2f}-{hi:,.2f}.")
    lines = [f"52-week high {hi:,.2f} (resistance)", f"52-week low {lo:,.2f} (support)"]
    if ma200 is not None:
        lines.append(f"200-day average {ma200:,.2f}")
    if name == "DXY":
        bro = _series(db, inds, "dtwexbgs")
        if bro:
            lines.append(f"Trade-weighted broad index {bro[-1][1]:,.1f} ({bro[-1][0].date().isoformat()})")
    else:
        lines.append("DXY has not been fetched yet - this reading uses the Fed's trade-weighted broad index.")
    return {
        "tone": tone, "word": word, "sentence": sentence,
        "headline": {"label": name, "text": f"{v:,.2f}", "as_of": _date(t), "when": t.date().isoformat()},
        "lines": lines, "flags": flags,
        "trend": {"3m": _signed(pct3, "%", 1) if pct3 is not None else None,
                  "12m": _signed(pct12, "%", 1) if pct12 is not None else None, "unit": "%"},
        "levels": [{"v": hi, "label": "52w high", "color": _ROSE}, {"v": lo, "label": "52w low", "color": _EMERALD}]
                  + ([{"v": ma200, "label": "200-day", "color": _SLATE}] if ma200 is not None else []),
    }


def read_unemployment(db: Session, inds: dict) -> dict:
    u = _series(db, inds, "unrate")
    if not u:
        return _blank(_BY_KEY["unemployment"], "The unemployment series has not been fetched yet.")
    t, v = u[-1]
    tone, word = _band_verdict(v, 4.0, 6.0, below_word="Very low - overheating risk",
                               above_word="Above the range - distress", in_word="In the usual range")
    flags = []
    up = _streak(u, True)
    if up >= 3:
        flags.append({"tone": "bad", "text": f"Rising for {up} months in a row - the Fed steps in on a steady climb"})
    v3, v12 = _near(u, t - _dt.timedelta(days=91)), _near(u, t - _dt.timedelta(days=365))
    if v12 is not None and v - v12 >= 0.5 and up < 3:
        flags.append({"tone": "watch", "text": f"Up {_fmt(v - v12, 'pp')} over 12 months"})
    lines = []
    sahm = _series(db, inds, "sahm")
    if sahm:
        sv = sahm[-1][1]
        lines.append(f"Sahm rule {sv:.2f} ({_month(sahm[-1][0])})" + (" - TRIGGERED" if sv >= 0.5 else ""))
        if sv >= 0.5:
            flags.append({"tone": "bad", "text": "Sahm rule at 0.50 or more - a recession-start signal"})
            if tone == "ok":
                tone, word = "watch", "Rising - Sahm rule triggered"
    sentence = f"Unemployment {_fmt(v)} in {_month(t)} - {word.lower()}" + (
        f"; {_signed(v - v12, 'pp')} over 12 months." if v12 is not None else ".")
    return {
        "tone": tone, "word": word, "sentence": sentence,
        "headline": {"label": "Unemployment rate", "text": _fmt(v), "as_of": _date(t), "when": _month(t)},
        "lines": lines, "flags": flags,
        "trend": {"3m": _signed(v - v3, "pp", 1) if v3 is not None else None,
                  "12m": _signed(v - v12, "pp", 1) if v12 is not None else None, "unit": "pp"},
    }


_STAGE_SECTORS = {
    "Expansion": ("Technology, Communication services, Industrials", "mid-cycle leaders"),
    "Early recovery": ("Financials, Consumer discretionary, Industrials, Real estate", "early-cycle leaders"),
    "Peak": ("Energy, Materials, Consumer staples, Health care", "late-cycle leaders"),
    "Contraction": ("Utilities, Consumer staples, Health care", "recession defensives"),
}


def cycle_stage(gdp_qoq, unrate, cpi, curve) -> dict:
    """An ESTIMATE of the stage from the data (the course reads Fidelity's chart, which has no
    free feed). Returns {stage, reasons[], sectors, sectors_note}."""
    reasons = []
    if not gdp_qoq:
        return {"stage": None, "reasons": ["No GDP data yet."], "sectors": None, "sectors_note": None}
    g1 = gdp_qoq[-1][1]
    g0 = gdp_qoq[-2][1] if len(gdp_qoq) > 1 else None
    u_rise = _streak(unrate, True) if unrate else 0
    u12 = (unrate[-1][1] - (_near(unrate, unrate[-1][0] - _dt.timedelta(days=365)) or unrate[-1][1])) if unrate else 0.0
    hot = bool(cpi) and cpi[-1][1] >= 4.0
    inverted = bool(curve) and curve[-1][1] < 0
    if g1 <= 0 and g0 is not None and g0 <= 0:
        stage = "Contraction"
        reasons.append(f"Two consecutive quarters of falling real GDP ({_quarter(gdp_qoq[-2][0])} {_fmt(g0)}, "
                       f"{_quarter(gdp_qoq[-1][0])} {_fmt(g1)}) - a technical recession.")
    elif g1 <= 0:
        stage = "Peak"
        reasons.append(f"Real GDP fell {_fmt(g1)} in {_quarter(gdp_qoq[-1][0])} after growing the quarter before.")
    elif g0 is not None and g0 <= 0:
        stage = "Early recovery"
        reasons.append(f"Real GDP turned up ({_fmt(g1)}) after a contraction in {_quarter(gdp_qoq[-2][0])}.")
    elif u_rise >= 3 or u12 >= 0.5 or hot:
        stage = "Peak"
        if u_rise >= 3:
            reasons.append(f"Unemployment has risen {u_rise} months in a row.")
        if u12 >= 0.5:
            reasons.append(f"Unemployment is up {_fmt(u12, 'pp')} over a year.")
        if hot:
            reasons.append(f"Inflation at {_fmt(cpi[-1][1])} is above the 4% line - late cycle.")
    else:
        stage = "Expansion"
        reasons.append(f"Real GDP growing ({_fmt(g1)} in {_quarter(gdp_qoq[-1][0])}) with unemployment steady or falling.")
    if inverted:
        reasons.append(f"The 2s10s curve is inverted ({_fmt(curve[-1][1], '%', 2)}) - a recession lead signal.")
    sec, note = _STAGE_SECTORS[stage]
    return {"stage": stage, "reasons": reasons, "sectors": sec, "sectors_note": note}


def read_cycle(db: Session, inds: dict) -> dict:
    g = _series(db, inds, "gdp_qoq")
    if not g:
        return _blank(_BY_KEY["cycle"], "The GDP series has not been fetched yet.")
    st = cycle_stage(g, _series(db, inds, "unrate"), _series(db, inds, "cpi"), _series(db, inds, "t10y2y"))
    tone = {"Expansion": "ok", "Early recovery": "ok", "Peak": "watch", "Contraction": "bad"}.get(st["stage"], "none")
    gy = _series(db, inds, "gdp_yoy")
    lines = list(st["reasons"])
    if st["sectors"]:
        lines.append(f"Sectors that lead this stage ({st['sectors_note']}): {st['sectors']}")
    return {
        "tone": tone, "word": f"{st['stage']} (estimated)",
        "sentence": f"The data reads as {st['stage'].lower()}: " + " ".join(st["reasons"][:2]),
        "headline": {"label": "Real GDP (YoY)", "text": _fmt(gy[-1][1]) if gy else "-",
                     "as_of": _date(gy[-1][0]) if gy else None, "when": _quarter(gy[-1][0]) if gy else "-"},
        "lines": lines, "flags": [],
        "trend": {}, "stage": st["stage"], "sectors": st["sectors"],
    }


def read_growth(db: Session, inds: dict) -> dict:
    g = _series(db, inds, "gdp_qoq")
    if not g:
        return _blank(_BY_KEY["growth"], "The GDP series has not been fetched yet.")
    t, v = g[-1]
    prev = g[-2][1] if len(g) > 1 else None
    flags = []
    if v <= 0 and prev is not None and prev <= 0:
        tone, word = "bad", "Technical recession"
        flags.append({"tone": "bad", "text": "Two consecutive quarters of contraction"})
    elif v <= 0:
        tone, word = "watch", "A quarter of contraction"
        flags.append({"tone": "watch", "text": "One negative quarter - a dip can be a one-off; a second is a recession"})
    elif v < 1.0:
        tone, word = "watch", "Barely growing"
    else:
        tone, word = "ok", "Growing"
    gy = _series(db, inds, "gdp_yoy")
    if gy and gy[-1][1] < 0:
        flags.append({"tone": "bad", "text": f"Year-on-year contraction ({_fmt(gy[-1][1])}) - more alarming than a quarter"})
        tone = "bad"
    lines = [f"Previous quarter {_fmt(prev)}" if prev is not None else "Previous quarter -"]
    if gy:
        lines.append(f"Year over year {_fmt(gy[-1][1])}")
    now = _series(db, inds, "gdpnow")
    if now:
        nt, nv = now[-1]
        lines.append(f"GDPNow projection for the current quarter {_fmt(nv)} (as of {nt.date().isoformat()})")
    sentence = f"Real GDP {_signed(v)} annualised in {_quarter(t)} - {word.lower()}" + (
        f"; GDPNow projects {_fmt(now[-1][1])} for the quarter in progress." if now else ".")
    return {
        "tone": tone, "word": word, "sentence": sentence,
        "headline": {"label": "Real GDP, q/q annualised", "text": _signed(v), "as_of": _date(t), "when": _quarter(t)},
        "lines": lines, "flags": flags,
        "trend": {"prev": _fmt(prev) if prev is not None else None,
                  "yoy": _fmt(gy[-1][1]) if gy else None, "unit": "%"},
    }


def read_debt(db: Session, inds: dict) -> dict:
    r = _series(db, inds, "debt_gdp")
    d = _series(db, inds, "debt")
    if not r and not d:
        return _blank(_BY_KEY["debt"], "The debt series have not been fetched yet.")
    lines, flags = [], []
    if d:
        dt_, dv = d[-1]
        d12 = _near(d, dt_ - _dt.timedelta(days=365))
        lines.append(f"Total federal debt {_fmt(dv * 1e-6, '$tn')} ({_quarter(dt_)})"
                     + (f", {_signed((dv / d12 - 1) * 100, '%', 1)} over a year" if d12 else ""))
    tone, word = "info", "Watch the news"
    if r:
        t, v = r[-1]
        r12 = _near(r, t - _dt.timedelta(days=365))
        head = {"label": "Debt as % of GDP", "text": _fmt(v, "%", 0), "as_of": _date(t), "when": _quarter(t)}
        trend = {"12m": _signed(v - r12, "pp", 1) if r12 is not None else None, "unit": "pp"}
        sentence = f"Federal debt is {_fmt(v, '%', 0)} of GDP ({_quarter(t)})" + (
            f", {_signed(v - r12, 'pp', 1)} over a year" if r12 is not None else "") + \
            ". The course: unsustainable over the long run, not overly worrying today - no regular check, only the news."
    else:
        t, v = d[-1]
        head = {"label": "Federal debt", "text": _fmt(v * 1e-6, "$tn"), "as_of": _date(t), "when": _quarter(t)}
        trend = {}
        sentence = lines[0] + "."
    return {"tone": tone, "word": word, "sentence": sentence, "headline": head, "lines": lines,
            "flags": flags, "trend": trend}


_READERS = {
    "inflation": read_inflation, "fed_funds": read_fed_funds, "dollar": read_dollar,
    "unemployment": read_unemployment, "cycle": read_cycle, "growth": read_growth, "debt": read_debt,
}

# ---------------------------------------------------------------------------
# The glance cards (v4.243, user: "the page looks too wordy, can you make it more graphical"):
# the course's cause-and-effect chain, the stock winners and losers, the rules to watch for.
# Short labels only; the full text stays in TOPICS[...]["text"], folded away on the page.
# ---------------------------------------------------------------------------
GLANCE = {
    "inflation": {
        "flow": ["Prices rise", "Input costs up, purchasing power down", "Profits and demand fall", "Stock prices fall"],
        "good": ["Value", "Energy", "Consumer staples", "Oil & gas"],
        "bad": ["High-growth", "Tech"],
        "watch": ["Above 4%: the Fed hikes", "At or below 0%: the Fed cuts", "Know the CPI and PCE release dates"],
    },
    "fed_funds": {
        "flow": ["Rate cut", "Borrowing gets cheaper", "Spending and investment rise", "Stocks rise (inflation risk later)"],
        "good": ["Lower rates: borrowers, asset prices"],
        "bad": ["Higher rates: borrowing costs, valuations"],
        "watch": ["The decision hits stocks at once; the economy about a year later",
                  "Position on the probabilities before the meeting", "The EFFR should stay inside the range"],
    },
    "dollar": {
        "flow": ["Dollar strengthens", "US exports less competitive", "Overseas revenue converts to fewer dollars", "Exporters and multinationals fall"],
        "good": ["Strong USD: importers"],
        "bad": ["Strong USD: exporters", "Multinationals (MSFT, ADBE, CRM)"],
        "watch": ["A break of support or resistance = strength or weakness", "The slope says how fast",
                  "Central banks step in on a move that is too fast or too far"],
    },
    "unemployment": {
        "flow": ["Jobs are lost", "Income and spending fall", "Company revenue falls", "GDP and stocks fall"],
        "good": ["Low and steady: consumer demand"],
        "bad": ["Rising: discretionary, cyclicals"],
        "watch": ["4-6% is usual; read the trend", "A sharp rise or a steady climb brings the Fed in",
                  "Sahm rule at 0.50 or more = a recession start"],
    },
    "cycle": {
        "flow": ["Expansion", "Peak", "Contraction", "Trough"],
        "good": [], "bad": [],          # filled per stage by the reading
        "watch": ["Check quarterly for a change of trajectory", "Institutions rotate sectors ahead of the stage",
                  "The stage here is estimated from the data, not Fidelity's chart"],
    },
    "growth": {
        "flow": ["Economy grows", "Profits and hiring rise", "Income and spending rise", "Stocks rise"],
        "good": ["Growing: cyclicals, discretionary"],
        "bad": ["Contracting: all but defensives"],
        "watch": ["Two negative quarters = technical recession", "A year-on-year contraction is more alarming",
                  "Watch the projection (GDPNow)"],
    },
    "debt": {
        "flow": ["Debt keeps rising", "Rating risk, higher yields", "Corporate borrowing costs rise", "Valuations pressured"],
        "good": [],
        "bad": ["A downgrade: long bonds, rate-sensitive names"],
        "watch": ["News only: ceiling fights, rating moves", "Own-currency debt is not bankruptcy while the dollar is in demand"],
    },
}
_STAGE_GOOD_BAD = {
    "Expansion": (["Technology", "Communication", "Industrials"], ["Utilities", "Consumer staples"]),
    "Early recovery": (["Financials", "Consumer discretionary", "Industrials", "Real estate"], ["Utilities", "Health care"]),
    "Peak": (["Energy", "Materials", "Consumer staples", "Health care"], ["Technology", "Consumer discretionary"]),
    "Contraction": (["Utilities", "Consumer staples", "Health care"], ["Industrials", "Technology", "Discretionary"]),
}


def glance(key: str, r: dict) -> dict:
    g = dict(GLANCE.get(key) or {"flow": [], "good": [], "bad": [], "watch": []})
    if key == "cycle" and r.get("stage") in _STAGE_GOOD_BAD:
        g["good"], g["bad"] = _STAGE_GOOD_BAD[r["stage"]]
        g["active"] = r["stage"] if r["stage"] != "Early recovery" else "Trough"
    return g


def _gauge_index(db: Session, inds: dict, key: str) -> dict | None:
    """The gauge for one price index: CPI and PCE on the 2-4% zone (the other banded index as a
    tick), PPI on a wider bar with only the 0% line."""
    s = _series(db, inds, key)
    if not s:
        return None
    v = s[-1][1]
    if _PRICE_INDEX[key][2]:
        other = "pce" if key == "cpi" else "cpi"
        o = _series(db, inds, other)
        return {"min": min(-2.0, v - 1), "max": max(8.0, v + 1), "value": v, "lo": 2.0, "hi": 4.0,
                "line": 0.0, "unit": "%", "left": "deflation", "right": "overheating",
                "marks": [{"v": o[-1][1], "label": _PRICE_INDEX[other][0]}] if o else []}
    cpi = _series(db, inds, "cpi")
    return {"min": min(-6.0, v - 2), "max": max(12.0, v + 2), "value": v, "lo": None, "hi": None,
            "line": 0.0, "unit": "%", "left": "falling input costs", "right": "rising input costs",
            "marks": [{"v": cpi[-1][1], "label": "CPI"}] if cpi else []}


def _gauge(db: Session, inds: dict, key: str, r: dict) -> dict | None:
    """A horizontal gauge for the headline: {min, max, value, lo, hi, line, marks[], left, right}
    - the zone (lo..hi) is shaded, ``line`` is a single reference level, ``marks`` are small
    ticks (PCE beside CPI, GDPNow beside GDP, the 200-day beside DXY). The cycle topic gets a
    stage track instead: {kind: 'stages', stages[], active}."""
    try:
        if key == "inflation":
            return _gauge_index(db, inds, "cpi")
        if key == "fed_funds":
            up, lo, ef, y2 = (_series(db, inds, k) for k in ("fftr_upper", "fftr_lower", "effr", "dgs2"))
            if not ef or not up:
                return None
            hi_v, lo_v, e = up[-1][1], (lo[-1][1] if lo else up[-1][1] - 0.25), ef[-1][1]
            return {"min": 0.0, "max": max(6.0, hi_v + 1.5), "value": e, "lo": lo_v, "hi": hi_v, "unit": "%",
                    "left": "0%", "right": "", "zone_label": "target range",
                    "marks": [{"v": y2[-1][1], "label": "2y yield"}] if y2 else []}
        if key == "dollar":
            lv = {l["label"]: l["v"] for l in (r.get("levels") or [])}
            h = r.get("headline")
            if not h or "52w high" not in lv:
                return None
            v = float(h["text"].replace(",", ""))
            lo, hi = lv["52w low"], lv["52w high"]
            pad = (hi - lo) * 0.15 or 1.0
            return {"min": lo - pad, "max": hi + pad, "value": v, "lo": None, "hi": None, "unit": "",
                    "left": f"52w low {lo:,.1f}", "right": f"52w high {hi:,.1f}", "band_lo": lo, "band_hi": hi,
                    "marks": [{"v": lv["200-day"], "label": "200d"}] if "200-day" in lv else []}
        if key == "unemployment":
            u = _series(db, inds, "unrate")
            if not u:
                return None
            v = u[-1][1]
            return {"min": 2.0, "max": max(10.0, v + 1), "value": v, "lo": 4.0, "hi": 6.0, "unit": "%",
                    "left": "overheating", "right": "distress", "marks": []}
        if key == "cycle":
            st = r.get("stage")
            return {"kind": "stages", "stages": ["Expansion", "Peak", "Contraction", "Trough"],
                    "active": ("Trough" if st == "Early recovery" else st)}
        if key == "growth":
            g = _series(db, inds, "gdp_qoq")
            now = _series(db, inds, "gdpnow")
            if not g:
                return None
            v = g[-1][1]
            return {"min": min(-4.0, v - 1), "max": max(8.0, v + 1), "value": v, "lo": 0.0, "hi": max(8.0, v + 1),
                    "line": 0.0, "unit": "%", "left": "contraction", "right": "growth", "zone_label": "growing",
                    "marks": [{"v": now[-1][1], "label": "GDPNow"}] if now else []}
        if key == "debt":
            d = _series(db, inds, "debt_gdp")
            if not d:
                return None
            v = d[-1][1]
            return {"min": 0.0, "max": max(150.0, v + 10), "value": v, "lo": None, "hi": None, "line": 100.0,
                    "unit": "%", "left": "0% of GDP", "right": "", "marks": []}
    except Exception:  # noqa: BLE001
        return None
    return None


def reading(db: Session, key: str) -> dict:
    """The topic's reading from the stored series: tone (ok / watch / bad / info / none), a
    word, a sentence, the headline figure, extra lines, flags, the trend, a gauge and the
    glance-card content."""
    t = topic(key)
    keys = set(all_series_keys()) | {"cpi", "unrate", "t10y2y", "dgs2", "dxy"}
    inds = _inds(db, keys)
    try:
        r = _READERS[t["key"]](db, inds)
    except Exception as exc:  # noqa: BLE001 - a broken series must not blank the page
        r = _blank(t, f"The reading could not be computed ({type(exc).__name__}).")
    r["gauge"] = _gauge(db, inds, t["key"], r)
    r["glance"] = glance(t["key"], r)
    return r


# ---------------------------------------------------------------------------
# One icon per topic (v4.248, user: "can you add some icon ... for each item in macro?"):
# inline SVG line icons (24x24, stroke = currentColor, no external file), with the topic's
# colour; the page animates the active one (a draw-in, the cycle's arrows turn, the flame
# flickers). Keys match TOPICS.
ICONS: dict[str, dict] = {
    "inflation": {"color": "#fb7185", "anim": "flame", "svg":
                  '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 '
                  '2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>'},
    "fed_funds": {"color": "#38bdf8", "anim": "", "svg":
                  '<line x1="3" x2="21" y1="22" y2="22"/><line x1="6" x2="6" y1="18" y2="11"/>'
                  '<line x1="10" x2="10" y1="18" y2="11"/><line x1="14" x2="14" y1="18" y2="11"/>'
                  '<line x1="18" x2="18" y1="18" y2="11"/><polygon points="12 2 20 7 4 7"/>'},
    "dollar": {"color": "#a78bfa", "anim": "swap", "svg":
               '<path d="M8 3 4 7l4 4"/><path d="M4 7h16"/><path d="m16 21 4-4-4-4"/><path d="M20 17H4"/>'},
    "unemployment": {"color": "#f59e0b", "anim": "", "svg":
                     '<rect width="20" height="14" x="2" y="7" rx="2" ry="2"/>'
                     '<path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>'},
    "cycle": {"color": "#34d399", "anim": "spin", "svg":
              '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
              '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>'},
    "growth": {"color": "#22d3ee", "anim": "rise", "svg":
               '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>'},
    "debt": {"color": "#94a3b8", "anim": "tilt", "svg":
             '<path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/>'
             '<path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="M7 21h10"/>'
             '<path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>'},
}


def icon(key: str) -> dict:
    return ICONS.get(key) or {"color": "#94a3b8", "anim": "", "svg": '<circle cx="12" cy="12" r="9"/>'}


def rail(db: Session) -> list[dict]:
    """The left rail: every topic with its tone, word and headline figure."""
    out = []
    for t in TOPICS:
        r = reading(db, t["key"])
        out.append({"key": t["key"], "label": t["label"], "blurb": t["blurb"], "cadence": t["cadence"],
                    "tone": r["tone"], "word": r["word"], "headline": r.get("headline"), "icon": icon(t["key"])})
    return out


# ---------------------------------------------------------------------------
# The next release, from the Calendar feed
# ---------------------------------------------------------------------------
def next_release(key: str, *, today: _dt.date | None = None, events=None, pattern: str | None = None
                 ) -> dict | None:
    """The first upcoming US release that belongs to this topic: {title, when (ISO UTC), period,
    forecast, previous}. ``events`` may be passed (tests); otherwise the Calendar feed is read
    (cached there). ``pattern`` narrows the match (one tab of a topic, e.g. PCE only). None when
    the topic has no release or the feed is empty."""
    t = topic(key)
    pattern = pattern or t.get("release")
    if not pattern:
        return None
    today = today or _dt.date.today()
    if events is None:
        try:
            from .calendars import economic_events
            events = economic_events(today, today + _dt.timedelta(days=60), ("US",), 0)
        except Exception:  # noqa: BLE001
            return None
    pat = re.compile(pattern)
    now = _dt.datetime.utcnow()
    for e in events:
        title = e.get("title") or ""
        if not pat.match(title):
            continue
        when = e.get("when")
        if when and when < now - _dt.timedelta(hours=2):
            continue
        return {"title": title, "when": when.isoformat() + "Z" if when else None, "period": e.get("period"),
                "forecast": e.get("forecast"), "previous": e.get("previous")}
    return None


# ---------------------------------------------------------------------------
# The chart payload
# ---------------------------------------------------------------------------
_MAX_POINTS = 3000


def _thin(points):
    """Keep a daily 25-year series under ~3000 points for the browser; the newest point always
    stays, so the latest reading is exact."""
    if len(points) <= _MAX_POINTS:
        return points
    step = math.ceil(len(points) / _MAX_POINTS)
    out = points[::step]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def payload(db: Session, key: str) -> dict:
    """Everything the page needs for one topic: the series (full stored history), the band and
    reference lines, the reading, the next release and the course text."""
    t = topic(key)
    inds = _inds(db, [s["key"] for s in t["series"]])
    series = []
    for s in t["series"]:
        ind = inds.get(s["key"])
        pts = _ind.points_for(db, ind) if ind is not None else []
        scale = s.get("scale")
        if scale:
            pts = [(a, b * scale) for a, b in pts]
        pts = _thin(pts)
        series.append({
            "key": s["key"], "label": s.get("label") or (ind.label if ind else s["key"]),
            "unit": s.get("unit") or (ind.unit if ind else ""), "note": ind.note if ind else None,
            "style": s.get("style", "line"), "color": s["color"], "on": bool(s.get("on", True)),
            "axis": s.get("axis", "right"), "width": s.get("width", 2),
            "toggle": bool(s.get("toggle", False)),   # drawn as a tick box in the legend; its state is shared across the tabs
            "source": f"FRED {ind.source_ref}" if ind and ind.source == "fred" else (f"Yahoo {ind.source_ref}" if ind else ""),
            "points": [[a.date().isoformat(), round(b, 4)] for a, b in pts],
            "n": len(pts), "first": pts[0][0].date().isoformat() if pts else None,
            "last": pts[-1][0].date().isoformat() if pts else None,
        })
    r = reading(db, key)
    lines = list(t.get("lines") or []) + list(r.get("levels") or [])
    # The charts: a topic is one chart unless it declares panels (Inflation: one per index).
    # Each panel carries the keys of its series, its band and its reference lines.
    panels = []
    for p in (t.get("panels") or [{"key": t["key"], "label": None, "series": [s["key"] for s in t["series"]]}]):
        pd = {"key": p["key"], "label": p.get("label"), "note": p.get("note"), "series": list(p["series"]),
              "band": p.get("band", t.get("band")), "lines": p.get("lines", lines)}
        if p.get("reading"):
            # the tab's own reading (the Inflation indexes): number, gauge, verdict, next release
            ik = p["reading"]
            try:
                pr = read_price_index(db, inds, ik)
                pr["gauge"] = _gauge_index(db, inds, ik)
                pr["glance"] = r.get("glance")
            except Exception as exc:  # noqa: BLE001
                pr = _blank(t, f"The reading could not be computed ({type(exc).__name__}).")
            pd["reading"] = pr
            pd["next"] = next_release(key, pattern=_PRICE_INDEX[ik][1])
        panels.append(pd)
    return {
        "topic": {"key": t["key"], "label": t["label"], "blurb": t["blurb"], "cadence": t["cadence"],
                  "cadence_text": t["cadence_text"], "icon": icon(t["key"])},
        "series": series, "panels": panels, "band": t.get("band"), "lines": lines, "reading": r,
        "next": next_release(key), "text": t["text"],
        "links": _links.for_topic(t["key"]),        # the course's link sheet, this topic's rows
    }
