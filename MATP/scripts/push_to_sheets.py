#!/usr/bin/env python
"""Push MATP_table.csv to a new dated tab in the configured Google Sheet.

By default, creates a tab named with today's date in YYYY-MM-DD format.
If the tab already exists, the script aborts unless --overwrite is passed.

Usage:
    python scripts/push_to_sheets.py
    python scripts/push_to_sheets.py --tab 2026-05-13
    python scripts/push_to_sheets.py --overwrite
    python scripts/push_to_sheets.py --csv path/to/other.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ENV_PATH = SKILL_DIR / ".env"
DEFAULT_CSV = SKILL_DIR / "MATP_table.csv"

REQUIRED_ENV = ("GOOGLE_SA_KEY_PATH", "MATP_SHEET_ID")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


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
            f"Re-run scripts/setup_sheets.py."
        )
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        default=str(DEFAULT_CSV),
        help=f"Path to MATP CSV (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--tab",
        default=None,
        help="Tab name (default: today in YYYY-MM-DD)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the tab if it already exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_env()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        sys.exit(f"ERROR: CSV is empty: {csv_path}")

    tab_name = args.tab or date.today().isoformat()

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        sys.exit("gspread / google-auth not installed. Run: pip install -r requirements.txt")

    try:
        creds = Credentials.from_service_account_file(
            config["GOOGLE_SA_KEY_PATH"], scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(config["MATP_SHEET_ID"])
    except FileNotFoundError:
        sys.exit(
            f"ERROR: Service account JSON not found at {config['GOOGLE_SA_KEY_PATH']}. "
            f"Re-run setup_sheets.py."
        )
    except gspread.exceptions.SpreadsheetNotFound:
        sys.exit(
            f"ERROR: Sheet '{config['MATP_SHEET_ID']}' not found or no longer shared "
            f"with the service account."
        )

    existing = {ws.title for ws in sh.worksheets()}
    n_rows = len(rows)
    n_cols = max(len(r) for r in rows)

    if tab_name in existing:
        if not args.overwrite:
            sys.exit(
                f"ERROR: Tab '{tab_name}' already exists. "
                f"Re-run with --overwrite to replace it, "
                f"or --tab <other-name> to write to a different tab."
            )
        # Clear-and-reuse rather than delete-and-recreate: Google Sheets
        # refuses to delete the only remaining tab in a document.
        ws = sh.worksheet(tab_name)
        ws.clear()
        ws.resize(rows=n_rows + 5, cols=n_cols + 2)
    else:
        ws = sh.add_worksheet(title=tab_name, rows=n_rows + 5, cols=n_cols + 2)

    ws.update(range_name="A1", values=rows, value_input_option="USER_ENTERED")

    # Cosmetics: freeze header row, bold all headers, right-align the numeric
    # column headers (MATP/MBP) so they sit flush with their currency values,
    # and currency-format the MATP (D) and MBP (E) data.
    ws.freeze(rows=1)
    ws.format(f"A1:{chr(ord('A') + n_cols - 1)}1", {"textFormat": {"bold": True}})
    currency = {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}
    right_align = {"horizontalAlignment": "RIGHT"}
    if n_cols >= 5:
        ws.format(f"D2:E{n_rows}", currency)
        ws.format("D1:E1", right_align)
    elif n_cols >= 4:
        ws.format(f"D2:D{n_rows}", currency)
        ws.format("D1:D1", right_align)

    data_rows = n_rows - 1  # exclude header
    sheet_url = (
        f"https://docs.google.com/spreadsheets/d/{config['MATP_SHEET_ID']}/edit#gid={ws.id}"
    )
    print(f"Pushed {data_rows} rows to tab '{tab_name}'.")
    print(f"Sheet URL: {sheet_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
