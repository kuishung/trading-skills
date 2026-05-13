#!/usr/bin/env python
"""Upload MATP artifacts (Pine indicator + TradingView watchlist) to the
configured Google Drive folder.

Uses the same service-account key as push_to_sheets.py. The folder ID
must be set via scripts/setup_drive.py and lives in .env as
MATP_DRIVE_FOLDER_ID. The folder must be shared with the service-account
email as Editor.

If a file with the same name already exists in the folder, it is
updated in place (same Drive file ID -> friends' bookmarks keep working
and the file's view link is stable). Otherwise a new file is created.

Default behavior (no flags): uploads MATP_indicator.pine AND
MATP_watchlist.txt if they exist in the skill directory. Pass --file to
upload just one specific file.

Usage:
    py scripts/upload_to_drive.py
    py scripts/upload_to_drive.py --file MATP_indicator.pine
    py scripts/upload_to_drive.py --file path/to/anything.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ENV_PATH = SKILL_DIR / ".env"

# Files uploaded by default (each one is skipped if not present).
DEFAULT_ARTIFACTS = [
    SKILL_DIR / "MATP_indicator.pine",
    SKILL_DIR / "MATP_watchlist.txt",
]

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
        default=None,
        help=(
            "Single file to upload. If omitted, uploads all default "
            "artifacts (MATP_indicator.pine + MATP_watchlist.txt)."
        ),
    )
    return parser.parse_args()


def upload_one(service, folder_id: str, path: Path) -> bool:
    """Upload or update one file. Returns True on success, False on
    soft-failure (e.g., service-account-no-quota on a first-time create
    that the user must fix manually). Hard errors raise/exit."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    filename = path.name

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
        sys.exit(f"ERROR: Drive list failed for {filename} (HTTP {exc.resp.status}): {exc}")
    existing = response.get("files", [])

    media = MediaFileUpload(str(path), mimetype="text/plain", resumable=False)

    try:
        if existing:
            file_id = existing[0]["id"]
            result = service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            ).execute()
            print(f"  Updated  {filename}  -> {result.get('webViewLink', '')}")
        else:
            body = {"name": filename, "parents": [folder_id]}
            try:
                result = service.files().create(
                    body=body,
                    media_body=media,
                    fields="id, webViewLink",
                    supportsAllDrives=True,
                ).execute()
                print(f"  Created  {filename}  -> {result.get('webViewLink', '')}")
            except HttpError as exc:
                err = str(exc)
                if "storageQuotaExceeded" in err or "Service Accounts do not have storage" in err:
                    print(
                        f"\n  ERROR: Cannot create {filename} — service account has no\n"
                        f"  storage quota on personal Drive.\n"
                        f"\n"
                        f"  One-time fix for this file:\n"
                        f"    1. Open the folder in a browser:\n"
                        f"         https://drive.google.com/drive/folders/{folder_id}\n"
                        f"    2. Drag this file into the folder so YOU own it:\n"
                        f"         {path}\n"
                        f"    3. Re-run this script.\n"
                    )
                    return False
                raise
    except HttpError as exc:
        sys.exit(f"ERROR: Drive write failed for {filename} (HTTP {exc.resp.status}): {exc}")

    return True


def main() -> int:
    args = parse_args()
    config = load_env()

    if args.file:
        targets = [Path(args.file)]
    else:
        targets = [p for p in DEFAULT_ARTIFACTS if p.exists()]
        if not targets:
            sys.exit(
                f"ERROR: No default artifacts found. Expected one or both of:\n"
                f"  {DEFAULT_ARTIFACTS[0]}\n"
                f"  {DEFAULT_ARTIFACTS[1]}\n"
                f"Run scripts/generate_pine.py and/or scripts/generate_watchlist.py first."
            )

    for p in targets:
        if not p.exists():
            sys.exit(f"ERROR: File not found: {p}")

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
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
    print(f"Uploading {len(targets)} file(s) to folder {folder_id}:")

    soft_failures = 0
    for p in targets:
        if not upload_one(service, folder_id, p):
            soft_failures += 1

    print(f"\nFolder: https://drive.google.com/drive/folders/{folder_id}")
    return 1 if soft_failures else 0


if __name__ == "__main__":
    sys.exit(main())
