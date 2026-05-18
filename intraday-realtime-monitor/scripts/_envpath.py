"""Resolve which .env file to use for credentials.

Three-tier resolution (first hit wins):

  1. Explicit env var: ``INTRADAY_ENV_DIR`` — if set and points at a real
     directory, use ``<INTRADAY_ENV_DIR>/<name>.env``. Useful for overrides
     or non-Dropbox setups.

  2. Auto-detect Dropbox-VAULT: read Dropbox's local ``info.json`` to find
     the Dropbox root on THIS machine, then look for
     ``<dropbox-root>/VAULT/Claude Credential/<name>.env``. This means the
     same Dropbox-synced credentials folder Just Works on every PC the
     user sets up, even when Dropbox lives at different drive letters or
     home paths.

  3. Legacy fallback: ``<skill_dir>/.env`` — backwards-compatible if neither
     Dropbox nor the env var produces a hit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DROPBOX_RELATIVE_VAULT = Path("VAULT") / "Claude Credential"


def _find_dropbox_root() -> Path | None:
    """Return the local Dropbox root, or None if Dropbox isn't installed."""
    candidates: list[Path] = []
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(Path(localappdata) / "Dropbox" / "info.json")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Dropbox" / "info.json")
    candidates.append(Path.home() / ".dropbox" / "info.json")

    for info_path in candidates:
        if not info_path.exists():
            continue
        try:
            data = json.loads(info_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for account_type in ("personal", "business"):
            path = (data.get(account_type) or {}).get("path")
            if path and Path(path).is_dir():
                return Path(path)
    return None


def env_path(skill_dir: Path, name: str) -> Path:
    central = os.environ.get("INTRADAY_ENV_DIR")
    if central and Path(central).is_dir():
        return Path(central) / f"{name}.env"

    dbx = _find_dropbox_root()
    if dbx is not None:
        vault = dbx / DROPBOX_RELATIVE_VAULT
        if vault.is_dir():
            return vault / f"{name}.env"

    return skill_dir / ".env"
