---
name: guns-bot
version: 1.0.0
description: Fully-automated paper-trading bot that runs a 5-minute Opening Range Breakout (ORB) on "Stocks in Play" per Zarattini, Barbon & Aziz (2024). The bot waits for the first 5 minutes of the session, picks the top 5–20 names from IBKR's live scanner union (TOP_PERC_GAIN ∪ TOP_VOLUME_RATE ∪ HOT_BY_VOLUME), and submits long or short stop-limit + OCO bracket orders based on the direction of the OR bar. Stop at the opposite end of the OR, take-profit at 10R, force-close all positions at 15:58 ET. Strict risk rules enforced at startup: 1% NLV risk per trade, 10R take-profit, refuses to launch if violated. Local dashboard at http://localhost:8000. Trigger phrases include "run orb", "start orb bot", "orb paper trade today".
---

# ORB Bot — 5-Min Opening Range Breakout on Stocks in Play

**Version:** 1.0.0 — 2026-05-20

Paper-trading bot implementing the strategy from Zarattini, Barbon & Aziz (2024), *"A Profitable Day Trading Strategy For The U.S. Equity Market"* ([SSRN](https://papers.ssrn.com/sol3/Delivery.cfm/4729284.pdf?abstractid=4729284&mirid=1), [Concretum implementation](https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/)).

**Paper-only.** Trades route through the `alpaca-trader-paper` sibling skill, which hard-refuses any non-paper base URL. Going live requires a deliberate code change there — not a config flip.

**Armed by default = NO.** The bot launches in dry-run mode unless you've explicitly clicked the **Arm** pill on the dashboard. Disarmed = signals evaluate + log, no Alpaca submissions.

## Strategy in 8 rules

```
1.  Universe:    top N "Stocks in Play" from the IBKR scanner union
                 (TOP_PERC_GAIN ∪ TOP_VOLUME_RATE ∪ HOT_BY_VOLUME minus HALTED)
2.  Wait:        first orb_minutes (=5) of RTH (09:30-09:35 ET)
3.  Direction:   if close > open of 5-min OR bar → LONG bias
                 if close < open of 5-min OR bar → SHORT bias
                 if neutral → skip
4.  Entry:       stop-limit at the OR boundary (1¢ past, 3¢ limit slip)
                 - long: BUY stop at OR_high + $0.01, limit + $0.03
                 - short: SELL stop at OR_low - $0.01, limit - $0.03
5.  Stop loss:   opposite end of the 5-min OR (long: OR_low; short: OR_high)
6.  Profit tgt:  10R (entry ± 10 × risk_per_share)
7.  Position:    1% NLV risk per trade, 10% NLV notional cap (whichever binds)
8.  Time exit:   entry cutoff 15:00 ET (cancel unfilled)
                 EOD sweep 15:58 ET (close everything, cancel all orders)
```

Reported numbers from the paper: **Sharpe 2.81, 1,600% cumulative return over 2016-2023** on the Stocks-in-Play portfolio (vs ~198% for SPY over the same window).

## Strict risk rules (enforced at startup)

Hard limits the bot refuses to violate. `trade_day.py:_validate_strict_rules` exits non-zero if config drifts.

| Rule | Value | Notes |
|---|---|---|
| `risk_per_trade_pct` | **≤ 1%** of NLV | Maximum dollar risk per position |
| `take_profit_R` | **== 10.0** | Exact 10R asymmetric payout per the paper |
| `max_position_pct` | 10% of NLV | Soft cap, also enforced by Alpaca's downstream risk guard |
| `max_open_concurrent_positions` | 5 | Across all live entries |

To loosen these, edit the constants at the top of [trade_day.py](scripts/trade_day.py) — the friction is the point.

## File layout

```
guns-bot/
├── SKILL.md                          # this file
├── requirements.txt                  # alpaca-py, python-dotenv, pytz, ib_insync, fastapi, uvicorn
├── config.json                       # bot knobs (gitignored)
├── config.example.json               # template
├── start_dashboard.bat               # Windows entry point
├── stop_dashboard.bat                # graceful POST /shutdown then force-kill
├── _supervise_dashboard.bat          # supervisor loop — relaunches on exit code 100 (Restart)
├── scripts/
│   ├── _common.py                    # env loader, ET clock, paper-only guard, data abstraction, Telegram
│   ├── _ibkr_data.py                 # IBKR data adapter (ib_insync); read-only enforced
│   ├── _events.py                    # tiny emit() helper -> state/events_*.jsonl
│   ├── _smoke_ibkr.py                # bare-socket TWS handshake smoke test
│   ├── _dryrun_ibkr.py               # exercise the data adapter
│   ├── signals.py                    # evaluate_orb_breakout + utilities (EMAs, splitting, sizing)
│   ├── scanner_observe.py            # IBKR scanner subscriber — feeds the ORB universe
│   ├── trade_day.py                  # main bot — phase_orb + phase_eod_close_all
│   ├── dashboard.py                  # FastAPI + WebSocket dashboard server
│   ├── setup_dashboard_launcher.py   # one-shot: create Desktop shortcuts
│   ├── setup_gateway_autostart.py    # one-shot: register IBKR gateway auto-start
│   ├── setup_ibkr.py                 # one-shot: write ibkr secrets / config
│   └── setup_schedule.py             # one-shot: Windows Task Scheduler hook
├── web/
│   └── index.html                    # the dashboard UI
├── ibc/                              # Interactive Brokers IBC launcher config
└── state/                            # runtime artifacts (gitignored)
    ├── events_<date>.jsonl           # structured event log
    ├── fills_<date>.jsonl            # order submissions + fills + exits
    ├── bot_<date>.log                # trade_day.py stdout/stderr
    ├── scanner_<date>.log            # scanner_observe.py stdout/stderr
    └── equity_<date>.json            # opening + closing NLV
```

## Subprocess architecture

The dashboard launches just **two children** on `/bot/start` (or at the configured `auto_start_et` on weekdays):

1. **trade_day.py** (the ORB engine) — runs from launch through 15:58 ET
   - Validates strict rules at startup, refuses to launch if violated
   - Snapshots opening NLV
   - Sleeps until 09:35 ET, then runs `phase_orb`:
     - Reads `scanner.snapshot` events for "Stocks in Play"
     - Fetches first 5 1-min RTH bars per symbol via the IBKR data adapter
     - Builds long/short bracket plans, submits stop-limit + OCO
     - Continuous management: poll fills → attach OCO → move stop to BE at 1R → poll for TP/SL completion
     - At 15:00, cancels any unfilled entries
   - Runs `phase_eod_close_all` at 15:58 — calls `tc.close_all_positions(cancel_orders=True)` so NOTHING carries overnight

2. **scanner_observe.py** — subscribes in parallel to 7 IBKR scanners (TOP_PERC_GAIN, TOP_OPEN_PERC_GAIN, HOT_BY_VOLUME, TOP_STOCK_BUY_IMBALANCE_ADV_RATIO, TOP_VOLUME_RATE, HOT_BY_OPT_VOLUME, HALTED), emits `scanner.snapshot` events every 30s with enriched per-row data (last price, prev close, change %, day volume).

## Running it

**First time on a new machine:**
```
git pull
py -m pip install -r requirements.txt
py scripts/setup_dashboard_launcher.py     # Desktop shortcuts
```

**Daily:**
- Auto-start at 09:00 ET on weekdays if `auto_start_enabled: true` in config.json
- Or: double-click the **GUNS Dashboard** Desktop shortcut
- Dashboard opens at http://localhost:8000

**Arming for live paper orders:**
- Click the **DISARMED** pill in the dashboard header — flips to **ARMED**
- Next bot launch will submit real paper orders to Alpaca
- Already-running bot picks up the new state at its *next* session

## What this bot does NOT do (intentional gaps)

- **No daily-chart filter** — the paper's edge comes from per-day selection (Stocks in Play) not multi-day trend filters.
- **No Level 2 gate** — your IBKR sub is IEX-only L2; not representative for cross-exchange depth. The paper doesn't require L2.
- **No float check** — the paper studies *liquidity* (high-volume gappers); float is an indirect proxy already captured by `TOP_VOLUME_RATE`.
- **No NYSE holiday calendar** — auto-start fires on weekdays without checking. On a holiday, the bot launches, finds no data, idles, exits at 15:58. Harmless but noisy.

## Telemetry

Live event log: `state/events_<date>.jsonl`. Key event types:

| Event type | When |
|---|---|
| `scanner.subscribed` | scanner_observe connects to a scan code |
| `scanner.snapshot` | every 30s — full top-N per scanner |
| `scanner.emit` / `scanner.drop` | per-symbol new entrant / drop-out |
| `entry_submitted` | phase_orb submits a stop-limit |
| `entry_filled` | Alpaca filled the entry; OCO will attach next |
| `oco_attached` | TP + SL bracket live |
| `stop_to_breakeven` | 1R reached, stop moved to entry |
| `exit_tp_filled` / `exit_sl_filled` | bracket leg completed |
| `entry_canceled_time_cutoff` | unfilled entry killed at 15:00 |
| `eod_close_all` | per-symbol EOD sweep result |

## References

- Zarattini, C., Barbon, A., & Aziz, A. (2024). *A Profitable Day Trading Strategy For The U.S. Equity Market*. SSRN. https://papers.ssrn.com/sol3/Delivery.cfm/4729284.pdf?abstractid=4729284&mirid=1
- Concretum Group implementation: https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/
- Concretum Python backtest walkthrough: https://concretumgroup.substack.com/p/how-to-backtest-a-orb-strategy-in
