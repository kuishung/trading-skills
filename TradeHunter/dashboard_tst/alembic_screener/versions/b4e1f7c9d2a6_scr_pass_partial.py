"""scr_pass.partial: a pass read on a partial list of optionable stocks

v4.137 (the first-run fix): on a first start the universe walk is streamed and the first
market pass reads the stocks filed so far. When the walk fails and waits for its resume,
that pass ends "finished" on a partial list. Until now only the collector's memory knew it
was partial, so a restart took such an end-of-day pass as the session's full one and the
rest of the list was never read for that session. ``partial`` stores the mark on the row:
True = read on a partial list, False = a complete list, NULL (every older row) = complete.

Mirrors ``app/screener_models.ScrPass``. Portable types only (SQLite dev, Postgres later);
``batch_alter_table`` so the drop works on SQLite too.

Revision ID: b4e1f7c9d2a6
Revises: 8d2f4b6a1c37
Create Date: 2026-10-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b4e1f7c9d2a6"
down_revision = "8d2f4b6a1c37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scr_pass", schema=None) as batch_op:
        batch_op.add_column(sa.Column("partial", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scr_pass", schema=None) as batch_op:
        batch_op.drop_column("partial")
