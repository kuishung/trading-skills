"""One-time IBKR setup wizard for intraday_bot.

Walks the user through:
  1. The IBKR client-portal toggles (Read-Only API ON, Socket Clients ON)
  2. Generating an IBC config file (for auto-login of IB Gateway)
  3. Flipping config.json to data_provider = "ibkr"
  4. Smoke-testing the connection

Run:
    py scripts/setup_ibkr.py

After this completes, run `scripts/setup_gateway_autostart.py` to register
the daily auto-start task.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution", "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---
from _common import CONFIG_PATH, CONFIG_EXAMPLE_PATH, SKILL_DIR, load_config  # noqa: E402

IBC_DIR_DEFAULT = SKILL_DIR / "ibc"
IBC_CONFIG_PATH = IBC_DIR_DEFAULT / "config.ini"
IBC_SECRETS_PATH = IBC_DIR_DEFAULT / "credentials.txt"

PAPER_PORT = 4002  # IB Gateway paper. Use 7497 if running TWS instead.


def step_print_checklist() -> None:
    print("""
================================================================
Intraday bot — IBKR setup wizard
================================================================

Before continuing, complete these steps inside IBKR's client portal
(https://www.interactivebrokers.com -> Login -> Settings):

  [ ] User Settings -> API -> Settings -> "Enable ActiveX and Socket
      Clients" = ON
  [ ] User Settings -> API -> Settings -> "Read-Only API" = ON
        (this physically blocks order placement from the API. The
         bot needs IBKR for data only and must not be able to place
         IBKR orders. If you ever turn this off, you're on your own.)
  [ ] User Settings -> Trading -> "Confirm market orders" doesn't
      matter for us since Read-Only API is on.

Then in IB Gateway itself (the desktop app you'll launch each morning):

  [ ] Configure -> Settings -> API -> Settings:
        * Read-Only API = ON
        * Socket port = 4002  (Gateway PAPER. TWS paper = 7497.)
        * Master API client ID = blank
        * Trusted IPs = 127.0.0.1  (click Create, type, OK)
        * Allow connections from localhost only = ON
        (Note: modern Gateway (10.19+) does not show a separate
         "Enable ActiveX and Socket Clients" checkbox — the API is
         always-on for Gateway. If your version does show it, tick it.)
  [ ] Configure -> Settings -> Lock and Exit:
        * Auto-restart = OFF  (we manage restart via the scheduled task)
""")
    input("Press Enter when you've completed the checklist...")


def step_credentials() -> dict[str, str]:
    print()
    print("--- IBKR paper-account credentials ---")
    print("These will be stored in a plaintext file (IBKR Gateway has no")
    print("headless / token auth mode). The next prompt asks WHERE to store")
    print("the file — pick a folder that is NOT synced to any cloud service")
    print("(Dropbox / OneDrive / iCloud / Google Drive).")
    print()
    username = input("IBKR paper username (looks like 'AB1234567' or your login): ").strip()
    if not username:
        sys.exit("Username is required.")
    password = getpass.getpass("IBKR paper password (input hidden): ")
    if not password:
        sys.exit("Password is required.")
    return {"username": username, "password": password}


def _looks_like_cloud_sync(path: Path) -> str | None:
    """Heuristic: warn the user if their chosen credentials directory looks
    like it's inside a known cloud-sync folder. Returns a label or None."""
    p = str(path).lower().replace("/", "\\")
    markers = {
        "dropbox": "Dropbox",
        "onedrive": "OneDrive",
        "google drive": "Google Drive",
        "googledrive": "Google Drive",
        "icloud": "iCloud",
        "box sync": "Box",
    }
    for needle, label in markers.items():
        if needle in p:
            return label
    return None


def step_credentials_path(default_ibc_dir: Path) -> Path:
    print()
    print("--- Where should the credentials file live? ---")
    print()
    print("Recommended: a vault folder that is NOT synced to any cloud service.")
    print("Examples of safe locations:")
    print("  - A VeraCrypt / Cryptomator mounted volume (e.g. V:\\vault\\)")
    print("  - A BitLocker-encrypted drive partition")
    print("  - A local-only folder outside Dropbox / OneDrive / iCloud / Google Drive")
    print()
    print(f"Default (NOT recommended — inside Dropbox): {default_ibc_dir}\\credentials.txt")
    print()
    raw = input("Full path to credentials file (or Enter for default): ").strip()
    if not raw:
        chosen = default_ibc_dir / "credentials.txt"
    else:
        chosen = Path(raw)
        if chosen.is_dir() or raw.endswith(("\\", "/")):
            chosen = chosen / "credentials.txt"
    chosen.parent.mkdir(parents=True, exist_ok=True)
    chosen = chosen.resolve()

    cloud_label = _looks_like_cloud_sync(chosen.parent)
    if cloud_label:
        print()
        print(f"⚠️  WARNING: {chosen.parent} looks like it's inside {cloud_label}.")
        print(f"   Storing your IBKR password there means it will be synced to")
        print(f"   {cloud_label}'s servers. Continue only if you've explicitly")
        print(f"   opted out of sync for this subfolder.")
        ans = input("   Continue anyway? [y/N] ").strip().lower()
        if ans != "y":
            sys.exit("Cancelled. Re-run with a non-cloud path.")
    return chosen


def step_paths() -> dict[str, str]:
    print()
    print("--- File system paths ---")
    cfg = load_config()
    existing_path = cfg.get("ibkr_gateway_path") or r"C:\Jts\ibgateway\latest\ibgateway.exe"
    print(f"Default executable path: {existing_path}")
    print("(Use the TWS exe (tws.exe) if running TWS; or ibgateway.exe if running Gateway.)")
    gateway_path = input(
        f"IB Gateway / TWS executable [{existing_path}]: "
    ).strip() or existing_path
    if not Path(gateway_path).exists():
        print(f"WARN: {gateway_path} doesn't exist yet. Download from")
        print("      https://www.interactivebrokers.com/en/trading/  (Gateway or TWS)")
        print("      and re-run this script, OR edit config.json later.")

    # Infer app type from the path (overridable via config). Used by the
    # launcher to decide whether to pass --gateway to IBC.
    inferred_app_type = "gateway" if "ibgateway" in gateway_path.lower() else "tws"
    app_type = cfg.get("ibkr_app_type") or inferred_app_type

    default_ibc = IBC_DIR_DEFAULT
    ibc_dir = input(f"IBC install directory [{default_ibc}]: ").strip() or str(default_ibc)
    Path(ibc_dir).mkdir(parents=True, exist_ok=True)
    return {"gateway_path": gateway_path,
            "ibc_dir": str(Path(ibc_dir).resolve()),
            "app_type": app_type}


def step_patch_ibc_scripts(paths: dict) -> None:
    """IBC's StartTWS.bat / StartGateway.bat use `setlocal` then hardcode
    `set IBC_PATH=%SYSTEMDRIVE%\\IBC` and `set TWS_PATH=%SYSTEMDRIVE%\\Jts`.
    The IBC userguide explicitly says to edit those lines for your setup.
    We do that edit automatically.

    Idempotent — patches only the SET lines, leaves the rest of the file alone,
    re-running is safe.
    """
    import re
    ibc_dir = Path(paths["ibc_dir"])
    exe_path = Path(paths["gateway_path"])
    # TWS_PATH must be the IBKR install ROOT (typically C:\Jts), NOT the
    # version-specific folder. IBC internally appends:
    #   - <TWS_PATH>\<version>\jars            for TWS
    #   - <TWS_PATH>\ibgateway\<version>\jars  for Gateway
    # So we walk up to find the "Jts" directory regardless of app type.
    tws_dir = exe_path.parent
    for ancestor in exe_path.parents:
        if ancestor.name.lower() == "jts":
            tws_dir = ancestor
            break
    else:
        # No `Jts` directory in the path — fall back to grandparent for
        # versioned installs (e.g., C:\custom\1045\tws.exe -> C:\custom),
        # parent for unversioned (e.g., C:\Jts\tws.exe -> C:\Jts).
        if paths.get("app_type") == "gateway":
            # Gateway is always <root>\ibgateway\<version>\ibgateway.exe
            tws_dir = exe_path.parent.parent.parent
        elif exe_path.parent.name.isdigit():
            tws_dir = exe_path.parent.parent
        else:
            tws_dir = exe_path.parent

    # Use callable replacements (not string replacements) — the path values
    # contain backslashes like `D:\Dropbox\...` and `re.sub` would try to
    # interpret \D / \B / etc. as regex escapes in a STRING replacement.
    # A callable bypasses that entirely.
    def _make_setter(value: str):
        def repl(m: "re.Match") -> str:
            return f"{m.group(1)}{value}"
        return repl

    # Convert env-var-clearing lines to be parent-process-aware:
    #   `set TWSUSERID=`  ->  `if not defined TWSUSERID set TWSUSERID=`
    # If our launcher already exported TWSUSERID, the `if not defined` check
    # is false and we keep the parent's value. If unset, we keep the original
    # behaviour (set to empty).
    def _preserve_env(m: "re.Match") -> str:
        var = m.group(2)
        return f"{m.group(1)}if not defined {var} set {var}="

    # Discover the installed offline-Gateway version from the configured path
    # so we can patch TWS_MAJOR_VRSN. Path like:
    #   C:\Jts\ibgateway\1037\ibgateway.exe  ->  1037
    #   C:\Jts\1037\tws.exe                   ->  1037
    # If the path is unversioned (e.g. C:\Jts\tws.exe), leave the existing
    # TWS_MAJOR_VRSN alone so the user can edit it manually.
    tws_major_vrsn: str | None = None
    for part in Path(paths["gateway_path"]).parts:
        if part.isdigit() and len(part) >= 4:
            tws_major_vrsn = part
            break

    config_ini_path = ibc_dir / "config.ini"

    replacements: list[tuple[str, "object"]] = [
        (r"^(\s*set\s+IBC_PATH\s*=).*$",      _make_setter(str(ibc_dir))),
        (r"^(\s*set\s+TWS_PATH\s*=).*$",      _make_setter(str(tws_dir))),
        (r"^(\s*set\s+TRADING_MODE\s*=).*$",  _make_setter("paper")),
        (r"^(\s*set\s+CONFIG\s*=).*$",        _make_setter(str(config_ini_path))),
        # Preserve env vars from the parent process for credentials
        (r"^(\s*)set\s+(TWSUSERID)\s*=\s*$",  _preserve_env),
        (r"^(\s*)set\s+(TWSPASSWORD)\s*=\s*$", _preserve_env),
    ]
    if tws_major_vrsn:
        replacements.append(
            (r"^(\s*set\s+TWS_MAJOR_VRSN\s*=).*$", _make_setter(tws_major_vrsn))
        )

    for name in ("StartTWS.bat", "StartGateway.bat"):
        p = ibc_dir / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="cp1252")
        patched = text
        for pat, repl in replacements:
            patched = re.sub(pat, repl, patched, flags=re.MULTILINE)
        if patched != text:
            p.write_text(patched, encoding="utf-8")
            knobs = ["IBC_PATH", "TWS_PATH", "TRADING_MODE", "CONFIG",
                     "TWSUSERID-passthrough", "TWSPASSWORD-passthrough"]
            if tws_major_vrsn:
                knobs.append(f"TWS_MAJOR_VRSN={tws_major_vrsn}")
            print(f"  patched {p}")
            print(f"    ({', '.join(knobs)})")
        else:
            print(f"  {p} already correct, no changes needed")


def step_write_ibc_files(creds: dict | None, paths: dict, secrets_path: Path) -> None:
    """Write IBC config + launcher batch (always), and the credentials file
    (only if `creds` is provided; pass None to preserve the existing one).

      1. {secrets_path}        — IbLoginId/IbPassword (only when creds is given)
      2. {ibc_dir}/config.ini  — IBC config without secrets (always written)
      3. {ibc_dir}/StartIBC-intraday.bat — launcher (always written, no secrets)
    """
    ibc_dir = Path(paths["ibc_dir"])
    ibc_dir.mkdir(parents=True, exist_ok=True)
    secrets_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Credentials file — only when creds were freshly collected.
    if creds is not None:
        secrets_path.write_text(
            f"IbLoginId={creds['username']}\n"
            f"IbPassword={creds['password']}\n",
            encoding="utf-8",
        )
        print(f"  wrote {secrets_path}  (PROTECT THIS FILE)")
    else:
        if secrets_path.exists():
            print(f"  preserved {secrets_path}  (skip-credentials in use)")
        else:
            print(f"  WARN: {secrets_path} does not exist and was not written. "
                  f"The launcher will fail at runtime until you create it.")

    # 2. IBC config.ini — does NOT contain secrets
    config_ini = ibc_dir / "config.ini"
    config_ini.write_text(f"""# IBC config for intraday_bot — generated by setup_ibkr.py
# https://github.com/IbcAlpha/IBC/blob/master/userguide.md
# Credentials are NOT stored here — see StartIBC-intraday.bat which sources them
# from {secrets_path} at runtime and passes them to IBC via env vars.

IbLoginId=
IbPassword=
TradingMode=paper
IbDir=
FIX=no

# Gateway, not full TWS
IbcPath={paths['ibc_dir']}
IbcIniPath={paths['ibc_dir']}\\config.ini
TwsPath={Path(paths['gateway_path']).parent}
TwsSettingsPath=

# Auto-close Gateway dialogs we don't want appearing
ExistingSessionDetectedAction=manual
AcceptIncomingConnectionAction=accept
ShowAllTrades=no
MinimizeMainWindow=yes
ReadOnlyLogin=no
ReadOnlyApi=yes
StoreSettingsOnServer=no

# Daily restart — handled by Windows Task Scheduler, not IBC
AutoRestartTime=
AutoLogoffTime=
ClosedownAt=
""", encoding="utf-8")
    print(f"  wrote {config_ini}")

    # 3. StartIBC-intraday.bat — the wrapper that Task Scheduler will run.
    #    Reads credentials from {secrets_path} at runtime, sets env vars,
    #    invokes IBC's StartIBC.bat. The wrapper itself contains no secrets.
    #    Modern IBC (3.20+) puts StartIBC.bat under scripts/; older releases
    #    put it at the IBC root. The launcher probes both at runtime so we
    #    work with either layout.
    start_at_root = (ibc_dir / "StartIBC.bat").exists()
    start_at_scripts = (ibc_dir / "scripts" / "StartIBC.bat").exists()
    if not (start_at_root or start_at_scripts):
        print(f"  NOTE: StartIBC.bat not found in {ibc_dir} or {ibc_dir}\\scripts.")
        print(f"        Extract the IBC zip into {ibc_dir} before running")
        print(f"        setup_gateway_autostart.py.")
    launcher = ibc_dir / "StartIBC-intraday.bat"
    app_type = paths.get("app_type", "tws")
    app_label = "Gateway" if app_type == "gateway" else "TWS"
    # IBC release layouts we support:
    #   3.21+  ->  StartTWS.bat / StartGateway.bat at IBC root (separate per app)
    #   3.20   ->  StartIBC.bat at IBC root or under scripts/, args control app
    primary_bat = "StartGateway.bat" if app_type == "gateway" else "StartTWS.bat"
    legacy_app_flag = "--gateway " if app_type == "gateway" else ""
    tws_dir = Path(paths['gateway_path']).parent
    launcher.write_text(f"""@echo off
REM Launcher generated by intraday_bot setup_ibkr.py for {app_label} mode.
REM Reads IBKR credentials from a path you chose (vault preferred), sets env
REM vars, then invokes IBC's start script. This file contains NO secrets and
REM is safe to commit / sync.

setlocal EnableDelayedExpansion

set "CRED_FILE={secrets_path}"
if not exist "%CRED_FILE%" (
    echo ERROR: credentials file not found at %CRED_FILE%
    echo If your vault drive isn't mounted, mount it first then re-run.
    exit /b 1
)

for /f "usebackq tokens=1,2 delims==" %%a in ("%CRED_FILE%") do (
    if /i "%%a"=="IbLoginId" set "TWSUSERID=%%b"
    if /i "%%a"=="IbPassword" set "TWSPASSWORD=%%b"
)

if "%TWSUSERID%"=="" (
    echo ERROR: credentials file %CRED_FILE% did not yield IbLoginId.
    exit /b 1
)

REM IBC needs to know where {app_label} AND where IBC itself are installed.
REM Without these, IBC defaults to C:\\Jts and C:\\IBC respectively.
set "TWS_PATH={tws_dir}"
set "IBC_PATH={ibc_dir}"
set "IBC_INI={ibc_dir}\\config.ini"
set "TRADING_MODE=paper"

REM Locate the IBC start script. Modern IBC (3.21+) has separate
REM Start{{TWS,Gateway}}.bat scripts per app. Older IBC (3.20 and earlier)
REM has a single StartIBC.bat that takes --gateway / --mode flags.
set "IBC_START="
set "IBC_STYLE="
if exist "{ibc_dir}\\{primary_bat}" (
    set "IBC_START={ibc_dir}\\{primary_bat}"
    set "IBC_STYLE=modern"
)
if not defined IBC_START if exist "{ibc_dir}\\scripts\\{primary_bat}" (
    set "IBC_START={ibc_dir}\\scripts\\{primary_bat}"
    set "IBC_STYLE=modern"
)
if not defined IBC_START if exist "{ibc_dir}\\StartIBC.bat" (
    set "IBC_START={ibc_dir}\\StartIBC.bat"
    set "IBC_STYLE=legacy"
)
if not defined IBC_START if exist "{ibc_dir}\\scripts\\StartIBC.bat" (
    set "IBC_START={ibc_dir}\\scripts\\StartIBC.bat"
    set "IBC_STYLE=legacy"
)
if not defined IBC_START (
    echo ERROR: No IBC start script found in {ibc_dir} or {ibc_dir}\\scripts.
    echo Expected one of: {primary_bat}, StartIBC.bat
    echo Did you extract the IBC zip into {ibc_dir}?
    exit /b 1
)

echo Using IBC: %IBC_START%  ^(style: %IBC_STYLE%^)
if "%IBC_STYLE%"=="modern" (
    call "%IBC_START%" paper
) else (
    call "%IBC_START%" {legacy_app_flag}--mode paper --tws-path "{tws_dir}"
)

endlocal
""", encoding="utf-8")
    print(f"  wrote {launcher}  ({app_label} mode; this is what Task Scheduler runs)")

    # 4. .gitignore inside the IBC dir — only catches credentials accidentally
    #    written here. The real defense is the vault path.
    (ibc_dir / ".gitignore").write_text("credentials.txt\n*.log\n", encoding="utf-8")


def step_write_config(paths: dict, secrets_path: Path) -> None:
    cfg = load_config()
    cfg["data_provider"] = "ibkr"
    cfg["ibkr_host"] = "127.0.0.1"
    # Preserve user-set port (e.g. 7497 for TWS) if they edited it. Otherwise
    # default to the paper port appropriate for the chosen app type.
    if not cfg.get("ibkr_port"):
        cfg["ibkr_port"] = 7497 if paths.get("app_type") == "tws" else PAPER_PORT
    cfg["ibkr_client_id"] = cfg.get("ibkr_client_id", 71)
    cfg["ibkr_gateway_path"] = paths["gateway_path"]
    cfg["ibkr_app_type"] = paths.get("app_type", "gateway")
    cfg["ibkr_ibc_dir"] = paths["ibc_dir"]
    cfg["ibkr_secrets_path"] = str(secrets_path)
    cfg["ibkr_launcher_bat"] = str(Path(paths["ibc_dir"]) / "StartIBC-intraday.bat")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"  wrote {CONFIG_PATH}  (data_provider=ibkr, app_type={cfg['ibkr_app_type']})")


def step_smoke_test() -> int:
    print()
    print("--- Smoke test ---")
    print("Make sure IB Gateway is running and logged into paper. Then we'll")
    print("connect and fetch one ticker to verify the pipe works.")
    ans = input("Is IB Gateway running and logged in? [y/N] ").strip().lower()
    if ans != "y":
        print("Skipping smoke test. Run this later:")
        print("  py scripts/_ibkr_data.py")
        return 0
    # Import _ibkr_data FIRST — its module-level shim installs an asyncio
    # event loop (needed by eventkit on Python 3.14+). After that, we can
    # safely check whether ib_insync is actually usable.
    from ibkr_data import smoke_test, IB
    if IB is None:
        print("ib_insync not installed (or failed to import). Install it:")
        print("  py -m pip install ib_insync")
        print("If it's installed but still failing, you may need:")
        print("  py -m pip install -U eventkit ib_insync")
        return 1
    cfg = load_config()
    return smoke_test(cfg)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--skip-credentials", action="store_true",
                   help="Leave existing IBC credentials file alone.")
    p.add_argument("--smoke-test-only", action="store_true",
                   help="Skip the wizard, just run the connection smoke test.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.smoke_test_only:
        return step_smoke_test()

    step_print_checklist()
    paths = step_paths()
    if not args.skip_credentials:
        creds = step_credentials()
        secrets_path = step_credentials_path(Path(paths["ibc_dir"]))
    else:
        print("(skipping credentials re-entry — using existing vault file)")
        existing_cfg = load_config()
        existing_secrets = existing_cfg.get("ibkr_secrets_path")
        if not existing_secrets:
            sys.exit("--skip-credentials needs an existing ibkr_secrets_path in "
                     "config.json. Run without --skip-credentials first.")
        secrets_path = Path(existing_secrets)
        creds = None  # signals step_write_ibc_files to preserve the credentials file

    # Always write the IBC config + launcher batch — these contain no secrets,
    # and they're what carries the latest template fixes (Gateway↔TWS toggle,
    # path probe order, etc.).
    step_write_ibc_files(creds, paths, secrets_path)
    # Patch IBC's StartTWS.bat / StartGateway.bat with the user's actual
    # IBC and TWS install paths (IBC ships them hardcoded to C:\IBC / C:\Jts).
    step_patch_ibc_scripts(paths)
    step_write_config(paths, secrets_path)

    print()
    print("--- Next steps ---")
    print("1. Install IBC if you haven't:")
    print("     https://github.com/IbcAlpha/IBC/releases  (download the zip,")
    print(f"     extract into {paths['ibc_dir']})")
    print("2. Install ib_insync:  py -m pip install ib_insync")
    print("3. Test connection:    py scripts/_ibkr_data.py")
    print("4. Schedule auto-start: py scripts/setup_gateway_autostart.py")
    print()
    print(f"Credentials live at: {secrets_path}")
    print("Treat that file like a password manager entry. If your vault is")
    print("a mounted encrypted volume, make sure it's unlocked before market")
    print("open so Task Scheduler can read the credentials.")
    print()
    return step_smoke_test()


if __name__ == "__main__":
    sys.exit(main())
