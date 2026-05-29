---
name: premarket-briefing
description: Produce a concise US-equities pre-market briefing 30 minutes before the open (09:00 ET) covering the economic calendar, pre-market sentiment, macro/micro insights, and large/mid-cap catalysts likely to drive significant intraday moves. Built to run as a weekday cron job delivered to Telegram.
version: 1.0.0
metadata:
  hermes:
    tags: [trading, markets, briefing, automation, finance]
    category: markets
    config:
      market: US equities
      open_time_et: "09:30"
      run_time_et: "09:00"
      universe: large and mid cap
---

# Pre-Market Briefing

## When to Use
Trigger every US trading weekday at **09:00 America/New_York** (30 minutes before the 09:30 ET cash open), via the scheduled cron job, or on demand with `/premarket-briefing`. The audience is a single self-directed intraday US-equities trader based in Malaysia (UTC+8). Output goes to Telegram and must be tight and scannable on a phone.

## Hard Rules (read first)
1. **This is informational, not advice.** Describe the tape and the catalysts; never tell the user to buy or sell.
2. **Freshness over completeness.** Everything must reflect TODAY's pre-market session. Always check the current date/time first. If you cannot confirm a number is from today's session, say so rather than guessing.
3. **Holiday / half-day check.** If the US equity market is closed today (weekend or holiday), send a one-line "US market closed today (<reason>)" note and stop. If it is an early-close half-day, say so in the header.
4. **DST awareness.** US open is 09:30 ET = 21:30 MYT during EDT (Mar–Nov) / 22:30 MYT during EST (Nov–Mar). State the user-local (MYT) open time in the header so the user knows when to be ready.
5. **Length budget.** Target under ~3500 characters so it fits one Telegram message. Be terse — fragments and figures, not prose.
6. **Cite trap names, don't hide them.** See "Catalyst trap annotations" below — the user has explicit setups that AVOID certain catalyst types, so a name being a trap is itself useful signal.

## Procedure

### Step 0 — Orient
- Get current date and time in ET. Compute minutes until 09:30 ET open and the MYT equivalent.
- Confirm it is a trading day (Step in Hard Rule 3). Use web search for "US stock market holidays <year>" if unsure.

### Step 1 — Economic Calendar (today, US-focused)
Web-search today's US economic releases. Good sources: Investing.com economic calendar, TradingEconomics, ForexFactory, MarketWatch calendar.
For each release list: **time (ET) · event · consensus vs prior · importance (high/med)**. Flag any release that lands DURING the session (these move the tape intraday). Note Fed speakers and Treasury auctions. If a major print (CPI, PCE, NFP, FOMC, ISM, retail sales, jobless claims) is due, call it out as the day's pivot.

### Step 2 — Pre-Market Sentiment
Pull current pre-market values (web search / browser): **ES / NQ / YM / RTY futures (% chg), VIX, DXY, US10Y yield, WTI crude, gold, BTC.** Add overnight context: how Asia (Nikkei, Hang Seng, KOSPI) and Europe (DAX, FTSE, STOXX) closed/are trading. Synthesize into a **one-line risk read**: risk-on / risk-off / mixed, and what's driving it.

### Step 3 — Macro & Micro Insights of the Day
- **Macro (top-down):** the single dominant narrative driving markets right now — Fed/rate-path repricing, inflation prints, geopolitics, oil, credit, USD. 2–3 bullets max.
- **Micro (bottom-up):** sector rotation in play, notable sector/factor moves pre-market (e.g. semis strong, regional banks weak), any theme (AI capex, GLP-1, rate-sensitives). 2–3 bullets max.

### Step 4 — Catalyst Watch (large & mid cap)
Scan for large- and mid-cap names with catalysts likely to drive a **significant intraday move**. Web-search sources: Benzinga, Briefing.com "In Play", Yahoo Finance trending, MarketWatch movers, Seeking Alpha headlines, Earnings Whispers, company PR wires.
Catalyst types to surface:
- **Earnings** — reported after yesterday's close or before today's open (beat/miss + guidance + pre-market % move).
- **Analyst actions** — notable upgrades/downgrades/initiations with price-target changes on large/mid caps.
- **Guidance / pre-announcements**, **M&A**, **FDA / clinical**, **legal/regulatory**, **product / contract** news.
- **Biggest pre-market % movers** in the large/mid-cap band with the reason.

For each name give: **TICKER · pre-mkt %chg · catalyst (one phrase) · why it could move intraday.** Aim for the 6–12 names with the cleanest catalysts; quality over a long dump.

#### Catalyst trap annotations (user's methodology — apply these tags)
The user trades setups that deliberately AVOID certain catalyst types because the edge is gone. Still LIST these names, but tag them so the user can skip fast:
- `[M&A-anchored]` — announced deal anchors price near the offer; little R:R left intraday.
- `[consumed-gapper]` — already gapped >~4–5% on a known catalyst; profit-taking tends to dominate, fade risk high.
- `[dilution]` — convertible note / secondary / at-the-market offering; tends to drift toward the offering reference price all morning.
Names without a trap tag are the "clean" candidates.

### Step 5 — Bottom Line
2–3 sentences: the day's setup in plain language — what's the dominant driver, what to watch at the open, and the key time(s) today when volatility is likely (session econ prints, Fed speakers).

## Output Format
Use the structure in `templates/briefing.md`. Plain text + light markdown only (Telegram-friendly). No tables (they wrap badly on mobile). No preamble, no sign-off — lead straight with the header.

## Verification
- Header shows today's date, minutes-to-open, and MYT open time.
- Every section is populated with TODAY's data (or explicitly marked unavailable).
- Catalyst names carry pre-market % and a one-phrase reason; trap tags applied where relevant.
- Total length fits one Telegram message (~under 3500 chars).
- No buy/sell recommendations.
