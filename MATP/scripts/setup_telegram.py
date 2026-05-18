#!/usr/bin/env python
"""Interactive one-time setup for the MATP daily bounce alert -> Telegram.

Walks the user through:
  1. Creating a bot via @BotFather and getting the bot token.
  2. Sending an opening message to the bot so we can resolve their chat_id.
  3. Confirming the round-trip with a test message.
  4. Saving TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env (preserving any
     unrelated keys already in there).

Run from the skill root directory:
    py scripts/setup_telegram.py
"""
from __future__ import annotations

import sys
import time
import urllib.parse
import urllib.request
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from _envpath import env_path
ENV_PATH = env_path(SKILL_DIR, "matp")

WALKTHROUGH = """
=== MATP daily bounce alert -> Telegram setup ===

You need a personal Telegram bot to receive the daily reports. Setup
takes ~3 minutes.

  1. In Telegram, open a chat with @BotFather (https://t.me/BotFather).

  2. Send the message: /newbot
     BotFather will ask for a display name (e.g. "MATP Alerts") and
     then a username (must end in 'bot', e.g. "matp_alerts_bot").
     If the username is taken, pick another.

  3. BotFather replies with a token like
       123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
     Copy it -- you'll paste it below.

  4. In Telegram, open your new bot's chat and tap "Start"
     (or send any message like "hi"). This is what lets us discover
     YOUR chat_id -- the bot can only DM you after you have spoken
     to it at least once.

Have the token + opening message ready, then continue below.
"""


def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip().strip('"').strip("'")
        if value:
            return value
        if default is not None:
            return default
        print("  (required) Please enter a value.")


def tg_get(token: str, method: str, params: dict | None = None) -> dict:
    """Minimal Telegram Bot API GET. Returns the decoded JSON body."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "matp-setup/1"})
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


def main() -> int:
    print(WALKTHROUGH)

    # --- Token ---
    while True:
        token = prompt("Telegram bot token")
        if ":" not in token or len(token) < 30:
            print("  That doesn't look like a bot token (expect '<digits>:<letters>').")
            continue
        print("  Verifying token via getMe...")
        info = tg_get(token, "getMe")
        if not info.get("ok"):
            print(f"  Failed: {info.get('description', 'unknown error')}. Try again.")
            continue
        bot = info["result"]
        print(f"  OK. Bot is @{bot.get('username')} ({bot.get('first_name')}).")
        break

    # --- Chat ID via getUpdates ---
    print("\nNow go to Telegram, open the bot's chat, and send any message")
    print("to it (e.g. 'hi'). Then press Enter here to continue.")
    input()

    chat_id: str | None = None
    for attempt in range(3):
        updates = tg_get(token, "getUpdates")
        if updates.get("ok") and updates.get("result"):
            # Take the most recent private chat we've seen.
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
            print("  Didn't see a message yet. If you just sent one, give it a")
            print("  couple of seconds and press Enter to retry...")
            input()
            time.sleep(1)

    if not chat_id:
        print("\nERROR: Couldn't find your chat. Telegram only exposes updates")
        print("from the last 24 hours, so please make sure you actually sent the")
        print("bot a message after creating it. Then re-run this setup.")
        return 1

    # --- Round-trip test ---
    print("\nSending a test message to confirm...")
    test = tg_get(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "MATP setup complete. You'll receive daily bounce reports here.",
    })
    if not test.get("ok"):
        print(f"  Failed: {test.get('description', 'unknown error')}.")
        print("  Common cause: you blocked the bot, or the chat_id is wrong.")
        return 1
    print("  Test message sent. Check your Telegram.")

    # --- Persist (preserve unrelated keys) ---
    existing: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip()
    existing["TELEGRAM_BOT_TOKEN"] = token
    existing["TELEGRAM_CHAT_ID"] = chat_id
    env_lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print(f"\nSaved config to {ENV_PATH}")
    print("Run the daily report with:  py scripts/daily_bounce_alert.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
