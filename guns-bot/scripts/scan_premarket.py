"""Pre-market watchlist builder for guns-bot.

Two modes:
  1. Manual file mode (default) — reads state/watchlist_<date>.txt, one ticker
     per line. User curates this list 9:00-9:25 ET from any source they trust
     (thestockmarketwatch.com, Finviz, broker scanner, news feeds).

  2. --auto-scan — query Alpaca's free News API for top gainers with
     news today, and for each candidate fetch the prior daily close + current
     PM data to compute gap%. Filters: gap > 5%, price >= $1.50, PM volume
     >= 30K. Float check is skipped (Alpaca doesn't expose it).

Both modes write the same `state/plan_<date>.json` artifact, which the
trade_day orchestrator consumes. The plan contains per-ticker pre-market
context (PM high/low/volume, spread, EMAs) plus a Setup 1 eligibility flag.

CLI:
    py scripts/scan_premarket.py
    py scripts/scan_premarket.py --auto-scan
    py scripts/scan_premarket.py --watchlist AAPL,NVDA,AMD
    py scripts/scan_premarket.py --fake-now 09:20
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    STATE_DIR, et_now, et_today_iso, get_latest_quote, get_pm_bars,
    load_config, plan_path, safe_log_stdout, watchlist_path,
)
from signals import (  # noqa: E402
    build_setup1_plan, is_consolidating_near_pm_high, pm_summary, split_pm_rth,
    spread_ok,
)


def read_watchlist(date_iso: str, inline: str | None) -> list[str]:
    if inline:
        return [t.strip().upper() for t in inline.split(",") if t.strip()]
    p = watchlist_path(date_iso)
    if not p.exists():
        sys.exit(
            f"No watchlist for {date_iso} at {p}.\n"
            f"Either create that file (one ticker per line), pass --watchlist AAA,BBB, "
            f"or pass --auto-scan to auto-build one."
        )
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip().upper()
        if line and not line.startswith("#"):
            out.append(line.split()[0])  # tolerate "AAPL # note" style
    return out


def auto_scan_watchlist(cfg: dict, date_iso: str, fake_now: str | None) -> list[str]:
    """Build a watchlist via Alpaca's News API. Returns tickers with at least
    one news article from the last 18 hours and a non-trivial gap.
    Float and catalyst-quality filters are not applied — those are deferred
    to the user. Errs toward more candidates."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    try:
        from _common import load_alpaca_env  # type: ignore
        key, secret = load_alpaca_env(cfg)
    except SystemExit:
        sys.stderr.write("Auto-scan needs alpaca-py credentials. Falling back to empty list.\n")
        return []

    end = et_now(fake_now)
    start = end - timedelta(hours=18)
    client = NewsClient(key, secret)
    req = NewsRequest(start=start.astimezone(timezone.utc), limit=50,
                      sort="desc", include_content=False)
    try:
        resp = client.get_news(req)
    except Exception as exc:
        sys.stderr.write(f"News API call failed: {exc}\n")
        return []

    seen: dict[str, int] = {}
    items = getattr(resp, "data", None) or []
    if isinstance(items, dict):
        # NewsResponse-style
        items = items.get("news", [])
    for n in items:
        symbols = getattr(n, "symbols", None) or []
        for s in symbols:
            seen[s.upper()] = seen.get(s.upper(), 0) + 1
    return sorted(seen.keys(), key=lambda k: -seen[k])[:30]


def fetch_pm_context(symbols: list[str], cfg: dict, fake_now: str | None) -> dict[str, dict]:
    """For each symbol, pull today's PM 1-min bars plus latest quote from
    whichever data_provider is configured (alpaca | ibkr).
    Returns dict symbol -> context dict; missing/empty PM data yields {} entry."""
    if not symbols:
        return {}

    bars_by_sym = get_pm_bars(symbols, cfg, fake_now)
    quotes = get_latest_quote(symbols, cfg)

    out: dict[str, dict] = {}
    for sym in symbols:
        pm_only, _ = split_pm_rth(bars_by_sym.get(sym, []))
        ctx: dict = {"symbol": sym, "pm_bar_count": len(pm_only)}
        summary = pm_summary(pm_only)
        if summary:
            ctx.update(summary)
        q = quotes.get(sym) if isinstance(quotes, dict) else None
        if q is not None:
            ctx["bid"] = q.get("bid")
            ctx["ask"] = q.get("ask")
        ctx["_pm_bars"] = pm_only  # kept in-memory only; not serialised
        out[sym] = ctx
    return out


def build_plan(symbols: list[str], cfg: dict, fake_now: str | None) -> dict:
    contexts = fetch_pm_context(symbols, cfg, fake_now)
    plan: dict = {
        "date": et_today_iso(fake_now),
        "built_at_et": et_now(fake_now).isoformat(),
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
    for sym, ctx in contexts.items():
        pm_bars = ctx.pop("_pm_bars", [])
        row: dict = {"context": {k: v for k, v in ctx.items() if k != "_pm_bars"}}
        # Filters
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
                if not spread_ok(ctx["bid"], ctx["ask"], cfg["spread_max_cents"]):
                    rejections.append(f"spread_too_wide ({ctx['ask']-ctx['bid']:.2f})")
        row["rejections"] = rejections
        row["setup1_eligible"] = False
        row["setup1"] = None
        if not rejections and pm_high is not None:
            if is_consolidating_near_pm_high(
                pm_bars, pm_high,
                cfg["pm_consolidation_window_min"],
                cfg["pm_consolidation_band_pct"],
            ):
                row["setup1_eligible"] = True
                row["setup1"] = build_setup1_plan(sym, pm_high, ctx, cfg["take_profit_R"])
        plan["tickers"][sym] = row
    return plan


def write_plan(plan: dict) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    p = plan_path(plan["date"])
    p.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    return p


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--watchlist", default=None,
                   help="Comma-separated tickers (overrides file).")
    p.add_argument("--auto-scan", action="store_true",
                   help="Auto-build watchlist via Alpaca News API.")
    p.add_argument("--fake-now", default=None,
                   help="ET wall-clock for testing, HH:MM.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    date_iso = et_today_iso(args.fake_now)

    if args.auto_scan:
        symbols = auto_scan_watchlist(cfg, date_iso, args.fake_now)
        if not symbols:
            sys.exit("Auto-scan returned no candidates. Provide --watchlist or a file.")
        safe_log_stdout(f"Auto-scan picked {len(symbols)} tickers: {', '.join(symbols)}")
        watchlist_path(date_iso).write_text(
            "\n".join(symbols) + "\n", encoding="utf-8"
        )
    else:
        symbols = read_watchlist(date_iso, args.watchlist)

    if not symbols:
        sys.exit("Empty watchlist.")

    safe_log_stdout(f"Building plan for {len(symbols)} tickers...")
    plan = build_plan(symbols, cfg, args.fake_now)
    p = write_plan(plan)
    safe_log_stdout(f"Plan written: {p}")

    s1 = [t for t, row in plan["tickers"].items() if row["setup1_eligible"]]
    rej = [(t, row["rejections"]) for t, row in plan["tickers"].items()
           if row["rejections"]]
    safe_log_stdout(f"Setup 1 eligible: {len(s1)} -> {', '.join(s1) if s1 else '(none)'}")
    if rej:
        safe_log_stdout(f"Rejected ({len(rej)}):")
        for t, rs in rej:
            safe_log_stdout(f"  {t}: {', '.join(rs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
