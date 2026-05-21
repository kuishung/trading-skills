"""Resources layer -- stateless data sources.

Read-only fetchers callable on-demand by any strategy. Examples in this
folder today: IBKR bars/quotes/scanner adapter, yfinance float lookup,
yfinance news catalyst classifier, IBKR smoke/dryrun debug tools.

The Resources layer:
  - knows nothing about strategies
  - has no per-strategy logic
  - never decides whether to trade
  - serves any number of strategies via the same module

Adding a new resource: drop a module here (e.g. resources/finviz.py),
import it from whichever strategy needs it. No registration needed.
"""
