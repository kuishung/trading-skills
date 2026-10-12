"""scr_underlying_daily.iv30_src: where a day's IV30 came from

v4.138 (IV history from Massive's options day files): a day's IV30 can now come from three
places - ``live`` (the end-of-day pass, from the whole chain), ``flat`` (one options day
file for every stock at once, ``services/scr_flativ``) or ``rest`` (the per-stock REST
history, ``opt_massive.iv30_history``). The tag lets the flat-file builder fill only empty
days and redo its own (and REST) readings when Massive re-publishes a day, while a live
reading always wins. Existing rows stay NULL (filed before the tag existed: unknown, kept).

Mirrors ``app/screener_models.ScrUnderlyingDaily``. Portable types only (SQLite dev,
Postgres later); ``batch_alter_table`` so the drop works on SQLite too.

Revision ID: c3d9e5f1a7b2
Revises: b4e1f7c9d2a6
Create Date: 2026-10-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c3d9e5f1a7b2"
down_revision = "b4e1f7c9d2a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scr_underlying_daily", schema=None) as batch_op:
        batch_op.add_column(sa.Column("iv30_src", sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scr_underlying_daily", schema=None) as batch_op:
        batch_op.drop_column("iv30_src")
