"""DITP Setup 4 (TC — Trend Continuation) — Day +1/+2 follow-through.

Source: strategies-reference/DITP.md §6 Setup 4 (capture began chat 2026-05-25).

**v0.1.0 — WATCH-ONLY skeleton.** Wires DITP TC into the orchestrator's
KNOWN_STRATEGIES list so it appears in the dashboard's Gating drawer,
flows into the Active Lists Candidates tab when ARMED, and starts
emitting journal events the Strategy Analysis drawer picks up.

It does NOT yet submit orders. evaluate() journals a `monitoring`
event per watchlist symbol per scheduled fire and returns None (no plan
→ no order), so ARM is operationally a no-op until the Phase 2 spec
(entry trigger / stop / TP / cautions) is taught and wired.

Pipeline:

  1. EOD on Day 0 (manually, or via post-EOD hook): `tc_scanner.py`
     walks today's P2 watchlist, filters to symbols that both broke out
     AND printed bullish Day-0, writes `state/watchlist_tc_<tomorrow>.json`.

  2. (TODO — Phase 2) Premarket on Day +1 at ~T-30 BMO: a premarket scanner
     reads the TC watchlist, validates the "premarket holds above Day-0
     high" rule, writes `state/shortlist_tc_<date>.json`. Strictness TBD
     (see DITP.md §6 Setup 4 "Eligibility 3").

  3. Day +1, at shortlist_et: do_shortlist() journals `watchlist_loaded`
     with per-tier counts. The watchlist itself was produced EOD Day 0.

  4. Day +1, at entry_et: pick_universe() returns TIER A+B (or as
     configured) symbols; evaluate() journals `monitoring` per symbol
     and returns None (no plan).

  5. (TODO — Phase 3) Once entry trigger + stop + TP are taught, evaluate()
     emits a plan-dict and the bot trades it via the existing bracket
     pipeline.

Config block (cfg.strategies.ditp_tc)
-------------------------------------
{
  "enabled": false,
  "shortlist_et": "09:00",
  "entry_et": "09:31",
  "entry_cutoff_et": "09:35",
  "max_concurrent": 3,
  "take_profit_R": 2.0,
  "params": {
    "min_tier": "C"
  }
}
"""
from __future__ import annotations

# --- intraday-bot bootstrap ---
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

STRATEGY_NAME = "ditp_tc"
TIER_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


def _watchlist_path(date_iso: str) -> Path:
    return STATE_DIR / f"watchlist_tc_{date_iso}.json"


def _load_watchlist(date_iso: str) -> dict | None:
    p = _watchlist_path(date_iso)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# Module-level cache so pick_universe → evaluate share one read per fire.
_CACHE: dict[tuple[str, float], dict] = {}


def _watchlist_cached(date_iso: str) -> dict | None:
    """Read the TC watchlist for `date_iso`. Fall back to most recent
    `watchlist_tc_*.json` if the exact-date file is missing — matches
    ditp_p2/impl.py's fallback behaviour."""
    p = _watchlist_path(date_iso)
    if p.exists():
        key = (date_iso, p.stat().st_mtime)
        if key in _CACHE:
            return _CACHE[key]
        blob = _load_watchlist(date_iso)
        if blob:
            _CACHE[key] = blob
            return blob
    candidates = sorted(STATE_DIR.glob("watchlist_tc_*.json"), reverse=True)
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


# ---------- Shortlist phase ----------

def do_shortlist(date_iso: str, cfg: dict, strat: Strategy) -> None:
    """Logging-only shortlist phase. The watchlist is produced EOD Day 0
    by strategy/DITP/tc_scanner.py; this fire journals what was found."""
    blob = _watchlist_cached(date_iso)
    if not blob:
        journal(STRATEGY_NAME, "watchlist_missing",
                expected_path=str(_watchlist_path(date_iso)),
                hint="run 'py strategy/DITP/tc_scanner.py' end-of-day to produce tomorrow's TC watchlist")
        return

    cands = blob.get("candidates", []) or []
    by_tier: dict[str, int] = {}
    by_event: dict[str, int] = {}
    for c in cands:
        t = c.get("p2_tier") or "?"
        by_tier[t] = by_tier.get(t, 0) + 1
        e = c.get("source_event") or "?"
        by_event[e] = by_event.get(e, 0) + 1

    journal(STRATEGY_NAME, "watchlist_loaded",
            date=date_iso,
            source_date=blob.get("source_date"),
            source_file=blob.get("source_file"),
            scanner_run_at_utc=blob.get("scanner_run_at_utc"),
            n_total=len(cands),
            n_tradeable=blob.get("n_tradeable_after_filter"),
            by_tier=by_tier,
            by_event=by_event,
            file=_watchlist_path(date_iso).name)


# ---------- Strategy callables ----------

def pick_universe(date_iso: str, cfg: dict) -> list[str]:
    """Entry phase universe = TIER A+B (default; configurable) symbols from
    the TC watchlist. Tier is INHERITED from the originating P2 candidate —
    a TC trade on a Tier-B P2 setup is still a Tier-B candidate.

    Configurable via `cfg.strategies.ditp_tc.params.min_tier` (default "C")."""
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
        tier = (c.get("p2_tier") or "D").upper()
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
    """No intraday bars needed for v0.1 watch-only. The Phase 3 entry
    pipeline will populate this once the trigger/stop/TP spec lands."""
    return {sym: [] for sym in symbols}


def evaluate(symbol: str, bars: list[dict], strat: Strategy) -> dict | None:
    """Watch-only evaluation. Journals a `monitoring` event with the
    Day-0 breakout facts + inherited P2 metadata so the dashboard's
    Strategy Analysis drawer can show TC activity alongside P2.
    Returns None — no plan submitted in v0.1.
    """
    blob = None
    for p in sorted(STATE_DIR.glob("watchlist_tc_*.json"), reverse=True):
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
            source_event=cand.get("source_event"),
            source_date=cand.get("source_date"),
            target_date=cand.get("target_date"),
            p2_tier=cand.get("p2_tier"),
            p2_variant=cand.get("p2_variant"),
            resistance=cand.get("resistance"),
            day0_close=cand.get("day0_close"),
            day0_close_position=cand.get("day0_close_position"),
            breakout_strength_atr=cand.get("breakout_strength_atr"),
            confluence_tier=cand.get("confluence_tier"),
            cautions=cand.get("cautions") or [],
            strategy_version=__version__,
            note="watch-only — Phase 2 (premarket validation) + Phase 3 (entry pipeline) not yet wired")
    return None   # no plan → no order, even when ARMED


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
