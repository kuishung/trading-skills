"""GUNS Setup 1 — Break of the Pre-Market High.

When the PDF says fire this setup
--------------------------------
Pre-market price is *consolidating very near* the PMH. The trader
pre-stages a buy-stop-limit just above PMH; market open then either
fills it (breakout) or the stop-out triggers (failed break).

Mechanics
---------
Entry      Buy stop trigger at PMH + $0.01, limit 3-5 cents above.
Stop loss  Price-tier table (10-50c by price bracket).
TP         2R - 2.5R above entry (we use 2.0R as default).
BE move    At +1R, the orchestrator's poll_breakeven_moves handles this.

Eligibility checks we apply mechanically
----------------------------------------
1) Symbol is on today's watchlist file.
2) PM volume >= 30,000 shares (PDF minimum).
3) Last close (or PMH proxy) >= $1.50 (PDF minimum).
4) "Very near PMH" — the last 15 minutes of PM bars must trade within
   `consol_band_pct` of the PMH (default 1.5%). This is the closest
   mechanical proxy for the PDF's visual rule.

What we do NOT check (deliberately deferred)
--------------------------------------------
- News catalyst type. The PDF requires earnings/FDA/analyst/clinical
  pass and bans M&A. The watchlist upstream is expected to enforce
  this (the sibling intraday-premarket-brief skill does it).
- Float < 100M / ideal 10-20M. Same reason — upstream watchlist
  filter, not enforced here.
- "Big window to next resistance" — discretionary; we trust the
  watchlist curator.

Timing
------
The buy-stop-limit is PLACED at `entry_et = 09:28 ET` -- i.e., MORE
than 1 minute before market open -- so the order is already resting
in Alpaca's book when RTH opens at 09:30:00. We don't try to react
to the PMH break in real time; the broker's matching engine fires
the stop-limit the moment price hits PMH + $0.01 once RTH is live.

`time_in_force=DAY` means the order is dormant until 09:30:00 RTH
opens; even if PM price briefly touches PMH + $0.01 between 09:28
and 09:30, the order won't fire pre-market.

The unfilled order is cancelled at `entry_cutoff_et = 09:35 ET` --
if PMH didn't break in the first ~5 minutes of RTH the setup's edge
is considered gone.

Eligibility evaluation runs at 09:28 using PM bars 04:00 -> 09:28.
The last-15-min consolidation window is 09:13 -> 09:28. PMH may
move higher between 09:28 and 09:30, but that's fine -- it would
just make our trigger conservative (we'd already be above any new
PMH that forms in those 2 minutes if we used 09:28's PMH).

Config block (cfg.strategies.guns_setup1)
-----------------------------------------
{
  "enabled": true,
  "entry_et": "09:28",
  "entry_cutoff_et": "09:35",
  "max_concurrent": 2,
  "take_profit_R": 2.0,
  "params": {
    "consol_band_pct": 1.5,        # PM consolidation tolerance to PMH
    "consol_lookback_min": 15,     # window for consolidation check
    "limit_cents_above_stop": 5    # buy-stop-limit slippage cap
  }
}
"""
from __future__ import annotations

# --- TradeHunter bootstrap: make sibling layers importable ---
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

__version__ = "1.1.0"   # see README.md Changelog for history

import json
from datetime import datetime, timezone

from _common import STATE_DIR, get_pm_bars  # noqa: E402  (scripts/_common.py)
from writer import journal  # noqa: E402  (journal/writer.py)
from base import Strategy  # noqa: E402  (strategy/base.py)
from smw_premarket_movers import fetch_smw_premarket_movers  # noqa: E402  (resources/)
from .._helpers import (  # noqa: E402  (strategy/GUNS/_helpers.py)
    MIN_PM_VOLUME, MIN_PRICE,
    build_long_buy_stop_limit_plan,
    guns_watchlist_path,
    load_guns_watchlist,
    pm_high_and_consolidation_range,
    pm_volume_total,
    price_tier_stop_cents,
)

STRATEGY_NAME = "guns_setup1"


def shortlist_path(date_iso: str):
    """state/shortlist_guns_setup1_<date>.json — the merged candidate
    list built by the 09:00 ET shortlist phase, consumed by the 09:28
    entry phase."""
    return STATE_DIR / f"shortlist_{STRATEGY_NAME}_{date_iso}.json"


def _params(strat: Strategy) -> dict:
    p = strat.params or {}
    return {
        "consol_band_pct": float(p.get("consol_band_pct", 1.5)),
        "consol_lookback_min": int(p.get("consol_lookback_min", 15)),
        "limit_cents_above_stop": int(p.get("limit_cents_above_stop", 5)),
    }


# ---------- Shortlist phase (fires at shortlist_et = 09:00 ET) ----------

def do_shortlist(date_iso: str, cfg: dict, strat: Strategy) -> None:
    """T-30 BMO shortlist builder for Setup 1.

    Pulls candidates from TWO sources:
      1. GUNS scanner output — `state/watchlist_guns_<date>.txt` (the
         family pre-market scanner's filtered + curated list).
      2. stockmarketwatch.com/movers/premarket scrape via
         `resources/smw_premarket_movers.py` — live premarket movers
         filtered to gainers ≥ 5% within $1.50-$500.

    Merges by symbol (union, dedupe), writes the shortlist artifact
    `state/shortlist_guns_setup1_<date>.json`. The entry phase at
    09:28 reads this file via `pick_universe`.

    Best-effort: if either source returns nothing, we proceed with
    whatever the other source produced. If BOTH fail, the shortlist
    file is still written (empty `merged_symbols`) and the entry
    phase journals `universe_picked n=0`.
    """
    # Source 1: GUNS scanner watchlist file
    scanner_syms = load_guns_watchlist(date_iso, STRATEGY_NAME)
    # Source 2: SMW /movers/premarket scrape
    smw_rows = fetch_smw_premarket_movers(
        direction="gainers",
        min_change_pct=5.0,
        min_price=MIN_PRICE,
        max_price=500.0,
        max_rows=50,
    )
    smw_syms = [r["symbol"] for r in smw_rows]

    # Union; preserve order (scanner first, then SMW-only newcomers).
    merged: list[str] = []
    seen: set[str] = set()
    for s in scanner_syms + smw_syms:
        if s in seen:
            continue
        seen.add(s)
        merged.append(s)

    payload = {
        "date": date_iso,
        "strategy": STRATEGY_NAME,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "guns_scanner_watchlist": scanner_syms,
            "smw_premarket_movers": smw_rows,
        },
        "merged_symbols": merged,
        "n_merged": len(merged),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    shortlist_path(date_iso).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    journal(STRATEGY_NAME, "shortlist_built",
            n_scanner=len(scanner_syms),
            n_smw=len(smw_syms),
            n_merged=len(merged),
            sources_overlap=len(set(scanner_syms) & set(smw_syms)),
            symbols=merged)


# ---------- Strategy callables ----------

def pick_universe(date_iso: str, cfg: dict) -> list[str]:
    """Entry phase universe = whatever the shortlist phase wrote.

    Falls back to the raw GUNS scanner watchlist if the shortlist file
    is missing (e.g. shortlist phase didn't run -- bot started late,
    OFF at 09:00, etc.). This keeps Setup 1 functional even when the
    shortlist phase was skipped.
    """
    sp = shortlist_path(date_iso)
    if sp.exists():
        try:
            blob = json.loads(sp.read_text(encoding="utf-8"))
            syms = blob.get("merged_symbols") or []
            journal(STRATEGY_NAME, "shortlist_loaded",
                    path=str(sp), n=len(syms))
            return syms
        except (json.JSONDecodeError, OSError) as exc:
            journal(STRATEGY_NAME, "shortlist_load_failed",
                    path=str(sp), error=str(exc))
    # Fallback: raw scanner watchlist
    return load_guns_watchlist(date_iso, STRATEGY_NAME)


def fetch_bars(symbols: list[str], cfg: dict, strat: Strategy) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    return get_pm_bars(symbols, cfg, fake_now=None)


def evaluate(symbol: str, bars: list[dict], strat: Strategy) -> dict | None:
    """Return a long plan-dict if PM is consolidating near PMH; else None.

    Every rejection path is journaled with a `reason` so the EOD report
    can show why a watchlist symbol didn't fire.
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

    # Mandatory price / volume floors.
    if pmh < MIN_PRICE:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pmh_below_min_price", pmh=pmh, min_price=MIN_PRICE)
        return None
    pm_vol = pm_volume_total(bars)
    if pm_vol < MIN_PM_VOLUME:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="pm_volume_below_min",
                pm_volume=pm_vol, min_pm_volume=MIN_PM_VOLUME)
        return None

    # Consolidation tolerance: how far below PMH did the recent window
    # stay? PDF says "very near" — we approximate with a band %.
    gap_to_pmh_pct = (pmh - consol_high) / pmh * 100.0
    if gap_to_pmh_pct > p["consol_band_pct"]:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="not_consolidating_near_pmh",
                pmh=pmh, consol_high=consol_high,
                gap_pct=round(gap_to_pmh_pct, 2),
                tolerance_pct=p["consol_band_pct"])
        return None

    # Build the order parameters from the PDF's recipe.
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
    # Tag the plan with this strategy's version so journal events
    # downstream (planned, entry_submitted, entry_filled, exit_filled)
    # can attribute outcomes to a specific rule-set when review/stats.py
    # buckets the data.
    plan["strategy_version"] = __version__
    journal(STRATEGY_NAME, "planned", symbol=symbol, plan=plan)
    return plan


# ---------- Factory ----------

def build(cfg: dict) -> Strategy:
    block = (cfg.get("strategies") or {}).get(STRATEGY_NAME) or {}
    return Strategy(
        name=STRATEGY_NAME,
        enabled=bool(block.get("enabled", False)),
        # Shortlist phase fires 30 min BMO -- pulls scanner output +
        # smw /movers/premarket, writes shortlist artifact.
        shortlist_et=str(block.get("shortlist_et", "09:00")),
        # Order placement fires ≥1 min before RTH open.
        entry_et=str(block.get("entry_et", "09:28")),
        entry_cutoff_et=str(block.get("entry_cutoff_et", "09:35")),
        take_profit_R=float(block.get("take_profit_R", 2.0)),
        max_concurrent=int(block.get("max_concurrent", 2)),
        params=dict(block.get("params") or {}),
        pick_universe=pick_universe,
        fetch_bars=fetch_bars,
        evaluate=evaluate,
        shortlist=do_shortlist,
    )
