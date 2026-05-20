---
name: guns-bot
version: 0.5.0
description: Fully-automated paper-trading bot that runs Adam Khoo's Gap Up News Scalp (GUNS) strategy on Alpaca paper, driven by parallel IBKR market scanners across PM, RTH, and closing windows. The dashboard auto-starts the trading session at 09:00 ET on weekdays; 7 IBKR scanners (TOP_PERC_GAIN, TOP_OPEN_PERC_GAIN, HOT_BY_VOLUME, TOP_STOCK_BUY_IMBALANCE, TOP_VOLUME_RATE, HOT_BY_OPT_VOLUME, HALTED) feed scanner-driven plan builders that surface Setup 1, Setup 4, Setup 5, and Setup 6 candidates. Setup 1/5 fire orders to Alpaca paper once "armed"; Setup 4 (intraday bull flag) and Setup 6 (closing-bell breakout) are observe-only pending live-execution wiring. Single browser dashboard at http://localhost:8000 shows everything in real time. Trigger phrases include "run guns", "start guns bot", "guns paper trade today", "kick off the gap-up bot".
---

# GUNS — Gap Up News Scalp (Alpaca paper-trading bot)

**Version:** 0.5.0 — 2026-05-20

Fully-automated layer for Adam Khoo's GUNS strategy (Lesson 8), extended beyond the deck's pre-market focus to cover the full session via parallel IBKR scanners. Auto-launches at 09:00 ET on weekdays; runs Setup 1 (PM-high break) and Setup 5 (first 1-min break) for live execution once armed; surfaces Setup 4 (intraday bull flag) and Setup 6 (closing-bell breakout, invented) as observe-only candidates. Single local dashboard at `http://localhost:8000` shows the whole pipeline in real time.

**Paper-only.** Trades route through the `alpaca-trader-paper` sibling skill, which hard-refuses any non-paper base URL. Going live requires a deliberate code change there — not a config flip in this skill.

**Armed by default = NO.** The bot launches in dry-run mode unless you've explicitly clicked the **Arm** pill on the dashboard. Disarmed = signals evaluate + log, no Alpaca submissions.

## Strategy reference (verbatim from Lesson 8)

Pre-market window (9:00–9:25 ET):
- **Universe**: stocks gapping up pre-market.
- **Watchlist filters**: price ≥ $1.50, pre-market volume ≥ 30,000, strong news catalyst (earnings beat, FDA approval, analyst upgrade, clinical-trial pass — avoid acquisitions or low-impact news), low float (< 100M, ideally 10M–20M).
- **Chart filters**: gap is above 9 EMA / 20 EMA / 50 SMA / 200 SMA on daily; pre-market chart consolidating within ~20% of the pre-market high; price above 9 / 20 / 50 EMA on 5-min PM chart.

Entry setups (5 in the deck + 1 invented for the closing-bell window):

| # | Name | Trigger | Order placed | Status |
|---|------|---------|--------------|--------|
| 1 | Break of Pre-Market High | PM consolidates near PM high | Buy-stop-limit 1¢ above PM high, max 3¢ limit slip | **Automated (live when armed)** |
| 2 | Break of Pre-Market Pivot | PM consolidates at pivot ≥ 1R below PM high | Same mechanic, around the pivot | Manual / future |
| 3 | New High of PM Bull Flag | PM bull-flag exists, 1R fits before PM high | Buy-stop-limit above last pullback candle | Manual / future |
| 4 | First Bull Flag (intraday) | After open, 1-min thrust → 30–62% pullback → consolidation above EMA9/20 with volume confirm | Buy-stop-limit 1¢ above last-3-bar high | **Automated detector (observe-only)** |
| 5 | Break of First 1-Min Candle | First 1-min candle bullish, closes above 9/20 EMA, not abnormally large | Buy-stop-limit 1¢ above 1-min high | **Automated (live when armed)** |
| 6 | Closing-Bell Breakout *(invented)* | Up ≥ 3% on day, close in upper-quartile of intraday range, last-bar vol ≥ 2× 30-bar avg, broke 5-bar consol high, above EMA9/20 | Buy-stop-limit above last bar; **force MOC exit at 15:58 ET** | **Automated detector (observe-only)** |

Exit mechanics (uniform across all 5 setups):
- **Stop loss** (sell-stop) — for setups 1/2/3 placed by price band:
  - < $20 → 10–15¢
  - $20–30 → 15–20¢
  - $30–50 → 20–30¢
  - $50–100 → 30–50¢
  - Or 1× ATR of the 1-min candle
  - Setup 4 / 5 → 1¢ below the trigger candle's low
- **Take profit** (sell-limit) — 2R to 2.5R above entry. This bot uses 2R (the conservative end).
- **Trade management** — once 1R unrealised, move stop to entry (breakeven).
- **Time stop** — cancel any unfilled entry by 10:30 ET; close any open position by 11:00 ET (intraday scalp, not a swing).

Hardware/account in the deck: $25K min (PDT), Level 2 quotes, hot buttons. The bot runs on Alpaca paper (PDT-exempt) and **skips Level 2 gates** — Alpaca paper doesn't expose a usable depth-of-book feed. This is a known accuracy gap; flagged below.

## What this bot does NOT do (and why)

These are intentional gaps. The bot prints the reason in the EOD report so the user knows what was skipped.

- **No Setup 2 / 3** — these require explicit pivot detection / multi-candle bull-flag pattern recognition on PM data. Deferred until we have N sessions of Setup 4 observe-only data to validate the harder patterns.
- **Setup 4 / Setup 6 do NOT submit orders yet** — both are observe-only via `intraday_intake.py`. They log qualifying candidates to `state/intraday_candidates_<date>.jsonl` and emit dashboard events so you can see what they'd have traded. Wiring them into `trade_day.py`'s order-submission path is the next deliberate step, after a few sessions of comparing detector hits against price action.
- **No Level 2 gate** — Alpaca paper feeds a top-of-book quote, not a usable order-book depth. The deck uses L2 to confirm tight spreads and absence of large asks before entering Setup 4/5. The bot substitutes a spread check (bid-ask spread ≤ 10¢) using the top-of-book quote.
- **No daily-chart EMA filter (9 / 20 / 50 / 200 EMA on daily)** — the deck wants the gap to open above all four daily MAs. Requires per-symbol daily-bar fetches that would bump up against IBKR's 60-request-per-10-minute historical pacing limit unless we cache. Skipped for MVP; flagged for follow-up.
- **No float check** — Alpaca doesn't expose shares-outstanding; IBKR's free scanner data doesn't either. Approximation via Finviz exists but only if `FINVIZ_*` credentials are set. The Finviz MCP could be wired in as a follow-up.
- **No NYSE holiday calendar** — auto-start fires on weekdays without checking the trading calendar. On a holiday, the bot launches, finds no data, and idles. Harmless but noisy. Worth a fix once we have `pandas_market_calendars` or a hard-coded list.

### Known bugs / sharp edges

- **Position size has no notional cap.** `signals.position_size()` currently only caps by risk (`equity × risk_pct / risk_per_share`), not by notional exposure. For high-priced stocks like NVDA ($200+) with $1 stop distances, this can produce qtys whose notional value exceeds the entire account (e.g., 500 shares × $220 = $110K on a $100K account). Alpaca's risk guard in `alpaca-trader-paper/scripts/risk.py` will reject these at submission time (`max_position_pct` default 10% of equity), so live orders just won't fill — but the dry-run output looks misleadingly fine. **Fix planned:** cap `qty` at `(equity × max_position_pct) / entry_price` as a second constraint inside `position_size()`. Until that lands, treat the dry-run qty as an over-estimate.

## File layout

```
guns-bot/
├── SKILL.md                          # this file
├── requirements.txt                  # alpaca-py, python-dotenv, pytz, ib_insync, fastapi, uvicorn
├── config.example.json               # copy to config.json to override risk knobs
├── .gitignore
├── start_dashboard.bat               # Windows entry point — opens dashboard + browser
├── stop_dashboard.bat                # graceful POST /shutdown then force-kill
├── _supervise_dashboard.bat          # supervisor loop — relaunches on exit code 100 (Restart)
├── scripts/
│   ├── _common.py                    # env loader, ET clock, paper-only guard, data abstraction, Telegram
│   ├── _ibkr_data.py                 # IBKR data adapter (ib_insync); read-only enforced
│   ├── _events.py                    # tiny emit() helper -> state/events_*.jsonl
│   ├── _smoke_ibkr.py                # bare-socket TWS handshake smoke test
│   ├── _dryrun_ibkr.py               # exercise the data adapter against SPY/AAPL/NVDA
│   ├── signals.py                    # Setup 1/4/5/6 detectors, EMA/ATR helpers, sizing
│   ├── scan_premarket.py             # legacy manual/Alpaca-news watchlist builder (still works)
│   ├── scanner_observe.py            # 7-scanner parallel IBKR subscription (sees PM/RTH/close)
│   ├── auto_plan.py                  # scanner-driven plan builder (PM 09:00-09:24, replaces manual watchlist)
│   ├── intraday_intake.py            # Setup 4 / Setup 6 detector (observe-only, 09:32-15:58)
│   ├── trade_day.py                  # session orchestrator (Setup 1/5 live, re-reads plan at 09:25)
│   ├── dashboard.py                  # FastAPI + WS local dashboard + BotManager (co-launches all 4 children)
│   ├── setup_schedule.py             # register a Windows Task Scheduler job (legacy)
│   ├── setup_dashboard_launcher.py   # per-PC installer: drops Desktop shortcuts via PowerShell COM
│   ├── setup_ibkr.py                 # one-time IBKR wizard (API toggle + IBC config + smoke test)
│   └── setup_gateway_autostart.py    # register Windows scheduled tasks for IBC start/stop
├── web/                              # dashboard UI (vanilla HTML + Tailwind CDN)
│   └── index.html
├── ibc/                              # IBC binaries + config; credentials.txt gitignored
└── state/                            # gitignored
    ├── armed.flag                    # presence = bot launches in LIVE mode; absence = DRY-RUN
    ├── auto_started_YYYY-MM-DD.flag  # idempotency marker for the 09:00 ET auto-start
    ├── watchlist_YYYY-MM-DD.txt      # legacy manual watchlist (still readable as fallback)
    ├── plan_YYYY-MM-DD.json          # per-ticker entry/stop/target levels (written by auto_plan)
    ├── intraday_candidates_*.jsonl   # Setup 4/6 detector hits (one row per candidate)
    ├── fills_YYYY-MM-DD.jsonl        # append-only: fills / cancels / BE-moves
    ├── equity_YYYY-MM-DD.json        # opening + closing equity snapshot
    ├── events_YYYY-MM-DD.jsonl       # structured event bus (scanner snapshots, plan refreshes, etc.)
    ├── bot_YYYY-MM-DD.log            # trade_day.py stdout
    ├── scanner_YYYY-MM-DD.log        # scanner_observe.py stdout
    ├── autoplan_YYYY-MM-DD.log       # auto_plan.py stdout
    └── intraday_YYYY-MM-DD.log       # intraday_intake.py stdout
```

## Full automation pipeline

When you click the **GUNS Dashboard** desktop shortcut, you start ONE
process — the dashboard server. The dashboard's auto-start loop fires the
trading session at **09:00 ET** on weekdays (configurable), spawning four
child subprocesses that work together:

```
                  DASHBOARD (uvicorn, port 8000)
                         │
                         │ 09:00 ET weekday  →  BotManager.start()
                         ▼
       ┌─────────────────┼─────────────────┬─────────────────┐
       ▼                 ▼                 ▼                 ▼
  trade_day.py    scanner_observe.py   auto_plan.py    intraday_intake.py
  orchestrator    7 parallel IBKR      PM 09:00-09:24  Setup 4 / Setup 6
  Setup 1 / 5     scanners (clientId   plan builder    observe-only
  live execution  80)                  (writes plan_*) (09:32-15:58)
       │                 │                 │                 │
       │                 │ scanner.snapshot events           │
       │                 └────────►  state/events_*.jsonl ◄──┤
       │                                   │
       │                                   ▼
       │              state/plan_<date>.json   state/intraday_candidates_*.jsonl
       │  ▲                                                  │
       │  │ re-read at 09:25 ET                              │ emits
       │  └──────────────────────────────────────────────────┘ setup4/6.candidate
       ▼
  phase_setup1 (09:25) → phase_setup5 (09:31) → phase_manage (09:31-10:30)
   → phase_entry_cutoff (10:30) → phase_force_close (11:00) → Telegram EOD
       ▼
  Alpaca paper (HTTPS) — execution layer, never IBKR
```

Each subprocess is independent and logs to its own `state/<name>_<date>.log`.
The dashboard tracks all four PIDs and exposes Stop / Restart / Shut down
via the UI dropdown.

## Scanner layer

`scripts/scanner_observe.py` opens **7 parallel IBKR `ScannerSubscription`s**
on clientId 80 (distinct from the bot's 71 and the dashboard health probe's
99). Each subscription emits its own `scanner.snapshot` events into
`state/events_*.jsonl` with the `scan_code` in the payload — about every 30
seconds.

| Scanner | Role | Best window | Lead time |
|---|---|---|---|
| `TOP_PERC_GAIN` | PM baseline (vs prior close) | 04:00–09:30 | lagging baseline |
| `TOP_OPEN_PERC_GAIN` | RTH baseline (vs today's open) | 09:30–15:30 | lagging baseline |
| `HOT_BY_VOLUME` | late-RTH cumulative volume | 14:30–15:50 | lagging baseline |
| `TOP_STOCK_BUY_IMBALANCE_ADV_RATIO` | closing-auction queued orders | 15:50–15:58 | **5–10 min lead** ⭐ |
| `TOP_VOLUME_RATE` | always-on: volume velocity | all session | **30s–5 min lead** |
| `HOT_BY_OPT_VOLUME` | always-on: options-flow tell | all session | **minutes to hours** |
| `HALTED` | always-on: reject set | all session | defensive |

**Parallel-plus-smart-consumer**, not rotated single scanner. Empty scanners
outside their natural window (auction imbalance pre-15:50, open-perc
pre-09:30) cost nothing — they emit empty snapshots that consumers skip.
The leading scanners surface emerging movers *before* a stock climbs the
lagging % gainer lists.

Scanners that need a data subscription the account lacks (e.g.
`HOT_BY_OPT_VOLUME` without options data) skip with a
`scanner.subscribe_failed` event rather than killing the process.

**Hard limit**: IBKR caps active scanner subscriptions at 10 per session.
We use 7 (70% of cap), leaving headroom for ad-hoc scans during development.

## The shortlisting funnel

Two parallel pipelines, same funnel shape, different inputs and final
pattern gate.

### PM funnel — `auto_plan.py` (09:00–09:24 ET)

```
7 IBKR scanners, 20 rows each   →   ~120 ticker mentions
                                          │
                                          ▼
Latest snapshot per scan_code (one pass, read backwards)
                                          │
                                          ▼
UNION of PM-relevant scanners (preserves rank-priority):
  TOP_PERC_GAIN  (primary baseline)
+ TOP_VOLUME_RATE  (leading)
+ HOT_BY_OPT_VOLUME  (leading)      →   ~40-60 unique symbols
                                          │
                                          ▼
SUBTRACT HALTED reject set          →   ~40-58 symbols
                                          │
                                          ▼
CAP at --max-symbols 15             →   15 prioritised candidates
                                          │
                                          ▼
Fetch PM bars (IBKR) + quotes + Alpaca News (one batched call)
                                          │
                                          ▼
REJECT FILTERS:
  • no_pm_bars
  • price_below_1.50
  • pm_volume_below_30k
  • spread_too_wide  (>10¢)
  • news contains acquire/buyout/    →   ~5-10 survivors
    merger/takeover/tender-offer
                                          │
                                          ▼
PATTERN GATE: is_consolidating_near_pm_high()
  (last 30 min of PM closed within 1.5% of PM-high)
                                          │
                                          ▼                  →   0-4 eligible
  state/plan_<date>.json ← trade_day.phase_setup1 at 09:25
```

Re-runs every 60s. Final write at 09:24 is what `phase_setup1` consumes
(trade_day re-reads from disk to pick up the freshest plan).

### Intraday / closing funnel — `intraday_intake.py` (09:32–15:58 ET)

Same funnel shape, different inputs and final pattern gate:

```
09:32-15:50 ET (RTH window, Setup 4):
  Union:  TOP_OPEN_PERC_GAIN + HOT_BY_VOLUME + TOP_VOLUME_RATE + HOT_BY_OPT_VOLUME
  Cap:    12 symbols
  Gate:   evaluate_setup4_bull_flag()
            thrust ≥ 2 × ATR(5)
            30-62% pullback retracement
            last 3 closes ≥ pullback midpoint
            above EMA9 AND EMA20
            last bar volume ≥ 1.2 × 20-bar avg
  Output: setup4.candidate events + state/intraday_candidates_*.jsonl

15:50-15:58 ET (closing window, Setup 6):
  Union:  TOP_STOCK_BUY_IMBALANCE_ADV_RATIO + HOT_BY_VOLUME + TOP_VOLUME_RATE
  Cap:    12 symbols
  Gate:   evaluate_setup6_closing_breakout()
            up ≥ 3% on day
            close in upper-quartile of intraday range
            last-bar volume ≥ 2 × 30-bar avg
            broke 5-bar consolidation high
            above EMA9 AND EMA20
  Output: setup6.candidate events + state/intraday_candidates_*.jsonl
  Exit:   force MOC at 15:58 (the bot would submit market-on-close)
```

5-min per-symbol cooldown prevents re-emitting the same candidate every
cycle. **Both detectors are observe-only right now** — they write candidate
plans but `trade_day.py` does not yet read them to submit orders. Wiring
into the order-submission path is the next deliberate step.

### Cross-scanner confirmation (the leading-signal payoff)

The architecture's main edge is in the union. A stock appearing on
`TOP_PERC_GAIN` alone is a gapper. A stock appearing on **both
`TOP_PERC_GAIN` AND `TOP_VOLUME_RATE`** is a gapper with accelerating volume
— much higher conviction.

Equally important: a stock appearing on **`TOP_VOLUME_RATE` alone** (not yet
a top gainer) is the *early* form of tomorrow's leader. Without the leading
scanners, the bot wouldn't see this name until it climbed the gainer ranks
5–10 minutes later — by which point most of the move is already done.

## Setup detectors (`signals.py`)

All pure functions, unit-testable. No I/O.

| Function | Inputs | Output |
|---|---|---|
| `pm_summary(pm_bars)` | 1-min PM bars | `{pm_high, pm_low, pm_volume, pm_open, pm_last_close, n_bars}` |
| `is_consolidating_near_pm_high(pm_bars, pm_high, window_min, band_pct)` | bars + window | `bool` |
| `build_setup1_plan(symbol, pm_high, summary, R)` | survives PM filters | plan dict |
| `evaluate_setup5_first_minute(first_min, prior_pm, R, symbol)` | first 1-min RTH bar | plan dict or None |
| `evaluate_setup4_bull_flag(intraday_bars, symbol, R, ...)` | trailing 30 1-min RTH bars | plan dict or None |
| `evaluate_setup6_closing_breakout(intraday_bars, open_price, symbol, R, ...)` | full-day 1-min bars + today's open | plan dict or None |
| `position_size(equity, risk_pct, risk_per_share)` | account + risk | `qty: int` |
| `spread_ok(bid, ask, max_cents)` | top of book | `bool` |

Each `evaluate_*` returns a complete plan dict — `entry_stop_trigger`,
`entry_limit`, `stop_loss`, `take_profit` (at `take_profit_R × risk_per_share`)
— plus rich pattern context (retrace %, thrust $, vol ratio, EMA values) for
human review on the dashboard.

## Arm / disarm

Single header pill replaces the prior dry-run toggle:

| State | Pill | Bot launch behavior |
|---|---|---|
| **Disarmed** (default, safe) | grey "○ DISARMED" | Launched with `--dry-run`. Signals evaluate + log; no Alpaca submissions. |
| **Armed** | red "● ARMED" | Launched without `--dry-run`. Real paper orders go to Alpaca. |

Persisted in `state/armed.flag` (file present = armed). Survives dashboard
restarts.

Clicking **Arm** requires a confirm dialog; clicking **Disarm** is one-click
(going to safer state).

**Mid-session arm/disarm**: the currently running bot keeps the mode it was
launched with (surfaced as `launched_armed` in `/bot/status`). The new arm
state applies at the *next* session start. The dashboard shows
"→ applies next start" when the toggle differs from the running mode.

To immediately stop a live armed session: **⏹ Stop bot** button — emergency
abort. Any open positions stay open in Alpaca.

## Auto-start

The dashboard runs an internal auto-start loop that wakes once a minute,
checks `now ET`, and fires `bot.start()` when ALL of:

1. `cfg.auto_start_enabled` is true (default), AND
2. ET wall-clock has crossed `cfg.auto_start_et` (default `09:00`), AND
3. Day-of-week is Mon–Fri, AND
4. `state/auto_started_<date>.flag` does not exist (idempotency), AND
5. The bot is not already running.

When it fires, it writes the flag and emits `session.auto_start`.
Restarting the dashboard later that day doesn't re-trigger — the flag
persists.

**Pre-condition**: the dashboard must be running at 09:00 ET. The Desktop
shortcut spawns the supervisor minimised; leaving it running overnight is
the supported pattern.

**No NYSE holiday calendar yet** — on a holiday the auto-start fires, the
children find no data, and they idle. Harmless but noisy.

## Restart / Exit / Shut down

Header dropdown menu in the dashboard:

| Action | Endpoint | Dashboard | Children (bot/scanner/auto_plan/intraday) |
|---|---|---|---|
| **↻ Restart** | `POST /restart` | exits code 100 → supervisor relaunches | **untouched** — keep running |
| **◼ Exit dashboard** | `POST /shutdown` | exits code 0 | **untouched** — keep running |
| **⏻ Shut down everything** | `POST /shutdown-all` | `bot.stop()` then exits code 0 | terminated |

Restart works because `start_dashboard.bat` spawns `_supervise_dashboard.bat`
in a loop: when the dashboard exits with code 100, the supervisor sees the
exit code and re-launches. Other exit codes are clean stops.

**Children survive a dashboard restart**, but the new dashboard loses their
subprocess handles and reports their status as "stopped" until they exit
naturally. PID-file adoption is a TODO.

## Live dashboard

`scripts/dashboard.py` is a local FastAPI + WebSocket server at
`http://localhost:8000`. Single browser tab; WebSocket auto-reconnect.

**Panels** (top to bottom):

- **Header status bar** (sticky): ET / MYT clocks, session tag, IBKR /
  Alpaca health dots, bot pill, scanner pill, arm pill, auto-start hint,
  ⏹ Stop bot button (when running), Exit dropdown.
- **Today** (sticky): Day P&L (colored), realized, unrealized, equity, BP.
- **Scanner**: ranked rows from the *primary scanner for the current ET
  window* (TOP_PERC_GAIN PM, TOP_OPEN_PERC_GAIN RTH, etc.).
- **Watchlist**: tickers from today's `state/plan_*.json` with
  entry / stop / target.
- **Pending orders / Open positions**: live from Alpaca (10s poll).
- **Intraday candidates**: Setup 4 / Setup 6 detector hits with retrace %,
  thrust, vol ratio context.
- **Today's trades**: from `state/fills_*.jsonl`.
- **Event log** + **Bot log**: structured events and tail of
  `state/bot_*.log`.

**Endpoints**:

| Path | Method | Purpose |
|---|---|---|
| `/` | GET | serves `web/index.html` |
| `/snapshot` | GET | full state for initial load + reconnect |
| `/config` | GET | safe-to-display config (auto-start ET, scanner window) |
| `/bot/status` | GET | `{status, pid, armed, launched_armed}` |
| `/bot/arm` | POST | set/clear `state/armed.flag` — body `{"armed": bool}` |
| `/bot/stop` | POST | SIGTERM all child processes |
| `/shutdown` | POST | kill dashboard only |
| `/shutdown-all` | POST | kill children + dashboard |
| `/restart` | POST | exit code 100 (supervisor relaunches) |
| `/ws` | WS | push: `snapshot`, `state`, `alpaca`, `health`, `event`, `botlog` |

**Event bus** (`scripts/_events.py`): every long-running script in the
session calls `emit("type.name", {payload})` to append a JSON record to
`state/events_<date>.jsonl`. The dashboard tails that file and pushes new
lines to all connected browsers within ~1s. Event types currently emitted:

```
scanner.subscribed / scanner.start / scanner.snapshot / scanner.emit /
scanner.drop / scanner.subscribe_failed / scanner.stop

autoplan.start / autoplan.waiting / autoplan.no_scanner_data /
plan.refresh / plan.candidate.added / plan.candidate.dropped / autoplan.stop

intraday.start / intraday.waiting / intraday.no_scanner_data /
setup4.candidate / setup6.candidate / intraday.error / intraday.stop

session.auto_start

(legacy from earlier versions: info.startup, error.*, fill.*, order.*)
```

Run it standalone if you need to (the Desktop shortcut handles this on
Windows via the supervisor bat):

    py scripts/dashboard.py

### Desktop shortcuts (Windows)

For one-click launching, run the per-PC installer once:

    py scripts/setup_dashboard_launcher.py

Drops two shortcuts on your Desktop:

  - `GUNS Dashboard` — idempotent. Starts the dashboard (if not already
    running) and opens it in your browser. The dashboard runs in a
    minimised cmd window; close it via the in-app **Exit dashboard**
    button or the stop shortcut.
  - `GUNS Dashboard (stop)` — POSTs `/shutdown` and falls back to killing
    the process owning port 8000.

The `start_dashboard.bat` and `stop_dashboard.bat` launchers are committed
(path-portable via `%~dp0`) and sync across PCs via Dropbox. Only the
per-user `.lnk` shortcuts on Desktop are local to each PC — re-run the
installer on each new machine.

### IBKR API probe details

The dashboard health probe is composite: a cheap bind-probe every 3 s
detects whether something owns the API port, and a full ib_insync
handshake every 30 s verifies the accept loop is responsive. The
handshake uses clientId 99 so it does not collide with the bot's
clientId 71. Earlier versions used a raw TCP connect every 3 s — that
left CloseWait sockets piled up in TWS until its accept loop choked.
Don't reintroduce that pattern.

## Data-feed architecture (recommended: manual TWS)

The bot separates **execution** (always Alpaca paper) from **data** (pluggable). Two supported architectures:

### Architecture A — Manual TWS + bot reads via API (recommended)

You open **TWS in paper mode** each morning. The bot connects to that same TWS over the API (port 7497, Read-Only mode). Same paper account serves both: you trade manually in the GUI, the bot reads bars and submits its own trades through Alpaca paper.

```
                                 ┌────────────────────────────────────────────┐
       You                       │  TWS (paper)                               │
   click buttons   ─────────────>│  - GUI for manual trading                  │
   in the GUI                    │  - API listener on 127.0.0.1:7497 (RO)     │
                                 └──────────────────┬─────────────────────────┘
                                                    │ bars / quotes
                                                    ▼
                                 ┌────────────────────────────────────────────┐
                                 │  guns-bot                                  │
                                 │  - reads TWS data (port 7497, read-only)   │
                                 │  - applies GUNS signals                    │
                                 │  - decides what to trade                   │
                                 └──────────────────┬─────────────────────────┘
                                                    │ bracket order
                                                    ▼
                                 ┌────────────────────────────────────────────┐
                                 │  Alpaca paper (port 443 over HTTPS)        │
                                 │  - $100K simulated equity                  │
                                 │  - executes the bot's orders               │
                                 └────────────────────────────────────────────┘
```

**Why this is the recommended path:**

- You keep **one** paper account that's both the manual-practice playground AND the data source for the bot. No coordinating multiple sessions.
- TWS already exposes a usable API — no extra software needed beyond what you launch for manual trading anyway.
- Read-Only API physically blocks the bot from placing IBKR orders. Even if a bug pointed it the wrong way, IBKR's server would reject. The bot can ONLY submit orders to Alpaca paper.
- No IBC, no scheduled Gateway start, no vault credentials, no auto-relogin scripts. You launch TWS once each morning when you sit down to work; the bot's scheduled task fires later and finds TWS already up.

**Tradeoffs:**

- No auto-start. You manually open TWS each session. If you forget, the bot falls back to Alpaca's IEX feed (built-in fallback in `_common.py`) with a Telegram warning — degraded data but no missed trading day.
- TWS uses ~600 MB RAM (Gateway is ~150 MB). Immaterial on any modern machine.

### Architecture B — IB Gateway + IBC (auto-start, deprecated for this project)

The original plan was IBC-managed Gateway with vault-stored credentials and Windows Task Scheduler auto-start. All that code still exists in the repo (`scripts/setup_ibkr.py`, `scripts/setup_gateway_autostart.py`, `ibc/`) and works, but **Architecture A is recommended** for the user who's both learning manually and running the bot. The IBC path is kept available for headless deployments (e.g., bot running on a separate machine where no human is logging in).

If you're choosing between the two:

| | Architecture A (manual TWS) | Architecture B (IBC + Gateway) |
|---|---|---|
| Same paper account as manual trading | ✓ Yes | ✗ No — Gateway takes the session |
| Auto-start at boot | ✗ No (you open TWS manually) | ✓ Yes (Windows Task Scheduler) |
| Setup complexity | Low — 3 toggles in TWS API settings | High — IBC install, vault, scheduled tasks |
| Bot can run unattended for weeks | Only if TWS stays up | Yes |
| RAM usage | ~600 MB (TWS) | ~150 MB (Gateway) |
| Recommended for | The user who's also learning manually | Headless / remote deployments |

The rest of this document assumes Architecture A unless explicitly stated.

### Data provider fallback

Regardless of architecture, if the bot can't reach IBKR at startup (TWS closed, Gateway not running, network drop) it **falls back to Alpaca's IEX feed** for that session — degraded but not dead. The fallback is logged in the Telegram EOD report so you know data quality was reduced.

## Setup (one-time)

### Step 1 — Install Python dependencies

```bash
cd guns-bot
py -m pip install -r requirements.txt
```

### Step 2 — Alpaca paper credentials

The bot **executes** orders through Alpaca paper. You need API keys:

1. Sign up / log in at https://app.alpaca.markets/paper/dashboard/overview
2. On the right panel, click **"Generate New Keys"** and copy both the Key ID and Secret immediately (secret is shown once).
3. Run the sibling skill's setup wizard:
   ```bash
   cd ../alpaca-trader-paper
   py -m pip install -r requirements.txt
   py scripts/setup.py
   ```
4. Paste the keys when prompted. The wizard verifies them with a test call and saves to `.env` (gitignored).

### Step 3 — Configure TWS API (one-time)

Launch TWS, log in to **paper** account. Then:

1. **File → Global Configuration → API → Settings**
2. Tick: ☑ **Enable ActiveX and Socket Clients**
3. Tick: ☑ **Read-Only API** *(critical — physically blocks IBKR orders, leaving Alpaca paper as the only execution path)*
4. Set **Socket port** = `7497` (TWS paper)
5. **Trusted IPs** → Create → `127.0.0.1` → OK
6. Tick: ☑ **Allow connections from localhost only**
7. Click OK, then **fully restart TWS** for settings to take effect

Verify:

```powershell
py -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 7497)); print('TWS API listening on 7497 OK'); s.close()"
```

Then run the full bot data adapter end-to-end (requires `ib_insync` installed via Step 1):

```bash
py scripts/_ibkr_data.py
```

Expected output:

```
Connecting to IBKR at 127.0.0.1:7497 ...
Connected. Managed accounts: ['DUN408540']    ← your paper account ID
Probing AAPL bars via the public helper ...
Probe AAPL: fetched <N> 1-min bars today.
```

### Step 4 — `config.json` should look like this

```jsonc
{
  // ... risk knobs ...
  "data_provider": "ibkr",
  "ibkr_host": "127.0.0.1",
  "ibkr_port": 7497,           // TWS paper. (Gateway paper = 4002.)
  "ibkr_client_id": 71,
  "ibkr_allow_live_port": false,
  "ibkr_app_type": "tws"       // or "gateway" if you're on Architecture B
  // ... other keys ...
}
```

If `data_provider` is `"ibkr"` and TWS isn't running, the bot logs the failure and silently falls back to `"alpaca"` for that session.

### Step 5 — Telegram (optional but recommended)

Reuses the `MATP` sibling skill's Telegram setup. If `../MATP/.env` already has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, this bot picks them up automatically. Otherwise:

```bash
py ../MATP/scripts/setup_telegram.py
```

### Step 6 — Schedule the daily bot run

```bash
py scripts/setup_schedule.py --time 20:55
```

The `--time` value is your **local** wall-clock. See the timezone table in "Session timeline" below for what value to use.

---

### Architecture B setup (legacy — IBC + Gateway, only if you need headless)

Skip this if you're using Architecture A (recommended). The original IBC-based path is documented separately as legacy:

- Install IBC from https://github.com/IbcAlpha/IBC/releases into `guns-bot/ibc/`
- Install offline IB Gateway from https://www.interactivebrokers.com/en/trading/tws-offline-stable.php (the **regular** Gateway installer is auto-updating and won't work with IBC — you need the **offline** one which puts a `jars\` folder under `C:\Jts\ibgateway\<version>\`)
- In IBKR client portal: API → Settings → tick Read-Only API
- Run `py scripts/setup_ibkr.py` — wizard for paths, vault-aware credentials, IBC config patches
- Run `py scripts/setup_gateway_autostart.py` — registers Windows scheduled tasks for IBC start/stop
- Bot connects to Gateway on port 4002

Credentials live in a path you choose (recommend a Cryptomator/VeraCrypt vault). The launcher batch script reads them at runtime via env vars — no secrets in the codebase. See git history of this file for the full IBC security model if you go this route.

## The morning catalyst workflow (the user's job)

The bot can only trade what's in the watchlist. **Curating the watchlist is the human's most important contribution** — bad watchlist, no edge no matter how good the rules are. This routine takes ~25 minutes each weekday morning before US pre-market activity slows down.

### Run timing

Best window is **~19:00–19:30 Malaysia time** (= 7:00–7:30 AM ET). By then US pre-market has been running ~3 hours and most overnight news has hit.

### Step 1 — Pull the raw gapper list (5 min)

Open two tabs:

- **Primary:** https://thestockmarketwatch.com/markets/today.aspx — lists every stock gapping >2% pre-market with the **news catalyst already attached**. Best free source for GUNS.
- **Cross-check:** https://finviz.com/screener.ashx?v=111&s=ta_topgainers — Finviz's pre-market gainers screener.

### Step 2 — Score each gapper on catalyst tier (10 min)

For each gapper above ~5%, classify the news:

| Tier | Catalyst type | Trade it? |
|---|---|---|
| 🟢 **A** | FDA approval, FDA breakthrough designation, Phase 3 trial passes | **Yes — best** |
| 🟢 **A** | Earnings beat WITH raised guidance, or beat + reaffirmed | **Yes** |
| 🟢 **A** | Major contract win (Pentagon, Apple, etc.) | **Yes** |
| 🟡 **B** | Earnings beat (no guidance change), analyst upgrade from major firm | Maybe — only if other criteria are stellar |
| 🟡 **B** | Insider buying, share buyback announcement | Maybe |
| 🔴 **C** | Acquired / being acquired at fixed price | **No** — gap is capped at deal price, no further upside |
| 🔴 **C** | "Strategic partnership" with no revenue impact | **No** — fluff |
| 🔴 **C** | Stock split announcement, dividend hike | **No** — minor catalyst, fades fast |
| 🔴 **C** | "No news" gap (sympathy moves, technical breakouts) | **No** — no fuel |

**Quick disqualifier shortcuts:**

- If the news says "acquired by", "to be acquired", "merger with" → skip
- If you can't find a news article in 30 seconds → assume no news → skip

### Step 3 — Float + chart check on survivors (10 min)

For each Tier A or B catalyst stock, two quick checks.

**Float check** — open `https://finviz.com/quote.ashx?t=<TICKER>`, find the "Shs Float" row:

| Shs Float | Verdict |
|---|---|
| < 20M | 🟢 **Sweet spot** — fast moves, big % swings |
| 20M – 100M | 🟢 Good |
| 100M – 500M | 🟡 OK if catalyst is Tier A |
| > 500M | 🔴 Too heavy — won't move enough on news |

**Chart check** — open daily chart (TradingView or TWS):

| Looking for | What's good | What's bad |
|---|---|---|
| Where today's gap opens | **Above** 9 EMA, 20 EMA, 50 SMA, 200 SMA | Below any of those |
| Recent price action | Multi-day base / consolidation broken by the gap | Coming off a parabolic 100%+ run already |
| Next resistance | Big "window" / open space above current price to next resistance | Strong overhead resistance within 1-2% of pre-market high |

A picture-perfect GUNS chart: stock has been trading sideways for weeks → news drops overnight → gap opens above all moving averages → empty space above to next major level.

### Step 4 — Write the watchlist file (3 min)

By now you've narrowed from ~30 raw gappers to 4-6 finalists. Write them to:

```
guns-bot/state/watchlist_YYYY-MM-DD.txt
```

One ticker per line, optional `# comment` annotation:

```
AVTX  # FDA Phase 3 positive, 18M float, gap +52%
GME   # earnings beat, big float but A catalyst
PLTR  # major Pentagon contract
```

Save. The bot reads this at 20:55 Malaysia time when it starts.

### Worked example

**You see on a typical Monday at 7:00 AM ET:**

| Ticker | Gap | News | Float | Verdict |
|---|---|---|---|---|
| AVTX | +52% | FDA Phase 3 positive readout | 18M | 🟢 PERFECT — A catalyst + sweet-spot float |
| GME | +31% | Earnings beat (Q3) | 305M | 🟡 Float too high for ideal, but A catalyst — include |
| AAPL | +2% | Slight beat, no guidance change | 15B | 🔴 Mega-cap, won't move 5% — skip |
| ZNGA | +18% | Acquired by Take-Two at $9.86/share | 880M | 🔴 Acquisition cap — skip |
| BBBY | +12% | "Reverse split announced" | 800M | 🔴 Technical catalyst, no fuel — skip |

**Watchlist that morning:** just AVTX and GME. Two strong candidates beat six mediocre ones — quality > quantity.

### Step 5 — Let the bot run (passive)

You don't need to be at the keyboard. The scheduled task fires at 20:55 MY, bot runs autonomously to ~23:00 MY. Telegram report arrives when it's done.

---

## Daily run

**Hands-off path (recommended)**: leave the dashboard running. At 09:00 ET
the auto-start loop fires `bot.start()`, which spawns all four children.
You don't open a terminal at all. End-of-day, `trade_day.py` exits
naturally at 11:00 ET after the force-close phase and posts the EOD report
to Telegram (if configured).

**Manual path (testing / one-offs)**: each subprocess can be run by hand
independently — useful for sanity-checking a single piece without spinning
up the full session.

```bash
py scripts/dashboard.py            # just the dashboard server, no bot
py scripts/trade_day.py            # bot only, reads existing plan_*.json
py scripts/trade_day.py --dry-run  # bot in DRY mode (signals + log only)
py scripts/scanner_observe.py      # 7 parallel scanners, write events.jsonl
py scripts/auto_plan.py            # plan builder, reads scanner events
py scripts/intraday_intake.py      # Setup 4/6 detector
py scripts/trade_day.py --fake-now 09:25  # advance the ET clock for testing
```

The dashboard's **Stop bot** button is the safe way to abort a live session.
Alpaca paper positions remain open through the abort and continue to fill
their attached OCO legs.

### Session timeline

The bot uses an **ET-aware internal clock** regardless of host machine timezone. So Phase events fire at the right ET time even if your machine is on Malaysia/Asia/London time.

| ET (US DST) | Malaysia (MY) | Phase | What happens |
|------|------|-------|--------------|
| 09:00 | 21:00 | Watchlist build | Read `state/watchlist_YYYY-MM-DD.txt` (or `--auto-scan`). For each ticker, fetch PM bars (1-min, 04:00–current) and compute PM high, PM volume, spread. Reject tickers failing price ≥ $1.50, PM volume ≥ 30K, spread ≤ 10¢. Write `state/plan_<date>.json`. |
| 09:25 | 21:25 | Setup 1 queue | For each surviving ticker classified as "consolidating near PM high" (last 30 min of PM within 1.5% of PM high), submit a buy-stop-limit entry: stop = PM high + 1¢, limit = stop + 3¢. TIF = day. No bracket — exits are attached after the fill. |
| 09:30 | 21:30 | Open | Watch for Setup 1 fills via polling (Alpaca REST, 2s cadence). On fill, immediately submit OCO: sell-limit @ 2R, sell-stop @ stop_loss_distance. |
| 09:31 | 21:31 | Setup 5 evaluation | For each watchlist ticker without a Setup 1 fill or pending entry, evaluate the just-closed 9:30–9:31 1-min candle: bullish (close > open) AND close > both 9 EMA + 20 EMA AND candle range ≤ 1.5× the prior 20-bar median range. If valid, submit buy-stop-limit at 1¢ above the 1-min high. |
| 09:31–10:30 | 21:31–22:30 | Manage | Continue polling fills every 2s. For each filled position, track 1R level; once unrealised P&L crosses 1R, cancel the current sell-stop and resubmit at entry (breakeven). |
| 10:30 | 22:30 | Time cutoff | Cancel any unfilled entry orders. Open positions remain — their OCO legs handle exit. |
| 11:00 | 23:00 | Force close | Market-close any positions still open. (Intraday scalp policy.) |
| 11:00 | 23:00 | EOD report | P&L per ticker, win/loss, R-multiple realised. Send to Telegram. Exit. |

**During US standard time** (Nov 1 → Mar 8), add **+1 hour** to the Malaysia column above (e.g., market open becomes 22:30 MY instead of 21:30 MY).

**Recommended scheduled-task fire time for Malaysia users:** 20:55 local. This is 5 min before the bot's "watchlist build" phase during DST, and ~1 hour early during US standard time. The early start during standard time is harmless — the bot's internal ET clock makes it sleep until the right ET moment.

### Risk parameters + automation knobs (override in `config.json`)

```json
{
  "max_open_concurrent_positions": 3,
  "risk_per_trade_pct": 0.005,
  "take_profit_R": 2.0,
  "max_setup1_candidates": 4,
  "max_setup5_candidates": 4,
  "spread_max_cents": 10,
  "pm_consolidation_window_min": 30,
  "pm_consolidation_band_pct": 1.5,
  "time_cutoff_entry_et": "10:30",
  "time_cutoff_force_close_et": "11:00",
  "alpaca_skill_path": "../alpaca-trader-paper",

  "auto_start_enabled": true,
  "auto_start_et": "09:00"
}
```

- `auto_start_enabled` — set false to disable the 09:00 ET auto-launch (e.g.
  when developing). The dashboard still runs; the bot just doesn't auto-spawn.
- `auto_start_et` — ET wall-clock for the auto-launch. Default 09:00 (= 30 min
  before NYSE open).

- `risk_per_trade_pct` — fraction of account equity risked per trade (default 0.5%). Position size = `(equity × risk_per_trade_pct) / (entry - stop)`.
- `max_open_concurrent_positions` — hard cap regardless of how many candidates the scan produces.
- `take_profit_R` — 2.0 (deck range: 2.0–2.5). Conservative; raise to 2.5 to match the deck's upper bound.

## Edge cases the bot handles

- **Multiple fills in the same minute** — orders are placed sequentially; before each new entry the bot re-checks `max_open_concurrent_positions`.
- **Buy-stop-limit slipped past the limit** — order stays open until 10:30 cutoff; if never filled, it's cancelled.
- **Partial fills** — the OCO is attached with `qty = filled_qty`; remaining open buy is cancelled at 10:30.
- **Gap-down at the open** (the stock opens below PM high) — Setup 1 buy-stop never triggers; bot evaluates Setup 5 instead at 9:31.
- **Watchlist file missing** — exits cleanly with instructions to either create the file or pass `--auto-scan` / `--watchlist`.
- **Run started after 9:25 ET** — bot logs the missed phases and continues from the current phase. Useful if your scheduler fires late.

## Scheduling

Run by hand the first few days to verify behavior, then automate with Task Scheduler:

```bash
py scripts/setup_schedule.py
```

This registers a daily 8:55 ET Windows scheduled task that invokes `py scripts/trade_day.py`. On macOS/Linux the script prints an equivalent `crontab` line.

## Versioning policy

Bump `version:` in the frontmatter and add a one-line changelog entry whenever this skill changes. Use semver:

- **Patch (x.y.Z)** — doc/wording fixes, no behaviour change.
- **Minor (x.Y.0)** — new setup added, new optional flag, more conservative default that's backward-compatible.
- **Major (X.0.0)** — change in risk math, change in default behaviour, removal of a setup.

## Changelog

- **0.5.0** (2026-05-20) — Full-day automated pipeline. Dashboard now auto-launches the trading session at 09:00 ET weekdays — no terminal commands. `BotManager` co-spawns four subprocesses (`trade_day`, `scanner_observe`, `auto_plan`, `intraday_intake`) that work together via the event bus + plan file. **`scanner_observe.py`** opens 7 parallel IBKR scanners (TOP_PERC_GAIN, TOP_OPEN_PERC_GAIN, HOT_BY_VOLUME, TOP_STOCK_BUY_IMBALANCE_ADV_RATIO, TOP_VOLUME_RATE, HOT_BY_OPT_VOLUME, HALTED) — parallel-plus-smart-consumer architecture, not rotated. **`auto_plan.py`** replaces the manual watchlist: takes the union of PM-relevant scanners, subtracts HALTED, runs price/PM-vol/spread/M&A-keyword filters, writes `plan_<date>.json` every 60s. **`intraday_intake.py`** (observe-only) adds Setup 4 (bull-flag detector — thrust + pullback + EMA hold + volume confirm) for 09:32–15:50 ET and **Setup 6** (closing-bell breakout, invented strategy not in deck — up ≥ 3%, upper-quartile close, vol surge, broke 5-bar high, force MOC exit) for 15:50–15:58 ET. **Arm/disarm replaces the dry-run toggle** (`state/armed.flag`); bot is launched with `--dry-run` unless armed. **Restart/Exit/Shut-down dropdown** in the dashboard header — Restart works via supervisor bat (`_supervise_dashboard.bat`) that re-launches on exit code 100. **IBKR probe fixed**: bind-probe every 3s + ib_insync handshake every 30s on clientId 99, eliminates the CloseWait socket accumulation that wedged TWS. Sticky header + Today P&L strip stay pinned while scrolling. Desktop shortcuts via PowerShell COM (handles AD-redirected Desktops correctly). `trade_day.py` re-reads `plan_<date>.json` just before `phase_setup1` at 09:25 ET so auto_plan's last refresh is picked up. Config additions: `auto_start_enabled` (default true), `auto_start_et` (default "09:00").
- **0.4.0** (2026-05-20) — Local dashboard scaffold. Single-page web UI at http://localhost:8000 with WebSocket push for fills, plan, equity. Tail of `state/events_*.jsonl` becomes the dashboard's event-stream channel. Vault-aware Alpaca env loader so the dashboard works on PCs where the sibling's local `.env` isn't synced.
- **0.3.0** (2026-05-19) — Architecture pivot. Recommended setup is now "Architecture A": the user manually launches TWS in paper each morning, and the bot reads data via TWS's API (port 7497, Read-Only). Same paper account serves both manual trading (clicks in TWS GUI) and the bot (reads bars + submits Alpaca orders) — no session contention because Read-Only API doesn't block GUI trading. IBC + Gateway path ("Architecture B") still works but is now documented as legacy / headless-only. Verified end-to-end dry-run pulled 431 bars of AAPL through TWS on the test session. Other changes: documented the morning catalyst-finding workflow (thestockmarketwatch + Finviz + A/B/C tier system + float/chart check + watchlist file format) — this is the user's daily 25-min routine. Added Malaysia local-time column to session timeline. Flagged a known bug: `position_size()` lacks a notional cap, so dry-run qtys for high-priced tickers (NVDA, TSLA) exceed account equity and would be rejected by Alpaca's `max_position_pct` guard on live submission. Fix planned for v0.3.1.
- **0.2.0** (2026-05-19) — Added IBKR data feed as an alternative to Alpaca IEX (execution still goes through Alpaca paper). New `data_provider` config key dispatches between `_alpaca_*` helpers and `_ibkr_data.py` (ib_insync-based). IBKR connection is enforced to Read-Only API mode and refuses the live port (7496) without explicit acknowledgement. Added `setup_ibkr.py` wizard (paths + IBC credentials + smoke test) and `setup_gateway_autostart.py` (Windows Task Scheduler entries for IBC start/stop). Auto-fallback to Alpaca on IBKR failure, surfaced in the EOD report. Bot can run end-to-end without IBKR set up — `data_provider="alpaca"` is the default.
- **0.1.0** (2026-05-19) — Initial scaffold. Setup 1 + Setup 5 automated, OCO exits, breakeven move at 1R, 10:30 entry cutoff, 11:00 force close, Telegram EOD report. Manual watchlist with optional `--auto-scan`. Level 2 gates and Setups 2/3/4 deferred.
