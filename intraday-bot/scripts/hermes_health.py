"""Hermes pre-flight readiness check.

Run on the Hermes VM after Phase 1+2 of HERMES_SETUP.md is done, BEFORE
kicking off the multi-day 180-day re-seed. Catches the common "I forgot
to install X" problems early so a 4-day ingest doesn't die at hour 6
because pyarrow wasn't actually installed.

CLI:
    py scripts/hermes_health.py
    py scripts/hermes_health.py --skip-ibkr      # skip the TWS probe
    py scripts/hermes_health.py --json           # machine-readable

Each check prints PASS/WARN/FAIL with a one-line reason. Exit code:
    0 = all PASS or only WARN
    1 = at least one FAIL (fix before continuing)

Designed to be safe to run anywhere (laptop too) — it just reports
status, doesn't change anything.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
SKILL_DIR = _root


# ---- Result helpers ----

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


def _result(status: str, name: str, message: str) -> dict:
    return {"status": status, "name": name, "message": message}


def _print(r: dict) -> None:
    color = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL"}.get(r["status"], r["status"])
    sys.stdout.write(f"  [{color}] {r['name']:<32}  {r['message']}\n")
    sys.stdout.flush()


# ---- Individual checks ----

def check_python_version() -> dict:
    v = sys.version_info
    msg = f"Python {v.major}.{v.minor}.{v.micro}"
    if v.major == 3 and v.minor >= 12:
        return _result(PASS, "python_version", msg + " (>=3.12 OK)")
    if v.major == 3 and v.minor >= 10:
        return _result(WARN, "python_version", msg + " (works but 3.12+ recommended)")
    return _result(FAIL, "python_version", msg + " (need 3.10+, prefer 3.12)")


REQUIRED_PACKAGES = [
    ("numpy", None),
    ("pandas", None),
    ("pyarrow", None),
    ("ib_insync", None),
    ("requests", None),
    ("yfinance", None),
    ("fastapi", None),
    ("uvicorn", None),
]


def check_packages() -> list[dict]:
    out = []
    for pkg, _attr in REQUIRED_PACKAGES:
        try:
            mod = importlib.import_module(pkg)
            ver = getattr(mod, "__version__", "?")
            out.append(_result(PASS, f"pkg:{pkg}", f"v{ver}"))
        except ImportError as exc:
            out.append(_result(FAIL, f"pkg:{pkg}",
                               f"NOT IMPORTABLE — `pip install {pkg}` ({exc})"))
        except RuntimeError as exc:
            # ib_insync on Python 3.14: eventkit.util.get_event_loop() raises
            # because asyncio no longer auto-creates loops. The package IS
            # installed but unusable on 3.14. Diagnose specifically.
            if pkg == "ib_insync" and "event loop" in str(exc).lower():
                py = sys.version_info
                if py.minor >= 14:
                    out.append(_result(WARN, f"pkg:{pkg}",
                                       f"installed but unusable on Python {py.major}.{py.minor} "
                                       f"(ib_insync's eventkit dep needs auto-event-loop, removed in 3.14). "
                                       f"For IBKR workloads use `py -3.12` explicitly."))
                else:
                    out.append(_result(FAIL, f"pkg:{pkg}", f"load-time error: {exc!r}"))
            else:
                out.append(_result(FAIL, f"pkg:{pkg}", f"load-time error: {exc!r}"))
        except Exception as exc:
            out.append(_result(FAIL, f"pkg:{pkg}", f"unexpected: {exc!r}"))
    return out


def check_skill_root() -> dict:
    skill_md = SKILL_DIR / "SKILL.md"
    if skill_md.exists():
        return _result(PASS, "skill_root", f"{SKILL_DIR}")
    return _result(FAIL, "skill_root",
                   f"SKILL.md not found at {SKILL_DIR} — Dropbox sync incomplete?")


def check_bars_store_readable() -> list[dict]:
    out = []
    try:
        import bars_store
        for tf in ("daily", "3min"):
            syms = bars_store.list_symbols(tf)
            n = len(syms)
            if tf == "daily" and n >= 1000:
                out.append(_result(PASS, f"bars_store:{tf}", f"{n} symbols on disk"))
            elif tf == "daily" and n > 0:
                out.append(_result(WARN, f"bars_store:{tf}",
                                   f"only {n} symbols (expected ~1500)"))
            elif tf == "daily":
                out.append(_result(FAIL, f"bars_store:{tf}",
                                   "no daily parquets — run yfinance seed first"))
            elif tf == "3min" and n >= 500:
                out.append(_result(PASS, f"bars_store:{tf}", f"{n} symbols on disk"))
            elif tf == "3min" and n > 0:
                out.append(_result(WARN, f"bars_store:{tf}",
                                   f"only {n} symbols (re-seed in progress?)"))
            else:
                out.append(_result(WARN, f"bars_store:{tf}",
                                   "no 3min parquets yet (expected — 180d re-seed pending)"))
    except Exception as exc:
        out.append(_result(FAIL, "bars_store", f"unreadable: {exc!r}"))
    return out


def check_disk_space() -> dict:
    try:
        total, used, free = shutil.disk_usage(str(SKILL_DIR))
        free_gb = free / 1e9
        if free_gb >= 100:
            return _result(PASS, "disk_space", f"{free_gb:.1f} GB free on {SKILL_DIR.anchor}")
        if free_gb >= 50:
            return _result(WARN, "disk_space",
                           f"{free_gb:.1f} GB free (180d re-seed may need ~20-30 GB; OK but tight)")
        return _result(FAIL, "disk_space",
                       f"only {free_gb:.1f} GB free (need >50 GB headroom)")
    except Exception as exc:
        return _result(FAIL, "disk_space", f"could not check: {exc!r}")


def check_ibc_credentials() -> dict:
    p = SKILL_DIR / "ibc" / "credentials.txt"
    if not p.exists():
        return _result(FAIL, "ibc_credentials",
                       f"missing {p} — see HERMES_SETUP.md Phase 2 step 6")
    try:
        body = p.read_text(encoding="utf-8")
        # Don't print the actual creds; just verify shape
        has_user = "IbLoginId=" in body and not body.split("IbLoginId=")[1].split("\n")[0].strip() == ""
        has_pass = "IbPassword=" in body and not body.split("IbPassword=")[1].split("\n")[0].strip() == ""
        if has_user and has_pass:
            return _result(PASS, "ibc_credentials", "IbLoginId + IbPassword set")
        return _result(FAIL, "ibc_credentials",
                       "file exists but IbLoginId or IbPassword is empty")
    except Exception as exc:
        return _result(FAIL, "ibc_credentials", f"unreadable: {exc!r}")


def check_config_json() -> dict:
    p = SKILL_DIR / "config.json"
    if not p.exists():
        return _result(WARN, "config_json",
                       f"{p} not found — copy from config.example.json + customize for this PC")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        cid = cfg.get("ibkr_client_id")
        if cid is None:
            return _result(WARN, "config_json", "ibkr_client_id not set in config.json")
        # Hermes should be 84; warn if it's still 71/80/83/99 (laptop's IDs)
        if cid in (84, 85):
            return _result(PASS, "config_json", f"ibkr_client_id={cid} (Hermes range)")
        if cid in (71, 80, 83, 98, 99):
            return _result(WARN, "config_json",
                           f"ibkr_client_id={cid} looks like a LAPTOP id — set to 84 on Hermes")
        return _result(PASS, "config_json", f"ibkr_client_id={cid}")
    except Exception as exc:
        return _result(FAIL, "config_json", f"unparseable: {exc!r}")


def check_ibkr_handshake(probe_client_id: int = 85) -> dict:
    """Lightweight TCP probe to IB Gateway (port 4002 = paper, 7497 = TWS paper).
    Does NOT do a full ib_insync connect — that would steal an IBKR slot.
    Just verifies the socket is reachable, which tells us IBC + Gateway are up."""
    host = "127.0.0.1"
    paper_port = 4002
    tws_port = 7497
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((host, paper_port))
        sock.close()
        return _result(PASS, "ibkr_handshake",
                       f"port {paper_port} reachable (IB Gateway paper)")
    except (ConnectionRefusedError, socket.timeout, OSError):
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((host, tws_port))
        sock.close()
        return _result(WARN, "ibkr_handshake",
                       f"port {tws_port} reachable (TWS paper, not Gateway — works but Gateway preferred)")
    except (ConnectionRefusedError, socket.timeout, OSError):
        return _result(FAIL, "ibkr_handshake",
                       f"neither port {paper_port} nor {tws_port} reachable — IBC/Gateway/TWS not running?")
    finally:
        try: sock.close()
        except Exception: pass


def check_adapter_registry() -> dict:
    try:
        from review import _adapter_registry
        known = _adapter_registry.known()
        if "ditp_p2" in known:
            return _result(PASS, "backtest_adapter", f"registered: {known}")
        return _result(FAIL, "backtest_adapter",
                       f"ditp_p2 not registered (found: {known})")
    except Exception as exc:
        return _result(FAIL, "backtest_adapter", f"registry unloadable: {exc!r}")


def check_scanner_imports() -> dict:
    try:
        from strategy.DITP.scanner import detect_p2, P2Config  # noqa: F401
        return _result(PASS, "scanner_imports", "scanner.detect_p2 importable")
    except Exception as exc:
        return _result(FAIL, "scanner_imports", f"import failed: {exc!r}")


def check_decision_engine() -> dict:
    try:
        from strategy.DITP import _decision_engine as de
        if all(hasattr(de, fn) for fn in
               ("entry_signal", "stop_price", "target_price", "tradeability_ok")):
            return _result(PASS, "decision_engine",
                           f"v{de.__version__} — all Phase 1 primitives present")
        return _result(FAIL, "decision_engine",
                       "module loads but missing required functions")
    except Exception as exc:
        return _result(FAIL, "decision_engine", f"import failed: {exc!r}")


# ---- Main ----

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-ibkr", action="store_true",
                    help="skip the IBKR handshake (useful for running on the laptop "
                         "or before IB Gateway is set up)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable JSON output")
    args = ap.parse_args()

    results: list[dict] = []
    results.append(check_skill_root())
    results.append(check_python_version())
    results.extend(check_packages())
    results.extend(check_bars_store_readable())
    results.append(check_disk_space())
    results.append(check_config_json())
    results.append(check_ibc_credentials())
    results.append(check_scanner_imports())
    results.append(check_decision_engine())
    results.append(check_adapter_registry())
    if not args.skip_ibkr:
        results.append(check_ibkr_handshake())

    if args.json:
        out = {
            "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "skill_dir": str(SKILL_DIR),
            "hostname": socket.gethostname(),
            "checks": results,
            "summary": {
                "pass": sum(1 for r in results if r["status"] == PASS),
                "warn": sum(1 for r in results if r["status"] == WARN),
                "fail": sum(1 for r in results if r["status"] == FAIL),
            },
        }
        print(json.dumps(out, indent=2))
    else:
        sys.stdout.write(f"\nHermes pre-flight on host={socket.gethostname()} at {datetime.now()}\n")
        sys.stdout.write(f"skill dir: {SKILL_DIR}\n\n")
        for r in results:
            _print(r)
        n_pass = sum(1 for r in results if r["status"] == PASS)
        n_warn = sum(1 for r in results if r["status"] == WARN)
        n_fail = sum(1 for r in results if r["status"] == FAIL)
        sys.stdout.write(f"\nresult: {n_pass} PASS  |  {n_warn} WARN  |  {n_fail} FAIL\n")
        if n_fail:
            sys.stdout.write("\nFix all FAILs before kicking off the 180-day re-seed.\n")
        elif n_warn:
            sys.stdout.write("\nWARNs are non-blocking but worth reviewing.\n")
        else:
            sys.stdout.write("\nAll checks pass. Hermes is ready.\n")

    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
