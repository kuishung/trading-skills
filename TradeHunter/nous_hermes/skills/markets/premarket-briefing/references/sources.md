# Trusted sources for the pre-market briefing

The agent uses its own web search + browser. These are the preferred, reliable
sources per section. Prefer primary/real-time sources; cross-check a number if
it looks stale or off-consensus.

## Economic calendar
- Investing.com economic calendar (filter: United States)
- TradingEconomics calendar
- ForexFactory calendar
- MarketWatch economic calendar
- Federal Reserve calendar (FOMC dates, speakers)

## Pre-market sentiment / futures / macro tape
- CNBC pre-markets / futures page — also a primary source for major market news/headlines
- MarketWatch markets / top stories — major market news + overnight narrative
- Investing.com (indices futures, VIX, DXY, US10Y, WTI, gold, BTC)
- TradingView quotes
- Reuters / Bloomberg markets headlines for the overnight narrative

## Sector rotation (RRG — Relative Rotation Graphs)
- StockCharts RRG (sector ETFs vs SPY benchmark) — leading / weakening / lagging / improving quadrants
- TradingView RRG — sector/asset rotation, same quadrant model
- Use to inform the Step 3 Micro sector-rotation read; flag any notable rotation.

## Catalysts / movers (large & mid cap)
- StreetInsider (stock-specific + industry catalysts: earnings, guidance, analyst actions, M&A, PR) — strong for per-name and sector catalyst flow
- TradingView (stock + industry/sector news feeds, movers, screeners) — cross-check pre-market % and sector moves
- Benzinga (Why Is It Moving, analyst ratings)
- Briefing.com "In Play"
- Yahoo Finance (trending tickers, earnings calendar)
- MarketWatch movers / premarket
- Seeking Alpha headlines
- Earnings Whispers (earnings calendar + reactions)
- Company press-release wires (Business Wire, PR Newswire, GlobeNewswire)
- FDA Calendar / BioPharmaCatalyst (clinical/regulatory)

## Holiday / session
- NYSE holidays page ("US stock market holidays <year>") — confirm full close vs early close (1:00 PM ET half-days)

## Notes
- US open 09:30 ET. EDT (Mar–Nov) = 21:30 MYT; EST (Nov–Mar) = 22:30 MYT.
- Half-days (day after Thanksgiving, Christmas/July-4th eves) close 13:00 ET.
- Future upgrade path: when the host server has finviz/yfinance available, the
  catalyst scan in Step 4 can be backed by a structured screener script placed
  in this skill's scripts/ dir and called via execute_code, instead of pure web
  research. Not wired yet (clean server install as of v1.0.0).
