# alpaca-trader-paper

Execution-layer skill for placing and managing **paper trades** on [Alpaca](https://alpaca.markets) for US equities and ETFs. Designed to be the trade-execution backbone for separate strategy skills — each strategy decides *what* to trade; this skill handles *how*.

> **Paper trading only.** This skill refuses to run against any base URL other than `https://paper-api.alpaca.markets`. Switching to live trading is intentionally a code change, not a config flip.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

(Or with `uv`: `uv pip install -r requirements.txt`)

### 2. Get your Alpaca paper API keys

Sign up at https://alpaca.markets, then go to https://app.alpaca.markets/paper/dashboard/overview and click **"View"** under API Keys. You will see:
- Key ID — looks like `PK...`
- Secret Key — long random string

### 3. Run the interactive setup

```bash
python scripts/setup.py
```

You will be prompted to paste the key + secret. The script will:
1. Make a test API call to confirm the keys work
2. Confirm the account is a paper account
3. Save credentials to `.env` in this directory (gitignored, chmod 600 on Unix)

After this, every other script will read from `.env` automatically.

---

## Usage

All commands run from the skill root directory. Add `--json` to most commands for machine-readable output.

### Account

```bash
python scripts/account.py                 # human-readable summary
python scripts/account.py --json          # JSON
```

### Market data

```bash
python scripts/market_data.py quote AAPL
python scripts/market_data.py bars AAPL --timeframe 1Day --limit 30
python scripts/market_data.py bars AAPL --timeframe 5Min --limit 100 --json
python scripts/market_data.py clock
```

Supported timeframes: `1Min`, `5Min`, `15Min`, `1Hour`, `1Day`.

### Orders

```bash
# Market
python scripts/orders.py market AAPL --qty 10 --side buy
python scripts/orders.py market AAPL --qty 10 --side buy --dry-run

# Limit
python scripts/orders.py limit AAPL --qty 10 --side buy --limit-price 180.50

# Stop
python scripts/orders.py stop AAPL --qty 10 --side sell --stop-price 175.00

# Stop-limit
python scripts/orders.py stop-limit AAPL --qty 10 --side sell --stop-price 175 --limit-price 174.50

# Bracket (entry + take-profit + stop-loss in one order)
python scripts/orders.py bracket AAPL --qty 10 --side buy --take-profit 200 --stop-loss 175

# List / cancel
python scripts/orders.py list
python scripts/orders.py list --status all
python scripts/orders.py cancel <order_id>
python scripts/orders.py cancel-all
```

Common flags on order placement:
- `--time-in-force` — `day` (default), `gtc`, `ioc`, `fok`, `opg`, `cls`
- `--extended-hours` — required for orders outside RTH
- `--max-position-pct` — override the default 10% cap for this order
- `--dry-run` — print the request without sending

### Positions

```bash
python scripts/positions.py list
python scripts/positions.py list --json
python scripts/positions.py close AAPL              # full close
python scripts/positions.py close AAPL --qty 5      # partial close (5 shares)
python scripts/positions.py close AAPL --pct 50     # partial close (50%)
python scripts/positions.py close-all
```

---

## Risk guardrails

These are enforced *before* the order hits Alpaca's API.

| Guardrail | Default | Override |
|---|---|---|
| Paper endpoint only | enforced | code change required |
| Max position size | 10% of equity | `--max-position-pct` or `config.json` |
| Max open positions | 10 | `--max-open-positions` or `config.json` |
| Market hours only | enforced | `--extended-hours` per order |

To set persistent overrides, copy `config.example.json` to `config.json` and edit. `config.json` is gitignored.

---

## Trade log

Every order submission and fill update is appended to `trade_log.jsonl` in this directory (one JSON object per line). Useful for:
- Auditing what the strategy actually did
- Comparing live paper performance against backtest
- Strategy review / improvement

The log is gitignored.

---

## Calling from strategy skills

A strategy skill should treat this skill as a black-box execution venue: invoke the CLI scripts via subprocess, do not import internals across skill boundaries. This keeps each skill independent and the contract explicit.

Typical flow:

```bash
# 1. Snapshot state
ACCOUNT=$(python ../alpaca-trader-paper/scripts/account.py --json)
POSITIONS=$(python ../alpaca-trader-paper/scripts/positions.py list --json)

# 2. Pull market data
BARS=$(python ../alpaca-trader-paper/scripts/market_data.py bars SPY --timeframe 5Min --limit 200 --json)

# 3. (Strategy logic — your skill decides whether to trade)

# 4. Execute
python ../alpaca-trader-paper/scripts/orders.py market SPY --qty 10 --side buy
```

---

## File layout

```
alpaca-trader-paper/
├── SKILL.md
├── README.md
├── requirements.txt
├── .gitignore
├── config.example.json
└── scripts/
    ├── _client.py        # Client factory + paper-endpoint guard
    ├── setup.py          # First-run interactive credential setup
    ├── account.py
    ├── market_data.py
    ├── orders.py
    ├── positions.py
    ├── risk.py
    └── trade_log.py
```

---

## Disclaimer

This is software for paper-trading. Even paper trading reflects real strategy behavior — review every script before running it, and read the Alpaca API docs at https://alpaca.markets/docs for anything you do not fully understand. The author is not responsible for any losses if you take this code live.
