"""GUNS — Gap Up News Scalp — shared helpers.

Source: Adam Khoo Piranha Profits, Lesson 8 (2017). See
`GUNS Materials/Lesson 8-Gap Up News Scalp Strategy.pdf` and the
reference doc at ../strategies-reference/GUNS.md in the worktree.

GUNS is ONE universe (gap-up + news catalyst + low float + >= $1.50,
>= 30K PM volume) with FIVE entry setups. Each setup is a separate
strategy module that shares this universe via load_guns_watchlist().

What lives here:
  - Watchlist loading (state/watchlist_YYYY-MM-DD.txt)
  - Price-tier stop-loss table (from the PDF)
  - Plan-dict builder for a long buy-stop-limit GUNS entry
  - PM-high / PM-pivot extraction from 1-min bars

Watchlist file format
    state/watchlist_guns_YYYY-MM-DD.txt
    one ticker per line; '#' starts a comment line; blank lines ok.
    The "guns_" prefix is deliberate — each strategy family gets its
    own scanner + watchlist (see project memory on compartmentalization).
    Build it pre-market with `py scripts/guns_scanner.py`, which is a
    self-contained pipeline (no sibling-skill dependencies):
      1. IBKR ScannerSubscription (GUNS-tuned filters from the PDF)
      2. thestockmarketwatch.com top-gainers scrape
      3. Float filter (scripts/guns_float_lookup.py) — drops > 100M
      4. Catalyst classifier (scripts/guns_catalyst_classifier.py) —
         drops M&A, secondary offerings, dilution, going-concern,
         SEC actions, FDA rejections
    The output file is ready to trade; no manual pruning needed.

The bot itself enforces the price floor ($1.50) and PM-volume floor
(30K) as a defensive double-check inside each setup's evaluate().
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

from _common import STATE_DIR  # noqa: E402
from writer import journal  # noqa: E402  (journal/writer.py)


# ---------- Watchlist path ----------

def guns_watchlist_path(date_iso: str):
    """state/watchlist_guns_<date>.txt — GUNS-specific input file.

    Kept distinct from any other strategy family's watchlist so each
    family can have its own scanner with its own filter criteria.
    Same .gitignore pattern (watchlist_*.txt) covers it.
    """
    return STATE_DIR / f"watchlist_guns_{date_iso}.txt"


# ---------- Mandatory universe filters (per the PDF) ----------

MIN_PRICE = 1.50          # >= $1.50
MIN_PM_VOLUME = 30_000    # >= 30K shares pre-market


# ---------- Price-tier stop-loss table (cents) ----------
#
# PDF Stop-Loss Placement (Setup 1, 2, 3, 4, 5 — same table for all):
#   < $20      : 10-15 cents
#   $20 - $30  : 15-20 cents
#   $30 - $50  : 20-30 cents
#   $50 - $100 : 30-50 cents
#
# We pick the midpoint of each band as the default. > $100 extrapolates
# at 50 cents (the PDF's max), which is conservative — strategy-specific
# overrides can be set via the config block (`stop_cents_override`).

def price_tier_stop_cents(price: float) -> int:
    """Return SL distance in cents for `price`. Midpoints of PDF bands."""
    p = float(price)
    if p < 20:
        return 12
    if p < 30:
        return 17
    if p < 50:
        return 25
    if p < 100:
        return 40
    return 50


# ---------- Watchlist ----------

def load_guns_watchlist(date_iso: str, strategy_name: str) -> list[str]:
    """Read state/watchlist_<date>.txt and return ticker symbols.

    Returns [] if the file is missing or empty — caller treats that as
    'nothing to evaluate today'. We journal the outcome so the EOD report
    can attribute an empty universe to a missing watchlist vs. all-rejected.
    """
    path = guns_watchlist_path(date_iso)
    if not path.exists():
        journal(strategy_name, "watchlist_missing", path=str(path))
        return []
    symbols: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate extra columns (e.g. "TSLA  earnings beat") — first token wins.
        symbols.append(line.split()[0].upper())
    journal(strategy_name, "watchlist_loaded",
            path=str(path), n=len(symbols), symbols=symbols)
    return symbols


# ---------- Plan-dict builder (long-only — GUNS has no short setups) ----------

def build_long_buy_stop_limit_plan(
    *,
    strategy_name: str,
    symbol: str,
    trigger_price: float,         # stop trigger (e.g. PMH + $0.01)
    limit_cents_above_stop: int,  # max limit offset above trigger (PDF: 3-5)
    stop_distance_cents: int,     # SL distance below entry
    take_profit_R: float,         # R multiple for TP (PDF: 2.0 to 2.5)
    evidence: dict | None = None,
) -> dict:
    """Construct the plan-dict consumed by trade_day.submit_setup_entry().

    The TP is computed off `risk_per_share`, which is also what the
    position-sizer divides equity-risk by. BE move at +1R is handled by
    the orchestrator (poll_breakeven_moves).
    """
    stop_trigger = round(float(trigger_price), 2)
    limit_price = round(stop_trigger + limit_cents_above_stop / 100.0, 2)
    risk_per_share = round(stop_distance_cents / 100.0, 2)
    stop_loss = round(limit_price - risk_per_share, 2)
    take_profit = round(limit_price + take_profit_R * risk_per_share, 2)
    plan = {
        "strategy": strategy_name,
        "symbol": symbol,
        "side": "long",
        "entry_stop_trigger": stop_trigger,
        "entry_limit": limit_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": risk_per_share,
        "take_profit_R": take_profit_R,
    }
    if evidence:
        plan.update(evidence)
    return plan


# ---------- Premarket-structure helpers ----------

def pm_high_and_consolidation_range(
    pm_bars: list[dict],
    *,
    consol_lookback_min: int = 15,
) -> tuple[float | None, float | None, float | None]:
    """Inspect the last `consol_lookback_min` PM bars and return
    (pm_high_overall, consol_high, consol_low).

    `pm_high_overall` is max(high) over ALL pm bars supplied — this is
    the PMH for Setup 1.

    `consol_high` / `consol_low` are the high/low over just the last
    N bars (default 15 min). Setup 1's "consolidating very near PMH"
    condition is satisfied when (pm_high_overall - consol_high) is
    small relative to risk_per_share — caller decides the threshold.
    """
    if not pm_bars:
        return None, None, None
    pm_high = max(b["h"] for b in pm_bars)
    window = pm_bars[-consol_lookback_min:] if len(pm_bars) >= consol_lookback_min else pm_bars
    consol_high = max(b["h"] for b in window)
    consol_low = min(b["l"] for b in window)
    return pm_high, consol_high, consol_low


def pm_volume_total(pm_bars: list[dict]) -> int:
    return sum(b.get("v", 0) for b in pm_bars or [])
