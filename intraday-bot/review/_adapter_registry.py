"""Maps strategy names → backtest adapter classes.

Mirrors strategy/__init__.py::_STRATEGY_IMPORT_PATHS. Adding a new
backtestable strategy = adding one line here AND shipping the adapter file
at the corresponding dotted path.

Why a registry instead of auto-discovery: explicit registration makes the
list of "what's backtestable today" greppable in one place, prevents
accidental inclusion of half-finished adapters, and gives a clean CLI
--list-strategies output. Trade-off: tiny manual edit when adding a
strategy. Acceptable.
"""
from __future__ import annotations

import importlib
from typing import Any


# (module_dotted_path, class_name) per strategy name.
# Strategy names are lower-snake-case and MUST match adapter.name attribute.
_ADAPTERS: dict[str, tuple[str, str]] = {
    "ditp_p2": ("strategy.DITP.ditp_p2.backtest_adapter", "DITPP2BacktestAdapter"),
    # Future:
    # "guns_setup1": ("strategy.GUNS.setup1.backtest_adapter", "GunsSetup1BacktestAdapter"),
    # "guns_setup5": ("strategy.GUNS.setup5.backtest_adapter", "GunsSetup5BacktestAdapter"),
    # "os_breakout": ("strategy.OS.os_breakout.backtest_adapter", "OSBreakoutBacktestAdapter"),
}


def known() -> list[str]:
    """Sorted list of registered strategy names. Used by CLI --list-strategies."""
    return sorted(_ADAPTERS.keys())


def load(name: str, **kwargs) -> Any:
    """Import + instantiate the adapter for `name`. kwargs are forwarded
    to the adapter's __init__ (e.g., `universe=[...]` for narrow runs).

    Raises KeyError with a helpful message if `name` isn't registered, or
    ImportError if the adapter module / class can't be loaded.
    """
    if name not in _ADAPTERS:
        raise KeyError(
            f"unknown strategy {name!r}; registered: {known()}. "
            f"Add an entry to _ADAPTERS in {__file__} to enable backtesting."
        )
    mod_path, cls_name = _ADAPTERS[name]
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as exc:
        raise ImportError(
            f"strategy {name!r}: failed to import adapter module {mod_path!r}: {exc}"
        ) from exc
    if not hasattr(mod, cls_name):
        raise ImportError(
            f"strategy {name!r}: module {mod_path!r} has no class {cls_name!r}"
        )
    cls = getattr(mod, cls_name)
    return cls(**kwargs)
