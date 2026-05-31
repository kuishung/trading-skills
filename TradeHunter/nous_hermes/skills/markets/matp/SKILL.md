---
name: matp
version: 1.0.0
description: Compute the faithful Median Analyst Target Price (MATP) + Max Buy Price (MBP) for TradeHunter and push the results to the platform. Use when asked to "refresh MATP", "run MATP", "update target prices", or on the scheduled cron. Reads the active Finviz screener filters from TradeHunter's API, expands them to a ticker universe, and for each ticker looks up the latest earnings date + analyst price targets on MarketBeat, keeps only targets issued AFTER the latest earnings, computes the median (MATP) and MBP = MATP/1.15, then POSTs the rows to TradeHunter's /api/matp endpoint. Does NOT write CSV / Google Sheets / Telegram -- output is the API push only.
---

# MATP — faithful Median Analyst Target Price for TradeHunter

This is the LLM/web-research half of TradeHunter's MATP feature. The platform
(deterministic Python on the Windows "Hermes" box) does NOT compute MATP — it
only stores + displays what this skill pushes. This skill does the fuzzy work:
browse Finviz + MarketBeat, apply the post-earnings filter, compute the median.

## Why this lives here (not in TradeHunter)
The faithful method needs per-analyst price targets WITH dates (to drop stale
pre-earnings targets). That requires reading MarketBeat's forecast pages —
messy HTML best handled by an agent with a browser, not a brittle scraper. So
TradeHunter offloads it to this agent (DeepSeek-backed), which pushes results
back over an authenticated API.

## Config (set once on the Linux box)
The skill needs two values (from the agent's env / `~/.hermes` config):
- `TRADEHUNTER_URL` — e.g. `https://app.tradehunter.net`
- `TST_INGEST_API_KEY` — the shared key, identical to `TST_INGEST_API_KEY` in
  TradeHunter's `app/.env` on the Windows Hermes box. Sent as the
  `X-API-Key` header.

## What to ask the user
Nothing on the scheduled run. If invoked ad-hoc for specific tickers
("refresh MATP for NVDA, MSFT"), use those tickers and skip Stage 1.

---

## Procedure

### Stage 1 — Get the universe (active Finviz filters)
```
GET {TRADEHUNTER_URL}/api/filters     header: X-API-Key: {TST_INGEST_API_KEY}
```
Returns `{"filters":[{"description","url"}, ...]}`. For each filter `url`,
open it in the browser and extract every ticker (with exchange). Finviz
paginates 20/page via `&r=21`, `&r=41`, ... — page through to the reported
total. **De-duplicate** tickers across filters into one universe.

(Ad-hoc mode: skip this; use the tickers the user named.)

### Stage 2 — Latest earnings date per ticker
For each ticker, open:
```
https://www.marketbeat.com/stocks/<EXCHANGE>/<TICKER>/earnings/
```
Take the most recent **past** earnings date (YYYY-MM-DD) from the history
table — not a future scheduled date. (MarketBeat tolerates NASDAQ or NYSE in
the path; both resolve.)

### Stage 3 — Analyst price targets per ticker
```
https://www.marketbeat.com/stocks/<EXCHANGE>/<TICKER>/forecast/
```
From the price-target history table, collect every row with a NUMERIC target:
`date | brokerage | $target`. For a "boost" like `$440 -> $480`, use the NEW
value ($480). Skip rows with no numeric target (reiterations, initiations with
no number). All targets are on one page — no pagination.

### Stage 4 — Filter, median, MBP, distribution
For each ticker:
1. Keep only targets with `target_date > latest_earnings_date` (**strictly**
   greater — same-day-as-earnings excluded). Call these the *post-earnings* set.
2. Sort the post-earnings set numerically; compute the **median** (MATP):
   odd n = middle value; even n = average of the two middle; n=0 = skip the
   ticker (no post-earnings coverage); n=1 = that single value.
3. `MBP = MATP / 1.15`, rounded to 2 dp. MATP rounded to 2 dp.
4. **Distribution** of the post-earnings set: `target_high` (max), `target_low`
   (min), `target_mean` (average) — the spread of analyst disagreement.
5. **Keep the full target list** — ALL targets found (post- AND pre-earnings),
   each `{brokerage, target_date, target_price}` — to push as evidence in
   Stage 5. TradeHunter recomputes post/pre from the date on display, so send
   both.

Do NOT use the mean for MATP — MATP is a median (the mean is only a
distribution stat).

### Stage 5 — Push to TradeHunter (the only output)
```
POST {TRADEHUNTER_URL}/api/matp     header: X-API-Key: {TST_INGEST_API_KEY}
Content-Type: application/json
{
  "source": "nous_hermes",
  "items": [
    {
      "symbol": "NVDA", "exchange": "NASDAQ", "last_earnings_date": "2026-05-21",
      "matp": 175.50, "mbp": 152.61, "n_targets": 12,
      "target_high": 220, "target_low": 140, "target_mean": 178.3,
      "targets": [
        {"brokerage": "Morgan Stanley", "target_date": "2026-05-22", "target_price": 200},
        {"brokerage": "Goldman Sachs",  "target_date": "2026-05-23", "target_price": 185},
        {"brokerage": "Citi",           "target_date": "2026-04-30", "target_price": 160}
      ]
    },
    ...
  ]
}
```
- `mbp` optional (server recomputes MATP/1.15 if omitted) — but send it.
- `target_high/low/mean` = distribution of the **post-earnings** set (Stage 4.4).
- `targets` = the **full** list (post + pre); server de-dupes on
  `(symbol, brokerage, target_date, target_price)`, so re-pushing the same list
  each run adds nothing new. Include `target_date` on every target — it's how
  post/pre is decided.
- Omit tickers with n=0 post-earnings targets (don't push a null MATP).
- Expect `{"ok":true,"upserted":N,"history_appended":H,"targets_added":T}`.
  401 = wrong key; 503 = TradeHunter has no `TST_INGEST_API_KEY` set yet.

Batch the POST — send all tickers in one request (or chunks of ~50).

**No CSV, no Google Sheets, no Pine, no Telegram.** The API push is the output.

---

## Quality checks before declaring done
- Every ticker pushed has `n_targets >= 1` and a sane MATP (positive, within an
  order of magnitude of the current price).
- The post-earnings filter was applied (you used `target_date > earnings_date`,
  strictly).
- Report a short summary: universe size, how many pushed, how many skipped for
  n=0, and any tickers whose earnings looked stale (>~3 months).

## Schedule (cron on the Linux agent)
Monthly full refresh (1st of month, 06:00 — pick a quiet hour):
```
hermes cron create "0 6 1 * *" \
  "Run the matp skill: refresh MATP for all active TradeHunter filters and POST the results." \
  --skill matp
```
For a more frequent earnings-aware pass, add a daily cron that runs matp only
for tickers whose latest earnings was in the last 1-2 days.

## Source / method
Mirrors the canonical MATP methodology in TradeHunter's
`resources/MATP/SKILL.md` (median of post-earnings analyst targets, MBP =
MATP/1.15) — but output is the API push, not CSV/Sheets/Pine.
