"""Shared Alpaca client factory.

Enforces the paper-trading-only invariant: refuses to construct any client
unless ALPACA_BASE_URL is the Alpaca paper endpoint. Going live requires a
deliberate code change here, not a config flip — that is intentional.
"""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
SKILL_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = SKILL_DIR / ".env"
CONFIG_PATH = SKILL_DIR / "config.json"

DEFAULT_CONFIG = {
    "max_position_pct": 0.10,
    "max_open_positions": 10,
    "default_time_in_force": "day",
    "trade_log_path": "trade_log.jsonl",
}


def load_config():
    """Load risk/runtime config, falling back to defaults if config.json is absent."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text())
            cfg.update(user_cfg)
        except json.JSONDecodeError as e:
            sys.exit(f"config.json is not valid JSON: {e}")
    return cfg


def load_credentials():
    """Read .env, validate paper endpoint, return (key, secret)."""
    if not ENV_PATH.exists():
        sys.exit(
            "No .env file found. Run setup first:\n"
            f"  python {SKILL_DIR / 'scripts' / 'setup.py'}"
        )
    load_dotenv(ENV_PATH, override=True)

    base_url = os.getenv("ALPACA_BASE_URL", PAPER_BASE_URL)
    if base_url != PAPER_BASE_URL:
        sys.exit(
            f"Refusing to run.\n"
            f"  ALPACA_BASE_URL is '{base_url}'\n"
            f"  but this skill is paper-only and requires '{PAPER_BASE_URL}'."
        )

    key = os.getenv("ALPACA_API_KEY_ID")
    secret = os.getenv("ALPACA_API_SECRET_KEY")
    if not key or not secret:
        sys.exit("ALPACA_API_KEY_ID or ALPACA_API_SECRET_KEY missing in .env")
    return key, secret


def trading_client():
    """Return an alpaca-py TradingClient pinned to paper."""
    from alpaca.trading.client import TradingClient

    key, secret = load_credentials()
    return TradingClient(key, secret, paper=True)


def market_data_client():
    """Return an alpaca-py StockHistoricalDataClient (data endpoint is shared)."""
    from alpaca.data.historical import StockHistoricalDataClient

    key, secret = load_credentials()
    return StockHistoricalDataClient(key, secret)
