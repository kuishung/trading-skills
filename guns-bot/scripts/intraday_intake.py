"""Intraday + closing setup detector — observe-only.

Runs alongside the bot during RTH. Watches the scanner output and applies
two detectors to each candidate symbol:

  Setup 4 (intraday bull flag)     — active 09:32 - 15:50 ET
  Setup 6 (closing-bell breakout)  — active 15:50 - 15:58 ET (new strategy,
                                     not in Adam Khoo's deck)

For each qualifier, this script writes a candidate row to
  state/intraday_candidates_<date>.jsonl
and emits a setup4.candidate / setup6.candidate event so the dashboard
shows what would have been traded. **Does NOT submit orders.** Wiring into
trade_day.py for live execution is a separate step.

Run:
    py scripts/intraday_intake.py
    py scripts/intraday_intake.py --start-et 09:32 --end-et 15:58
"""
from __future__ import annotations

# Python 3.14 / eventkit shim must run before ib_insync touches asyncio.
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import argparse
import json
import signal
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _events import emit  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"

# Lazy imports — pulled in once after the argparse so a missing dep yields a
# clear error.
def _lazy():
    from _common import (
        et_today_iso, get_pm_bars, load_config,
    )
    from signals import (
        evaluate_setup4_bull_flag,
        evaluate_setup6_closing_breakout,
        split_pm_rth,
    )
    return locals()


# Window definitions (ET) — these are the windows where each setup is active.
SETUP4_WINDOW = ("09:32", "15:50")
SETUP6_WINDOW = ("15:50", "15:58")


def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        return pytz.timezone("America/New_York")


def _now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(_et_tz())


def _today_et_at(hhmm: str) -> datetime:
    hh, mm = hhmm.split(":")
    return _now_et().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def _in_window(now_et: datetime, window: tuple[str, str]) -> bool:
    hm = now_et.strftime("%H:%M")
    return window[0] <= hm < window[1]


# Scanners for the RTH window (09:32–15:50). TOP_OPEN_PERC_GAIN is the
# primary baseline; HOT_BY_VOLUME catches late accumulation; the two
# leading scanners surface emerging movers early.
RTH_CANDIDATE_SCANNERS = (
    "TOP_OPEN_PERC_GAIN", "HOT_BY_VOLUME",
    "TOP_VOLUME_RATE", "HOT_BY_OPT_VOLUME",
)
# Scanners for the closing window (15:50–15:58). Buy imbalance is the
# leading signal here — actual queued orders, not price reactions.
CLOSE_CANDIDATE_SCANNERS = (
    "TOP_STOCK_BUY_IMBALANCE_ADV_RATIO",
    "HOT_BY_VOLUME", "TOP_VOLUME_RATE",
)
HALTED_SCANNER = "HALTED"


def _latest_snapshots_by_scanner(date_iso: str) -> dict[str, list[str]]:
    """Return {scan_code: [symbols]} from the most recent snapshot of each
    scan code in today's events file."""
    log = STATE_DIR / f"events_{date_iso}.jsonl"
    if not log.exists():
        return {}
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}
    latest: dict[str, list[str]] = {}
    seen_codes: set[str] = set()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "scanner.snapshot":
            continue
        payload = ev.get("payload") or {}
        code = payload.get("scan_code")
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        rows = payload.get("rows") or []
        latest[code] = [r["symbol"] for r in rows if r.get("symbol")]
    return latest


def _latest_scanner_snapshot(date_iso: str,
                             prefer_closing: bool = False) -> list[str] | None:
    """Union of relevant scanners for the current window, minus HALTED.

    prefer_closing=True picks the close-window scanner set (auction-imbalance
    primary); False picks the RTH-window set (TOP_OPEN_PERC_GAIN primary).
    Symbols are deduplicated; order preserves primary-baseline rank."""
    by_code = _latest_snapshots_by_scanner(date_iso)
    if not by_code:
        return None
    halted = set(by_code.get(HALTED_SCANNER, []))
    pool = CLOSE_CANDIDATE_SCANNERS if prefer_closing else RTH_CANDIDATE_SCANNERS
    out: list[str] = []
    seen: set[str] = set()
    for code in pool:
        for sym in by_code.get(code, []):
            if sym in halted or sym in seen:
                continue
            out.append(sym)
            seen.add(sym)
    return out or None


def _append_candidate(date_iso: str, payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = STATE_DIR / f"intraday_candidates_{date_iso}.jsonl"
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        }, default=str) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Intraday + closing setup detector")
    ap.add_argument("--start-et", default="09:32",
                    help="Start ET (HH:MM). Default 09:32 — one bar after open "
                         "so the first 1-min RTH bar exists.")
    ap.add_argument("--end-et", default="15:58",
                    help="Hard stop ET. Default 15:58 — two minutes before "
                         "close so Setup 6 has time to exit MOC.")
    ap.add_argument("--interval", type=int, default=60,
                    help="Seconds between scans (default 60).")
    ap.add_argument("--max-symbols", type=int, default=12,
                    help="Cap on candidates evaluated per cycle (default 12).")
    args = ap.parse_args()

    imp = _lazy()
    cfg = imp["load_config"]()
    date_iso = imp["et_today_iso"](None)

    # ---------- Time gate ----------
    start_dt = _today_et_at(args.start_et)
    end_dt = _today_et_at(args.end_et)
    now = _now_et()
    if end_dt <= now:
        emit("intraday.skipped", {
            "reason": "end_et already passed",
            "now_et": now.strftime("%H:%M"),
        })
        print(f"end-et {args.end_et} already passed (now {now.strftime('%H:%M')}). Exit.")
        return
    if now < start_dt:
        wait_s = int((start_dt - now).total_seconds())
        print(f"Idle until {args.start_et} ET — waiting {wait_s}s.")
        emit("intraday.waiting", {"until_et": args.start_et, "wait_s": wait_s})
        stop_flag = [False]

        def _wait_sig(_s, _f): stop_flag[0] = True

        signal.signal(signal.SIGINT, _wait_sig)
        try: signal.signal(signal.SIGTERM, _wait_sig)
        except Exception: pass
        while not stop_flag[0] and _now_et() < start_dt:
            _time.sleep(min(15, max(1, (start_dt - _now_et()).total_seconds())))
        if stop_flag[0]:
            emit("intraday.stop", {"reason": "interrupted while waiting"})
            return

    emit("intraday.start", {
        "start_et": args.start_et, "end_et": args.end_et,
        "interval_s": args.interval,
        "setup4_window": SETUP4_WINDOW,
        "setup6_window": SETUP6_WINDOW,
    })
    print(f"intraday_intake running until {args.end_et} ET — every {args.interval}s.")

    stop_flag = [False]

    def _on_sig(_s, _f): stop_flag[0] = True

    signal.signal(signal.SIGINT, _on_sig)
    try: signal.signal(signal.SIGTERM, _on_sig)
    except Exception: pass

    # Per-symbol cooldown so we don't re-emit the same candidate every cycle.
    last_emitted: dict[str, float] = {}
    COOLDOWN_S = 5 * 60  # 5 min — re-emit allowed after this gap

    while not stop_flag[0] and _now_et() < end_dt:
        try:
            now_et = _now_et()
            do_setup4 = _in_window(now_et, SETUP4_WINDOW)
            do_setup6 = _in_window(now_et, SETUP6_WINDOW)
            if not (do_setup4 or do_setup6):
                # Outside both detector windows — quiet sleep.
                _time.sleep(min(30, args.interval))
                continue

            symbols = (_latest_scanner_snapshot(date_iso, prefer_closing=do_setup6)
                       or [])[: args.max_symbols]
            if not symbols:
                emit("intraday.no_scanner_data", {})
                _time.sleep(args.interval)
                continue

            # Fetch full-day 1-min bars per symbol. _common.get_pm_bars covers
            # 04:00 onwards but split_pm_rth lets us slice RTH out cleanly.
            bars_by_sym = imp["get_pm_bars"](symbols, cfg, None)

            for sym in symbols:
                bars = bars_by_sym.get(sym) or []
                pm_bars, rth_bars = imp["split_pm_rth"](bars)
                if not rth_bars:
                    continue

                # Cooldown check
                last = last_emitted.get(sym, 0)
                if (_time.time() - last) < COOLDOWN_S:
                    continue

                plan = None
                detected_setup = None
                if do_setup4:
                    plan = imp["evaluate_setup4_bull_flag"](
                        rth_bars, sym, cfg["take_profit_R"],
                    )
                    if plan:
                        detected_setup = 4
                if not plan and do_setup6:
                    open_price = rth_bars[0]["o"]
                    plan = imp["evaluate_setup6_closing_breakout"](
                        rth_bars, open_price, sym, cfg["take_profit_R"],
                    )
                    if plan:
                        detected_setup = 6

                if plan is None:
                    continue

                event_name = f"setup{detected_setup}.candidate"
                payload = {
                    "symbol": sym, "setup": detected_setup, **plan,
                    "n_rth_bars": len(rth_bars),
                    "detected_at_et": now_et.strftime("%H:%M:%S"),
                }
                emit(event_name, payload)
                _append_candidate(date_iso, payload)
                last_emitted[sym] = _time.time()
                print(f"[{now_et.strftime('%H:%M:%S')}] Setup {detected_setup} -> {sym}  "
                      f"entry≥${plan['entry_stop_trigger']:.2f}  "
                      f"stop ${plan['stop_loss']:.2f}  "
                      f"tgt ${plan['take_profit']:.2f}")
        except Exception as exc:
            sys.stderr.write(f"intraday_intake iteration error: {exc}\n")
            emit("intraday.error", {"error": str(exc)})

        # Responsive sleep to honour SIGTERM promptly.
        slept = 0.0
        while slept < args.interval and not stop_flag[0] and _now_et() < end_dt:
            chunk = min(2.0, args.interval - slept)
            _time.sleep(chunk)
            slept += chunk

    emit("intraday.stop", {
        "reason": "signal" if stop_flag[0] else "end_et reached",
    })
    print("intraday_intake stopped.")


if __name__ == "__main__":
    main()
