"""The member connector download: ``TradeHunter-IBKR-Connector-<version>.zip``.

OPTIONS_V2_DESIGN.md §5.3. ``GET /options/connector/download`` hands a member the
connector that feeds their own IBKR data to the Options page. The zip is built in
memory from ``dashboard_tst/bridge/`` on every request (about 100 KB), so it always
matches the code the server runs - there is no separate release step to forget.

Contents: ``ibkr_bridge.py`` (the connector), ``th_ibkr.py`` (the IBKR fetch
library, when present), ``start_ibkr_bridge.bat``, ``install_bridge.ps1``,
``requirements.txt`` and a generated ``README.txt`` (a plain install guide).

The version in the file name is read from the files' ``VERSION = "..."`` line
(``th_ibkr.py`` first, then ``ibkr_bridge.py``) with a regex, not an import: the
bridge folder is not a package, and importing it from the web app would pull its
module state into the server process.

Windows text files (``.bat``, ``.ps1``, ``.txt``) are written with CRLF line endings:
the repo may hold them with LF, and cmd.exe misreads some LF-only batch files.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[2] / "bridge"     # dashboard_tst/bridge
REQUIRED = ("ibkr_bridge.py", "start_ibkr_bridge.bat", "install_bridge.ps1", "requirements.txt")
OPTIONAL = ("th_ibkr.py",)
CRLF_SUFFIXES = (".bat", ".ps1", ".txt")
NAME_PREFIX = "TradeHunter-IBKR-Connector"
TOP_FOLDER = NAME_PREFIX            # the one folder every zip entry sits in
_VERSION_RE = re.compile(r"""^VERSION\s*=\s*["']([0-9A-Za-z.\-]+)["']""", re.MULTILINE)


def connector_version(bridge_dir: Path | None = None) -> str:
    """``th_ibkr.VERSION``, else ``ibkr_bridge.VERSION``, else "unknown"."""
    d = Path(bridge_dir) if bridge_dir else BRIDGE_DIR
    for name in ("th_ibkr.py", "ibkr_bridge.py"):
        p = d / name
        if p.is_file():
            m = _VERSION_RE.search(p.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
    return "unknown"


def readme_text(version: str) -> str:
    """The plain install guide shipped as README.txt (ASCII, read in Notepad)."""
    return f"""TradeHunter IBKR Connector {version}
{"=" * (len("TradeHunter IBKR Connector ") + len(version))}

A small program that runs on YOUR PC, next to YOUR Trader Workstation (TWS) or
IB Gateway. The TradeHunter Options page reads live option data from your own
IBKR login through it, and the data you read is shared with the other members.
It is read-only: it cannot place orders.

INSTALL (once, about two minutes)

  1. Unzip this file anywhere you like, e.g. into Documents. It makes one folder,
     TradeHunter-IBKR-Connector. Keep that folder there afterwards: Windows starts
     the connector from it.

  2. Right-click install_bridge.ps1 > Run with PowerShell.
     It checks for Python 3.12 (and offers to install it with winget if it is
     missing), installs the one library the connector needs (ib_insync), makes
     the connector start with Windows, starts it, and opens its settings page.

  3. In the settings page that opens (http://127.0.0.1:9224/), pick your TWS
     port and press "Save & reconnect":
        TWS live 7496   TWS paper 7497   IB Gateway live 4001   IB Gateway paper 4002

IN TWS (once)

  File > Global Configuration > API > Settings:
    - tick "Enable ActiveX and Socket Clients"
    - the "Socket port" must be the port you picked in step 3
    - "Read-Only API" can stay ticked (the connector never places orders)
  IB Gateway: Configure > Settings > API > Settings, the same boxes.

THE PILL ON THE OPTIONS PAGE

  green  the connector is running and connected to TWS
  amber  the connector is running but cannot reach TWS: start TWS, or check
         the port in the connector's settings page
  red    the connector is not running: double-click start_ibkr_bridge.bat

EVERYDAY USE

  The connector starts with Windows after step 2. To start it by hand,
  double-click start_ibkr_bridge.bat; to stop it, close its window.
  Settings page: http://127.0.0.1:9224/
  Settings file: %APPDATA%\\TradeHunter\\connector.json

UPDATE / REMOVE

  Update: download the new zip from the Options page, unzip it over this
  folder, and run install_bridge.ps1 again.
  Remove: in PowerShell in this folder run
     powershell -ExecutionPolicy Bypass -File install_bridge.ps1 -Uninstall
  then delete the folder.
"""


def _crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _entry(zf: zipfile.ZipFile, name: str, data: bytes, mtime: float) -> None:
    if name.lower().endswith(CRLF_SUFFIXES):
        data = _crlf(data)
    stamp = time.localtime(max(mtime, 315532800.0))[:6]      # zip dates start in 1980
    info = zipfile.ZipInfo(name, date_time=stamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def build_zip(bridge_dir: Path | None = None) -> tuple[bytes, str]:
    """(zip bytes, file name). FileNotFoundError when a required file is missing."""
    d = Path(bridge_dir) if bridge_dir else BRIDGE_DIR
    missing = [n for n in REQUIRED if not (d / n).is_file()]
    if missing:
        raise FileNotFoundError(f"connector files missing in {d}: {', '.join(missing)}")
    version = connector_version(d)
    buf = io.BytesIO()
    # Every entry sits under one top-level folder, so "Extract here" in Downloads makes a
    # folder instead of spilling six files beside the member's other downloads.
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in REQUIRED[:1] + OPTIONAL + REQUIRED[1:]:
            p = d / name
            if p.is_file():
                _entry(zf, f"{TOP_FOLDER}/{name}", p.read_bytes(), p.stat().st_mtime)
        _entry(zf, f"{TOP_FOLDER}/README.txt", readme_text(version).encode("ascii", "replace"), time.time())
    return buf.getvalue(), f"{NAME_PREFIX}-{version}.zip"
