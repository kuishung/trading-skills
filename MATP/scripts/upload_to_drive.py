#!/usr/bin/env python
"""Upload MATP_indicator.pine to the configured Google Drive folder.

Uses the same service-account key as push_to_sheets.py. The folder ID
must be set via scripts/setup_drive.py and lives in .env as
MATP_DRIVE_FOLDER_ID. The folder must be shared with the service-account
email as Editor.

If a file named MATP_indicator.pine already exists in the folder, it is
updated in place (same Drive file ID -> friends' bookmarks keep working
and the file's view link is stable). Otherwise a new file is created.

Usage:
    py scripts/upload_to_drive.py
    py scripts/upload_to_drive.py --file path/to/other.pine
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ENV_PATH = SKILL_DIR / ".env"
DEFAULT_PINE = SKILL_DIR / "MATP_indicator.pine"

REQUIRED_ENV = ("GOOGLE_SA_KEY_PATH", "MATP_DRIVE_FOLDER_ID")
SCOPES = ["https://www.googleapis.com/auth/drive"]


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        sys.exit(f"ERROR: {ENV_PATH} not found. Run scripts/setup_sheets.py first.")
    config: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    missing = [k for k in REQUIRED_ENV if not config.get(k)]
    if missing:
        sys.exit(
            f"ERROR: {', '.join(missing)} missing from {ENV_PATH}. "
            f"Run scripts/setup_drive.py if MATP_DRIVE_FOLDER_ID is missing."
        )
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        default=str(DEFAULT_PINE),
        help=f"Path to .pine file to upload (default: {DEFAULT_PINE})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_env()

    pine_path = Path(args.file)
    if not pine_path.exists():
        sys.exit(
            f"ERROR: Pine file not found: {pine_path}. "
            f"Run scripts/generate_pine.py first."
        )

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        sys.exit(
            "google-api-python-client not installed. "
            "Run: py -m pip install -r requirements.txt"
        )

    creds = Credentials.from_service_account_file(
        config["GOOGLE_SA_KEY_PATH"], scopes=SCOPES
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    folder_id = config["MATP_DRIVE_FOLDER_ID"]
    filename = pine_path.name

    # Look for an existing file with the same name in the folder so we
    # can update in place rather than creating a duplicate.
    query = (
        f"'{folder_id}' in parents and name = '{filename}' "
        f"and trashed = false"
    )
    try:
        response = service.files().list(
            q=query,
            fields="files(id, name, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except HttpError as exc:
        sys.exit(f"ERROR: Drive list failed (HTTP {exc.resp.status}): {exc}")
    existing = response.get("files", [])

    media = MediaFileUpload(str(pine_path), mimetype="text/plain", resumable=False)

    try:
        if existing:
            file_id = existing[0]["id"]
            result = service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            ).execute()
            print(f"Updated existing file {filename}")
        else:
            body = {"name": filename, "parents": [folder_id]}
            try:
                result = service.files().create(
                    body=body,
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                ).execute()
                print(f"Created new file {filename}")
            except HttpError as exc:
                err = str(exc)
                if "storageQuotaExceeded" in err or "Service Accounts do not have storage" in err:
                    sys.exit(
                        f"\nERROR: Service account cannot create new files in personal Google Drive.\n"
                        f"\n"
                        f"One-time fix:\n"
                        f"  1. Open the folder in a browser:\n"
                        f"       https://drive.google.com/drive/folders/{folder_id}\n"
                        f"  2. Drag this file into the folder so YOU own it:\n"
                        f"       {pine_path}\n"
                        f"  3. Re-run this script. From then on it will update the\n"
                        f"     file in place forever.\n"
                        f"\n"
                        f"Why: Drive service accounts have no storage quota on\n"
                        f"personal Drive, so they can modify existing files but\n"
                        f"cannot create the very first one.\n"
                    )
                raise
    except HttpError as exc:
        sys.exit(f"ERROR: Drive write failed (HTTP {exc.resp.status}): {exc}")

    print(f"File view: {result.get('webViewLink', '(no link returned)')}")
    print(f"Folder:    https://drive.google.com/drive/folders/{folder_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
