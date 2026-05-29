"""DITP Setup 1 (P2 Pattern) — Break of Pre-Existing Mountain Resistance.

Source: strategies-reference/DITP.md §6 (Setup 1, sub-setups A / B / C).

**v0.1.0 — WATCH-ONLY skeleton.** Wires DITP P2 into the orchestrator's
KNOWN_STRATEGIES list so it appears in the dashboard's Gating drawer,
flows into the Active Lists Candidates tab when ARMED, and starts
emitting journal events the Strategy Analysis drawer picks up.

It does NOT yet submit orders. evaluate() journals a `monitoring`
event per watchlist symbol per scheduled fire and returns None
(no plan → no order), so ARM is operationally a no-op until the Step 3
intraday anti-pattern monitor lands. Reasoning: the user's teaching
explicitly deferred trade execution + sizing — *"we execution of trade
and size position we deal with it later. now we focus on the pattern
first"*. Wiring the strategy into the orchestrator without entry logic
gives the dashboard everything it needs to show DITP status without
risking premature order submission.

Pipeline:

  1. EOD (manually, or via post-EOD hook): `strategy/DITP/scanner.py`
     scans the daily-parquet universe → writes
     `state/watchlist_ditp_<tomorrow>.json`.

  2. Next day, at shortlist_et: do_shortlist() journals
     `watchlist_loaded` with the per-tier counts. The watchlist itself
     was already produced — this is a logging step, no recomputation.

  3. At entry_et: pick_universe() reads the watchlist's TIER A + B
     symbols (D is excluded as low-conviction); evaluate() journals
     `monitoring` per symbol carrying the resistance level + variant +
     cautions; returns None.

  4. Step 3 (NEXT version 0.2.0, after the 5 anti-patterns are wired):
     evaluate() will read live 3m bars, run the anti-pattern detectors
     against each watchlist symbol's resistance, emit a `fading_signal`
     when any of the 5 patterns fires, and (separately, when a clean
     break triggers) return a plan-dict to submit a buy-stop-limit.

Config block (cfg.strategies.ditp_p2)
-------------------------------------
{
  "enabled": false,
  "shortlist_et": "09:00",
  "entry_et": "09:31",
  "entry_cutoff_et": "09:35",
  "max_concurrent": 3,
  "take_profit_R": 2.0,
  "params": {
    "min_tier": "B"
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

__version__ = "0.1.0"

import json

from _common import STATE_DIR  # noqa: E402
from writer import journal  # noqa: E402
from base import Strategy  # noqa: E402

STRATEGY_NAME = "ditp_p2"
TIER_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


def _watchlist_path(date_iso: str) -> Path:
    return STATE_DIR / f"watchlist_ditp_{date_iso}.json"


def _load_watchlist(date_iso: str) -> dict | None:
    """Read state/watchlist_ditp_<date>.json. None if file missing or unreadable."""
    p = _watchlist_path(date_iso)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# Module-level cache so pick_universe → evaluate share one read of the
# watchlist JSON per scheduled fire. Cache key is (date_iso, mtime).
_CACHE: dict[tuple[str, float], dict] = {}


def _watchlist_cached(date_iso: str) -> dict | None:
    """Read the watchlist for `date_iso`. If that exact file is missing,
    fall back to the most recent `watchlist_ditp_*.json` available — covers
    the common case where the EOD scanner produced a watchlist targeting a
    different date (e.g. ran Friday EOD for Monday, but the bot dry-runs on
    Saturday) and the late-start case (scanner didn't run last night)."""
    p = _watchlist_path(date_iso)
    if p.exists():
        key = (date_iso, p.stat().st_mtime)
        if key in _CACHE:
            return _CACHE[key]
        blob = _load_watchlist(date_iso)
        if blob:
            _CACHE[key] = blob
            return blob
    # Fallback: most recent watchlist on disk.
    candidates = sorted(STATE_DIR.glob("watchlist_ditp_*.json"), reverse=True)
    for p2 in candidates:
        key = (p2.name, p2.stat().st_mtime)
        if key in _CACHE:
            return _CACHE[key]
        try:
            blob = json.loads(p2.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if blob:
            _CACHE[key] = blob
            return blob
    return None


def _candidate_by_symbol(blob: dict, sym: str) -> dict | None:
    if not blob:
        return None
    for c in blob.get("candidates", []):
        if (c.get("symbol") or "").upper() == sym.upper():
            return c
    return None


# ---------- Shortlist phase (runs at shortlist_et) ----------

def do_shortlist(date_iso: str, cfg: dict, strat: Strategy) -> None:
    """Logging-only shortlist phase. The watchlist itself is produced
    end-of-day by strategy/DITP/scanner.py; this fire just journals
    what was found so the dashboard's Strategy Analysis drawer sees a
    `watchlist_loaded` event tagged `strategy.ditp_p2.*`.
    """
    blob = _watchlist_cached(date_iso)
    if not blob:
        journal(STRATEGY_NAME, "watchlist_missing",
                expected_path=str(_watchlist_path(date_iso)),
                hint="run 'py strategy/DITP/scanner.py' end-of-day to produce tomorrow's watchlist")
        return

    cands = blob.get("candidates", []) or []
    by_tier: dict[str, int] = {}
    for c in cands:
        t = c.get("tier") or "?"
        by_tier[t] = by_tier.get(t, 0) + 1

    journal(STRATEGY_NAME, "watchlist_loaded",
            date=date_iso,
            scanner_run_at_utc=blob.get("scanner_run_at_utc"),
            n_total=len(cands),
            by_tier=by_tier,
            file=str(_watchlist_path(date_iso).name))


# ---------- Strategy callables ----------

def pick_universe(date_iso: str, cfg: dict) -> list[str]:
    """Entry phase universe = TIER A + B symbols from the DITP P2 watchlist.

    D is excluded (write_watchlist already drops D from the .txt; we apply
    the same rule here). C is included by default but the user can tighten
    via `cfg.strategies.ditp_p2.params.min_tier`.
    """
    block = (cfg.get("strategies") or {}).get(STRATEGY_NAME) or {}
    params = block.get("params") or {}
    min_tier = str(params.get("min_tier", "C")).upper()
    cutoff = TIER_RANK.get(min_tier, TIER_RANK["C"])

    blob = _watchlist_cached(date_iso)
    if not blob:
        journal(STRATEGY_NAME, "shortlist_load_failed",
                reason="watchlist_missing",
                path=str(_watchlist_path(date_iso)))
        return []

    syms: list[str] = []
    for c in blob.get("candidates", []) or []:
        sym = (c.get("symbol") or "").upper()
        tier = (c.get("tier") or "D").upper()
        if not sym:
            continue
        if TIER_RANK.get(tier, 9) > cutoff:
            continue
        syms.append(sym)

    journal(STRATEGY_NAME, "shortlist_loaded",
            n=len(syms),
            min_tier=min_tier,
            symbols=syms)
    return syms


def fetch_bars(symbols: list[str], cfg: dict, strat: Strategy) -> dict[str, list[dict]]:
    """No intraday bars needed for the v0.1 watch-only mode. The Step 3
    intraday monitor (v0.2+) will populate this with 3m bars from
    bars_store + a live feed."""
    return {sym: [] for sym in symbols}


def evaluate(symbol: str, bars: list[dict], strat: Strategy) -> dict | None:
    """Watch-only evaluation. Journals a `monitoring` event carrying the
    candidate's resistance + variant + tier + cautions so the dashboard's
    Strategy Analysis drawer shows DITP activity. Returns None
    deliberately: no plan submitted until Step 3 anti-pattern detection
    is wired in v0.2.
    """
    # date_iso is implicit via the most recent watchlist file — pick
    # by scanning the state dir for whichever watchlist contains this
    # symbol. Cheap because we only run for the actual universe.
    blob = None
    for p in sorted(STATE_DIR.glob("watchlist_ditp_*.json"), reverse=True):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
            if blob and _candidate_by_symbol(blob, symbol):
                break
        except (json.JSONDecodeError, OSError):
            continue

    cand = _candidate_by_symbol(blob, symbol) if blob else None
    if not cand:
        journal(STRATEGY_NAME, "rejected", symbol=symbol,
                reason="not_in_watchlist")
        return None

    journal(STRATEGY_NAME, "monitoring",
            symbol=symbol,
            tier=cand.get("tier"),
            variant=cand.get("variant"),
            resistance=cand.get("resistance"),
            resistance_low=cand.get("resistance_low"),
            distance_atr=cand.get("distance_atr"),
            cautions=cand.get("cautions") or [],
            strategy_version=__version__,
            note="watch-only — Step 3 anti-pattern detection not yet wired")
    return None   # no plan → no order submission, even when ARMED


# ---------- Factory ----------

def build(cfg: dict) -> Strategy:
    block = (cfg.get("strategies") or {}).get(STRATEGY_NAME) or {}
    return Strategy(
        name=STRATEGY_NAME,
        enabled=bool(block.get("enabled", False)),
        shortlist_et=str(block.get("shortlist_et", "09:00")),
        entry_et=str(block.get("entry_et", "09:31")),
        entry_cutoff_et=str(block.get("entry_cutoff_et", "09:35")),
        take_profit_R=float(block.get("take_profit_R", 2.0)),
        max_concurrent=int(block.get("max_concurrent", 3)),
        params=dict(block.get("params") or {}),
        pick_universe=pick_universe,
        fetch_bars=fetch_bars,
        evaluate=evaluate,
        shortlist=do_shortlist,
    )
