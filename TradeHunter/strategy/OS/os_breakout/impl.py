"""OS Breakout — Break of Pre-Market High at 09:30 ET, fully automated.

Source: strategies-reference/OS.md §6.

Mechanically the same shape as GUNS Setup 1 (pre-rested buy-stop-limit
at PMH+1¢, price-tier stop, 2R target) but:
  - universe comes from `strategy/OS/scanner.py` (IBKR TOP_PERC_GAIN);
    no catalyst classifier, no float cap
  - 0.5% per-trade risk vs GUNS's 1% (smaller bet per name to compensate
    for the wider, less-filtered universe)
  - time-based exit at 10:30 ET if not yet at +1R (BE move via existing
    poll_breakeven_moves handles the >1R case automatically)

Per CLAUDE.md, ALL thresholds are exposed via the config block so we
don't hardcode. Default state: ON + ARMED in paper, validated by 30
days of journal data before any consideration of live capital.

Config block (cfg.strategies.os_breakout)
-----------------------------------------
{
  "enabled": true,
  "shortlist_et": "09:00",
  "entry_et": "09:28",
  "entry_cutoff_et": "09:35",
  "max_concurrent": 3,
  "take_profit_R": 2.0,
  "params": {
    "consol_band_pct": 1.5,
    "consol_lookback_min": 15,
    "limit_cents_above_stop": 5,
    "scanner_rows": 50,
    "scanner_min_change_pct": 3.0,
    "scanner_min_avg_volume": 200000
  }
}
"""
from __future__ import annotations

# --- TradeHunter bootstrap ---
import sys
from pathlib import Path
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution", "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

__version__ = "1.0.0"

import json
from datetime import datetime, timezone

from _common import STATE_DIR, get_pm_bars  # noqa: E402
from writer import journal  # noqa: E402
from base import Strategy  # noqa: E402
from .._helpers import (  # noqa: E402  (strategy/OS/_helpers.py)
    OS_MIN_PM_VOLUME, OS_MIN_PRICE, OS_MAX_PRICE,
    build_long_buy_stop_limit_plan,
    load_os_watchlist,
    pm_high_and_consolidation_range,
    pm_volume_total,
    price_tier_stop_cents,
)

STRATEGY_NAME = "os_breakout"


def shortlist_path(date_iso: str):
    """state/shortlist_os_breakout_<date>.json — written by the 09:00
    shortlist phase, consumed by the 09:28 entry phase."""
    return STATE_DIR / f"shortlist_{STRATEGY_NAME}_{date_iso}.json"


def _params(strat: Strategy) -> dict:
    p = strat.params or {}
    return {
        "consol_band_pct": float(p.get("consol_band_pct", 1.5)),
        "consol_lookback_min": int(p.get("consol_lookback_min", 15)),
        "limit_cents_above_stop": int(p.get("limit_cents_above_stop", 5)),
        "scanner_rows": int(p.get("scanner_rows", 50)),
        "scanner_min_change_pct": float(p.get("scanner_min_change_pct", 3.0)),
        "scanner_min_avg_volume": int(p.get("scanner_min_avg_volume", 200_000)),
        # Kill-switch for the IBKR scanner API call. When False, the
        # shortlist phase skips the scan entirely and produces an empty
        # watchlist. Per user rule 2026-05-23: *"we take out the OS
        # strategy first. do not remove the code, just disable the scan
        # API request for this strategy"*. The OS code paths stay
        # functional — re-enable by setting `scan_enabled: true` under
        # cfg.strategies.os_breakout.params. Default False reflects the
        # current "OS is taken out" stance so re-enabling is explicit.
        "scan_enabled": bool(p.get("scan_enabled", False)),
    }


# ---------- Shortlist phase (fires at shortlist_et = 09:00 ET) ----------

def do_shortlist(date_iso: str, cfg: dict, strat: Strategy) -> None:
    """T-30 BMO shortlist builder. Runs `strategy.OS.scanner.build_os_watchlist`
    to pull IBKR's TOP_PERC_GAIN, then persists both the .txt watchlist
    AND a per-setup shortlist .json for the entry phase to load.

    Best-effort: scanner failures land an empty shortlist + a journal
    event. The entry phase will then evaluate against zero symbols
    rather than crash.

    When `params.scan_enabled` is False (default True in config.example,
    currently set False per user rule 2026-05-23), the IBKR scanner call
    is skipped entirely — no API request, no watchlist file. A
    `shortlist_skipped` journal event records the reason for audit.
    """
    p = _params(strat)
    if not p["scan_enabled"]:
        journal(STRATEGY_NAME, "shortlist_skipped",
                reason="scan_enabled=False — IBKR scanner call disabled by config")
        syms: list[str] = []
    else:
        try:
            from strategy.OS.scanner import build_os_watchlist, write_os_watchlist
            syms = build_os_watchlist(
                rows=p["scanner_rows"],
                min_change_pct=p["scanner_min_change_pct"],
                min_avg_volume=p["scanner_min_avg_volume"],
                cfg=cfg,
            )
            write_os_watchlist(syms, date_iso)
        except Exception as exc:
            journal(STRATEGY_NAME, "shortlist_failed", error=str(exc))
            syms = []

    payload = {
        "date": date_iso,
        "strategy": STRATEGY_NAME,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {"ibkr_top_perc_gain": syms},
        "merged_symbols": syms,
        "n_merged": len(syms),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    shortlist_path(date_iso).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    journal(STRATEGY_NAME, "shortlist_built",
            n_scanner=len(syms),
            n_merged=len(syms),
            symbols=syms)


# ---------- Strategy callables ----------

def pick_universe(date_iso: str, cfg: dict) -> list[str]:
    """Entry phase universe = the shortlist JSON, with a fallback to the
    raw .txt watchlist file if the JSON is missing (e.g., shortlist
    phase didn't run because bot started late or was OFF at 09:00)."""
    sp = shortlist_path(date_iso)
    if sp.exists():
        try:
            blob = json.loads(sp.read_text(encoding="utf-8"))
            syms = blob.get("merged_symbols") or []
            journal(STRATEGY_NAME, "shortlist_loaded", path=str(sp), n=len(syms))
            return syms
        except (json.JSONDecodeError, OSError) as exc:
            journal(STRATEGY_NAME, "shortlist_load_failed",
                    path=str(sp), error=str(exc))
    return load_os_watchlist(date_iso, STRATEGY_NAME)


def fetch_bars(symbols: list[str], cfg: dict, strat: Strategy) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    return get_pm_bars(symbols, cfg, fake_now=None)


def evaluate(symbol: str, bars: list[dict], strat: Strategy) -> dict | None:
    """Return a long plan-dict if PM is consolidating near PMH; else None.

    Every rejection path is journaled with a `reason`.
    """
    p = _params(strat)
    if not bars:
        journal(STRATEGY_NAME, "rejected", symbol=symbol, reason="no_pm_bars")
        return None

    pmh, consol_high, _consol_low = pm_high_and_consolidation_range(
        bars, consol_lookback_min=p["consol_lookback_min"]
    )
    if pmh is None or consol_high is None:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pm_structure_unavailable")
        return None

    # Mandatory price band (OS is intentionally narrower than GUNS — the
    # scanner already pre-filters by abovePrice/belowPrice, but a defensive
    # double-check catches edge cases where the scanner returned a name
    # that drifted outside the band by 09:28).
    if pmh < OS_MIN_PRICE:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pmh_below_min_price",
                pmh=pmh, min_price=OS_MIN_PRICE)
        return None
    if pmh > OS_MAX_PRICE:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pmh_above_max_price",
                pmh=pmh, max_price=OS_MAX_PRICE)
        return None

    pm_vol = pm_volume_total(bars)
    if pm_vol < OS_MIN_PM_VOLUME:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pm_volume_below_min",
                pm_volume=pm_vol, min_pm_volume=OS_MIN_PM_VOLUME)
        return None

    # PM consolidation tolerance — within consol_band_pct of PMH.
    gap_to_pmh_pct = (pmh - consol_high) / pmh * 100.0
    if gap_to_pmh_pct > p["consol_band_pct"]:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="not_consolidating_near_pmh",
                pmh=pmh, consol_high=consol_high,
                gap_pct=round(gap_to_pmh_pct, 2),
                tolerance_pct=p["consol_band_pct"])
        return None

    trigger_price = round(pmh + 0.01, 2)
    stop_cents = price_tier_stop_cents(trigger_price)
    plan = build_long_buy_stop_limit_plan(
        strategy_name=STRATEGY_NAME,
        symbol=symbol,
        trigger_price=trigger_price,
        limit_cents_above_stop=p["limit_cents_above_stop"],
        stop_distance_cents=stop_cents,
        take_profit_R=strat.take_profit_R,
        evidence={
            "pmh": pmh,
            "pm_volume": pm_vol,
            "consol_high": consol_high,
            "gap_to_pmh_pct": round(gap_to_pmh_pct, 2),
            "stop_cents": stop_cents,
        },
    )
    plan["strategy_version"] = __version__
    journal(STRATEGY_NAME, "planned", symbol=symbol, plan=plan)
    return plan


# ---------- Factory ----------

def build(cfg: dict) -> Strategy:
    block = (cfg.get("strategies") or {}).get(STRATEGY_NAME) or {}
    return Strategy(
        name=STRATEGY_NAME,
        enabled=bool(block.get("enabled", True)),
        shortlist_et=str(block.get("shortlist_et", "09:00")),
        entry_et=str(block.get("entry_et", "09:28")),
        entry_cutoff_et=str(block.get("entry_cutoff_et", "09:35")),
        take_profit_R=float(block.get("take_profit_R", 2.0)),
        max_concurrent=int(block.get("max_concurrent", 3)),
        params=dict(block.get("params") or {}),
        pick_universe=pick_universe,
        fetch_bars=fetch_bars,
        evaluate=evaluate,
        shortlist=do_shortlist,
    )
