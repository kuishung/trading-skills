"""scr_contract.bar_open / bar_high / bar_low / vwap: the contract's whole day bar

v4.225 (the closing snapshot): Massive's chain snapshot carries each contract's day bar (open,
high, low, close, volume, VWAP); the screener kept only the close (``last``) and the volume. The
user wants the daily snapshot to hold the prices AND the IV from the same end-of-day read (one
source, one moment), so the rest of the bar is stored too. NULL when the contract has not traded
in the session the pass reads (Massive's bar is then an older day's).

Mirrors ``app/screener_models.ScrContract``. Portable types only (SQLite dev, Postgres later);
``batch_alter_table`` so the drop works on SQLite too.

Revision ID: e5f7a9b1c3d6
Revises: d4e6f8a0b2c4
Create Date: 2026-10-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e5f7a9b1c3d6"
down_revision = "d4e6f8a0b2c4"
branch_labels = None
depends_on = None

COLS = ("bar_open", "bar_high", "bar_low", "vwap")


def upgrade() -> None:
    with op.batch_alter_table("scr_contract", schema=None) as batch_op:
        for c in COLS:
            batch_op.add_column(sa.Column(c, sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scr_contract", schema=None) as batch_op:
        for c in reversed(COLS):
            batch_op.drop_column(c)
