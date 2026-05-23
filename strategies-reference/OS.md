# OS — Opening Surge (reference)

> **STATUS: scaffolded + wired, fully automated, paper-mode.**
>
> This is an INTERNAL strategy — no external course or PDF source. It's a
> simplified pre-market breakout play modeled on GUNS Setup 1 but trimmed
> for fully automated execution (no catalyst classifier, no float cap, no
> human in the loop). The trade-off vs GUNS is: wider universe, more
> trades, no qualitative filters — so a higher expected loss rate per
> trade, offset by faster decision-making and zero human latency.
>
> **Real money is NOT authorized until 30 calendar days of paper-eval
> journal data clear the expectancy check in `review/stats.py`.**

---

## 1. Source attribution

- **Author:** Internal (Claude, 2026-05-23) derived from chat conversation about
  capturing the opening surge of top US movers
- **Methodology heritage:** GUNS Setup 1 (Adam Khoo Piranha Profits, Lesson 8)
  with the catalyst + float qualitative gates stripped out
- **Vendored material:** none

## 2. Methodology type

- **Type:** Fully mechanical, fully automated. **Zero human decision-maker.**
- **Time horizon:** Intraday — entry pre-market, exit by EOD at the latest
- **Instruments:** US equities, $1.50–$50, NYSE / NASDAQ / AMEX
- **Direction:** Long-only (v1)

## 3. Top-level rules

- Pre-rest the entry order before 09:30:00 ET so it sits in the broker's
  book at the open. Never react to a print after the fact.
- One setup, one trigger per name per day. No re-entries on failed breakouts.
- 0.5 % per-trade risk (tighter than GUNS's 1 %) since this strategy has
  fewer qualitative filters than GUNS, so we want smaller bets per trade.
- Max 3 concurrent positions.
- Auto-flat by 15:58 ET via the orchestrator's EOD sweep.
- **Time-based exit at 10:30 ET** if not at +1R — exit at break-even. This
  is the key differentiator from GUNS Setup 1, which lets the trade run
  to TP / SL all day.

## 4. Pattern / setup catalog

| # | Name | Trigger time | Pattern (one-line) |
|---|------|--------------|--------------------|
| 1 | **OS Breakout** (`os_breakout`) | 09:28 ET buy-stop-limit pre-rest | Top mover via IBKR scanner + tight PMH consolidation → buy-stop-limit at PMH+1¢ |

## 5. Key level hierarchy

- **PMH** — pre-market high (the level we break)
- **PML** — pre-market low (informational; not used for entry)
- **Consolidation high/low** — last 15 min of PM bars; the band must be
  within `consol_band_pct` of PMH to qualify

## 6. Entry / exit rules per setup

### Setup 1 — OS Breakout

**Eligibility (all must hold):**

1. **Symbol is a top IBKR mover** (TOP_PERC_GAIN scan, top-50 at 09:00 ET)
   AND in the dynamically refreshed shortlist
2. Price in `[$1.50, $50]` band (avoid penny stocks + too-expensive names)
3. PM volume ≥ `min_pm_volume` (default 100,000 shares)
4. Last 15 min of PM bars consolidating within `consol_band_pct` (default
   1.5 %) of the PMH — same mechanical proxy as GUNS Setup 1
5. Strategy is ON and ARMED (state flags). `ON+DISARMED` runs through
   evaluate + journal but doesn't submit.

**Trigger:** Buy-stop-limit at `PMH + 0.01`, limit at `trigger + 5¢`,
TIF=DAY. Order submitted at 09:28 ET — well before 09:30:00 open so it's
resting in Alpaca's book the moment RTH starts.

**Stop loss:** Price-tier table (12 ¢ / 17 ¢ / 25 ¢ / 40 ¢ / 50 ¢ by price
bracket — same as GUNS for now; will be ATR-normalized in v1.1).

**Take profit:** 2R (configurable via `take_profit_R`).

**Break-even move:** At +1R unrealised, stop moves to entry. Handled by
the orchestrator's `poll_breakeven_moves`.

**Time-based exit:** At 10:30 ET, if the trade has not reached +1R, close
at market. This protects against opening-surge fakeouts that drift
sideways or slowly bleed into the late morning.

**Entry cutoff:** Unfilled buy-stop-limits cancelled at 09:35 ET (5 min of
RTH was enough to break PMH; if it didn't, the setup's edge is gone).

**Concurrency cap:** 3.

## 7. Stock screening criteria

**Universe builder** (`strategy/OS/scanner.py`):

1. Pull IBKR's `TOP_PERC_GAIN` scanner at 09:00 ET (top 50 rows, NYSE +
   NASDAQ + AMEX, `min_price=$1.50`, `min_change_pct=3%`,
   `min_avg_volume=200K`).
2. Drop symbols outside `[$1.50, $50]`.
3. Drop symbols with multi-word tickers (units / preferred).
4. Write `state/watchlist_os_<date>.txt` for the entry phase to read.

No catalyst classifier, no float cap. The hypothesis is that an IBKR top
mover with sufficient PM volume + a clean PMH consolidation IS the signal —
adding catalyst inference (M&A drop, dilution drop) like GUNS does makes
this less automatable.

## 8. Catalyst guidance

**None.** Deliberate design choice — keeps the strategy automatable + fast.

The cost: OS will occasionally trade names that GUNS would reject (M&A
deals, secondaries). The expectation is that the 0.5 %-per-trade risk cap
+ time-based exit + 30-day paper-eval will surface these as bad bets in
the journal stats, which can then inform a v2 catalyst filter if needed.

## 9. Risk + sizing

- **Per-trade risk:** 0.5 % of NLV (tighter than the global 1 % cap)
- **Per-day risk:** -2 % NLV → orchestrator auto-disarms the strategy for
  the day (v1.1 feature; not in v1.0)
- **Max position notional:** 10 % NLV (the global cap)
- **Max concurrent positions:** 3
- **Position size formula:** `floor((risk_per_trade_pct × NLV) / stop_distance_$)`

## 10. What's DISCRETIONARY (resists mechanization)

**Nothing.** That's the point. Any judgment call would defeat the "fully
automated, no human" goal. If a decision can't be made mechanically by
the rules above, the setup doesn't fire.

This is what makes OS a different beast from DITP (lots of discretionary
chart reading) and GUNS (catalyst classifier has fuzzy NLP edges).

## 11. Implementation status

| Setup | Status | Code | Notes |
|------|--------|------|-------|
| 1 — OS Breakout (`os_breakout`) | wired, paper-eval | `strategy/OS/os_breakout/impl.py` v1.0.0 | ON + ARMED in paper mode. Will accumulate journal data for 30 days before expectancy review. |

## 12. Glossary

- **OS** — Opening Surge. The family name.
- **Opening surge** — the first 15-30 minutes of RTH on a stock that's
  been moving aggressively in pre-market. High volatility, high volume,
  often retraces 30-50 % of the move within the first hour.
- **Auto-arm** — strategy starts the day ARMED with no human approval
  step. The 30-day paper-eval is the validation gate before this goes
  near real money.
- **PMH consolidation** — same as in GUNS: the last 15 min of PM bars
  trading within a band of the PMH. Indicates accumulation before the
  break vs a "flush up" that may exhaust.
