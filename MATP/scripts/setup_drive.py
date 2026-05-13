#!/usr/bin/env python
"""Interactive setup for the Google Drive folder used to publish
MATP_indicator.pine to friends.

Adds MATP_DRIVE_FOLDER_ID to the existing .env file (preserves any
other keys already there). Verifies the service account can read and
write to the folder before persisting.

Prerequisites:
    - scripts/setup_sheets.py has already been run (so .env contains
      GOOGLE_SA_KEY_PATH and the service-account email is known).
    - You have created a Drive folder and shared it with the
      service-account email as Editor.

Usage:
    py scripts/setup_drive.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ENV_PATH = SKILL_DIR / ".env"

WALKTHROUGH = """
=== MATP -> Google Drive setup ===

You need a Drive folder to share with friends. The MATP service account
(set up via setup_sheets.py) will write MATP_indicator.pine into it on
every run; friends bookmark the folder and copy the .pine contents into
TradingView whenever they want the latest levels.

One-time setup (~2 minutes):

  1. Open Google Drive (in the same Google account that owns the sheet,
     or any account - they don't have to match).
  2. Create a new folder (any name, e.g. "MATP Pine Script").
  3. Open the folder. The URL looks like
       https://drive.google.com/drive/folders/<FOLDER_ID>
     Copy the FOLDER_ID part (a string of ~33 letters / digits / dashes).
  4. Right-click the folder -> Share. Paste the service-account email
     (the same one used for the sheet, e.g.
     matp-writer@matp-496207.iam.gserviceaccount.com) and grant Editor.
     Untick "Notify people". Click Share.
  5. Right-click the folder -> Share again, click "General access" ->
     "Anyone with the link" -> Viewer. Click Done. This is what lets
     your friends actually open the file.

When the folder is shared with both the service account (Editor) and
anyone-with-the-link (Viewer), continue below.

IMPORTANT - one-time manual step AFTER this script finishes:
  Service accounts have NO storage quota on personal Google Drive, so
  they cannot create the very first file in the folder. You must seed
  it once yourself:
    1. Run:   py scripts/generate_pine.py
    2. Drag the generated MATP_indicator.pine from the skill directory
       into your Drive folder (so YOU own it, not the service account).
    3. From then on, py scripts/upload_to_drive.py will update the
       file in place forever.
"""


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        sys.exit(
            f"ERROR: {ENV_PATH} not found. Run scripts/setup_sheets.py first."
        )
    config: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    return config


def write_env(config: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in config.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def prompt(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip().strip('"').strip("'")
        if value:
            return value
        print("  (required) Please enter a value.")


def main() -> int:
    print(WALKTHROUGH)
    config = load_env()

    if not config.get("GOOGLE_SA_KEY_PATH"):
        sys.exit(
            "ERROR: GOOGLE_SA_KEY_PATH not in .env. "
            "Run scripts/setup_sheets.py first."
        )

    raw = prompt("Drive folder ID (or full folder URL)")
    # Tolerate full URL paste.
    if "/folders/" in raw:
        raw = raw.split("/folders/", 1)[1]
    folder_id = raw.split("?", 1)[0].split("/", 1)[0].strip()

    print(f"\nUsing folder ID: {folder_id}")
    print("Testing Drive access...")

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        sys.exit(
            "google-api-python-client not installed. "
            "Run: py -m pip install -r requirements.txt"
        )

    creds = Credentials.from_service_account_file(
        config["GOOGLE_SA_KEY_PATH"],
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    try:
        meta = service.files().get(
            fileId=folder_id,
            fields="name, mimeType",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            print(
                f"ERROR: Could not access folder {folder_id} "
                f"(HTTP {exc.resp.status})."
            )
            print(
                "Make sure the folder is shared with the service-account "
                "email as Editor."
            )
        else:
            print(f"ERROR: Drive API returned HTTP {exc.resp.status}: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc!r}")
        return 1

    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        sys.exit(
            f"ERROR: ID {folder_id} is not a folder "
            f"(type: {meta.get('mimeType')})."
        )

    print(f"OK: Connected to folder '{meta['name']}'.")

    config["MATP_DRIVE_FOLDER_ID"] = folder_id
    write_env(config)
    print(f"\nSaved MATP_DRIVE_FOLDER_ID to {ENV_PATH}")
    print("Future runs of scripts/upload_to_drive.py will publish there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
