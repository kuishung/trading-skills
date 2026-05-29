"""OS family-shared helpers.

Per CLAUDE.md "Never blend rules from multiple frameworks into one
strategy file", OS does NOT import from strategy/GUNS/_helpers.py. The
primitives below mirror what GUNS provides but live in OS's namespace so
edits here don't affect GUNS and vice versa.

Reference doc: strategies-reference/OS.md.

Public:
    OS_MIN_PRICE, OS_MAX_PRICE, OS_MIN_PM_VOLUME
    os_watchlist_path(date_iso)
    load_os_watchlist(date_iso, strategy_name)
    price_tier_stop_cents(price)
    pm_high_and_consolidation_range(bars, consol_lookback_min)
    pm_volume_total(bars)
    build_long_buy_stop_limit_plan(...)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# --- TradeHunter bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

from writer import journal  # noqa: E402  (journal/writer.py)


STATE_DIR = SKILL_DIR / "state"

# Universe / eligibility floors (see strategies-reference/OS.md §7).
OS_MIN_PRICE = 1.50
OS_MAX_PRICE = 50.00
OS_MIN_PM_VOLUME = 100_000


def os_watchlist_path(date_iso: str) -> Path:
    """state/watchlist_os_<date>.txt — written by strategy/OS/scanner.py."""
    return STATE_DIR / f"watchlist_os_{date_iso}.txt"


def load_os_watchlist(date_iso: str, strategy_name: str) -> list[str]:
    """Read the OS scanner output. Lines may be SYM or SYM<tab>... — take
    the first token. Missing file -> empty list + journal event."""
    p = os_watchlist_path(date_iso)
    if not p.exists():
        journal(strategy_name, "watchlist_missing", path=str(p))
        return []
    out: list[str] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sym = line.split()[0].upper()
            if sym:
                out.append(sym)
    except OSError as exc:
        journal(strategy_name, "watchlist_load_failed",
                path=str(p), error=str(exc))
        return []
    return out


# ---------- Price-tier stop distance ----------

def price_tier_stop_cents(price: float) -> int:
    """Stop distance in cents by price bracket. Mirrors the GUNS table —
    deliberately the same numbers in v1.0 because the underlying logic
    (low-float liquid intraday names) is similar. Will be ATR-normalized
    in v1.1 per CLAUDE.md's Option A rule."""
    if price < 2.0:
        return 12
    if price < 5.0:
        return 17
    if price < 10.0:
        return 25
    if price < 25.0:
        return 40
    return 50


# ---------- PM bar analytics ----------

def pm_volume_total(bars: list[dict]) -> int:
    """Sum of v across all PM bars (04:00 -> 09:30 ET)."""
    return int(sum(int(b.get("v") or 0) for b in (bars or [])))


def pm_high_and_consolidation_range(bars: list[dict], *,
                                    consol_lookback_min: int = 15
                                    ) -> tuple[float | None, float | None, float | None]:
    """Return (PMH, consol_high, consol_low) over the PM bars.

    PMH = max(high) over the whole PM window.
    Consol high/low = the last `consol_lookback_min` bars' range.
    Returns (None, None, None) if bars are missing or malformed.
    """
    if not bars:
        return None, None, None
    try:
        pmh = max(float(b["h"]) for b in bars)
    except (KeyError, ValueError, TypeError):
        return None, None, None
    tail = bars[-consol_lookback_min:] if len(bars) >= consol_lookback_min else bars
    try:
        consol_high = max(float(b["h"]) for b in tail)
        consol_low = min(float(b["l"]) for b in tail)
    except (KeyError, ValueError, TypeError):
        return pmh, None, None
    return pmh, consol_high, consol_low


# ---------- Plan builder ----------

def build_long_buy_stop_limit_plan(*,
                                   strategy_name: str,
                                   symbol: str,
                                   trigger_price: float,
                                   limit_cents_above_stop: int,
                                   stop_distance_cents: int,
                                   take_profit_R: float,
                                   evidence: dict) -> dict:
    """Build a long buy-stop-limit plan dict the orchestrator can consume.

    `trigger_price` -- where the stop fires (PMH + 1¢).
    `limit_cents_above_stop` -- max slippage to pay (limit = trigger + N¢).
    `stop_distance_cents` -- per-share risk distance for SL placement.
    `take_profit_R` -- multiple of `stop_distance` to set TP above entry.
    `evidence` -- diag dict appended to the plan for journaling.
    """
    entry_stop = round(float(trigger_price), 2)
    entry_limit = round(entry_stop + (limit_cents_above_stop / 100.0), 2)
    risk_per_share = round(stop_distance_cents / 100.0, 2)
    stop_loss = round(entry_stop - risk_per_share, 2)
    take_profit = round(entry_stop + take_profit_R * risk_per_share, 2)

    return {
        "strategy": strategy_name,
        "symbol": symbol.upper(),
        "side": "long",
        "entry_stop_trigger": entry_stop,
        "entry_limit": entry_limit,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": risk_per_share,
        "take_profit_R": float(take_profit_R),
        **evidence,
    }


__all__ = [
    "OS_MIN_PRICE", "OS_MAX_PRICE", "OS_MIN_PM_VOLUME",
    "os_watchlist_path", "load_os_watchlist",
    "price_tier_stop_cents",
    "pm_high_and_consolidation_range", "pm_volume_total",
    "build_long_buy_stop_limit_plan",
]
