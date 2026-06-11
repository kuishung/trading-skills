r"""Headless ingest-status readout - the tray progress window, as text.

Prints the exact same signals the tray's progress window shows (NOW activity,
ingest progress, Supervisor/Gateway liveness, completed-through date, deep-check
result + live deep-check progress) but to the console, so you can check status
over RDP / PowerShell on Hermes without opening the GUI tray.

It REUSES the tray's data-getters (no duplicated logic) - `tray_status.py`
imports cleanly because its GUI/main code is guarded behind `__main__`.

Usage
-----
  py -3.12 dashboard_intraday\show_ingest_status.py            # one-shot
  py -3.12 dashboard_intraday\show_ingest_status.py --json     # machine-readable
  py -3.12 dashboard_intraday\show_ingest_status.py --watch 3  # refresh every 3s
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))


def _import_tray():
    """Import tray_status for its data-getters. tray_status hard-exits if
    pystray/PIL are missing, but those are GUI-only - the getters we use read
    files + shell out and never touch them. So if the GUI deps are absent
    (e.g. a box that doesn't run the tray), stub them and import anyway."""
    try:
        import tray_status as ts            # noqa: E402
        return ts
    except SystemExit:
        pil = types.ModuleType("PIL")
        for n in ("Image", "ImageDraw", "ImageFont"):
            setattr(pil, n, types.SimpleNamespace())
        sys.modules.setdefault("PIL", pil)
        sys.modules.setdefault("pystray", types.ModuleType("pystray"))
        import tray_status as ts            # noqa: E402
        return ts


ts = _import_tray()


def collect() -> dict:
    """Gather every status signal into one dict (each getter is best-effort)."""
    def safe(fn, default=None):
        try:
            return fn()
        except Exception as exc:                 # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"} if default is None else default

    ip = safe(ts.get_item_progress)
    prog = safe(ts.get_progress, {})
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "item_progress": ip,
        "progress": prog,
        "supervisor": safe(ts.get_supervisor_status, {}),
        "gateway": safe(ts.get_gateway_status, {}),
        "completed_through": safe(ts.get_completed_through, {}),
        "deepcheck": safe(ts.get_deepcheck_status, {}),
        "deepcheck_progress": safe(ts.get_deepcheck_progress),
    }


def _now_line(s: dict) -> str:
    dcp = s.get("deepcheck_progress")
    ip = s.get("item_progress") or {}
    if dcp:
        f = dcp["frac"] * 100
        return (f"deep check running - {dcp['done']:,}/{dcp['total']:,} "
                f"files ({f:.0f}%)")
    if ip and ip.get("items_total"):
        if ip.get("finished"):
            nb = ip.get("new_bars", 0)
            return ("ingest complete - up to date" if nb == 0
                    else f"ingest complete - +{nb:,} new")
        return (f"ingest running - {ip.get('items_done', 0):,}/"
                f"{ip.get('items_total', 0):,} checked | +{ip.get('new_bars', 0):,} new")
    return "idle - nothing running"


def render(s: dict) -> str:
    out: list[str] = []
    out.append(f"TradeHunter - Ingest Status   ({s['ts']})")
    out.append("=" * 60)
    out.append(f"NOW:  {_now_line(s)}")
    out.append("")

    # INGEST
    out.append("INGEST")
    ip = s.get("item_progress") or {}
    if ip and ip.get("items_total"):
        di, tt, nb = ip.get("items_done", 0), ip.get("items_total", 0), ip.get("new_bars", 0)
        pct = (di / tt * 100) if tt else 0.0
        out.append(f"  {pct:5.1f}%   {di:,} / {tt:,} checked   |   +{nb:,} new")
        if ip.get("latest_sym"):
            out.append(f"  latest: {ip['latest_sym']} {ip.get('latest_tf', '')}".rstrip())
        out.append(f"  state:  {'finished' if ip.get('finished') else 'checking...'}")
    else:
        prog = s.get("progress") or {}
        pct = (prog.get("progress_fraction") or 0.0) * 100
        done, target = prog.get("symbols_done", 0), prog.get("target", 0)
        out.append(f"  {pct:5.1f}%   {done} / {target} symbols")
        if prog.get("latest_symbol"):
            out.append(f"  latest: {prog['latest_symbol']}")
    ct = s.get("completed_through") or {}
    if ct.get("date"):
        out.append(f"  completed through: {ct['date']}  ({ct.get('source', '')})")
    else:
        out.append("  completed through: (no data yet)")
    out.append("")

    # SERVICES
    out.append("SERVICES")
    sup = s.get("supervisor") or {}
    if sup.get("alive"):
        act = sup.get("action")
        out.append(f"  Supervisor: LIVE" + (f"  |  {act}" if act else ""))
    else:
        out.append("  Supervisor: DOWN  (not running - install/start the task)")
    gw = s.get("gateway") or {}
    if gw.get("up"):
        out.append(f"  Gateway:    LIVE  (port {gw.get('port', '?')})")
    else:
        out.append(f"  Gateway:    down  (port {gw.get('port', '?')})")
    out.append("")

    # DEEP CHECK
    out.append("DEEP CHECK")
    dc = s.get("deepcheck") or {}
    st = dc.get("status", "-")
    tstamp = f"   {dc['ts']}" if dc.get("ts") else ""
    out.append(f"  result: {str(st).upper()} - {dc.get('summary', '')}{tstamp}")
    dcp = s.get("deepcheck_progress")
    if dcp:
        f = dcp["frac"] * 100
        ph = {"tier1": "metadata pass", "tier2": "full-read pass"}.get(dcp["phase"], dcp["phase"])
        out.append(f"  running | {ph} | {dcp['done']:,}/{dcp['total']:,} files ({f:.0f}%)")
    else:
        out.append("  running: idle")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Show the tray ingest status as text.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--watch", type=float, default=0,
                    help="refresh every N seconds (Ctrl-C to stop)")
    args = ap.parse_args()

    def once():
        s = collect()
        if args.json:
            print(json.dumps(s, indent=2, default=str))
        else:
            print(render(s))

    if args.watch and args.watch > 0:
        try:
            while True:
                # clear screen (Windows 'cls' equiv via ANSI; harmless if unsupported)
                sys.stdout.write("\033[2J\033[H")
                once()
                sys.stdout.flush()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    else:
        once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
