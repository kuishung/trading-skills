"""GUNS strategy family — Gap Up News Scalp.

Source: Adam Khoo Piranha Profits Lesson 8.
Reference doc: strategies-reference/GUNS.md.

ONE universe (gap-up + news catalyst + low float + price>=$1.50 +
PM-volume>=30K) with FIVE entry setups. This family wires setups 1
and 5; setups 2/3/4 are deferred (setup 4 needs a rolling 09:30-10:30
watch window the current single-fire entry_et model does not support).

Family layout:
  GUNS/
    __init__.py     (this file -- package marker)
    _helpers.py     (family-shared helpers: watchlist loader,
                     price-tier stop table, long-plan builder,
                     PM-high/consolidation extractor)
    scanner.py      (family pre-market scanner CLI -- writes
                     state/watchlist_guns_<date>.txt)
    guns_setup1/    (Break of Pre-Market High at 09:30)
    guns_setup5/    (Break of First 1-Minute RTH Candle at 09:31)
"""
