"""Scanner-driven plan builder — the bridge that wires Stage 1 scanner output
into the bot's plan_<date>.json so trade_day.py runs fully automated.

Loops every --interval seconds during the ET window (default 09:00-09:24).
Each iteration:

  1. Reads the latest scanner.snapshot event from state/events_*.jsonl.
     If empty (scanner not yet emitted), skips this cycle.
  2. For each candidate symbol from the snapshot, pulls PM bars + latest
     quote via the configured data provider (_common abstractions; routes
     to IBKR when config.data_provider == 'ibkr').
  3. Applies GUNS qualification filters:
       - PM bars present (else 'no_pm_bars')
       - pm_high >= $1.50 (deck)
       - pm_volume >= 30,000 (deck)
       - bid/ask spread <= cfg.spread_max_cents
       - news headline does NOT contain M&A keywords (reject the trap)
         — uses Alpaca News API if creds are available; soft-skip otherwise
       - is_consolidating_near_pm_high → Setup-1 eligibility
  4. Writes state/plan_<date>.json (atomic via temp+rename).
  5. Emits plan.refresh / plan.candidate.added / plan.candidate.rejected events.

Exits cleanly at end_et even if --interval keeps it busy. Catches SIGINT.

Run:
    py scripts/auto_plan.py
    py scripts/auto_plan.py --start-et 09:00 --end-et 09:24 --interval 60

Limitations (documented, not yet enforced):
  - Daily-chart EMA filter (>9/20/50/200 EMA on daily) is not applied yet
    — would require per-symbol daily fetches with caching to stay under
    IBKR's 60-req/10-min pacing limit. Future Stage 2 work.
  - Float < 100M filter is skipped (no free feed exposes it).
  - US market holidays not special-cased.
"""
from __future__ import annotations

# Python 3.14 / eventkit shim must run before ib_insync imports inside _common
# touch the asyncio event loop.
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import argparse
import json
import os
import signal
import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _events import emit  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"

# Lazy imports for _common + signals so a missing dep surfaces a clear error
# rather than an opaque traceback on script load.
def _lazy_imports():
    from _common import (
        et_now, et_today_iso, get_latest_quote, get_pm_bars, load_config,
        plan_path, watchlist_path,
    )
    from signals import (
        build_setup1_plan, is_consolidating_near_pm_high, pm_summary, split_pm_rth,
        spread_ok,
    )
    return {
        "et_now": et_now, "et_today_iso": et_today_iso,
        "get_latest_quote": get_latest_quote, "get_pm_bars": get_pm_bars,
        "load_config": load_config, "plan_path": plan_path,
        "watchlist_path": watchlist_path,
        "build_setup1_plan": build_setup1_plan,
        "is_consolidating_near_pm_high": is_consolidating_near_pm_high,
        "pm_summary": pm_summary, "split_pm_rth": split_pm_rth,
        "spread_ok": spread_ok,
    }


# Keywords that indicate the catalyst is M&A / takeover-related — the deck
# explicitly tells the trader to avoid these (the upside is capped at the
# acquisition price). Case-insensitive substring match on news headlines.
MNA_KEYWORDS = (
    "acquire", "acquires", "acquired", "acquiring", "acquisition",
    "buyout", "buy out", "take private", "going private",
    "merger", "merging", "merge with", "merge into",
    "takeover", "take over",
    "tender offer",
    "to be acquired", "to acquire",
)


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


# Scanners auto_plan considers as candidate sources during PM (04:00-09:30).
# Order is preference: TOP_PERC_GAIN is the primary baseline; the leading
# scanners contribute newer signals on top.
PM_CANDIDATE_SCANNERS = ("TOP_PERC_GAIN", "TOP_VOLUME_RATE", "HOT_BY_OPT_VOLUME")
# Scanner code we treat as a REJECT set (not candidate source).
HALTED_SCANNER = "HALTED"


def _latest_snapshots_by_scanner(date_iso: str) -> dict[str, list[str]]:
    """Return {scan_code: [symbols]} from the most recent snapshot of each
    scan code in today's events file. One pass, no double-read."""
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


def _latest_scanner_snapshot(date_iso: str) -> list[str] | None:
    """Union of PM-relevant scanners minus HALTED reject set.

    Preserves rank-priority of TOP_PERC_GAIN — it's the primary baseline;
    additional leading-scanner symbols are appended after."""
    by_code = _latest_snapshots_by_scanner(date_iso)
    if not by_code:
        return None
    halted = set(by_code.get(HALTED_SCANNER, []))
    out: list[str] = []
    seen: set[str] = set()
    for code in PM_CANDIDATE_SCANNERS:
        for sym in by_code.get(code, []):
            if sym in halted or sym in seen:
                continue
            out.append(sym)
            seen.add(sym)
    return out or None


def _check_mna(symbols: list[str], cfg: dict) -> dict[str, str]:
    """Return {symbol: reason} for symbols whose recent news headlines look
    like M&A. Symbols not in the dict have no M&A flag (either no news at
    all, or news is clean). News fetch is best-effort; failure -> empty dict
    so we don't accidentally reject everyone."""
    if not symbols:
        return {}
    try:
        from _common import load_alpaca_env
        key, secret = load_alpaca_env(cfg)
    except SystemExit:
        return {}
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError:
        return {}

    client = NewsClient(key, secret)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=18)
    flagged: dict[str, str] = {}
    # One batched query — Alpaca News supports a `symbols` list.
    try:
        req = NewsRequest(
            symbols=",".join(symbols),
            start=cutoff,
            limit=50, sort="desc", include_content=False,
        )
        resp = client.get_news(req)
        items = getattr(resp, "data", None) or []
        if isinstance(items, dict):
            items = items.get("news", [])
        for n in items:
            headline = (getattr(n, "headline", "") or "").lower()
            for kw in MNA_KEYWORDS:
                if kw in headline:
                    for s in getattr(n, "symbols", None) or []:
                        s = s.upper()
                        if s not in flagged:
                            flagged[s] = f"news: '{kw}' in headline"
                    break
    except Exception as exc:
        sys.stderr.write(f"M&A news check failed (soft-skip): {exc}\n")
    return flagged


def _build_plan(symbols: list[str], cfg: dict, fake_now: str | None,
                imp: dict, date_iso: str) -> dict:
    """Same shape as scan_premarket.build_plan, with the added M&A rejection."""
    if not symbols:
        return {
            "date": date_iso,
            "built_at_et": imp["et_now"](fake_now).isoformat(),
            "source": "auto_plan (scanner-driven)",
            "tickers": {},
        }

    bars_by_sym = imp["get_pm_bars"](symbols, cfg, fake_now)
    quotes = imp["get_latest_quote"](symbols, cfg)
    mna_flags = _check_mna(symbols, cfg)

    plan: dict = {
        "date": date_iso,
        "built_at_et": imp["et_now"](fake_now).isoformat(),
        "source": "auto_plan (scanner-driven)",
        "config_snapshot": {
            "take_profit_R": cfg["take_profit_R"],
            "risk_per_trade_pct": cfg["risk_per_trade_pct"],
            "max_open_concurrent_positions": cfg["max_open_concurrent_positions"],
            "spread_max_cents": cfg["spread_max_cents"],
            "pm_consolidation_window_min": cfg["pm_consolidation_window_min"],
            "pm_consolidation_band_pct": cfg["pm_consolidation_band_pct"],
        },
        "tickers": {},
    }

    for sym in symbols:
        pm_bars, _ = imp["split_pm_rth"](bars_by_sym.get(sym, []))
        ctx: dict = {"symbol": sym, "pm_bar_count": len(pm_bars)}
        summary = imp["pm_summary"](pm_bars)
        if summary:
            ctx.update(summary)
        q = quotes.get(sym) if isinstance(quotes, dict) else None
        if q is not None:
            ctx["bid"] = q.get("bid")
            ctx["ask"] = q.get("ask")

        rejections: list[str] = []
        pm_high = ctx.get("pm_high")
        if pm_high is None or ctx.get("pm_bar_count", 0) == 0:
            rejections.append("no_pm_bars")
        else:
            if pm_high < 1.50:
                rejections.append(f"price_below_1.50 ({pm_high})")
            if ctx.get("pm_volume", 0) < 30000:
                rejections.append(f"pm_volume_below_30k ({ctx.get('pm_volume')})")
            if ctx.get("bid") and ctx.get("ask"):
                if not imp["spread_ok"](ctx["bid"], ctx["ask"], cfg["spread_max_cents"]):
                    rejections.append(f"spread_too_wide ({ctx['ask']-ctx['bid']:.2f})")
        if sym in mna_flags:
            rejections.append(mna_flags[sym])

        row = {
            "context": {k: v for k, v in ctx.items() if not k.startswith("_")},
            "rejections": rejections,
            "setup1_eligible": False,
            "setup1": None,
        }
        if not rejections and pm_high is not None:
            if imp["is_consolidating_near_pm_high"](
                pm_bars, pm_high,
                cfg["pm_consolidation_window_min"],
                cfg["pm_consolidation_band_pct"],
            ):
                row["setup1_eligible"] = True
                row["setup1"] = imp["build_setup1_plan"](
                    sym, pm_high, ctx, cfg["take_profit_R"],
                )
        plan["tickers"][sym] = row
    return plan


def _write_plan_atomic(plan: dict, target: Path) -> None:
    """Write to a temp file then rename, so trade_day never observes a
    half-written JSON."""
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, target)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scanner-driven plan builder")
    ap.add_argument("--start-et", default="09:00",
                    help="Start time ET (HH:MM). Default 09:00.")
    ap.add_argument("--end-et", default="09:24",
                    help="Hard stop ET (HH:MM). Default 09:24 — one minute "
                         "before trade_day's phase_setup1 fires at 09:25.")
    ap.add_argument("--interval", type=int, default=60,
                    help="Seconds between refreshes (default 60).")
    ap.add_argument("--max-symbols", type=int, default=15,
                    help="Cap on candidates fed into _build_plan per refresh.")
    args = ap.parse_args()

    imp = _lazy_imports()
    cfg = imp["load_config"]()
    date_iso = imp["et_today_iso"](None)
    target_path = imp["plan_path"](date_iso)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Time gates
    start_dt = _today_et_at(args.start_et)
    end_dt = _today_et_at(args.end_et)
    now = _now_et()
    if end_dt <= now:
        print(f"end-et {args.end_et} already passed (now ET {now.strftime('%H:%M')}). "
              f"Nothing to do — exit.")
        emit("autoplan.skipped", {
            "reason": "end_et already passed",
            "now_et": now.strftime("%H:%M"),
        })
        return
    if now < start_dt:
        wait_s = (start_dt - now).total_seconds()
        print(f"Idle until {args.start_et} ET — waiting {int(wait_s)}s.")
        emit("autoplan.waiting", {"until_et": args.start_et, "wait_s": int(wait_s)})
        stop_flag = [False]

        def _wait_sig(_s, _f): stop_flag[0] = True
        signal.signal(signal.SIGINT, _wait_sig)
        try: signal.signal(signal.SIGTERM, _wait_sig)
        except Exception: pass
        while not stop_flag[0] and _now_et() < start_dt:
            _time.sleep(min(15, max(1, (start_dt - _now_et()).total_seconds())))
        if stop_flag[0]:
            emit("autoplan.stop", {"reason": "interrupted while waiting"})
            return

    emit("autoplan.start", {
        "start_et": args.start_et, "end_et": args.end_et,
        "interval_s": args.interval,
    })
    print(f"auto_plan running until {args.end_et} ET — refreshing every "
          f"{args.interval}s.")

    stop_flag = [False]

    def _on_signal(_s, _f): stop_flag[0] = True
    signal.signal(signal.SIGINT, _on_signal)
    try: signal.signal(signal.SIGTERM, _on_signal)
    except Exception: pass

    last_emitted: set[str] = set()

    while not stop_flag[0] and _now_et() < end_dt:
        try:
            symbols = _latest_scanner_snapshot(date_iso) or []
            symbols = symbols[: args.max_symbols]
            if not symbols:
                emit("autoplan.no_scanner_data", {})
                print(f"[{_now_et().strftime('%H:%M:%S')}] no scanner snapshot yet")
            else:
                plan = _build_plan(symbols, cfg, None, imp, date_iso)
                _write_plan_atomic(plan, target_path)

                eligible = [s for s, row in plan["tickers"].items()
                            if row.get("setup1_eligible")]
                rejected = {s: row["rejections"]
                            for s, row in plan["tickers"].items()
                            if row.get("rejections")}
                emit("plan.refresh", {
                    "n_candidates": len(symbols),
                    "n_eligible": len(eligible),
                    "n_rejected": len(rejected),
                    "eligible": eligible,
                })
                # Per-symbol diff events
                eligible_set = set(eligible)
                for s in eligible_set - last_emitted:
                    emit("plan.candidate.added", {"symbol": s})
                for s in last_emitted - eligible_set:
                    emit("plan.candidate.dropped", {"symbol": s})
                last_emitted = eligible_set

                ts = _now_et().strftime("%H:%M:%S")
                print(f"[{ts}] {len(symbols)} cand · {len(eligible)} eligible"
                      f"{' (' + ', '.join(eligible) + ')' if eligible else ''}"
                      f" · {len(rejected)} rejected")
        except Exception as exc:
            sys.stderr.write(f"auto_plan iteration error: {exc}\n")
            emit("autoplan.error", {"error": str(exc)})

        # Sleep in small chunks so SIGTERM is responsive.
        slept = 0.0
        while slept < args.interval and not stop_flag[0] and _now_et() < end_dt:
            chunk = min(2.0, args.interval - slept)
            _time.sleep(chunk)
            slept += chunk

    emit("autoplan.stop", {
        "reason": "signal" if stop_flag[0] else "end_et reached",
    })
    print("auto_plan stopped.")


if __name__ == "__main__":
    main()
