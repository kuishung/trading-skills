"""Strategy registry — discovers strategy modules and instantiates the
enabled ones based on config.json.

Adding a new strategy:
  1. Create scripts/strategies/<name>.py exposing build(cfg) -> Strategy
  2. Add the name to KNOWN_STRATEGIES below
  3. Add an entry under cfg.strategies.<name> with `enabled: true` plus
     any per-strategy params (entry_et, take_profit_R, max_concurrent, ...)

trade_day.py calls load_enabled(cfg) once at startup and orchestrates
whatever comes back. The orchestrator itself doesn't know any specific
strategy by name — it only sees the Strategy interface.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Ensure scripts/ is on the path so strategy modules can find sibling
# _common, signals, _journal, etc.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from .base import Strategy  # re-export


KNOWN_STRATEGIES: tuple[str, ...] = (
    "orb_5min",
)


def load_enabled(cfg: dict) -> list[Strategy]:
    """Return the list of enabled strategies in priority order (config order).
    Modules that fail to import are reported to stderr and skipped."""
    out: list[Strategy] = []
    for name in KNOWN_STRATEGIES:
        try:
            mod = importlib.import_module(f"strategies.{name}")
        except Exception as exc:
            sys.stderr.write(f"[strategies] failed to import {name}: {exc}\n")
            continue
        if not hasattr(mod, "build"):
            sys.stderr.write(f"[strategies] {name} has no build(cfg) factory; skipping\n")
            continue
        try:
            strat = mod.build(cfg)
        except Exception as exc:
            sys.stderr.write(f"[strategies] {name}.build(cfg) raised: {exc}\n")
            continue
        if not isinstance(strat, Strategy):
            sys.stderr.write(f"[strategies] {name}.build did not return a Strategy; skipping\n")
            continue
        if strat.enabled:
            out.append(strat)
    return out


__all__ = ["Strategy", "load_enabled", "KNOWN_STRATEGIES"]
