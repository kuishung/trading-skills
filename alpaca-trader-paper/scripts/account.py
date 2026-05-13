"""Account info CLI.

Usage:
  python scripts/account.py              # human-readable summary
  python scripts/account.py --json       # JSON for strategy skills
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _client import trading_client


def get_account_summary():
    client = trading_client()
    a = client.get_account()
    return {
        "account_number": a.account_number,
        "status": str(a.status),
        "currency": a.currency,
        "cash": float(a.cash),
        "equity": float(a.equity),
        "buying_power": float(a.buying_power),
        "portfolio_value": float(a.portfolio_value),
        "long_market_value": float(a.long_market_value),
        "short_market_value": float(a.short_market_value),
        "daytrade_count": a.daytrade_count,
        "pattern_day_trader": a.pattern_day_trader,
        "trading_blocked": a.trading_blocked,
        "transfers_blocked": a.transfers_blocked,
        "account_blocked": a.account_blocked,
        "shorting_enabled": a.shorting_enabled,
    }


def main():
    parser = argparse.ArgumentParser(description="Show Alpaca paper account summary")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    summary = get_account_summary()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return
    width = max(len(k) for k in summary)
    for k, v in summary.items():
        if isinstance(v, float) and k in {
            "cash", "equity", "buying_power", "portfolio_value",
            "long_market_value", "short_market_value",
        }:
            v = f"${v:,.2f}"
        print(f"{k:>{width}} : {v}")


if __name__ == "__main__":
    main()
