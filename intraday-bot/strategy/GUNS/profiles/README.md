# strategy/GUNS/profiles/ — per-ticker behavioral baselines

Cached JSON profiles for tickers the GUNS family has seen. Built and
refreshed by `resources/ticker_profile.py` with a 24-hour TTL.

Each `<TICKER>.json`:

```json
{
  "ticker": "POET",
  "as_of": "2026-05-21",
  "atr_14d": 2.53,
  "atr_pct": 17.10,
  "prev_close": 14.78,
  "daily_trend": "Uptrend",
  "avg_minute_vol_rth": 199859,
  "minute_vol_stddev": 714675,
  "data_source": "yfinance",
  "last_refreshed_utc": "2026-05-21T08:51:59+00:00"
}
```

## Why per-family

Different strategy families care about different baseline statistics
(GUNS cares about pre-market vol distributions; a future ORB family
would care about the opening-range width). Per-family profiles let
each family tune what it stores without conflict.

## Refresh

Refreshed on demand by `resources/ticker_profile.refresh_profile(ticker)`
when the cache is stale (>= 24h since `last_refreshed_utc`). Strategies
typically call `get_or_refresh()` in their shortlist phase, once per
candidate.

## Gitignored or committed?

**Committed.** Profiles are small, helpful for cross-PC continuity,
and useful as a record of what each ticker looked like at the time a
trade was logged. If the file grows unwieldy in a year's time, switch
to gitignore + sync via Dropbox only.

## Changelog

### 2026-05-21 — Folder established
- Created during the Track 5 (per-ticker behavior memory) buildout of
  the enrichment program. Initial population happens via
  `resources/ticker_profile.py` from yfinance daily + minute bars.
- TradingView MCP and IBKR data sources are scaffolded but not yet
  wired (see `resources/ticker_profile.py` for the source-priority
  comment).
