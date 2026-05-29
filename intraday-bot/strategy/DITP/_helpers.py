"""DITP — family-shared helpers.

Skeleton. Add helpers here as setups are wired and patterns repeat:
- watchlist path + loader (if DITP gets its own scanner)
- shared eligibility constants (price floors, ATR%, etc.)
- plan builders shared across setups

Reference doc: strategies-reference/DITP.md (in the worktree, not
TradeHunter/).

Keep DITP helpers separate from GUNS helpers. Per CLAUDE.md
"Never blend rules from multiple frameworks into one strategy file."
"""
from __future__ import annotations

# --- TradeHunter bootstrap ---
import sys
from pathlib import Path
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

# Placeholder constants — replace with values from
# strategies-reference/DITP.md §7 Stock screening criteria once
# the source material is filled in.
#
# Examples that will likely live here (mirrors strategy/GUNS/_helpers.py
# shape, but DITP's numbers will differ):
#
# MIN_PRICE = ...   # cheapest tradeable price
# MIN_VOLUME = ...  # premarket / RTH volume floor
# MAX_FLOAT = ...   # if DITP cares about low float
