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


@dataclass
class Strategy:
    """Strategy spec + behaviour. Build from a strategy module's `build(cfg)`."""
    # Identity
    name: str
    enabled: bool
    # Timing (ET wall-clocks, HH:MM)
    entry_et: str                # time the entry phase fires
    entry_cutoff_et: str         # after this, cancel unfilled entries
    # Sizing / risk caps
    take_profit_R: float
    max_concurrent: int          # per-strategy position cap
    # Optional strategy-specific kwargs (orb_minutes, lookback_bars, etc.)
    params: dict = field(default_factory=dict)
    # Behaviour callables
    pick_universe: PickUniverse = None  # type: ignore[assignment]
    fetch_bars:    FetchBars    = None  # type: ignore[assignment]
    evaluate:      Evaluate     = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return (f"Strategy(name={self.name!r}, enabled={self.enabled}, "
                f"entry_et={self.entry_et!r}, cap={self.max_concurrent}, "
                f"R={self.take_profit_R})")
