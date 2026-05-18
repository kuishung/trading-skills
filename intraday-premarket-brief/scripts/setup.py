#!/usr/bin/env python
"""One-time interactive setup for the intraday pre-market brief.

Prompts for:
  1. The Finviz screener URL to scan every run (required).
  2. Telegram bot token + chat ID for --send-telegram (optional, can skip
     and re-run later to add).

Writes to a gitignored .env in the skill root. Preserves any unrelated
keys already in .env (so re-running this doesn't wipe other config).

Usage:
    py scripts/setup.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from _envpath import env_path
ENV_PATH = env_path(SKILL_DIR, "intraday-premarket")


def prompt(label: str, default: str | None = None, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip().strip('"').strip("'")
        if value:
            return value
        if default is not None:
            return default
        if allow_empty:
            return ""
        print("  (required) Please enter a value.")


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def save_env(values: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in values.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tg_get(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "intraday-setup/1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            pass
        return {"ok": False, "error_code": exc.code, "description": body or str(exc)}
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


def setup_finviz(existing: dict[str, str]) -> str:
    print("\n--- 1. Finviz screener URL ---")
    print("Paste the full URL of your intraday-mover screener. Keep the")
    print("query params; the &r=<N> pagination offset is optional (the")
    print("brief paginates itself).\n")
    current = existing.get("INTRADAY_FINVIZ_URL", "")
    if current:
        print(f"Currently configured: {current}")
        change = prompt("Change it? (y/N)", default="N").lower()
        if change != "y":
            return current
    url = prompt("Finviz URL")
    if not url.startswith("https://finviz.com/screener"):
        print("  Warning: URL doesn't start with 'https://finviz.com/screener'.")
        confirm = prompt("Keep anyway? (y/N)", default="N").lower()
        if confirm != "y":
            return setup_finviz(existing)
    return url


def setup_telegram(existing: dict[str, str]) -> tuple[str, str]:
    print("\n--- 2. Telegram (optional) ---")
    print("Configure if you want --send-telegram to push briefs to your phone.")
    print("Skip with empty input — you can re-run setup.py later to add it.\n")

    current_token = existing.get("TELEGRAM_BOT_TOKEN", "")
    current_chat = existing.get("TELEGRAM_CHAT_ID", "")
    if current_token and current_chat:
        print(f"Currently configured: token ...{current_token[-6:]}, chat_id {current_chat}")
        change = prompt("Change it? (y/N)", default="N").lower()
        if change != "y":
            return current_token, current_chat

    print("Quick setup walkthrough:")
    print("  1. In Telegram, chat with @BotFather, send /newbot.")
    print("  2. Pick a name + username (must end in 'bot').")
    print("  3. BotFather replies with a token. Paste below.")
    print("  4. Open your new bot's chat and tap Start (or send any message).\n")

    token = prompt("Bot token (or press Enter to skip Telegram)", allow_empty=True)
    if not token:
        return "", ""

    if ":" not in token or len(token) < 30:
        print("  That doesn't look like a bot token — skipping Telegram.")
        return "", ""

    print("  Verifying token via getMe...")
    info = tg_get(token, "getMe")
    if not info.get("ok"):
        print(f"  Failed: {info.get('description', 'unknown error')}. Skipping Telegram.")
        return "", ""
    bot = info["result"]
    print(f"  OK. Bot is @{bot.get('username')} ({bot.get('first_name')}).")

    print("\n  Now open the bot in Telegram and send any message to it.")
    print("  Press Enter when done.")
    input()

    chat_id: str | None = None
    for attempt in range(3):
        updates = tg_get(token, "getUpdates")
        if updates.get("ok") and updates.get("result"):
            for upd in reversed(updates["result"]):
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                if chat.get("type") == "private" and "id" in chat:
                    chat_id = str(chat["id"])
                    sender = chat.get("first_name") or chat.get("username") or "(you)"
                    print(f"  Found chat with {sender} (id={chat_id}).")
                    break
        if chat_id:
            break
        if attempt < 2:
            print("  Didn't see your message yet. Wait a sec and press Enter to retry...")
            input()
            time.sleep(1)

    if not chat_id:
        print("  Couldn't resolve chat_id. Skipping Telegram for now.")
        print("  Re-run setup.py after you've sent a message to the bot.")
        return "", ""

    test = tg_get(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "Intraday pre-market brief setup complete. You'll receive briefs here.",
    })
    if not test.get("ok"):
        print(f"  Test send failed: {test.get('description', 'unknown error')}.")
        return "", ""
    print("  Test message sent — check Telegram.")
    return token, chat_id


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    print("=== Intraday pre-market brief — one-time setup ===")

    existing = load_env()
    url = setup_finviz(existing)
    token, chat_id = setup_telegram(existing)

    existing["INTRADAY_FINVIZ_URL"] = url
    if token and chat_id:
        existing["TELEGRAM_BOT_TOKEN"] = token
        existing["TELEGRAM_CHAT_ID"] = chat_id

    save_env(existing)
    print(f"\nSaved config to {ENV_PATH}")
    print("\nRun the brief with:")
    print("  py scripts/premarket_brief.py --mode t60")
    print("  py scripts/premarket_brief.py --mode t30")
    if token and chat_id:
        print("  py scripts/premarket_brief.py --mode t60 --send-telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
