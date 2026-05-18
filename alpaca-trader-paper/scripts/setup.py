"""Interactive first-run setup.

Prompts for Alpaca paper API key + secret, validates them with a live test
call, confirms the account is a paper account, and writes them to .env.
"""
import os
import sys
from getpass import getpass
from pathlib import Path

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _envpath import env_path
ENV_PATH = env_path(SKILL_DIR, "alpaca")


def prompt_keys():
    print("Alpaca Trader (paper) — Setup")
    print("=" * 40)
    print("Get your keys from:")
    print("  https://app.alpaca.markets/paper/dashboard/overview")
    print(f"They will be saved to: {ENV_PATH}")
    print()

    if ENV_PATH.exists():
        ans = input(".env already exists. Overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted — existing .env left untouched.")
            sys.exit(0)

    key = input("ALPACA_API_KEY_ID: ").strip()
    if not key:
        sys.exit("Empty key — aborting.")
    secret = getpass("ALPACA_API_SECRET_KEY (input hidden): ").strip()
    if not secret:
        sys.exit("Empty secret — aborting.")
    return key, secret


def validate_keys(key, secret):
    print("\nTesting credentials against Alpaca paper API...")
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        sys.exit(
            "alpaca-py is not installed. Install it first:\n"
            f"  pip install -r {SKILL_DIR / 'requirements.txt'}"
        )
    try:
        client = TradingClient(key, secret, paper=True)
        account = client.get_account()
    except Exception as e:
        sys.exit(f"Credential test failed:\n  {type(e).__name__}: {e}")

    print(f"  account_number : {account.account_number}")
    print(f"  status         : {account.status}")
    print(f"  buying_power   : ${float(account.buying_power):,.2f}")
    print(f"  equity         : ${float(account.equity):,.2f}")

    # Paper accounts on Alpaca conventionally start with "PA". Warn if not.
    if not str(account.account_number).startswith("PA"):
        print()
        print("  WARNING: account_number does not start with 'PA' (typical paper")
        print("  account prefix). Double-check this is your paper account.")
        ans = input("  Continue anyway? [y/N]: ").strip().lower()
        if ans != "y":
            sys.exit("Aborted.")


def write_env(key, secret):
    content = (
        f"ALPACA_API_KEY_ID={key}\n"
        f"ALPACA_API_SECRET_KEY={secret}\n"
        f"ALPACA_BASE_URL={PAPER_BASE_URL}\n"
    )
    ENV_PATH.write_text(content)
    try:
        # chmod 600 — best-effort. Windows largely ignores POSIX modes; the
        # main protection is that .env is gitignored.
        os.chmod(ENV_PATH, 0o600)
    except Exception:
        pass
    print(f"\nSaved credentials to {ENV_PATH}")
    print("Setup complete. You can now run any of the other scripts.")


def main():
    key, secret = prompt_keys()
    validate_keys(key, secret)
    write_env(key, secret)


if __name__ == "__main__":
    main()
