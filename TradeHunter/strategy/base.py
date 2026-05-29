"""Strategy interface — the contract every entry strategy implements.

A strategy is a struct of:
  - identity/config (name, enabled flag, timing, caps)
  - 3 callables (pick_universe, fetch_bars, evaluate)

The orchestrator (trade_day.py) loads enabled strategies from
config.json -> strategies.{name}, calls pick_universe at the strategy's
entry_et, then fetches bars and evaluates each symbol. A non-None plan
dict triggers a stop-limit + OCO submission. All trades carry the
strategy name in TradeRecord.strategy_name so per-strategy P&L is
attributable downstream.

Strategy modules MUST expose a `build(cfg: dict) -> Strategy` factory.
That factory reads cfg.strategies.<name> and returns a fully-populated
Strategy. The orchestrator never inspects strategy internals beyond
this interface.

Plan dict contract (what evaluate returns):
    {
      "symbol":              str,
      "side":                "long" | "short",
      "entry_stop_trigger":  float,
      "entry_limit":         float,
      "stop_loss":           float,
      "take_profit":         float,
      "risk_per_share":      float,
      ...                    # any strategy-specific evidence fields
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# Type aliases for clarity.
PickUniverse = Callable[[str, dict], list[str]]                 # (date_iso, cfg) -> symbols
FetchBars    = Callable[[list[str], dict, "Strategy"], dict[str, list[dict]]]
Evaluate     = Callable[[str, list[dict], "Strategy"], dict | None]
# Optional shortlist hook -- called once at shortlist_et (pre-entry_et).
# Receives (date_iso, cfg, strategy) and is expected to write a shortlist
# artifact under state/ that the entry phase will then consume. Free to
# emit journal events.
Shortlist    = Callable[[str, dict, "Strategy"], None]


@dataclass
class Strategy:
    """Strategy spec + behaviour. Build from a strategy module's `build(cfg)`."""
    # Identity
    name: str
    enabled: bool
    # Timing (ET wall-clocks, HH:MM)
    entry_et: str                # time the entry phase fires
    entry_cutoff_et: str         # after this, cancel unfilled entries
    # Optional shortlist phase that fires earlier than entry_et.
    # When set, the orchestrator calls strategy.shortlist() at this time
    # so the strategy can gather candidates from external sources and
    # write a state/shortlist_<name>_<date>.json artifact. The entry phase
    # at entry_et then reads that artifact. Leave None for strategies
    # that don't need a separate shortlist phase.
    shortlist_et: str | None = None
    # Sizing / risk caps
    take_profit_R: float = 0.0
    max_concurrent: int = 0       # per-strategy position cap
    # Optional strategy-specific kwargs (orb_minutes, lookback_bars, etc.)
    params: dict = field(default_factory=dict)
    # Behaviour callables
    pick_universe: PickUniverse = None  # type: ignore[assignment]
    fetch_bars:    FetchBars    = None  # type: ignore[assignment]
    evaluate:      Evaluate     = None  # type: ignore[assignment]
    shortlist:     Shortlist | None = None   # optional

    def __repr__(self) -> str:
        sl = f", shortlist_et={self.shortlist_et!r}" if self.shortlist_et else ""
        return (f"Strategy(name={self.name!r}, enabled={self.enabled}, "
                f"entry_et={self.entry_et!r}{sl}, cap={self.max_concurrent}, "
                f"R={self.take_profit_R})")
