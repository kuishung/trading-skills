#!/usr/bin/env python
"""Interactive one-time setup for MATP Google Sheets push.

Walks the user through:
  1. Creating a Google Cloud service account and downloading the JSON key.
  2. Sharing the target Sheet with the service-account email.
  3. Saving the key path + Sheet ID to a gitignored .env file.

Run from the skill root directory:
    python scripts/setup_sheets.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ENV_PATH = SKILL_DIR / ".env"

DEFAULT_SHEET_ID = "1w-wlM2ORvcr9EEibQlTUCtm16T6iXh_eIFhulfQtm8Y"

WALKTHROUGH = """
=== MATP -> Google Sheets setup ===

You need a Google Cloud service account with Sheets + Drive API access.
One-time setup (~5 minutes):

  1. Create a Google Cloud project:
     https://console.cloud.google.com/projectcreate
     (any name, e.g. "matp-bot")

  2. In that project, enable BOTH APIs:
       Sheets API: https://console.cloud.google.com/apis/library/sheets.googleapis.com
       Drive API : https://console.cloud.google.com/apis/library/drive.googleapis.com

  3. Create a service account:
     https://console.cloud.google.com/iam-admin/serviceaccounts
       - Click "Create Service Account"
       - Name it (e.g. "matp-writer") -> Create and Continue
       - Skip role grants and user permissions -> Done

  4. Generate a JSON key for the service account:
       - Click the service account you just created
       - Keys tab -> Add Key -> Create new key -> JSON -> Create
       - A JSON file downloads. Keep it private. Recommended location:
         %USERPROFILE%\\.gcp\\matp-sa.json   (Windows)
         ~/.gcp/matp-sa.json                (mac/Linux)

  5. The JSON contains a field "client_email" that looks like
       matp-writer@<project>.iam.gserviceaccount.com
     Open your target Google Sheet, click Share, paste that email,
     and grant it Editor access.

When the JSON is on disk AND the sheet is shared with the service-account
email, continue below.
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


def main() -> int:
    print(WALKTHROUGH)

    # --- JSON key path ---
    while True:
        json_path_str = prompt("Path to service-account JSON key file")
        json_path = Path(json_path_str).expanduser()
        try:
            json_path = json_path.resolve(strict=True)
        except FileNotFoundError:
            print(f"  File not found: {json_path}. Try again.")
            continue
        try:
            with json_path.open(encoding="utf-8") as f:
                key_data = json.load(f)
        except json.JSONDecodeError as exc:
            print(f"  Could not parse JSON: {exc}. Try again.")
            continue
        sa_email = key_data.get("client_email")
        if not sa_email or key_data.get("type") != "service_account":
            print("  This file does not look like a service-account key "
                  "(missing client_email / wrong type). Try again.")
            continue
        break

    print(f"\nService account email: {sa_email}")
    print("Make sure the target sheet is shared with this email as Editor.\n")

    # --- Sheet ID ---
    sheet_id = prompt("Google Sheet ID", default=DEFAULT_SHEET_ID)

    # --- Connectivity test ---
    print("\nTesting connection...")
    try:
        import gspread  # noqa: F401  (imported for side effect / version check)
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("ERROR: gspread / google-auth not installed.")
        print("Run: pip install -r requirements.txt")
        return 1

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = Credentials.from_service_account_file(str(json_path), scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"ERROR: Sheet '{sheet_id}' not found, or not shared with {sa_email}.")
        print("Share the sheet with that email as Editor and re-run setup.")
        return 1
    except gspread.exceptions.APIError as exc:
        # Surface the underlying HTTP status + message so the user knows
        # exactly what went wrong (most common: APIs not enabled, or sheet
        # not shared with the service account).
        err = {}
        try:
            err = exc.response.json().get("error", {})
        except Exception:
            pass
        status = err.get("status", "")
        message = err.get("message", "")
        code = err.get("code", "")
        print(f"ERROR: APIError code={code} status={status}")
        if message:
            print(f"  {message}")
        if status == "PERMISSION_DENIED":
            print(f"Most likely cause: the target sheet is not shared with {sa_email}.")
            print("Open the sheet, click Share, and add that email as Editor.")
        if "has not been used" in message or "is disabled" in message or "SERVICE_DISABLED" in status:
            print("Most likely cause: the Sheets or Drive API is not enabled for your project.")
            print("Enable both, then wait ~30 seconds before retrying:")
            print("  https://console.cloud.google.com/apis/library/sheets.googleapis.com")
            print("  https://console.cloud.google.com/apis/library/drive.googleapis.com")
        if not message and not status:
            print(f"  raw: {exc!r}")
        return 1
    except Exception as exc:
        import traceback
        print(f"ERROR: {type(exc).__name__}: {exc!r}")
        for attr in ("filename", "filename2", "errno", "strerror"):
            val = getattr(exc, attr, None)
            if val is not None:
                print(f"  {attr}: {val}")
        print("--- Traceback ---")
        traceback.print_exc()
        return 1

    print(f"OK: Connected to sheet '{sh.title}'.")

    # --- Persist ---
    env_lines = [
        f"GOOGLE_SA_KEY_PATH={json_path}",
        f"MATP_SHEET_ID={sheet_id}",
    ]
    ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print(f"\nSaved config to {ENV_PATH}")
    print("Future MATP runs will append a new dated tab to this sheet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
