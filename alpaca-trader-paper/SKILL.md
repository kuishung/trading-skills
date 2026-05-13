---
name: alpaca-trader-paper
description: Infrastructure for Alpaca paper trading on US equities and ETFs. Use this skill whenever the user wants to interact with their Alpaca paper-trading account — placing trades (market, limit, stop, stop-limit, bracket), checking account info (buying power, equity, day-trade count), listing or closing positions, canceling orders, fetching quotes or bars, or checking market hours. Trigger phrases include "buy/sell <ticker> on alpaca", "place a paper trade", "check my alpaca account", "what's my buying power", "show my positions", "close my position in X", "cancel my order". Do NOT trigger this skill for strategy generation, signal analysis, backtesting, or chart reading — those are handled by separate strategy skills that delegate execution to this one.
---

# Alpaca Trader (paper-only)

Execution-layer skill for placing and managing paper trades on Alpaca for US equities and ETFs. Strategy skills should delegate trade execution here rather than building order logic themselves.

**Paper trading only.** This skill refuses to run against any base URL other than `https://paper-api.alpaca.markets`. Going live requires a code change, not a config flip — that is intentional.

## First-run setup

If `.env` does not exist in the skill directory, run setup before anything else:

```bash
python scripts/setup.py
```

This prompts the user for their Alpaca paper API key + secret, makes a test call to confirm they work, and saves them to `.env` (gitignored). The user must paste the keys into the interactive prompt themselves — never ask them to share keys in chat.

Get keys from: https://app.alpaca.markets/paper/dashboard/overview

If alpaca-py is not yet installed, also run:
```bash
pip install -r requirements.txt
```

## Common tasks

All scripts are run from the skill root directory. Pass `--json` for machine-readable output (useful when called from strategy skills).

### Account info
```bash
python scripts/account.py
python scripts/account.py --json
```

### Market data
```bash
python scripts/market_data.py quote AAPL
python scripts/market_data.py bars AAPL --timeframe 1Day --limit 30
python scripts/market_data.py clock
```

### Place orders
```bash
python scripts/orders.py market AAPL --qty 10 --side buy
python scripts/orders.py limit AAPL --qty 10 --side buy --limit-price 180.50
python scripts/orders.py stop AAPL --qty 10 --side sell --stop-price 175.00
python scripts/orders.py stop-limit AAPL --qty 10 --side sell --stop-price 175 --limit-price 174.50
python scripts/orders.py bracket AAPL --qty 10 --side buy --take-profit 200 --stop-loss 175
python scripts/orders.py market AAPL --qty 10 --side buy --dry-run
```

### Manage orders
```bash
python scripts/orders.py list
python scripts/orders.py list --status all
python scripts/orders.py cancel <order_id>
python scripts/orders.py cancel-all
```

### Positions
```bash
python scripts/positions.py list
python scripts/positions.py close AAPL
python scripts/positions.py close AAPL --qty 5
python scripts/positions.py close-all
```

## Risk guardrails

Checked before every order. If a check fails, the order is rejected with a clear message and nothing is sent to Alpaca.

- **Paper endpoint only.** `ALPACA_BASE_URL` must be `https://paper-api.alpaca.markets`. Hard refusal otherwise.
- **Max position size.** Default 10% of equity per position. Override per-call with `--max-position-pct` or globally by creating `config.json` (copy from `config.example.json`).
- **Max open positions.** Default 10. Override with `--max-open-positions` or `config.json`.
- **Market hours.** Orders outside regular trading hours are rejected unless `--extended-hours` is explicitly passed. Not all order types support extended hours on Alpaca.
- **Dry run.** `--dry-run` prints the intended request without sending it. Useful for confirming an order before committing.

## Trade log

Every submitted order is appended to `trade_log.jsonl` (one JSON object per line). The log is gitignored. Strategy skills can read it for backtesting comparisons or post-trade analysis.

## Calling from strategy skills

Strategy skills should invoke these CLI scripts via subprocess. Do not import the modules across skill boundaries — keep the interface CLI-shaped so each skill stays independent and the contract is explicit.

Typical strategy skill flow:
1. Get state: `python <skill>/scripts/account.py --json` and `python <skill>/scripts/positions.py list --json`
2. Get market data: `python <skill>/scripts/market_data.py bars TICKER --timeframe 5Min --limit 100 --json`
3. Apply strategy logic
4. Execute: `python <skill>/scripts/orders.py market TICKER --qty N --side buy`

## File layout

- `scripts/setup.py` — interactive credential setup + validation
- `scripts/_client.py` — client factory with paper-only guard (no CLI)
- `scripts/account.py` — account summary
- `scripts/market_data.py` — quotes, bars, market clock
- `scripts/orders.py` — place/list/cancel orders
- `scripts/positions.py` — list/close positions
- `scripts/risk.py` — risk checks (used by orders.py)
- `scripts/trade_log.py` — append-only JSONL log helpers
- `config.example.json` — copy to `config.json` to override risk defaults
- `requirements.txt` — Python dependencies
