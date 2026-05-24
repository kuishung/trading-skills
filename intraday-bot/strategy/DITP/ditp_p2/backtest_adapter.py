"""DITP P2 backtest adapter — wires the family's decision engine + scanner
into the generic harness defined by review/_strategy_adapter.py.

Source: strategies-reference/DITP.md §6 Setup 1.

This file is the ONLY DITP-specific code the backtest harness sees.
Everything strategy-mechanical lives in:
  - strategy/DITP/scanner.py           (candidate selection — daily-bar scan)
  - strategy/DITP/_decision_engine.py  (entry / stop / target / tradeability)

This adapter exists so that:
  1. The harness (review/backtest.py) stays strategy-agnostic
  2. When ditp_p2/impl.py v0.2.0 wires live execution, it imports the same
     _decision_engine module — guaranteeing backtest and live use identical
     decision math
  3. Future DITP setups (P3 retest, etc.) get their own adapter file in
     their own setup folder; this one stays focused on Setup 1 (P2)
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Sequence

# --- intraday-bot bootstrap (same shape as scanner.py) ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import bars_store  # noqa: E402  (resources/bars_store.py)

# The decision engine + scanner — the only DITP-specific imports.
from strategy.DITP import _decision_engine as de  # noqa: E402
from strategy.DITP.scanner import scan_universe, P2Config  # noqa: E402


class DITPP2BacktestAdapter:
    """Implements review._strategy_adapter.BacktestAdapter for DITP P2.

    Phase 1 adapter: bare-bracket math only — no momentum gate, no 1m
    confirmation, no anti-patterns, no trailing stop, no add-to-winner.
    Phases 2-4 will land additional optional hooks here as
    `_decision_engine.py` gains the corresponding primitives.
    """

    name = "ditp_p2"
    engine_version = de.__version__   # propagates "0.1.0" from _decision_engine
    primary_timeframe = "3min"        # P2 entry trigger lives on the 3-min chart

    def __init__(self, cfg: P2Config | None = None,
                 universe: list[str] | None = None) -> None:
        self.cfg = cfg or P2Config()
        # If caller pins a universe (debug / smoke), use it; otherwise
        # the scanner walks every symbol with a daily parquet.
        self._universe_override = universe

    # ---- Required Protocol surface ----

    def pick_candidates(self, as_of_date: date) -> Sequence[dict]:
        """Run the DITP P2 scanner as-of the given date and return the
        Tier-1+ confluence-filtered list as plain dicts. The scanner's
        `as_of_date` parameter is the look-ahead guard — see
        strategy/DITP/scanner.py::detect_p2() docstring.
        """
        universe = (self._universe_override
                    if self._universe_override is not None
                    else bars_store.list_symbols("daily"))
        cands = scan_universe(
            universe, self.cfg, {"A", "B", "C"}, as_of_date=as_of_date
        )
        # Tradeability filter from scanner v0.2-alpha1 (commit d3d3be5):
        # drop D-tier AND Tier-0 confluence. Same rule the .txt watchlist uses.
        tradeable = [c for c in cands if c.tier != "D" and c.confluence_tier > 0]
        return [self._candidate_to_dict(c) for c in tradeable]

    def entry_signal(self, candidate: dict, curr_bar: dict,
                     prev_bar: dict | None,
                     bars_so_far: list[dict]) -> bool:
        """3-min close above daily resistance, first-crossing only.
        Phase 1: no EMA / 1m confirmation / anti-pattern filters."""
        if prev_bar is None:
            return False
        return de.entry_signal(
            curr_close=curr_bar["c"],
            prev_close=prev_bar["c"],
            resistance=candidate["daily_R"],
        )

    def stop_price(self, candidate: dict, entry: float) -> float:
        return de.stop_price(entry=entry, atr_daily=candidate["atr_used"])

    def target_price(self, candidate: dict, entry: float) -> float:
        return de.target_price(entry=entry, atr_daily=candidate["atr_used"])

    def tradeability_ok(self, candidate: dict, entry: float,
                        stop: float, target: float) -> bool:
        return de.tradeability_ok(
            entry=entry, target=target, atr_daily=candidate["atr_used"]
        )

    # ---- Internal helpers ----

    @staticmethod
    def _candidate_to_dict(c) -> dict:
        """Flatten a P2Candidate to a plain dict. Top-level keys are the
        harness-required minimum (symbol, atr_used) plus the entry rule's
        own input (daily_R). Everything else is preserved verbatim so the
        backtest summary can bucket by tier / variant / confluence / etc.
        """
        return {
            # Required by the harness contract
            "symbol":   c.symbol,
            "atr_used": float(c.atr14),
            # Required by THIS adapter's decision engine
            "daily_R":  float(c.resistance),
            # Strategy-specific metadata — preserved as candidate_meta on
            # the trade dict for per-bucket cuts in the metrics module
            "scanner_tier":       c.tier,
            "variant":            c.variant,
            "confluence_tier":    c.confluence_tier,
            "confluence_reasons": list(c.confluence_reasons or []),
            "cautions":           list(c.cautions or []),
            "resistance_low":     float(c.resistance_low),
            "yesterday_high":     float(c.yesterday_high),
            "yesterday_low":      float(c.yesterday_low),
            "yesterday_close":    float(c.yesterday_close),
            "score":              c.score,
        }
