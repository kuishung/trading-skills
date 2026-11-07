"""The course's data-source links (v4.243) - "Macro Insights: Key Data Source Links v1.20"
(Beyond Insights, 22-Mar-2025), transcribed for the Macro page. Every link sits on the topic
page(s) it belongs to, under "Where to look it up", grouped by the sheet's own headings (user,
2026-11-07: "put the data link to each of the page which is relevant" - no separate tab).

Each item: (label, url, topics) - ``topics`` are the Macro topic keys
(``services/macro_topics.TOPIC_KEYS``) the link is shown under; every link has at least one.
Grouped as the sheet groups them, page 1 (key sources) then page 2 (advanced references).
"""
from __future__ import annotations

_FF = "https://www.forexfactory.com/"
_TE = "https://tradingeconomics.com/"

GROUPS: list[dict] = [
    {"key": "monetary", "label": "Monetary policy", "page": 1, "items": [
        ("Fed interest rate", _TE + "united-states/interest-rate", ("fed_funds",)),
        ("FOMC calendar, statements, press conferences, projections (dot plot)",
         "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", ("fed_funds",)),
        ("Fed rate projection - CME FedWatch", "https://www.cmegroup.com/trading/interest-rates/countdown-to-fomc.html",
         ("fed_funds",)),
        ("Expected future path of the fed funds rate (Atlanta Fed market probability tracker)",
         "https://www.atlantafed.org/cenfis/market-probability-tracker", ("fed_funds",)),
        ("European Central Bank", "https://www.ecb.europa.eu/home/html/index.en.html", ("fed_funds", "dollar")),
        ("Bank of England", "https://www.bankofengland.co.uk/", ("fed_funds", "dollar")),
        ("People's Bank of China", "http://www.pbc.gov.cn/en/3688006/index.html", ("fed_funds", "dollar")),
        ("Central banks news", "https://www.centralbanking.com/", ("fed_funds",)),
        ("Bank reserve requirement by country", "https://www.ceicdata.com/en/indicator/reserve-requirement-ratio", ("fed_funds",)),
        ("Global monetary policy tracker (CFR)", "https://www.cfr.org/global/global-monetary-policy-tracker/p37726#!/p37726",
         ("fed_funds", "dollar")),
    ]},
    {"key": "bonds", "label": "Bonds and the yield curve", "page": 1, "items": [
        ("Yield curve (GuruFocus)", "https://www.gurufocus.com/yield_curve.php", ("fed_funds", "cycle")),
        ("Yield curve by country", "https://secure.fundsupermart.com/fsm/bonds/yield-curve", ("fed_funds",)),
        ("Yield curve 2Y vs 10Y (FRED T10Y2Y)", "https://fred.stlouisfed.org/series/T10Y2Y", ("fed_funds", "cycle")),
        ("Dynamic yield curve with SPX (StockCharts)", "https://stockcharts.com/freecharts/yieldcurve.php", ("cycle",)),
        ("10-year bond yield", _TE + "united-states/government-bond-yield", ("fed_funds",)),
        ("20-year bond yield", _TE + "usgg20y:ind", ("fed_funds",)),
        ("US corporate bonds (SIFMA)", "https://www.sifma.org/resources/research/us-corporate-bonds-statistics/", ("debt",)),
        ("US Treasury bonds (TreasuryDirect)", "https://www.treasurydirect.gov/indiv/products/prod_tbonds_glance.htm", ("debt",)),
        ("Asian bonds (ADB)", "https://asianbondsonline.adb.org/", ("debt",)),
    ]},
    {"key": "indicators", "label": "Economic indicators - overall", "page": 1, "items": [
        ("Inflation (CPI)", _TE + "united-states/inflation-cpi", ("inflation",)),
        ("Consumer Price Index report (BLS)", "https://www.bls.gov/cpi/", ("inflation",)),
        ("Producer Price Index report (BLS)", "https://www.bls.gov/ppi/", ("inflation",)),
        ("Employment: ADP, NFP, unemployment rate, jobless claims, JOLTS (Forex Factory calendar)", _FF,
         ("unemployment",)),
        ("Labor participation rate (FRED CIVPART)", "https://fred.stlouisfed.org/series/CIVPART", ("unemployment",)),
        ("US wages and salaries growth", _TE + "united-states/wage-growth", ("unemployment", "inflation")),
        ("Consumer confidence (Forex Factory calendar)", _FF, ("growth",)),
        ("Consumer spending", _TE + "united-states/consumer-spending", ("growth", "cycle")),
        ("GDP", _TE + "united-states/gdp", ("growth",)),
        ("GDP growth", _TE + "united-states/gdp-growth", ("growth",)),
        ("Preliminary GDP (Forex Factory calendar)", _FF, ("growth",)),
        ("Current economic cycle (Fidelity business cycle update)",
         "https://institutional.fidelity.com/app/item/RD_13569_40890/business-cycle-update.html", ("cycle",)),
        ("Household debt to GDP", _TE + "united-states/households-debt-to-gdp", ("debt",)),
    ]},
    {"key": "sector", "label": "Economic indicators - sector and industry", "page": 1, "items": [
        ("Retail sales and core retail sales", _TE + "united-states/retail-sales", ("growth",)),
        ("Manufacturing PMI (Forex Factory calendar)", _FF, ("cycle", "growth")),
        ("Services PMI (Forex Factory calendar)", _FF, ("cycle", "growth")),
        ("Housing market: home sales, building permits (Forex Factory calendar)", _FF, ("growth",)),
        ("Global supply chain pressure index", _TE + "world/supply-chain-pressure-index", ("inflation",)),
        ("Global supply chain pressure index - explainer",
         "https://transportgeography.org/contents/chapter7/logistics-freight-distribution/global-supply-chain-pressure-index/",
         ("inflation",)),
        ("Durable goods orders (Haver)",
         "https://www.haver.com/articles/u-s-durable-goods-orders-surprised-again-on-the-upside-in-june", ("growth",)),
        ("Home price index (FRED, Case-Shiller 20)", "https://fred.stlouisfed.org/series/SPCS20RSA", ("inflation", "growth")),
    ]},
    {"key": "trade", "label": "Trade dependencies (OEC)", "page": 1, "items": [
        ("US exports", "https://oec.world/en/visualize/tree_map/hs92/export/usa/all/show/2022", ("dollar",)),
        ("US imports", "https://oec.world/en/visualize/tree_map/hs92/import/usa/all/show/2022/", ("dollar",)),
        ("US major export countries", "https://oec.world/en/visualize/tree_map/hs92/export/usa/show/all/2022/", ("dollar",)),
        ("US major import countries", "https://oec.world/en/visualize/tree_map/hs92/import/usa/show/all/2022/", ("dollar",)),
        ("What China imports from the US", "https://oec.world/en/visualize/tree_map/hs92/import/chn/usa/show/2022/", ("dollar",)),
        ("What the US imports from China", "https://oec.world/en/visualize/tree_map/hs92/import/usa/chn/show/2022/", ("dollar",)),
        ("Major exporters of petroleum gas and crude oil",
         "https://oec.world/en/visualize/tree_map/hs92/export/show/all/52711.52709/2022", ("inflation", "dollar")),
    ]},
    {"key": "fiscal", "label": "Fiscal policy and US politics", "page": 1, "items": [
        ("US fiscal revenue vs expenditure (Treasury)", "https://fiscaldata.treasury.gov/americas-finance-guide/", ("debt",)),
        ("Covid-19 stimulus (Investopedia)",
         "https://www.investopedia.com/government-stimulus-efforts-to-fight-the-covid-19-crisis-4799723", ("debt",)),
        ("Stock market performance by president (Macrotrends)",
         "https://www.macrotrends.net/2481/stock-market-performance-by-president", ("cycle",)),
        ("Stock market returns under every president (Forbes)",
         "https://www.forbes.com/sites/sergeiklebnikov/2020/07/23/historical-stock-market-returns-under-every-us-president/",
         ("cycle",)),
        ("American Rescue Plan", "https://en.wikipedia.org/wiki/American_Rescue_Plan_Act_of_2021", ("debt",)),
        ("Build Back Better", "https://www.whitehouse.gov/build-back-better/", ("debt",)),
        ("American Families Plan", "https://en.wikipedia.org/wiki/American_Families_Plan", ("debt",)),
        ("American Jobs Plan", "https://en.wikipedia.org/wiki/American_Jobs_Plan", ("debt",)),
        ("Inflation Reduction Act guidebook", "https://www.whitehouse.gov/cleanenergy/inflation-reduction-act-guidebook/",
         ("debt", "inflation")),
        ("Congress legislation", "https://www.congress.gov/", ("debt",)),
        ("GovTrack", "https://www.govtrack.us/", ("debt",)),
        ("Biden 2025 tax proposals (Tax Foundation)",
         "https://taxfoundation.org/research/all/federal/biden-budget-2025-tax-proposals/", ("debt",)),
    ]},
    {"key": "money", "label": "Money supply", "page": 2, "items": [
        ("US Fed balance sheet", "https://www.federalreserve.gov/monetarypolicy/bst_recenttrends.htm", ("fed_funds",)),
        ("ECB balance sheet", _TE + "euro-area/central-bank-balance-sheet", ("fed_funds",)),
        ("UK central bank balance sheet", _TE + "united-kingdom/central-bank-balance-sheet", ("fed_funds",)),
        ("China central bank balance sheet", _TE + "china/central-bank-balance-sheet", ("fed_funds",)),
        ("Japan central bank balance sheet", _TE + "japan/central-bank-balance-sheet", ("fed_funds",)),
        ("How much money is out there", "https://www.rankred.com/how-much-money-is-there-in-the-world/", ("fed_funds",)),
        ("S&P 500 earnings (multpl)", "https://www.multpl.com/s-p-500-earnings", ("growth",)),
        ("US capital markets fact book (SIFMA)", "https://www.sifma.org/resources/research/fact-book/", ("debt",)),
    ]},
    {"key": "debt", "label": "Debt", "page": 2, "items": [
        ("US government debt to GDP", _TE + "united-states/government-debt-to-gdp", ("debt",)),
        ("US Debt Clock", "https://www.usdebtclock.org/", ("debt",)),
        ("Holders of US Treasuries (SIFMA)",
         "https://www.sifma.org/resources/research/us-treasury-securities-statistics/us-treasury-securities-statistics-sifma/",
         ("debt",)),
        ("Investor margin debt (FINRA via GuruFocus)", "https://www.gurufocus.com/economic_indicators/4264/finra-investor-margin-debt",
         ("debt", "cycle")),
        ("Margin debt as % of total market value", "https://www.currentmarketvaluation.com/models/margin-debt.php", ("debt", "cycle")),
        ("US margin debt (MacroMicro)", "https://en.macromicro.me/charts/415/us-margin-debt", ("debt",)),
        ("Delinquency rate on consumer loans (FRED DRCLACBS)", "https://fred.stlouisfed.org/series/DRCLACBS", ("debt",)),
        ("Delinquency rate on all loans (FRED DRALACBN)", "https://fred.stlouisfed.org/series/DRALACBN", ("debt",)),
    ]},
    {"key": "flows", "label": "Fund flows", "page": 2, "items": [
        ("RRG charting (free, not stable)", "https://www.relativerotationgraphs.com/", ("cycle",)),
        ("RRG charting (StockCharts, subscription)", "https://stockcharts.com/", ("cycle",)),
        ("Fed balance sheet vs S&P 500", "https://www.currentmarketvaluation.com/posts/2021/07/Fed-Balance-Sheet-vs-SP500.php",
         ("fed_funds",)),
        ("World interest rates (FXStreet)", "https://www.fxstreet.com/economic-calendar/world-interest-rates", ("fed_funds", "dollar")),
        ("Currency relative strength (Finviz)", "https://elite.finviz.com/forex_performance.ashx", ("dollar",)),
        ("Composition of reserve currencies (IMF COFER)", "https://data.imf.org/?sk=E6A5F467-C14B-4AA8-9F6D-5A09EC4E62A4", ("dollar",)),
        ("EPFR weekly fund flows highlights (video)",
         "https://epfr.com/insights/multimedia/weekly-fund-flows-highlights-25-march-2024-equity-fund-flows-bond-flows-financial-markets-data-investor-sentiment-daily-fund-flows-emerging-markets/",
         ("cycle",)),
        ("Fund flow news (Reuters)", "https://www.reuters.com/markets/funds/", ("cycle",)),
        ("ETF fund flows (etf.com)", "https://www.etf.com/etfanalytics/etf-fund-flows-tool", ("cycle",)),
        ("Estimated long-term mutual fund flows (ICI)", "https://www.ici.org/research/stats/flows", ("cycle",)),
        ("Money market funds - total financial assets (FRED MMMFFAQ027S)", "https://fred.stlouisfed.org/series/MMMFFAQ027S",
         ("fed_funds", "cycle")),
        ("Money market fund assets, weekly (ICI)", "https://www.ici.org/research/stats/mmfassets", ("fed_funds", "cycle")),
        ("Foreign direct investment flows (OECD)", "https://www.oecd.org/investment/statistics.htm", ("dollar", "growth")),
        ("S&P 500 revenue, earnings and margins (Yardeni)", "https://www.yardeni.com/pub/peacocksp500tables.pdf", ("growth",)),
        ("S&P 500 earnings, dividends and buybacks (Yardeni)", "https://www.yardeni.com/pub/bbdivepsyield.pdf", ("growth",)),
        ("Major cryptoassets by market cap", "https://coinmarketcap.com/charts/", ("cycle",)),
        ("Crypto miners' revenue", "https://www.blockchain.com/charts/miners-revenue", ("cycle",)),
    ]},
    {"key": "more", "label": "Additional references", "page": 2, "items": [
        ("Caixin Global (China news)", "https://www.caixinglobal.com/", ("growth",)),
        ("China Briefing", "https://www.china-briefing.com/", ("growth",)),
        ("South China Morning Post", "https://www.scmp.com/", ("growth",)),
        ("How bad is China's manufacturing exodus? (Nikkei)",
         "https://asia.nikkei.com/Spotlight/Caixin/How-bad-is-China-s-manufacturing-exodus", ("growth", "dollar")),
        ("Macro and micro indicators (MacroMicro)", "https://en.macromicro.me/", ("growth", "cycle")),
        ("Euro area statistics - inflation",
         "https://www.euro-area-statistics.org/digital-publication/statistics-insights-inflation/index.html?lang=en", ("inflation",)),
        ("Impact of inflation and war globally (OECD economic outlook)", "https://www.oecd.org/economic-outlook/#cost-of-living",
         ("inflation",)),
        ("Inflation and annual GDP growth projections (OECD)", "https://www.oecd.org/", ("growth", "inflation")),
        ("Central bank balance sheet reduction (CaixaBank Research)",
         "https://www.caixabankresearch.com/en/economics-markets/financial-markets/reduction-central-banks-balance-sheets",
         ("fed_funds",)),
        ("Market risk premium", "http://www.market-risk-premia.com/market-risk-premia.html", ("cycle",)),
        ("The Buffett indicator (Current Market Valuation)", "https://www.currentmarketvaluation.com/models/buffett-indicator.php/",
         ("cycle",)),
        ("The Buffett indicator (Longtermtrends)", "https://www.longtermtrends.net/market-cap-to-gdp-the-buffett-indicator/", ("cycle",)),
        ("Wilshire 5000 / GDP (TradingView)", "https://www.tradingview.com/chart/?symbol=FRED%3AWILL5000PR%2FFRED%3AGDP", ("cycle",)),
        ("OPEC share of world crude oil reserves", "https://www.opec.org/opec_web/en/data_graphs/330.htm", ("inflation",)),
        ("OPEC", "https://www.opec.org/opec_web/en/index.htm", ("inflation",)),
    ]},
]

SHEET = {"title": "Macro Insights: Key Data Source Links", "version": "v1.20", "date": "22 Mar 2025",
         "publisher": "Beyond Insights"}


def for_topic(key: str) -> list[dict]:
    """The sheet's links for one Macro topic, grouped by the sheet's headings in sheet order:
    [{group, items: [{label, url}]}] (empty groups left out)."""
    out = []
    for g in GROUPS:
        items = [{"label": l, "url": u} for l, u, topics in g["items"] if key in topics]
        if items:
            out.append({"group": g["label"], "items": items})
    return out


def all_items() -> list[tuple[str, str, tuple]]:
    return [it for g in GROUPS for it in g["items"]]


def all_urls() -> list[str]:
    return [u for _, u, _ in all_items()]
