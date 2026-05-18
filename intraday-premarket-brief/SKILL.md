---
name: intraday-premarket-brief
version: 0.2.0
description: Pre-market intraday brief — a twice-daily ritual that takes a Finviz screener URL of liquid US movers, enriches every ticker with gap %, pre-market structure, news catalysts, and daily trend, then ranks them into Early Gappers (continuation candidates) and Faders (extended / fade candidates). Runs in two modes — T-60 (60 min before US open) and T-30 (30 min before US open) — and surfaces the Consensus list (top 10 in BOTH runs) as the trader's actionable shortlist. Trigger this skill when the user wants to "run the pre-market brief", "do pre-market study", "find today's intraday candidates", "scan for intraday setups", or any request involving pre-open Finviz-based screening for day-trading.
---

# Intraday Pre-Market Brief

**Version:** 0.2.0 — 2026-05-18

Twice-daily ritual that turns one Finviz intraday-mover screener URL into a ranked, sectioned brief of trade candidates for the upcoming US session.

## Changelog

- **0.2.0** (2026-05-18) — Catalyst relevance gate. yfinance's per-ticker news feed leaks tangential stories — observed live in the first T-60 run: SMCI's feed returned a Nokia/Cisco earnings article, GOOG and GOOGL's feeds returned a Baidu earnings article, SNDK's feed returned a macro Dow Jones piece mentioning "Nvidia Earnings Ahead". Each of those phantom-earnings tags inflated the score by 40 points (catalyst weight × earnings weight × 100) and pushed the false-positive tickers to the top of the ranking. Fix: new `fetch_ticker_aliases` step pulls each ticker's `shortName` / `longName` via `yfinance.Ticker.info` (parallel, 10 workers, same pattern as the news fetch) and derives lowercased name tokens (corporate suffixes like Inc/Corp/Holdings stripped). `classify_news` now requires the headline or summary to mention the ticker symbol OR a distinctive name token (word-boundary match) before applying any catalyst tag, including the M&A hard-exclude. This refinement is strictly stricter than 0.1.0 — every true positive from 0.1.0 still matches (legitimate Coinbase/Rocket Lab/Netflix headlines all mention the company by name) and only the spurious matches drop, so it's a backward-compatible accuracy fix rather than a formula change. New `filtered_count` field per ticker in the snapshot JSON reports how many headlines were dropped by the gate, for tunability. No new dependencies.
- **0.1.0** (2026-05-18) — Initial release. Two modes (`--mode t60` / `--mode t30`), Finviz scrape with `&r=` pagination, yfinance enrichment (daily OHLC + EMA20/50/200 trend, pre-market 1-min bars for gap / VWAP / structure), `Ticker.news` catalyst classification with M&A hard-exclude, catalyst-weighted scoring (40/30/20/10), gap-quality bell curve replacing naive gap-size weighting, section split into 🟢 Early Gappers / 🔴 Faders, T-30 mode reads same-day T-60 snapshot to compute Consensus (priorities) / Faded / Emerged buckets. Output is markdown to stdout; optional Telegram send via `--send-telegram` once `setup.py` configures the bot.

## Versioning policy

Bump the version field in the frontmatter **and** add a one-line entry to the Changelog above whenever the skill changes. Use semantic versioning:

- **Patch (x.y.Z)** — typo/wording fixes, clarifications that don't change behaviour.
- **Minor (x.Y.0)** — new optional step, extra output, new edge-case handling that's backward-compatible.
- **Major (X.0.0)** — change in inputs, change in output format/columns, change in scoring formula, change in section-split rules, or anything that would make a previous run's output incomparable.

Always update both places (frontmatter `version:` and the dated Changelog line) in the same edit so they never drift.

## The ritual

Two runs per market day:

| Mode | When (US ET) | When (Malaysia, EDT) | When (Malaysia, EST) | Purpose |
| --- | --- | --- | --- | --- |
| `t60` | 08:30 | 20:30 | 21:30 | Initial study — chart prep |
| `t30` | 09:00 | 21:00 | 22:00 | Confirmation study — last-mile setups |

Each run independently ranks the screener's tickers and emits its own top 10 per section. The T-30 run additionally reads the same-day T-60 snapshot and surfaces:

- **⭐ Consensus** — tickers that appeared in BOTH the T-60 and T-30 top 10 of the same section. These are your priority trades.
- **📉 Faded** — appeared in T-60 only. Setup deteriorated in the last 30 min.
- **📈 Emerged** — appeared in T-30 only. Late entrants (often fresh catalysts).

The Consensus list (typically 5-7 names) is the actionable shortlist for the open.

## What gets studied per ticker

For every ticker on the Finviz screen, the brief enriches with:

| Dimension | Source | Used in |
| --- | --- | --- |
| Gap % vs prior close | yfinance pre-market quote | Scoring, section split |
| Pre-market high / low / VWAP | yfinance 1-min bars, ET 04:00-09:30 today | Section split (structure signals) |
| Position-in-premkt-range | derived | Section split |
| Pre-market volume vs 20d avg daily vol | yfinance + daily bars | Scoring (gap×vol component) |
| Prior-day high / low | yfinance daily bars | Scoring (level proximity) |
| Daily trend (Uptrend/Sideways/Downtrend) | EMA20/50/200 + EMA50 slope, same rule as MATP `classify_trend.py` | Scoring (trend alignment) |
| News headlines (last 24h) | `yfinance.Ticker.news` | Catalyst classification |
| Earnings calendar (next 7d) | `yfinance.Ticker.calendar` | Catalyst classification |

### Catalyst classification

Each ticker's recent news is regex-classified into one of:

| Type | Weight in catalyst score | Examples |
| --- | --- | --- |
| Earnings | 1.0 | "Q3 earnings beat", "EPS miss", "raises guidance" |
| Regulatory / clinical | 0.9 | "FDA approval", "PDUFA", "phase 3 results", "SEC settles" |
| Analyst action | 0.7 | "upgrade to Buy", "$200 price target", "initiates coverage" |
| None | 0.0 | no matching keywords |
| **M&A (HARD EXCLUDE)** | — | "to be acquired", "merger", "tender offer", "go private" |

M&A names are dropped from the ranking entirely. Reason: once a buyout deal is announced, the price converges to the deal value and trades like a tight rubber band — no intraday R:R. The brief footer lists excluded tickers so the user can verify the filter isn't being overzealous.

## Scoring formula

```
score = 100 × (
    0.40 × catalyst_strength
  + 0.30 × gap_quality × premkt_volume_signal
  + 0.20 × trend_alignment
  + 0.10 × level_proximity
)
```

Where:

- **`catalyst_strength`** ∈ {0, 0.7, 0.9, 1.0} for none / analyst / regulatory / earnings (max if multiple match).
- **`gap_quality`** is a bell curve over `|gap%|`: peaks at 1-3%, decays above ~4% (penalty for late-to-the-party / fade-prone moves). Specifically: 0.2 at <0.5%, 0.5 at 0.5-1%, 1.0 at 1-3%, 0.7 at 3-4%, 0.4 at 4-6%, 0.2 at 6-10%, 0.1 above 10%.
- **`premkt_volume_signal`** = `clamp(premkt_vol / (20d_avg_daily_vol × 0.02), 0, 1)`. Roughly: pre-market volume that's 2% of an average day's volume = full signal. Below, partial. Above, capped at 1.
- **`trend_alignment`** = 1 if (Uptrend AND gap up) OR (Downtrend AND gap down); else 0.
- **`level_proximity`** = 1 if pre-market price is above prior-day-high OR below prior-day-low; else 0.

Final score is in [0, 100]. Used purely for ranking within sections.

## Section split

After scoring, tickers are bucketed by **pre-market structure** (not just gap size):

| Section | Filter |
| --- | --- |
| 🟢 **Early Gappers** | `0.5% ≤ \|gap%\| ≤ 4%` AND `position_in_range ≥ 0.5` AND `price ≥ premkt_vwap` |
| 🔴 **Faders** | `\|gap%\| > 4%` OR `position_in_range < 0.4` OR `price < premkt_vwap` |
| (neutral) | doesn't fit either — dropped from output |

Where `position_in_range = (current_price - premkt_low) / (premkt_high - premkt_low)`. Above 0.5 = trading in upper half of premkt range (momentum holding). Below 0.4 = sellers winning the morning.

**Direction note:** Faders are not always shorts — for some they're "avoid longs, wait for fill-to-VWAP scalp." The section label is "structure says extended/exhausted," interpretation is the trader's.

Each section is independently ranked by score, top 10 per section per run.

## What this skill does NOT do (yet)

- Place trades — execution is delegated to the sibling `alpaca-trader-paper` skill if you choose. This brief is read-only.
- Options-flow analysis — explicitly out of scope (paid feed required for real signal).
- Backtest the scoring — pure forward signal, no historical performance evaluation. Tune the weights in `_common.py` based on observed live performance.
- Auto-schedule itself — phase-1 is on-demand or manual cron. Telegram delivery + Windows Task Scheduler / GitHub Actions cron is a phase-2 add.

## First-run setup (one time)

```powershell
py -m pip install -r requirements.txt
py scripts/setup.py
```

`setup.py` prompts for:

1. **Finviz screener URL** (required) — the intraday-mover URL the daily ritual scans. Persisted to `.env` as `INTRADAY_FINVIZ_URL`.
2. **Telegram bot token + chat ID** (optional) — for `--send-telegram` flag. If you skip, briefs only go to stdout. You can re-run setup later to add Telegram.

Both are saved to a gitignored `.env`.

## Running the brief

```powershell
# T-60 run (60 min before US open)
py scripts/premarket_brief.py --mode t60

# T-30 run (30 min before US open) — emits Consensus / Faded / Emerged
py scripts/premarket_brief.py --mode t30

# Print to stdout only, even if Telegram is configured (useful for testing)
py scripts/premarket_brief.py --mode t60 --no-telegram

# Send to Telegram in addition to stdout
py scripts/premarket_brief.py --mode t60 --send-telegram

# Override the configured Finviz URL for a one-off run
py scripts/premarket_brief.py --mode t60 --url 'https://finviz.com/screener?v=131&...'

# Use an explicit ticker list (skips Finviz scrape — useful if scrape is failing)
py scripts/premarket_brief.py --mode t60 --tickers NVDA,AMD,TSLA,META

# JSON output to stdout instead of markdown
py scripts/premarket_brief.py --mode t60 --json
```

Snapshots are written to `snapshots/{YYYY-MM-DD}_{t60|t30}.json`. The T-30 run reads `{today}_t60.json` to compute the diff; if missing, T-30 still produces its own ranking but skips the Consensus block and notes the absence in the footer.

## File layout

- `SKILL.md` — this file.
- `scripts/setup.py` — one-time interactive config for Finviz URL + optional Telegram.
- `scripts/premarket_brief.py` — main brief script (`--mode t60|t30`). Self-contained: catalyst classifier, scoring, EMA, formatting, and Telegram client all live in this file.
- `requirements.txt` — Python dependencies (`yfinance`, `requests`, `beautifulsoup4`).
- `.env.example` — template config; copy to `.env` or let `setup.py` create it.
- `.gitignore` — keeps `.env` and `snapshots/` out of version control.
- `snapshots/` — daily JSON snapshots (gitignored).

## Quality checks before declaring done

- Every ticker on the Finviz screen is either in a section's top 10, in the M&A exclusion list, or accounted for in "neutral (dropped)" count.
- M&A exclusion list is sanity-checked — every excluded ticker's most relevant headline is shown so the user can spot false positives.
- For T-30: Consensus block lists only tickers actually in both T-60 and T-30 of the *same* section (Early Gappers consensus is separate from Faders consensus).
- Each ticker shows its concrete contributions: gap %, premkt vol vs avg, catalyst tag, trend, position-in-range, premkt-VWAP relation. So the score is auditable, not a black box.

## Edge cases worth flagging in the output footer

- Tickers where pre-market data was unavailable from yfinance (silent skip otherwise — surface count + names).
- Tickers with < 210 daily bars (can't compute EMA200 → trend = Unknown).
- T-30 run with no same-day T-60 snapshot found (skips Consensus block, notes why).
- Finviz scrape returned 0 tickers (error out clearly, suggest `--tickers` override).
