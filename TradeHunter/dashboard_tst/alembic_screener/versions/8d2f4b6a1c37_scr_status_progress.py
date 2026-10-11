"""scr_status: error_kind, next_try, warn, universe_done, earnings_on, progress

The screener collector's heartbeat row grows the structured fields the page and the tray
read since v4.137 (the first-run fix): the active alert's kind and its retry time, a
non-pausing warning, the completion stamp of the last FULL universe walk (a streamed,
partial list never sets it), the ET day of the last earnings read (so a restart the same
day skips it) and a JSON progress block (universe pages, pass percent / ETA, stock days
and IV history left). All nullable: the web app migrates the DB before the collector
restarts, so an older collector keeps writing rows without them for a while.

Mirrors ``app/screener_models.ScrStatus``. Portable types only (SQLite dev, Postgres
later); ``batch_alter_table`` so the drop works on SQLite too.

Revision ID: 8d2f4b6a1c37
Revises: 5c7a1d3e9b20
Create Date: 2026-10-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "8d2f4b6a1c37"
down_revision = "5c7a1d3e9b20"
branch_labels = None
depends_on = None

COLUMNS = ("error_kind", "next_try", "warn", "universe_done", "earnings_on", "progress")


def upgrade() -> None:
    with op.batch_alter_table("scr_status", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error_kind", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("next_try", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("warn", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("universe_done", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("earnings_on", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("progress", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scr_status", schema=None) as batch_op:
        for name in reversed(COLUMNS):
            batch_op.drop_column(name)
