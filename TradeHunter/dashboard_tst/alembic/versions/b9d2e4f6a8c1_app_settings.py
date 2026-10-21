"""app_settings: key / value settings an administrator edits in the app (v4.193)

The Settings gear -> Integration page stores the Discord and Telegram connection details
here (webhook URL, bot token, channel and guild ids, Telegram bot token and default chat
id) so they can be set in the app instead of in app\\.env on Hermes; a value in this table
wins over the environment, a blank or missing row falls back to it. Portable types only.

Revision ID: b9d2e4f6a8c1
Revises: a7c3e91d5b24
Create Date: 2026-10-21
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b9d2e4f6a8c1"
down_revision = "a7c3e91d5b24"
branch_labels = None
depends_on = None

TABLE = "app_settings"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return
    op.create_table(
        TABLE,
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    if TABLE in _tables():
        op.drop_table(TABLE)
