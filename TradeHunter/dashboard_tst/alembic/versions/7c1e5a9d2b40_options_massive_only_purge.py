"""options: purge every non-Massive row (data only, no schema change)

v4.135 removed the Options page and its screener; only the Massive data pipeline
stays, so the option tables keep only data read from Massive:

* ``opt_quote``, ``opt_refresh_log``, ``option_chain_snapshot`` - rows whose
  ``source`` is not ``massive`` (the v4.133 IBKR rows ``hermes`` / ``member``, the v1
  ``ibkr`` / ``cboe`` snapshots) are deleted.
* ``opt_underlying_daily`` - EVERY row is deleted. A day row is updated field by field
  and relabelled ``massive`` by the last writer, so a row Massive touched can still
  carry an IBKR IV30 under a ``massive`` label; the only way to be sure the history is
  Massive's alone is to rebuild it. ``opt_underlying.history_done`` is reset, so the
  Hermes collector rebuilds each basket ticker's 2 years of bars and 1 year of IV30
  from Massive on its next start (``opt_massive.backfill_history``).
* ``opt_underlying`` - the derived statistics (ATR, HV, average volume, IV30 and its
  rank) are cleared, to be recomputed from the rebuilt history; a spot not read from
  Massive is cleared. The earnings date (Yahoo, the one non-Massive figure by design)
  and the row itself stay.
* ``iv_daily`` and ``option_signal`` (the v1 Options module's per-day header and card
  cache, all from IBKR / CBOE) are emptied.

Member records (``option_trades``, ``option_trade_checks``), the basket
(``option_basket``, the collector's universe), the collector heartbeat and the
legacy IV screener's ``iv_history`` are not touched. Portable SQLAlchemy core
expressions only (SQLite dev, Postgres prod). The downgrade is a no-op: deleted
data cannot be restored.

Revision ID: 7c1e5a9d2b40
Revises: 3b26d60468a0
Create Date: 2026-10-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "7c1e5a9d2b40"
down_revision = "3b26d60468a0"
branch_labels = None
depends_on = None

MASSIVE = "massive"
SOURCED = ("opt_quote", "opt_refresh_log", "option_chain_snapshot")   # delete source != massive
EMPTIED = ("opt_underlying_daily", "iv_daily", "option_signal")       # delete every row
STATS = ("atr14", "hv20", "hv60", "avg_vol20", "bars_as_of", "iv30", "iv_rank", "iv_pct",
         "iv_n", "iv_lo", "iv_hi", "iv_as_of")
SPOT = ("spot", "spot_as_of", "spot_source", "spot_user_id", "spot_mdt")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _tables()
    for name in SOURCED:
        if name in have:
            t = sa.table(name, sa.column("source", sa.String))
            op.execute(sa.delete(t).where(sa.or_(t.c.source.is_(None), t.c.source != MASSIVE)))
    for name in EMPTIED:
        if name in have:
            op.execute(sa.delete(sa.table(name)))
    if "opt_underlying" in have:
        u = sa.table("opt_underlying", sa.column("history_done", sa.Boolean),
                     *(sa.column(c) for c in STATS + SPOT))
        op.execute(sa.update(u).values(history_done=False, **{c: None for c in STATS}))
        op.execute(sa.update(u)
                   .where(sa.or_(u.c.spot_source.is_(None), u.c.spot_source != MASSIVE))
                   .values(**{c: None for c in SPOT}))


def downgrade() -> None:
    """Nothing to undo: the purge deleted data, the schema is unchanged."""
