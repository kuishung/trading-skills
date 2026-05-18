---
name: intraday-realtime-monitor
version: 0.1.0
description: Real-time intraday trading brain that subscribes to live US-equities bars via Alpaca IEX WebSocket, evaluates strategy setups on the consensus watchlist produced by intraday-premarket-brief, and submits paper bracket orders via alpaca-trader-paper. Same brain function runs in three modes — live, dry-run, and historical replay — so backtests use literally the same code as live trading. All strategy thresholds are ticker-relative (ATR multiples, volume z-scores, R-multiples) so one rule set adapts to NVDA's $4 ATR and HIMS's $1.50 ATR without hand-tuning. Trigger when the user wants to run the intraday brain, start the realtime monitor, replay a historical date to backtest a setup, or work on intraday strategy code.
---

# Intraday Realtime Monitor

**Version:** 0.1.0 — 2026-05-18

The "brain" of the intraday system. Takes the morning consensus shortlist from `intraday-premarket-brief`, watches their live data during the US session, evaluates setup conditions, and submits paper bracket orders when conditions fire. Same brain code runs against historical data (replay mode) for backtesting.

## Changelog

- **0.1.0** (2026-05-18) — Initial release. Three modes (`--mode live | dry-run | replay`) sharing one `evaluate_setup()` brain function from `strategies/gap_and_go.py`. Per-ticker behavioral profiles (`profiles/<TICKER>.json`) cache ATR, avg minute volume + stddev, premkt range avg, prior close, daily trend — refreshed via `profile_builder.py`. All strategy thresholds normalized to ticker-relative measures (ATR multiples, volume z-scores, R-multiples) so one rule set adapts across volatility regimes. Reads the consensus watchlist from `intraday-premarket-brief/snapshots/<date>_t30.json` by default; `--tickers` override for testing. Replay mode pulls historical 1-min bars from Alpaca REST, walks bars in sequence, simulates bracket outcomes (entry at signal-bar close + slippage, stop/target check on subsequent bars, EOD exit if neither hit). Per-ticker stats emitted from every run so the user can identify which names the strategy actually has edge on. Live mode subprocs to `alpaca-trader-paper/scripts/orders.py bracket` for actual order submission, preserving skill isolation. Decision log written to `runs/<date>_<mode>.jsonl` (every evaluation, not just every trade — captures the "evaluated but rejected" reasons that explain strategy behavior).

## What it does NOT do

- **Pick the watchlist** — that's `intraday-premarket-brief`'s job. This skill consumes the T-30 consensus list as input.
- **Decide WHEN to trade** at the day-shape level (no day-cycle awareness like "avoid the 11am-2pm chop") — that lives inside the strategy file. The dispatcher just feeds bars and trusts the brain.
- **Manage open positions after entry** — bracket orders are submitted to Alpaca; the broker enforces TP/SL. If the script dies, exits are still honored by the broker. This is why we use brackets, not naked entries with separate stop submissions.
- **Live trading** — paper-only via `alpaca-trader-paper`. Going live requires a deliberate code change in that sibling skill, not a config flip.

## Architecture

```
intraday-premarket-brief                 alpaca-trader-paper
        │                                       ▲
   T-30 snapshot                                │ subprocess
        │                                       │ orders.py bracket
        ▼                                       │
┌──────────────────────────────────────────────────────────┐
│  intraday-realtime-monitor                                │
│                                                           │
│  ┌─ profile_builder.py ─┐  refreshes profiles/<TKR>.json  │
│  │  (daily, pre-open)   │  (ATR, avg vol, etc.)           │
│  └──────────────────────┘                                 │
│                                                           │
│  ┌─ monitor.py ─────────────────────────────────────────┐ │
│  │  --mode live       Alpaca WebSocket bars  →  brain   │ │
│  │  --mode dry-run    Alpaca WebSocket bars  →  brain   │ │
│  │  --mode replay     Alpaca REST 1m bars    →  brain   │ │
│  │                                                       │ │
│  │  brain = evaluate_setup(bar, profile, state)          │ │
│  │  from strategies/gap_and_go.py                        │ │
│  │                                                       │ │
│  │  on yes → bracket order (live) / log (dry-run)        │ │
│  │           / simulate fill walk (replay)               │ │
│  └───────────────────────────────────────────────────────┘ │
│                                                           │
│  runs/<date>_<mode>.jsonl   ← every decision logged       │
└──────────────────────────────────────────────────────────┘
```

The three modes are the same code path with a different bar source and a different "what to do on YES" handler. The brain doesn't know which mode it's in — that's the testability invariant.

## Normalized parameters (Option A — non-negotiable)

Every numeric threshold in any strategy file is **ticker-relative**:

| Naive (forbidden in this codebase) | Normalized (required) |
| --- | --- |
| "Stop 1% below entry" | "Stop 0.3 × today's ATR below entry" |
| "Volume > 3M shares in the bar" | "Volume > 2σ above this ticker's typical 1-min volume" |
| "Gap > 2%" | "Gap > 0.7 × ATR%" |
| "Target = +$3" | "Target = entry + N × R (where R = stop distance)" |

Per-ticker baselines live in `profiles/<TICKER>.json` (refreshed daily by `profile_builder.py`). Brain reads the profile at decision time and computes thresholds.

## Files

- `SKILL.md` — this file.
- `scripts/monitor.py` — main dispatcher. Handles `--mode live | dry-run | replay`. Loads watchlist, state, profiles; runs the brain on bars; submits/logs/simulates.
- `scripts/profile_builder.py` — refreshes ticker profiles. Run daily before market open (cron / Task Scheduler / on-demand).
- `scripts/strategies/gap_and_go.py` — first strategy: gap-and-go breakout. The `evaluate_setup()` function the dispatcher calls.
- `scripts/strategies/__init__.py` — makes the strategies dir a package.
- `scripts/_envpath.py` — same Dropbox-VAULT env resolution as sibling skills.
- `profiles/<TICKER>.json` — per-ticker behavioral profile, gitignored.
- `runs/<date>_<mode>.jsonl` — append-only decision log, gitignored.
- `requirements.txt` — `alpaca-py`, `pandas`, `python-dotenv` (already installed via alpaca-trader-paper).
- `.env.example` — template. Reuses Alpaca creds from the vault (`alpaca.env`); only Telegram is skill-local.

## Setup (one time, per machine)

```powershell
# Dependencies (most installed already via sibling skills)
py -m pip install -r requirements.txt

# Build profiles for today's consensus watchlist
py scripts/profile_builder.py

# Configure Telegram (optional — reuses MATP's bot or new one)
# Edit <vault>/intraday-realtime.env directly, or copy from .env.example
```

Reads Alpaca credentials from `<vault>/alpaca.env` (set up via `alpaca-trader-paper/scripts/setup.py`).

## Usage

```powershell
# Replay last Friday on the gap-and-go strategy — backtest, no orders submitted
py scripts/monitor.py --mode replay --date 2026-05-15

# Live monitor in dry-run (no orders submitted, decisions logged)
py scripts/monitor.py --mode dry-run

# Live with paper orders submitted via alpaca-trader-paper
py scripts/monitor.py --mode live

# Override watchlist for testing
py scripts/monitor.py --mode replay --date 2026-05-15 --tickers NVDA,AMD,PLTR

# Refresh profiles before running (recommended daily, pre-open)
py scripts/profile_builder.py --tickers NVDA,AMD,PLTR
py scripts/profile_builder.py --from-snapshot     # uses today's T-30 watchlist
```

## Workflow

Standard daily routine:

1. **T-60 / T-30 brief** runs → produces consensus watchlist
2. **Profile builder** auto-runs against that watchlist → refreshes `profiles/`
3. **Replay last 5-30 days** on the consensus tickers if you're testing a new strategy variant
4. **Dry-run live** for a session or two to compare vs replay expectations
5. **Live paper** once dry-run aligns with backtest stats
6. **Weekly review** — `runs/*.jsonl` → per-ticker stats → prune low-edge tickers

## Per-ticker performance is the feedback loop

Normalized parameters mean the same code works across tickers, but it does NOT mean the strategy has edge on every ticker. Every run produces a per-ticker breakdown:

```
Strategy: gap_and_go (replay 2026-04-18 → 2026-05-17)

Ticker   Trades  Win%   Avg R   Total R
─────────────────────────────────────────
NVDA     12      67%   +1.5    +9.8
AMD       8      50%   +0.8    +3.0
PLTR      9      56%   +1.2    +5.1
TSLA      6      17%   -0.6    -2.8       ← strategy doesn't fit; whitelist out
MSFT      3       0%   -1.0    -3.0       ← drop entirely
```

After a few weeks, build a whitelist file in `profiles/whitelist.json` listing tickers where the strategy has positive expectancy. Monitor skips ticker that aren't on the whitelist for that strategy.

## What's NOT in v0.1 (deferred)

- Multiple strategies in parallel (one strategy file for v0.1; multi-strategy dispatcher is v0.2)
- Whitelist file (manual until you have enough data — ~50+ trades per ticker)
- Analytics/journal beyond the raw JSONL log (weekly review script is v0.3)
- Streaming risk overlay (daily loss limit / max positions are gated in the brain; portfolio-level risk daemon is later)
- Auto profile refresh on session start (run manually for now)
