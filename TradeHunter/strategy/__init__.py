"""Strategy layer -- the analysis units the bot fires.

Each strategy is a self-contained unit with:
  - own risk + sizing policy
  - own scanner / universe selection
  - own resource pulls (which resources/ modules it asks for)
  - own analysis logic
  - own conviction text written to the journal
  - own changelog.md tracking edits to its rules

Two independent LIVE runtime gates per strategy (managed by
scripts/_gating.py):
  ON/OFF  -- does the pipeline run at all?
  ARMED   -- do plans submit to Execution?

Layout:
  strategy/
    __init__.py       <- KNOWN_STRATEGIES + load_known() (this file)
    base.py           <- Strategy dataclass + interface
    signals.py        <- shared math helpers (EMA, position sizing, etc.)
    GUNS/             <- strategy family (Gap Up News Scalp)
      __init__.py     <- family package marker
      _helpers.py     <- GUNS-shared helpers (watchlist loader,
                         price-tier stops, plan builder)
      scanner.py      <- GUNS family pre-market scanner CLI
      guns_setup1/    <- one folder per strategy
        __init__.py   <- re-exports build(), __version__
        impl.py       <- pick_universe / fetch_bars / evaluate / build
        changelog.md  <- per-strategy version history
      guns_setup5/
        __init__.py
        impl.py
        changelog.md
    (future families: ORB/, DITP/, ...)
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

# --- TradeHunter bootstrap: make sibling layers importable as top-level
# modules + the TradeHunter root importable so the strategy package is
# accessible as `import strategy`. Walks up to find SKILL.md. ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution", "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

from base import Strategy  # noqa: E402  (resolved by the path injection above)


# Add a strategy by:
#   1. creating strategy/<FAMILY>/<setup_name>/impl.py with
#      build(cfg) -> Strategy
#   2. adding strategy/<FAMILY>/<setup_name>/__init__.py that re-exports
#      build via `from .impl import build, __version__`
#   3. listing the LEAF NAME here in KNOWN_STRATEGIES
#   4. adding the leaf-name -> FAMILY.<setup_name> dotted path mapping
#      to _STRATEGY_IMPORT_PATHS below
#
# The leaf name is what shows up everywhere user-facing -- state flag
# files (state/armed_<name>.flag), journal events, dashboard pills.
# The dotted path is only used internally for importlib.

KNOWN_STRATEGIES: tuple[str, ...] = (
    "guns_setup1",   # GUNS -- Break of Pre-Market High (09:30)
    "guns_setup5",   # GUNS -- Break of First 1-Minute Candle (09:31)
    "os_breakout",   # OS   -- Opening Surge breakout, fully automated (09:28)
    "ditp_p2",       # DITP -- P2 Pattern break of mountain resistance (watch-only v0.1)
    "ditp_tc",       # DITP -- Trend Continuation (Day +1/+2 follow-through, watch-only v0.1)
)


_STRATEGY_IMPORT_PATHS: dict[str, str] = {
    "guns_setup1": "GUNS.guns_setup1",
    "guns_setup5": "GUNS.guns_setup5",
    "os_breakout": "OS.os_breakout",
    "ditp_p2":     "DITP.ditp_p2",
    "ditp_tc":     "DITP.ditp_tc",
}


def _import_path_for(name: str) -> str:
    """Resolve leaf name -> dotted path inside the strategy package.
    Falls back to the leaf name itself for back-compat with the old
    flat layout."""
    return _STRATEGY_IMPORT_PATHS.get(name, name)


def load_known(cfg: dict) -> list[Strategy]:
    """Return every wired strategy (KNOWN_STRATEGIES) with a successful
    build(cfg). The config-level `enabled` flag is the FIRST-RUN default
    only -- the live ON/OFF state lives in state/enabled_<name>.flag and
    is consulted by the orchestrator at each scheduled fire.

    Modules that fail to import or whose build() raises are reported to
    stderr and skipped.
    """
    out: list[Strategy] = []
    for name in KNOWN_STRATEGIES:
        dotted = _import_path_for(name)
        try:
            mod = importlib.import_module(f"strategy.{dotted}")
        except Exception as exc:
            sys.stderr.write(f"[strategy] failed to import {name} "
                             f"(strategy.{dotted}): {exc}\n")
            continue
        if not hasattr(mod, "build"):
            sys.stderr.write(f"[strategy] {name} has no build(cfg) factory; skipping\n")
            continue
        try:
            strat = mod.build(cfg)
        except Exception as exc:
            sys.stderr.write(f"[strategy] {name}.build(cfg) raised: {exc}\n")
            continue
        if not isinstance(strat, Strategy):
            sys.stderr.write(f"[strategy] {name}.build did not return a Strategy; skipping\n")
            continue
        out.append(strat)
    return out


# Back-compat alias. The returned list is identical regardless of
# `enabled` -- the runtime gate has moved to state/enabled_<name>.flag.
load_enabled = load_known


__all__ = [
    "Strategy", "load_known", "load_enabled",
    "KNOWN_STRATEGIES", "_STRATEGY_IMPORT_PATHS",
]
